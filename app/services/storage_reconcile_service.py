"""Index/object reconciliation: find instance rows whose stored object is gone (H-07).

Ingest compensates its own publish failures (``indexing_service.
compensate_unpublished_instance``), but an object can still disappear after
the fact: a disk replaced, a backend path remounted, an object deleted out of
band. Its row keeps answering QIDO while every WADO request 404s, and nothing
told an operator.

This sweep walks ``instances`` in bounded keyset batches (N-5), checks each
object with ``storage_service.exists`` and records every newly missing one as a
single ``OBJECT_MISSING`` audit row (SOP/study UIDs only, N-2).

It deliberately **never deletes metadata**. A backend that is temporarily
unreachable (an unplugged drive, an S3 outage) is indistinguishable from a
missing object, and deleting rows on that signal would destroy the index of a
study whose pixels may come back. Rejected alternative: an ``availability``
column flipped by the sweep. It needs a migration, and every read path would
have to learn to filter on it; the audit trail gives the operator the list
without changing what clinical reads return.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.db.models import AuditLog, Instance, Series, Study
from app.db.session import SyncSessionLocal
from app.services import storage_service

log = logging.getLogger("paxs.storage.reconcile")

#: Audit action recorded once per instance whose object is missing.
MISSING_ACTION = "OBJECT_MISSING"

_cursor_lock = threading.Lock()
_cursor = 0  # last instances.id checked; the sweep wraps to 0 after a full pass


def reset_cursor() -> None:
    """Restart the next sweep from the first instance (tests, operator rerun)."""
    global _cursor
    with _cursor_lock:
        _cursor = 0


def _batch(session, after_id: int, limit: int) -> list[tuple[int, str, str, str | None, str]]:
    """One keyset page: (id, sop_uid, file_path, backend, study_uid)."""
    rows = session.execute(
        select(Instance.id, Instance.sop_instance_uid, Instance.file_path,
               Instance.storage_backend, Study.study_instance_uid)
        .join(Series, Series.id == Instance.series_id)
        .join(Study, Study.id == Series.study_id)
        .where(Instance.id > after_id)
        .order_by(Instance.id.asc())
        .limit(limit)
    ).all()
    return [tuple(row) for row in rows]


def _check(rows) -> tuple[list[tuple[str, str]], int]:
    """(missing (sop, study) pairs, backend error count) for one page."""
    missing: list[tuple[str, str]] = []
    errors = 0
    for _id, sop_uid, file_path, backend, study_uid in rows:
        try:
            if not storage_service.exists(file_path, backend_name=backend):
                missing.append((sop_uid, study_uid))
        except Exception as exc:  # noqa: BLE001 - an unreachable backend is "unknown", never "missing"
            errors += 1
            log.debug("storage_reconcile_check_failed error_type=%s", type(exc).__name__)
    return missing, errors


def _flag_new(session, missing: list[tuple[str, str]]) -> int:
    """Write one OBJECT_MISSING audit row per instance not flagged before."""
    if not missing:
        return 0
    already = set(session.execute(
        select(AuditLog.sop_instance_uid).where(
            AuditLog.action == MISSING_ACTION,
            AuditLog.sop_instance_uid.in_([sop for sop, _study in missing]),
        )
    ).scalars())
    new = [(sop, study) for sop, study in missing if sop not in already]
    for sop, study in new:
        session.add(AuditLog(sop_instance_uid=sop, study_instance_uid=study,
                             action=MISSING_ACTION, performed_by="storage-reconcile"))
    session.commit()
    return len(new)


def sweep_missing_objects(batch_size: int | None = None) -> dict[str, Any]:
    """Check one bounded batch of instances; flag those whose object is missing.

    ``batch_size`` defaults to ``STORAGE_RECONCILE_BATCH_SIZE``; ``0`` disables
    the sweep (N-5). Successive calls resume where the previous one stopped
    and wrap around after the last instance. Returns PHI-free counters.
    """
    global _cursor
    limit = settings.STORAGE_RECONCILE_BATCH_SIZE if batch_size is None else batch_size
    report = {"checked": 0, "missing": 0, "newly_flagged": 0, "errors": 0,
              "last_id": 0, "wrapped": False}
    if limit <= 0:
        return report
    with _cursor_lock:
        after_id = _cursor
    with SyncSessionLocal() as session:
        rows = _batch(session, after_id, limit)
        missing, errors = _check(rows)
        flagged = _flag_new(session, missing)
    last_id = rows[-1][0] if rows else 0
    wrapped = len(rows) < limit
    with _cursor_lock:
        _cursor = 0 if wrapped else last_id
    report.update(checked=len(rows), missing=len(missing), newly_flagged=flagged,
                  errors=errors, last_id=last_id, wrapped=wrapped)
    if flagged or errors:
        log.warning(
            "storage_reconcile missing=%d newly_flagged=%d backend_errors=%d checked=%d "
            "(see audit action %s)", len(missing), flagged, errors, len(rows), MISSING_ACTION,
        )
    return report
