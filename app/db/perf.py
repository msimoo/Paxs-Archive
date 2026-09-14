"""Database performance instrumentation for the admin dashboard.

Powers the Admin → **DB Pool** panel (read-replica / connection-pool view):

* **Live pool stats** — size, checked-out, overflow from the sync QueuePool
  that the DICOM receiver / worker threads share (the async API engine uses
  NullPool — one connection per request — so its "pool" is intentionally
  unpooled).
* **Slow-query log** — engine hooks time every cursor execute and keep the
  statements that ran at or above the threshold in an in-memory ring buffer.
  The toggle + threshold are persisted in ``server_settings`` by the service
  layer; the hooks themselves only need module state.
* **EXPLAIN analyzer** — ``explain_query`` runs the plan for the hottest
  archive queries (portable ``EXPLAIN QUERY PLAN`` on SQLite).

This module deliberately imports nothing from ``app`` (except config) so that
``app/db/session.py`` can install the hooks without a circular import.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

# --------------------------------------------------------------------------- #
# slow-query ring buffer + hook state
# --------------------------------------------------------------------------- #

SLOW_QUERIES: deque[dict[str, Any]] = deque(maxlen=200)
_slow_enabled = False
_slow_threshold_ms = 500.0


def set_slow_query(enabled: bool, threshold_ms: float) -> None:
    """Turn the slow-query log on/off and (re)set the threshold (milliseconds)."""
    global _slow_enabled, _slow_threshold_ms
    _slow_enabled = bool(enabled)
    _slow_threshold_ms = max(1.0, float(threshold_ms or 0))


def slow_query_state() -> dict[str, Any]:
    return {"enabled": _slow_enabled, "threshold_ms": _slow_threshold_ms}


def slow_query_entries() -> list[dict[str, Any]]:
    """Snapshot of the ring buffer, newest first (safe to call from any loop)."""
    return list(reversed(SLOW_QUERIES))


def install_slow_query_hooks(engine: Engine, label: str) -> None:
    """Time cursor executions on *engine*; slow ones land in the ring buffer.

    ``label`` tags entries with the engine that produced them (``async`` API
    engine vs ``sync`` receiver/worker engine).
    """

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, _cursor, _statement, _params, _context, _executemany):
        if not _slow_enabled:
            return
        conn.info["paxs_perf_start"] = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, _cursor, statement, params, _context, _executemany):
        start = conn.info.pop("paxs_perf_start", None)
        if start is None:
            return
        ms = (time.perf_counter() - start) * 1000.0
        if not _slow_enabled or ms < _slow_threshold_ms:
            return
        sql = " ".join(str(statement).split())
        SLOW_QUERIES.append(
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "engine": label,
                "ms": round(ms, 1),
                "sql": sql[:600],
                "params": repr(params)[:200] if params else None,
            }
        )


# --------------------------------------------------------------------------- #
# pool snapshot
# --------------------------------------------------------------------------- #

def pool_snapshot(pool: Any) -> dict[str, Any]:
    """Live metrics from a SQLAlchemy pool.

    QueuePool exposes ``checkedout() / size() / overflow()`` — the three
    numbers the dashboard shows. NullPool (the async engine) reports 0s.
    ``status()`` is only a human-readable string in SQLAlchemy 2.0, so we read
    the counters via the pool API and keep the caps from the pool internals.
    """
    name = type(pool).__name__
    checked = size = overflow = 0
    try:
        checked = int(pool.checkedout())
        size = int(pool.size())
        overflow = max(int(pool.overflow()), 0)
    except Exception:  # pragma: no cover - NullPool / exotic pools
        pass
    max_overflow = int(getattr(pool, "_max_overflow", 0) or 0)
    try:  # the internal queue's maxsize is the effective pool cap
        pool_size = int(getattr(getattr(pool, "_pool", None), "maxsize", 0) or 0)
    except Exception:  # pragma: no cover
        pool_size = 0
    return {
        "class": name,
        "checkedout": checked,
        "size": size,
        "overflow": overflow,
        "checkedout_size": checked,
        "overflow_size": overflow,
        "max_overflow": max_overflow,
        "pool_size": pool_size,
    }
