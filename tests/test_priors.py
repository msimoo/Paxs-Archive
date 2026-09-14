"""Tests for Prior Study Linking feature."""

from __future__ import annotations

import pytest
from pydicom.uid import generate_uid

from tests.helpers import make_instance


@pytest.mark.anyio
async def test_find_prior_studies_service():
    """Verify prior study finding and relevance scoring."""
    pid = "PRIOR_PAT_01"
    st1_uid = generate_uid()
    st2_uid = generate_uid()

    # Create prior CT study
    ds1 = make_instance(patient_id=pid, study_uid=st1_uid, modality="CT", body_part="CHEST", study_date="20250101")
    # Create current CT study
    ds2 = make_instance(patient_id=pid, study_uid=st2_uid, modality="CT", body_part="CHEST", study_date="20260101")

    from app.db.session import AsyncSessionLocal
    from app.dicom.parser import parse_metadata
    from app.services import indexing_service, prior_service, storage_service

    async with AsyncSessionLocal() as session:
        async with session.begin():
            k1, s1 = storage_service.save_dataset(ds1)
            k2, s2 = storage_service.save_dataset(ds2)
            await indexing_service.index_new_instance(session, parse_metadata(ds1), k1, s1)
            await indexing_service.index_new_instance(session, parse_metadata(ds2), k2, s2)

    async with AsyncSessionLocal() as session:
        matches = await prior_service.find_prior_studies(session, study_instance_uid=st2_uid)
        assert len(matches) == 1
        assert matches[0].study_instance_uid == st1_uid
        assert matches[0].is_exact_modality_match is True
        assert matches[0].is_exact_body_part_match is True
        assert matches[0].relevance_score >= 100


def _get_auth_headers(client, username="admin_priors"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin Priors"},
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


def test_priors_api(client):
    """Test Prior Study REST API endpoints."""
    headers = _get_auth_headers(client, "admin_priors_api")

    resp_st = client.get("/api/v1/priors/study/non_existent_uid", headers=headers)
    assert resp_st.status_code == 200
    assert resp_st.json() == []

    resp_pat = client.get("/api/v1/priors/patient/NON_EXISTENT_PAT", headers=headers)
    assert resp_pat.status_code == 200
    assert resp_pat.json() == []
