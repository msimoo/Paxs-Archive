"""Feature #1 — C-STORE (SCP/SCU) milestone tests.

The SCP (receive) side is covered in test_receiver / test_vna / test_hl7 /
test_tls. This suite exercises the SCU (send) side end to end over real DIMSE:

  * ``sender.send_file`` round-trip to a destination SCP,
  * ``sender.send_to_node_sync`` — resolve a registered ``dicom_nodes`` entry
    and forward a whole study (local backend),
  * the REST trigger ``POST /api/v1/nodes/{ae}/send`` with audit trail,
  * VNA scoping — a tenant user cannot forward another tenant's study,
  * S3/MinIO materialization — remote objects are temp-materialized before send,
  * error paths — unknown/disabled node, empty scope, non-admin.
"""

import socket
import time

from pynetdicom import AE, StoragePresentationContexts, evt
from pynetdicom.sop_class import Verification
from pydicom.uid import ExplicitVRLittleEndian
from sqlalchemy import select

from app.db.models import AuditLog, DicomNode, Study
from app.db.session import SyncSessionLocal
from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers
from tests.test_receiver import _store_via_scu


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _recording_scp(port, ae_title="PAXS_DEST"):
    """A minimal destination SCP that accepts C-STORE (records SOP UIDs) and
    C-ECHO — the SCU target for forwarding tests."""
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
        (evt.EVT_C_STORE, on_store), (evt.EVT_C_ECHO, on_echo),
    ])
    return ae, received


def _wait_for(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met within timeout")


def _auth(client, username, password="secret123", role=None):
    body = {"username": username, "password": password}
    if role:
        body["role"] = role
    client.post("/api/v1/auth/register", json=body)
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _register_node(client, admin_h, ae_title, host, port, enabled=True):
    r = client.post("/api/v1/nodes", headers=admin_h, json={
        "ae_title": ae_title, "host": host, "port": port, "enabled": enabled,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _stow(client, ds, boundary="cstore-1"):
    r = client.post(
        "/dicom-web/studies",
        headers=stow_headers(boundary),
        content=build_stow_body([ds_bytes(ds)], boundary=boundary),
    )
    assert r.status_code == 200, r.text
    return r


def _wait_stored(study_uid):
    def _check():
        with SyncSessionLocal() as s:
            return s.execute(
                select(Study).where(Study.study_instance_uid == study_uid)
            ).scalar_one_or_none()
    return _wait_for(lambda: _check() is not None)


# --------------------------------------------------------------------------- #
# send_file round-trip
# --------------------------------------------------------------------------- #
def test_send_file_scu_round_trip():
    """sender.send_file C-STORE's one instance to a peer SCP over real DIMSE."""
    port = _free_port()
    dest_ae, received = _recording_scp(port)
    try:
        from app.dicom.sender import send_file

        ds = make_instance(study_uid="1.2.840.113619.2.408.600", series_uid="1.2.3.4.600",
                           sop_uid="1.2.3.5.600.1")
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp()) / "one.dcm"
        ds.save_as(tmp, enforce_file_format=True)
        res = send_file("127.0.0.1", port, "PAXS_DEST", tmp)
        assert res["ok"] is True, res
        _wait_for(lambda: len(received) == 1)
        assert received == ["1.2.3.5.600.1"]
    finally:
        dest_ae.shutdown()


def test_send_file_rejects_bad_peer():
    """An unreachable peer yields a graceful failure dict, not an exception."""
    from app.dicom.sender import send_file

    ds = make_instance(study_uid="1.2.840.113619.2.408.601", sop_uid="1.2.3.5.601.1")
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "x.dcm"
    ds.save_as(tmp, enforce_file_format=True)
    res = send_file("127.0.0.1", 1, "GHOST", tmp)  # port 1 -> refused
    assert res["ok"] is False
    assert "reason" in res


# --------------------------------------------------------------------------- #
# send_to_node_sync (node-aware SCU, local backend)
# --------------------------------------------------------------------------- #
def _failing_scp(port, refuse_uid, ae_title="PAXS_DEST"):
    """Destination SCP that refuses one specific SOP UID (0xC212) and accepts
    the rest — locks in per-instance failure accounting."""
    received = []

    def on_store(event):
        uid = str(event.dataset.SOPInstanceUID)
        if uid == refuse_uid:
            return 0xC212  # Processing failure
        received.append(uid)
        return 0x0000

    ae = AE(ae_title=ae_title)
    ae.supported_contexts = StoragePresentationContexts
    ae.add_supported_context(Verification, ExplicitVRLittleEndian)
    ae.start_server(("127.0.0.1", port), block=False, evt_handlers=[
        (evt.EVT_C_STORE, on_store),
    ])
    return ae, received


def test_send_to_node_sync_partial_failure(receiver):
    """One refused instance fails the item, not the whole study forward."""
    src_port, _ = receiver
    study_uid = "1.2.840.113619.2.408.611"
    ds1 = make_instance(study_uid=study_uid, series_uid="1.2.3.4.611",
                        sop_uid="1.2.3.5.611.1", instance_num=1)
    ds2 = make_instance(study_uid=study_uid, series_uid="1.2.3.4.611",
                        sop_uid="1.2.3.5.611.2", instance_num=2)
    assert _store_via_scu("127.0.0.1", src_port, [ds_bytes(ds1), ds_bytes(ds2)]) == [0x0000, 0x0000]
    _wait_stored(study_uid)

    dest_port = _free_port()
    dest_ae, received = _failing_scp(dest_port, refuse_uid="1.2.3.5.611.2")
    try:
        with SyncSessionLocal() as s:
            s.add(DicomNode(ae_title="PAXS_DEST", host="127.0.0.1", port=dest_port, enabled=True))
            s.commit()

        from app.dicom.sender import send_to_node_sync

        result = send_to_node_sync("PAXS_DEST", study_uids=[study_uid])
        assert result["ok"] is False
        assert result["sent"] == 1 and result["failed"] == 1
        by_uid = {r["sop_uid"]: r for r in result["results"]}
        assert by_uid["1.2.3.5.611.1"]["ok"] is True
        assert by_uid["1.2.3.5.611.2"]["ok"] is False
        _wait_for(lambda: len(received) == 1)
        assert received == ["1.2.3.5.611.1"]
    finally:
        dest_ae.shutdown()


# --------------------------------------------------------------------------- #
# send_to_node_sync (node-aware SCU, local backend)
# --------------------------------------------------------------------------- #
def test_send_to_node_sync_forwards_study(receiver):
    """resolve node -> C-STORE every instance of a study to the destination."""
    src_port, _ = receiver

    # Seed a 2-instance study into the source archive via DIMSE.
    study_uid = "1.2.840.113619.2.408.610"
    ds1 = make_instance(study_uid=study_uid, series_uid="1.2.3.4.610",
                        sop_uid="1.2.3.5.610.1", instance_num=1)
    ds2 = make_instance(study_uid=study_uid, series_uid="1.2.3.4.610",
                        sop_uid="1.2.3.5.610.2", instance_num=2)
    assert _store_via_scu("127.0.0.1", src_port, [ds_bytes(ds1), ds_bytes(ds2)]) == [0x0000, 0x0000]
    _wait_stored(study_uid)

    # Register the destination node in the registry (what C-MOVE also resolves).
    dest_port = _free_port()
    with SyncSessionLocal() as s:
        s.add(DicomNode(ae_title="PAXS_DEST", host="127.0.0.1", port=dest_port, enabled=True))
        s.commit()

    dest_ae, received = _recording_scp(dest_port)
    try:
        from app.dicom.sender import send_to_node_sync

        result = send_to_node_sync("PAXS_DEST", study_uids=[study_uid])
        assert result["ok"] is True, result
        assert result["sent"] == 2 and result["failed"] == 0
        _wait_for(lambda: len(received) == 2)
        assert sorted(received) == ["1.2.3.5.610.1", "1.2.3.5.610.2"]
    finally:
        dest_ae.shutdown()


def test_send_to_node_sync_unknown_node():
    from app.dicom.sender import send_to_node_sync

    result = send_to_node_sync("NO_SUCH_AE", study_uids=["1.2.3.4"])
    assert result["ok"] is False and result["reason"] == "Node not found"


def test_send_to_node_sync_disabled_node(receiver):
    src_port, _ = receiver
    study_uid = "1.2.840.113619.2.408.612"
    ds = make_instance(study_uid=study_uid, sop_uid="1.2.3.5.612.1")
    assert _store_via_scu("127.0.0.1", src_port, [ds_bytes(ds)]) == [0x0000]
    _wait_stored(study_uid)

    with SyncSessionLocal() as s:
        s.add(DicomNode(ae_title="DISABLED", host="127.0.0.1", port=1, enabled=False))
        s.commit()

    from app.dicom.sender import send_to_node_sync

    result = send_to_node_sync("DISABLED", study_uids=[study_uid])
    assert result["ok"] is False and result["reason"] == "Node is disabled"


# --------------------------------------------------------------------------- #
# REST trigger: POST /api/v1/nodes/{ae}/send
# --------------------------------------------------------------------------- #
def test_rest_send_forwards_study_and_audits(client):
    admin_h = _auth(client, "boss.cstore")

    # STOW a study into the archive.
    study_uid = "1.2.840.113619.2.408.620"
    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.620",
                       sop_uid="1.2.3.5.620.1", instance_num=1)
    _stow(client, ds, boundary="cstore-620")
    _wait_stored(study_uid)

    dest_port = _free_port()
    dest_ae, received = _recording_scp(dest_port)
    try:
        _register_node(client, admin_h, "PAXS_DEST", "127.0.0.1", dest_port)
        r = client.post("/api/v1/nodes/PAXS_DEST/send", headers=admin_h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["sent"] == 1 and body["failed"] == 0
        assert body["ae_title"] == "PAXS_DEST"

        _wait_for(lambda: len(received) == 1)
        assert received == ["1.2.3.5.620.1"]

        # Audit trail carries the FORWARDED event.
        with SyncSessionLocal() as s:
            rows = s.execute(
                select(AuditLog).where(AuditLog.action == "FORWARDED")
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].sop_instance_uid == "1.2.3.5.620.1"
            assert rows[0].performed_by == "boss.cstore"
    finally:
        dest_ae.shutdown()


def test_rest_send_series_scope(client):
    """Series-level scope forwards only the requested series' instances."""
    admin_h = _auth(client, "boss.series")
    study_uid = "1.2.840.113619.2.408.621"
    ds_a = make_instance(study_uid=study_uid, series_uid="1.2.3.4.621.1",
                         sop_uid="1.2.3.5.621.1", instance_num=1)
    ds_b = make_instance(study_uid=study_uid, series_uid="1.2.3.4.621.2",
                         sop_uid="1.2.3.5.621.2", instance_num=1)
    _stow(client, ds_a, boundary="cstore-621a")
    _stow(client, ds_b, boundary="cstore-621b")
    _wait_stored(study_uid)

    dest_port = _free_port()
    dest_ae, received = _recording_scp(dest_port)
    try:
        _register_node(client, admin_h, "PAXS_DEST", "127.0.0.1", dest_port)
        r = client.post("/api/v1/nodes/PAXS_DEST/send", headers=admin_h,
                        json={"series_uids": ["1.2.3.4.621.2"]})
        assert r.status_code == 200, r.text
        assert r.json()["sent"] == 1
        _wait_for(lambda: len(received) == 1)
        assert received == ["1.2.3.5.621.2"]
    finally:
        dest_ae.shutdown()


def test_rest_send_validation(client):
    admin_h = _auth(client, "boss.val")
    dest_port = _free_port()
    dest_ae, _ = _recording_scp(dest_port)
    try:
        _register_node(client, admin_h, "PAXS_DEST", "127.0.0.1", dest_port)
        # Empty scope -> 422
        r = client.post("/api/v1/nodes/PAXS_DEST/send", headers=admin_h, json={})
        assert r.status_code == 422
        # Unknown node -> 404
        r = client.post("/api/v1/nodes/NO_SUCH/send", headers=admin_h,
                        json={"study_uids": ["1.2.3.4"]})
        assert r.status_code == 404
        # Disabled node -> 409
        _register_node(client, admin_h, "DISABLED", "127.0.0.1", dest_port, enabled=False)
        r = client.post("/api/v1/nodes/DISABLED/send", headers=admin_h,
                        json={"study_uids": ["1.2.3.4"]})
        assert r.status_code == 409
        # Study that doesn't exist -> 404
        r = client.post("/api/v1/nodes/PAXS_DEST/send", headers=admin_h,
                        json={"study_uids": ["1.2.3.4.999.999"]})
        assert r.status_code == 404
        # Unauthenticated -> 401
        r = client.post("/api/v1/nodes/PAXS_DEST/send",
                        json={"study_uids": ["1.2.3.4"]})
        assert r.status_code == 401
    finally:
        dest_ae.shutdown()


def test_rest_send_requires_destructive_role(client):
    client.post("/api/v1/auth/register", json={"username": "chief.x", "password": "secret123"})
    # PHYSICIAN (not ADMIN/RADIOLOGIST) cannot forward.
    admin_h = _auth(client, "chief.x")
    _register_node(client, admin_h, "PAXS_DEST", "127.0.0.1", 1)
    phys_h = _auth(client, "dr.phys", role="PHYSICIAN")
    r = client.post("/api/v1/nodes/PAXS_DEST/send", headers=phys_h,
                    json={"study_uids": ["1.2.3.4"]})
    assert r.status_code == 403


def test_rest_send_vna_scoped(client):
    """A tenant user cannot forward another tenant's study (opaque 404)."""
    admin_h = _auth(client, "vna.boss")
    # Create a tenant + assign a radiologist to it.
    r = client.post("/api/v1/tenants", headers=admin_h,
                    json={"name": "Tenant C", "code": "tc"})
    assert r.status_code == 201, r.text
    tenant_id = r.json()["id"]
    rad_h = _auth(client, "dr.tenantc", role="RADIOLOGIST")
    assert client.post(f"/api/v1/tenants/{tenant_id}/users", headers=admin_h,
                       json={"username": "dr.tenantc"}).status_code == 200

    # A study lands in the legacy unscoped scope (STOW by admin).
    study_uid = "1.2.840.113619.2.408.630"
    _stow(client, make_instance(study_uid=study_uid, series_uid="1.2.3.4.630",
                                sop_uid="1.2.3.5.630.1"))
    _wait_stored(study_uid)

    dest_port = _free_port()
    dest_ae, received = _recording_scp(dest_port)
    try:
        _register_node(client, admin_h, "PAXS_DEST", "127.0.0.1", dest_port)
        # The tenant radiologist sees no matching instances -> opaque 404.
        r = client.post("/api/v1/nodes/PAXS_DEST/send", headers=rad_h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 404
        time.sleep(0.3)
        assert received == []
        # The admin (include_all) can forward the same study.
        r = client.post("/api/v1/nodes/PAXS_DEST/send", headers=admin_h,
                        json={"study_uids": [study_uid]})
        assert r.status_code == 200 and r.json()["sent"] == 1
        _wait_for(lambda: len(received) == 1)
    finally:
        dest_ae.shutdown()


# --------------------------------------------------------------------------- #
# SCU over S3/MinIO backend (temp-materialize before send)
# --------------------------------------------------------------------------- #
def test_send_to_node_sync_over_s3_materializes(client, monkeypatch):
    """Remote (S3) instances are materialized to temp files before C-STORE."""
    from app.core.config import settings
    from app.services.storage import get_storage_backend, reset_storage_backend
    from tests.test_storage_backend import FakeS3Client

    fake = FakeS3Client()
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "S3_BUCKET", "paxs-test")
    reset_storage_backend()
    backend = get_storage_backend()
    backend._client = fake  # replace the lazy real client with the fake
    try:
        admin_h = _auth(client, "s3.boss")
        study_uid = "1.2.840.113619.2.408.640"
        _stow(client, make_instance(study_uid=study_uid, series_uid="1.2.3.4.640",
                                    sop_uid="1.2.3.5.640.1"), boundary="cstore-640")
        _wait_stored(study_uid)
        with SyncSessionLocal() as s:
            study = s.execute(select(Study).where(Study.study_instance_uid == study_uid)).scalar_one()
            inst_key = study.series_list[0].instances[0].file_path
        assert backend.local_path(inst_key) is None  # remote, no local path

        dest_port = _free_port()
        dest_ae, received = _recording_scp(dest_port)
        try:
            _register_node(client, admin_h, "PAXS_DEST", "127.0.0.1", dest_port)
            r = client.post("/api/v1/nodes/PAXS_DEST/send", headers=admin_h,
                            json={"study_uids": [study_uid]})
            assert r.status_code == 200, r.text
            assert r.json()["sent"] == 1
            _wait_for(lambda: len(received) == 1)
            assert received == ["1.2.3.5.640.1"]
        finally:
            dest_ae.shutdown()
    finally:
        reset_storage_backend()
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
