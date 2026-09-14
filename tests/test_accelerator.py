"""Transfer Accelerator tests (feature 19).

The accelerator pushes a study's instances to a peer's DICOMweb service over
N parallel STOW-RS connections with chunked transfer encoding (bypassing
DIMSE C-STORE). These tests exercise the real endpoint
(``/api/v1/nodes/{ae}/web/upload?accelerated=1``) against a loopback HTTP
server that decodes both Content-Length and chunked request bodies, records
what arrived, and can simulate transient failures for the retry path.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from sqlalchemy import select

from app.db.models import AuditLog
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import dicomweb_client, indexing_service, storage_service
from tests.helpers import make_instance


def _ingest_study(patient_id: str, n_instances: int = 8,
                  study_uid: str = "1.2.840.113619.2.408.700") -> str:
    """Save + index a local study; returns its StudyInstanceUID."""
    from app.dicom.parser import parse_metadata

    for i in range(1, n_instances + 1):
        ds = make_instance(study_uid=study_uid, series_uid=f"1.2.3.4.700.{i}",
                           sop_uid=f"1.2.3.5.700.{i}", instance_num=i,
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


class _AccelHandler(BaseHTTPRequestHandler):
    """Loopback STOW-RS peer — accepts chunked + Content-Length bodies and
    can fail the first N requests (transient-error simulation)."""

    received_parts: list = []
    received_ct: str = ""
    received_te: str = ""
    received_sops: set = set()   # accumulated across every (parallel) request
    request_count: int = 0
    fail_first_n: int = 0
    response_status: int = 200
    response_body: bytes = b""

    def _read_body(self) -> bytes:
        te = self.headers.get("Transfer-Encoding", "")
        if "chunked" in te.lower():
            body = b""
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line, 16)
                except ValueError:
                    break
                if size == 0:
                    while True:  # trailers until the blank line
                        tail = self.rfile.readline()
                        if tail in (b"\r\n", b"\n", b""):
                            break
                    break
                body += self.rfile.read(size)
                self.rfile.read(2)  # CRLF after the chunk
            return body
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_POST(self):
        _AccelHandler.request_count += 1
        body = self._read_body()
        _AccelHandler.received_ct = self.headers.get("Content-Type", "")
        _AccelHandler.received_te = self.headers.get("Transfer-Encoding", "")
        _AccelHandler.received_parts = list(
            dicomweb_client._iter_multipart_parts(body, _AccelHandler.received_ct)
        )
        for part in _AccelHandler.received_parts:
            ds = dicomweb_client._dataset_from_bytes(part)
            if ds is not None and getattr(ds, "SOPInstanceUID", None):
                _AccelHandler.received_sops.add(str(ds.SOPInstanceUID))
        path = urlparse(self.path).path
        if path.startswith("/dicom-web"):
            path = path[len("/dicom-web"):] or "/"
        if path != "/studies":
            self.send_response(404)
            self.end_headers()
            return
        status = 500 if _AccelHandler.request_count <= _AccelHandler.fail_first_n \
            else _AccelHandler.response_status
        self.send_response(status)
        self.send_header("Content-Type", "application/dicom+json")
        self.send_header("Content-Length", str(len(_AccelHandler.response_body)))
        self.end_headers()
        if _AccelHandler.response_body:
            self.wfile.write(_AccelHandler.response_body)

    def log_message(self, *args):
        pass


def _start_stow_server():
    _AccelHandler.received_parts = []
    _AccelHandler.received_ct = ""
    _AccelHandler.received_te = ""
    _AccelHandler.received_sops = set()
    _AccelHandler.request_count = 0
    _AccelHandler.fail_first_n = 0
    _AccelHandler.response_status = 200
    _AccelHandler.response_body = b""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AccelHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _auth_headers(client, username="accel_admin", password="secret123", role="ADMIN"):
    reg = client.post("/api/v1/auth/register", json={"username": username, "password": password,
                                                     "role": role})
    if reg.status_code == 201:
        token = reg.json()["access_token"]
    else:
        login = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sop_uids_received():
    return set(_AccelHandler.received_sops)


def _audit_forwarded_uids():
    with sync_engine.connect() as conn:
        return {
            row[0] for row in conn.execute(
                select(AuditLog.sop_instance_uid).where(AuditLog.action == "FORWARDED")
            ).all()
        }


def _register_peer(client, h, port):
    r = client.post("/api/v1/nodes", headers=h, json={
        "ae_title": "ACCEL_PEER", "host": "127.0.0.1", "port": port,
        "web_url": f"http://127.0.0.1:{port}/dicom-web",
    })
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------- #
# Success paths
# --------------------------------------------------------------------------- #
def test_accelerated_upload_sends_all_instances(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = _ingest_study("ACCP1", n_instances=8)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        _register_peer(client, h, port)

        r = client.post("/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1", headers=h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["transport"] == "dicomweb"
        assert data["sent"] == 8 and data["failed"] == 0
        assert data["accelerator"] is True
        assert data["streamed"] is True
        assert data["batches"] >= 1 and data["workers"] >= 1
        assert data["elapsed_seconds"] > 0
        assert data["bytes"] > 0 and data["mb_per_sec"] >= 0

        # The peer received every instance, streamed as chunked HTTP.
        assert _sop_uids_received() == {f"1.2.3.5.700.{i}" for i in range(1, 9)}
        assert "multipart/related" in _AccelHandler.received_ct

        # Every transferred instance is audit-logged as FORWARDED.
        assert _audit_forwarded_uids() == {f"1.2.3.5.700.{i}" for i in range(1, 9)}
    finally:
        server.shutdown()


def test_accelerated_upload_streams_ndjson_progress(client, monkeypatch):
    """?progress=1 streams NDJSON: per-batch progress events that advance to
    the confirmed-sent count, then a final done event carrying the full result
    (same shape as the non-streaming JSON response)."""
    h = _auth_headers(client)
    study_uid = _ingest_study("ACCPB", n_instances=8)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        _register_peer(client, h, port)

        r = client.post(
            "/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1&progress=1",
            headers=h, json={"study_uids": [study_uid]},
        )
        assert r.status_code == 200, r.text
        assert "ndjson" in r.headers.get("content-type", "")

        events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
        progress = [e for e in events if e["event"] == "progress"]
        done = [e for e in events if e["event"] == "done"]
        errors = [e for e in events if e["event"] == "error"]

        # Progress advanced to the full confirmed-sent count; every event
        # total matches the instance count.
        assert errors == []
        assert progress, "expected at least one progress event"
        assert all(e["total"] == 8 for e in progress)
        assert progress[-1]["done"] == 8
        assert len(progress) <= 8  # one event per resolved batch at most
        assert sorted(e["done"] for e in progress) == sorted(set(e["done"] for e in progress))

        # Final done event carries the full result — same fields as JSON mode.
        assert len(done) == 1
        result = done[0]["result"]
        assert result["ok"] is True and result["transport"] == "dicomweb"
        assert result["sent"] == 8 and result["failed"] == 0
        assert result["accelerator"] is True
        assert result["ae_title"] == "ACCEL_PEER"

        # The peer got everything and the audit trail exists.
        assert _sop_uids_received() == {f"1.2.3.5.700.{i}" for i in range(1, 9)}
        assert _audit_forwarded_uids() == {f"1.2.3.5.700.{i}" for i in range(1, 9)}
    finally:
        server.shutdown()


def test_accelerated_upload_progress_error_event_when_all_fail(client, monkeypatch):
    """All batches failing still streams a terminal error event (instead of
    raising a 502 after the stream opened) — nothing is audited."""
    h = _auth_headers(client)
    study_uid = _ingest_study("ACCPC", n_instances=3)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    _AccelHandler.fail_first_n = 99  # every attempt dies
    try:
        _register_peer(client, h, port)
        r = client.post(
            "/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1&progress=1",
            headers=h, json={"study_uids": [study_uid]},
        )
        assert r.status_code == 200, r.text  # stream itself opened fine
        events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
        assert events[-1]["event"] == "error"
        assert "failed" in events[-1]["message"]
        assert not any(e["event"] == "done" for e in events)
        assert _audit_forwarded_uids() == set()
    finally:
        server.shutdown()


def test_accelerated_upload_uses_chunked_transfer_encoding(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = _ingest_study("ACCP2", n_instances=3)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        _register_peer(client, h, port)
        r = client.post("/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1", headers=h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 200, r.text
        # The accelerator streams each batch (no Content-Length, chunked TE).
        assert "chunked" in _AccelHandler.received_te.lower()
    finally:
        server.shutdown()


def test_accelerated_upload_batches_by_config(client, monkeypatch):
    from app.services import accelerator_service

    h = _auth_headers(client)
    study_uid = _ingest_study("ACCP3", n_instances=6)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    monkeypatch.setattr(accelerator_service.settings, "TRANSFER_ACCELERATOR_BATCH_SIZE", 2)
    try:
        _register_peer(client, h, port)
        r = client.post("/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1&workers=3", headers=h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["sent"] == 6
        assert data["batches"] == 3          # 6 instances / batch size 2
        assert data["workers"] == 3
        assert _sop_uids_received() == {f"1.2.3.5.700.{i}" for i in range(1, 7)}
    finally:
        server.shutdown()


def test_accelerated_upload_retries_transient_batch(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = _ingest_study("ACCP4", n_instances=4)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    _AccelHandler.fail_first_n = 1  # first batch request dies with HTTP 500
    try:
        _register_peer(client, h, port)
        r = client.post("/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1", headers=h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["sent"] == 4 and data["failed"] == 0   # retried, not lost
        assert _AccelHandler.request_count == 2            # 1 fail + 1 retry
        assert _sop_uids_received() == {f"1.2.3.5.700.{i}" for i in range(1, 5)}
    finally:
        server.shutdown()


def test_accelerated_upload_partial_rejection_is_final(client, monkeypatch):
    """A per-instance rejection (FailedSOPSequence in a 200 body) is final —
    it must NOT trigger a whole-batch retry."""
    h = _auth_headers(client)
    study_uid = _ingest_study("ACCP9", n_instances=4)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    _AccelHandler.response_body = json.dumps({
        "ReferencedSOPSequence": [
            {"ReferencedSOPInstanceUID": "1.2.3.5.700.1"},
            {"ReferencedSOPInstanceUID": "1.2.3.5.700.2"},
            {"ReferencedSOPInstanceUID": "1.2.3.5.700.3"},
        ],
        "FailedSOPSequence": [{"ReferencedSOPInstanceUID": "1.2.3.5.700.4"}],
    }).encode()
    try:
        _register_peer(client, h, port)
        r = client.post("/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1", headers=h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["sent"] == 3 and data["failed"] == 1
        assert _AccelHandler.request_count == 1   # rejected -> no whole-batch retry
        assert _audit_forwarded_uids() == {"1.2.3.5.700.1", "1.2.3.5.700.2", "1.2.3.5.700.3"}
    finally:
        server.shutdown()


def test_accelerated_upload_disabled_falls_back_to_plain_http(client, monkeypatch):
    from app.services import accelerator_service

    h = _auth_headers(client)
    study_uid = _ingest_study("ACCP5", n_instances=3)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    monkeypatch.setattr(accelerator_service.settings, "TRANSFER_ACCELERATOR_ENABLED", False)
    try:
        _register_peer(client, h, port)
        r = client.post("/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1", headers=h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["sent"] == 3 and data["accelerator"] is False
        assert data["streamed"] is False       # non-streamed single body
        assert "chunked" not in _AccelHandler.received_te.lower()
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #
def test_accelerated_upload_all_fail_surfaces_502(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = _ingest_study("ACCP6", n_instances=3)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    _AccelHandler.fail_first_n = 99  # every attempt dies
    try:
        _register_peer(client, h, port)
        r = client.post("/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1", headers=h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 502
        assert "one or more batches failed" in r.json()["detail"]
        assert _audit_forwarded_uids() == set()  # nothing transferred -> no audit
    finally:
        server.shutdown()


def test_accelerated_upload_ssrf_recheck_422(client, monkeypatch):
    h = _auth_headers(client)
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        _register_peer(client, h, port)
        _ingest_study("ACCP7", n_instances=2)
        # Tighten the guard after node creation — the per-call re-check must
        # refuse the accelerated upload (DNS-rebinding protection).
        monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", False)
        r = client.post("/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1", headers=h,
                        json={"study_uids": ["1.2.840.113619.2.408.700"]})
        assert r.status_code == 422
        assert "blocked" in r.json()["detail"]
    finally:
        server.shutdown()


def test_accelerated_upload_requires_scope(client):
    h = _auth_headers(client)
    r = client.post("/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1", headers=h,
                    json={"study_uids": []})
    assert r.status_code == 422


def test_accelerated_upload_requires_destructive_role(client, monkeypatch):
    admin_h = _auth_headers(client, username="root_accel", role="ADMIN")
    h = _auth_headers(client, username="phys_accel", role="PHYSICIAN")
    server, port = _start_stow_server()
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    # Self-registration can no longer mint admins — promote a second one via
    # the admin user-management API, then log in as it.
    client.post("/api/v1/users", headers=admin_h, json={
        "username": "root_accel2", "password": "secret123", "role": "ADMIN"})
    admin_h = _auth_headers(client, username="root_accel2")
    try:
        _register_peer(client, admin_h, port)
        _ingest_study("ACCP8", n_instances=2)
        r = client.post("/api/v1/nodes/ACCEL_PEER/web/upload?accelerated=1", headers=h,
                        json={"study_uids": ["1.2.840.113619.2.408.700"]})
        assert r.status_code == 403
    finally:
        server.shutdown()
