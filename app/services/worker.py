"""Background worker for lossless compression + thumbnail generation.

Runs in a daemon thread and consumes the `compression_jobs` table — no Redis
or Celery required for a single-node deployment. Celery can be swapped in for
horizontal scale (see docs/IMPLEMENTATION-PLAN.md Phase 2.8).

Uses the sync engine because it lives in a non-async thread.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.db.models import CompressionJob, Instance, Series, Session, Study, TriageFlag
from app.db.session import SyncSessionLocal
from app.services import content_version, storage_service, thumbnail_service
from app.services import triage_service
from app.services.compression_service import (
    NATIVE_METHOD, SOURCE_METHOD, THUMBNAIL_METHOD, compression_window_open,
)

log = logging.getLogger("paxs.worker")


class CompressionWorker(threading.Thread):
    def __init__(self, poll_seconds: float | None = None) -> None:
        super().__init__(name="paxs-compression-worker", daemon=True)
        self.poll_seconds = poll_seconds or settings.WORKER_POLL_SECONDS
        self.owner_id = uuid.uuid4().hex
        self._stop_event = threading.Event()
        self._active_jobs_lock = threading.Lock()
        self._active_job_ids: set[int] = set()
        self._tick = 0
        self._session_tick = 0
        self._quota_tick = 0
        self._reconcile_tick = 0

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("Compression worker started (poll %.1fs)", self.poll_seconds)
        from app.services import replication_service, transfer_service

        tasks = (
            ("transfer", transfer_service.process_one_pending_job, lambda: 1),
            ("replication", replication_service.sweep,
             lambda: settings.DR_REPLICATION_SWEEP_EVERY_TICKS if settings.DR_REPLICATION_ENABLED else 0),
            ("routing", self._sweep_routing,
             lambda: settings.ROUTING_SWEEP_EVERY_TICKS if settings.ROUTING_RUN_IN_WORKER else 0),
        )
        workers = [threading.Thread(target=self._poll, args=task,
                                    name=f"paxs-{task[0]}-worker", daemon=True)
                   for task in tasks]
        heartbeat_thread = threading.Thread(
            target=self._shared_heartbeat_loop,
            name="paxs-job-lease-heartbeat",
            daemon=True,
        )
        workers.append(heartbeat_thread)
        for worker in workers:
            worker.start()
        try:
            self._poll("compression", self._process_batch, lambda: 1)
        finally:
            self.stop()
            for worker in workers:
                worker.join(timeout=1)
        log.info("Compression worker stopped")

    def _poll(self, name, operation, every_ticks) -> None:
        tick = 0
        while not self._stop_event.is_set():
            interval = every_ticks()
            tick = tick + 1 if interval > 0 else 0
            if interval > 0 and tick >= interval:
                tick = 0
                try:
                    operation()
                except Exception as exc:  # A failed batch must not stop polling.
                    log.error("worker_batch_failed worker=%s error_type=%s", name, type(exc).__name__)
            self._stop_event.wait(self.poll_seconds)

    # ------------------------------------------------------------------ #
    def _claim_jobs(self, session: Any, limit: int | None = None) -> list[CompressionJob]:
        """Atomically claim pending or lease-expired jobs via FOR UPDATE SKIP LOCKED."""
        from sqlalchemy import or_, update as sa_update

        limit = limit or settings.WORKER_BATCH_SIZE
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        eligible = or_(
            CompressionJob.status == "PENDING",
            (CompressionJob.status == "RUNNING") & (CompressionJob.lease_until <= now),
        )
        if not compression_window_open(
            settings.COMPRESSION_TIMING, settings.COMPRESSION_START_TIME,
            settings.COMPRESSION_END_TIME, datetime.now().strftime("%H:%M"),
        ):
            eligible &= CompressionJob.method == THUMBNAIL_METHOD
        ids = session.execute(
            select(CompressionJob.id)
            .where(eligible, CompressionJob.attempt < settings.WORKER_JOB_MAX_ATTEMPTS)
            .order_by(CompressionJob.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars().all()
        if not ids:
            session.rollback()
            return []
        res = session.execute(
            sa_update(CompressionJob)
            .where(
                CompressionJob.id.in_(ids), eligible,
                CompressionJob.attempt < settings.WORKER_JOB_MAX_ATTEMPTS,
            )
            .values(
                status="RUNNING",
                owner_id=self.owner_id,
                lease_until=now + timedelta(seconds=settings.WORKER_JOB_LEASE_SECONDS),
                attempt=CompressionJob.attempt + 1,
            )
        )
        session.commit()
        if res.rowcount == 0:
            return []
        return session.execute(
            select(CompressionJob)
            .where(
                CompressionJob.id.in_(ids),
                CompressionJob.status == "RUNNING",
                CompressionJob.owner_id == self.owner_id,
            )
            .order_by(CompressionJob.id)
        ).scalars().all()

    # ------------------------------------------------------------------ #
    def _run_claimed_job_id(self, job_id: int) -> None:
        """Process one already-claimed job in its own sync DB session."""
        with self._active_jobs_lock:
            self._active_job_ids.add(job_id)
        try:
            with SyncSessionLocal() as session:
                job = session.get(CompressionJob, job_id)
                if job is None or job.owner_id != self.owner_id:
                    return
                self._process_job(session, job)
                session.commit()
        finally:
            with self._active_jobs_lock:
                self._active_job_ids.discard(job_id)

    def _run_compression_jobs(self) -> None:
        batch_size = max(1, int(settings.WORKER_BATCH_SIZE))
        concurrency = max(1, int(getattr(settings, "WORKER_COMPRESSION_CONCURRENCY", 1)))
        if concurrency == 1:
            for _ in range(batch_size):
                if self._stop_event.is_set():
                    return
                with SyncSessionLocal() as session:
                    jobs = self._claim_jobs(session, limit=1)
                    if not jobs:
                        break
                    job = jobs[0]
                    with self._active_jobs_lock:
                        self._active_job_ids.add(job.id)
                    try:
                        self._process_job(session, job)
                        session.commit()
                    finally:
                        with self._active_jobs_lock:
                            self._active_job_ids.discard(job.id)
            return
        processed = 0
        with ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="paxs-compress"
        ) as pool:
            while processed < batch_size and not self._stop_event.is_set():
                wave = min(concurrency, batch_size - processed)
                with SyncSessionLocal() as session:
                    job_ids = [j.id for j in self._claim_jobs(session, limit=wave)]
                if not job_ids:
                    break
                processed += len(job_ids)
                for fut in [pool.submit(self._run_claimed_job_id, jid) for jid in job_ids]:
                    try:
                        fut.result()
                    except Exception as exc:  # noqa: BLE001 - isolate per-job failure
                        log.warning(
                            "compression_worker_task_failed error_type=%s",
                            type(exc).__name__,
                        )

    def _process_batch(self) -> None:
        self._run_compression_jobs()
        with SyncSessionLocal() as session:
            if settings.TRIAGE_RUN_IN_WORKER:
                self._process_triage(session)
                session.commit()
            # Lifecycle rule sweep — every N ticks to bound DB scans.
            if settings.LIFECYCLE_RUN_IN_WORKER:
                self._tick += 1
                if self._tick >= settings.LIFECYCLE_SWEEP_EVERY_TICKS:
                    self._tick = 0
                    self._sweep_lifecycle()
            # Session housekeeping — prune expired/revoked rows every N ticks.
            self._session_tick += 1
            if self._session_tick >= settings.SESSION_SWEEP_EVERY_TICKS:
                self._session_tick = 0
                self._sweep_sessions()
                self._sweep_scheduled_backup()
                self._sweep_retention()
            # Quota reconciliation (C-17) — recompute tenant counters every N
            # ticks so a drifted `used_bytes` self-heals without an operator.
            if settings.QUOTA_RECONCILE_RUN_IN_WORKER:
                self._quota_tick += 1
                if self._quota_tick >= settings.QUOTA_RECONCILE_SWEEP_EVERY_TICKS:
                    self._quota_tick = 0
                    self._sweep_quota_counters()
            # Index/object reconciliation (H-07) — one bounded batch every N ticks.
            if settings.STORAGE_RECONCILE_SWEEP_EVERY_TICKS > 0:
                self._reconcile_tick += 1
                if self._reconcile_tick >= settings.STORAGE_RECONCILE_SWEEP_EVERY_TICKS:
                    self._reconcile_tick = 0
                    self._sweep_missing_objects()

    def _process_job(self, session, job: CompressionJob) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if not self._heartbeat_job(session, job.id, now):
            session.rollback()
            return
        session.commit()

        instance = session.get(Instance, job.instance_id)
        bk = getattr(instance, "storage_backend", None) if instance is not None else None
        if instance is None or not storage_service.exists(instance.file_path, backend_name=bk):
            job.status = "FAILED"
            job.error_message = "Instance object missing"
            job.finished_at = now
            job.owner_id = None
            job.lease_until = None
            return

        session.commit()
        try:
            if job.method == THUMBNAIL_METHOD:
                if settings.THUMBNAIL_ENABLED and not thumbnail_service.generate_thumbnail(
                    instance.file_path, storage_service.thumbnail_key(instance.sop_instance_uid), backend_name=bk,
                ):
                    raise RuntimeError("Thumbnail generation failed")
                if not self._heartbeat_job(session, job.id):
                    session.rollback()
                    return
                job.status = "DONE"
                job.finished_at = now
                job.owner_id = None
                job.lease_until = None
                return
            result = storage_service.compress(instance.file_path, job.method, backend_name=bk)
            # Never publish DB metadata if this worker no longer owns the lease.
            if not self._heartbeat_job(session, job.id):
                session.rollback()
                log.warning("compression_job_lease_lost job_id=%s", job.id)
                return
            instance.transfer_syntax_uid = result["transfer_syntax_uid"]
            instance.file_size_bytes = result["compressed_bytes"]
            if job.method not in {NATIVE_METHOD, SOURCE_METHOD}:
                instance.compressed_at = now
            # C-04: the stored codestream changed (native frames differ).
            content_version.bump(instance)

            job.original_size = result["original_bytes"]
            job.compressed_size = result["compressed_bytes"]
            job.ratio_pct = result["saved_percent"]
            job.status = "DONE"
            job.finished_at = now
            job.owner_id = None
            job.lease_until = None

            # Audit + thumbnail
            from app.services.audit_service import add_audit

            add_audit(
                session,
                sop_instance_uid=instance.sop_instance_uid,
                action="DECOMPRESSED" if job.method == NATIVE_METHOD else "COMPRESSED",
                performed_by="worker",
                study_instance_uid=None,
                file_path=instance.file_path,
                # Same backend the compression wrote to: hashing against the
                # default store misses an instance kept on another backend and
                # would fail a job that actually succeeded.
                backend_name=bk,
            )
            if settings.THUMBNAIL_ENABLED and not settings.THUMBNAIL_ON_RECEIPT:
                thumbnail_service.generate_thumbnail(
                    instance.file_path,
                    storage_service.thumbnail_key(instance.sop_instance_uid),
                    backend_name=bk,
                )
            log.debug("Compressed %s (%s, %.1f%%)", instance.sop_instance_uid,
                      result["method"], result["saved_percent"])
        except Exception as exc:  # noqa: BLE001 - boundary guard: compression is an isolated background job
            if not self._heartbeat_job(session, job.id):
                session.rollback()
                log.warning("compression_job_lease_lost job_id=%s", job.id)
                return
            error_message = f"Compression failed ({type(exc).__name__})"
            log.warning(
                "compression_job_failed job_id=%s error_type=%s",
                job.id, type(exc).__name__,
            )
            job.status = "FAILED"
            job.error_message = error_message
            if job.attempt < settings.WORKER_JOB_MAX_ATTEMPTS:
                job.status = "PENDING"
                job.finished_at = None
            else:
                job.finished_at = now
            job.owner_id = None
            job.lease_until = None

    def _heartbeat_job(self, session, job_id: int, now: datetime | None = None) -> bool:
        """Extend this worker's live lease with an ownership-guarded update."""
        from sqlalchemy import update

        current = now or datetime.now(timezone.utc).replace(tzinfo=None)
        result = session.execute(
            update(CompressionJob)
            .where(
                CompressionJob.id == job_id,
                CompressionJob.status == "RUNNING",
                CompressionJob.owner_id == self.owner_id,
                CompressionJob.lease_until > current,
            )
            .values(lease_until=current + timedelta(seconds=settings.WORKER_JOB_LEASE_SECONDS))
        )
        return result.rowcount == 1

    def _heartbeat_jobs_batch(
        self, session: Any, job_ids: list[int], now: datetime | None = None
    ) -> int:
        """Renew leases for all active jobs owned by this worker in one UPDATE."""
        if not job_ids:
            return 0
        from sqlalchemy import update

        current = now or datetime.now(timezone.utc).replace(tzinfo=None)
        result = session.execute(
            update(CompressionJob)
            .where(
                CompressionJob.id.in_(job_ids),
                CompressionJob.status == "RUNNING",
                CompressionJob.owner_id == self.owner_id,
                CompressionJob.lease_until > current,
            )
            .values(lease_until=current + timedelta(seconds=settings.WORKER_JOB_LEASE_SECONDS))
        )
        return int(result.rowcount or 0)

    def _shared_heartbeat_loop(self) -> None:
        """Single shared heartbeat thread renewing all active compression leases."""
        while not self._stop_event.wait(settings.WORKER_JOB_HEARTBEAT_SECONDS):
            with self._active_jobs_lock:
                job_ids = list(self._active_job_ids)
            if not job_ids:
                continue
            try:
                with SyncSessionLocal() as session:
                    self._heartbeat_jobs_batch(session, job_ids)
                    session.commit()
            except Exception as exc:  # A failed heartbeat lets the lease expire for recovery.
                log.warning(
                    "compression_job_heartbeat_failed count=%d error_type=%s",
                    len(job_ids), type(exc).__name__,
                )

    def _heartbeat_loop(self, job_id: int, stop: threading.Event) -> None:
        """Renew an active compression lease without sharing its DB session."""
        while not stop.wait(settings.WORKER_JOB_HEARTBEAT_SECONDS):
            try:
                with SyncSessionLocal() as session:
                    renewed = self._heartbeat_job(session, job_id)
                    session.commit()
                if not renewed:
                    return
            except Exception as exc:  # A failed heartbeat lets the lease expire for recovery.
                log.warning(
                    "compression_job_heartbeat_failed job_id=%s error_type=%s",
                    job_id, type(exc).__name__,
                )
                return

    # ------------------------------------------------------------------ #
    def _sweep_sessions(self) -> None:
        """Prune expired and revoked session rows (auth plan §3.2).

        Sessions that have expired, or were revoked by logout / theft
        detection / an admin, are deleted entirely — the refresh cookie they
        hold becomes invalid because no matching row exists. Runs on its own
        sync session; failures are logged and never kill the worker loop.
        """
        from sqlalchemy import delete

        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            with SyncSessionLocal() as session:
                result = session.execute(
                    delete(Session).where(
                        (Session.expires_at < now) | (Session.is_revoked.is_(True))
                    )
                )
                session.commit()
                if result.rowcount:
                    log.info("Session sweep: pruned %d row(s)", result.rowcount)
        except Exception as exc:  # noqa: BLE001 - boundary guard: a failed sweep must not stop the worker loop
            log.error("session_sweep_failed error_type=%s", type(exc).__name__)

    # ------------------------------------------------------------------ #
    def _sweep_routing(self) -> None:
        """Dispatch due Smart Router queue items (durable, retried).

        Runs on its own sync session because dispatch opens one internally;
        failures are logged and never kill the worker loop.
        """
        try:
            from app.services import routing_service

            counts = routing_service.process_queue_batch()
            if any(counts.values()):
                log.info("Routing sweep: %s", counts)
        except Exception as exc:  # noqa: BLE001 - boundary guard: a failed sweep must not stop the worker loop
            log.error("routing_sweep_failed error_type=%s", type(exc).__name__)

    # ------------------------------------------------------------------ #
    def _sweep_lifecycle(self) -> None:
        """Evaluate enabled study-lifecycle rules (archive/restore/purge).

        Runs on its own sync session because it opens one internally; failures
        are logged and never kill the worker loop.
        """
        try:
            from app.services import lifecycle_service

            counts = lifecycle_service.evaluate_and_run(performed_by="lifecycle-worker")
            if any(counts.values()):
                log.info("Lifecycle sweep: %s", counts)
        except Exception as exc:  # noqa: BLE001 - boundary guard: a failed sweep must not stop the worker loop
            log.error("lifecycle_sweep_failed error_type=%s", type(exc).__name__)

    # ------------------------------------------------------------------ #
    def _sweep_quota_counters(self) -> None:
        """Recompute every tenant's storage counters from the indexed bytes.

        The scheduled half of the C-17 fix: the write and delete paths keep
        ``storage_quotas.used_bytes`` current incrementally, and this sweep is
        what repairs it if any of them ever fails to. Idempotent — it derives the
        figure from the instance rows, never from the counter it is correcting —
        so a run that finds nothing drifted does nothing and logs nothing.

        Runs on its own sync session (the service opens one internally); a
        failure is logged and never kills the worker loop.
        """
        try:
            from app.services import quota_service

            report = quota_service.scheduled_reconcile_tenants()
            if report["reconciled"]:
                log.info(
                    "Quota reconciliation: %d row(s) corrected across %d tenant(s)",
                    report["reconciled"], report["tenants"],
                )
        except Exception as exc:  # noqa: BLE001 - boundary guard: a failed sweep must not stop the worker loop
            log.error("quota_reconciliation_failed error_type=%s", type(exc).__name__)

    # ------------------------------------------------------------------ #
    def _sweep_scheduled_backup(self) -> None:
        try:
            from app.services import db_admin

            db_admin.run_scheduled_backup_if_due()
        except Exception as exc:  # noqa: BLE001 - best-effort scheduled maintenance
            log.error("scheduled_backup_failed error_type=%s", type(exc).__name__)

    def _sweep_retention(self) -> None:
        try:
            from app.services import retention_service

            retention_service.run_retention_sweep()
        except Exception as exc:  # noqa: BLE001 - best-effort table hygiene
            log.error("retention_sweep_failed error_type=%s", type(exc).__name__)

    # ------------------------------------------------------------------ #
    def _sweep_missing_objects(self) -> None:
        """Flag index rows whose stored object is gone (H-07); never deletes."""
        try:
            from app.services import storage_reconcile_service

            storage_reconcile_service.sweep_missing_objects()
        except Exception as exc:  # noqa: BLE001 - boundary guard: a failed sweep must not stop the worker loop
            log.error("storage_reconcile_failed error_type=%s", type(exc).__name__)

    # ------------------------------------------------------------------ #
    def _process_triage(self, session) -> None:
        """Run triage detectors on series whose triage_status is PENDING.

        Idempotent and race-safe: a series is claimed (PENDING -> RUNNING)
        before analysis, so even a clean series with no findings is marked
        DONE exactly once — it is never re-analyzed — and two workers cannot
        both pick the same series.
        """
        from sqlalchemy import update as sa_update

        from app.services.redis_service import get_redis_client
        r_client = get_redis_client()
        lock = None
        if r_client:
            try:
                lock = r_client.lock("paxs:lock:worker:triage", timeout=30)
                if not lock.acquire(blocking=False):
                    return  # Another worker is claiming series right now
            except Exception as exc:
                log.debug("triage_redis_lock_acquire_skipped error_type=%s",
                          type(exc).__name__)
                lock = None

        try:
            # Claim up to 5 pending series atomically (SKIP LOCKED prevents
            # multi-worker races even when Redis is unavailable).
            pending_ids = (
                session.execute(
                    select(Series.id)
                    .where(Series.triage_status == "PENDING")
                    .order_by(Series.id)
                    .limit(5)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            if not pending_ids:
                return
            session.execute(
                sa_update(Series)
                .where(Series.id.in_(pending_ids))
                .values(triage_status="RUNNING")
            )
            session.commit()
        finally:
            if lock:
                try:
                    lock.release()
                except Exception as exc:  # noqa: BLE001 - boundary guard: the triage lock may have expired
                    # Expected when the lock TTL elapsed during a long analysis;
                    # the batch above already completed.
                    log.debug("triage_lock_release_failed error_type=%s",
                              type(exc).__name__)

        pending = (
            session.execute(
                select(Series).options()
                .where(Series.id.in_(pending_ids))
                .order_by(Series.id)
            )
            .scalars()
            .all()
        )

        instances = (
            session.execute(
                select(Instance)
                .where(Instance.series_id.in_(pending_ids))
                .order_by(Instance.instance_number.is_not(None).desc(),
                          Instance.instance_number, Instance.id)
            )
            .scalars()
            .all()
        )
        by_series: dict[int, list] = {}
        for inst in instances:
            by_series.setdefault(inst.series_id, []).append(inst)

        try:
            for series in pending:
                rows = by_series.get(series.id, [])
                if not rows:
                    series.triage_status = "DONE"
                    continue
                findings = triage_service.run_detectors_on_series(series, rows)
                # Load the study UID once per series (no per-flag lazy loads).
                study_uid = session.execute(
                    select(Study.study_instance_uid)
                    .where(Study.id == series.study_id)
                ).scalar_one()
                for f in findings:
                    session.add(TriageFlag(
                        study_instance_uid=study_uid,
                        series_instance_uid=f.series_instance_uid,
                        sop_instance_uid=f.sop_instance_uid,
                        finding=f.finding,
                        severity=f.severity,
                        confidence=f.confidence,
                        summary=f.summary,
                        detector=f.detector,
                    ))
                series.triage_status = "DONE"
        except Exception as exc:  # noqa: BLE001 - boundary guard: restore claimed series after a detector batch failure
            # Re-claim claimed series so they are retried next tick instead
            # of being stuck RUNNING forever.
            log.error(
                "triage_batch_failed error_type=%s; re-queuing claimed series",
                type(exc).__name__,
            )
            session.execute(
                sa_update(Series)
                .where(Series.id.in_(pending_ids))
                .values(triage_status="PENDING")
            )


_worker: CompressionWorker | None = None


def start_worker() -> CompressionWorker:
    global _worker
    if _worker is not None and _worker.is_alive():
        return _worker
    _recover_stuck_jobs()
    from app.services.routing_service import recover_stuck_queue_items

    recover_stuck_queue_items()
    _worker = CompressionWorker()
    _worker.start()
    return _worker


def _recover_stuck_jobs() -> None:
    """Requeue only expired compression leases; active workers keep ownership."""
    from sqlalchemy import update

    try:
        with SyncSessionLocal() as session:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            session.execute(
                update(CompressionJob)
                .where(
                    CompressionJob.status == "RUNNING",
                    CompressionJob.lease_until <= now,
                    CompressionJob.attempt < settings.WORKER_JOB_MAX_ATTEMPTS,
                )
                .values(
                    status="PENDING",
                    owner_id=None,
                    lease_until=None,
                )
                .execution_options(synchronize_session=False)
            )
            session.execute(
                update(CompressionJob)
                .where(
                    CompressionJob.status == "RUNNING",
                    CompressionJob.lease_until <= now,
                    CompressionJob.attempt >= settings.WORKER_JOB_MAX_ATTEMPTS,
                )
                .values(
                    status="FAILED",
                    owner_id=None,
                    lease_until=None,
                    finished_at=now,
                    error_message="Maximum worker attempts exceeded",
                )
                .execution_options(synchronize_session=False)
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 - boundary guard: startup recovery must not log dataset details
        log.error("stuck_job_recovery_failed error_type=%s", type(exc).__name__)


def stop_worker() -> None:
    global _worker
    if _worker is not None:
        _worker.stop()
        # Give the in-flight batch a moment to finish so its DB session
        # closes before the engine is disposed on shutdown.
        _worker.join(timeout=5)
        _worker = None


def run_standalone(poll_seconds: float | None = None, once: bool = False) -> None:
    """Run a standalone worker process (horizontal scale-out).

    Compression is CPU-bound, so N worker processes on a multi-core host scale
    transcode throughput ~linearly (each process claims its own batch via the
    atomic PENDING->RUNNING transition; the loser of a race claims nothing).
    Triage is additionally guarded by a Redis lock, so several workers can
    safely share the same DB.

    Usage:
        python -m app.services.worker                  # loop (Ctrl-C to stop)
        python -m app.services.worker --poll 2.0       # faster poll
        python -m app.services.worker --once           # one batch, then exit
        python -m app.services.worker --check-codecs   # print codec matrix, exit
    """
    # Ensure the schema exists before claiming jobs (idempotent; matches the
    # app's startup). Alembic migrations are still the production path.
    try:
        from app.db.session import sync_engine
        from app.db.models import Base

        Base.metadata.create_all(sync_engine)
    except Exception as exc:  # noqa: BLE001 - boundary guard: migrations may manage schema; keep startup behavior
        log.error(
            "worker_schema_init_failed error_type=%s; continuing",
            type(exc).__name__,
        )

    _recover_stuck_jobs()
    from app.services.routing_service import recover_stuck_queue_items

    recover_stuck_queue_items()

    worker = CompressionWorker(poll_seconds=poll_seconds)
    if once:
        from app.services import transfer_service

        worker._process_batch()
        transfer_service.process_one_pending_job()
        log.info("Standalone worker finished one batch")
        return

    worker.daemon = False  # keep the process alive until Ctrl-C
    worker.start()
    try:
        while worker.is_alive():
            worker.join(timeout=1.0)
    except KeyboardInterrupt:
        log.info("Stopping standalone worker...")
        worker.stop()
        worker.join(timeout=5)


def _check_codecs_cli() -> int:
    """Print the lossless codec availability matrix; used by --check-codecs.

    Returns 0 when the configured ``DEFAULT_COMPRESSION`` is usable by the
    compression worker, 1 otherwise (so a deploy gate can fail fast if the
    configured codec's package was never installed and it would silently
    degrade to RLE).
    """
    import sys

    # Windows consoles default to cp1252 and crash on non-ASCII output; force
    # UTF-8 (errors=replace) so the matrix always prints.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError, AttributeError) as exc:  # a console we cannot reconfigure still prints
        # Not every stream supports reconfigure() (a captured stdout, a pipe
        # wrapper). Debug: the codec matrix is still emitted below.
        log.debug("stdout_reconfigure_failed error_type=%s", type(exc).__name__)

    from app.services.compression_service import (
        COMPRESSION_METHODS,
        codec_status,
        normalize_method,
    )

    status = codec_status()
    available = {entry["method"]: entry["available"] for entry in status}

    print("PAXS lossless codec availability")
    print("=" * 72)
    for entry in status:
        mark = "OK   " if entry["available"] else "MISSING"
        note = entry["note"].replace("\u2014", "-")
        print(f"  {entry['method']:<10} {mark}  {entry['transfer_syntax_uid']:<24} {note}")

    configured = normalize_method(settings.DEFAULT_COMPRESSION)
    if configured in {NATIVE_METHOD, SOURCE_METHOD}:
        print(f"\nDEFAULT_COMPRESSION={configured}: no encoder required [OK]")
        return 0
    configured_ok = configured in COMPRESSION_METHODS and available.get(configured, False)
    if configured_ok:
        print(f"\nDEFAULT_COMPRESSION={configured}: usable [OK]")
    else:
        print(f"\nDEFAULT_COMPRESSION={configured}: UNAVAILABLE - "
              f"new ingestions will fall back to RLE")
    return 0 if configured_ok else 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="paxs-worker",
        description="PAXS background worker (compression, thumbnails, triage, routing, lifecycle, replication).",
    )
    parser.add_argument("--poll", type=float, default=None,
                        help="poll interval in seconds (default: WORKER_POLL_SECONDS)")
    parser.add_argument("--once", action="store_true",
                        help="claim and process one batch, then exit (cron-style)")
    parser.add_argument("--check-codecs", action="store_true",
                        help="print the lossless codec availability matrix and exit "
                             "(exit 0 when DEFAULT_COMPRESSION is usable, 1 otherwise)")
    args = parser.parse_args()

    import logging as _logging

    _logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.check_codecs:
        raise SystemExit(_check_codecs_cli())
    run_standalone(poll_seconds=args.poll, once=args.once)

