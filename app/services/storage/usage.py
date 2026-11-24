"""Storage usage counters and their TTL cache.

``usage_bytes()`` walks the storage tree (O(objects) on the local backend; a
bucket listing on S3), and the dashboard ``/stats`` endpoint wants it on every
page load — a real cost past ~10^5 objects. The cached variant keeps a TTL
cache: Redis when available (shared across API nodes behind HAProxy), else an
in-process LRU. The TTL is ``STATS_USAGE_CACHE_SECONDS``; ``0`` disables the
cache entirely. ``STATS_USAGE_CACHE_MAX_ENTRIES=0`` disables only local
retention while preserving Redis caching.

The cache is an optimisation only, so every failure inside it degrades to
recomputing the figure — a stale or unreadable cache value must never fail a
request that could simply measure the store.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import OrderedDict
from types import ModuleType

from app.core.config import settings

# `_resolve` here is the facade-routed seam (see resolve._resolve_seam).
from app.services.storage.resolve import _resolve_seam as _resolve

log = logging.getLogger("paxs.storage")

_FACADE = "app.services.storage_service"


def _facade() -> ModuleType:
    """The public ``app.services.storage_service`` namespace, resolved per call.

    ``storage_usage_bytes`` and ``_resolve`` are both patched on the facade by
    the scalability suite (it counts walks, and swaps in a failing backend), so
    the cached path must look them up there rather than binding them here.
    """
    return sys.modules[_FACADE]


_usage_cache: OrderedDict[tuple[str | None, str], tuple[float, int]] = OrderedDict()
_usage_cache_lock = threading.Lock()


def storage_usage_bytes(backend_name: str | None = None) -> int:
    """Return the byte usage of the selected storage backend."""
    return _resolve(backend_name).usage_bytes()


def cached_storage_usage_bytes(backend_name: str | None = None) -> int:
    """storage_usage_bytes() with a TTL cache (Redis-first, in-process
    fallback). Safe to call from async endpoints: the underlying walk is I/O
    bound, so callers should still run it via asyncio.to_thread() when the
    cache is cold.
    """
    ttl = getattr(settings, "STATS_USAGE_CACHE_SECONDS", 60)
    if not ttl:
        return _facade().storage_usage_bytes(backend_name)

    cache_key = f"paxs:usage:{(backend_name or 'default')}"

    # 1) Redis (shared across API nodes).
    from app.services.redis_service import cache_get, cache_set

    redis_val = cache_get(cache_key)
    if redis_val is not None:
        try:
            return int(redis_val)
        except (TypeError, ValueError) as exc:
            # A corrupt/foreign value under our key is ignored and the usage is
            # recomputed; debug keeps a schema change visible.
            log.debug("usage_cache_value_invalid error_type=%s", type(exc).__name__)

    # 2) In-process cache.
    now = time.monotonic()
    maximum = settings.STATS_USAGE_CACHE_MAX_ENTRIES
    local_key = (backend_name, "local")
    with _usage_cache_lock:
        _trim_usage_cache_locked(maximum)
        hit = _usage_cache.get(local_key)
        if hit is not None:
            if now - hit[0] < ttl:
                _usage_cache.move_to_end(local_key)
                return hit[1]
            _usage_cache.pop(local_key, None)

    # 3) Cold: compute and store in both caches.
    total = _facade().storage_usage_bytes(backend_name)
    if maximum > 0:
        with _usage_cache_lock:
            inserted_at = time.monotonic()
            _trim_usage_cache_locked(maximum)
            _usage_cache[local_key] = (inserted_at, total)
            _usage_cache.move_to_end(local_key)
            _trim_usage_cache_locked(maximum)
    cache_set(cache_key, str(total), expire_seconds=ttl)
    return total


def _trim_usage_cache_locked(maximum: int) -> None:
    """Enforce the LRU cap with the cache lock held; zero clears every entry."""
    if maximum == 0:
        _usage_cache.clear()
        return
    while len(_usage_cache) > maximum:
        _usage_cache.popitem(last=False)


def invalidate_usage_cache(backend_name: str | None = None) -> None:
    """Drop cached usage counters (called after deletes/tiering so /stats
    reflects the change immediately instead of waiting out the TTL)."""
    with _usage_cache_lock:
        _usage_cache.pop((backend_name, "local"), None)
    from app.services.redis_service import cache_delete_pattern

    cache_delete_pattern(f"paxs:usage:{(backend_name or 'default')}")
