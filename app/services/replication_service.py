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
* Objects are copied **backend-to-backend** through the storage facade
  (stream the object off the study's backend, write it to the target under
  ``{DR_REPLICATION_PREFIX}{StudyInstanceUID}/...``), so any primary backend
  (local folder, MinIO, AWS) replicates to any off-site S3 target.
* **Streaming (C-8):** a study can hold multi-GB instances, so the copy never
  loads an object into RAM. Each object is spooled to a local temp file in
  bounded chunks while its SHA-256 is computed incrementally, then uploaded with
  the target's native multipart uploader (``upload_fileobj``). A sweep therefore
  holds one chunk — plus at most one spooled object — rather than a whole study.
* The background worker sweeps every ``DR_REPLICATION_SWEEP_EVERY_TICKS``:
  studies with no record get enqueued, and PENDING/FAILED records are retried
  until success or the attempt cap. The Admin → Replication panel can also
  trigger a sweep or an on-demand replicate for one study.

Replication is deliberately **never destructive**: the target is write-only for
PAXS. Restoring from the replica is an out-of-band operation (copy objects back
or point a new node's backend at the bucket).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import update as sa_update

from app.core.config import settings
from app.db.models import Instance, ReplicationRecord, Series, Study
from app.db.session import SyncSessionLocal, try_worker_lock
from app.services import storage_service
from app.services.storage import get_storage_backend
from app.services.storage.backend import iter_file_chunks

log = logging.getLogger("paxs.replication")

_STUDY_ENQUEUE_BATCH = 500  # max new studies considered per sweep


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _copy_object_streaming(src_backend, key: str, dst_backend, remote: str) -> tuple[int, str]:
    """Copy one object source→target without buffering it (C-8).

    The object is read off ``src_backend`` in bounded chunks, spooled to a local
    temp file while a SHA-256 is computed **incrementally over those chunks**,
    then handed to ``dst_backend.upload_fileobj`` (managed multipart on S3). Peak
    memory is one chunk; the only full-size copy is the spool, which is removed
    as soon as the upload returns.

    Returns ``(bytes_copied, sha256_hex)``. Raises :class:`StorageError` (or the
    backend's error) on failure — the caller records the study as FAILED.
    """
    fd, spool = tempfile.mkstemp(suffix=".repl")
    os.close(fd)
    digest = hashlib.sha256()
    total = 0
    src_fh = None
    try:
        src_fh = src_backend.read_stream(key)
        with open(spool, "wb") as out:
            for chunk in iter_file_chunks(src_fh):
                digest.update(chunk)
                total += len(chunk)
                out.write(chunk)
        with open(spool, "rb") as spooled:
            dst_backend.upload_fileobj(remote, spooled)
        return total, digest.hexdigest()
    finally:
        if src_fh is not None:
            try:
                src_fh.close()
            except Exception as exc:  # Best-effort close must not mask the copy result.
                log.debug(
                    "replication_source_stream_close_failed error_type=%s",
                    type(exc).__name__,
                )
        with contextlib.suppress(OSError):
            os.unlink(spool)


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
_REPLICATION_LOCK = 1


class ReplicationBusyError(RuntimeError):
    """An automatic or manual replication operation owns the worker lock."""


async def mark_study_dirty(session, study_uid: str) -> None:
    statement = insert(ReplicationRecord).values(
        study_instance_uid=study_uid, target=_target_name(), status="PENDING"
    )
    await session.execute(statement.on_conflict_do_update(
        index_elements=[ReplicationRecord.study_instance_uid],
        set_={
            "generation": ReplicationRecord.generation + 1,
            "status": "PENDING",
            "attempts": 0,
            "first_attempted_at": None,
            "last_error": None,
            "updated_at": func.now(),
        },
    ))


def replicate_study(study_uid: str) -> dict:
    """Copy a study on demand, sharing the automatic sweep's ownership lock."""
    with try_worker_lock(_REPLICATION_LOCK) as acquired:
        if not acquired:
            raise ReplicationBusyError("Replication is already running")
        backend = target_backend()
        with SyncSessionLocal() as session:
            study_id = session.scalar(select(Study.id).where(Study.study_instance_uid == study_uid))
            if study_id is None:
                raise KeyError("Study not found")
            _enqueue_missing(session, [study_uid], _target_name())
            record_id = session.scalar(select(ReplicationRecord.id).where(
                ReplicationRecord.study_instance_uid == study_uid
            ))
            session.commit()
        return _replicate_record(record_id, study_uid, backend)


def _enqueue_missing(session, study_uids: list[str], target: str) -> None:
    if study_uids:
        session.execute(insert(ReplicationRecord).values([
            {"study_instance_uid": uid, "target": target, "status": "PENDING"}
            for uid in study_uids
        ]).on_conflict_do_nothing(index_elements=[ReplicationRecord.study_instance_uid]))


def _replicate_record(record_id: int, study_uid: str, backend, *, automatic: bool = False) -> dict:
    """Book one attempt, then finalize only if ingestion has not advanced the generation."""
    with SyncSessionLocal() as session:
        claim = sa_update(ReplicationRecord).where(ReplicationRecord.id == record_id)
        if automatic:
            claim = claim.where(_due_records())
        generation = session.scalar(claim.values(
            attempts=ReplicationRecord.attempts + 1,
            status="PENDING",
            target=_target_name(),
            first_attempted_at=func.coalesce(ReplicationRecord.first_attempted_at, utcnow()),
        ).returning(ReplicationRecord.generation))
        session.commit()
    if generation is None:
        return {"skipped": True}

    try:
        copied, total_bytes = _copy_study_objects(study_uid, backend)
    except Exception as exc:  # Copy errors may contain storage paths or identifiers.
        _finish_record(record_id, generation, status="FAILED",
                       last_error=f"Replication failed ({type(exc).__name__})")
        log.warning("replication_failed error_type=%s", type(exc).__name__)
        return {"study": study_uid, "ok": False, "error": "Replication failed"}

    completed = _finish_record(
        record_id, generation, status="REPLICATED", attempts=0,
        first_attempted_at=None, object_count=copied, bytes_replicated=total_bytes,
        last_error=None, replicated_at=utcnow(),
    )
    return {"study": study_uid, "objects": copied, "bytes": total_bytes,
            "ok": True, "needs_resweep": not completed}


def _finish_record(record_id: int, generation: int, **values) -> bool:
    with SyncSessionLocal() as session:
        result = session.execute(sa_update(ReplicationRecord).where(
            ReplicationRecord.id == record_id,
            ReplicationRecord.generation == generation,
        ).values(**values))
        session.commit()
        return result.rowcount == 1


def _copy_study_objects(study_uid: str, backend) -> tuple[int, int]:
    prefix = settings.DR_REPLICATION_PREFIX.rstrip("/") + "/"
    with SyncSessionLocal() as session:
        study_id = session.scalar(select(Study.id).where(Study.study_instance_uid == study_uid))
        if study_id is None:
            raise KeyError("Study not found")
        instances = session.execute(
            select(Instance.file_path, Instance.storage_backend, Instance.sop_instance_uid)
            .join(Series, Instance.series_id == Series.id)
            .where(Series.study_id == study_id)
            .order_by(Instance.id)
        ).all()

    copied = total_bytes = 0
    for inst in instances:
        key = inst.file_path
        if not key:
            raise ValueError("Instance storage key is missing")
        remote = f"{prefix}{study_uid}/{inst.sop_instance_uid}.dcm"
        # C-8: stream the object off its backend and upload it to the
        # target in bounded chunks — a multi-GB instance is never held
        # in RAM, and the digest is computed incrementally over the
        # streamed chunks rather than by re-reading the object.
        src_backend = storage_service.resolve_backend(inst.storage_backend)
        size, digest = _copy_object_streaming(src_backend, key, backend, remote)
        log.debug("replicated object %s (%d bytes, sha256 %s)",
                  inst.sop_instance_uid, size, digest)
        total_bytes += size
        copied += 1
        # Thumbnail — strictly best-effort: a transient miss (the
        # thumbnail may not be generated yet, or is being rewritten)
        # must never fail the whole study.
        try:
            thumb = storage_service.thumbnail_key(inst.sop_instance_uid)
            if storage_service.exists(thumb, backend_name=inst.storage_backend):
                _copy_object_streaming(
                    src_backend, thumb, backend,
                    f"{prefix}.thumbnails/{inst.sop_instance_uid}.png",
                )
        except (OSError, ValueError, KeyError) as exc:  # a thumbnail miss must not fail the study
            log.warning("replication_thumbnail_skipped error_type=%s",
                        type(exc).__name__)
    return copied, total_bytes


def _due_records():
    return (
        ReplicationRecord.status.in_(("PENDING", "FAILED"))
        & (ReplicationRecord.attempts < settings.DR_REPLICATION_MAX_ATTEMPTS)
    )


def sweep() -> dict:
    """Discover a bounded batch and process it under exclusive replica ownership."""
    counts = {"enabled": settings.DR_REPLICATION_ENABLED, "replicated": 0,
              "failed": 0, "pending": 0, "skipped": 0}
    if not settings.DR_REPLICATION_ENABLED:
        return counts
    with try_worker_lock(_REPLICATION_LOCK) as acquired:
        if not acquired:
            return {**counts, "busy": True}
        try:
            backend = target_backend()
        except Exception as exc:
            log.warning("DR replication sweep skipped error_type=%s", type(exc).__name__)
            return {**counts, "error": "Replication target unavailable"}

        with SyncSessionLocal() as session:
            has_record = select(ReplicationRecord.id).where(
                ReplicationRecord.study_instance_uid == Study.study_instance_uid
            ).exists()
            new_uids = session.scalars(select(Study.study_instance_uid)
                .where(~has_record).order_by(Study.id).limit(_STUDY_ENQUEUE_BATCH)).all()
            _enqueue_missing(session, list(new_uids), _target_name())
            due = session.execute(select(ReplicationRecord.id, ReplicationRecord.study_instance_uid)
                .where(_due_records()).order_by(ReplicationRecord.updated_at, ReplicationRecord.id)
                .limit(_STUDY_ENQUEUE_BATCH)).all()
            session.commit()

        for record_id, uid in due:
            result = _replicate_record(record_id, uid, backend, automatic=True)
            if result.get("skipped") or result.get("needs_resweep"):
                counts["skipped"] += 1
            elif result["ok"]:
                counts["replicated"] += 1
            else:
                counts["failed"] += 1
        with SyncSessionLocal() as session:
            counts["pending"] = session.scalar(
                select(func.count()).select_from(ReplicationRecord).where(_due_records())
            )
    return counts


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
