"""Tests for Critical Results Tracking clinical workflow feature."""

from __future__ import annotations

import pytest
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, CriticalResult
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import critical_results_service, indexing_service, storage_service
from tests.helpers import ds_bytes, make_instance


@pytest.mark.anyio
async def test_create_and_acknowledge_critical_result_service():
    """Verify flagging and acknowledging a critical finding via service."""
    study_uid = generate_uid()
    ds = make_instance(patient_id="CRIT_P1", study_uid=study_uid)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            key, sha = storage_service.save_dataset(ds)
            from app.dicom.parser import parse_metadata
            await indexing_service.index_new_instance(session, parse_metadata(ds), key, sha)

    # 1. Create critical result
    async with AsyncSessionLocal() as session:
        async with session.begin():
            res = await critical_results_service.create_critical_result(
                session,
                study_instance_uid=study_uid,
                description="Massive tension pneumothorax right lung",
                reported_by="dr.rad",
                finding_level="CRITICAL_RED",
                communicated_to="dr.er",
                communication_channel="PHONE",
            )
            res_id = res.id
            assert res.status == "PENDING_ACK"
            assert res.patient_id == "CRIT_P1"

    # 2. Acknowledge critical result
    async with AsyncSessionLocal() as session:
        async with session.begin():
            ack_res = await critical_results_service.acknowledge_critical_result(
                session,
                result_id=res_id,
                acknowledged_by="dr.er",
                notes="Patient prepped for chest tube insertion",
            )
            assert ack_res.status == "ACKNOWLEDGED"
            assert ack_res.acknowledged_by == "dr.er"
            assert ack_res.acknowledged_at is not None

    # Verify audit logs
    with Session(sync_engine) as session:
        audits = session.execute(
            select(AuditLog).where(AuditLog.study_instance_uid == study_uid)
        ).scalars().all()
        actions = {a.action for a in audits}
        assert "CRITICAL_RESULT_FLAGGED" in actions
        assert "CRITICAL_RESULT_ACKNOWLEDGED" in actions


@pytest.mark.anyio
async def test_list_critical_results_filtering():
    """Verify filtering critical results by status and finding level."""
    study1 = generate_uid()
    study2 = generate_uid()

    async with AsyncSessionLocal() as session:
        async with session.begin():
            r1 = await critical_results_service.create_critical_result(
                session, study1, "Tension pneumothorax", "rad1", finding_level="CRITICAL_RED", patient_id="P_RED"
            )
            r2 = await critical_results_service.create_critical_result(
                session, study2, "Mild pulmonary edema", "rad2", finding_level="URGENT_ORANGE", patient_id="P_ORANGE"
            )
            await critical_results_service.acknowledge_critical_result(session, r1.id, "er1")

    async with AsyncSessionLocal() as session:
        # Filter by PENDING_ACK status for study2
        pending = await critical_results_service.list_critical_results(
            session, study_instance_uid=study2, status="PENDING_ACK"
        )
        assert len(pending) == 1
        assert pending[0].patient_id == "P_ORANGE"

        # Filter by ACKNOWLEDGED status for study1
        acked = await critical_results_service.list_critical_results(
            session, study_instance_uid=study1, status="ACKNOWLEDGED"
        )
        assert len(acked) == 1
        assert acked[0].patient_id == "P_RED"

        # Filter by finding level
        reds = await critical_results_service.list_critical_results(
            session, study_instance_uid=study1, finding_level="CRITICAL_RED"
        )
        assert len(reds) == 1
        assert reds[0].patient_id == "P_RED"


def _get_auth_headers(client, username="admin_cr"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin CR"},
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


def test_critical_results_api(client):
    """Test Critical Results REST API endpoints."""
    headers = _get_auth_headers(client, "dr_rad_api")
    study_uid = generate_uid()

    # 1. POST /api/v1/critical-results (Flag result)
    resp = client.post(
        "/api/v1/critical-results",
        json={
            "study_instance_uid": study_uid,
            "description": "Acute intracranial hemorrhage",
            "finding_level": "CRITICAL_RED",
            "communicated_to": "Dr. Neuro",
            "communication_channel": "PHONE",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    res_data = resp.json()
    res_id = res_data["id"]
    assert res_data["status"] == "PENDING_ACK"
    assert res_data["description"] == "Acute intracranial hemorrhage"

    # 2. GET /api/v1/critical-results/{id}
    resp_get = client.get(f"/api/v1/critical-results/{res_id}", headers=headers)
    assert resp_get.status_code == 200
    assert resp_get.json()["id"] == res_id

    # 3. GET /api/v1/critical-results/study/{study_uid}
    resp_study = client.get(f"/api/v1/critical-results/study/{study_uid}", headers=headers)
    assert resp_study.status_code == 200
    assert len(resp_study.json()) == 1

    # 4. POST /api/v1/critical-results/{id}/acknowledge
    resp_ack = client.post(
        f"/api/v1/critical-results/{res_id}/acknowledge",
        json={"notes": "OR team notified for decompression"},
        headers=headers,
    )
    assert resp_ack.status_code == 200
    assert resp_ack.json()["status"] == "ACKNOWLEDGED"
    assert resp_ack.json()["acknowledged_by"] == "dr_rad_api"
    assert resp_ack.json()["acknowledgement_notes"] == "OR team notified for decompression"

    # 5. GET /api/v1/critical-results with status=ACKNOWLEDGED
    resp_list = client.get(f"/api/v1/critical-results?status=ACKNOWLEDGED&study_instance_uid={study_uid}", headers=headers)
    assert resp_list.status_code == 200
    assert len(resp_list.json()) == 1
