"""Auto-routing Rules Engine Service.

Evaluates user-configured DICOM tag rules when new studies arrive, and
dispatches matching studies to remote DICOM Node destinations via C-STORE.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.concurrency import BoundedBackgroundPool
from app.core.config import settings
from app.db.models import DicomNode, Instance, RoutingQueueItem, RoutingRule, Series, Study
from app.db.session import AsyncSessionLocal, SyncSessionLocal
from app.dicom.sender import send_file
from app.services import audit_service, storage_service
from app.services.compression_service import compression_window_open

log = logging.getLogger("paxs.routing")

# Two accepted slots cover the brief executor-accounting tail after one sweep
# sets its idle event and before BoundedBackgroundPool decrements ``inflight``.
_MANUAL_SWEEP_POOL = BoundedBackgroundPool(max_workers=1, queue_max=2)
_MANUAL_SWEEP_LOCK = threading.Lock()
_MANUAL_SWEEP_ACTIVE = False
_MANUAL_SWEEP_IDLE = threading.Event()
_MANUAL_SWEEP_IDLE.set()


def _normalize_rule_value(value: Any) -> str:
    """Normalize DICOM routing predicates without altering stored values."""
    return str(value or "").strip().upper()


def _rule_mismatch(
    rule: RoutingRule,
    modalities: set[str],
    body_parts: set[str],
    source_aet: str,
    study_desc: Any,
) -> str | None:
    """Why ``rule`` does not match this study, or ``None`` when it does (F-01).

    Every predicate goes through :func:`_normalize_rule_value`, so a rule stored
    as ``" ct "`` matches a study whose Modality is ``"CT"``. Without that, a
    stray space or a lower-case value made the rule silently never match — and
    "the rule never matches" is indistinguishable from "auto-routing is broken",
    which is exactly how this defect presented.

    Three further robustness rules, each of which used to make a rule match or
    fail in a way nobody could observe:

    * a stored pattern is **stripped** before compiling (a trailing space in the
      admin form produced a regex that matched nothing);
    * a pattern rule **fails closed** on a study with no description. The old
      ``elif rule.study_description_pattern and study_desc`` guard skipped the
      check entirely when ``study_desc`` was empty, so a description-scoped rule
      matched every study without one;
    * a malformed pattern is reported **per rule**. Previously ``re.error``
      escaped :func:`evaluate_rules_for_study` and was swallowed by
      ``indexing_service``, so one typo in one rule disabled auto-routing for
      every study in the archive.
    """
    if not rule.destination_node or not rule.destination_node.enabled:
        return "destination_unavailable"
    if rule.modality and _normalize_rule_value(rule.modality) not in modalities:
        return "modality_mismatch"
    if rule.body_part_examined and _normalize_rule_value(rule.body_part_examined) not in body_parts:
        return "body_part_mismatch"
    if rule.source_aet and _normalize_rule_value(rule.source_aet) != source_aet:
        return "calling_ae_mismatch"

    pattern = (rule.study_description_pattern or "").strip()
    if not pattern:
        return None
    description = str(study_desc or "").strip()
    if not description:
        return "study_description_missing"
    try:
        if re.search(pattern, description, re.IGNORECASE):
            return None
    except re.error:
        log.warning("routing rule %s has an invalid study_description_pattern", rule.id)
        return "invalid_study_description_pattern"
    return "study_description_mismatch"


async def evaluate_rules_for_study(
    session: AsyncSession,
    study_instance_uid: str,
    meta: dict[str, Any] | None = None,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[RoutingRule]:
    """Evaluate active routing rules against a study and return matched rules."""
    stmt = (
        select(RoutingRule)
        .options(joinedload(RoutingRule.destination_node))
        .where(RoutingRule.enabled.is_(True))
        .order_by(RoutingRule.priority.asc(), RoutingRule.id.asc())
    )
    rules = (await session.execute(stmt)).scalars().all()
    if not rules:
        log.warning("no routing rules evaluated: no enabled rules")
        return []

    from app.services.query_service import apply_study_tenant_scope

    study_stmt = select(Study).options(joinedload(Study.series_list)).where(
        Study.study_instance_uid == study_instance_uid
    )
    study_stmt = apply_study_tenant_scope(study_stmt, tenant_id, include_all)
    studies = (await session.execute(study_stmt)).scalars().unique().all()
    if len(studies) != 1:
        log.warning("no routing rules evaluated: study lookup returned no result")
        return []
    study = studies[0]

    study_desc = (meta.get("study_description") if meta else None) or study.study_description or ""
    source_aet = _normalize_rule_value(meta.get("source_aet") if meta else "")
    modalities = {
        _normalize_rule_value(series.modality)
        for series in study.series_list
        if _normalize_rule_value(series.modality)
    }
    body_parts = {
        _normalize_rule_value(series.body_part_examined)
        for series in study.series_list
        if _normalize_rule_value(series.body_part_examined)
    }
    if meta:
        modality = _normalize_rule_value(meta.get("modality"))
        body_part = _normalize_rule_value(meta.get("body_part_examined"))
        if modality:
            modalities.add(modality)
        if body_part:
            body_parts.add(body_part)

    matched_rules: list[RoutingRule] = []
    unmatched_reasons: list[str] = []
    for rule in rules:
        reason = _rule_mismatch(rule, modalities, body_parts, source_aet, study_desc)

        if reason is not None:
            unmatched_reasons.append(f"{rule.id}:{reason}")
            continue

        matched_rules.append(rule)
        log.debug("routing rule matched rule_id=%s", rule.id)

    if not matched_rules:
        reasons = ",".join(unmatched_reasons) or "no_eligible_rules"
        log.warning("no routing rules matched (rules_checked=%d reasons=%s)", len(rules), reasons)
    return matched_rules


def dispatch_study_to_node_sync(
    study_instance_uid: str, node_id: int, tenant_id: int | None = None
) -> dict[str, Any]:
    """Synchronously send all instances of a study to a DICOM destination node."""
    with SyncSessionLocal() as session:
        node = session.execute(
            select(DicomNode).where(DicomNode.id == node_id)
        ).scalar_one_or_none()

        if not node or not node.enabled:
            return {"ok": False, "reason": "Destination node disabled or missing"}

        from app.services.query_service import apply_study_tenant_scope

        study_stmt = select(Study).where(Study.study_instance_uid == study_instance_uid)
        study_stmt = apply_study_tenant_scope(study_stmt, tenant_id)
        studies = session.execute(study_stmt).scalars().all()
        study = studies[0] if len(studies) == 1 else None

        if not study:
            return {"ok": False, "reason": "Study not found"}

        # Fetch all instances
        instances = (
            session.execute(
                select(Instance)
                .join(Series)
                .where(Series.study_id == study.id)
            )
        ).scalars().all()

        sent_count = 0
        failed_count = 0

        for inst in instances:
            bk = getattr(inst, "storage_backend", None)
            if not storage_service.exists(inst.file_path, backend_name=bk):
                failed_count += 1
                continue

            local = storage_service.local_path(inst.file_path, backend_name=bk)
            temp_path: str | None = None
            if local:
                file_target = local
            else:
                # Materialize a temp file for a remote (S3) object. It is removed
                # in `finally` below — an auto-routed S3 study must not leak one
                # `.dcm` per instance into the system temp dir (H-2).
                import tempfile
                data = storage_service.read_bytes(inst.file_path, backend_name=bk)
                with tempfile.NamedTemporaryFile(suffix=".dcm", delete=False) as tmp:
                    tmp.write(data)
                    file_target = tmp.name
                    temp_path = tmp.name

            try:
                res = send_file(
                    node.host, node.port, node.ae_title, file_target,
                    our_ae_title=settings.ROUTING_SCU_AE_TITLE,
                )
                if res.get("ok"):
                    sent_count += 1
                    audit_service.add_audit(
                        session,
                        sop_instance_uid=inst.sop_instance_uid,
                        study_instance_uid=study_instance_uid,
                        action="AUTO_ROUTED",
                        performed_by=f"auto-router:{node.ae_title}",
                    )
                else:
                    failed_count += 1
            finally:
                if temp_path is not None:
                    try:
                        os.unlink(temp_path)
                    except OSError as exc:
                        log.warning(
                            "routing_temp_file_cleanup_failed error_type=%s",
                            type(exc).__name__,
                        )

        session.commit()

        log.info(
            "Auto-routed study %s to %s (%s:%d): %d sent, %d failed",
            study_instance_uid,
            node.ae_title,
            node.host,
            node.port,
            sent_count,
            failed_count,
        )
        return {"ok": failed_count == 0, "sent": sent_count, "failed": failed_count}


async def _find_active_dispatch(
    session: AsyncSession,
    study_instance_uid: str,
    node_id: int,
    tenant_id: int | None,
) -> RoutingQueueItem | None:
    """Find an existing pending/sending item for the same study and node."""
    result = await session.execute(
        select(RoutingQueueItem).where(
            RoutingQueueItem.study_instance_uid == study_instance_uid,
            RoutingQueueItem.tenant_id == tenant_id,
            RoutingQueueItem.node_id == node_id,
            RoutingQueueItem.status.in_(("PENDING", "SENDING")),
        )
    )
    return result.scalar_one_or_none()


async def enqueue_dispatch(
    session: AsyncSession,
    study_instance_uid: str,
    rule: RoutingRule,
    tenant_id: int | None = None,
) -> RoutingQueueItem | None:
    """Enqueue a (study, destination node) dispatch job for the background worker.

    Idempotent: if the same study is already queued for this node (PENDING /
    SENDING), nothing new is created — repeated evaluations (e.g. a later
    instance of the same study arriving) must not duplicate the job.
    """
    if not settings.ROUTING_QUEUE_ENABLED:
        return None
    existing = await _find_active_dispatch(
        session, study_instance_uid, rule.destination_node_id, tenant_id
    )
    if existing is not None:
        return existing
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Honor the rule's dispatch delay at enqueue time: the item simply becomes
    # due later (worker sweeps skip items whose next_retry_at is in the future).
    next_retry_at = now + timedelta(seconds=max(rule.delay_seconds or 0, 0))
    item = RoutingQueueItem(
        study_instance_uid=study_instance_uid,
        tenant_id=tenant_id,
        rule_id=rule.id,
        node_id=rule.destination_node_id,
        status="PENDING",
        max_attempts=settings.ROUTING_MAX_ATTEMPTS,
        next_retry_at=next_retry_at,
    )
    session.add(item)
    await session.flush()
    log.info("Routing queue: enqueued study %s -> node %s (due in %ss)",
             study_instance_uid, rule.destination_node_id, rule.delay_seconds or 0)
    return item


async def evaluate_and_route_study(
    study_instance_uid: str,
    meta: dict[str, Any] | None = None,
    tenant_id: int | None = None,
    include_all: bool = False,
    session: AsyncSession | None = None,
) -> int:
    """Evaluate routing rules on incoming study and enqueue background dispatch.

    Returns the number of matched rules (queue jobs created). Dispatch itself
    happens in the worker (durable + retried) — see `process_queue_batch`.
    Ingest callers pass their active session so the queue job commits atomically
    with a newly created study; manual callers may let this function own a session.
    """
    if session is not None:
        return await _evaluate_and_enqueue_in_session(
            session, study_instance_uid, meta, tenant_id, include_all
        )
    async with AsyncSessionLocal() as session:
        async with session.begin():
            return await _evaluate_and_enqueue_in_session(
                session, study_instance_uid, meta, tenant_id, include_all
            )


async def _evaluate_and_enqueue_in_session(
    session: AsyncSession,
    study_instance_uid: str,
    meta: dict[str, Any] | None,
    tenant_id: int | None,
    include_all: bool,
) -> int:
    """Evaluate one scoped study and enqueue its matched rules in this transaction."""
    from app.services.query_service import apply_study_tenant_scope

    study_stmt = apply_study_tenant_scope(
        select(Study).where(Study.study_instance_uid == study_instance_uid),
        tenant_id,
        include_all,
    )
    studies = (await session.execute(study_stmt)).scalars().all()
    if len(studies) != 1:
        return 0
    resolved_tenant_id = studies[0].tenant_id
    matched_rules = await evaluate_rules_for_study(
        session, study_instance_uid, meta, tenant_id, include_all
    )
    for rule in matched_rules:
        # Delay by scheduling, never by sleeping inside the DB transaction.
        await enqueue_dispatch(session, study_instance_uid, rule, resolved_tenant_id)
    return len(matched_rules)


# --------------------------------------------------------------------------- #
# Send queue processing (runs in the background worker thread)
# --------------------------------------------------------------------------- #
def _due_conditions(now: datetime) -> tuple:
    """SQL predicates for "this PENDING item is due for dispatch now".

    Shared by the candidate SELECT and the claim UPDATE so the two can never
    disagree about what "due" means — a claim that is broader than its
    candidate list would hand back rows it did not own (C-1).
    """
    return (
        RoutingQueueItem.status == "PENDING",
        (RoutingQueueItem.next_retry_at.is_(None))
        | (RoutingQueueItem.next_retry_at <= now),
    )


def _claim_due_items(session, limit: int, now: datetime) -> list[dict[str, Any]]:
    """Atomically move due PENDING items to SENDING; return only what we own.

    C-1 / FP-1: the claim is a guarded ``UPDATE ... WHERE status='PENDING'``
    and the ``rowcount`` is the *only* thing that decides ownership — the same
    pattern as :meth:`app.services.worker.CompressionWorker._claim_jobs`.

    The previous read-then-write (``fresh = session.get(...); if
    fresh.status != "PENDING": continue; fresh.status = "SENDING"``) is a
    classic check-then-act race: two workers running the sweep at the same time
    could both read PENDING, both pass the guard, and both C-STORE the whole
    study to the destination node. The send is not idempotent, so the peer
    received the study twice and the queue reported success twice.

    The claim is issued per candidate id rather than as one bulk UPDATE so the
    return value can never include a row another worker claimed concurrently: a
    bulk ``UPDATE ... WHERE id IN (...)`` reports a single aggregate rowcount,
    which cannot distinguish "I claimed all three" from "I claimed two and
    somebody else claimed the third" — and returning the third would reintroduce
    exactly the double dispatch this function exists to prevent.
    """
    candidate_ids = session.execute(
        select(RoutingQueueItem.id)
        .where(*_due_conditions(now))
        .order_by(RoutingQueueItem.id)
        .limit(limit)
    ).scalars().all()
    if not candidate_ids:
        return []

    claimed_ids: list[int] = []
    for item_id in candidate_ids:
        result = session.execute(
            sa_update(RoutingQueueItem)
            .where(RoutingQueueItem.id == item_id, *_due_conditions(now))
            .values(
                status="SENDING",
                # Incremented in the same statement, so the attempt counter can
                # never be lost to a concurrent claim either.
                attempts=RoutingQueueItem.attempts + 1,
            )
        )
        if result.rowcount == 1:
            claimed_ids.append(item_id)
    # Commit the claim before dispatching: dispatch opens its own session and
    # SQLite serializes writers, so an uncommitted claim would deadlock the
    # send ("database is locked") on single-file DBs.
    session.commit()
    if not claimed_ids:
        return []

    rows = session.execute(
        select(
            RoutingQueueItem.id,
            RoutingQueueItem.study_instance_uid,
            RoutingQueueItem.node_id,
            RoutingQueueItem.tenant_id,
        )
        .where(RoutingQueueItem.id.in_(claimed_ids))
        .order_by(RoutingQueueItem.id)
    ).all()
    return [
        {"id": item_id, "study_instance_uid": study_uid, "node_id": node_id,
         "tenant_id": tenant_id}
        for item_id, study_uid, node_id, tenant_id in rows
    ]


def process_queue_batch(limit: int = 10) -> dict[str, int]:
    """Claim and dispatch due PENDING queue items; retry failures with backoff.

    Runs in the worker thread (sync engine). For each claimed item:
      * dispatch the study to its node via C-STORE
      * success -> COMPLETED (with sent/failed instance counts)
      * failure -> attempts += 1; if attempts >= max_attempts -> FAILED,
        else next_retry_at = now + backoff (grows with attempts).
    """
    if not settings.ROUTING_QUEUE_ENABLED or not compression_window_open(
        settings.ROUTING_TIMING, settings.ROUTING_START_TIME, settings.ROUTING_END_TIME,
        datetime.now().strftime("%H:%M"),
    ):
        return {"claimed": 0, "completed": 0, "failed": 0, "requeued": 0}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    counts = {"claimed": 0, "completed": 0, "failed": 0, "requeued": 0}

    with SyncSessionLocal() as session:
        claimed = _claim_due_items(session, limit, now)

    # Dispatch outside the claim transaction: dispatch_study_to_node_sync opens
    # its own session (SQLite serializes writers), so committing the claim first
    # avoids a "database is locked" deadlock on single-file DBs.
    for item in claimed:
        counts["claimed"] += 1

        try:
            res = dispatch_study_to_node_sync(
                item["study_instance_uid"], item["node_id"], item["tenant_id"]
            )
        except Exception as exc:  # noqa: BLE001 - boundary guard: a send error must never kill the sweep
            res = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

        with SyncSessionLocal() as session:
            fresh = session.get(RoutingQueueItem, item["id"])
            if fresh is None:
                continue
            # Only the worker holding the SENDING claim may finalize it. If the
            # row is no longer SENDING, a startup recovery pass re-queued it and
            # another sweep owns it now — finalizing here would race that claim.
            if fresh.status != "SENDING":
                log.debug(
                    "Routing queue: item %s was re-queued during dispatch (status=%s); "
                    "leaving it to the current owner", item["id"], fresh.status,
                )
                continue
            if res.get("ok"):
                fresh.status = "COMPLETED"
                fresh.sent_count = int(res.get("sent", 0))
                fresh.failed_count = int(res.get("failed", 0))
                fresh.last_error = None
                fresh.next_retry_at = None
                counts["completed"] += 1
                log.info("Routing queue: study %s sent to node %s (%s sent, %s failed)",
                         item["study_instance_uid"], item["node_id"],
                         fresh.sent_count, fresh.failed_count)
            else:
                fresh.last_error = str(res.get("reason") or "unknown error")[:1000]
                if fresh.attempts >= fresh.max_attempts:
                    fresh.status = "FAILED"
                    fresh.next_retry_at = None
                    counts["failed"] += 1
                    log.warning(
                        "routing_queue_item_failed study_uid=%s attempts=%d",
                        item["study_instance_uid"],
                        fresh.attempts,
                    )
                else:
                    backoff = settings.ROUTING_RETRY_BACKOFF_SECONDS * (fresh.attempts ** 2)
                    fresh.status = "PENDING"
                    fresh.next_retry_at = now + timedelta(seconds=backoff)
                    counts["requeued"] += 1
                    log.info(
                        "routing_queue_retry_scheduled study_uid=%s attempt=%d delay_seconds=%d",
                        item["study_instance_uid"],
                        fresh.attempts,
                        backoff,
                    )
            session.commit()
    return counts


def _run_manual_queue_sweep() -> None:
    """Run one requested sweep and release the single-flight admission slot."""
    global _MANUAL_SWEEP_ACTIVE
    try:
        process_queue_batch()
    except Exception as exc:  # noqa: BLE001 - boundary guard: background worker must redact errors
        log.error("manual_routing_sweep_failed error_type=%s", type(exc).__name__)
    finally:
        with _MANUAL_SWEEP_LOCK:
            _MANUAL_SWEEP_ACTIVE = False
            _MANUAL_SWEEP_IDLE.set()


def start_manual_queue_sweep() -> bool:
    """Schedule one bounded sweep; repeated calls coalesce with the active run.

    A dedicated one-worker pool reserves capacity for explicit ADMIN requests.
    The active flag limits accepted work to one running or queued sweep, so the
    executor's internal work queue cannot grow with request volume.
    """
    global _MANUAL_SWEEP_ACTIVE
    with _MANUAL_SWEEP_LOCK:
        if _MANUAL_SWEEP_ACTIVE:
            return True
        _MANUAL_SWEEP_ACTIVE = True
        _MANUAL_SWEEP_IDLE.clear()
        if not _MANUAL_SWEEP_POOL.submit(_run_manual_queue_sweep):
            _MANUAL_SWEEP_ACTIVE = False
            _MANUAL_SWEEP_IDLE.set()
            return False
        return True


def wait_for_manual_queue_sweep(timeout: float | None = 10.0) -> bool:
    """Wait for an explicitly requested sweep to finish (tests and shutdown)."""
    return _MANUAL_SWEEP_IDLE.wait(timeout)


def retry_queue_item(
    queue_id: int, tenant_id: int | None = None, include_all: bool = False
) -> dict[str, Any]:
    """Manually re-queue a FAILED item for immediate dispatch."""
    with SyncSessionLocal() as session:
        if tenant_id is None and not include_all:
            return {"ok": False, "reason": "Queue item not found"}
        stmt = select(RoutingQueueItem).where(RoutingQueueItem.id == queue_id)
        if not include_all:
            stmt = stmt.where(RoutingQueueItem.tenant_id == tenant_id)
        item = session.execute(stmt).scalar_one_or_none()
        if item is None:
            return {"ok": False, "reason": "Queue item not found"}
        if item.status != "FAILED":
            return {"ok": False, "reason": f"Item is {item.status}, not FAILED"}
        item.status = "PENDING"
        item.attempts = 0
        item.next_retry_at = datetime.now(timezone.utc).replace(tzinfo=None)
        item.last_error = None
        session.commit()
        log.info("Routing queue: manually re-queued item %s for study %s",
                 queue_id, item.study_instance_uid)
        return {"ok": True, "item": _queue_item_to_dict(item)}


def list_queue(
    status: str | None = None,
    limit: int = 100,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[dict[str, Any]]:
    """List queue items (optionally filtered by status), newest first."""
    with SyncSessionLocal() as session:
        if tenant_id is None and not include_all:
            return []
        stmt = select(RoutingQueueItem).options(joinedload(RoutingQueueItem.node))
        if not include_all:
            stmt = stmt.where(RoutingQueueItem.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(RoutingQueueItem.status == status.upper())
        stmt = stmt.order_by(RoutingQueueItem.id.desc()).limit(limit)
        return [_queue_item_to_dict(i) for i in session.execute(stmt).scalars().unique().all()]


def _queue_item_to_dict(item: RoutingQueueItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "study_instance_uid": item.study_instance_uid,
        "rule_id": item.rule_id,
        "node_id": item.node_id,
        "node_ae_title": item.node.ae_title if item.node else None,
        "status": item.status,
        "attempts": item.attempts,
        "max_attempts": item.max_attempts,
        "next_retry_at": item.next_retry_at.isoformat() if item.next_retry_at else None,
        "last_error": item.last_error,
        "sent_count": item.sent_count,
        "failed_count": item.failed_count,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def recover_stuck_queue_items() -> None:
    """Items left SENDING by a crashed worker get re-queued on startup."""
    try:
        with SyncSessionLocal() as session:
            session.execute(
                sa_update(RoutingQueueItem)
                .where(RoutingQueueItem.status == "SENDING")
                .values(status="PENDING", next_retry_at=datetime.now(timezone.utc).replace(tzinfo=None))
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 - boundary guard: startup recovery must not log dataset details
        log.error("stuck_routing_recovery_failed error_type=%s", type(exc).__name__)


# CRUD Helpers
async def create_rule(session: AsyncSession, data: dict[str, Any]) -> RoutingRule:
    rule = RoutingRule(**data)
    session.add(rule)
    await session.flush()
    return rule


async def get_rule(session: AsyncSession, rule_id: int) -> RoutingRule | None:
    return (
        await session.execute(
            select(RoutingRule)
            .options(joinedload(RoutingRule.destination_node))
            .where(RoutingRule.id == rule_id)
        )
    ).scalar_one_or_none()


async def list_rules(session: AsyncSession) -> list[RoutingRule]:
    return list(
        (
            await session.execute(
                select(RoutingRule)
                .options(joinedload(RoutingRule.destination_node))
                .order_by(RoutingRule.priority.asc(), RoutingRule.id.asc())
            )
        ).scalars().all()
    )


async def delete_rule(session: AsyncSession, rule_id: int) -> bool:
    rule = await get_rule(session, rule_id)
    if not rule:
        return False
    await session.delete(rule)
    await session.flush()
    return True
