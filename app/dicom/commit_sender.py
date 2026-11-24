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
# An association reference is only useful for a short-lived DIMSE exchange;
# after five minutes it is an orphan and the registered-node fallback applies.
ASSOCIATION_MAX_AGE_SECONDS = 300.0
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
_QUEUE: queue.Queue[str] = queue.Queue(
    maxsize=max(1, settings.STORAGE_COMMIT_MAX_PENDING)
)
_WORKER: threading.Thread | None = None
_START_LOCK = threading.Lock()


def register_association(tx_uid: str, assoc: object) -> bool:
    """Track an N-ACTION association when the configured in-memory budget allows."""
    try:
        assoc.acse_timeout = _DIMSE_TIMEOUT_SECONDS
        assoc.dimse_timeout = _DIMSE_TIMEOUT_SECONDS
    except (AttributeError, ValueError, TypeError) as exc:  # timeout tuning is best-effort
        # Some association objects are read-only proxies; the defaults then
        # stand. Debug keeps a genuinely wrong object type visible.
        log.debug("commit_assoc_timeout_unsettable error_type=%s", type(exc).__name__)
    with _ASSOCS_LOCK:
        with _CREATED_AT_LOCK:
            if tx_uid not in _CREATED_AT and len(_CREATED_AT) >= settings.STORAGE_COMMIT_MAX_PENDING:
                log.warning("commit tracking capacity reached; rejecting N-ACTION")
                return False
            _ASSOCS[tx_uid] = assoc
            _CREATED_AT.setdefault(tx_uid, _time.monotonic())
    _ensure_worker()
    return True


def queue_report(tx_uid: str) -> None:
    """Schedule a report without blocking; the DB sweep recovers saturation."""
    with _CREATED_AT_LOCK:
        if tx_uid not in _CREATED_AT:
            if len(_CREATED_AT) >= settings.STORAGE_COMMIT_MAX_PENDING:
                log.warning("commit tracking capacity reached; report deferred to DB sweep")
                _ensure_worker()
                return
            _CREATED_AT[tx_uid] = _time.monotonic()
    _ensure_worker()
    try:
        _QUEUE.put_nowait(tx_uid)
    except queue.Full:
        log.warning("commit dispatch queue full; report deferred to DB sweep")


def _cleanup_bookkeeping(tx_uid: str) -> None:
    """Drop both in-memory association records for a terminal/aborted request."""
    with _ASSOCS_LOCK:
        with _CREATED_AT_LOCK:
            _ASSOCS.pop(tx_uid, None)
            _CREATED_AT.pop(tx_uid, None)


def _sweep_orphan_associations(now: float | None = None) -> int:
    """Remove association references older than the five-minute safety window."""
    current = _time.monotonic() if now is None else now
    removed = 0
    with _ASSOCS_LOCK:
        with _CREATED_AT_LOCK:
            stale_ids = {
                tx_uid
                for tx_uid, created_at in _CREATED_AT.items()
                if current - created_at >= ASSOCIATION_MAX_AGE_SECONDS
            }
            # An association without its timestamp cannot be aged safely, but it
            # cannot be used to match a report either; remove only when the ID is
            # not represented by a timestamp after a later registration.
            for tx_uid in stale_ids:
                _ASSOCS.pop(tx_uid, None)
                _CREATED_AT.pop(tx_uid, None)
                removed += 1
    if removed:
        log.warning("swept %d stale storage-commit association record(s)", removed)
    return removed


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
                _sweep_orphan_associations()
                _finalize_stale()
            except OperationalError:
                # Table dropped / engine closed (shutdown, test teardown) —
                # the daemon worker simply has nothing left to sweep.
                log.debug("commit sweep skipped (DB unavailable)")
            except Exception as exc:
                log.error("commit sweep failed error_type=%s", type(exc).__name__)
            continue
        try:
            _deliver(tx_uid)
        except Exception as exc:
            log.error("commit delivery failed for %s error_type=%s",
                      tx_uid, type(exc).__name__)
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
    """Send the report and always release association bookkeeping."""
    try:
        _deliver_impl(tx_uid)
    finally:
        _cleanup_bookkeeping(tx_uid)


def _deliver_impl(tx_uid: str) -> None:
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
            except Exception as exc:
                log.warning(
                    "commit %s: report on original association failed error_type=%s",
                    tx_uid,
                    type(exc).__name__,
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
        # B-16: the report carries SOP UIDs and patient-study references, so
        # it must ride the same verified TLS channel as the images. Without
        # tls_args this association was plaintext even when DICOM_TLS_ENABLED
        # was on — and it silently skipped peer verification.
        from app.dicom.sender import _tls_args

        assoc = ae.associate(
            node.host, node.port, ae_title=ae_title,
            tls_args=_tls_args(node.host, getattr(node, "trust_cert", None)),
        )
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
    except Exception as exc:
        log.error("commit %s: fresh-association delivery failed error_type=%s",
                  tx_uid, type(exc).__name__)
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
    max_pending = settings.STORAGE_COMMIT_MAX_PENDING
    if max_pending == 0:
        return
    with _CREATED_AT_LOCK:
        created_snapshot = dict(_CREATED_AT)
    with SyncSessionLocal() as session:
        stale = session.execute(
            select(StorageCommitRequest)
            .where(StorageCommitRequest.status == "PENDING")
            .order_by(StorageCommitRequest.id.asc())
            .limit(max_pending)
        ).scalars().all()
        candidates = [(req.transaction_uid, req.created_at) for req in stale]
    for tx_uid, created_at in candidates:
        mono = created_snapshot.get(tx_uid)
        if mono is not None:
            age = _time.monotonic() - mono
        elif created_at is not None:
            # Restart fallback: naive-UTC on SQLite, server-local on MySQL.
            age = _time.time() - calendar.timegm(created_at.utctimetuple())
        else:
            continue
        if age >= FINALIZE_AFTER_SECONDS:
            _deliver(tx_uid)
