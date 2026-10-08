"""C06 — clinical report integrity.

`POST /reports/save` used to be a second, weaker reporting workflow: it took the
radiologist name from the request body, overwrote FINALIZED reports in place, and
set final status without the ORU delivery path. It now delegates to the one
supported service contract, and finalized reports are immutable with explicit
addenda.
"""

from __future__ import annotations

import numpy as np
import pytest
from pydicom.uid import generate_uid

from app.core.config import settings
from app.services import report_service
from tests.helpers import auth_for_role, build_stow_body, ds_bytes, make_instance, stow_headers

SAVE = "/api/v1/reports/save"


@pytest.fixture()
def study(auth_client) -> str:
    """A stored study to attach reports to."""
    study_uid, series_uid = generate_uid(), generate_uid()
    datasets = [
        ds_bytes(make_instance(
            patient_id="RPT_PAT",
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=generate_uid(),
            instance_num=i + 1,
            rows=8,
            cols=8,
            pixel=np.full((8, 8), 50 + i, dtype=np.uint16),
        ))
        for i in range(2)
    ]
    resp = auth_client.post(
        "/dicom-web/studies", content=build_stow_body(datasets), headers=stow_headers()
    )
    assert resp.status_code == 200, resp.text
    return study_uid


@pytest.fixture()
def mllp(monkeypatch):
    """Capture ORU^R01 messages instead of opening a socket to an EMR."""
    sent: list[str] = []
    monkeypatch.setattr(
        report_service,
        "send_oru_to_emr",
        lambda message, *a, **kw: (sent.append(message), {"ok": True, "ack": "AA"})[1]
    )
    monkeypatch.setattr(settings, "HL7_EMR_ENABLED", True, raising=False)
    return sent


def _save(client, study_uid: str, *, status: str = "DRAFT", headers=None, **fields):
    return client.post(
        SAVE,
        headers=headers or {},
        json={"study_instance_uid": study_uid, "status": status, **fields}
    )


# --------------------------------------------------------------------------- #
# authentication & actor identity
# --------------------------------------------------------------------------- #
def test_anonymous_save_is_rejected(anonymous_client, study):
    assert _save(anonymous_client, study, findings="x").status_code == 401


def test_radiologist_identity_comes_from_the_authenticated_actor(auth_client, study):
    """A client-supplied name must never become the signed author."""
    resp = _save(
        auth_client, study,
        findings="real findings", radiologist="Dr. Impostor"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["radiologist"] == "test_admin"
    assert "Impostor" not in resp.text


# --------------------------------------------------------------------------- #
# DRAFT editing vs finalized immutability
# --------------------------------------------------------------------------- #
def test_draft_can_be_edited(auth_client, study):
    assert _save(auth_client, study,
                 findings="first").status_code == 200
    again = _save(auth_client, study, findings="second")
    assert again.status_code == 200
    assert again.json()["findings"] == "second"
    assert again.json()["status"] == "DRAFT"


def test_finalized_report_cannot_be_overwritten(auth_client, study, mllp):
    created = _save(auth_client, study,
                    findings="signed findings", impression="signed impression")
    report_id = created.json()["id"]
    assert _save(auth_client, study,
                 status="FINALIZED").status_code == 200

    # A further ordinary edit must be refused, and the signed text untouched.
    resp = _save(auth_client, study,
                 findings="tampered", impression="tampered")
    assert resp.status_code == 409, resp.text

    fetched = auth_client.get(f"/api/v1/reports/{report_id}")
    assert fetched.status_code == 200
    assert fetched.json()["findings"] == "signed findings"
    assert fetched.json()["impression"] == "signed impression"
    assert fetched.json()["status"] == "FINALIZED"


# --------------------------------------------------------------------------- #
# finalize authorization
# --------------------------------------------------------------------------- #
def test_finalize_requires_a_destructive_role(client, auth_client, auth_headers, study):
    """A PHYSICIAN may draft but not sign off."""
    physician = auth_for_role(
        client, "rpt_physician", role="PHYSICIAN", admin=auth_headers
    )
    draft = _save(client, study, headers=physician, findings="draft by physician")
    assert draft.status_code == 200, draft.text

    resp = _save(client, study, headers=physician, status="FINALIZED")
    assert resp.status_code == 403, resp.text
    assert "radiologist" in resp.text.lower() or "admin" in resp.text.lower()


def test_radiologist_may_finalize(radiologist_client, study, mllp):
    assert _save(radiologist_client, study,
                 findings="signed").status_code == 200
    resp = _save(radiologist_client, study,
                 status="FINALIZED")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "FINALIZED"


# --------------------------------------------------------------------------- #
# ORU delivery
# --------------------------------------------------------------------------- #
def test_finalize_delivers_oru_once(auth_client, study, mllp):
    """Finalizing must reach the EMR exactly once (idempotent)."""
    _save(auth_client, study, findings="to sign")
    first = _save(auth_client, study, status="FINALIZED")
    assert first.status_code == 200, first.text
    assert len(mllp) == 1, "the ORU must be delivered on finalize"
    assert first.json()["oru_status"] == "SENT"
    assert first.json()["delivery"]["status"] == "SENT"

    # A second finalize of the same report must not duplicate it in the EMR.
    second = _save(auth_client, study, status="FINALIZED")
    assert second.status_code == 200
    assert len(mllp) == 1, "a second finalize must not re-send the ORU"


def test_ui_finalize_matches_the_api_finalize(auth_client, study, mllp):
    """/save (the UI path) and /{id}/finalize must deliver identically."""
    created = _save(auth_client, study, findings="sign me")
    report_id = created.json()["id"]
    via_save = _save(auth_client, study, status="FINALIZED")
    assert via_save.status_code == 200
    assert len(mllp) == 1
    assert via_save.json()["oru_status"] == "SENT"

    # The dedicated endpoint agrees, and stays idempotent against the same row.
    api = auth_client.post(f"/api/v1/reports/{report_id}/finalize")
    assert api.status_code == 200
    assert api.json()["status"] == "FINALIZED"
    assert len(mllp) == 1


# --------------------------------------------------------------------------- #
# addenda
# --------------------------------------------------------------------------- #
def test_addendum_requires_a_finalized_report(auth_client, study):
    created = _save(auth_client, study, findings="draft")
    report_id = created.json()["id"]
    resp = auth_client.post(
        f"/api/v1/reports/{report_id}/addendum",
                json={"text": "too early"}
    )
    assert resp.status_code == 409, resp.text


def test_addendum_preserves_the_original_and_records_the_actor(
    radiologist_client, study, mllp
):
    created = _save(radiologist_client, study,
                    findings="original findings", impression="original impression")
    report_id = created.json()["id"]
    assert _save(radiologist_client, study,
                 status="FINALIZED").status_code == 200

    added = radiologist_client.post(
        f"/api/v1/reports/{report_id}/addendum",
                json={"text": "Follow-up: repeat CT in 6 weeks."}
    )
    assert added.status_code == 201, added.text
    assert added.json()["author"] == "test_radiologist"
    assert added.json()["text"].startswith("Follow-up")

    # The signed fields are unchanged and the addendum sits beside them.
    fetched = radiologist_client.get(f"/api/v1/reports/{report_id}",
                                     ).json()
    assert fetched["findings"] == "original findings"
    assert fetched["impression"] == "original impression"
    assert len(fetched["addenda"]) == 1
    assert fetched["addenda"][0]["author"] == "test_radiologist"


def test_addendum_needs_a_destructive_role(
    client, auth_client, auth_headers, study, mllp
):
    created = _save(auth_client, study, findings="x")
    report_id = created.json()["id"]
    _save(auth_client, study, status="FINALIZED")

    physician = auth_for_role(
        client, "rpt_phys_addendum", role="PHYSICIAN", admin=auth_headers
    )
    resp = client.post(
        f"/api/v1/reports/{report_id}/addendum",
        headers=physician,
        json={"text": "should be refused"}
    )
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# error handling
# --------------------------------------------------------------------------- #
def test_unknown_study_is_a_clean_404(auth_client):
    resp = _save(auth_client, "1.2.3.4.5.6.7.8.9",
                 findings="x")
    assert resp.status_code == 404, resp.text
    assert "not found" in resp.text.lower()
