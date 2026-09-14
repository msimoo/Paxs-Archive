"""Tests for the Core-DICOM milestone: DICOM node registry API, C-MOVE SCP
(destination resolution + sub-operations over real DIMSE), WADO-URI and
webhooks (StudyReceived)."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pydicom
from pynetdicom import AE, StoragePresentationContexts, evt
from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelMove
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian
from sqlalchemy import select

from app.db.models import DicomNode, Instance
from app.db.session import SyncSessionLocal
from tests.helpers import ds_bytes, make_instance

from tests.test_receiver import _store_via_scu


# --------------------------------------------------------------------------- #
# Node registry API
# --------------------------------------------------------------------------- #
def _auth_headers(client, username="admin", password="secret123"):
    client.post("/api/v1/auth/register", json={"username": username, "password": password})
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def test_node_registry_crud(client):
    h = _auth_headers(client)
    # Empty list
    r = client.get("/api/v1/nodes", headers=h)
    assert r.status_code == 200 and r.json() == []

    # Create
    r = client.post("/api/v1/nodes", headers=h, json={
        "ae_title": "ARCHIVE_A", "host": "10.0.0.5", "port": 104,
        "description": "Remote archive", "enabled": True,
    })
    assert r.status_code == 201
    body = r.json()
    assert body["ae_title"] == "ARCHIVE_A" and body["port"] == 104

    # Duplicate -> 409
    r = client.post("/api/v1/nodes", headers=h, json={
        "ae_title": "ARCHIVE_A", "host": "10.0.0.6", "port": 105,
    })
    assert r.status_code == 409

    # List shows it
    r = client.get("/api/v1/nodes", headers=h)
    assert len(r.json()) == 1

    # Update (rename host + disable)
    r = client.put("/api/v1/nodes/ARCHIVE_A", headers=h, json={
        "ae_title": "ARCHIVE_A", "host": "10.0.0.9", "port": 11112, "enabled": False,
    })
    assert r.status_code == 200
    assert r.json()["host"] == "10.0.0.9" and r.json()["enabled"] is False

    # Delete
    r = client.delete("/api/v1/nodes/ARCHIVE_A", headers=h)
    assert r.status_code == 204
    assert client.get("/api/v1/nodes", headers=h).json() == []


def test_node_registry_requires_admin(client):
    # A non-admin (first user is admin, so register a second user)
    client.post("/api/v1/auth/register", json={"username": "adminx", "password": "secret123"})
    client.post("/api/v1/auth/register", json={"username": "dr.sara", "password": "secret123"})
    res = client.post("/api/v1/auth/login", json={"username": "dr.sara", "password": "secret123"})
    h = {"Authorization": f"Bearer {res.json()['access_token']}"}
    r = client.post("/api/v1/nodes", headers=h, json={
        "ae_title": "ARCHIVE_B", "host": "1.2.3.4", "port": 104,
    })
    assert r.status_code == 403


def test_node_update_delete_require_admin(client):
    # Seed a node as admin
    client.post("/api/v1/auth/register", json={"username": "boss", "password": "secret123"})
    res = client.post("/api/v1/auth/login", json={"username": "boss", "password": "secret123"})
    admin_h = {"Authorization": f"Bearer {res.json()['access_token']}"}
    client.post("/api/v1/nodes", headers=admin_h, json={
        "ae_title": "ARCHIVE_C", "host": "10.0.0.1", "port": 104,
    })

    # Non-admin gets 403 on PUT and DELETE
    client.post("/api/v1/auth/register", json={"username": "dr.omar", "password": "secret123"})
    res = client.post("/api/v1/auth/login", json={"username": "dr.omar", "password": "secret123"})
    h = {"Authorization": f"Bearer {res.json()['access_token']}"}
    r = client.put("/api/v1/nodes/ARCHIVE_C", headers=h, json={
        "ae_title": "ARCHIVE_C", "host": "10.0.0.2", "port": 105,
    })
    assert r.status_code == 403
    r = client.delete("/api/v1/nodes/ARCHIVE_C", headers=h)
    assert r.status_code == 403
    # Still present
    assert len(client.get("/api/v1/nodes", headers=admin_h).json()) == 1


def test_node_ping_echo(client, receiver):
    """POST /api/v1/nodes/{ae}/ping runs a real C-ECHO against the node."""
    port, _ae = receiver  # our test SCP accepts C-ECHO

    client.post("/api/v1/auth/register", json={"username": "pingadmin", "password": "secret123"})
    res = client.post("/api/v1/auth/login", json={"username": "pingadmin", "password": "secret123"})
    h = {"Authorization": f"Bearer {res.json()['access_token']}"}
    client.post("/api/v1/nodes", headers=h, json={
        "ae_title": "PAXS_TEST_SCP", "host": "127.0.0.1", "port": port,
    })
    r = client.post("/api/v1/nodes/PAXS_TEST_SCP/ping", headers=h)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Unreachable node -> graceful failure, not a 500
    client.post("/api/v1/nodes", headers=h, json={
        "ae_title": "GHOST", "host": "127.0.0.1", "port": 1,
    })
    r = client.post("/api/v1/nodes/GHOST/ping", headers=h)
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_node_registry_requires_auth(client):
    r = client.get("/api/v1/nodes")
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# C-MOVE SCP end-to-end (real DIMSE, two receivers)
# --------------------------------------------------------------------------- #
def _echo_only_scp(port, ae_title="PAXS_DEST"):
    """A minimal destination SCP that accepts C-STORE (records SOP UIDs) and
    C-ECHO, so the C-MOVE sub-operations have a real target."""
    from pynetdicom.sop_class import Verification

    received = []

    def on_store(event):
        received.append(str(event.dataset.SOPInstanceUID))
        return 0x0000

    def on_echo(event):
        return 0x0000

    ae = AE(ae_title=ae_title)
    ae.supported_contexts = StoragePresentationContexts
    ae.add_supported_context(Verification, ExplicitVRLittleEndian)
    ae.start_server(("127.0.0.1", port), block=False, evt_handlers=[
        (evt.EVT_C_STORE, on_store),
        (evt.EVT_C_ECHO, on_echo),
    ])
    return ae, received


def test_c_move_to_registered_destination(receiver):
    src_port, _ = receiver

    # Register the destination node in the DB so the SCP can resolve it.
    dest_port = _free_port()
    with SyncSessionLocal() as s:
        s.add(DicomNode(ae_title="PAXS_DEST", host="127.0.0.1", port=dest_port, enabled=True))
        s.commit()

    # Store one study (2 instances) into the source receiver.
    study_uid = "1.2.840.113619.2.408.77"
    ds1 = make_instance(study_uid=study_uid, series_uid="1.2.3.4.77.1",
                        sop_uid="1.2.3.5.77.1", instance_num=1)
    ds2 = make_instance(study_uid=study_uid, series_uid="1.2.3.4.77.1",
                        sop_uid="1.2.3.5.77.2", instance_num=2)
    assert _store_via_scu("127.0.0.1", src_port, [ds_bytes(ds1), ds_bytes(ds2)]) == [0x0000, 0x0000]

    for _ in range(50):
        with SyncSessionLocal() as s:
            if s.execute(select(Instance)).scalars().all():
                break
        time.sleep(0.1)

    # Start the destination SCP (accepts C-STORE sub-operations).
    dest_ae, received = _echo_only_scp(dest_port)

    try:
        # SCU sends C-MOVE to the source: move STUDY to PAXS_DEST.
        ae = AE(ae_title="PAXS_MOVE_SCU")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove, ExplicitVRLittleEndian)
        assoc = ae.associate("127.0.0.1", src_port, ae_title="PAXS_TEST_SCP")
        assert assoc.is_established

        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = study_uid

        final_status = None
        for status, _identifier in assoc.send_c_move(
            query, move_aet="PAXS_DEST", query_model=StudyRootQueryRetrieveInformationModelMove
        ):
            if status is not None:
                final_status = int(status.Status)
        assoc.release()

        assert final_status == 0x0000, f"C-MOVE failed: {final_status:#06x}"

        for _ in range(50):
            if len(received) == 2:
                break
            time.sleep(0.1)
        assert sorted(received) == ["1.2.3.5.77.1", "1.2.3.5.77.2"]
    finally:
        dest_ae.shutdown()


def test_c_move_unknown_destination(receiver):
    src_port, _ = receiver
    ae = AE(ae_title="PAXS_MOVE_SCU")
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove, ExplicitVRLittleEndian)
    assoc = ae.associate("127.0.0.1", src_port, ae_title="PAXS_TEST_SCP")
    assert assoc.is_established

    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    query.StudyInstanceUID = "1.2.3.4.999"

    statuses = [int(s.Status) for s, _ in assoc.send_c_move(
        query, move_aet="NO_SUCH_AE", query_model=StudyRootQueryRetrieveInformationModelMove
    ) if s is not None]
    assoc.release()

    # 0xA801 = Refused: Move Destination Unknown
    assert statuses and statuses[-1] == 0xA801


# --------------------------------------------------------------------------- #
# WADO-URI
# --------------------------------------------------------------------------- #
def test_wado_uri_returns_dicom(client):
    # Seed one instance directly through the API (STOW-RS)
    ds = make_instance(study_uid="1.2.840.113619.2.408.55", series_uid="1.2.3.4.55",
                       sop_uid="1.2.3.5.55.1")
    boundary = "wado-test-1"
    body = (
        f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
        + ds_bytes(ds) + b"\r\n" + f"--{boundary}--\r\n".encode()
    )
    r = client.post(
        "/dicom-web/studies",
        headers={"Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}'},
        content=body,
    )
    assert r.status_code == 200

    # WADO-URI fetch
    r = client.get("/dicom-web/wado", params={
        "requestType": "WADO",
        "studyUID": "1.2.840.113619.2.408.55",
        "seriesUID": "1.2.3.4.55",
        "objectUID": "1.2.3.5.55.1",
    })
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/dicom"
    ds_back = pydicom.dcmread(pydicom.filebase.BytesIO(r.content), force=True)
    assert str(ds_back.SOPInstanceUID) == "1.2.3.5.55.1"

    # WADO-URI rendered as PNG
    r = client.get("/dicom-web/wado", params={
        "requestType": "WADO", "objectUID": "1.2.3.5.55.1", "contentType": "image/png",
    })
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    # Missing objectUID -> 400
    r = client.get("/dicom-web/wado", params={"requestType": "WADO"})
    assert r.status_code == 400

    # Unknown instance -> 404
    r = client.get("/dicom-web/wado", params={
        "requestType": "WADO", "objectUID": "1.2.3.5.99.99",
    })
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
class _CaptureHandler(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _CaptureHandler.received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def test_webhook_study_received(client, monkeypatch):
    _CaptureHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    monkeypatch.setattr("app.core.config.settings.WEBHOOK_URLS", f"http://127.0.0.1:{port}/hook")
    try:
        ds = make_instance(study_uid="1.2.840.113619.2.408.66")
        boundary = "hook-test-1"
        body = (
            f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
            + ds_bytes(ds) + b"\r\n" + f"--{boundary}--\r\n".encode()
        )
        r = client.post(
            "/dicom-web/studies",
            headers={"Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}'},
            content=body,
        )
        assert r.status_code == 200

        for _ in range(50):
            if _CaptureHandler.received:
                break
            time.sleep(0.1)
        assert len(_CaptureHandler.received) == 1
        payload = _CaptureHandler.received[0]
        assert payload["event"] == "StudyReceived"
        assert payload["study_uid"] == "1.2.840.113619.2.408.66"
        assert payload["patient_id"] == "P0001"
        assert "occurred_at" in payload
    finally:
        server.shutdown()


def test_webhook_not_fired_for_existing_study(client, monkeypatch):
    _CaptureHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    monkeypatch.setattr("app.core.config.settings.WEBHOOK_URLS", f"http://127.0.0.1:{port}/hook")
    try:
        study_uid = "1.2.840.113619.2.408.67"
        for i in (1, 2):
            ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.67",
                               sop_uid=f"1.2.3.5.67.{i}", instance_num=i)
            boundary = f"hook-test-{i}"
            body = (
                f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
                + ds_bytes(ds) + b"\r\n" + f"--{boundary}--\r\n".encode()
            )
            r = client.post(
                "/dicom-web/studies",
                headers={"Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}'},
                content=body,
            )
            assert r.status_code == 200

        # Second instance of the same study must NOT re-fire the webhook.
        time.sleep(0.5)
        assert len(_CaptureHandler.received) == 1
    finally:
        server.shutdown()


def test_send_move_scu(receiver):
    """app.dicom.sender.send_move issues a real C-MOVE and reports the final
    status (exercised against our SCP with an unknown destination)."""
    from app.dicom.sender import send_move

    src_port, _ = receiver
    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    query.StudyInstanceUID = "1.2.3.4.555"

    result = send_move("127.0.0.1", src_port, "PAXS_TEST_SCP", "NOWHERE_AE", query=query)
    assert result["ok"] is False
    assert result["status"] == 0xA801  # Refused: Move destination unknown


def _free_port():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
