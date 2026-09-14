"""Tests for Study Split and Merge clinical workflow feature."""

from __future__ import annotations

import pytest
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Instance, Patient, Series, Study
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import indexing_service, split_merge_service, storage_service
from tests.helpers import ds_bytes, make_instance


@pytest.mark.anyio
async def test_split_series_to_new_study_service():
    """Verify splitting a series out of a study into a new study."""
    study_uid = generate_uid()
    series1_uid = generate_uid()
    series2_uid = generate_uid()

    ds1 = make_instance(patient_id="SPLIT_P1", study_uid=study_uid, series_uid=series1_uid, series_num=1)
    ds2 = make_instance(patient_id="SPLIT_P1", study_uid=study_uid, series_uid=series2_uid, series_num=2)

    # Ingest both instances into DB
    async with AsyncSessionLocal() as session:
        async with session.begin():
            key1, sha1 = storage_service.save_dataset(ds1)
            key2, sha2 = storage_service.save_dataset(ds2)
            meta1 = indexing_service.parse_metadata(ds1) if hasattr(indexing_service, "parse_metadata") else None
            if meta1 is None:
                from app.dicom.parser import parse_metadata
                meta1 = parse_metadata(ds1)
                meta2 = parse_metadata(ds2)
            else:
                meta2 = indexing_service.parse_metadata(ds2)

            await indexing_service.index_new_instance(session, meta1, key1, sha1)
            await indexing_service.index_new_instance(session, meta2, key2, sha2)

    # Perform Split of series2
    async with AsyncSessionLocal() as session:
        async with session.begin():
            new_study = await split_merge_service.split_series_to_new_study(
                session,
                source_study_uid=study_uid,
                series_uids=[series2_uid],
                target_patient_id="SPLIT_P2",
                target_patient_name="Split^PatientTwo",
            )
            new_study_uid = new_study.study_instance_uid

    # Verify DB state
    with Session(sync_engine) as session:
        orig_study = session.execute(
            select(Study).where(Study.study_instance_uid == study_uid)
        ).scalar_one_or_none()
        assert orig_study is not None

        new_st = session.execute(
            select(Study).where(Study.study_instance_uid == new_study_uid)
        ).scalar_one_or_none()
        assert new_st is not None
        assert new_st.patient.patient_id == "SPLIT_P2"

        # Check series allocation
        s1 = session.execute(select(Series).where(Series.series_instance_uid == series1_uid)).scalar_one()
        s2 = session.execute(select(Series).where(Series.series_instance_uid == series2_uid)).scalar_one()
        assert s1.study_id == orig_study.id
        assert s2.study_id == new_st.id

        # Verify audit log
        audits = session.execute(select(AuditLog).where(AuditLog.action == "SPLIT")).scalars().all()
        assert len(audits) >= 1


@pytest.mark.anyio
async def test_merge_studies_service():
    """Verify merging series from a source study into a target study."""
    study1_uid = generate_uid()
    study2_uid = generate_uid()
    series1_uid = generate_uid()
    series2_uid = generate_uid()

    ds1 = make_instance(patient_id="MERGE_P1", study_uid=study1_uid, series_uid=series1_uid)
    ds2 = make_instance(patient_id="MERGE_P2", study_uid=study2_uid, series_uid=series2_uid)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            key1, sha1 = storage_service.save_dataset(ds1)
            key2, sha2 = storage_service.save_dataset(ds2)
            from app.dicom.parser import parse_metadata
            await indexing_service.index_new_instance(session, parse_metadata(ds1), key1, sha1)
            await indexing_service.index_new_instance(session, parse_metadata(ds2), key2, sha2)

    # Merge study1 into study2
    async with AsyncSessionLocal() as session:
        async with session.begin():
            target_st = await split_merge_service.merge_studies(
                session,
                source_study_uids=[study1_uid],
                target_study_uid=study2_uid,
            )
            assert target_st.study_instance_uid == study2_uid

    # Verify DB state
    with Session(sync_engine) as session:
        src_study = session.execute(
            select(Study).where(Study.study_instance_uid == study1_uid)
        ).scalar_one_or_none()
        assert src_study is None  # Source study cleaned up

        tgt_study = session.execute(
            select(Study).where(Study.study_instance_uid == study2_uid)
        ).scalar_one_or_none()
        assert tgt_study is not None

        # Both series now belong to tgt_study
        series_list = session.execute(
            select(Series).where(Series.study_id == tgt_study.id)
        ).scalars().all()
        assert len(series_list) == 2


def _get_auth_headers(client, username="admin_sm"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin SM"},
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


def test_split_and_merge_api(client):
    """Test API endpoints for study split and merge."""
    headers = _get_auth_headers(client, "admin_sm_api")

    study1_uid = generate_uid()
    study2_uid = generate_uid()
    series1_uid = generate_uid()
    series2_uid = generate_uid()

    ds1 = make_instance(patient_id="API_P1", study_uid=study1_uid, series_uid=series1_uid)
    ds2 = make_instance(patient_id="API_P1", study_uid=study1_uid, series_uid=series2_uid)

    from tests.helpers import build_stow_body, stow_headers
    body = build_stow_body([ds_bytes(ds1), ds_bytes(ds2)])
    client.post(f"/dicom-web/studies/{study1_uid}", content=body, headers=stow_headers())

    # Split series2 via API
    split_resp = client.post(
        "/api/v1/studies/split",
        json={
            "source_study_uid": study1_uid,
            "series_instance_uids": [series2_uid],
            "target_patient_id": "API_P2",
        },
        headers=headers,
    )
    assert split_resp.status_code == 200
    new_study_uid = split_resp.json()["new_study_instance_uid"]
    assert new_study_uid != study1_uid

    # Merge new study back into study1 via API
    merge_resp = client.post(
        "/api/v1/studies/merge",
        json={
            "source_study_uids": [new_study_uid],
            "target_study_uid": study1_uid,
        },
        headers=headers,
    )
    assert merge_resp.status_code == 200
    assert merge_resp.json()["target_study_instance_uid"] == study1_uid
