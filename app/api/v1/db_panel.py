"""Admin → DB Pool panel API (read-replica / connection-pool dashboard).

* ``GET  /api/v1/db/perf``            — pool stats + MySQL counters + replica
* ``POST /api/v1/db/perf/slow-query`` — toggle / threshold for the slow-query log
* ``GET  /api/v1/db/perf/slow-queries``— recent slow statements (ring buffer)
* ``GET  /api/v1/db/perf/explain``     — EXPLAIN for a hot query or custom SQL
* ``POST /api/v1/db/perf/replica``     — toggle read-replica routing

Everything is gated behind ``require_destructive_role`` (ADMIN / RADIOLOGIST)
— pool internals, EXPLAIN plans and the slow-query log reveal server internals.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.v1.deps import require_destructive_role
from app.db.models import User
from app.services import db_panel

router = APIRouter(prefix="/api/v1/db/perf", tags=["db perf"])


class SlowQueryBody(BaseModel):
    enabled: bool | None = None
    threshold_ms: float | None = None


class ReplicaBody(BaseModel):
    enabled: bool


@router.get("")
async def perf(_user: User = Depends(require_destructive_role)):
    """Live pool stats, MySQL counters, replica status and slow-query state."""
    return await db_panel.perf_panel()


@router.post("/slow-query")
async def toggle_slow_query(body: SlowQueryBody,
                            _user: User = Depends(require_destructive_role)):
    return await asyncio.to_thread(db_panel.set_slow_query, body.enabled, body.threshold_ms)


@router.get("/slow-queries")
async def list_slow_queries(_user: User = Depends(require_destructive_role)):
    return db_panel.slow_queries()


@router.get("/explain")
async def explain(name: str = "", sql: str = "",
                  _user: User = Depends(require_destructive_role)):
    try:
        return await asyncio.to_thread(db_panel.explain_query, name, sql)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/replica")
async def toggle_replica(body: ReplicaBody,
                         _user: User = Depends(require_destructive_role)):
    try:
        return await asyncio.to_thread(db_panel.set_replica, body.enabled)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
