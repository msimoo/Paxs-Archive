"""DICOM SCU primitives: one association, one answer.

Scope: the operations that open exactly one association and report one result
— C-STORE of a single file, C-ECHO verification, and the MPPS N-CREATE/N-SET
pair — plus the directory walker built on top of ``send_file``. Nothing here
knows about the archive (no DB session, no storage keys); the batching and
query layers in the sibling modules build on these primitives.

Why the seam indirection (:func:`_facade`): these helpers used to live in a
single ``app.dicom.sender`` module, and the test suite patches that module's
attributes (``sender.AE``, ``sender._tls_args``) to drive a fake association.
Resolving those names through the facade at call time keeps the split
behaviour-preserving for those seams.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pydicom
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian, generate_uid
from app.dicom.sender.association import association_failure_reason
from pynetdicom import build_role, evt
from pynetdicom.dsutils import decode
from pynetdicom.sop_class import StorageCommitmentPushModel

log = logging.getLogger("paxs.dicom.sender")

_FACADE = "app.dicom.sender"


def _configure_scu_ae(ae, timeout: float | None = None) -> None:
    """Apply finite association and network timeouts to every outbound AE."""
    if timeout is None:
        ae.acse_timeout = 30
        ae.dimse_timeout = 30
        ae.network_timeout = 60
        return
    bounded = max(0.1, min(float(timeout), 60.0))
    ae.acse_timeout = bounded
    ae.dimse_timeout = bounded
    ae.network_timeout = max(bounded, 1.0)


def _facade() -> ModuleType:
    """The public ``app.dicom.sender`` namespace, resolved on every call."""
    return sys.modules[_FACADE]


def _tls_args(peer_host: str, trust_cert: str | Path | None = None):
    """(context, server_hostname) when DICOM TLS is enabled, else None.

    The same global switch that turns TLS on for the receiver also upgrades
    every outbound association (node ping, forwarding, C-MOVE) — so a TLS
    archive talks to TLS peers without per-call plumbing. `peer_host` is the
    address we actually connect to: it feeds TLS hostname verification.

    B-16: `trust_cert` is the peer's pinned certificate from the node registry,
    used when no global CA is configured. ``tls.client_context`` raises
    ``TlsConfigurationError`` rather than returning an unverified context, so a
    production deployment that cannot authenticate the peer refuses to connect.
    """
    from app.dicom.tls import scu_tls_args

    return scu_tls_args(peer_host, trust_cert)


def _add_store_context(ae: Any, sop_class: str, transfer_syntax: str) -> None:
    """Request the file's transfer syntax, falling back to Explicit VR LE."""
    try:
        ae.add_requested_context(sop_class, transfer_syntax)
    except ValueError as exc:
        log.warning(
            "C-STORE context rejected (sop_class=%s, transfer_syntax=%s, error_type=%s); "
            "retrying Explicit VR Little Endian",
            sop_class,
            transfer_syntax,
            type(exc).__name__,
        )
        ae.add_requested_context(sop_class, ExplicitVRLittleEndian)


def send_file(host: str, port: int, ae_title: str, file_path: str | Path,
              our_ae_title: str = "PAXS_SCU") -> dict:
    """Send one DICOM file via C-STORE. Returns {"ok": bool, ...}."""
    ds = pydicom.dcmread(str(file_path))
    sop_class = str(getattr(ds, "SOPClassUID", ""))
    transfer_syntax = str(getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", ExplicitVRLittleEndian))

    ae = _facade().AE(ae_title=our_ae_title)
    _configure_scu_ae(ae)
    _add_store_context(ae, sop_class, transfer_syntax)

    assoc = ae.associate(host, port, ae_title=ae_title, tls_args=_facade()._tls_args(host))
    if not assoc.is_established:
        reason = "association rejected"
        try:
            reason = getattr(assoc.acceptor, "reason", reason) or reason
        except Exception as exc:
            log.debug(
                "Could not read C-STORE association rejection detail (error_type=%s)",
                type(exc).__name__,
            )
        assoc.release()
        return {"ok": False, "reason": reason}

    try:
        status = assoc.send_c_store(ds, msg_id=1)
    finally:
        assoc.release()

    code = int(status.Status) if status is not None else None
    return {"ok": code == 0x0000, "status": code, "sop_instance_uid": str(ds.SOPInstanceUID)}


def send_echo(host: str, port: int, ae_title: str, our_ae_title: str = "PAXS_SCU",
              trust_cert: str | Path | None = None, *, timeout: float | None = None) -> dict:
    """C-ECHO (DICOM verification) against a remote AE. Returns {"ok": ...}."""
    from pynetdicom.sop_class import Verification

    ae = _facade().AE(ae_title=our_ae_title)
    _configure_scu_ae(ae, timeout=timeout)
    # Offer pynetdicom's standard uncompressed syntaxes, including the
    # mandatory default Implicit VR Little Endian used by older peers.
    ae.add_requested_context(Verification)
    assoc = ae.associate(host, port, ae_title=ae_title,
                         tls_args=(_facade()._tls_args(host, trust_cert) if trust_cert is not None
                                   else _facade()._tls_args(host)))
    if not assoc.is_established:
        diagnostic = association_failure_reason(assoc, ae_title, our_ae_title)
        assoc.release()
        return {"ok": False, "reason": "association rejected", "diagnostic": diagnostic}
    try:
        status = assoc.send_c_echo()
    finally:
        assoc.release()
    value = getattr(status, "Status", None)
    code = int(value) if value is not None else None
    result = {"ok": code == 0x0000, "status": code}
    if code is None:
        result["reason"] = "No C-ECHO response received; the peer timed out or aborted the association."
    elif code != 0x0000:
        result["reason"] = f"Remote C-ECHO returned status 0x{code:04X}"
    return result


def request_storage_commitment(
    host: str,
    port: int,
    ae_title: str,
    references: list[tuple[str, str]],
    our_ae_title: str = "PAXS_SCU",
    trust_cert: str | Path | None = None,
    wait_for_report: bool = False,
    report_timeout: float = 10.0,
) -> dict:
    """Request Storage Commitment and optionally wait for its N-EVENT-REPORT.

    The helper asks for both roles on the Push Model association, so peers that
    return the report on the same association work. If the peer acknowledges
    N-ACTION but sends the report later, the default result is a bounded
    ``PENDING`` response; callers may opt into a bounded wait.
    """
    if not references:
        raise ValueError("Storage Commitment requires at least one reference")
    normalized: list[tuple[str, str]] = []
    for reference in references:
        if len(reference) != 2:
            raise ValueError("Storage Commitment references need SOP class and instance UIDs")
        sop_class_uid, sop_instance_uid = (str(part).strip() for part in reference)
        if not sop_class_uid or not sop_instance_uid:
            raise ValueError("Storage Commitment references need non-empty UIDs")
        normalized.append((sop_class_uid, sop_instance_uid))

    transaction_uid = generate_uid()
    report_event = threading.Event()
    report_holder: dict[str, object] = {}

    def _on_event_report(event) -> tuple[int, None]:
        dataset = getattr(event, "dataset", None)
        if dataset is None:
            request = getattr(event, "request", None)
            raw = getattr(request, "EventInformation", None)
            context = getattr(event, "context", None)
            if raw is not None and context is not None:
                ts = context.transfer_syntax
                dataset = decode(raw, ts.is_implicit_VR, ts.is_little_endian, ts.is_deflated)
        if dataset is not None:
            report_holder.update(_storage_commitment_report(dataset))
        report_event.set()
        return 0x0000, None

    ae = _facade().AE(ae_title=our_ae_title)
    _configure_scu_ae(ae)
    transfer_syntaxes = [ImplicitVRLittleEndian, ExplicitVRLittleEndian]
    ae.add_requested_context(StorageCommitmentPushModel, transfer_syntaxes)
    ae.add_supported_context(
        StorageCommitmentPushModel, transfer_syntaxes,
        scu_role=True, scp_role=True,
    )
    association = ae.associate(
        host, port, ae_title=ae_title,
        ext_neg=[build_role(StorageCommitmentPushModel, scu_role=True, scp_role=True)],
        evt_handlers=[(evt.EVT_N_EVENT_REPORT, _on_event_report)],
        tls_args=_facade()._tls_args(host, trust_cert),
    )
    if not association.is_established:
        reason = association_failure_reason(association, ae_title, our_ae_title)
        association.release()
        return {
            "ok": False, "transaction_uid": transaction_uid,
            "action_status": None, "report_status": "NOT_SENT",
            "results": [], "reason": reason,
        }

    action = Dataset()
    action.TransactionUID = transaction_uid
    action.ReferencedSOPSequence = []
    for sop_class_uid, sop_instance_uid in normalized:
        item = Dataset()
        item.ReferencedSOPClassUID = sop_class_uid
        item.ReferencedSOPInstanceUID = sop_instance_uid
        action.ReferencedSOPSequence.append(item)

    try:
        status, _ = association.send_n_action(
            action, 1, StorageCommitmentPushModel,
            "1.2.840.10008.1.20.1.1",
        )
        action_status = int(status.Status) if status is not None else None
        if action_status != 0x0000:
            return {
                "ok": False, "transaction_uid": transaction_uid,
                "action_status": action_status, "report_status": "ACTION_FAILED",
                "results": [], "reason": f"status 0x{action_status:04x}" if action_status is not None else "no action response",
            }
        if wait_for_report:
            report_event.wait(max(0.0, min(float(report_timeout), 60.0)))
        if report_holder:
            report_ok = bool(report_holder.get("ok"))
            return {
                "ok": report_ok, "transaction_uid": transaction_uid,
                "action_status": action_status, "report_status": "RECEIVED",
                "results": report_holder["results"],
            }
        return {
            "ok": True, "transaction_uid": transaction_uid,
            "action_status": action_status,
            "report_status": "PENDING" if wait_for_report else "NOT_WAITED",
            "results": [],
        }
    except Exception as exc:
        log.error("Storage Commitment N-ACTION failed (error_type=%s)", type(exc).__name__)
        return {
            "ok": False, "transaction_uid": transaction_uid,
            "action_status": None, "report_status": "ACTION_FAILED",
            "results": [], "reason": f"Storage Commitment failed ({type(exc).__name__})",
        }
    finally:
        association.release()


def _storage_commitment_report(dataset: Dataset) -> dict[str, object]:
    """Parse a commitment event report into compact UID-only results."""
    results: list[dict[str, object]] = []
    for item in getattr(dataset, "ReferencedSOPSequence", []) or []:
        result: dict[str, object] = {
            "sop_class_uid": str(getattr(item, "ReferencedSOPClassUID", "")),
            "sop_instance_uid": str(getattr(item, "ReferencedSOPInstanceUID", "")),
        }
        if hasattr(item, "FailureReason"):
            result["failure_reason"] = int(item.FailureReason)
        results.append(result)
    return {"ok": not any("failure_reason" in item for item in results), "results": results}


def _send_mpps_n_op(host: str, port: int, ae_title: str, ds: Dataset,
                    sop_uid: str, operation: str,
                    our_ae_title: str = "PAXS_SCU") -> dict:
    """Shared N-CREATE/N-SET SCU for the Modality Performed Procedure Step.

    `operation` is "create" or "set". Returns ``{"ok", "status", "reason"?}``
    — status 0x0000 = success, 0x0112 = unknown SOP instance (N-SET before
    N-CREATE), 0xA700/0xA701 = attribute list error (missing / inconsistent
    value). Association failure or a mid-transfer abort yields
    ``{"ok": False, "reason": ...}``.
    """
    from pynetdicom.sop_class import ModalityPerformedProcedureStep

    ae = _facade().AE(ae_title=our_ae_title)
    _configure_scu_ae(ae)
    ae.add_requested_context(ModalityPerformedProcedureStep, ExplicitVRLittleEndian)
    assoc = ae.associate(host, port, ae_title=ae_title, tls_args=_facade()._tls_args(host))
    if not assoc.is_established:
        assoc.release()
        return {"ok": False, "reason": "association rejected"}
    result: dict = {"ok": False, "status": None}
    try:
        if operation == "create":
            status, _ = assoc.send_n_create(
                ds, ModalityPerformedProcedureStep, sop_uid)
        else:
            status, _ = assoc.send_n_set(
                ds, ModalityPerformedProcedureStep, sop_uid)
        code = int(status.Status) if status is not None and status.Status is not None else None
        result["status"] = code
        result["ok"] = code == 0x0000
        if not result["ok"] and code is not None:
            result["reason"] = f"status 0x{code:04x}"
    except Exception as exc:
        # Mid-transfer abort (connection reset, protocol error) must not leak
        # an exception — match the sibling SCU helpers' graceful contract.
        log.error("MPPS %s failed (error_type=%s)", operation, type(exc).__name__)
        result["ok"] = False
        result["reason"] = f"MPPS operation failed ({type(exc).__name__})"
    finally:
        assoc.release()
    return result


def send_mpps_create(host: str, port: int, ae_title: str, ds: Dataset,
                     sop_uid: str, our_ae_title: str = "PAXS_SCU") -> dict:
    """MPPS N-CREATE SCU — report exam start (status IN PROGRESS) to a remote
    archive, RIS or EMR. `ds` is the performed-step attribute list."""
    return _send_mpps_n_op(host, port, ae_title, ds, sop_uid, "create",
                           our_ae_title=our_ae_title)


def send_mpps_set(host: str, port: int, ae_title: str, ds: Dataset,
                  sop_uid: str, our_ae_title: str = "PAXS_SCU") -> dict:
    """MPPS N-SET SCU — report exam end (COMPLETED / DISCONTINUED) to a remote
    archive, closing the performed step it previously created."""
    return _send_mpps_n_op(host, port, ae_title, ds, sop_uid, "set",
                           our_ae_title=our_ae_title)


def send_directory(host: str, port: int, ae_title: str, directory: str | Path) -> dict:
    """Send every DICOM file under a directory. Returns summary."""
    directory = Path(directory)
    results = {"ok": 0, "failed": 0, "errors": []}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        try:
            result = _facade().send_file(host, port, ae_title, path)
            if result.get("ok"):
                results["ok"] += 1
            else:
                results["failed"] += 1
                results["errors"].append(f"{path.name}: {result.get('reason')}")
        except Exception as exc:
            # Filesystem names and decoder exception text may contain PHI.
            log.error("Directory C-STORE instance failed (error_type=%s)",
                      type(exc).__name__)
            results["failed"] += 1
            results["errors"].append(f"Instance transfer failed ({type(exc).__name__})")
    return results
