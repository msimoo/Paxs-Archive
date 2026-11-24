"""Tenant-scoped persistence and worker execution for one-shot transfers (FT-1)."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import DicomNode, TransferJob, User
from app.db.session import SyncSessionLocal, try_worker_lock
from app.services import query_service, tenant_service

log = logging.getLogger("paxs.transfers")
_TRANSFER_LOCK = 2


def scope_transfer_query(statement: Any, user: User) -> Any:
    """Apply the archive's canonical tenant predicate to a transfer query."""
    tenant_id, include_all = tenant_service.scope_from_user(user)
    return query_service.apply_tenant_scope(
        statement, TransferJob.tenant_id, tenant_id, include_all
    )


async def enqueue(db: AsyncSession, user: User, values: dict[str, Any]) -> TransferJob:
    """Create one durable request after enforcing the bounded pending queue."""
    outstanding = await db.scalar(
        select(func.count()).select_from(TransferJob).where(
            TransferJob.status.in_(("PENDING", "RUNNING"))
        )
    )
    capacity = max(0, int(settings.TRANSFER_JOB_QUEUE_CAP))
    if capacity == 0 or int(outstanding or 0) >= capacity:
        raise OverflowError("Transfer queue is full or disabled")
    job = TransferJob(
        tenant_id=user.tenant_id,
        requested_by=user.id,
        status="PENDING",
        **values,
    )
    db.add(job)
    await db.flush()
    return job


async def list_jobs(db: AsyncSession, user: User, limit: int, offset: int) -> list[TransferJob]:
    """Return a bounded, newest-first page of jobs visible to the caller."""
    statement = scope_transfer_query(select(TransferJob), user)
    rows = await db.scalars(
        statement.order_by(TransferJob.created_at.desc(), TransferJob.id.desc())
        .offset(offset).limit(limit)
    )
    return list(rows.all())


async def get_job(db: AsyncSession, user: User, job_id: int) -> TransferJob | None:
    """Resolve one job within the caller's tenant scope; misses stay opaque."""
    statement = scope_transfer_query(select(TransferJob), user)
    return await db.scalar(statement.where(TransferJob.id == job_id))


async def require_send_scope(db: AsyncSession, user: User, study_uids: list[str]) -> None:
    """Require every selected study to exist inside the caller's tenant scope."""
    from app.db.models import Study
    from app.services import query_service, tenant_service

    tenant_id, include_all = tenant_service.scope_from_user(user)
    statement = select(Study.study_instance_uid).where(
        Study.study_instance_uid.in_(study_uids)
    )
    statement = query_service.apply_study_tenant_scope(statement, tenant_id, include_all)
    found = set((await db.scalars(statement)).all())
    if found != set(study_uids):
        raise LookupError("One or more studies were not found")


async def cancel_pending(db: AsyncSession, user: User, job_id: int) -> bool:
    """Cancel only a still-pending job with one guarded update."""
    statement = scope_transfer_query(update(TransferJob), user)
    result = await db.execute(
        statement.where(TransferJob.id == job_id, TransferJob.status == "PENDING")
        .values(status="CANCELLED", finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
    )
    return result.rowcount == 1


async def clear_history(db: AsyncSession, user: User, limit: int) -> int:
    """Remove a bounded batch of terminal job records, preserving active work."""
    terminal = ("COMPLETED", "PARTIAL", "FAILED", "CANCELLED")
    selection = scope_transfer_query(select(TransferJob.id), user).where(
        TransferJob.status.in_(terminal)
    ).order_by(TransferJob.created_at, TransferJob.id).limit(limit)
    job_ids = list((await db.scalars(selection)).all())
    if not job_ids:
        return 0
    statement = scope_transfer_query(delete(TransferJob), user).where(
        TransferJob.id.in_(job_ids), TransferJob.status.in_(terminal)
    )
    result = await db.execute(statement)
    return result.rowcount


def process_one_pending_job() -> bool:
    """Recover interrupted transfers and run one job under exclusive queue ownership."""
    with try_worker_lock(_TRANSFER_LOCK) as acquired:
        if not acquired:
            return False
        return _process_owned_queue()


def _process_owned_queue() -> bool:
    with SyncSessionLocal() as session:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # A peer may have received part or all of an interrupted send.
        session.execute(update(TransferJob).where(TransferJob.status == "RUNNING").values(
            status="FAILED", finished_at=now, error_code="TRANSFER_INTERRUPTED",
            error_message="Transfer was interrupted. Verify the destination before retrying.",
        ))
        session.commit()
        job_id = session.scalar(
            select(TransferJob.id).where(TransferJob.status == "PENDING")
            .order_by(TransferJob.created_at, TransferJob.id).limit(1)
        )
        if job_id is None:
            return False
        claim = session.execute(
            update(TransferJob)
            .where(TransferJob.id == job_id, TransferJob.status == "PENDING")
            .values(status="RUNNING", started_at=now, error_code=None, error_message=None)
        )
        session.commit()
        if claim.rowcount != 1:
            return False
        job = session.get(TransferJob, job_id)
        if job is None:
            return False
        payload = {
            "tenant_id": job.tenant_id,
            "include_all": job.include_all,
            "node_ae_title": job.node_ae_title,
            "direction": job.direction,
            "transport": job.transport,
            "our_ae_title": job.our_ae_title,
            "study_uids": json.loads(job.study_uids_json),
        }
    _execute_claimed(job_id, payload)
    return True


def _execute_claimed(job_id: int, job: dict[str, Any]) -> None:
    """Run the selected existing protocol adapter and persist safe counters."""
    on_progress = _progress_callback(job_id)
    try:
        if job["direction"] == "SEND":
            result = _send(job, on_progress)
            completed = int(result.get("sent", 0))
            failed = int(result.get("failed", 0))
            outcomes = result.get("results") or []
        else:
            result = _retrieve(job, on_progress)
            completed = int(result.get("pulled", 0))
            failed = int(result.get("failed", 0))
            outcomes = result.get("results") or []
        total = completed + failed
        from app.services.transfer_outcome import classify_outcome

        status, error_code, error_message = classify_outcome(result, completed, failed)
        _finish(job_id, status, total, completed, failed, error_code, error_message)
        _audit_send_results(outcomes, job) if job["direction"] == "SEND" else None
    except Exception as exc:  # protocol boundary; exception text may contain PHI
        log.warning("transfer job failed (id=%s, error_type=%s)", job_id, type(exc).__name__)
        _finish(job_id, "FAILED", 0, 0, 0, "TRANSFER_FAILED", "Transfer failed; inspect peer and archive logs")


def _send(job: dict[str, Any], on_progress: Any = None) -> dict[str, Any]:
    """Send selected local study UIDs through the existing C-STORE batch path."""
    from app.dicom.sender import send_to_node_sync

    return send_to_node_sync(
        job["node_ae_title"], study_uids=job["study_uids"],
        our_ae_title=job["our_ae_title"], tenant_id=job["tenant_id"],
        include_all=job["include_all"], on_progress=on_progress,
    )


def _retrieve(job: dict[str, Any], on_progress: Any = None) -> dict[str, Any]:
    """Retrieve one study using the selected DIMSE or DICOMweb adapter."""
    if job["transport"] == "DIMSE":
        from app.dicom.sender import pull_from_node_sync

        return pull_from_node_sync(
            job["node_ae_title"], level="STUDY",
            filters={"StudyInstanceUID": job["study_uids"][0]},
            our_ae_title=job["our_ae_title"], tenant_id=job["tenant_id"],
            on_progress=on_progress, include_all=job["include_all"],
        )
    if job["transport"] == "CMOVE":
        return _retrieve_move(job, on_progress)
    with SyncSessionLocal() as session:
        node = session.scalar(
            select(DicomNode).where(DicomNode.ae_title == job["node_ae_title"])
        )
        if node is None or not node.enabled or not node.web_url:
            raise ValueError("DICOMweb node unavailable")
        detached = SimpleNamespace(
            ae_title=node.ae_title, web_url=node.web_url,
            web_username=node.web_username, web_password=node.web_password,
        )
    from app.services.dicomweb_client import wado_retrieve

    return wado_retrieve(
        detached, job["study_uids"][0], job["tenant_id"],
        on_progress=on_progress, include_all=job["include_all"],
    )


def _retrieve_move(job: dict[str, Any], on_progress: Any = None) -> dict[str, Any]:
    from app.dicom.dcmtk_move import retrieve_study
    from app.dicom.sender.cget import _PullCollector

    with SyncSessionLocal() as session:
        node = session.scalar(select(DicomNode).where(DicomNode.ae_title == job["node_ae_title"]))
        if node is None or not node.enabled:
            return {"ok": False, "reason": "DICOM node unavailable"}
        peer = SimpleNamespace(
            host=node.host, port=node.port, ae_title=node.ae_title, trust_cert=node.trust_cert,
        )
    collector = _PullCollector(job["tenant_id"], on_progress, include_all=job["include_all"])
    return retrieve_study(peer, job["study_uids"][0], job["our_ae_title"], collector)


def _update_progress(job_id: int, total: int, completed: int, failed: int) -> None:
    """Persist a PHI-free snapshot after each completed instance."""
    with SyncSessionLocal() as session:
        session.execute(
            update(TransferJob).where(
                TransferJob.id == job_id, TransferJob.status == "RUNNING"
            ).values(
                total_items=max(0, int(total)), completed_items=max(0, int(completed)),
                failed_items=max(0, int(failed)),
            )
        )
        session.commit()


def _notify_progress(job_id: int, total: int, completed: int, failed: int) -> None:
    """Keep observational progress persistence from altering a DIMSE result."""
    try:
        _update_progress(job_id, total, completed, failed)
    except Exception as exc:  # progress is advisory; the transfer outcome remains authoritative
        log.warning("transfer progress update failed (id=%s, error_type=%s)",
                    job_id, type(exc).__name__)


def _progress_callback(job_id: int):
    """Coalesce progress writes so large studies add at most two updates per second."""
    last_write = 0.0

    def report(total: int, completed: int, failed: int) -> None:
        nonlocal last_write
        now = time.monotonic()
        if now - last_write < 0.5:
            return
        last_write = now
        _notify_progress(job_id, total, completed, failed)

    return report


def _finish(job_id: int, status: str, total: int, completed: int, failed: int,
            error_code: str | None, error_message: str | None) -> None:
    """Persist final counters without exposing remote exception contents."""
    with SyncSessionLocal() as session:
        session.execute(
            update(TransferJob).where(
                TransferJob.id == job_id, TransferJob.status == "RUNNING"
            ).values(
                status=status, total_items=total, completed_items=completed,
                failed_items=failed, error_code=error_code,
                error_message=error_message,
                finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        session.commit()


def _audit_send_results(outcomes: list[dict[str, Any]], job: dict[str, Any]) -> None:
    """Audit successful outbound instances using the existing PHI-safe audit service."""
    from app.services.audit_service import add_audit

    with SyncSessionLocal() as session:
        for item in outcomes:
            if item.get("ok"):
                add_audit(
                    session, item["sop_uid"], "FORWARDED", performed_by="transfer-job",
                    study_instance_uid=item.get("study_uid"), file_path=None,
                )
        session.commit()
