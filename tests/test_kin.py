"""Tests for Key Image Notes (KIN / Key Object Selection Document) feature."""

from __future__ import annotations

import pytest
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, KeyImageNote
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import indexing_service, kin_service, storage_service
from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers


@pytest.mark.anyio
async def test_create_kin_service():
    """Verify creating a Key Image Note synthesizes DICOM KO and indexes DB."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    img_sop1 = generate_uid()
    img_sop2 = generate_uid()

    # Create image instances first
    ds1 = make_instance(study_uid=study_uid, series_uid=series_uid, sop_uid=img_sop1)
    ds2 = make_instance(study_uid=study_uid, series_uid=series_uid, sop_uid=img_sop2)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            k1, s1 = storage_service.save_dataset(ds1)
            k2, s2 = storage_service.save_dataset(ds2)
            from app.dicom.parser import parse_metadata
            await indexing_service.index_new_instance(session, parse_metadata(ds1), k1, s1)
            await indexing_service.index_new_instance(session, parse_metadata(ds2), k2, s2)

    # Synthesize KIN note
    async with AsyncSessionLocal() as session:
        async with session.begin():
            kin = await kin_service.create_key_image_note(
                session,
                study_instance_uid=study_uid,
                series_instance_uid=series_uid,
                referenced_sop_uids=[img_sop1, img_sop2],
                notes="Primary lesion slice + follow up slice",
                title_code="113003",  # For Surgery
                author_name="dr.surgeon",
            )
            assert kin.title_concept_code == "113003"
            assert kin.title_concept_text == "For Surgery"
            assert img_sop1 in kin.referenced_sop_instance_uids
            assert img_sop2 in kin.referenced_sop_instance_uids

    # Query DB
    with Session(sync_engine) as session:
        notes = session.execute(
            select(KeyImageNote).where(KeyImageNote.study_instance_uid == study_uid)
        ).scalars().all()
        assert len(notes) == 1
        assert notes[0].notes == "Primary lesion slice + follow up slice"

        # Check Audit Log
        audits = session.execute(
            select(AuditLog).where(AuditLog.study_instance_uid == study_uid)
        ).scalars().all()
        actions = {a.action for a in audits}
        assert "KEY_IMAGE_NOTE_CREATED" in actions


@pytest.mark.anyio
async def test_find_kin_by_instance():
    """Verify querying Key Image Notes by bookmarked instance SOP UID."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    target_sop = generate_uid()

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await kin_service.create_key_image_note(
                session,
                study_instance_uid=study_uid,
                series_instance_uid=series_uid,
                referenced_sop_uids=[target_sop],
                notes="Bookmarked for teaching case",
                title_code="113004",
            )

    async with AsyncSessionLocal() as session:
        results = await kin_service.find_kin_for_instance(session, target_sop)
        assert len(results) == 1
        assert results[0].title_concept_text == "For Teaching"


def _get_auth_headers(client, username="admin_kin"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin KIN"},
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


def test_kin_api(client):
    """Test Key Image Notes REST API endpoints."""
    headers = _get_auth_headers(client, "dr_rad_kin")

    study_uid = generate_uid()
    series_uid = generate_uid()
    img_sop = generate_uid()

    img = make_instance(study_uid=study_uid, series_uid=series_uid, sop_uid=img_sop)
    body = build_stow_body([ds_bytes(img)])
    client.post(f"/dicom-web/studies/{study_uid}", content=body, headers=stow_headers())

    # 1. POST /api/v1/kin (Create KIN)
    resp = client.post(
        "/api/v1/kin",
        json={
            "study_instance_uid": study_uid,
            "series_instance_uid": series_uid,
            "referenced_sop_instance_uids": [img_sop],
            "notes": "Key image showing right lower lobe opacity",
            "title_concept_code": "113000",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    kin_data = resp.json()
    kin_sop = kin_data["sop_instance_uid"]
    assert kin_data["title_concept_text"] == "Of Interest"

    # 2. GET /api/v1/kin/study/{study_uid}
    resp_study = client.get(f"/api/v1/kin/study/{study_uid}", headers=headers)
    assert resp_study.status_code == 200
    assert len(resp_study.json()) == 1

    # 3. GET /api/v1/kin/instance/{sop}/notes
    resp_inst = client.get(f"/api/v1/kin/instance/{img_sop}/notes", headers=headers)
    assert resp_inst.status_code == 200
    assert len(resp_inst.json()) == 1

    # 4. GET /api/v1/kin/{sop_uid}
    resp_get = client.get(f"/api/v1/kin/{kin_sop}", headers=headers)
    assert resp_get.status_code == 200
    assert resp_get.json()["notes"] == "Key image showing right lower lobe opacity"
