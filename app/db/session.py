"""Async + sync SQLAlchemy engines/sessions — PostgreSQL only.

* Async engine/session  -> FastAPI endpoints (asyncpg, one long-lived loop)
* Sync engine/session   -> the DICOM receiver, background worker and C-FIND
  handlers which run in threads (psycopg v3; pynetdicom worker threads can't
  share the async loop).

MySQL and SQLite support was retired: ``settings.DATABASE_URL`` is validated
to be a ``postgresql+asyncpg://`` URL, and the sync URL is derived from it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Coroutine
from contextlib import contextmanager
from typing import Any, TypeVar

from sqlalchemy import create_engine, text
from sqlalchemy.exc import TimeoutError as SQLAlchemyPoolTimeout
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import is_production_environment, settings
from app.db import perf

log = logging.getLogger("paxs.db")

ASYNC_PREFIX = "postgresql+asyncpg://"
SYNC_PREFIX = "postgresql+psycopg://"
_WORKER_LOCK_NAMESPACE = 0x50415853  # Separate two-key lock space from the migration lock.


@contextmanager
def try_worker_lock(job_key: int):
    """Hold an advisory lock on a dedicated PostgreSQL connection until work ends."""
    with _worker_lock_engine.connect() as conn:
        yield bool(conn.scalar(
            text("SELECT pg_try_advisory_lock(:namespace, :key)"),
            {"namespace": _WORKER_LOCK_NAMESPACE, "key": job_key},
        ))


def to_sync_url(url: str) -> str:
    """Derive the psycopg (sync) URL from the asyncpg one."""
    for prefix in (ASYNC_PREFIX, "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return SYNC_PREFIX + url[len(prefix):]
    return url


def async_pool_options(url: str) -> dict[str, object]:
    """Bounded pool settings shared by the primary and the read replica."""
    opts: dict[str, object] = {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT_SECONDS,
        "pool_pre_ping": settings.DB_POOL_PRE_PING,
        "pool_recycle": settings.DB_POOL_RECYCLE_SECONDS,
    }
    if settings.DB_PGBOUNCER_COMPAT:
        opts["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        }
    return opts


# The API lives on one long-lived loop. DIMSE/worker code uses the sync engine,
# so its fresh asyncio loops never borrow connections from this async pool.
_async_url = settings.DATABASE_URL
async_engine = create_async_engine(
    _async_url, echo=False, future=True, **async_pool_options(_async_url)
)
perf.install_slow_query_hooks(async_engine.sync_engine, "api")

AsyncSessionLocal = async_sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False
)

# C-03 / P1.2: dedicated pooled async engine for DIMSE C-STORE handlers.
# Instead of NullPool + asyncio.run() per slice (which opened and closed a fresh
# PostgreSQL TCP/TLS connection for every received image), DIMSE handlers submit
# coroutines to one long-lived background event loop thread via `run_dimse_coro`
# so connections are pooled and reused across thousands of slices without ever
# touching the FastAPI event loop.
def _dimse_pool_options() -> dict[str, object]:
    size = min(max(int(settings.DB_POOL_SIZE), int(settings.DICOM_MAX_ASSOCIATIONS)), 16)
    opts: dict[str, object] = {
        "pool_size": max(2, size),
        "max_overflow": 8,
        "pool_timeout": settings.DB_POOL_TIMEOUT_SECONDS,
        "pool_pre_ping": settings.DB_POOL_PRE_PING,
        "pool_recycle": settings.DB_POOL_RECYCLE_SECONDS,
    }
    if settings.DB_PGBOUNCER_COMPAT:
        opts["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        }
    return opts


def dimse_pool_capacity() -> int:
    """Maximum simultaneous PostgreSQL connections the DIMSE async pool may open."""
    opts = _dimse_pool_options()
    return int(opts["pool_size"]) + int(opts["max_overflow"])


dimse_async_engine = create_async_engine(
    _async_url, echo=False, future=True, **_dimse_pool_options()
)
perf.install_slow_query_hooks(dimse_async_engine.sync_engine, "dimse")

DimseAsyncSessionLocal = async_sessionmaker(
    dimse_async_engine, class_=AsyncSession, expire_on_commit=False
)


_T = TypeVar("_T")


class _DimseLoopRunner:
    """Single background event loop that owns `dimse_async_engine` connections."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                return self._loop
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._serve, name="paxs-dimse-async-loop", daemon=True
            )
            self._thread.start()
        self._ready.wait(timeout=5.0)
        assert self._loop is not None
        return self._loop

    def run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def shutdown(self) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = None
            self._thread = None
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(dimse_async_engine.dispose(), loop).result(timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - best-effort pool teardown
            log.debug("dimse_engine_dispose_failed error_type=%s", type(exc).__name__)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5.0)


_dimse_loop = _DimseLoopRunner()


def run_dimse_coro(coro: Coroutine[Any, Any, _T]) -> _T:
    """Execute an async DIMSE DB coroutine on the dedicated pooled DIMSE loop."""
    return _dimse_loop.run(coro)


def shutdown_dimse_loop() -> None:
    """Dispose `dimse_async_engine` on its owning loop and stop the loop thread."""
    _dimse_loop.shutdown()

# NullPool closes the physical connection on exit, releasing the session lock
# even on exceptions. Autocommit avoids an idle DB transaction during network I/O.
_worker_lock_engine = create_engine(
    to_sync_url(settings.DATABASE_URL), isolation_level="AUTOCOMMIT", poolclass=NullPool
)

# Sync engine: the DICOM receiver + worker run in threads and share this
# bounded QueuePool with staleness guards (pre-ping + recycle).
_sync_connect_args: dict[str, object] = (
    {"prepare_threshold": None} if settings.DB_PGBOUNCER_COMPAT else {}
)
sync_engine = create_engine(
    to_sync_url(settings.DATABASE_URL),
    echo=False,
    future=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
    poolclass=perf.ObservedQueuePool,
    connect_args=_sync_connect_args,
)
perf.install_slow_query_hooks(sync_engine, "worker")

SyncSessionLocal = sessionmaker(sync_engine, expire_on_commit=False)

# --- optional PostgreSQL read replica (Admin → DB Pool) ----------------------
# When DB_READ_REPLICA_URL is configured (a hot standby) we build a second async
# engine whose sessions serve the hottest read-only endpoints. The admin toggles
# routing at runtime (server_settings.dbp_read_replica, default OFF so
# replication lag never surprises anyone); get_read_db falls back to the
# primary transparently whenever the replica is unreachable.
_read_replica_url = settings.DB_READ_REPLICA_URL or ""
read_async_engine = (
    create_async_engine(
        _read_replica_url, echo=False, future=True,
        **async_pool_options(_read_replica_url),
    )
    if _read_replica_url
    else None
)
ReadAsyncSessionLocal = (
    async_sessionmaker(read_async_engine, class_=AsyncSession, expire_on_commit=False)
    if read_async_engine is not None
    else None
)
_REPLICA_TOGGLE_KEY = "dbp_read_replica"


def replica_configured() -> bool:
    return read_async_engine is not None


def _replica_toggle_enabled() -> bool:
    """Read the persisted admin toggle (default off). Cheap indexed lookup."""
    if read_async_engine is None:
        return False
    try:
        from sqlalchemy import select

        from app.db.models import ServerSetting

        with SyncSessionLocal() as s:
            row = s.execute(
                select(ServerSetting.value).where(ServerSetting.key == _REPLICA_TOGGLE_KEY)
            ).scalar_one_or_none()
        return bool(row and str(row) in ("1", "true", "True"))
    except Exception:  # pragma: no cover - table missing on brand-new DB
        return False


#: H-08: how long the replica toggle is trusted before it is re-read.
REPLICA_TOGGLE_TTL_SECONDS = 5.0
_replica_toggle_lock = threading.Lock()
_replica_toggle_cache: tuple[bool, float] | None = None  # (value, monotonic read time)


def reset_replica_toggle_cache() -> None:
    """Forget the cached toggle so the next read hits the DB (admin change, tests)."""
    global _replica_toggle_cache
    with _replica_toggle_lock:
        _replica_toggle_cache = None


def _cached_replica_toggle() -> bool | None:
    """The cached toggle while it is younger than the TTL, else None."""
    with _replica_toggle_lock:
        cached = _replica_toggle_cache
    if cached is None or time.monotonic() - cached[1] >= REPLICA_TOGGLE_TTL_SECONDS:
        return None
    return cached[0]


def _refresh_replica_toggle() -> bool:
    """Read the toggle from the DB (sync) and cache it."""
    global _replica_toggle_cache
    value = _replica_toggle_enabled()
    with _replica_toggle_lock:
        _replica_toggle_cache = (value, time.monotonic())
    return value


async def replica_toggle_enabled_cached() -> bool:
    """The admin replica toggle, read at most once per TTL and never on the loop (H-08).

    ``_replica_toggle_enabled`` opens a sync session; calling it from
    ``get_read_db`` blocked the event loop on every hot read. A toggle change
    now takes effect within ``REPLICA_TOGGLE_TTL_SECONDS`` (immediately in the
    process that made it — ``db_panel`` resets the cache).
    """
    cached = _cached_replica_toggle()
    if cached is not None:
        return cached
    return await asyncio.to_thread(_refresh_replica_toggle)


async def replica_probe() -> dict:
    """Connection + replication-lag probe for the panel (PostgreSQL hot standby)."""
    if read_async_engine is None:
        return {"configured": False}
    try:
        async with read_async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        return {"configured": True, "connected": False, "error": str(exc)[:200]}
    out: dict = {"configured": True, "connected": True}
    try:
        async with read_async_engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT pg_is_in_recovery() AS standby, "
                "EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp())) AS lag"
            ))).mappings().first()
        out["standby"] = bool(row and row["standby"])
        out["lag_seconds"] = (
            round(float(row["lag"]), 1) if row and row["lag"] is not None else None
        )
    except Exception as exc:  # noqa: BLE001 - boundary guard: lag is best-effort
        # `lag_seconds = None` is the documented sentinel for "not readable",
        # which the dashboard renders as "unknown" rather than "0 behind".
        log.debug("replica_lag_unavailable error_type=%s", type(exc).__name__)
        out["lag_seconds"] = None
    return out


async def get_read_db():
    """AsyncSession for read-only endpoints, routed to the replica when enabled.

    The admin toggle lives in ``server_settings`` (default off). When the
    replica is unreachable or unconfigured the dependency transparently serves
    the primary engine.
    """
    if ReadAsyncSessionLocal is not None and await replica_toggle_enabled_cached():
        replica_context = ReadAsyncSessionLocal()
        try:
            replica_session = await replica_context.__aenter__()
            # Acquire before yielding so only pre-request replica outages use
            # primary fallback. Never replay a query after handler execution.
            await replica_session.execute(text("SELECT 1"))
        except Exception as exc:  # Replica connectivity is best-effort; redact driver details.
            try:
                await replica_context.__aexit__(type(exc), exc, exc.__traceback__)
            except Exception:
                pass
            log.warning(
                "read_replica_unavailable falling_back_to_primary error_type=%s",
                type(exc).__name__,
            )
        else:
            try:
                yield replica_session
                await replica_session.commit()
            except SQLAlchemyPoolTimeout as exc:
                await replica_session.rollback()
                raise _pool_overloaded() from exc
            except Exception:
                await replica_session.rollback()
                raise
            finally:
                await replica_context.__aexit__(None, None, None)
            return
    for attempt in range(settings.DB_READ_ACQUIRE_RETRIES + 1):
        context = AsyncSessionLocal()
        session = None
        context_closed = False
        try:
            session = await context.__aenter__()
            # Acquire before yielding: only this idempotent connection checkout
            # is retried. Endpoint queries are never replayed here.
            acquire = getattr(session, "connection", None)
            if acquire is not None:
                await acquire()
            try:
                yield session
                await session.commit()
            except SQLAlchemyPoolTimeout as exc:
                try:
                    await session.rollback()
                except SQLAlchemyPoolTimeout:
                    pass
                raise _pool_overloaded() from exc
            except Exception:
                await session.rollback()
                raise
            return
        except SQLAlchemyPoolTimeout as exc:
            if not context_closed:
                await context.__aexit__(type(exc), exc, exc.__traceback__)
                context_closed = True
            if attempt >= settings.DB_READ_ACQUIRE_RETRIES:
                raise _pool_overloaded() from exc
            await asyncio.sleep(settings.DB_READ_RETRY_DELAY_SECONDS)
        finally:
            if not context_closed:
                await context.__aexit__(None, None, None)


def _pool_overloaded():
    """Construct the stable HTTP overload response without driver details."""
    from fastapi import HTTPException

    return HTTPException(
        status_code=503,
        detail="Database capacity is temporarily unavailable",
        headers={"Retry-After": "1"},
    )


async def get_db():
    """FastAPI dependency yielding an AsyncSession (auto-commit on success)."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except SQLAlchemyPoolTimeout as exc:
            try:
                await session.rollback()
            except SQLAlchemyPoolTimeout:
                pass
            raise _pool_overloaded() from exc
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Prepare the schema at startup (H11).

    Alembic is the single schema authority. A brand-new **empty** database is
    brought up by running the migration chain, so ``alembic_version`` is
    authoritative from the first boot. An existing database is only *checked*:
    drift is reported, never silently created — `create_all` would add objects
    that no migration records, which is exactly how schema and history diverged
    before. ``create_all`` is now used only by the test fixtures.
    """
    import asyncio

    from sqlalchemy import inspect

    def _state() -> tuple[bool, list[str]]:
        inspector = inspect(sync_engine)
        tables = set(inspector.get_table_names())
        # Ignore alembic's own bookkeeping table when deciding "empty".
        return (not (tables - {"alembic_version"}), [])

    empty, _ = await asyncio.to_thread(_state)
    if empty:
        # Fresh install: run the chain so the revision table is the source of
        # truth from the very first boot.
        from app.services import db_admin

        log.info("Empty database — applying migrations to head")
        await asyncio.to_thread(db_admin.run_migrations, settings.DATABASE_URL)
        log.info("Database schema ready (%s)", settings.DATABASE_URL.split("://")[0])
        return

    from app.services import db_admin

    current, heads = await asyncio.to_thread(db_admin.revision_status, settings.DATABASE_URL)
    if current != heads:
        message = (
            f"database revision {sorted(current)} != code heads {sorted(heads)}: "
            "run 'alembic upgrade head'"
        )
        if is_production_environment(settings):
            raise RuntimeError(message)
        log.error(message)

    missing = await asyncio.to_thread(db_admin.schema_drift, settings.DATABASE_URL)
    if missing:
        message = (
            "Schema drift detected (%d object(s) present in the models but not in "
            "the database): %s — run 'alembic upgrade head' to reconcile; startup "
            "will NOT create them implicitly."
        )
        if is_production_environment(settings):
            raise RuntimeError(message % (len(missing), ", ".join(missing[:10])))
        log.error(message, len(missing), ", ".join(missing[:10]))
    else:
        log.info("Database schema verified (%s)", settings.DATABASE_URL.split("://")[0])


async def close_db() -> None:
    """Close all engines (call on shutdown so PostgreSQL connections close cleanly)."""
    await async_engine.dispose()
    await asyncio.to_thread(shutdown_dimse_loop)
    await dimse_async_engine.dispose()
    if read_async_engine is not None:
        await read_async_engine.dispose()
    sync_engine.dispose()
    log.debug("Async + sync engines disposed")
