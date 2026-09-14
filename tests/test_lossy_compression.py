"""Tests for Lossy Compression Engine feature."""

from __future__ import annotations

import pytest
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Instance
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import indexing_service, lossy_compression_service, storage_service
from tests.helpers import ds_bytes, make_instance


@pytest.mark.anyio
async def test_transcode_dataset_lossy_service():
    """Verify in-memory transcoding of DICOM dataset to lossy JPEG."""
    ds = make_instance(patient_id="LOSSY_P1", modality="CT")
    orig_bytes_len = len(ds_bytes(ds))

    transcoded, new_len = lossy_compression_service.transcode_dataset_lossy(
        ds, target_ts_uid="1.2.840.10008.1.2.4.50", quality_factor=75
    )

    assert transcoded.file_meta.TransferSyntaxUID == "1.2.840.10008.1.2.4.50"
    assert transcoded.PhotometricInterpretation == "YBR_FULL_422"
    assert new_len > 0


@pytest.mark.anyio
async def test_evaluate_and_run_lossy_compression():
    """Verify rule evaluation transcodes matching stored study instances."""
    study_uid = generate_uid()
    ds = make_instance(patient_id="LOSSY_P2", study_uid=study_uid, modality="CT")

    # Ingest study
    async with AsyncSessionLocal() as session:
        async with session.begin():
            key, sha = storage_service.save_dataset(ds)
            from app.dicom.parser import parse_metadata
            await indexing_service.index_new_instance(session, parse_metadata(ds), key, sha)

    # Create lossy rule (min_age_days=0)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await lossy_compression_service.create_rule(
                session,
                {
                    "name": "Compress CT immediately",
                    "enabled": True,
                    "priority": 1,
                    "min_age_days": 0,
                    "modality": "CT",
                    "quality_factor": 80,
                },
            )

    # Run lossy engine run
    async with AsyncSessionLocal() as session:
        async with session.begin():
            res = await lossy_compression_service.evaluate_and_run_lossy_compression(session)
            assert res["processed"] >= 1

    # Verify DB instance update
    with Session(sync_engine) as session:
        inst = session.execute(
            select(Instance).where(Instance.sop_instance_uid == ds.SOPInstanceUID)
        ).scalar_one()
        assert inst.transfer_syntax_uid == "1.2.840.10008.1.2.4.50"

        # Check Audit Log
        audits = session.execute(
            select(AuditLog).where(AuditLog.sop_instance_uid == ds.SOPInstanceUID)
        ).scalars().all()
        actions = {a.action for a in audits}
        assert "LOSSY_COMPRESSED" in actions


def _get_auth_headers(client, username="admin_lossy"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin Lossy"},
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


def test_lossy_compression_api_crud(client):
    """Test Lossy Compression Engine REST API endpoints."""
    headers = _get_auth_headers(client, "admin_lossy_api")

    # 1. POST /api/v1/compression/lossy/rules (Create)
    resp = client.post(
        "/api/v1/compression/lossy/rules",
        json={
            "name": "Archive 1Y Rule",
            "description": "Compress studies after 1 year",
            "enabled": True,
            "priority": 10,
            "min_age_days": 365,
            "quality_factor": 85,
        },
        headers=headers,
    )
    assert resp.status_code == 201
    rule_data = resp.json()
    rule_id = rule_data["id"]
    assert rule_data["min_age_days"] == 365

    # 2. GET /api/v1/compression/lossy/rules (List)
    resp_list = client.get("/api/v1/compression/lossy/rules", headers=headers)
    assert resp_list.status_code == 200
    assert len(resp_list.json()) >= 1

    # 3. GET /api/v1/compression/lossy/rules/{id} (Get)
    resp_get = client.get(f"/api/v1/compression/lossy/rules/{rule_id}", headers=headers)
    assert resp_get.status_code == 200
    assert resp_get.json()["id"] == rule_id

    # 4. PUT /api/v1/compression/lossy/rules/{id} (Update)
    resp_put = client.put(
        f"/api/v1/compression/lossy/rules/{rule_id}",
        json={"quality_factor": 75},
        headers=headers,
    )
    assert resp_put.status_code == 200
    assert resp_put.json()["quality_factor"] == 75

    # 5. POST /api/v1/compression/lossy/run (Trigger run)
    resp_run = client.post("/api/v1/compression/lossy/run", headers=headers)
    assert resp_run.status_code == 200
    assert "processed_instances" in resp_run.json()

    # 6. DELETE /api/v1/compression/lossy/rules/{id} (Delete)
    resp_del = client.delete(f"/api/v1/compression/lossy/rules/{rule_id}", headers=headers)
    assert resp_del.status_code == 200
