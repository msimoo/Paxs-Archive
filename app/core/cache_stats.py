"""In-process cache telemetry, shared by the decode caches and /metrics.

WHY
---
The audit recommended measuring cache effectiveness before tuning it further
(V1-2 / V3-6): without hit/miss counters there is no way to tell whether a byte
budget is too small, too large, or whether the cache is doing anything at all.
This module is the single place those counters live, so the two cache
implementations do not each grow their own.

DESIGN
------
Deliberately dependency-free (no ``settings``, no numpy) so it can be imported
from the cache modules, from ``/metrics``, and from tests without import cycles
or side effects. Counters are plain ints behind one lock; a cache operation is
microseconds against decodes measured in seconds, so the lock is free.

Counters are cumulative since process start (Prometheus ``counter`` semantics —
``rate()`` over the scrape window is unaffected by a restart). Gauges are set,
not incremented, so they always reflect current state even if a cache is cleared
by another module.
"""

from __future__ import annotations

import threading

# Cache identities. Kept as a tuple so /metrics can emit a stable series set
# (a cache with no traffic still reports 0 rather than vanishing between scrapes,
# which would break rate() and alerting).
CACHES: tuple[str, ...] = ("volume", "frame", "ai", "bundle")

COUNTERS: tuple[str, ...] = ("hits", "misses", "inserts", "evictions")
GAUGES: tuple[str, ...] = ("bytes", "entries")

_lock = threading.Lock()
_state: dict[str, dict[str, int]] = {
    cache: {**{c: 0 for c in COUNTERS}, **{g: 0 for g in GAUGES}}
    for cache in CACHES
}


def _bucket(cache: str) -> dict[str, int] | None:
    return _state.get(cache)


def incr(cache: str, counter: str, n: int = 1) -> None:
    """Increment a counter. Unknown cache/counter names are ignored (a typo must
    never break a request path)."""
    if n == 0:
        return
    with _lock:
        bucket = _bucket(cache)
        if bucket is None or counter not in bucket:
            return
        bucket[counter] += n


def set_gauges(cache: str, *, bytes: int | None = None,
               entries: int | None = None) -> None:
    """Set absolute gauge values (not increments — see module docstring)."""
    with _lock:
        bucket = _bucket(cache)
        if bucket is None:
            return
        if bytes is not None:
            bucket["bytes"] = int(bytes)
        if entries is not None:
            bucket["entries"] = int(entries)


def snapshot() -> dict[str, dict[str, int]]:
    """A deep copy of every cache's counters and gauges."""
    with _lock:
        return {cache: dict(values) for cache, values in _state.items()}


def reset(cache: str | None = None) -> None:
    """Zero one cache's stats, or all of them (used by tests)."""
    with _lock:
        targets = [cache] if cache else list(_state)
        for name in targets:
            bucket = _bucket(name)
            if bucket is None:
                continue
            for key in bucket:
                bucket[key] = 0
