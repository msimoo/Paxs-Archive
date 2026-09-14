"""Tests for remote-node DICOMweb transport (QIDO-RS query / WADO-RS retrieve).

  * SSRF guard — validate_web_url refuses loopback/private/link-local targets
    by default and embedded credentials; allow_private opt-out works.
  * Node registry — web_url/web_username fields persist and are validated on
    create/update.
  * QIDO-RS — /web/query against a mock DICOMweb server returns C-FIND-shaped
    matches; error + missing-web_url paths surface cleanly.
  * WADO-RS — /web/retrieve parses a multipart/related body and ingests every
    instance through the normal pipeline (idempotent, audit PULLED).
  * Roles — query/retrieve require ADMIN/RADIOLOGIST (destructive role).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from sqlalchemy import select

from app.db.models import AuditLog, Instance
from app.db.session import SyncSessionLocal
from app.services import dicomweb_client
from app.services.dicomweb_client import validate_web_url
from tests.helpers import ds_bytes, make_instance

# --------------------------------------------------------------------------- #
# SSRF guard
# --------------------------------------------------------------------------- #
def test_validate_web_url_blocks_loopback_by_default(monkeypatch):
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", False)
    for bad in (
        "http://127.0.0.1:8080/dicom-web",
        "http://localhost:8042/dicom-web",
        "http://[::1]:8080/dicom-web",
        "http://10.0.0.5/dicom-web",
        "http://192.168.1.10/dicom-web",
        "http://169.254.169.254/latest/meta-data",
    ):
        try:
            validate_web_url(bad)
        except ValueError as exc:
            assert "blocked" in str(exc) or "protected" in str(exc), str(exc)
        else:
            raise AssertionError(f"expected {bad} to be rejected")


def test_validate_web_url_rejects_bad_shape():
    for bad in ("ftp://x/y", "not-a-url", "", "  ",
                "http://user:pass@host/dicom-web", "http:///dicom-web"):
        try:
            validate_web_url(bad, allow_private=True)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected {bad!r} to be rejected")


def test_validate_web_url_accepts_public_and_allows_private(monkeypatch):
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", False)
    # A numeric public IP resolves without DNS (works in offline sandboxes).
    ok = validate_web_url("https://8.8.8.8/dicom-web")
    assert ok == "https://8.8.8.8/dicom-web"
    # A public hostname is fine when it resolves publicly (guard allows it).
    monkeypatch.setattr(
        dicomweb_client.socket, "getaddrinfo",
        lambda host, *a, **k: [(0, 0, 0, "", ("8.8.4.4", 0))],
    )
    ok = validate_web_url("https://pacs.example.org/dicom-web")
    assert ok == "https://pacs.example.org/dicom-web"
    # Explicit opt-out permits loopback (integration test peers).
    ok = validate_web_url("http://127.0.0.1:9000/dicom-web", allow_private=True)
    assert ok == "http://127.0.0.1:9000/dicom-web"


# --------------------------------------------------------------------------- #
# Node registry web fields
# --------------------------------------------------------------------------- #
def _auth_headers(client, username="webadmin", password="secret123"):
    client.post("/api/v1/auth/register", json={"username": username, "password": password})
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def test_node_web_fields_crud_and_ssrf_on_create(client, monkeypatch):
    h = _auth_headers(client)
    # SSRF guard applies at creation: loopback is refused while private blocked.
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", False)
    r = client.post("/api/v1/nodes", headers=h, json={
        "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": 9000,
        "web_url": "http://127.0.0.1:9000/dicom-web",
    })
    assert r.status_code == 422
    assert "blocked" in r.json()["detail"]

    # With the opt-out, creation succeeds and the fields round-trip.
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    r = client.post("/api/v1/nodes", headers=h, json={
        "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": 9000,
        "description": "DICOMweb peer",
        "web_url": "http://127.0.0.1:9000/dicom-web/",
        "web_username": "u", "web_password": "p",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["web_url"] == "http://127.0.0.1:9000/dicom-web"  # normalized
    assert body["has_web_auth"] is True

    # Malformed URL still refused even when private allowed.
    r = client.post("/api/v1/nodes", headers=h, json={
        "ae_title": "BAD_WEB", "host": "x", "port": 104,
        "web_url": "ftp://bad/dicom-web",
    })
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Mock remote DICOMweb server (QIDO-RS + WADO-RS)
# --------------------------------------------------------------------------- #
class _WebHandler(BaseHTTPRequestHandler):
    datasets: list = []          # DICOM datasets served as the "remote archive"
    seen_headers: list = []

    def do_GET(self):
        path = urlparse(self.path).path
        # The node's web_url base is /dicom-web — strip it, then route.
        if path.startswith("/dicom-web"):
            path = path[len("/dicom-web"):] or "/"
        _WebHandler.seen_headers.append(dict(self.headers))
        if path == "/studies":
            # QIDO-RS: return one JSON match per unique study.
            studies = {}
            for ds in self.datasets:
                uid = str(ds.StudyInstanceUID)
                studies.setdefault(uid, {
                    "StudyInstanceUID": {"Value": [uid]},
                    "PatientID": {"Value": [str(ds.PatientID)]},
                    "PatientName": {"Value": [str(ds.PatientName)]},
                    "StudyDate": {"Value": [str(ds.StudyDate)]},
                    "StudyDescription": {"Value": [str(ds.StudyDescription)]},
                    "ModalitiesInStudy": {"Value": [str(ds.Modality)]},
                    "AccessionNumber": {"Value": [str(ds.AccessionNumber)]},
                })
            body = json.dumps(list(studies.values())).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/dicom+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/studies/"):
            # WADO-RS: multipart/related with one DICOM part per instance.
            boundary = "webnode-boundary-xyz"
            parts = []
            for ds in self.datasets:
                if str(ds.StudyInstanceUID) in path:
                    parts.append(
                        f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
                        + ds_bytes(ds) + b"\r\n"
                    )
            body = b"".join(parts) + f"--{boundary}--\r\n".encode()
            self.send_response(200)
            self.send_header("Content-Type",
                             f'multipart/related; type="application/dicom"; boundary={boundary}')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


def _start_web_server(datasets):
    _WebHandler.datasets = datasets
    _WebHandler.seen_headers = []
    server = HTTPServer(("127.0.0.1", 0), _WebHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


# --------------------------------------------------------------------------- #
# QIDO-RS query end-to-end
# --------------------------------------------------------------------------- #
def test_web_query_qido(client, monkeypatch):
    h = _auth_headers(client)
    ds = make_instance(study_uid="1.2.840.113619.2.408.90", patient_id="WEBP1",
                       accession="ACCWEB1")
    server, port = _start_web_server([ds])
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        r = client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })
        assert r.status_code == 201

        r = client.post("/api/v1/nodes/WEB_PEER/web/query", headers=h,
                        json={"level": "STUDY", "filters": {"PatientID": "WEBP1"}})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["transport"] == "dicomweb"
        assert len(data["matches"]) == 1
        m = data["matches"][0]
        assert m["StudyInstanceUID"] == "1.2.840.113619.2.408.90"
        assert m["PatientID"] == "WEBP1"

        # Auth header reaches the mock server only when web credentials exist;
        # without them no Authorization header is sent.
        assert not any("authorization" in {k.lower(): v for k, v in hh.items()}
                       for hh in _WebHandler.seen_headers)
    finally:
        server.shutdown()


def test_web_query_requires_destructive_role(client, monkeypatch):
    # First user is admin; register a plain PHYSICIAN.
    client.post("/api/v1/auth/register", json={"username": "webadmin", "password": "secret123"})
    client.post("/api/v1/auth/register", json={"username": "dr.doc", "password": "secret123"})
    res = client.post("/api/v1/auth/login", json={"username": "dr.doc", "password": "secret123"})
    h = {"Authorization": f"Bearer {res.json()['access_token']}"}
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    admin_h = _auth_headers(client, username="webadmin2")
    client.post("/api/v1/nodes", headers=admin_h, json={
        "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": 9000,
        "web_url": "http://127.0.0.1:9000/dicom-web",
    })
    r = client.post("/api/v1/nodes/WEB_PEER/web/query", headers=h,
                    json={"level": "STUDY", "filters": {}})
    assert r.status_code == 403


def test_web_actions_without_web_url(client):
    h = _auth_headers(client)
    client.post("/api/v1/nodes", headers=h, json={
        "ae_title": "DIMSE_ONLY", "host": "10.1.2.3", "port": 104,
    })
    r = client.post("/api/v1/nodes/DIMSE_ONLY/web/query", headers=h,
                    json={"level": "STUDY", "filters": {}})
    assert r.status_code == 409
    assert "web_url" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# WADO-RS retrieve end-to-end
# --------------------------------------------------------------------------- #
def test_web_retrieve_wado_ingests_instances(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = "1.2.840.113619.2.408.91"
    ds1 = make_instance(study_uid=study_uid, series_uid="1.2.3.4.91.1",
                        sop_uid="1.2.3.5.91.1", instance_num=1, patient_id="WEBP2")
    ds2 = make_instance(study_uid=study_uid, series_uid="1.2.3.4.91.1",
                        sop_uid="1.2.3.5.91.2", instance_num=2, patient_id="WEBP2")
    server, port = _start_web_server([ds1, ds2])
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })

        r = client.post("/api/v1/nodes/WEB_PEER/web/retrieve", headers=h,
                        json={"level": "STUDY", "filters": {"StudyInstanceUID": study_uid}})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["pulled"] == 2 and data["failed"] == 0

        # Both instances now live in the local archive.
        with SyncSessionLocal() as s:
            uids = s.execute(select(Instance.sop_instance_uid)).scalars().all()
            assert sorted(uids) == ["1.2.3.5.91.1", "1.2.3.5.91.2"]
            actions = {a.action for a in s.execute(select(AuditLog)).scalars().all()}
            assert "PULLED" in actions

        # Missing StudyInstanceUID -> 422
        r = client.post("/api/v1/nodes/WEB_PEER/web/retrieve", headers=h,
                        json={"level": "STUDY", "filters": {}})
        assert r.status_code == 422
    finally:
        server.shutdown()


def test_web_retrieve_remote_error_surfaces_502(client, monkeypatch):
    h = _auth_headers(client)
    server, port = _start_web_server([])  # empty archive: /studies/{uid} -> 200 empty multipart
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })
        # Empty multipart body → zero parts → ok=False → 502.
        r = client.post("/api/v1/nodes/WEB_PEER/web/retrieve", headers=h,
                        json={"level": "STUDY",
                              "filters": {"StudyInstanceUID": "1.2.840.113619.2.408.92"}})
        assert r.status_code == 502
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# Client unit: multipart parsing + QIDO row normalization
# --------------------------------------------------------------------------- #
def test_multipart_parser_splits_dicom_parts():
    ds = make_instance(study_uid="1.2.840.113619.2.408.93", sop_uid="1.2.3.5.93.1")
    boundary = "unit-boundary"
    body = (
        f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
        + ds_bytes(ds) + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )
    parts = list(dicomweb_client._iter_multipart_parts(
        body, f'multipart/related; type="application/dicom"; boundary={boundary}'))
    assert len(parts) == 1
    parsed = dicomweb_client._dataset_from_bytes(parts[0])
    assert str(parsed.SOPInstanceUID) == "1.2.3.5.93.1"


def test_qido_to_match_rows_normalizes_values():
    matches = [
        {"PatientName": {"Value": ["Doe^John"]}, "StudyInstanceUID": {"Value": ["1.2.3"]},
         "ModalitiesInStudy": {"Value": ["CT", "MR"]}},
        {"PatientID": "BARE"},  # bare scalar tolerated
    ]
    rows = dicomweb_client.qido_to_match_rows("STUDY", matches)
    assert rows[0]["PatientName"] == "Doe^John"
    assert rows[0]["StudyInstanceUID"] == "1.2.3"
    assert rows[0]["ModalitiesInStudy"] == "CT\\MR"  # multi-value joined, C-FIND parity
    assert rows[1]["PatientID"] == "BARE"


def test_qido_path_substitutes_study_series_uids():
    assert dicomweb_client._qido_path("STUDY", {}) == "/studies"
    path = dicomweb_client._qido_path(
        "SERIES", {"StudyInstanceUID": "1.2.3.4"})
    assert path == "/studies/1.2.3.4/series"
    path = dicomweb_client._qido_path(
        "INSTANCE", {"StudyInstanceUID": "1.2.3.4", "SeriesInstanceUID": "1.2.3.5"})
    assert path == "/studies/1.2.3.4/series/1.2.3.5/instances"

    # Missing required UIDs -> ValueError (the API maps it to 422).
    try:
        dicomweb_client._qido_path("SERIES", {})
    except ValueError:
        pass
    else:
        raise AssertionError("SERIES without StudyInstanceUID must raise")
    try:
        dicomweb_client._qido_path("INSTANCE", {"StudyInstanceUID": "1.2.3.4"})
    except ValueError:
        pass
    else:
        raise AssertionError("INSTANCE without SeriesInstanceUID must raise")
