"""S9 (plans/09): one DB query per series for WADO-RS frame requests, not one per frame.

A viewer scrolling a 683-slice CT issues hundreds of concurrent
``/frames/N`` requests. Each used to run its own scoped instance lookup, and
under two parallel readers the database dropped connections mid-query
(``OperationalError 2013``) and frames failed with 500.

On the first frame of a series this loads every instance row of that series
in the caller's scope — five narrow columns, no ``metadata_json`` — and keeps
lightweight snapshots for ``TTL_SECONDS``. A per-series lock makes the first
burst of parallel requests share that single query.

Safety:
- the key includes the caller's tenant scope, so one tenant's lookup is never
  served to another;
- the TTL is short and every frame still checks that the stored object exists
  (``storage_service.exists`` in the caller), so a deleted study stops serving
  pixels as soon as its files are gone, and a re-indexed one within seconds;
- ``content_version`` travels in the snapshot, so frame ETags/Redis keys still
  change when bytes are rewritten (worst case: TTL seconds late);
- a lookup that loses its DB connection mid-query is retried once.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Instance

TTL_SECONDS = 15.0
MAX_SERIES = 256


@dataclass(frozen=True)
class FrameInstance:
    """What the frame path reads from an instance row (no session needed)."""

    id: int
    sop_instance_uid: str
    file_path: str
    storage_backend: str | None
    content_version: int


SeriesKey = tuple[int | None, bool, str, str]


class FrameInstanceCache:
    def __init__(self, ttl: float = TTL_SECONDS, max_series: int = MAX_SERIES,
                 clock=time.monotonic) -> None:
        self._ttl = ttl
        self._max = max_series
        self._clock = clock
        self._series: OrderedDict[SeriesKey, tuple[float, dict[str, FrameInstance]]] = OrderedDict()
        self._locks: dict[SeriesKey, asyncio.Lock] = {}

    def _fresh(self, key: SeriesKey) -> dict[str, FrameInstance] | None:
        entry = self._series.get(key)
        if entry is None:
            return None
        loaded_at, rows = entry
        if self._clock() - loaded_at > self._ttl:
            self._series.pop(key, None)
            return None
        self._series.move_to_end(key)
        return rows

    def _store(self, key: SeriesKey, rows: dict[str, FrameInstance]) -> None:
        self._series[key] = (self._clock(), rows)
        self._series.move_to_end(key)
        while len(self._series) > self._max:
            evicted, _ = self._series.popitem(last=False)
            self._locks.pop(evicted, None)

    def clear(self) -> None:
        self._series.clear()
        self._locks.clear()

    async def get(self, db: AsyncSession, study_uid: str, series_uid: str, sop_uid: str, *,
                  tenant_id: int | None, include_all: bool) -> FrameInstance | None:
        key: SeriesKey = (tenant_id, bool(include_all), study_uid, series_uid)
        rows = self._fresh(key)
        if rows is None:
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                rows = self._fresh(key)
                if rows is None:
                    rows = await _load_series(db, study_uid, series_uid,
                                              tenant_id=tenant_id, include_all=include_all)
                    self._store(key, rows)
        return rows.get(sop_uid)


async def _load_series(db: AsyncSession, study_uid: str, series_uid: str, *,
                       tenant_id: int | None, include_all: bool) -> dict[str, FrameInstance]:
    from app.services.query_service import instance_query

    stmt = instance_query(study_uid, series_uid, {}, tenant_id=tenant_id,
                          include_all=include_all).with_only_columns(
        Instance.id, Instance.sop_instance_uid, Instance.file_path,
        Instance.storage_backend, Instance.content_version,
    )
    for attempt in (1, 2):
        try:
            result = await db.execute(stmt)
            break
        except DBAPIError as exc:
            # SQLAlchemy flags a dropped/terminated connection (asyncpg
            # ConnectionDoesNotExistError, server restart, admin kill) as
            # connection_invalidated; anything else is a real query error.
            if attempt == 2 or not exc.connection_invalidated:
                raise
            await db.rollback()  # discard the dead connection; the pool pre-pings a fresh one
    return {
        row.sop_instance_uid: FrameInstance(
            id=row.id, sop_instance_uid=row.sop_instance_uid, file_path=row.file_path,
            storage_backend=row.storage_backend, content_version=row.content_version or 1,
        )
        for row in result
    }


frame_instances = FrameInstanceCache()
