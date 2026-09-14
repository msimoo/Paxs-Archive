"""Tests for DICOMDIR Media Export feature."""

from __future__ import annotations

import io
import zipfile

import pytest
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import dicomdir_service, indexing_service, storage_service
from tests.helpers import make_instance


def test_create_dicomdir_dataset():
    """Verify building standard DICOMDIR dataset hierarchy."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    sop_uid = generate_uid()

    structure = [
        {
            "patient_id": "PAT_DICOMDIR",
            "patient_name": "Doe^Jane",
            "study_instance_uid": study_uid,
            "study_date": "20260805",
            "accession_number": "ACC123",
            "series_list": [
                {
                    "series_instance_uid": series_uid,
                    "modality": "CT",
                    "instances": [
                        {
                            "sop_instance_uid": sop_uid,
                            "sop_class_uid": "1.2.840.10008.5.1.4.1.1.2",
                            "relative_file_id": ["IMAGES", "IMG00001"],
                        }
                    ],
                }
            ],
        }
    ]

    ds = dicomdir_service.create_dicomdir_dataset(structure)

    assert ds.file_meta.MediaStorageSOPClassUID == "1.2.840.10008.1.3.10"
    records = ds.DirectoryRecordSequence
    record_types = [r.DirectoryRecordType for r in records]

    assert "PATIENT" in record_types
    assert "STUDY" in record_types
    assert "SERIES" in record_types
    assert "IMAGE" in record_types


@pytest.mark.anyio
async def test_export_dicomdir_package_service():
    """Verify exporting a ZIP bundle containing DICOMDIR and DICOM image files."""
    study_uid = generate_uid()
    ds = make_instance(patient_id="PAT_PACKAGE", study_uid=study_uid)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            key, sha = storage_service.save_dataset(ds)
            from app.dicom.parser import parse_metadata
            await indexing_service.index_new_instance(session, parse_metadata(ds), key, sha)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            zip_bytes, filename = await dicomdir_service.export_dicomdir_package(
                session, [study_uid], performed_by="test_user"
            )
            assert len(zip_bytes) > 0
            assert filename.endswith(".zip")

            # Inspect ZIP contents
            with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                file_names = zf.namelist()
                assert "DICOMDIR" in file_names
                images = [f for f in file_names if f.startswith("IMAGES/")]
                assert len(images) == 1

    # Check Audit Log
    with Session(sync_engine) as session:
        audits = session.execute(
            select(AuditLog).where(AuditLog.study_instance_uid == study_uid)
        ).scalars().all()
        actions = {a.action for a in audits}
        assert "EXPORT_DICOMDIR_CREATED" in actions


def _get_auth_headers(client, username="admin_ddir"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin DDir"},
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


def test_dicomdir_api(client):
    """Test DICOMDIR REST API endpoints."""
    headers = _get_auth_headers(client, "admin_ddir_api")

    study_uid = generate_uid()
    ds = make_instance(patient_id="DDIR_API_P1", study_uid=study_uid)

    from tests.helpers import build_stow_body, ds_bytes, stow_headers
    body = build_stow_body([ds_bytes(ds)])
    client.post(f"/dicom-web/studies/{study_uid}", content=body, headers=stow_headers())

    # 1. POST /api/v1/export/dicomdir/preview
    resp_prev = client.post(
        "/api/v1/export/dicomdir/preview",
        json={"study_instance_uids": [study_uid]},
        headers=headers,
    )
    assert resp_prev.status_code == 200
    prev_data = resp_prev.json()
    assert prev_data["total_directory_records"] >= 4

    # 2. POST /api/v1/export/dicomdir (Download ZIP)
    resp_zip = client.post(
        "/api/v1/export/dicomdir",
        json={"study_instance_uids": [study_uid]},
        headers=headers,
    )
    assert resp_zip.status_code == 200
    assert resp_zip.headers["content-type"] == "application/zip"
    assert len(resp_zip.content) > 0

    with zipfile.ZipFile(io.BytesIO(resp_zip.content), "r") as zf:
        assert "DICOMDIR" in zf.namelist()
