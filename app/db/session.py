"""Async + sync SQLAlchemy engines/sessions.

* Async engine/session  -> FastAPI endpoints (single async event loop)
* Sync engine/session   -> the DICOM receiver, background worker and C-FIND
  handlers which run in threads (pynetdicom worker threads can't share the
  async loop).

sqlite gets WAL + busy_timeout so the receiver, API and worker can coexist.

The journal mode is configurable via PAXS_SQLITE_JOURNAL ("wal"/"delete") —
WAL is the default, but network filesystems / some SANs misbehave with it and
need the classic rollback journal.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db import perf

log = logging.getLogger("paxs.db")

if sys.platform == "win32" and sys.version_info < (3, 14):
    # aiomysql (a pure-asyncio MySQL client) historically crashed against the
    # default Proactor event loop on Windows; the Selector loop was the
    # standard fix. Python 3.14+ unified the Windows event loop and deprecates
    # the policy system entirely (removal planned in 3.16), so we only apply
    # the legacy policy on older interpreters where it is still needed.
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:  # pragma: no cover - very old/new interpreters
        log.warning("Could not set Windows selector event loop policy", exc_info=True)


def to_sync_url(url: str) -> str:
    """Derive a sync driver URL from an async one."""
    if url.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + url[len("sqlite+aiosqlite://"):]
    if url.startswith("mysql+aiomysql://"):
        return "mysql+pymysql://" + url[len("mysql+aiomysql://"):]
    return url


def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute(
        f"PRAGMA journal_mode={getattr(settings, 'SQLITE_JOURNAL_MODE', 'WAL')}"
    )
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
_is_mysql = settings.DATABASE_URL.startswith("mysql")


def _ensure_charset(url: str) -> str:
    """Append utf8mb4 to a MySQL URL that doesn't already declare a charset.

    aiomysql/pymysql default to latin1 when the server charset isn't pinned;
    DICOM patient names routinely carry non-ASCII characters (accents, Arabic
    names) which would raise on INSERT. The engine-level default below makes
    utf8mb4 unconditional.
    """
    if not _is_mysql or "charset=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "charset=utf8mb4"


# NullPool: never reuse connections across event loops. The DICOM receiver
# runs DB work in a fresh `asyncio.run()` per image and the test suite spins
# up a new loop per TestClient — a pooled aiomysql connection would bind its
# awaits to the wrong loop and crash. One connection per request is cheap.
_async_url = _ensure_charset(settings.DATABASE_URL)
async_engine = create_async_engine(
    _async_url, echo=False, future=True, poolclass=NullPool
)
if _is_sqlite:
    event.listen(async_engine.sync_engine, "connect", _set_sqlite_pragma)
perf.install_slow_query_hooks(async_engine.sync_engine, "api")

AsyncSessionLocal = async_sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False
)

# Sync engine: the DICOM receiver + worker run in threads and share this
# pool, so use a bounded QueuePool with MySQL-staleness guards (pre-ping +
# recycle). SQLite keeps its pragma listener and no pool tuning needed.
sync_engine = create_engine(
    to_sync_url(_ensure_charset(settings.DATABASE_URL)),
    echo=False,
    future=True,
    **(
        {
            "pool_size": settings.DB_POOL_SIZE,
            "max_overflow": settings.DB_MAX_OVERFLOW,
            "pool_pre_ping": settings.DB_POOL_PRE_PING,
            "pool_recycle": settings.DB_POOL_RECYCLE_SECONDS,
        }
        if _is_mysql
        else {}
    ),
)
if _is_sqlite:
    event.listen(sync_engine, "connect", _set_sqlite_pragma)
perf.install_slow_query_hooks(sync_engine, "worker")

SyncSessionLocal = sessionmaker(sync_engine, expire_on_commit=False)

# --- optional MySQL read replica (Admin → DB Pool) ---------------------------
# When DB_READ_REPLICA_URL is configured we build a second async engine whose
# sessions serve the hottest read-only endpoints. The admin toggles routing
# at runtime (server_settings.dbp_read_replica, default OFF so replication lag
# never surprises anyone); get_read_db falls back to the primary transparently
# whenever the replica is unreachable.
_read_replica_url = (
    _ensure_charset(settings.DB_READ_REPLICA_URL)
    if settings.DB_READ_REPLICA_URL
    else ""
)
read_async_engine = (
    create_async_engine(_read_replica_url, echo=False, future=True, poolclass=NullPool)
    if _read_replica_url.startswith(("mysql", "sqlite"))
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


async def replica_probe() -> dict:
    """Connection + replication-lag probe for the panel (MySQL replica only)."""
    if read_async_engine is None:
        return {"configured": False}
    try:
        async with read_async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        return {"configured": True, "connected": False, "error": str(exc)[:200]}
    out: dict = {"configured": True, "connected": True}
    if _read_replica_url.startswith("mysql"):
        try:
            async with read_async_engine.connect() as conn:
                rows = (await conn.execute(
                    text("SHOW REPLICA STATUS")
                )).mappings().all()
                if not rows:  # MariaDB / older MySQL
                    rows = (await conn.execute(
                        text("SHOW SLAVE STATUS")
                    )).mappings().all()
            if rows:
                r = rows[0]
                out["io_running"] = r.get("Replica_IO_Running") or r.get("Slave_IO_Running")
                out["sql_running"] = r.get("Replica_SQL_Running") or r.get("Slave_SQL_Running")
                out["lag_seconds"] = r.get("Seconds_Behind_Source") or r.get("Seconds_Behind_Master")
        except Exception:  # pragma: no cover - privileges may be restricted
            out["lag_seconds"] = None
    return out


async def get_read_db():
    """AsyncSession for read-only endpoints, routed to the replica when enabled.

    The admin toggle lives in ``server_settings`` (default off). When the
    replica is unreachable or unconfigured the dependency transparently serves
    the primary engine.
    """
    if ReadAsyncSessionLocal is not None and _replica_toggle_enabled():
        try:
            async with ReadAsyncSessionLocal() as session:
                # Ping before yielding: AsyncSession connects lazily, so a down
                # replica would otherwise fail on the endpoint's first query
                # (outside this try) and the client would see a 500 instead of
                # the transparent primary fallback.
                await session.execute(text("SELECT 1"))
                yield session
                return
        except Exception:
            log.warning("Read replica unavailable — falling back to primary", exc_info=True)
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db():
    """FastAPI dependency yielding an AsyncSession (auto-commit on success)."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
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

    missing = await asyncio.to_thread(db_admin.schema_drift, settings.DATABASE_URL)
    if missing:
        log.warning(
            "Schema drift detected (%d object(s) present in the models but not in "
            "the database): %s — run 'alembic upgrade head' to reconcile; startup "
            "will NOT create them implicitly.",
            len(missing),
            ", ".join(missing[:10]),
        )
    else:
        log.info("Database schema verified (%s)", settings.DATABASE_URL.split("://")[0])


async def close_db() -> None:
    """Close all engines (call on shutdown so MySQL disconnects cleanly)."""
    await async_engine.dispose()
    if read_async_engine is not None:
        await read_async_engine.dispose()
    sync_engine.dispose()
    log.debug("Async + sync engines disposed")
