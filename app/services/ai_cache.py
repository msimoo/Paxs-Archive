"""Tenant-scoped byte-bounded LRU for experimental AI results and masks."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from app.core import cache_stats
from app.core.config import settings

_AI_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_AI_MASK_INDEX: dict[str, str] = {}
_AI_CACHE_LOCK = threading.RLock()
_MAX_AI_CACHE_SIZE = 32
_AI_CACHE_BYTES = 0


def _mask_key(tenant_id: int | None, include_all: bool, series_uid: str) -> str:
    """Build the same tenant-qualified identity as the inference service."""
    scope = "all" if include_all else f"tenant:{tenant_id}" if tenant_id is not None else "legacy"
    return f"mask:{scope}:{series_uid}"


def _budget_bytes() -> int:
    """Read the live configured cache budget; zero disables retention."""
    try:
        megabytes = int(settings.AI_MASK_CACHE_BUDGET_MB)
    except (AttributeError, TypeError, ValueError):
        return 0
    return max(0, megabytes) * 1024 * 1024


def _set_gauges_locked() -> None:
    cache_stats.set_gauges("ai", bytes=_AI_CACHE_BYTES, entries=len(_AI_CACHE))


def _drop_entry_locked(cache_key: str, *, eviction: bool) -> None:
    """Remove one canonical result and any index pointing to it."""
    global _AI_CACHE_BYTES
    result = _AI_CACHE.pop(cache_key, None)
    if result is None:
        return
    _AI_CACHE_BYTES -= int(result["_mask_array"].nbytes)
    stale = [key for key, indexed_key in _AI_MASK_INDEX.items() if indexed_key == cache_key]
    for key in stale:
        del _AI_MASK_INDEX[key]
    if eviction:
        cache_stats.incr("ai", "evictions")
    _set_gauges_locked()


def _trim_locked(budget: int) -> None:
    """Evict least-recent entries until both active bounds hold."""
    while _AI_CACHE and (
        len(_AI_CACHE) > _MAX_AI_CACHE_SIZE or _AI_CACHE_BYTES > budget
    ):
        _drop_entry_locked(next(iter(_AI_CACHE)), eviction=True)


def get_cached_inference(
    cache_key: str,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
    series_uid: str | None = None,
) -> dict[str, Any] | None:
    """Return a JSON-safe cache hit and promote it in LRU order."""
    with _AI_CACHE_LOCK:
        _trim_locked(_budget_bytes())
        result = _AI_CACHE.get(cache_key)
        if result is None:
            cache_stats.incr("ai", "misses")
            return None
        _AI_CACHE.move_to_end(cache_key)
        if series_uid is not None:
            _AI_MASK_INDEX[_mask_key(tenant_id, include_all, series_uid)] = cache_key
        cache_stats.incr("ai", "hits")
        return {key: value for key, value in result.items() if not key.startswith("_")}


def cache_inference(
    cache_key: str,
    tenant_id: int | None,
    include_all: bool,
    series_uid: str,
    result: dict[str, Any],
) -> None:
    """Store a result only when it fits both the byte and entry ceilings."""
    global _AI_CACHE_BYTES
    mask = result.get("_mask_array")
    size = getattr(mask, "nbytes", None)
    if not isinstance(size, int) or size <= 0:
        return

    with _AI_CACHE_LOCK:
        budget = _budget_bytes()
        _trim_locked(budget)
        if budget == 0 or size > budget or _MAX_AI_CACHE_SIZE <= 0:
            mask_lookup_key = _mask_key(tenant_id, include_all, series_uid)
            previous_mask_key = _AI_MASK_INDEX.get(mask_lookup_key)
            _drop_entry_locked(cache_key, eviction=True)
            if previous_mask_key is not None and previous_mask_key != cache_key:
                _drop_entry_locked(previous_mask_key, eviction=True)
            return
        _drop_entry_locked(cache_key, eviction=False)
        while _AI_CACHE and (
            len(_AI_CACHE) >= _MAX_AI_CACHE_SIZE or _AI_CACHE_BYTES + size > budget
        ):
            _drop_entry_locked(next(iter(_AI_CACHE)), eviction=True)
        _AI_CACHE[cache_key] = result
        _AI_CACHE_BYTES += size
        _AI_MASK_INDEX[_mask_key(tenant_id, include_all, series_uid)] = cache_key
        cache_stats.incr("ai", "inserts")
        _set_gauges_locked()


def get_cached_mask(
    series_uid: str,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> Any | None:
    """Return a mask only from the cached inference in the same caller scope."""
    with _AI_CACHE_LOCK:
        _trim_locked(_budget_bytes())
        cache_key = _AI_MASK_INDEX.get(_mask_key(tenant_id, include_all, series_uid))
        result = _AI_CACHE.get(cache_key) if cache_key is not None else None
        if result is None:
            cache_stats.incr("ai", "misses")
            return None
        _AI_CACHE.move_to_end(cache_key)
        cache_stats.incr("ai", "hits")
        return result["_mask_array"]


def ai_cache_stats() -> dict[str, int]:
    """Expose PHI-free cache gauges and eviction count for diagnostics/tests."""
    with _AI_CACHE_LOCK:
        _trim_locked(_budget_bytes())
        stats = cache_stats.snapshot()["ai"]
        return {**stats, "budget_bytes": _budget_bytes()}


def clear_ai_cache(*, reset_stats: bool = False) -> None:
    """Drop all retained masks; optionally reset process-local test counters."""
    global _AI_CACHE_BYTES
    with _AI_CACHE_LOCK:
        _AI_CACHE.clear()
        _AI_MASK_INDEX.clear()
        _AI_CACHE_BYTES = 0
        _set_gauges_locked()
        if reset_stats:
            cache_stats.reset("ai")
