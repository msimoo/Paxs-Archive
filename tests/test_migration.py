"""Tests for PACS Migration Tools (Folder watcher/ingest and C-MOVE batch migration)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Instance
from app.db.session import sync_engine
from app.services import migration_service
from tests.helpers import ds_bytes, make_instance


def test_scan_and_import_folder_sync():
    """Verify scanning a folder with synthetic DICOM files imports them into PAXS."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Generate 3 synthetic DICOM instances
        ds1 = make_instance(patient_id="MIG001", accession="ACC_MIG1", instance_num=1)
        ds2 = make_instance(patient_id="MIG002", accession="ACC_MIG2", instance_num=1)
        ds3 = make_instance(patient_id="MIG003", accession="ACC_MIG3", instance_num=1)

        (tmp_path / "study1.dcm").write_bytes(ds_bytes(ds1))
        (tmp_path / "study2.dcm").write_bytes(ds_bytes(ds2))
        (tmp_path / "subfolder").mkdir()
        (tmp_path / "subfolder" / "study3.dcm").write_bytes(ds_bytes(ds3))

        # Also write a non-DICOM junk file to test error/skip handling
        (tmp_path / "junk.txt").write_text("not a dicom file")

        # Run folder scan and import
        res = migration_service.scan_and_import_folder_sync(tmp_path, delete_after_import=False)

        assert res["processed"] == 3
        assert res["failed"] == 1  # junk.txt failed pydicom read
        assert len(res["errors"]) >= 1

        # Check DB rows created
        with Session(sync_engine) as session:
            insts = session.execute(select(Instance)).scalars().all()
            sops = {i.sop_instance_uid for i in insts}
            assert ds1.SOPInstanceUID in sops
            assert ds2.SOPInstanceUID in sops
            assert ds3.SOPInstanceUID in sops


def test_scan_and_import_folder_delete_after():
    """Verify that delete_after_import=True removes successfully imported files."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        ds = make_instance(patient_id="MIG_DEL", accession="ACC_DEL")
        file_p = tmp_path / "sample.dcm"
        file_p.write_bytes(ds_bytes(ds))

        assert file_p.exists()

        res = migration_service.scan_and_import_folder_sync(tmp_path, delete_after_import=True)

        assert res["processed"] == 1
        assert not file_p.exists()


def _get_auth_headers(client, username="admin_mig"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin Mig"},
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


def test_folder_migration_api(client):
    """Test POST /api/v1/migration/folder and GET endpoints."""
    headers = _get_auth_headers(client, "admin_mig1")

    with tempfile.TemporaryDirectory() as tmp_dir:
        ds = make_instance(patient_id="MIG_API")
        (Path(tmp_dir) / "api_test.dcm").write_bytes(ds_bytes(ds))

        # Start job
        resp = client.post(
            "/api/v1/migration/folder",
            json={"folder_path": tmp_dir, "delete_after_import": False},
            headers=headers,
        )

        assert resp.status_code == 202
        data = resp.json()
        assert "job" in data
        job_id = data["job"]["id"]
        assert data["job"]["job_type"] == "FOLDER_WATCH"

        # Check job status via GET /jobs/{id}
        resp_job = client.get(f"/api/v1/migration/jobs/{job_id}", headers=headers)
        assert resp_job.status_code == 200
        assert resp_job.json()["id"] == job_id

        # List all jobs
        resp_list = client.get("/api/v1/migration/jobs", headers=headers)
        assert resp_list.status_code == 200
        assert len(resp_list.json()) >= 1


def test_cmove_migration_api_validation(client):
    """Test POST /api/v1/migration/cmove validation."""
    headers = _get_auth_headers(client, "admin_mig2")

    # Empty study list returns 400
    resp = client.post(
        "/api/v1/migration/cmove",
        json={"ae_title": "REMOTE_PACS", "study_instance_uids": []},
        headers=headers,
    )
    assert resp.status_code == 400

    # Valid request starts C-MOVE job
    resp = client.post(
        "/api/v1/migration/cmove",
        json={"ae_title": "REMOTE_PACS", "study_instance_uids": ["1.2.840.10008.1.999"]},
        headers=headers,
    )
    assert resp.status_code == 202
    assert resp.json()["job"]["job_type"] == "CMOVE_BATCH"
