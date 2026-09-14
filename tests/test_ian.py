"""Tests for Instance Availability Notification (IAN) feature."""

from __future__ import annotations

import pytest
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DicomNode, InstanceAvailabilityNotification
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import ian_service, indexing_service, storage_service
from tests.helpers import make_instance


def _create_node(ae_title: str, host: str = "127.0.0.1", port: int = 10408) -> DicomNode:
    with Session(sync_engine) as session:
        node = DicomNode(ae_title=ae_title, host=host, port=port, description="RIS Listener Node")
        session.add(node)
        session.commit()
        session.refresh(node)
        return node


def test_build_ian_dataset():
    """Verify DICOM IAN N-CREATE dataset structure."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    sop_uid = generate_uid()

    series_structure = [
        {
            "series_instance_uid": series_uid,
            "instances": [{"sop_instance_uid": sop_uid, "sop_class_uid": "1.2.840.10008.5.1.4.1.1.2"}],
        }
    ]

    ds = ian_service.build_ian_dataset(study_uid, series_structure, availability="ONLINE")

    assert ds.SOPClassUID == "1.2.840.10008.5.1.4.33"
    assert ds.StudyInstanceUID == study_uid
    assert len(ds.ReferencedSeriesSequence) == 1
    assert ds.ReferencedSeriesSequence[0].SeriesInstanceUID == series_uid
    assert ds.ReferencedSeriesSequence[0].ReferencedSOPSequence[0].InstanceAvailability == "ONLINE"


@pytest.mark.anyio
async def test_notify_study_available_service():
    """Verify notifying study availability creates DB record and log."""
    node = _create_node("RIS_IAN_NODE", "127.0.0.1", 10408)

    study_uid = generate_uid()
    ds = make_instance(patient_id="IAN_P1", study_uid=study_uid)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            key, sha = storage_service.save_dataset(ds)
            from app.dicom.parser import parse_metadata
            await indexing_service.index_new_instance(session, parse_metadata(ds), key, sha)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            records = await ian_service.notify_study_available(session, study_uid, target_ae_title="RIS_IAN_NODE")
            assert len(records) == 1
            assert records[0].target_ae_title == "RIS_IAN_NODE"
            assert records[0].study_instance_uid == study_uid

    with Session(sync_engine) as session:
        logs = session.execute(
            select(InstanceAvailabilityNotification).where(
                InstanceAvailabilityNotification.study_instance_uid == study_uid
            )
        ).scalars().all()
        assert len(logs) == 1
        assert logs[0].instance_availability == "ONLINE"


def _get_auth_headers(client, username="admin_ian"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin IAN"},
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


def test_ian_api(client):
    """Test IAN REST API endpoints."""
    headers = _get_auth_headers(client, "admin_ian_api")

    node = _create_node("EMR_IAN_NODE", "127.0.0.1", 10409)
    study_uid = generate_uid()
    ds = make_instance(patient_id="IAN_API_P1", study_uid=study_uid)

    from tests.helpers import build_stow_body, ds_bytes, stow_headers
    body = build_stow_body([ds_bytes(ds)])
    client.post(f"/dicom-web/studies/{study_uid}", content=body, headers=stow_headers())

    # 1. POST /api/v1/ian/notify
    resp = client.post(
        "/api/v1/ian/notify",
        json={"study_instance_uid": study_uid, "target_ae_title": "EMR_IAN_NODE"},
        headers=headers,
    )
    assert resp.status_code == 200
    res_data = resp.json()
    assert len(res_data["notifications"]) == 1
    notif_id = res_data["notifications"][0]["id"]

    # 2. GET /api/v1/ian/notifications
    resp_list = client.get("/api/v1/ian/notifications", headers=headers)
    assert resp_list.status_code == 200
    assert len(resp_list.json()) >= 1

    # 3. GET /api/v1/ian/notifications/{id}
    resp_get = client.get(f"/api/v1/ian/notifications/{notif_id}", headers=headers)
    assert resp_get.status_code == 200
    assert resp_get.json()["id"] == notif_id
