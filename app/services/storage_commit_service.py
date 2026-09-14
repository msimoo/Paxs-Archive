"""Storage Commitment Push Model service (SOP Class 1.2.840.10008.1.20.1).

A modality asks "will you guarantee these instances are archived?" with an
N-ACTION and PAXS answers *asynchronously* with an N-EVENT-REPORT. The DIMSE
entry point is `handle_n_action` in `app/dicom/receiver.py`; this module owns
the `commitment_statuses` rows and the verification logic.

Verification is the source of truth: an instance passes only if its row
exists *and* its object is readable on the active storage backend (local or
S3 after Phase B1) — a DB row without a file must fail exactly the way the
protocol exists to surface.

Delivery timing (see `app/dicom/commit_sender.py`):
  * all references verified at N-ACTION time -> report queued immediately
  * otherwise the request stays PENDING and each C-STORE re-verifies it; when
    the last reference lands, the report fires automatically (the C-STORE
    hook below)
  * stragglers that never arrive are finalized by the sender's sweep with the
    truth as of now (successes + FailureReason per missing instance)
"""

from __future__ import annotations

import json
import logging

from pydicom.dataset import Dataset
from sqlalchemy import select

from app.db.models import Instance, StorageCommitRequest
from app.db.session import SyncSessionLocal
from app.services import storage_service

log = logging.getLogger("paxs.commit")

# DICOM FailureReason codes (PS3.4 Storage Commitment).
FAILURE_PROCESSING = 0x0110     # Processing failure (row exists, file gone)
FAILURE_NO_OBJECT = 0x0112      # No such object instance
FAILURE_CLASS_UNSUPPORTED = 0x0122  # SOP class not supported


def _refs_from_ds(ds: Dataset) -> list[dict]:
    """Extract (sop_class_uid, sop_instance_uid) pairs from the request's
    ReferencedSOPSequence (0008,1199)."""
    refs: list[dict] = []
    try:
        for item in getattr(ds, "ReferencedSOPSequence", []) or []:
            sop = str(getattr(item, "ReferencedSOPInstanceUID", "")).strip()
            if not sop:
                continue
            cls = str(getattr(item, "ReferencedSOPClassUID", "")).strip()
            refs.append({"sop_class_uid": cls or None, "sop_instance_uid": sop})
    except Exception:
        log.debug("commit: unusable ReferencedSOPSequence", exc_info=True)
    return refs


def _verify_one(session, ref: dict) -> dict:
    """Verify a single reference against DB + active storage backend."""
    sop_uid = ref["sop_instance_uid"]
    inst = session.execute(
        select(Instance).where(Instance.sop_instance_uid == sop_uid)
    ).scalars().first()
    if inst is None:
        return {
            "sop_instance_uid": sop_uid,
            "sop_class_uid": ref.get("sop_class_uid"),
            "ok": False,
            "failure_reason": FAILURE_NO_OBJECT,
        }
    if not storage_service.exists(inst.file_path, backend_name=getattr(inst, "storage_backend", None)):
        return {
            "sop_instance_uid": sop_uid,
            "sop_class_uid": ref.get("sop_class_uid"),
            "ok": False,
            "failure_reason": FAILURE_PROCESSING,
        }
    return {
        "sop_instance_uid": sop_uid,
        "sop_class_uid": ref.get("sop_class_uid"),
        "ok": True,
    }


def verify_instances(session, refs: list[dict]) -> list[dict]:
    return [_verify_one(session, ref) for ref in refs]


def _all_ok(results: list[dict]) -> bool:
    return bool(results) and all(r.get("ok") for r in results)


def _load_payload(req: StorageCommitRequest) -> dict:
    try:
        payload = json.loads(req.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return payload


def handle_action(session, tx_uid: str, ae_title: str | None, ds: Dataset) -> list[dict]:
    """Handle an N-ACTION (request storage commitment).

    Verifies every referenced instance, upserts the request row and, when
    everything is already verifiable, queues the asynchronous N-EVENT-REPORT
    delivery. Returns the per-instance results.
    """
    refs = _refs_from_ds(ds)
    if not refs:
        raise ValueError("N-ACTION carried no referenced instances")
    results = verify_instances(session, refs)

    payload = {
        "ae_title": ae_title or "",
        "requested": refs,
        "results": results,
    }
    req = session.execute(
        select(StorageCommitRequest).where(
            StorageCommitRequest.transaction_uid == tx_uid
        )
    ).scalars().first()
    if req is None:
        req = StorageCommitRequest(
            transaction_uid=tx_uid,
            ae_title=ae_title or "",
            status="PENDING",
            payload_json=json.dumps(payload),
        )
        session.add(req)
    else:
        req.ae_title = ae_title or req.ae_title
        req.payload_json = json.dumps(payload)
        if req.status != "PENDING":
            req.status = "PENDING"
    session.commit()

    if _all_ok(results):
        _queue(req.transaction_uid)
    return results


def pending_requests_for_sop(session, sop_uid: str) -> list[StorageCommitRequest]:
    """PENDING requests that reference `sop_uid` (for the C-STORE hook)."""
    out: list[StorageCommitRequest] = []
    for req in session.execute(
        select(StorageCommitRequest).where(StorageCommitRequest.status == "PENDING")
    ).scalars().all():
        payload = _load_payload(req)
        if any(r.get("sop_instance_uid") == sop_uid for r in payload.get("requested", [])):
            out.append(req)
    return out


def on_instance_stored(sop_uid: str) -> None:
    """Called by the C-STORE handler after a successful store.

    The user-facing contract: if a modality asked for commitment *before* its
    images were fully stored, the N-EVENT-REPORT fires automatically once the
    last referenced instance lands on disk (no manual re-request). Requests
    whose every reference is now verified are handed to the sender queue.
    """
    if not sop_uid:
        return
    try:
        with SyncSessionLocal() as session:
            for req in pending_requests_for_sop(session, sop_uid):
                payload = _load_payload(req)
                results = verify_instances(session, payload.get("requested", []))
                payload["results"] = results
                req.payload_json = json.dumps(payload)
                session.commit()
                if _all_ok(results):
                    _queue(req.transaction_uid)
    except Exception:
        log.exception("commit hook failed for instance %s", sop_uid)


def _queue(tx_uid: str) -> None:
    """Start the sender (if needed) and queue the report for `tx_uid`."""
    from app.dicom.commit_sender import queue_report

    queue_report(tx_uid)
