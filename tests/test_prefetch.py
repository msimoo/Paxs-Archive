"""Tests for Prefetching Engine feature."""

from __future__ import annotations

import pytest
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, DicomNode, PrefetchJob, PrefetchRule
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import prefetch_service


def _create_node(ae_title: str, host: str = "127.0.0.1", port: int = 10410) -> DicomNode:
    with Session(sync_engine) as session:
        node = DicomNode(ae_title=ae_title, host=host, port=port, description="Archive Node for Prefetching")
        session.add(node)
        session.commit()
        session.refresh(node)
        return node


@pytest.mark.anyio
async def test_evaluate_and_trigger_prefetch_service():
    """Verify prefetching evaluation queries remote nodes and creates PrefetchJob."""
    node = _create_node("ARCHIVE_PREFETCH", "127.0.0.1", 10410)

    # 1. Create rule
    async with AsyncSessionLocal() as session:
        async with session.begin():
            rule = await prefetch_service.create_rule(
                session,
                {
                    "name": "Prefetch CT priors",
                    "enabled": True,
                    "priority": 1,
                    "modality": "CT",
                    "max_prior_age_days": 365,
                    "max_priors_count": 2,
                    "source_node_id": node.id,
                },
            )
            assert rule.id is not None

    # 2. Trigger prefetch
    async with AsyncSessionLocal() as session:
        async with session.begin():
            jobs = await prefetch_service.evaluate_and_trigger_prefetch(
                session, patient_id="PREFETCH_P1", modality="CT"
            )
            assert len(jobs) == 1
            assert jobs[0].patient_id == "PREFETCH_P1"
            assert jobs[0].status in ("COMPLETED", "FAILED")

    # 3. Check Audit Log
    with Session(sync_engine) as session:
        audits = session.execute(
            select(AuditLog).where(AuditLog.performed_by.like("prefetch:%"))
        ).scalars().all()
        assert len(audits) >= 1


def _get_auth_headers(client, username="admin_pft"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin Prefetch"},
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


def test_prefetch_api_crud(client):
    """Test Prefetching Engine REST API endpoints."""
    headers = _get_auth_headers(client, "admin_pft_api")
    node = _create_node("REMOTE_VNA", "127.0.0.1", 10411)

    # 1. POST /api/v1/prefetch/rules (Create)
    resp = client.post(
        "/api/v1/prefetch/rules",
        json={
            "name": "MR Priors Rule",
            "description": "Fetch MR priors within 2 years",
            "enabled": True,
            "priority": 5,
            "modality": "MR",
            "max_prior_age_days": 730,
            "max_priors_count": 3,
            "source_node_id": node.id,
        },
        headers=headers,
    )
    assert resp.status_code == 201
    rule_data = resp.json()
    rule_id = rule_data["id"]
    assert rule_data["modality"] == "MR"

    # 2. GET /api/v1/prefetch/rules (List)
    resp_list = client.get("/api/v1/prefetch/rules", headers=headers)
    assert resp_list.status_code == 200
    assert len(resp_list.json()) >= 1

    # 3. GET /api/v1/prefetch/rules/{id} (Get)
    resp_get = client.get(f"/api/v1/prefetch/rules/{rule_id}", headers=headers)
    assert resp_get.status_code == 200
    assert resp_get.json()["id"] == rule_id

    # 4. PUT /api/v1/prefetch/rules/{id} (Update)
    resp_put = client.put(
        f"/api/v1/prefetch/rules/{rule_id}",
        json={"max_priors_count": 5},
        headers=headers,
    )
    assert resp_put.status_code == 200
    assert resp_put.json()["max_priors_count"] == 5

    # 5. POST /api/v1/prefetch/trigger (Trigger)
    resp_trig = client.post(
        "/api/v1/prefetch/trigger",
        json={"patient_id": "PAT_TRIG_1", "modality": "MR"},
        headers=headers,
    )
    assert resp_trig.status_code == 200
    assert "jobs" in resp_trig.json()

    # 6. GET /api/v1/prefetch/jobs (List jobs)
    resp_jobs = client.get("/api/v1/prefetch/jobs", headers=headers)
    assert resp_jobs.status_code == 200
    assert len(resp_jobs.json()) >= 1

    # 7. DELETE /api/v1/prefetch/rules/{id} (Delete)
    resp_del = client.delete(f"/api/v1/prefetch/rules/{rule_id}", headers=headers)
    assert resp_del.status_code == 200
