"""Auto-routing Rules Engine Service.

Evaluates user-configured DICOM tag rules when new studies arrive, and
dispatches matching studies to remote DICOM Node destinations via C-STORE.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db.models import DicomNode, Instance, RoutingQueueItem, RoutingRule, Series, Study
from app.db.session import AsyncSessionLocal, SyncSessionLocal
from app.dicom.sender import send_file
from app.services import audit_service, storage_service

log = logging.getLogger("paxs.routing")


async def evaluate_rules_for_study(
    session: AsyncSession,
    study_instance_uid: str,
    meta: dict[str, Any] | None = None,
) -> list[RoutingRule]:
    """Evaluate active routing rules against a study and return matched rules."""
    # Query all active rules ordered by priority ASC (lower number = higher priority)
    stmt = (
        select(RoutingRule)
        .options(joinedload(RoutingRule.destination_node))
        .where(RoutingRule.enabled == True)  # noqa: E712
        .order_by(RoutingRule.priority.asc(), RoutingRule.id.asc())
    )
    rules = (await session.execute(stmt)).scalars().all()
    if not rules:
        return []

    # Fetch study details if meta not fully provided
    study = (
        await session.execute(
            select(Study)
            .options(joinedload(Study.series_list))
            .where(Study.study_instance_uid == study_instance_uid)
        )
    ).scalars().unique().one_or_none()

    if not study:
        return []

    # Extract study attributes for matching
    study_desc = meta.get("study_description") if meta else study.study_description
    source_aet = meta.get("source_aet") if meta else ""
    modalities = {s.modality for s in study.series_list if s.modality}
    if meta and meta.get("modality"):
        modalities.add(meta["modality"])

    body_parts = {s.body_part_examined for s in study.series_list if s.body_part_examined}
    if meta and meta.get("body_part_examined"):
        body_parts.add(meta["body_part_examined"])

    matched_rules: list[RoutingRule] = []

    for rule in rules:
        # Check node enabled
        if not rule.destination_node or not rule.destination_node.enabled:
            continue

        # Check Modality match
        if rule.modality and rule.modality.upper() not in {m.upper() for m in modalities if m}:
            continue

        # Check Body Part match
        if (
            rule.body_part_examined
            and rule.body_part_examined.upper() not in {b.upper() for b in body_parts if b}
        ):
            continue

        # Check Source AET match
        if rule.source_aet and source_aet:
            if rule.source_aet.upper() != source_aet.upper():
                continue

        # Check Study Description Pattern match
        if rule.study_description_pattern and study_desc:
            pattern = rule.study_description_pattern
            if not re.search(pattern, study_desc, re.IGNORECASE):
                continue

        matched_rules.append(rule)

    return matched_rules


def dispatch_study_to_node_sync(study_instance_uid: str, node_id: int) -> dict[str, Any]:
    """Synchronously send all instances of a study to a DICOM destination node."""
    with SyncSessionLocal() as session:
        node = session.execute(
            select(DicomNode).where(DicomNode.id == node_id)
        ).scalar_one_or_none()

        if not node or not node.enabled:
            return {"ok": False, "reason": "Destination node disabled or missing"}

        study = session.execute(
            select(Study).where(Study.study_instance_uid == study_instance_uid)
        ).scalar_one_or_none()

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
                res = send_file(node.host, node.port, node.ae_title, file_target)
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
                    except OSError:
                        log.warning(
                            "could not remove routing temp file %s", temp_path,
                            exc_info=True,
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


async def enqueue_dispatch(
    session: AsyncSession,
    study_instance_uid: str,
    rule: RoutingRule,
) -> RoutingQueueItem | None:
    """Enqueue a (study, destination node) dispatch job for the background worker.

    Idempotent: if the same study is already queued for this node (PENDING /
    SENDING), nothing new is created — repeated evaluations (e.g. a later
    instance of the same study arriving) must not duplicate the job.
    """
    if not settings.ROUTING_QUEUE_ENABLED:
        return None
    existing = (
        await session.execute(
            select(RoutingQueueItem).where(
                RoutingQueueItem.study_instance_uid == study_instance_uid,
                RoutingQueueItem.node_id == rule.destination_node_id,
                RoutingQueueItem.status.in_(("PENDING", "SENDING")),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Honor the rule's dispatch delay at enqueue time: the item simply becomes
    # due later (worker sweeps skip items whose next_retry_at is in the future).
    next_retry_at = now + timedelta(seconds=max(rule.delay_seconds or 0, 0))
    item = RoutingQueueItem(
        study_instance_uid=study_instance_uid,
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


async def evaluate_and_route_study(study_instance_uid: str, meta: dict[str, Any] | None = None) -> int:
    """Evaluate routing rules on incoming study and enqueue background dispatch.

    Returns the number of matched rules (queue jobs created). Dispatch itself
    happens in the worker (durable + retried) — see `process_queue_batch`.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            matched_rules = await evaluate_rules_for_study(session, study_instance_uid, meta)
            if not matched_rules:
                return 0
            for rule in matched_rules:
                # `delay_seconds` is honored at enqueue time (next_retry_at),
                # not by sleeping — never hold a DB transaction open while idle.
                await enqueue_dispatch(session, study_instance_uid, rule)
            return len(matched_rules)


# --------------------------------------------------------------------------- #
# Send queue processing (runs in the background worker thread)
# --------------------------------------------------------------------------- #
def process_queue_batch(limit: int = 10) -> dict[str, int]:
    """Claim and dispatch due PENDING queue items; retry failures with backoff.

    Runs in the worker thread (sync engine). For each claimed item:
      * dispatch the study to its node via C-STORE
      * success -> COMPLETED (with sent/failed instance counts)
      * failure -> attempts += 1; if attempts >= max_attempts -> FAILED,
        else next_retry_at = now + backoff (grows with attempts).
    """
    if not settings.ROUTING_QUEUE_ENABLED:
        return {"claimed": 0, "completed": 0, "failed": 0, "requeued": 0}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    counts = {"claimed": 0, "completed": 0, "failed": 0, "requeued": 0}
    with SyncSessionLocal() as session:
        items = (
            session.execute(
                select(RoutingQueueItem)
                .where(
                    RoutingQueueItem.status == "PENDING",
                    (RoutingQueueItem.next_retry_at.is_(None))
                    | (RoutingQueueItem.next_retry_at <= now),
                )
                .order_by(RoutingQueueItem.id)
                .limit(limit)
            )
        ).scalars().all()

    # Dispatch outside the claim transaction: dispatch_study_to_node_sync opens
    # its own session (SQLite serializes writers), so committing the claim first
    # avoids a "database is locked" deadlock on single-file DBs.
    for item in items:
        with SyncSessionLocal() as session:
            fresh = session.get(RoutingQueueItem, item.id)
            if fresh is None or fresh.status != "PENDING":
                continue
            fresh.status = "SENDING"
            fresh.attempts += 1
            session.commit()
        counts["claimed"] += 1

        try:
            res = dispatch_study_to_node_sync(item.study_instance_uid, item.node_id)
        except Exception as exc:  # noqa: BLE001 — a send error must never kill the sweep
            res = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

        with SyncSessionLocal() as session:
            fresh = session.get(RoutingQueueItem, item.id)
            if fresh is None:
                continue
            if res.get("ok"):
                fresh.status = "COMPLETED"
                fresh.sent_count = int(res.get("sent", 0))
                fresh.failed_count = int(res.get("failed", 0))
                fresh.last_error = None
                fresh.next_retry_at = None
                counts["completed"] += 1
                log.info("Routing queue: study %s sent to node %s (%s sent, %s failed)",
                         item.study_instance_uid, item.node_id,
                         fresh.sent_count, fresh.failed_count)
            else:
                fresh.last_error = str(res.get("reason") or "unknown error")[:1000]
                if fresh.attempts >= fresh.max_attempts:
                    fresh.status = "FAILED"
                    fresh.next_retry_at = None
                    counts["failed"] += 1
                    log.warning("Routing queue: study %s FAILED after %d attempts (%s)",
                                item.study_instance_uid, fresh.attempts, fresh.last_error)
                else:
                    backoff = settings.ROUTING_RETRY_BACKOFF_SECONDS * (fresh.attempts ** 2)
                    fresh.status = "PENDING"
                    fresh.next_retry_at = now + timedelta(seconds=backoff)
                    counts["requeued"] += 1
                    log.info("Routing queue: study %s retry #%d in %ds (%s)",
                             item.study_instance_uid, fresh.attempts, backoff, fresh.last_error)
            session.commit()
    return counts


def retry_queue_item(queue_id: int) -> dict[str, Any]:
    """Manually re-queue a FAILED item for immediate dispatch."""
    with SyncSessionLocal() as session:
        item = session.get(RoutingQueueItem, queue_id)
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


def list_queue(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """List queue items (optionally filtered by status), newest first."""
    with SyncSessionLocal() as session:
        stmt = (
            select(RoutingQueueItem)
            .options(joinedload(RoutingQueueItem.node))
            .order_by(RoutingQueueItem.id.desc())
            .limit(limit)
        )
        if status:
            stmt = (
                select(RoutingQueueItem)
                .options(joinedload(RoutingQueueItem.node))
                .where(RoutingQueueItem.status == status.upper())
                .order_by(RoutingQueueItem.id.desc())
                .limit(limit)
            )
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
    from sqlalchemy import update

    try:
        with SyncSessionLocal() as session:
            session.execute(
                update(RoutingQueueItem)
                .where(RoutingQueueItem.status == "SENDING")
                .values(status="PENDING", next_retry_at=datetime.now(timezone.utc).replace(tzinfo=None))
            )
            session.commit()
    except Exception:
        log.exception("Failed to recover stuck routing queue items")


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
