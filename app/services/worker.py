"""Background worker for lossless compression + thumbnail generation.

Runs in a daemon thread and consumes the `compression_jobs` table — no Redis
or Celery required for a single-node deployment. Celery can be swapped in for
horizontal scale (see docs/IMPLEMENTATION-PLAN.md Phase 2.8).

Uses the sync engine because it lives in a non-async thread.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import settings
from app.db.models import CompressionJob, Instance, Series, Session, Study, TriageFlag
from app.db.session import SyncSessionLocal
from app.services import storage_service, thumbnail_service
from app.services import triage_service
from app.services.compression_service import (
    NATIVE_METHOD, SOURCE_METHOD, THUMBNAIL_METHOD, compression_window_open,
)

log = logging.getLogger("paxs.worker")


class CompressionWorker(threading.Thread):
    def __init__(self, poll_seconds: float | None = None) -> None:
        super().__init__(name="paxs-compression-worker", daemon=True)
        self.poll_seconds = poll_seconds or settings.WORKER_POLL_SECONDS
        self._stop = threading.Event()
        self._tick = 0
        self._routing_tick = 0
        self._replication_tick = 0
        self._session_tick = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("Compression worker started (poll %.1fs)", self.poll_seconds)
        while not self._stop.is_set():
            try:
                self._process_batch()
            except Exception:
                log.exception("Worker batch error")
            self._stop.wait(self.poll_seconds)
        log.info("Compression worker stopped")

    # ------------------------------------------------------------------ #
    def _claim_jobs(self, session, limit: int | None = None) -> list[CompressionJob]:
        """Atomically claim up to `limit` PENDING compression jobs.

        Claiming is a single guarded UPDATE (status='PENDING' -> 'RUNNING')
        restricted to the first `limit` pending ids. With several standalone
        worker processes sharing one MySQL, each row is claimed by exactly one
        worker: concurrent UPDATEs serialize on the row lock, and the loser's
        statement matches 0 rows. rowcount tells us how many we won, and we
        re-select exactly those (still RUNNING) rows to process.
        """
        from sqlalchemy import update as sa_update

        limit = limit or settings.WORKER_BATCH_SIZE
        eligible = CompressionJob.status == "PENDING"
        if not compression_window_open(
            settings.COMPRESSION_TIMING, settings.COMPRESSION_START_TIME,
            settings.COMPRESSION_END_TIME, datetime.now().strftime("%H:%M"),
        ):
            eligible &= CompressionJob.method == THUMBNAIL_METHOD
        ids = (
            session.execute(
                select(CompressionJob.id)
                .where(eligible)
                .order_by(CompressionJob.id)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        if not ids:
            return []
        res = session.execute(
            sa_update(CompressionJob)
            .where(CompressionJob.id.in_(ids), eligible)
            .values(status="RUNNING")
        )
        session.commit()  # make the claim visible to other workers immediately
        if res.rowcount == 0:
            return []
        return (
            session.execute(
                select(CompressionJob)
                .where(CompressionJob.id.in_(ids), CompressionJob.status == "RUNNING")
                .order_by(CompressionJob.id)
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------------ #
    def _process_batch(self) -> None:
        with SyncSessionLocal() as session:
            for _ in range(settings.WORKER_BATCH_SIZE):
                if self._stop.is_set():
                    break
                jobs = self._claim_jobs(session, limit=1)
                if not jobs:
                    break
                self._process_job(session, jobs[0])
                session.commit()
            if settings.TRIAGE_RUN_IN_WORKER:
                self._process_triage(session)
                session.commit()
            # Lifecycle rule sweep — every N ticks to bound DB scans.
            if settings.LIFECYCLE_RUN_IN_WORKER:
                self._tick += 1
                if self._tick >= settings.LIFECYCLE_SWEEP_EVERY_TICKS:
                    self._tick = 0
                    self._sweep_lifecycle()
            # Smart Router send queue — every N ticks, on its own session.
            if settings.ROUTING_RUN_IN_WORKER:
                self._routing_tick += 1
                if self._routing_tick >= settings.ROUTING_SWEEP_EVERY_TICKS:
                    self._routing_tick = 0
                    self._sweep_routing()
            # DR replication — every N ticks (roadmap #49).
            if settings.DR_REPLICATION_ENABLED:
                self._replication_tick += 1
                if self._replication_tick >= settings.DR_REPLICATION_SWEEP_EVERY_TICKS:
                    self._replication_tick = 0
                    try:
                        from app.services import replication_service

                        replication_service.sweep()
                    except Exception:
                        log.exception("DR replication sweep failed")
            # Session housekeeping — prune expired/revoked rows every N ticks.
            self._session_tick += 1
            if self._session_tick >= settings.SESSION_SWEEP_EVERY_TICKS:
                self._session_tick = 0
                self._sweep_sessions()

    def _process_job(self, session, job: CompressionJob) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        job.status = "RUNNING"
        session.flush()

        instance = session.get(Instance, job.instance_id)
        bk = getattr(instance, "storage_backend", None) if instance is not None else None
        if instance is None or not storage_service.exists(instance.file_path, backend_name=bk):
            job.status = "FAILED"
            job.error_message = "Instance object missing"
            job.finished_at = now
            return

        try:
            if job.method == THUMBNAIL_METHOD:
                if settings.THUMBNAIL_ENABLED and not thumbnail_service.generate_thumbnail(
                    instance.file_path, storage_service.thumbnail_key(instance.sop_instance_uid), backend_name=bk,
                ):
                    raise RuntimeError("Thumbnail generation failed")
                job.status = "DONE"
                job.finished_at = now
                return
            result = storage_service.compress(instance.file_path, job.method, backend_name=bk)
            instance.transfer_syntax_uid = result["transfer_syntax_uid"]
            instance.file_size_bytes = result["compressed_bytes"]
            if job.method not in {NATIVE_METHOD, SOURCE_METHOD}:
                instance.compressed_at = now

            job.original_size = result["original_bytes"]
            job.compressed_size = result["compressed_bytes"]
            job.ratio_pct = result["saved_percent"]
            job.status = "DONE"
            job.finished_at = now

            # Audit + thumbnail
            from app.services.audit_service import add_audit

            add_audit(
                session,
                sop_instance_uid=instance.sop_instance_uid,
                action="DECOMPRESSED" if job.method == NATIVE_METHOD else "COMPRESSED",
                performed_by="worker",
                study_instance_uid=None,
                file_path=instance.file_path,
            )
            if settings.THUMBNAIL_ENABLED and not settings.THUMBNAIL_ON_RECEIPT:
                thumbnail_service.generate_thumbnail(
                    instance.file_path,
                    storage_service.thumbnail_key(instance.sop_instance_uid),
                    backend_name=bk,
                )
            log.debug("Compressed %s (%s, %.1f%%)", instance.sop_instance_uid,
                      result["method"], result["saved_percent"])
        except Exception as exc:
            log.warning("Compression job %s failed: %s", job.id, exc)
            job.status = "FAILED"
            job.error_message = str(exc)[:1000]
            job.finished_at = now

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
        except Exception:
            log.exception("Session sweep failed")

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
        except Exception:
            log.exception("Routing sweep failed")

    # ------------------------------------------------------------------ #
    def _sweep_lifecycle(self) -> None:
        """Evaluate enabled study-lifecycle rules (archive/restore/purge).

        Runs on its own sync session because it opens one internally; failures
        are logged and never kill the worker loop.
        """
        try:
            from app.services import lifecycle_service

            counts = lifecycle_service.evaluate_and_run("lifecycle-worker")
            if any(counts.values()):
                log.info("Lifecycle sweep: %s", counts)
        except Exception:
            log.exception("Lifecycle sweep failed")

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
                log.debug("Redis lock acquire skipped: %s", exc)
                lock = None

        try:
            # Claim up to 5 pending series atomically (guards multi-worker races).
            pending_ids = (
                session.execute(
                    select(Series.id)
                    .where(Series.triage_status == "PENDING")
                    .order_by(Series.id)
                    .limit(5)
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
                except Exception:
                    pass

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
                    log.info("TRIAGE %s %s on %s (%.0f%%)",
                             f.severity, f.finding, study_uid, f.confidence * 100)
                series.triage_status = "DONE"
        except Exception:
            # Re-claim claimed series so they are retried next tick instead
            # of being stuck RUNNING forever.
            log.exception("Triage batch failed — re-queuing claimed series")
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
    """Jobs left RUNNING by a crashed worker get retried on restart."""
    from sqlalchemy import update

    try:
        with SyncSessionLocal() as session:
            session.execute(
                update(CompressionJob)
                .where(CompressionJob.status == "RUNNING")
                .values(status="PENDING")
            )
            # Series claimed by a crashed triage batch get re-queued too.
            session.execute(
                update(Series)
                .where(Series.triage_status == "RUNNING")
                .values(triage_status="PENDING")
            )
            session.commit()
    except Exception:
        log.exception("Failed to recover stuck compression jobs")


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
    except Exception:
        log.exception("Schema init failed — continuing (migrations may manage it)")

    _recover_stuck_jobs()
    from app.services.routing_service import recover_stuck_queue_items

    recover_stuck_queue_items()

    worker = CompressionWorker(poll_seconds=poll_seconds)
    if once:
        worker._process_batch()
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
    except Exception:
        pass

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
