"""Tests for lifecycle auto-archive on WADO-RS pulls (item 14 integration).

A study retrieved from a remote node over WADO-RS (Web Retrieve) can be
tiered straight to the cold ``.archive/`` prefix:

  * ``auto_archive: true`` in the request tiers the pulled study to cold
    storage right after ingest (state ARCHIVED, instance keys under
    ``.archive/``).
  * the default (flag off) keeps the study in the hot tier (state ONLINE).
  * the global ``LIFECYCLE_AUTO_ARCHIVE_WADO_PULL`` setting archives every
    pull even without the per-request flag.

Reuses the mock remote DICOMweb server from the web-node tests.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from sqlalchemy import select

from app.core.config import settings
from app.db.models import Instance, Series, Study
from app.db.session import SyncSessionLocal
from app.services import dicomweb_client, lifecycle_service
from tests.helpers import ds_bytes, make_instance

ARCHIVE_PREFIX = lifecycle_service.ARCHIVE_PREFIX


def _auth_headers(client, username="web_admin", password="secret123"):
    client.post("/api/v1/auth/register", json={"username": username, "password": password})
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


class _WebHandler(BaseHTTPRequestHandler):
    datasets: list = []

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/dicom-web"):
            path = path[len("/dicom-web"):] or "/"
        if path.startswith("/studies/"):
            boundary = "autoarchive-boundary"
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
    server = HTTPServer(("127.0.0.1", 0), _WebHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _pull(client, headers, study_uid, **extra):
    r = client.post(
        "/api/v1/nodes/WEB_PEER/web/retrieve", headers=headers,
        json={"level": "STUDY", "filters": {"StudyInstanceUID": study_uid}, **extra},
    )
    return r


def _study_key(study_uid: str) -> str:
    with SyncSessionLocal() as s:
        key = (
            s.execute(
                select(Instance.file_path)
                .join(Series, Series.id == Instance.series_id)
                .where(Series.study_id == select(Study.id)
                       .where(Study.study_instance_uid == study_uid).scalar_subquery())
            ).scalars().first()
        )
    return key


def _study_state(study_uid: str) -> str:
    with SyncSessionLocal() as s:
        return s.execute(
            select(Study.lifecycle_state).where(Study.study_instance_uid == study_uid)
        ).scalar_one()


# --------------------------------------------------------------------------- #
# auto_archive flag tiers the pulled study to cold storage
# --------------------------------------------------------------------------- #
def test_wado_pull_with_auto_archive_tiers_study(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = "1.2.840.113619.2.408.101"
    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.101.1",
                       sop_uid="1.2.3.5.101.1", patient_id="WAAP1")
    server, port = _start_web_server([ds])
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })

        r = _pull(client, h, study_uid, auto_archive=True)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["pulled"] == 1
        assert data["auto_archived"] is True
        assert data["archive_state"] == "ARCHIVED"

        # Study is ARCHIVED and its object moved under the cold prefix.
        assert _study_state(study_uid) == "ARCHIVED"
        key = _study_key(study_uid)
        assert key and key.startswith(ARCHIVE_PREFIX)
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# Default (no flag) keeps the study in the hot tier
# --------------------------------------------------------------------------- #
def test_wado_pull_default_stays_online(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = "1.2.840.113619.2.408.102"
    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.102.1",
                       sop_uid="1.2.3.5.102.1", patient_id="WAAP2")
    server, port = _start_web_server([ds])
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    try:
        client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })

        r = _pull(client, h, study_uid)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["pulled"] == 1
        assert data.get("auto_archived") is False
        assert data.get("archive_state") is None

        assert _study_state(study_uid) == "ONLINE"
        key = _study_key(study_uid)
        assert key and not key.startswith(ARCHIVE_PREFIX)
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# Global LIFECYCLE_AUTO_ARCHIVE_WADO_PULL applies to every pull
# --------------------------------------------------------------------------- #
def test_wado_pull_global_setting_archives_every_pull(client, monkeypatch):
    h = _auth_headers(client)
    study_uid = "1.2.840.113619.2.408.103"
    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.103.1",
                       sop_uid="1.2.3.5.103.1", patient_id="WAAP3")
    server, port = _start_web_server([ds])
    monkeypatch.setattr(dicomweb_client.settings, "DICOMWEB_CLIENT_ALLOW_PRIVATE", True)
    monkeypatch.setattr(settings, "LIFECYCLE_AUTO_ARCHIVE_WADO_PULL", True)
    try:
        client.post("/api/v1/nodes", headers=h, json={
            "ae_title": "WEB_PEER", "host": "127.0.0.1", "port": port,
            "web_url": f"http://127.0.0.1:{port}/dicom-web",
        })

        # No per-request flag — the global setting alone triggers tiering.
        r = _pull(client, h, study_uid)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["auto_archived"] is True
        assert data["archive_state"] == "ARCHIVED"
        assert _study_state(study_uid) == "ARCHIVED"
        assert _study_key(study_uid).startswith(ARCHIVE_PREFIX)
    finally:
        server.shutdown()
