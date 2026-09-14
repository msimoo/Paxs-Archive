"""Asynchronous N-EVENT-REPORT delivery for the Storage Commitment Push Model.

The N-ACTION handler acknowledges the request immediately (0x0000); the actual
verdict is an N-EVENT-REPORT (EventTypeID=1) delivered a moment later, usually
on the very association that carried the request (the modality stays
associated waiting for it). If that association is gone — the modality timed
out or dropped it — we fall back to a fresh association to its AE resolved
through the `dicom_nodes` registry.

A single worker thread serializes deliveries (pynetdicom associations are
locked per-AE) and also sweeps stale PENDING requests: a request whose
references never all landed is finalized with the truth as of now (successes
plus a FailureReason per missing instance) instead of hanging forever.
"""

from __future__ import annotations

import calendar
import json
import logging
import queue
import threading
import time as _time
from datetime import datetime, timezone

from pydicom.dataset import Dataset
from pynetdicom import AE as _AE
from pynetdicom.sop_class import StorageCommitmentPushModel
from pydicom.uid import ExplicitVRLittleEndian
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.db.models import StorageCommitRequest
from app.db.session import SyncSessionLocal
from app.services.node_service import get_node_sync

log = logging.getLogger("paxs.commit-sender")

# How old a PENDING request must be before the sweep finalizes it (seconds).
FINALIZE_AFTER_SECONDS = 2.0
# Sweep cadence of the worker thread.
SWEEP_INTERVAL_SECONDS = 1.0
# Bound the time the worker waits on a single DIMSE round trip. A modality
# that silently disappears must not stall the whole delivery queue (pynetdicom
# defaults to 30s).
_DIMSE_TIMEOUT_SECONDS = 5.0

# Transaction UID -> association that carried the N-ACTION request.
_ASSOCS: dict[str, object] = {}
_ASSOCS_LOCK = threading.Lock()
# Transaction UID -> monotonic arrival time. The staleness sweep must not
# trust created_at from the DB: MySQL's NOW() is server-local (not UTC), so a
# wall-clock comparison would finalize requests early or never. Our own
# monotonic clock is immune to both clock skew and DB timezone differences.
_CREATED_AT: dict[str, float] = {}
_CREATED_AT_LOCK = threading.Lock()
_QUEUE: queue.Queue = queue.Queue()
_WORKER: threading.Thread | None = None
_START_LOCK = threading.Lock()


def register_association(tx_uid: str, assoc) -> None:
    """Remember the association for `tx_uid` so the report can ride it."""
    try:
        assoc.acse_timeout = _DIMSE_TIMEOUT_SECONDS
        assoc.dimse_timeout = _DIMSE_TIMEOUT_SECONDS
    except Exception:
        pass
    with _ASSOCS_LOCK:
        _ASSOCS[tx_uid] = assoc
    with _CREATED_AT_LOCK:
        _CREATED_AT.setdefault(tx_uid, _time.monotonic())
    _ensure_worker()


def queue_report(tx_uid: str) -> None:
    with _CREATED_AT_LOCK:
        _CREATED_AT.setdefault(tx_uid, _time.monotonic())
    _ensure_worker()
    _QUEUE.put(tx_uid)


def _ensure_worker() -> None:
    global _WORKER
    with _START_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_run, name="paxs-commit-sender", daemon=True)
            _WORKER.start()


def _run() -> None:
    while True:
        try:
            tx_uid = _QUEUE.get(timeout=SWEEP_INTERVAL_SECONDS)
        except queue.Empty:
            try:
                _finalize_stale()
            except OperationalError:
                # Table dropped / engine closed (shutdown, test teardown) —
                # the daemon worker simply has nothing left to sweep.
                log.debug("commit sweep skipped (DB unavailable)")
            except Exception:
                log.exception("commit sweep failed")
            continue
        try:
            _deliver(tx_uid)
        except Exception:
            log.exception("commit delivery failed for %s", tx_uid)
        finally:
            _QUEUE.task_done()


# --------------------------------------------------------------------------- #
# Report building
# --------------------------------------------------------------------------- #
def _build_report(payload: dict) -> Dataset:
    ds = Dataset()
    ds.EventTypeID = 1
    ds.TransactionUID = str(payload.get("transaction_uid", ""))
    seq = []
    for r in payload.get("results", []):
        item = Dataset()
        item.ReferencedSOPClassUID = str(r.get("sop_class_uid") or "")
        item.ReferencedSOPInstanceUID = r["sop_instance_uid"]
        if not r.get("ok"):
            item.FailureReason = int(r.get("failure_reason", 0x0110))
        seq.append(item)
    ds.ReferencedSOPSequence = seq
    return ds


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def _deliver(tx_uid: str) -> None:
    """Send the N-EVENT-REPORT for `tx_uid`, then finalize the request row."""
    with SyncSessionLocal() as session:
        req = session.execute(
            select(StorageCommitRequest).where(
                StorageCommitRequest.transaction_uid == tx_uid
            )
        ).scalars().first()
        if req is None:
            return
        if req.status == "COMPLETED":
            return  # already acknowledged — do not double-deliver

        payload = json.loads(req.payload_json or "{}")
        if not isinstance(payload, dict):
            payload = {}
        payload["transaction_uid"] = tx_uid

        with _ASSOCS_LOCK:
            assoc = _ASSOCS.get(tx_uid)

        ok = False
        if assoc is not None and getattr(assoc, "is_alive", False):
            try:
                rsp, _ = assoc.send_n_event_report(
                    _build_report(payload), 1, StorageCommitmentPushModel, tx_uid
                )
                ok = rsp is not None and int(getattr(rsp, "Status", 0xFFFF)) == 0x0000
            except Exception:
                log.warning(
                    "commit %s: report on original association failed", tx_uid, exc_info=True
                )
        if not ok:
            ok = _deliver_via_node(tx_uid, payload, req.ae_title)

        req.status = "COMPLETED" if ok else "FAILED"
        # naive-UTC to match created_at (server_default func.now())
        req.sent_at = (
            datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) if ok else None
        )
        session.commit()
        log.info("Storage Commitment %s -> %s (%s)", tx_uid, req.status,
                 "delivered" if ok else "undeliverable")
    # Drop the per-transaction bookkeeping now that the request is final.
    # Keeping these would leak: _ASSOCS holds strong references to closed
    # pynetdicom associations and both dicts grow unboundedly on a long-running
    # archive (Storage Commitment is exactly the long-running case).
    with _ASSOCS_LOCK:
        _ASSOCS.pop(tx_uid, None)
    with _CREATED_AT_LOCK:
        _CREATED_AT.pop(tx_uid, None)


def _deliver_via_node(tx_uid: str, payload: dict, ae_title: str | None) -> bool:
    """Fallback: deliver on a fresh association to the modality's AE, resolved
    through the dicom_nodes registry (works even if the original association
    was closed before the report was sent)."""
    if not ae_title:
        return False
    node = get_node_sync(ae_title)
    if node is None:
        log.warning("commit %s: no dicom node for AE '%s' to retry delivery",
                    tx_uid, ae_title)
        return False
    try:
        ae = _AE(ae_title=settings.DICOM_AE_TITLE)
        ae.add_requested_context(StorageCommitmentPushModel, ExplicitVRLittleEndian)
        ae.acse_timeout = _DIMSE_TIMEOUT_SECONDS
        ae.dimse_timeout = _DIMSE_TIMEOUT_SECONDS
        assoc = ae.associate(node.host, node.port, ae_title=ae_title)
        if not assoc.is_established:
            log.warning("commit %s: could not re-associate to %s:%s",
                        tx_uid, node.host, node.port)
            return False
        try:
            rsp, _ = assoc.send_n_event_report(
                _build_report(payload), 1, StorageCommitmentPushModel, tx_uid
            )
            return rsp is not None and int(getattr(rsp, "Status", 0xFFFF)) == 0x0000
        finally:
            assoc.release()
    except Exception:
        log.exception("commit %s: fresh-association delivery failed", tx_uid)
        return False


# --------------------------------------------------------------------------- #
# Stale request sweep
# --------------------------------------------------------------------------- #
def _finalize_stale() -> None:
    """Deliver PENDING requests that have been waiting too long.

    References that never landed (unknown SOP UIDs, files that went missing)
    must still be answered — the report carries the truth as of now, with a
    FailureReason per instance, so the modality can react.
    """
    with _CREATED_AT_LOCK:
        created_snapshot = dict(_CREATED_AT)
    with SyncSessionLocal() as session:
        stale = session.execute(
            select(StorageCommitRequest).where(StorageCommitRequest.status == "PENDING")
        ).scalars().all()
        for req in stale:
            mono = created_snapshot.get(req.transaction_uid)
            if mono is not None:
                age = _time.monotonic() - mono
            elif req.created_at is not None:
                # Restart fallback: created_at is naive UTC on SQLite
                # (func.now() = UTC) but server-local on MySQL — best effort.
                age = _time.time() - calendar.timegm(req.created_at.utctimetuple())
            else:
                continue
            if age >= FINALIZE_AFTER_SECONDS:
                _deliver(req.transaction_uid)
