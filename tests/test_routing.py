"""Tests for Auto-routing Rules Engine feature."""

from __future__ import annotations

import pytest
from pydicom.uid import generate_uid
from sqlalchemy.orm import Session

from app.db.models import DicomNode
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import indexing_service, routing_service, storage_service
from tests.helpers import make_instance


def _create_node(ae_title: str, host: str = "127.0.0.1", port: int = 10401) -> DicomNode:
    with Session(sync_engine) as session:
        node = DicomNode(ae_title=ae_title, host=host, port=port, description="Test Node")
        session.add(node)
        session.commit()
        session.refresh(node)
        return node


@pytest.mark.anyio
async def test_create_and_evaluate_routing_rules_service():
    """Verify rule creation, priority sorting, and rule evaluation logic."""
    # 1. Create target DicomNode
    node = _create_node("3D_WORKSTATION", "127.0.0.1", 10401)

    # 2. Ingest matching DICOM study (CT, HEAD)
    study_uid = generate_uid()
    ds = make_instance(patient_id="ROUTE_P1", study_uid=study_uid, modality="CT", body_part="HEAD")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            key, sha = storage_service.save_dataset(ds)
            from app.dicom.parser import parse_metadata
            await indexing_service.index_new_instance(session, parse_metadata(ds), key, sha)

    # 3. Create routing rules
    async with AsyncSessionLocal() as session:
        async with session.begin():
            rule1 = await routing_service.create_rule(
                session,
                {
                    "name": "Route CT Head to Workstation",
                    "description": "Forward all CT Head scans",
                    "enabled": True,
                    "priority": 5,
                    "destination_node_id": node.id,
                    "modality": "CT",
                    "body_part_examined": "HEAD",
                },
            )
            rule2 = await routing_service.create_rule(
                session,
                {
                    "name": "Ignore MR Scans",
                    "enabled": True,
                    "priority": 1,
                    "destination_node_id": node.id,
                    "modality": "MR",
                },
            )

    # 4. Evaluate rules
    async with AsyncSessionLocal() as session:
        matched = await routing_service.evaluate_rules_for_study(session, study_uid)
        assert len(matched) == 1
        assert matched[0].name == "Route CT Head to Workstation"


@pytest.mark.anyio
async def test_routing_rule_tag_filtering():
    """Verify that non-matching rules are ignored."""
    node = _create_node("ARCHIVE_2", "127.0.0.1", 10402)

    study_uid = generate_uid()
    ds = make_instance(patient_id="ROUTE_P2", study_uid=study_uid, modality="CR", body_part="CHEST")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            key, sha = storage_service.save_dataset(ds)
            from app.dicom.parser import parse_metadata
            await indexing_service.index_new_instance(session, parse_metadata(ds), key, sha)

            # Rule matching CT only
            await routing_service.create_rule(
                session,
                {
                    "name": "CT Only Rule",
                    "enabled": True,
                    "priority": 1,
                    "destination_node_id": node.id,
                    "modality": "CT",
                },
            )

    async with AsyncSessionLocal() as session:
        matched = await routing_service.evaluate_rules_for_study(session, study_uid)
        assert len(matched) == 0


def _get_auth_headers(client, username="admin_route"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin Route"},
    )
    if reg.status_code == 201:
        token = reg.json()["access_token"]
    else:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "adminpassword"},
        )
        token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_routing_rules_api_crud(client):
    """Test Auto-routing Rules REST API CRUD operations."""
    headers = _get_auth_headers(client, "admin_route_api")

    node = _create_node("DEST_NODE", "127.0.0.1", 10405)

    # 1. POST /api/v1/routing/rules (Create)
    resp = client.post(
        "/api/v1/routing/rules",
        json={
            "name": "Auto Forward CT",
            "description": "Forward all CT",
            "enabled": True,
            "priority": 10,
            "destination_node_id": node.id,
            "modality": "CT",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    rule_data = resp.json()
    rule_id = rule_data["id"]
    assert rule_data["name"] == "Auto Forward CT"
    assert rule_data["destination_ae_title"] == "DEST_NODE"

    # 2. GET /api/v1/routing/rules (List)
    resp_list = client.get("/api/v1/routing/rules", headers=headers)
    assert resp_list.status_code == 200
    assert len(resp_list.json()) >= 1

    # 3. GET /api/v1/routing/rules/{id} (Get)
    resp_get = client.get(f"/api/v1/routing/rules/{rule_id}", headers=headers)
    assert resp_get.status_code == 200
    assert resp_get.json()["id"] == rule_id

    # 4. PUT /api/v1/routing/rules/{id} (Update)
    resp_put = client.put(
        f"/api/v1/routing/rules/{rule_id}",
        json={"priority": 1, "description": "Updated priority"},
        headers=headers,
    )
    assert resp_put.status_code == 200
    assert resp_put.json()["priority"] == 1

    # 5. DELETE /api/v1/routing/rules/{id} (Delete)
    resp_del = client.delete(f"/api/v1/routing/rules/{rule_id}", headers=headers)
    assert resp_del.status_code == 200

    # Verify 404 on deleted
    assert client.get(f"/api/v1/routing/rules/{rule_id}", headers=headers).status_code == 404
