"""Tests for the radiologist reading worklist (roadmap #30).

Reader task lists built on the archive:
  UNREAD    — studies with images, no report, not read
  DICTATED  — report drafted, awaiting finalization
  READ      — finalized report (or explicitly marked read)
  STAT      — urgent overlay (manual flag / pending critical / HIGH triage)
"""

from __future__ import annotations

import asyncio

from pydicom.uid import generate_uid

from app.db.session import AsyncSessionLocal
from app.services import indexing_service, storage_service
from tests.helpers import make_instance


def _auth_headers(client, username="rwl_doc"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "pw123456", "full_name": "Dr Reader"},
    )
    if reg.status_code == 201:
        token = reg.json()["access_token"]
    else:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "pw123456"},
        )
        token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _ingest(study_uid: str, patient_name: str = "Reader^Test",
            patient_id: str = "RWL-PAT"):
    from app.dicom.parser import parse_metadata

    ds = make_instance(study_uid=study_uid, series_uid=generate_uid(),
                       sop_uid=generate_uid(), instance_num=1,
                       patient_id=patient_id, patient_name=patient_name)
    key, sha = storage_service.save_dataset(ds)

    async def _index():
        async with AsyncSessionLocal() as s:
            async with s.begin():
                await indexing_service.index_new_instance(
                    s, parse_metadata(ds), key, sha, performed_by="test"
                )

    asyncio.run(_index())


def _draft_report(client, headers, study_uid):
    return client.post(
        "/api/v1/reports",
        json={"study_instance_uid": study_uid, "findings": "Normal.",
              "impression": "No acute findings."},
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# State derivation
# --------------------------------------------------------------------------- #

def test_unread_dictated_read_states(client):
    headers = _auth_headers(client)
    unread_uid, dictated_uid, final_uid = generate_uid(), generate_uid(), generate_uid()
    _ingest(unread_uid)
    _ingest(dictated_uid)
    _ingest(final_uid)

    # Draft a report on one study (-> DICTATED)…
    assert _draft_report(client, headers, dictated_uid).status_code == 201
    # …and finalize another (-> READ).
    r = _draft_report(client, headers, final_uid)
    rid = r.json()["id"]
    assert client.post(f"/api/v1/reports/{rid}/finalize", headers=headers).status_code == 200

    def _uids(res):
        return {i["study_uid"] for i in res.json().get("items", [])}

    unread = client.get("/api/v1/worklist-tasks?state=UNREAD", headers=headers)
    assert unread.status_code == 200
    assert unread_uid in _uids(unread)
    assert dictated_uid not in _uids(unread)
    assert final_uid not in _uids(unread)

    dictated = client.get("/api/v1/worklist-tasks?state=DICTATED", headers=headers)
    assert dictated_uid in _uids(dictated)

    read = client.get("/api/v1/worklist-tasks?state=READ", headers=headers)
    assert final_uid in _uids(read)

    counts = client.get("/api/v1/worklist-tasks/counts", headers=headers).json()
    assert counts["unread"] >= 1
    assert counts["dictated"] >= 1
    assert counts["read"] >= 1


def test_read_overlay_and_unread_action(client):
    headers = _auth_headers(client)
    uid = generate_uid()
    _ingest(uid)

    # Mark read via API -> moves out of UNREAD.
    r = client.post(f"/api/v1/worklist-tasks/{uid}/read", headers=headers)
    assert r.status_code == 200
    tasks = client.get("/api/v1/worklist-tasks?state=UNREAD", headers=headers).json()
    assert uid not in {i["study_uid"] for i in tasks["items"]}
    tasks = client.get("/api/v1/worklist-tasks?state=READ", headers=headers).json()
    assert uid in {i["study_uid"] for i in tasks["items"]}
    assert any(i["read_by"] == "rwl_doc" for i in tasks["items"] if i["study_uid"] == uid)

    # Unread -> back in the pile.
    assert client.post(f"/api/v1/worklist-tasks/{uid}/unread", headers=headers).status_code == 200
    tasks = client.get("/api/v1/worklist-tasks?state=UNREAD", headers=headers).json()
    assert uid in {i["study_uid"] for i in tasks["items"]}


# --------------------------------------------------------------------------- #
# STAT overlay
# --------------------------------------------------------------------------- #

def test_stat_flag_toggle(client):
    headers = _auth_headers(client)
    uid = generate_uid()
    _ingest(uid)

    r = client.post(f"/api/v1/worklist-tasks/{uid}/stat",
                    json={"on": True}, headers=headers)
    assert r.status_code == 200 and r.json()["stat"] is True

    stat_tasks = client.get("/api/v1/worklist-tasks?state=STAT", headers=headers).json()
    assert uid in {i["study_uid"] for i in stat_tasks["items"]}
    assert any(i["stat"] is True for i in stat_tasks["items"] if i["study_uid"] == uid)

    counts = client.get("/api/v1/worklist-tasks/counts", headers=headers).json()
    assert counts["stat"] >= 1

    # Off again.
    assert client.post(f"/api/v1/worklist-tasks/{uid}/stat",
                       json={"on": False}, headers=headers).status_code == 200
    stat_tasks = client.get("/api/v1/worklist-tasks?state=STAT", headers=headers).json()
    assert uid not in {i["study_uid"] for i in stat_tasks["items"]}


def test_stat_from_pending_critical_result(client):
    headers = _auth_headers(client)
    uid = generate_uid()
    _ingest(uid)

    r = client.post(
        "/api/v1/critical-results",
        json={
            "study_instance_uid": uid,
            "finding_level": "CRITICAL_RED",
            "description": "Pneumothorax suspected",
            "reported_by": "rwl_doc",
        },
        headers=headers,
    )
    assert r.status_code == 201

    stat_tasks = client.get("/api/v1/worklist-tasks?state=STAT", headers=headers).json()
    assert uid in {i["study_uid"] for i in stat_tasks["items"]}

    counts = client.get("/api/v1/worklist-tasks/counts", headers=headers).json()
    assert counts["stat_by_source"]["critical"] >= 1


def test_assign_radiologist(client):
    headers = _auth_headers(client)
    uid = generate_uid()
    _ingest(uid)

    r = client.post(f"/api/v1/worklist-tasks/{uid}/assign",
                    json={"radiologist": "dr.ahmed"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["assigned_radiologist"] == "dr.ahmed"

    mine = client.get(
        "/api/v1/worklist-tasks?state=UNREAD&radiologist=dr.ahmed", headers=headers
    ).json()
    assert uid in {i["study_uid"] for i in mine["items"]}

    # Clearing assignment.
    assert client.post(f"/api/v1/worklist-tasks/{uid}/assign",
                       json={"radiologist": None}, headers=headers).status_code == 200
    mine = client.get(
        "/api/v1/worklist-tasks?state=UNREAD&radiologist=dr.ahmed", headers=headers
    ).json()
    assert uid not in {i["study_uid"] for i in mine["items"]}


# --------------------------------------------------------------------------- #
# API guards / errors
# --------------------------------------------------------------------------- #

def test_requires_auth_and_unknown_study(client):
    assert client.get("/api/v1/worklist-tasks").status_code in (401, 403)
    headers = _auth_headers(client)
    assert client.post(f"/api/v1/worklist-tasks/{generate_uid()}/stat",
                       json={"on": True}, headers=headers).status_code == 404
    assert client.get(
        "/api/v1/worklist-tasks?state=NOPE", headers=headers
    ).status_code == 422
