"""Tests for the Storage Commitment Push Model SCP — Phase A3.

Real DIMSE N-ACTION -> N-EVENT-REPORT round trips over the network:

  * stored instances -> ack 0x0000, then a report marking them all success
  * request-first flow -> the report fires automatically once C-STORE lands
  * unknown SOP Instance UIDs -> FailureReason 0x0112 (no such object)
  * DB row with deleted file -> FailureReason 0x0110 (processing failure)
  * closed association -> report retried on a fresh association via the
    dicom_nodes registry
  * REST surface: auth gating + listing
"""

import json
import time

from pynetdicom import AE, evt
from pynetdicom.sop_class import CTImageStorage, StorageCommitmentPushModel
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from app.db.models import DicomNode, Instance, StorageCommitRequest
from app.db.session import SyncSessionLocal
from app.services import storage_service
from tests.conftest import free_port
from tests.helpers import ds_bytes, make_instance
from tests.test_receiver import _store_via_scu

SCU_AE = "PAXS_COMMIT_SCU"
received: list = []


def _on_event_report(event):
    """SCU-side handler: records the N-EVENT-REPORT Event Information.

    pynetdicom does not expose a decoded `event.dataset` for N-EVENT-REPORT
    in this version (only C-STORE), so we decode the raw Event Information
    with the context's transfer syntax — the same approach the receiver uses
    for N-CREATE/N-SET/N-ACTION.
    """
    from pynetdicom.dsutils import decode

    raw = event.request.EventInformation
    ts = event.context.transfer_syntax
    ds = decode(raw, ts.is_implicit_VR, ts.is_little_endian, ts.is_deflated)
    received.append(ds)
    return 0x0000, None


def _scu(port, ae_title=SCU_AE):
    """Associate as a modality SCU that can also receive N-EVENT-REPORTs."""
    received.clear()
    ae = AE(ae_title=ae_title)
    ae.add_requested_context(StorageCommitmentPushModel, ExplicitVRLittleEndian)
    ae.add_supported_context(StorageCommitmentPushModel, ExplicitVRLittleEndian)
    assoc = ae.associate(
        "127.0.0.1", port, ae_title="PAXS_TEST_SCP",
        evt_handlers=[(evt.EVT_N_EVENT_REPORT, _on_event_report)],
    )
    assert assoc.is_established, "storage commitment association failed"
    return ae, assoc


def _send_n_action(assoc, tx_uid, refs):
    ds = Dataset()
    ds.TransactionUID = tx_uid
    ds.ReferencedSOPSequence = []
    for cls, sop in refs:
        item = Dataset()
        item.ReferencedSOPClassUID = cls
        item.ReferencedSOPInstanceUID = sop
        ds.ReferencedSOPSequence.append(item)
    status, _ = assoc.send_n_action(ds, 1, StorageCommitmentPushModel, tx_uid)
    return int(status.Status) if status is not None else None


def _wait_report(tx_uid, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for ds in list(received):
            if str(getattr(ds, "TransactionUID", "")) == tx_uid:
                return ds
        time.sleep(0.1)
    return None


def _get_request(tx_uid):
    with SyncSessionLocal() as s:
        return s.query(StorageCommitRequest).filter(
            StorageCommitRequest.transaction_uid == tx_uid).one_or_none()


# --------------------------------------------------------------------------- #
# Real DIMSE round trips
# --------------------------------------------------------------------------- #
def test_n_action_ack_and_report_for_stored_instances(receiver):
    port, _ = receiver
    sop1, sop2 = generate_uid(), generate_uid()
    ds1 = make_instance(study_uid="1.2.840.113619.2.408.300",
                        series_uid="1.2.3.4.300", sop_uid=sop1)
    ds2 = make_instance(study_uid="1.2.840.113619.2.408.300",
                        series_uid="1.2.3.4.300", sop_uid=sop2)
    assert _store_via_scu("127.0.0.1", port, [ds_bytes(ds1), ds_bytes(ds2)]) == [0x0000, 0x0000]

    tx_uid = generate_uid()
    _, assoc = _scu(port)
    try:
        assert _send_n_action(assoc, tx_uid, [(CTImageStorage, sop1), (CTImageStorage, sop2)]) == 0x0000
        report = _wait_report(tx_uid)
        assert report is not None
        items = report.ReferencedSOPSequence
        assert {str(i.ReferencedSOPInstanceUID) for i in items} == {sop1, sop2}
        # every stored instance succeeds (no FailureReason element)
        assert all(not hasattr(i, "FailureReason") for i in items)
        # the request row is finalized as delivered (poll: the sender marks
        # COMPLETED after the RSP returns, slightly after the report lands)
        for _ in range(50):
            if _get_request(tx_uid) and _get_request(tx_uid).status == "COMPLETED":
                break
            time.sleep(0.1)
        assert _get_request(tx_uid).status == "COMPLETED"
    finally:
        assoc.release()


def test_report_fires_automatically_after_c_store(receiver):
    """The headline behaviour: a modality asks for commitment *before* its
    images land; the N-EVENT-REPORT fires on its own once C-STORE completes —
    no second N-ACTION needed."""
    port, _ = receiver
    tx_uid = generate_uid()
    sop = generate_uid()

    _, assoc = _scu(port)
    try:
        assert _send_n_action(assoc, tx_uid, [(CTImageStorage, sop)]) == 0x0000
        # nothing verifiable yet -> request stays PENDING, no premature report
        assert _get_request(tx_uid).status == "PENDING"
        time.sleep(0.5)
        assert not received, "report fired before the instances were stored"

        # now the images arrive -> the C-STORE hook must complete the request
        ds = make_instance(study_uid="1.2.840.113619.2.408.301",
                           series_uid="1.2.3.4.301", sop_uid=sop)
        assert _store_via_scu("127.0.0.1", port, [ds_bytes(ds)]) == [0x0000]

        report = _wait_report(tx_uid)
        assert report is not None
        item = report.ReferencedSOPSequence[0]
        assert str(item.ReferencedSOPInstanceUID) == sop
        assert not hasattr(item, "FailureReason")
        # The report is delivered on the modality's thread; the sender marks
        # the row COMPLETED only after the RSP arrives — poll instead of
        # asserting on a fast DB (MySQL commits slower than the network hop).
        for _ in range(50):
            if _get_request(tx_uid).status == "COMPLETED":
                break
            time.sleep(0.1)
        assert _get_request(tx_uid).status == "COMPLETED"
    finally:
        assoc.release()


def test_unknown_instance_reported_0112(receiver):
    """A reference that never existed (and never will) is answered with
    FailureReason 0x0112 after the PENDING grace period — no hang forever."""
    port, _ = receiver
    tx_uid = generate_uid()
    unknown = generate_uid()

    _, assoc = _scu(port)
    try:
        assert _send_n_action(assoc, tx_uid, [(CTImageStorage, unknown)]) == 0x0000
        report = _wait_report(tx_uid, timeout=20)
        assert report is not None
        item = report.ReferencedSOPSequence[0]
        assert str(item.ReferencedSOPInstanceUID) == unknown
        assert int(item.FailureReason) == 0x0112
        # sender marks COMPLETED only after the RSP returns — poll, don't assert
        for _ in range(50):
            if _get_request(tx_uid) and _get_request(tx_uid).status == "COMPLETED":
                break
            time.sleep(0.1)
        assert _get_request(tx_uid).status == "COMPLETED"
    finally:
        assoc.release()


def test_missing_file_reported_0110(receiver):
    """A DB row whose object is gone from the storage backend fails with
    0x0110 — commitment reflects the backend, not just the row."""
    port, _ = receiver
    sop = generate_uid()
    ds = make_instance(study_uid="1.2.840.113619.2.408.302",
                       series_uid="1.2.3.4.302", sop_uid=sop)
    assert _store_via_scu("127.0.0.1", port, [ds_bytes(ds)]) == [0x0000]

    with SyncSessionLocal() as s:
        inst = s.query(Instance).filter(Instance.sop_instance_uid == sop).one()
        assert storage_service.delete_file(inst.file_path)

    tx_uid = generate_uid()
    _, assoc = _scu(port)
    try:
        assert _send_n_action(assoc, tx_uid, [(CTImageStorage, sop)]) == 0x0000
        report = _wait_report(tx_uid, timeout=20)
        assert report is not None
        item = report.ReferencedSOPSequence[0]
        assert str(item.ReferencedSOPInstanceUID) == sop
        assert int(item.FailureReason) == 0x0110
    finally:
        assoc.release()


def test_report_retried_via_node_fallback(receiver):
    """If the modality closes the association before the report can be
    delivered, the sender re-associates to its AE via the dicom_nodes
    registry and delivers on a fresh association."""
    port, _ = receiver
    node_port = free_port()
    modality_reports: list = []

    def _on_report(event):
        from pynetdicom.dsutils import decode

        raw = event.request.EventInformation
        ts = event.context.transfer_syntax
        modality_reports.append(
            decode(raw, ts.is_implicit_VR, ts.is_little_endian, ts.is_deflated)
        )
        return 0x0000, None

    mae = AE(ae_title="PAXS_MODALITY")
    mae.add_supported_context(StorageCommitmentPushModel, ExplicitVRLittleEndian)
    mae.start_server(("127.0.0.1", node_port), block=False,
                     evt_handlers=[(evt.EVT_N_EVENT_REPORT, _on_report)])
    try:
        with SyncSessionLocal() as s:
            s.add(DicomNode(ae_title="PAXS_MODALITY", host="127.0.0.1", port=node_port))
            s.commit()

        tx_uid = generate_uid()
        sop = generate_uid()
        _, assoc = _scu(port, ae_title="PAXS_MODALITY")
        try:
            assert _send_n_action(assoc, tx_uid, [(CTImageStorage, sop)]) == 0x0000
        finally:
            assoc.release()  # close BEFORE the images land -> sender must fall back

        ds = make_instance(study_uid="1.2.840.113619.2.408.303",
                           series_uid="1.2.3.4.303", sop_uid=sop)
        assert _store_via_scu("127.0.0.1", port, [ds_bytes(ds)]) == [0x0000]

        for _ in range(100):
            if modality_reports:
                break
            time.sleep(0.1)
        assert modality_reports, "report never delivered via fresh association"
        item = modality_reports[0].ReferencedSOPSequence[0]
        assert str(item.ReferencedSOPInstanceUID) == sop
        assert not hasattr(item, "FailureReason")
        for _ in range(50):
            if _get_request(tx_uid) and _get_request(tx_uid).status == "COMPLETED":
                break
            time.sleep(0.1)
        assert _get_request(tx_uid).status == "COMPLETED"
    finally:
        mae.shutdown()


# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #
def _admin_headers(client):
    client.post("/api/v1/auth/register", json={"username": "commitboss", "password": "secret123"})
    res = client.post("/api/v1/auth/login", json={"username": "commitboss", "password": "secret123"})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def test_commitments_api_lists_requests(client):
    h = _admin_headers(client)
    with SyncSessionLocal() as s:
        s.add(StorageCommitRequest(
            transaction_uid="1.2.3.4.999", ae_title="CT1", status="PENDING",
            payload_json=json.dumps({
                "ae_title": "CT1",
                "requested": [{"sop_class_uid": str(CTImageStorage), "sop_instance_uid": "1.2.3.5.999"}],
                "results": [{"sop_instance_uid": "1.2.3.5.999", "ok": False, "failure_reason": 0x0112}],
            }),
        ))
        s.add(StorageCommitRequest(
            transaction_uid="1.2.3.4.1000", ae_title="MR3", status="COMPLETED",
            payload_json=json.dumps({
                "ae_title": "MR3",
                "requested": [{"sop_class_uid": str(CTImageStorage), "sop_instance_uid": "1.2.3.5.1000"}],
                "results": [{"sop_instance_uid": "1.2.3.5.1000", "ok": True}],
            }),
        ))
        s.commit()

    r = client.get("/api/v1/commitments", headers=h)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    failed = next(x for x in rows if x["transaction_uid"] == "1.2.3.4.999")
    assert failed["status"] == "PENDING"
    assert failed["instance_count"] == 1
    assert failed["verified_count"] == 0
    assert failed["failures"][0]["failure_reason"] == 0x0112
    ok = next(x for x in rows if x["transaction_uid"] == "1.2.3.4.1000")
    assert ok["status"] == "COMPLETED" and ok["verified_count"] == 1

    r = client.get("/api/v1/commitments?status=COMPLETED", headers=h)
    assert len(r.json()) == 1


def test_commitments_api_requires_auth(client):
    assert client.get("/api/v1/commitments").status_code == 401
