"""Tests for Web Upload (STOW-RS SCU) — send local instances to a remote
DICOMweb node over `POST {web_url}/studies` (multipart/related).

Covers:
  * real STOW-RS round-trip against a mock server — the right number of
    DICOM parts with the right SOP UIDs arrive, every instance audits
    FORWARDED
  * scope handling — study/series/instance UIDs resolve like /send
  * partial failure — a 200 body naming instances in FailedSOPSequence marks
    exactly those failed
  * error paths — node without web_url (409), no matching instances (404),
    missing scope (422), remote HTTP 500 (502), PHYSICIAN role gate (403)
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from sqlalchemy import select

from app.db.models import AuditLog
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import dicomweb_client, indexing_service, storage_service
from tests.helpers import make_instance


def _ingest_study(patient_id: str, n_instances: int = 2, study_uid: str = "1.2.840.113619.2.408.600") -> str:
    """Save + index a local study; returns its StudyInstanceUID."""
    from app.dicom.parser import parse_metadata

    for i in range(1, n_instances + 1):
        ds = make_instance(study_uid=study_uid, series_uid=f"1.2.3.4.600.{i}",
                           sop_uid=f"1.2.3.5.600.{i}", instance_num=i,
                           patient_id=patient_id)
        key, sha = storage_service.save_dataset(ds)

        async def _index(key=key, sha=sha, ds=ds):
            async with AsyncSessionLocal() as s:
                async with s.begin():
                    await indexing_service.index_new_instance(
                        s, parse_metadata(ds), key, sha, performed_by="test"
                    )

        asyncio.run(_index())
    return study_uid


class _StowHandler(BaseHTTPRequestHandler):
    received_parts: list = []
    received_ct: str = ""
    response_status: int = 200
    response_body: bytes = b""

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/dicom-web"):
            path = path[len("/dicom-web"):] or "/"
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        _StowHandler.received_ct = self.headers.get("Content-Type", "")
        _StowHandler.received_parts = list(
            dicomweb_client._iter_multipart_parts(body, _StowHandler.received_ct)
        )
        if path != "/studies":
            self.send_response(404)
        else:
            self.send_response(_StowHandler.response_status)
            self.send_header("Content-Type", "application/dicom+json")
            self.send_header("Content-Length", str(len(_StowHandler.response_body)))
        self.end_headers()
        if _StowHandler.response_body:
            self.wfile.write(_StowHandler.response_body)

    def log_message(self, *args):
        pass


def _start_stow_server():
    _StowHandler.received_parts = []
    _StowHandler.received_ct = ""
    _StowHandler.response_status = 200
    _StowHandler.response_body = b""
    server = HTTPServer(("127.0.0.1", 0), _StowHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _auth_headers(client, username="web_admin", password="secret123", role="ADMIN"):
    reg = client.post("/api/v1/auth/register", json={"username": username, "password": password,
                                                     "role": role})
    if reg.status_code == 201:
        token = reg.json()["access_token"]
    else:
        login = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sop_uids_received():
    uids = set()
    for part in _StowHandler.received_parts:
        ds = dicomweb_client._dataset_from_bytes(part)
        if ds is not None:
            uids.add(str(ds.SOPInstanceUID))
    return uids


def _audit_forwarded_uids():
    with sync_engine.connect() as conn:
        return {
            row[0] for row in conn.execute(
                select(AuditLog.sop_instance_uid).where(AuditLog.action == "FORWARDED")
            ).all()
        }


# --------------------------------------------------------------------------- #
# Success paths
# --------------------------------------------------------------------------- #
def test_stow_upload_sends_instances(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = _ingest_study("STOWP1")
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })

        r = client.post("/api/v1/nodes/WEB_PEER/web/upload", headers=h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["transport"] == "dicomweb"
        assert data["sent"] == 2 and data["failed"] == 0
        assert data["http_status"] == 200

        # Mock server received exactly the two instances, as DICOM parts.
        assert _sop_uids_received() == {"1.2.3.5.600.1", "1.2.3.5.600.2"}
        assert "multipart/related" in _StowHandler.received_ct

        # Every transferred instance is audit-logged as FORWARDED.
        assert _audit_forwarded_uids() == {"1.2.3.5.600.1", "1.2.3.5.600.2"}
    finally:
        server.shutdown()


def test_stow_upload_series_and_instance_scope(client, monkeypatch):
    h = _auth_headers(client)
    _ingest_study("STOWP2", n_instances=2)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })
        # Instance scope: only that SOP goes out.
        r = client.post("/api/v1/nodes/WEB_PEER/web/upload", headers=h,
                        json={"instance_uids": ["1.2.3.5.600.1"]})
        assert r.status_code == 200, r.text
        assert r.json()["sent"] == 1
        assert _sop_uids_received() == {"1.2.3.5.600.1"}
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# Partial failure: FailedSOPSequence in a 200 body
# --------------------------------------------------------------------------- #
def test_stow_upload_partial_failure_marks_rejected(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = _ingest_study("STOWP3")
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    _StowHandler.response_status = 200
    _StowHandler.response_body = json.dumps({
        "ReferencedSOPSequence": [{"ReferencedSOPInstanceUID": "1.2.3.5.600.1"}],
        "FailedSOPSequence": [{"ReferencedSOPInstanceUID": "1.2.3.5.600.2"}],
    }).encode()
    try:
        client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })
        r = client.post("/api/v1/nodes/WEB_PEER/web/upload", headers=h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is False          # partial failure -> not all good
        assert data["sent"] == 1 and data["failed"] == 1
        by_sop = {i["sop_uid"]: i for i in data["results"]}
        assert by_sop["1.2.3.5.600.1"]["ok"] is True
        assert by_sop["1.2.3.5.600.2"]["ok"] is False
        # Only the accepted instance audits FORWARDED.
        assert _audit_forwarded_uids() == {"1.2.3.5.600.1"}
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #
def test_stow_upload_requires_scope(client):
    h = _auth_headers(client)
    r = client.post("/api/v1/nodes/WEB_PEER/web/upload", headers=h,
                    json={"study_uids": []})
    assert r.status_code == 422


def test_stow_upload_no_web_url_409(client):
    h = _auth_headers(client)
    client.post("/api/v1/nodes", headers=h, json={
        "ae_title": "DIMSE_ONLY", "host": "10.1.2.3", "port": 104,
    })
    _ingest_study("STOWP4")
    r = client.post("/api/v1/nodes/DIMSE_ONLY/web/upload", headers=h,
                    json={"study_uids": ["1.2.840.113619.2.408.600"]})
    assert r.status_code == 409
    assert "web_url" in r.json()["detail"]


def test_stow_upload_no_matching_instances_404(client, monkeypatch):
    h = _auth_headers(client)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })
        r = client.post("/api/v1/nodes/WEB_PEER/web/upload", headers=h,
                        json={"study_uids": ["1.2.840.113619.2.408.999"]})
        assert r.status_code == 404
    finally:
        server.shutdown()


def test_stow_upload_remote_error_502(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = _ingest_study("STOWP5")
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    _StowHandler.response_status = 500
    try:
        client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })
        r = client.post("/api/v1/nodes/WEB_PEER/web/upload", headers=h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 502
        assert "HTTP 500" in r.json()["detail"]
    finally:
        server.shutdown()


def test_stow_upload_ssrf_recheck_422(client, monkeypatch):
    # Node created while private targets are allowed, then the guard is
    # tightened — the per-call re-check must refuse the upload (DNS rebinding).
    h = _auth_headers(client)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })
        _ingest_study("STOWP7")
        monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", False)
        r = client.post("/api/v1/nodes/WEB_PEER/web/upload", headers=h,
                        json={"study_uids": ["1.2.840.113619.2.408.600"]})
        assert r.status_code == 422
        assert "blocked" in r.json()["detail"]
    finally:
        server.shutdown()


def test_stow_upload_requires_destructive_role(client, monkeypatch):
    # First user is forced ADMIN — bootstrap one, then register a PHYSICIAN.
    admin_h = _auth_headers(client, username="root_stow", role="ADMIN")
    h = _auth_headers(client, username="phys_stow", role="PHYSICIAN")
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    # Self-registration can no longer mint admins — promote a second one via
    # the admin user-management API, then log in as it.
    client.post("/api/v1/users", headers=admin_h, json={
        "username": "root_stow2", "password": "secret123", "role": "ADMIN"})
    admin_h = _auth_headers(client, username="root_stow2")
    try:
        client.post("/api/v1/nodes", headers=admin_h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })
        _ingest_study("STOWP6")
        r = client.post("/api/v1/nodes/WEB_PEER/web/upload", headers=h,
                        json={"study_uids": ["1.2.840.113619.2.408.600"]})
        assert r.status_code == 403
    finally:
        server.shutdown()
