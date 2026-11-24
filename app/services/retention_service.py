"""Automated multi-year database hygiene for high-volume PAXS archives.

Keeps ``compression_jobs`` and (optionally) ``audit_logs`` bounded in small
batches so multi-year clinical installations never suffer table bloat or long
maintenance locks.

Compliance invariant:
  ``AUDIT_LOG_RETENTION_DAYS`` defaults to ``0`` (never delete). When an
  operator configures a positive retention window, rows are serialized to a
  gzipped NDJSON archive under ``BACKUP_DIR/audit_archives/``, flushed to disk,
  and verified by reading the archive back *before* those exact row IDs are
  deleted from PostgreSQL.
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AuditLog, CompressionJob
from app.db.session import SyncSessionLocal

log = logging.getLogger("paxs.retention")

DEFAULT_RETENTION_BATCH_SIZE = 1000
_TERMINAL_COMPRESSION_STATUSES = ("DONE", "FAILED")


def _utc_cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)


def _serialize_audit_row(row: AuditLog) -> str:
    performed = getattr(row, "performed_at", None)
    payload = {
        "id": row.id,
        "user_id": getattr(row, "user_id", None),
        "performed_at": performed.isoformat() if performed is not None else None,
        "action": row.action,
        "performed_by": row.performed_by,
        "sop_instance_uid": row.sop_instance_uid,
        "study_instance_uid": row.study_instance_uid,
        "sha256_hash": getattr(row, "sha256_hash", None),
        "config_key": getattr(row, "config_key", None),
        "old_value": getattr(row, "old_value", None),
        "new_value": getattr(row, "new_value", None),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _write_verified_audit_archive(rows: list[AuditLog], archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    target = archive_dir / f"audit_archive_{stamp}.ndjson.gz"
    lines = [_serialize_audit_row(r) for r in rows]
    with gzip.open(target, "wt", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
    # Verify readable gzip integrity and exact line count before any DB delete.
    with gzip.open(target, "rt", encoding="utf-8") as fh:
        verified_count = sum(1 for _ in fh)
    if verified_count != len(rows):
        target.unlink(missing_ok=True)
        raise OSError(f"Audit archive verification failed ({verified_count} != {len(rows)})")
    return target


def prune_completed_compression_jobs(
    session: Session,
    *,
    retention_days: int | None = None,
    batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
) -> int:
    """Delete one bounded batch of old terminal (DONE/FAILED) compression_jobs."""
    days = int(settings.COMPRESSION_JOBS_RETENTION_DAYS if retention_days is None else retention_days)
    if days <= 0:
        return 0
    cutoff = _utc_cutoff(days)
    ids = list(
        session.execute(
            select(CompressionJob.id)
            .where(
                CompressionJob.status.in_(_TERMINAL_COMPRESSION_STATUSES),
                CompressionJob.finished_at.is_not(None),
                CompressionJob.finished_at < cutoff,
            )
            .order_by(CompressionJob.id)
            .limit(max(1, int(batch_size)))
        ).scalars().all()
    )
    if not ids:
        return 0
    res = session.execute(delete(CompressionJob).where(CompressionJob.id.in_(ids)))
    return int(res.rowcount or 0)


def archive_and_prune_audit_logs(
    session: Session,
    *,
    retention_days: int | None = None,
    archive_dir: Path | None = None,
    batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
) -> dict[str, Any]:
    """Archive one bounded batch of old audit_logs to NDJSON.gz, then delete them."""
    days = int(settings.AUDIT_LOG_RETENTION_DAYS if retention_days is None else retention_days)
    if days <= 0:
        return {"archived": 0, "pruned": 0, "archive_path": None}
    cutoff = _utc_cutoff(days)
    rows = list(
        session.execute(
            select(AuditLog)
            .where(AuditLog.performed_at < cutoff)
            .order_by(AuditLog.id)
            .limit(max(1, int(batch_size)))
        ).scalars().all()
    )
    if not rows:
        return {"archived": 0, "pruned": 0, "archive_path": None}
    dest_dir = archive_dir or (Path(settings.BACKUP_DIR) / "audit_archives")
    archive_path = _write_verified_audit_archive(rows, dest_dir)
    ids = [r.id for r in rows]
    res = session.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
    return {
        "archived": len(rows),
        "pruned": int(res.rowcount or 0),
        "archive_path": str(archive_path),
    }


def run_retention_sweep() -> dict[str, Any]:
    """Execute one bounded retention pass across compression_jobs and audit_logs."""
    with SyncSessionLocal() as session:
        comp_pruned = prune_completed_compression_jobs(session)
        audit_res = archive_and_prune_audit_logs(session)
        session.commit()
    if comp_pruned or audit_res["pruned"]:
        log.info(
            "retention_sweep compression_jobs_pruned=%d audit_logs_archived=%d",
            comp_pruned,
            audit_res["archived"],
        )
    return {
        "compression_jobs_pruned": comp_pruned,
        "audit_logs_archived": audit_res["archived"],
        "audit_logs_pruned": audit_res["pruned"],
        "audit_archive_path": audit_res["archive_path"],
    }
