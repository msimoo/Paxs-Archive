"""Feature #5 — C-ECHO (SCP/SCU) milestone tests.

The Verification SOP Class is the DICOM "ping": a lightweight end-to-end proof
that a peer really speaks DICOM. The SCP side is auto-handled by pynetdicom
whenever the receiver advertises the Verification context. This suite pins the
SCU helper and the REST trigger:

  * ``sender.send_echo`` round-trips against our own receiver,
  * it interoperates with a minimal echo-only peer SCP,
  * it fails *gracefully* (no exception, no hang) against unreachable hosts
    and against TCP servers that do not speak DICOM at all,
  * the receiver answers a direct C-ECHO with 0x0000 (SCP proof),
  * ``POST /api/v1/nodes/{ae}/ping`` maps unknown/disabled/unauthenticated and
    surfaces a 200 with ``ok:false`` for unreachable nodes.
"""

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from pynetdicom import AE
from pynetdicom.sop_class import Verification
from pydicom.uid import ExplicitVRLittleEndian


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _PlainHttpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"not dicom")

    def do_POST(self):
        self.do_GET()

    def log_message(self, *args):
        pass


def _http_server():
    """A TCP server that speaks HTTP, not DICOM — send_echo must refuse it."""
    server = HTTPServer(("127.0.0.1", 0), _PlainHttpHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _auth(client, username, password="secret123", role=None):
    body = {"username": username, "password": password}
    if role:
        body["role"] = role
    client.post("/api/v1/auth/register", json=body)
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


# --------------------------------------------------------------------------- #
# send_echo SCU
# --------------------------------------------------------------------------- #
def test_send_echo_round_trip_against_receiver(receiver):
    """send_echo succeeds against our own SCP and reports the success status."""
    from app.dicom.sender import send_echo

    port, _ = receiver
    result = send_echo("127.0.0.1", port, "PAXS_TEST_SCP")
    assert result["ok"] is True
    assert result["status"] == 0x0000
    assert result.get("reason") is None


def test_send_echo_interoperates_with_echo_only_peer():
    """The SCU works against a minimal peer that only supports Verification."""
    from app.dicom.sender import send_echo

    port = _free_port()
    ae = AE(ae_title="PAXS_ECHO_PEER")
    ae.add_supported_context(Verification, ExplicitVRLittleEndian)
    ae.start_server(("127.0.0.1", port), block=False)
    try:
        result = send_echo("127.0.0.1", port, "PAXS_ECHO_PEER")
        assert result["ok"] is True
        assert result["status"] == 0x0000
    finally:
        ae.shutdown()


def test_send_echo_custom_calling_ae():
    """our_ae_title is respected on the wire (the peer records who pinged)."""
    from app.dicom.sender import send_echo

    seen = []

    def on_echo(event):
        seen.append(event.assoc.requestor.ae_title)
        return 0x0000

    port = _free_port()
    from pynetdicom import evt

    ae = AE(ae_title="PAXS_ECHO_PEER")
    ae.add_supported_context(Verification, ExplicitVRLittleEndian)
    ae.start_server(("127.0.0.1", port), block=False,
                    evt_handlers=[(evt.EVT_C_ECHO, on_echo)])
    try:
        result = send_echo("127.0.0.1", port, "PAXS_ECHO_PEER", our_ae_title="PAXS_HEALTH")
        assert result["ok"] is True
        assert seen == ["PAXS_HEALTH"]
    finally:
        ae.shutdown()


def test_send_echo_unreachable_peer_is_graceful():
    """An unreachable host yields a clean failure dict — no exception, no hang."""
    from app.dicom.sender import send_echo

    result = send_echo("127.0.0.1", 1, "GHOST")  # port 1 -> connection refused
    assert result["ok"] is False
    assert result["reason"] == "association rejected"


def test_send_echo_refuses_non_dicom_server():
    """A TCP server that speaks HTTP is not a DICOM peer — association fails
    gracefully instead of hanging."""
    from app.dicom.sender import send_echo

    server = _http_server()
    try:
        result = send_echo("127.0.0.1", server.server_port, "NOT_DICOM")
        assert result["ok"] is False
        assert result["reason"] == "association rejected"
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# SCP side
# --------------------------------------------------------------------------- #
def test_receiver_answers_c_echo(receiver):
    """Direct C-ECHO against the receiver returns success (SCP proof)."""
    port, _ = receiver
    ae = AE(ae_title="PAXS_ECHO_SCU")
    ae.add_requested_context(Verification, ExplicitVRLittleEndian)
    assoc = ae.associate("127.0.0.1", port, ae_title="PAXS_TEST_SCP")
    assert assoc.is_established
    status = assoc.send_c_echo()
    assoc.release()
    assert status is not None and int(status.Status) == 0x0000


# --------------------------------------------------------------------------- #
# REST trigger: POST /api/v1/nodes/{ae}/ping
# --------------------------------------------------------------------------- #
def test_rest_ping_success_and_unreachable(client, receiver):
    """Ping a reachable node (200 ok:true) and an unreachable one (200 ok:false
    with a reason — a connectivity probe, not an HTTP error)."""
    port, _ = receiver
    admin_h = _auth(client, "echo.admin")

    r = client.post("/api/v1/nodes", headers=admin_h, json={
        "ae_title": "PAXS_TEST_SCP", "host": "127.0.0.1", "port": port,
    })
    assert r.status_code == 201
    r = client.post("/api/v1/nodes/PAXS_TEST_SCP/ping", headers=admin_h)
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["status"] == 0x0000

    r = client.post("/api/v1/nodes", headers=admin_h, json={
        "ae_title": "GHOST", "host": "127.0.0.1", "port": 1,
    })
    assert r.status_code == 201
    r = client.post("/api/v1/nodes/GHOST/ping", headers=admin_h)
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "association rejected"


def test_rest_ping_error_paths(client):
    admin_h = _auth(client, "echo.err")
    client.post("/api/v1/nodes", headers=admin_h, json={
        "ae_title": "DISABLED", "host": "127.0.0.1", "port": 104, "enabled": False,
    })

    # Unknown node -> 404
    r = client.post("/api/v1/nodes/NO_SUCH/ping", headers=admin_h)
    assert r.status_code == 404
    # Disabled node -> 409
    r = client.post("/api/v1/nodes/DISABLED/ping", headers=admin_h)
    assert r.status_code == 409
    # No auth -> 401
    r = client.post("/api/v1/nodes/NO_SUCH/ping")
    assert r.status_code == 401


def test_rest_ping_available_to_any_authenticated_user(client, receiver):
    """Ping is a read-only connectivity probe — a non-admin user may use it."""
    port, _ = receiver
    admin_h = _auth(client, "echo.boss")
    client.post("/api/v1/nodes", headers=admin_h, json={
        "ae_title": "PAXS_TEST_SCP", "host": "127.0.0.1", "port": port,
    })
    phys_h = _auth(client, "dr.echo", role="PHYSICIAN")
    r = client.post("/api/v1/nodes/PAXS_TEST_SCP/ping", headers=phys_h)
    assert r.status_code == 200
    assert r.json()["ok"] is True
