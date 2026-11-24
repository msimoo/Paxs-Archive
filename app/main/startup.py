"""Application lifespan: schema, workers, DIMSE/HL7 listeners, storage sweep.

Everything that starts a thread, opens a socket, or holds a lock is torn down
here. A listener that outlives the lifespan keeps ``:10400`` bound and the next
test run fails with "address already in use", so shutdown is not optional.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.core.config import settings
from app.db.session import close_db, init_db, read_async_engine, sync_engine
from app.dicom.hl7 import shutdown_hl7, start_hl7_listener
from app.dicom.receiver import shutdown_receiver, start_receiver_in_thread
from app.services.settings_service import apply_db_overrides
from app.services.worker import start_worker, stop_worker

log = logging.getLogger("paxs.main")


def verify_shipped_viewer_integrity(viewer_directory: Path | None = None) -> list[str]:
    """Check shipped viewer hashes; production fails closed, development warns."""
    from app.core.config import is_production_environment
    from app.core.viewer_integrity import enforce_viewer_manifest

    directory = viewer_directory or Path(__file__).resolve().parents[1] / "static" / "viewer"
    production = is_production_environment(settings)
    try:
        errors = enforce_viewer_manifest(
            directory,
            production=production,
            strict_development=settings.VIEWER_MANIFEST_STRICT_DEVELOPMENT,
        )
    except RuntimeError:
        log.critical("viewer_asset_integrity_failed issues_detected")
        raise
    if errors:
        log.warning("viewer_asset_integrity_degraded issue_count=%d", len(errors))
    else:
        log.info("viewer_asset_integrity_ok")
    return errors



async def _sweep_stale_storage_once() -> None:
    """Run one bounded storage sweep without blocking the event loop."""
    from app.services import storage_service

    try:
        removed = await asyncio.to_thread(
            storage_service.purge_stale_temp_files,
            None,
            settings.STORAGE_STALE_THRESHOLD_SECONDS,
        )
        if removed:
            log.info("Storage sweep removed %d stale staged file(s)", removed)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # Best-effort background maintenance; omit backend details.
        log.warning("storage_sweep_failed error_type=%s", type(exc).__name__)


async def _storage_sweep_loop() -> None:
    """Periodically purge abandoned staged objects (H09).

    A crash between staging and publishing leaves a `.tmp_*` file behind. The
    sweeper only ever removes staged temporary files older than
    ``STORAGE_STALE_THRESHOLD_SECONDS`` — a published object is never a
    candidate — so this cannot damage the archive.

    08-3: the period and the age threshold are settings, not literals. They are
    separate on purpose — a test, or an operator chasing disk pressure, wants a
    fast sweep without also declaring a five-minute-old in-flight upload
    abandoned. Both are re-read every iteration so an admin override applies
    without a restart, and ``STORAGE_SWEEP_INTERVAL_SECONDS <= 0`` disables the
    sweep outright (AGENTS.md N-5: 0 means off, never "use the default").
    """
    while True:
        interval = settings.STORAGE_SWEEP_INTERVAL_SECONDS
        if interval <= 0:
            # One guard covers both cases: disabled at boot (the first iteration
            # returns before any sweep) and switched off by a runtime override
            # (the next iteration returns). N-5: 0 means off — this must never
            # fall back to a default period.
            log.info(
                "Storage sweep disabled (STORAGE_SWEEP_INTERVAL_SECONDS=%d)", interval
            )
            return
        await _sweep_stale_storage_once()
        await asyncio.sleep(interval)


async def _event_loop_lag_loop() -> None:
    """Sample event-loop scheduling delay without blocking the loop."""
    from app.core.observability import record_event_loop_lag
    from app.core.config import settings

    loop = asyncio.get_running_loop()
    interval = max(0.0, float(settings.METRICS_EVENT_LOOP_SAMPLE_SECONDS))
    if interval == 0:
        return
    deadline = loop.time() + interval
    while True:
        await asyncio.sleep(max(0.0, deadline - loop.time()))
        now = loop.time()
        record_event_loop_lag(now - deadline)
        deadline = now + interval


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Widen the default executor before anything starts serving requests —
    # WADO-RS frame/rendered/thumbnail retrieval offloads disk I/O + pydicom
    # decode via asyncio.to_thread(), and asyncio's built-in default pool
    # (min(32, cpu+4)) is too small for a CT/MRI viewer firing dozens of
    # concurrent frame requests per series (see app/api/v1/dicomweb.py).
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(
            max_workers=settings.IO_THREAD_POOL_WORKERS,
            thread_name_prefix="paxs-io",
        )
    )
    # H06: fail closed before serving anything if the configuration is
    # insecure in production (default JWT secret, insecure cookies, ...).
    from app.core.config import validate_memory_budgets, validate_production_settings

    validate_production_settings(settings)
    # Cache memory budgets (V1-2 / V3-6) are tuning, not security, so an
    # incoherent combination is reported rather than fatal — a server that
    # refuses to boot over a cache size would be worse than one that tells you
    # the cache will thrash.
    for _warning in validate_memory_budgets(settings):
        log.warning("cache budget: %s", _warning)
    await init_db()
    # Replay persisted admin-settings overrides before any service binds, so
    # boot-consumed keys (AE title, ports, toggles) honor the last edit.
    apply_db_overrides()
    # B-22: the gate above ran against the *environment* configuration, before
    # any stored override existed in memory. `apply_db_overrides` validates its
    # own candidate, but re-running the gate on the effective configuration
    # makes the invariant explicit and independent of that function's internals:
    # whatever this process is about to serve must satisfy the production rules,
    # no matter which layer supplied the values.
    validate_production_settings(settings)
    verify_shipped_viewer_integrity()
    # PostgreSQL: every engine is pooled, so always report the connection budget.
    from app.db.session import async_engine, dimse_pool_capacity

    primary_capacity = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    replica_capacity = (
        primary_capacity
        if read_async_engine is not None
        else 0
    )
    dimse_capacity = dimse_pool_capacity() if settings.DICOM_RECEIVER_ENABLED else 0
    per_process_capacity = (
        primary_capacity + replica_capacity + primary_capacity + dimse_capacity
    )
    configured_capacity = per_process_capacity * settings.APP_WORKERS
    log.info(
        "db_pool_config async_class=%s sync_class=%s pool_size=%d max_overflow=%d "
        "dimse_pool_capacity=%d pool_timeout_seconds=%s pre_ping=%s recycle_seconds=%d "
        "app_workers=%d replica_pool=%s configured_max_connections=%d",
        type(async_engine.sync_engine.pool).__name__,
        type(sync_engine.pool).__name__,
        settings.DB_POOL_SIZE,
        settings.DB_MAX_OVERFLOW,
        dimse_capacity,
        settings.DB_POOL_TIMEOUT_SECONDS,
        settings.DB_POOL_PRE_PING,
        settings.DB_POOL_RECYCLE_SECONDS,
        settings.APP_WORKERS,
        bool(replica_capacity),
        configured_capacity,
    )
    if settings.DB_MAX_CONNECTIONS and configured_capacity > settings.DB_MAX_CONNECTIONS:
        log.warning(
            "db_pool_capacity_exceeds_configured_ceiling configured=%d ceiling=%d",
            configured_capacity,
            settings.DB_MAX_CONNECTIONS,
        )
    if settings.WORKER_ENABLED:
        start_worker()
    if settings.DICOM_RECEIVER_ENABLED:
        start_receiver_in_thread()
    if settings.HL7_ENABLED:
        start_hl7_listener()
    from app.core.licensing import get_license_manager
    _lic = get_license_manager()
    log.info("PAXS License: %s (%s) — Server HW-FP: %s",
             _lic.status.value, _lic.status_message, _lic.get_summary().get("server_fingerprint"))

    async def _heartbeat_loop():
        while True:
            await asyncio.sleep(max(3600, settings.LICENSE_HEARTBEAT_HOURS * 3600))
            try:
                await asyncio.to_thread(_lic.validate_online)
            except Exception as exc:  # noqa: BLE001 - boundary guard: heartbeat is best-effort
                log.warning("License background heartbeat error (error_type=%s)", type(exc).__name__)

    _hb_task = (
        asyncio.create_task(_heartbeat_loop())
        if settings.LICENSE_AUTO_VALIDATE_ONLINE
        else None
    )
    # H09: sweep abandoned `.tmp_*` staged files (hourly by default; see
    # STORAGE_SWEEP_INTERVAL_SECONDS / STORAGE_STALE_THRESHOLD_SECONDS).
    _sweep_task = asyncio.create_task(_storage_sweep_loop())
    _lag_task = asyncio.create_task(_event_loop_lag_loop())

    log.info("%s v%s ready — API http://0.0.0.0  DICOM %s:%s%s",
             settings.APP_NAME, settings.APP_VERSION,
             settings.DICOM_HOST, settings.DICOM_PORT,
             f"  HL7 {settings.HL7_HOST}:{settings.HL7_PORT}" if settings.HL7_ENABLED else "")
    yield
    if _hb_task:
        _hb_task.cancel()
    _sweep_task.cancel()
    _lag_task.cancel()
    shutdown_receiver()
    stop_worker()
    shutdown_hl7()
    await close_db()
