"""Admin → DB Pool panel service.

Composes everything the panel renders:

* live SQLAlchemy pool metrics (sync QueuePool shared by the receiver/worker)
* PostgreSQL live counters (pg_stat_activity / pg_stat_database)
* read-replica status + the persisted routing toggle
* the slow-query log ring buffer + its persisted toggle/threshold
* the EXPLAIN analyzer for the hottest archive queries

Endpoints call these via ``asyncio.to_thread`` (all of it is synchronous
except the async replica probe).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db import perf
from app.db.session import (
    SyncSessionLocal,
    async_engine,
    replica_configured,
    replica_probe,
    sync_engine,
    to_sync_url,
)
from app.services import db_admin

log = logging.getLogger("paxs.db_panel")

_REPLICA_TOGGLE_KEY = "dbp_read_replica"
_SLOW_TOGGLE_KEY = "dbp_slow_query"
_SLOW_THRESHOLD_KEY = "dbp_slow_threshold_ms"


def _get_setting(key: str) -> str | None:
    from app.db.models import ServerSetting

    with SyncSessionLocal() as s:
        row = s.execute(
            select(ServerSetting.value).where(ServerSetting.key == key)
        ).scalar_one_or_none()
    return str(row) if row is not None else None


def _set_setting(key: str, value: str) -> None:
    from app.db.models import ServerSetting

    with SyncSessionLocal() as session:
        row = session.execute(
            select(ServerSetting).where(ServerSetting.key == key)
        ).scalar_one_or_none()
        if row is None:
            session.add(ServerSetting(key=key, value=value))
        else:
            row.value = value
        session.commit()


# --------------------------------------------------------------------------- #
# PostgreSQL live counters
# --------------------------------------------------------------------------- #

def _postgres_counters() -> dict | None:
    """Server-side connection / activity counters for the panel (best-effort)."""
    p = db_admin.parse_db_url(settings.DATABASE_URL)
    if p["backend"] != "postgresql":
        return None
    import psycopg

    try:
        conn = psycopg.connect(
            host=p["host"], port=p["port"] or 5432, user=p["user"],
            password=p["password"] or "", dbname=p["database"] or "postgres",
            connect_timeout=3,
        )
    except Exception as exc:  # pragma: no cover - DB briefly down
        # Driver messages can carry host/user details; report the type only.
        return {"ok": False, "error": f"connection failed ({type(exc).__name__})"}
    try:
        activity = conn.execute(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE state = 'active') AS active, "
            "count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_tx "
            "FROM pg_stat_activity WHERE datname = current_database()"
        ).fetchone()
        stats = conn.execute(
            "SELECT xact_commit, xact_rollback, blks_hit, blks_read, deadlocks, "
            "pg_database_size(current_database()) "
            "FROM pg_stat_database WHERE datname = current_database()"
        ).fetchone()
        max_conn = conn.execute("SHOW max_connections").fetchone()[0]
        uptime = conn.execute(
            "SELECT EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))"
        ).fetchone()[0]
        hit, read = int(stats[2] or 0), int(stats[3] or 0)
        return {
            "ok": True,
            "connections": int(activity[0]),
            "active": int(activity[1]),
            "idle_in_transaction": int(activity[2]),
            "max_connections": int(max_conn),
            "commits": int(stats[0] or 0),
            "rollbacks": int(stats[1] or 0),
            "deadlocks": int(stats[4] or 0),
            "cache_hit_ratio": round(hit / (hit + read), 4) if (hit + read) else None,
            "database_bytes": int(stats[5] or 0),
            "uptime_seconds": int(uptime or 0),
        }
    except Exception as exc:  # pragma: no cover - defensive (e.g. missing pg_stat privileges)
        return {"ok": False, "error": f"counters unavailable ({type(exc).__name__})"}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# EXPLAIN analyzer
# --------------------------------------------------------------------------- #

# Representative SQL for the hottest archive query paths, with literal bind
# values so the diagnostic runs as-is on PostgreSQL. These
# mirror the real ORM statements (studies list, search, series/instance
# traversal, worklist, VNA composite path, reading UNREAD).
HOT_QUERIES = [
    {
        "name": "dashboard_studies",
        "label": "Studies list (date desc, LIMIT 20) — the dashboard / Studies tab",
        "sql": (
            "SELECT studies.* FROM studies JOIN patients ON patients.id = studies.patient_id "
            "ORDER BY studies.study_date DESC, studies.id DESC LIMIT 20"
        ),
    },
    {
        "name": "studies_search",
        "label": "Studies search — patient name / MRN / description ILIKE",
        "sql": (
            "SELECT studies.* FROM studies JOIN patients ON patients.id = studies.patient_id "
            "WHERE patients.patient_name ILIKE '%ahmed%' OR patients.patient_id ILIKE '%123%' "
            "OR studies.study_description ILIKE '%chest%' "
            "ORDER BY studies.study_date DESC LIMIT 20"
        ),
    },
    {
        "name": "series_by_study",
        "label": "Series of a study (viewer traversal)",
        "sql": (
            "SELECT * FROM series WHERE study_id = 1 "
            "ORDER BY series_number IS NULL, series_number, id"
        ),
    },
    {
        "name": "instances_by_series",
        "label": "Instances of a series (frame navigation)",
        "sql": (
            "SELECT * FROM instances WHERE series_id = 1 "
            "ORDER BY instance_number IS NULL, instance_number, id"
        ),
    },
    {
        "name": "worklist_scheduled",
        "label": "MWL — scheduled procedure steps by status",
        "sql": (
            "SELECT * FROM scheduled_procedure_steps WHERE status = 'SCHEDULED' "
            "ORDER BY start_date DESC, id DESC"
        ),
    },
    {
        "name": "studies_tenant_created",
        "label": "VNA — studies of a tenant by creation time",
        "sql": (
            "SELECT * FROM studies WHERE tenant_id = 1 "
            "ORDER BY created_at DESC, id DESC LIMIT 20"
        ),
    },
    {
        "name": "reading_unread",
        "label": "Reading worklist — UNREAD (no report, not read)",
        "sql": (
            "SELECT studies.* FROM studies "
            "JOIN patients ON patients.id = studies.patient_id "
            "LEFT JOIN radiology_reports ON radiology_reports.study_id = studies.id "
            "WHERE radiology_reports.id IS NULL AND studies.read_at IS NULL "
            "ORDER BY studies.study_date DESC LIMIT 25"
        ),
    },
    {
        "name": "counts_by_modality",
        "label": "Series counts grouped by modality",
        "sql": "SELECT modality, COUNT(*) AS n FROM series GROUP BY modality ORDER BY n DESC",
    },
]


def _hot_query_names() -> list[str]:
    return [q["name"] for q in HOT_QUERIES]


def explain_query(name: str = "", custom_sql: str = "") -> dict:
    """Run EXPLAIN for a named hot query (or a read-only custom statement).

    PostgreSQL ``EXPLAIN`` (planner output), inside a READ ONLY transaction.
    A throwaway NullPool engine keeps this off the app's live pools.
    """
    sql = ""
    if name:
        hit = next((q for q in HOT_QUERIES if q["name"] == name), None)
        if hit is None:
            raise ValueError(f"Unknown hot query '{name}' — pick one of {_hot_query_names()}")
        sql = hit["sql"]
    elif custom_sql.strip():
        sql = custom_sql.strip()
    else:
        raise ValueError("Provide a hot-query name or a custom read-only SQL statement")
    # Normalize: permit at most one trailing semicolon, nothing else.
    sql = sql.rstrip()
    if sql.endswith(";"):
        sql = sql[:-1]
    if ";" in sql:
        raise ValueError("Only a single statement is allowed")
    # Read-only guard — checked on the *cleaned* statement so "WITH … DELETE"
    # (a valid CTE-backed write on PostgreSQL) can't slip through. The plan also
    # runs inside a READ ONLY transaction, so even a SELECT that calls a writing
    # function is refused by the server.
    stripped = sql.lstrip().upper()
    if not (stripped.startswith("SELECT") or stripped.startswith("EXPLAIN")):
        raise ValueError("Only read-only statements (SELECT / EXPLAIN) are allowed")

    url = to_sync_url(settings.DATABASE_URL)
    eng = create_engine(url, poolclass=NullPool)
    try:
        with eng.connect() as conn:
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            if stripped.startswith("EXPLAIN"):
                raise ValueError("Submit the SELECT only — the analyzer adds EXPLAIN itself")
            rows = conn.execute(text(f"EXPLAIN {sql}")).mappings().all()
            conn.rollback()
        return {
            "backend": settings.DATABASE_URL.split("://")[0],
            "name": name or "custom",
            "sql": " ".join(sql.split()),
            "columns": list(rows[0].keys()) if rows else [],
            "rows": [dict(r) for r in rows],
        }
    finally:
        eng.dispose()


# --------------------------------------------------------------------------- #
# panel payload
# --------------------------------------------------------------------------- #

async def perf_panel() -> dict:
    """Everything the Admin → DB Pool panel needs in one call."""
    url = settings.DATABASE_URL
    p = db_admin.parse_db_url(url)
    slow_cfg = perf.slow_query_state()
    # Re-apply persisted overrides to the runtime module state: the hooks keep
    # their in-process defaults across a restart, so without this a stored
    # "enabled" toggle would look on in the panel but capture nothing.
    stored_enabled = _get_setting(_SLOW_TOGGLE_KEY)
    stored_threshold = _get_setting(_SLOW_THRESHOLD_KEY)
    if stored_enabled is not None or stored_threshold is not None:
        enabled = (stored_enabled in ("1", "true", "True")) if stored_enabled is not None \
            else slow_cfg["enabled"]
        threshold = slow_cfg["threshold_ms"]
        if stored_threshold is not None:
            try:
                threshold = float(stored_threshold)
            except ValueError as exc:
                # A non-numeric stored override keeps the configured threshold;
                # the type is enough to spot a corrupt settings row.
                log.debug("slow_query_threshold_unparsable error_type=%s",
                          type(exc).__name__)
        perf.set_slow_query(enabled, threshold)
        slow_cfg = perf.slow_query_state()

    replica = await replica_probe()
    if replica.get("configured"):
        replica["url_masked"] = db_admin.masked_url(settings.DB_READ_REPLICA_URL)
        replica["enabled"] = (_get_setting(_REPLICA_TOGGLE_KEY) in ("1", "true", "True"))
        replica["routed_endpoints"] = ["/api/v1/stats", "/api/v1/studies"]
    else:
        replica["enabled"] = False
        replica["routed_endpoints"] = []

    return {
        "backend": p["backend"],
        "url_masked": db_admin.masked_url(url),
        "database": p.get("database") or p.get("file"),
        "pool": {
            "sync": perf.pool_snapshot(sync_engine.pool),
            "async_class": type(async_engine.pool).__name__,
            "recycle_seconds": settings.DB_POOL_RECYCLE_SECONDS,
            "pre_ping": settings.DB_POOL_PRE_PING,
            "pool_size": settings.DB_POOL_SIZE,
            "max_overflow": settings.DB_MAX_OVERFLOW,
        },
        "postgresql": _postgres_counters(),
        "replica": replica,
        "slow_query": {
            **slow_cfg,
            "ring_size": len(perf.slow_query_entries()),
            "default_threshold_ms": settings.DB_SLOW_QUERY_THRESHOLD_MS,
        },
        "hot_queries": _hot_query_names(),
        "time": datetime.now().isoformat(timespec="seconds"),
    }


def set_slow_query(enabled: bool | None = None, threshold_ms: float | None = None) -> dict:
    """Persist + apply the slow-query log toggle/threshold."""
    cur = perf.slow_query_state()
    enabled = cur["enabled"] if enabled is None else bool(enabled)
    threshold = cur["threshold_ms"] if threshold_ms is None else float(threshold_ms)
    if threshold_ms is not None:
        _set_setting(_SLOW_THRESHOLD_KEY, str(int(threshold)))
    _set_setting(_SLOW_TOGGLE_KEY, "1" if enabled else "0")
    perf.set_slow_query(enabled, threshold)
    return {"enabled": enabled, "threshold_ms": threshold}


def set_replica(enabled: bool) -> dict:
    """Persist the read-replica routing toggle (no-op if not configured)."""
    if not replica_configured():
        raise ValueError("No read replica configured (DB_READ_REPLICA_URL is empty)")
    _set_setting(_REPLICA_TOGGLE_KEY, "1" if enabled else "0")
    # H-08: get_read_db caches the toggle; apply the change here at once.
    from app.db.session import reset_replica_toggle_cache

    reset_replica_toggle_cache()
    return {"configured": True, "enabled": enabled}


def slow_queries() -> list[dict]:
    return perf.slow_query_entries()


def has_replica() -> bool:
    return replica_configured()
