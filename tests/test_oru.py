"""Tests for Phase E2 — HL7 ORU report write-back to the EMR.

Covers:
  * report CRUD via REST (draft → edit → list/get, one report per study)
  * ORU^R01 generation — MSH-9, PID/ORC/OBR/OBX layout, LOINC identifiers,
    result status F, HL7 escaping of user text
  * finalize — locks the report; when HL7_EMR_ENABLED delivers the ORU over a
    real MLLP socket (framing 0x0B … 0x1C 0x0D) and stores the ACK; a dead
    EMR leaves oru_status FAILED with a recorded error
  * resend — re-delivers a failed finalized report once the EMR is up
  * role gate — PHYSICIAN cannot finalize (403)
"""

from __future__ import annotations

import asyncio
import socket
import threading

from sqlalchemy import select

from app.db.models import RadiologyReport, Study
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import indexing_service, report_service, storage_service
from tests.helpers import make_instance


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ingest_study(patient_id: str, study_uid: str = "1.2.840.113619.2.408.700",
                  accession: str = "ACC-ORU-1", description: str = "CT CHEST") -> str:
    """Save + index a study (1 instance) so a report can attach to it."""
    from app.dicom.parser import parse_metadata

    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.700.1",
                       sop_uid="1.2.3.5.700.1", patient_id=patient_id,
                       accession=accession, study_desc=description,
                       modality="CT")
    key, sha = storage_service.save_dataset(ds)

    async def _index():
        async with AsyncSessionLocal() as s:
            async with s.begin():
                await indexing_service.index_new_instance(
                    s, parse_metadata(ds), key, sha, performed_by="test"
                )

    asyncio.run(_index())
    return study_uid


def _auth_headers(client, username="dr_rad", password="secret123", role="ADMIN"):
    reg = client.post("/api/v1/auth/register", json={"username": username, "password": password,
                                                     "role": role})
    if reg.status_code == 201:
        token = reg.json()["access_token"]
    else:
        login = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _report_row(report_id: int):
    with sync_engine.connect() as conn:
        row = conn.execute(
            select(RadiologyReport.status, RadiologyReport.oru_status,
                   RadiologyReport.oru_error, RadiologyReport.finalized_at)
            .where(RadiologyReport.id == report_id)
        ).first()
    return row


class _EmrMllp:
    """Minimal mock EMR MLLP receiver: stores the framed message, answers ACK."""

    def __init__(self):
        self.received = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            buf = b""
            while b"\x1c\x0d" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            self.received = buf
            # Reply with an ACK for the received control id (MSH-10).
            text = buf.decode("utf-8", errors="replace").strip("\x0b\x1c\x0d")
            msh = text.split("\r")[0] if text else ""
            ctrl = (msh.split("|")[9] if len(msh.split("|")) > 9 else "?") or "?"
            ack = ("MSH|^~\\&|EMR|EMR|PAXS|PAXS_ARCHIVE||||ACK^R01^ACK|||P|2.3\r"
                   f"MSA|AA|{ctrl}")
            conn.sendall(b"\x0b" + ack.encode() + b"\x1c\x0d")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _parse_frame(frame: bytes) -> list[str]:
    """Unframe an MLLP payload into segments."""
    assert frame.startswith(b"\x0b")
    assert frame.endswith(b"\x1c\x0d")
    text = frame[1:-2].decode("utf-8", errors="replace")
    return [s for s in text.replace("\r", "\n").split("\n") if s.strip()]


# --------------------------------------------------------------------------- #
# ORU message builder (unit)
# --------------------------------------------------------------------------- #
def test_build_oru_message_structure():
    # Build a minimal in-memory report + study without a DB round-trip by
    # creating rows through the service path below instead: use a real study
    # row so relationships (study.patient) resolve.
    study_uid = _ingest_study("ORU_P1")
    with sync_engine.connect() as conn:
        study_id = conn.execute(
            select(Study.id).where(Study.study_instance_uid == study_uid)
        ).scalar_one()
        
        from app.db.models import RadiologyReport
        from app.db.session import SyncSessionLocal

        with SyncSessionLocal() as s:
            report = RadiologyReport(
                study_id=study_id, study_instance_uid=study_uid,
                patient_id="ORU_P1", status="FINALIZED",
                findings="No acute\nintrathoracic^process|seen",
                impression="Normal chest",
                radiologist="Dr. Radi",
            )
            s.add(report)
            s.commit()
            s.refresh(report)
            study = report.study
            message = report_service.build_oru_message(report, study, study.patient)
            segments = message.rstrip("\r").split("\r")

    assert segments[0].startswith("MSH|^~\\&|PAXS|")
    assert "|ORU^R01^ORU_R01|" in segments[0]
    pid = segments[1]
    assert pid.startswith("PID|1|")
    assert "ORU_P1" in pid
    obr = segments[4]
    assert obr.startswith("OBR|1|")
    assert "ACC-ORU-1" in obr            # filler order number -> accession
    assert "|RAD|F" in obr or obr.endswith("|RAD|F")
    findings_obx = segments[5]
    assert findings_obx.startswith("OBX|1|TX|50005-5^FINDINGS^LN|")
    assert "|F" in findings_obx          # OBX-11 final
    # HL7 escaping of user text: ^ -> \S\, | -> \F\ (line break survives)
    assert "intrathoracic\\S\\process\\F\\seen" in findings_obx
    impression_obx = segments[6]
    assert impression_obx.startswith("OBX|2|TX|50122-5^IMPRESSION^LN|")
    assert "Normal chest" in impression_obx


# --------------------------------------------------------------------------- #
# Report CRUD via REST
# --------------------------------------------------------------------------- #
def test_report_crud_via_api(client):
    h = _auth_headers(client)
    study_uid = _ingest_study("ORU_P2", study_uid="1.2.840.113619.2.408.701")

    # Create draft
    r = client.post("/api/v1/reports", headers=h, json={
        "study_instance_uid": study_uid, "findings": "Draft findings",
        "impression": "Draft impression",
    })
    assert r.status_code == 201, r.text
    data = r.json()
    rid = data["id"]
    assert data["status"] == "DRAFT"
    assert data["radiologist"] == "dr_rad"

    # Edit draft
    r = client.patch(f"/api/v1/reports/{rid}", headers=h,
                     json={"findings": "Updated findings"})
    assert r.status_code == 200
    assert r.json()["findings"] == "Updated findings"

    # List + get
    r = client.get("/api/v1/reports", headers=h)
    assert r.status_code == 200 and len(r.json()) == 1
    r = client.get(f"/api/v1/reports/{rid}", headers=h)
    assert r.status_code == 200 and r.json()["id"] == rid

    # One report per study: duplicate create -> 409; missing study -> 404
    r = client.post("/api/v1/reports", headers=h,
                    json={"study_instance_uid": study_uid})
    assert r.status_code == 409
    r = client.post("/api/v1/reports", headers=h,
                    json={"study_instance_uid": "1.2.999"})
    assert r.status_code == 404

    # Edit a missing report -> 404
    r = client.patch("/api/v1/reports/99999", headers=h, json={"findings": "x"})
    assert r.status_code == 404


def test_finalize_requires_destructive_role(client):
    _auth_headers(client, username="root_oru", role="ADMIN")
    h = _auth_headers(client, username="phys_oru", role="PHYSICIAN")
    study_uid = _ingest_study("ORU_P3", study_uid="1.2.840.113619.2.408.702")
    r = client.post("/api/v1/reports", headers=h, json={"study_instance_uid": study_uid})
    rid = r.json()["id"]
    r = client.post(f"/api/v1/reports/{rid}/finalize", headers=h)
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Finalize + ORU delivery
# --------------------------------------------------------------------------- #
def test_finalize_without_emr_leaves_pending(client):
    h = _auth_headers(client)
    study_uid = _ingest_study("ORU_P4", study_uid="1.2.840.113619.2.408.703")
    rid = client.post("/api/v1/reports", headers=h,
                      json={"study_instance_uid": study_uid}).json()["id"]

    r = client.post(f"/api/v1/reports/{rid}/finalize", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "FINALIZED"
    assert body["delivery"]["status"] == "PENDING"
    status, oru, err, finalized = _report_row(rid)
    assert status == "FINALIZED" and oru == "PENDING" and finalized is not None

    # Finalized report is locked
    r = client.patch(f"/api/v1/reports/{rid}", headers=h, json={"findings": "x"})
    assert r.status_code == 409


def test_finalize_sends_oru_over_mllp(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = _ingest_study("ORU_P5", study_uid="1.2.840.113619.2.408.704",
                              accession="ACC-ORU-5")
    rid = client.post("/api/v1/reports", headers=h, json={
        "study_instance_uid": study_uid, "findings": "Small right pneumothorax",
        "impression": "Follow up in 24h",
    }).json()["id"]

    emr = _EmrMllp()
    monkeypatch.setattr(report_service.settings, "HL7_EMR_ENABLED", True)
    monkeypatch.setattr(report_service.settings, "HL7_EMR_HOST", "127.0.0.1")
    monkeypatch.setattr(report_service.settings, "HL7_EMR_PORT", emr.port)
    try:
        r = client.post(f"/api/v1/reports/{rid}/finalize", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["delivery"]["status"] == "SENT"
        assert r.json()["delivery"]["ack"].startswith("MSH|")
        status, oru, err, finalized = _report_row(rid)
        assert status == "FINALIZED" and oru == "SENT" and err is None

        # The mock EMR received a properly framed ORU^R01.
        segments = _parse_frame(emr.received)
        assert segments[0].split("|")[8].startswith("ORU^R01")
        assert any("Small right pneumothorax" in s for s in segments)
        assert any("Follow up in 24h" in s for s in segments)
        assert any("ACC-ORU-5" in s for s in segments)
    finally:
        emr.close()


def test_finalize_emr_down_fails_then_resend(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = _ingest_study("ORU_P6", study_uid="1.2.840.113619.2.408.705")
    rid = client.post("/api/v1/reports", headers=h, json={
        "study_instance_uid": study_uid, "findings": "Normal",
    }).json()["id"]

    # EMR enabled but nothing listening -> delivery FAILED with an error.
    monkeypatch.setattr(report_service.settings, "HL7_EMR_ENABLED", True)
    monkeypatch.setattr(report_service.settings, "HL7_EMR_HOST", "127.0.0.1")
    monkeypatch.setattr(report_service.settings, "HL7_EMR_PORT", 1)  # nothing there
    try:
        r = client.post(f"/api/v1/reports/{rid}/finalize", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["delivery"]["status"] == "FAILED"
        status, oru, err, _ = _report_row(rid)
        assert status == "FINALIZED" and oru == "FAILED" and err

        # Re-sending while the EMR is still down keeps FAILED.
        r = client.post(f"/api/v1/reports/{rid}/resend", headers=h)
        assert r.status_code == 200
        assert r.json()["delivery"]["status"] == "FAILED"

        # Bring the EMR up — resend now delivers and records SENT.
        emr = _EmrMllp()
        monkeypatch.setattr(report_service.settings, "HL7_EMR_PORT", emr.port)
        try:
            r = client.post(f"/api/v1/reports/{rid}/resend", headers=h)
            assert r.status_code == 200, r.text
            assert r.json()["delivery"]["status"] == "SENT"
            status, oru, err, _ = _report_row(rid)
            assert oru == "SENT" and err is None
            assert b"\x0b" == emr.received[:1] and emr.received.endswith(b"\x1c\x0d")
        finally:
            emr.close()
    finally:
        pass


def test_double_finalize_is_idempotent(client, monkeypatch):
    """A second finalize on an already-SENT report must not re-deliver the ORU."""
    h = _auth_headers(client)
    study_uid = _ingest_study("ORU_P10", study_uid="1.2.840.113619.2.408.709")
    rid = client.post("/api/v1/reports", headers=h, json={
        "study_instance_uid": study_uid, "findings": "Normal",
    }).json()["id"]

    emr = _EmrMllp()
    monkeypatch.setattr(report_service.settings, "HL7_EMR_ENABLED", True)
    monkeypatch.setattr(report_service.settings, "HL7_EMR_HOST", "127.0.0.1")
    monkeypatch.setattr(report_service.settings, "HL7_EMR_PORT", emr.port)
    try:
        r1 = client.post(f"/api/v1/reports/{rid}/finalize", headers=h)
        assert r1.status_code == 200
        assert r1.json()["delivery"]["status"] == "SENT"
        first_frame = emr.received

        # Second finalize: still 200 + SENT (report already delivered), but the
        # EMR must NOT receive a second ORU frame.
        r2 = client.post(f"/api/v1/reports/{rid}/finalize", headers=h)
        assert r2.status_code == 200
        assert r2.json()["delivery"]["status"] == "SENT"
        assert emr.received == first_frame  # same frame object → no new delivery
    finally:
        emr.close()


def test_resend_draft_rejected(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = _ingest_study("ORU_P7", study_uid="1.2.840.113619.2.408.706")
    rid = client.post("/api/v1/reports", headers=h,
                      json={"study_instance_uid": study_uid}).json()["id"]
    monkeypatch.setattr(report_service.settings, "HL7_EMR_ENABLED", True)
    r = client.post(f"/api/v1/reports/{rid}/resend", headers=h)
    assert r.status_code == 409
    assert "FINALIZED" in r.json()["detail"]


def test_list_reports_filter(client):
    h = _auth_headers(client)
    uid1 = _ingest_study("ORU_P8", study_uid="1.2.840.113619.2.408.707")
    uid2 = _ingest_study("ORU_P9", study_uid="1.2.840.113619.2.408.708")
    client.post("/api/v1/reports", headers=h, json={"study_instance_uid": uid1,
                                                    "findings": "A"})
    client.post("/api/v1/reports", headers=h, json={"study_instance_uid": uid2,
                                                    "findings": "B"})
    client.post(f"/api/v1/reports/{client.get('/api/v1/reports', headers=h).json()[1]['id']}/finalize", headers=h)

    r = client.get("/api/v1/reports?status=DRAFT", headers=h)
    assert len(r.json()) == 1
    r = client.get(f"/api/v1/reports?study_uid={uid2}", headers=h)
    assert len(r.json()) == 1 and r.json()[0]["study_instance_uid"] == uid2
    r = client.get("/api/v1/reports?status=BOGUS", headers=h)
    assert r.status_code == 422
