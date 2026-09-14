"""Disaster Recovery replication (roadmap #49).

Duplicates study objects to an **off-site S3 / Azure-Blob-compatible target** so
the archive survives the loss of the primary site. The target is either a
registered storage backend (Admin → Storage, kind=s3 — e.g. a MinIO replica or
AWS S3 bucket) named by ``DR_REPLICATION_TARGET``, or the environment S3 config
(``S3_*``) when the name is empty.

Design
------
* ``ReplicationRecord`` rows (``replication_records``) track per-study state:
  PENDING -> REPLICATED, or FAILED with ``last_error`` and a bounded ``attempts``
  counter (``DR_REPLICATION_MAX_ATTEMPTS``).
* Objects are copied **backend-to-backend** through the storage facade (read
  bytes on the study's backend, write to the target under
  ``{DR_REPLICATION_PREFIX}{StudyInstanceUID}/...``), so any primary backend
  (local folder, MinIO, AWS) replicates to any off-site S3 target.
* The background worker sweeps every ``DR_REPLICATION_SWEEP_EVERY_TICKS``:
  studies with no record get enqueued, and PENDING/FAILED records are retried
  until success or the attempt cap. The Admin → Replication panel can also
  trigger a sweep or an on-demand replicate for one study.

Replication is deliberately **never destructive**: the target is write-only for
PAXS. Restoring from the replica is an out-of-band operation (copy objects back
or point a new node's backend at the bucket).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core.config import settings
from app.db.models import Instance, ReplicationRecord, Series, Study
from app.db.session import SyncSessionLocal
from app.services import storage_service
from app.services.storage import get_storage_backend

log = logging.getLogger("paxs.replication")

_STUDY_ENQUEUE_BATCH = 500  # max new studies considered per sweep


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Target resolution
# --------------------------------------------------------------------------- #
def target_display() -> str:
    """Human-readable description of the configured replica target."""
    name = (settings.DR_REPLICATION_TARGET or "").strip()
    if name:
        return name
    if (settings.STORAGE_BACKEND or "local").lower() == "s3":
        return f"env-s3 ({settings.S3_BUCKET}@{settings.S3_ENDPOINT_URL or 'aws'})"
    return "unconfigured (set DR_REPLICATION_TARGET or STORAGE_BACKEND=s3)"


def target_backend():
    """The StorageBackend replica target (raises ValueError if unconfigured)."""
    name = (settings.DR_REPLICATION_TARGET or "").strip()
    if name:
        return get_storage_backend(name)  # raises KeyError if unknown/disabled
    if (settings.STORAGE_BACKEND or "local").lower() == "s3":
        return get_storage_backend()
    raise ValueError("no DR target configured (set DR_REPLICATION_TARGET "
                     "or STORAGE_BACKEND=s3)")


def _target_name() -> str:
    name = (settings.DR_REPLICATION_TARGET or "").strip()
    return name if name else "env-s3"


# --------------------------------------------------------------------------- #
# Enqueue / replicate
# --------------------------------------------------------------------------- #
def enqueue_study(session, study_uid: str, target: str | None = None) -> ReplicationRecord:
    """Idempotently mark a study for replication (PENDING)."""
    target = target or _target_name()
    row = session.execute(
        select(ReplicationRecord).where(
            ReplicationRecord.study_instance_uid == study_uid
        )
    ).scalar_one_or_none()
    if row is None:
        row = ReplicationRecord(
            study_instance_uid=study_uid, target=target, status="PENDING"
        )
        session.add(row)
    elif row.status == "FAILED":
        row.status = "PENDING"
        row.last_error = None
    return row


def replicate_study(study_uid: str) -> dict:
    """Copy one study's objects to the replica target; updates its record.

    Runs synchronously (the worker calls it in its own session; the API wraps
    it in a thread). Returns a summary dict; raises on configuration errors.
    """
    backend = target_backend()
    target = _target_name()
    prefix = settings.DR_REPLICATION_PREFIX.rstrip("/") + "/"

    with SyncSessionLocal() as session:
        study = session.execute(
            select(Study).where(Study.study_instance_uid == study_uid)
        ).scalar_one_or_none()
        if study is None:
            raise KeyError(f"study not found: {study_uid}")
        record = enqueue_study(session, study_uid, target)
        # A fresh row's ORM default (0) is only applied at flush, so guard None.
        record.attempts = (record.attempts or 0) + 1
        if record.first_attempted_at is None:
            record.first_attempted_at = utcnow()
        record.status = "PENDING"
        session.flush()

        instances = session.execute(
            select(Instance)
            .join(Series, Instance.series_id == Series.id)
            .where(Series.study_id == study.id)
            .order_by(Instance.id)
        ).scalars().all()
        if not instances:
            record.status = "REPLICATED"
            record.object_count = 0
            record.bytes_replicated = 0
            record.replicated_at = utcnow()
            session.commit()
            return {"study": study_uid, "objects": 0, "bytes": 0}

        copied = 0
        total_bytes = 0
        try:
            for inst in instances:
                key = inst.file_path
                if not key:
                    continue
                remote = f"{prefix}{study_uid}/{inst.sop_instance_uid}.dcm"
                data = storage_service.read_bytes(
                    key, backend_name=inst.storage_backend
                )
                backend.save_bytes(remote, data)
                total_bytes += len(data)
                copied += 1
                # Thumbnail — strictly best-effort: a transient miss (the
                # thumbnail may not be generated yet, or is being rewritten)
                # must never fail the whole study.
                try:
                    thumb = storage_service.thumbnail_key(inst.sop_instance_uid)
                    if storage_service.exists(thumb, backend_name=inst.storage_backend):
                        backend.save_bytes(
                            f"{prefix}.thumbnails/{inst.sop_instance_uid}.png",
                            storage_service.read_bytes(thumb, backend_name=inst.storage_backend),
                        )
                except Exception as exc:
                    log.warning("replication: thumbnail for %s skipped: %s",
                                inst.sop_instance_uid, exc)
        except Exception as exc:
            record.status = "FAILED"
            record.last_error = str(exc)[:1000]
            session.commit()
            log.warning("replication of %s failed (%d/%d objects): %s",
                        study_uid, copied, len(instances), exc)
            return {"study": study_uid, "objects": copied, "bytes": total_bytes,
                    "ok": False, "error": str(exc)}

        record.status = "REPLICATED"
        record.object_count = copied
        record.bytes_replicated = total_bytes
        record.last_error = None
        record.replicated_at = utcnow()
        session.commit()
        log.info("replicated %s (%d objects, %d bytes) -> %s",
                 study_uid, copied, total_bytes, target)
        return {"study": study_uid, "objects": copied, "bytes": total_bytes, "ok": True}


# --------------------------------------------------------------------------- #
# Sweep (worker + on-demand)
# --------------------------------------------------------------------------- #
def sweep() -> dict:
    """Replicate due studies: enqueue new ones, retry PENDING/FAILED.

    Returns a summary; never raises (failures are recorded per study)."""
    if not settings.DR_REPLICATION_ENABLED:
        return {"enabled": False, "replicated": 0, "failed": 0, "pending": 0}
    try:
        target_backend()  # validate target up front
    except Exception as exc:
        log.warning("DR replication sweep skipped: %s", exc)
        return {"enabled": True, "replicated": 0, "failed": 0,
                "pending": 0, "error": str(exc)}

    with SyncSessionLocal() as session:
        # (1) enqueue studies that have no record yet (recent first).
        existing = set(session.execute(
            select(ReplicationRecord.study_instance_uid)
        ).scalars().all())
        new_uids = session.execute(
            select(Study.study_instance_uid)
            .order_by(Study.id.desc())
            .limit(_STUDY_ENQUEUE_BATCH)
        ).scalars().all()
        for uid in new_uids:
            if uid not in existing:
                enqueue_study(session, uid, _target_name())
        session.commit()

        # (2) replicate due records.
        due = session.execute(
            select(ReplicationRecord)
            .where(ReplicationRecord.status.in_(("PENDING", "FAILED")))
            .where(ReplicationRecord.attempts < settings.DR_REPLICATION_MAX_ATTEMPTS)
            .order_by(ReplicationRecord.id)
        ).scalars().all()
        due_uids = [r.study_instance_uid for r in due]
    session.close()

    done = failed = 0
    for uid in due_uids:
        try:
            result = replicate_study(uid)
            if result.get("ok", True) is False:
                failed += 1
            else:
                done += 1
        except Exception as exc:
            failed += 1
            log.warning("replication sweep item %s failed: %s", uid, exc)
    return {"enabled": True, "replicated": done, "failed": failed,
            "pending": len(due_uids) - done - failed}


def status() -> dict:
    """Summary counts + target description for the dashboard/API."""
    with SyncSessionLocal() as session:
        rows = session.execute(
            select(ReplicationRecord.status, func.count(ReplicationRecord.id))
            .group_by(ReplicationRecord.status)
        ).all()
        total_bytes = session.execute(
            select(func.coalesce(func.sum(ReplicationRecord.bytes_replicated), 0))
        ).scalar_one()
    counts = {s: int(n) for s, n in rows}
    return {
        "enabled": bool(settings.DR_REPLICATION_ENABLED),
        "target": target_display(),
        "prefix": settings.DR_REPLICATION_PREFIX,
        "pending": counts.get("PENDING", 0),
        "replicated": counts.get("REPLICATED", 0),
        "failed": counts.get("FAILED", 0),
        "total_studies": sum(counts.values()),
        "total_bytes": int(total_bytes),
    }


def records(limit: int = 30) -> list[dict]:
    with SyncSessionLocal() as session:
        rows = session.execute(
            select(ReplicationRecord).order_by(ReplicationRecord.id.desc()).limit(
                min(max(limit, 1), 200)
            )
        ).scalars().all()
        return [
            {
                "id": r.id,
                "study_instance_uid": r.study_instance_uid,
                "target": r.target,
                "status": r.status,
                "attempts": r.attempts,
                "object_count": r.object_count,
                "bytes_replicated": r.bytes_replicated,
                "last_error": r.last_error,
                "replicated_at": r.replicated_at.isoformat() if r.replicated_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
