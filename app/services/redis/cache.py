"""The shared Redis client pool and the Tier-1 in-memory cache.

Two layers, one API:

* **L2 — Redis.** One thread-safe, lazily created client, guarded by a lock.
  Redis being unreachable is a *supported* state, not an error: every caller
  falls back to the RAM cache and the process keeps serving.
* **L1 — a bounded in-process LRU.** Capped in both items (``_MEM_MAX_ITEMS``)
  and bytes (``_MEM_MAX_BYTES``), because an unbounded cache in a DICOM archive
  becomes an OOM (N-5). ``_CURRENT_MEM_BYTES`` is the number the eviction loop
  compares against the budget, so **every** removal goes through
  :func:`_mem_release`; a removal that forgets to decrement makes the next
  insert evict live entries chasing phantom bytes (the C-3 defect).

The client pool is reached through ``app.services.redis_service`` at call time
(:func:`_facade`) so ``monkeypatch.setattr(redis_service, "get_redis_client",
...)`` still drives the cache path.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from types import ModuleType
from typing import Any

import redis

from app.core.config import settings

log = logging.getLogger("paxs.redis")

#: What a Redis call can raise here: the client's own hierarchy, the
#: socket layer beneath it, and a reply that does not parse. Named once so
#: the call sites below cannot drift back into a blind `except Exception`.
_REDIS_CALL_ERRORS = (redis.exceptions.RedisError, OSError, ValueError, TypeError)

_FACADE = "app.services.redis_service"


def _facade() -> ModuleType:
    """The public ``app.services.redis_service`` namespace, resolved per call.

    Two reasons to go through it: ``get_redis_client`` is mutated by the test
    suite (a fake client must be observed by the cache path), and
    ``masked_redis_url`` lives in ``config``, which itself imports this module —
    so a direct import here would close a cycle.
    """
    return sys.modules[_FACADE]


_redis_client: redis.Redis | None = None
_redis_available: bool = False
_redis_lock = threading.Lock()


class RedisCircuitOpenError(RuntimeError):
    """Raised instead of contacting Redis while its circuit is open."""


class RedisCircuitBreaker:
    """Thread-safe circuit breaker with a single half-open probe."""

    def __init__(self, failure_threshold: int, reset_seconds: float) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.reset_seconds = max(0.01, float(reset_seconds))
        self._lock = threading.Lock()
        self._failures = 0
        self._errors = 0
        self._opened_at = 0.0
        self._state = "closed"
        self._probe_active = False

    def call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call Redis if admitted; record failures and recover on a successful probe."""
        with self._lock:
            if self._state == "open":
                if time.monotonic() - self._opened_at < self.reset_seconds:
                    raise RedisCircuitOpenError("Redis circuit is open")
                self._state = "half_open"
            if self._state == "half_open":
                if self._probe_active:
                    raise RedisCircuitOpenError("Redis circuit probe is already active")
                self._probe_active = True
        try:
            result = function(*args, **kwargs)
        except _REDIS_CALL_ERRORS:
            with self._lock:
                self._errors += 1
                self._failures += 1
                self._opened_at = time.monotonic()
                self._probe_active = False
                if self._failures >= self.failure_threshold or self._state == "half_open":
                    self._state = "open"
            raise
        with self._lock:
            self._failures = 0
            self._probe_active = False
            self._state = "closed"
        return result

    def stats(self) -> dict[str, int | str | bool]:
        """Return PHI-free breaker gauges."""
        with self._lock:
            return {"state": self._state, "failures": self._failures,
                    "errors": self._errors, "probe_active": self._probe_active}

    def is_fast_open(self) -> bool:
        """True when calls would immediately fail closed without contacting Redis."""
        with self._lock:
            if self._state == "open":
                return (time.monotonic() - self._opened_at) < self.reset_seconds
            return self._state == "half_open" and self._probe_active

    def reset(self) -> None:
        """Clear circuit state."""
        with self._lock:
            self._failures = self._errors = 0
            self._opened_at = 0.0
            self._state = "closed"
            self._probe_active = False


_REDIS_BREAKER = RedisCircuitBreaker(
    getattr(settings, "REDIS_CIRCUIT_FAILURE_THRESHOLD", 5),
    getattr(settings, "REDIS_CIRCUIT_RESET_SECONDS", 10.0),
)


def redis_circuit_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one synchronous Redis command through the process circuit breaker."""
    return _REDIS_BREAKER.call(function, *args, **kwargs)


def reset_redis_circuit() -> None:
    """Rebuild circuit state from current settings."""
    global _REDIS_BREAKER
    _REDIS_BREAKER = RedisCircuitBreaker(
        getattr(settings, "REDIS_CIRCUIT_FAILURE_THRESHOLD", 5),
        getattr(settings, "REDIS_CIRCUIT_RESET_SECONDS", 10.0),
    )


def redis_circuit_stats() -> dict[str, int | str | bool]:
    """Return Redis breaker state without data-derived labels."""
    return _REDIS_BREAKER.stats()


def is_redis_ready_fast() -> bool:
    """Non-blocking check: False when Redis is disabled or circuit is open."""
    if not getattr(settings, "REDIS_CACHE_ENABLED", True) or not getattr(settings, "REDIS_URL", None):
        return False
    return not _REDIS_BREAKER.is_fast_open()

# High-speed In-Memory LRU Cache (Layer 1 RAM Cache - max 256MB / 4000 items)
_MEM_CACHE: OrderedDict[str, tuple[bytes, float]] = OrderedDict()
_MEM_LOCK = threading.Lock()
_MEM_MAX_ITEMS = 4000
_MEM_MAX_BYTES = 256 * 1024 * 1024  # 256 MB cap
_CURRENT_MEM_BYTES = 0


# ------------------------------------------------ L1 byte accounting (C-3) --
# `_CURRENT_MEM_BYTES` is the number the eviction loop compares against
# `_MEM_MAX_BYTES`, so it must equal the live payload exactly. It is a module
# global rather than a `len()` of the cache because recomputing it on every set
# would be O(n) on the hot path. The price of that is that *every* removal must
# go through `_mem_release` — a removal that forgets to decrement leaves the
# counter overstating the cache, and the next `cache_set_mem` then evicts live
# entries until the phantom bytes are "freed". That is the C-3 symptom: the L1
# cache evicting far more than the budget intends.
#
# Callers must hold `_MEM_LOCK` (the two helpers mutate the cache and the
# counter together, which is what makes them safe to compose).
def _mem_release(key: str) -> int:
    """Remove ``key`` from the L1 cache, keeping the byte counter exact.

    Returns the number of bytes released (0 when the key was absent).
    """
    global _CURRENT_MEM_BYTES
    entry = _MEM_CACHE.pop(key, None)
    if entry is None:
        return 0
    released = len(entry[0])
    _CURRENT_MEM_BYTES = max(0, _CURRENT_MEM_BYTES - released)
    return released


def _mem_recount() -> int:
    """Recompute the byte counter from the live cache (self-heal for C-3).

    Called by the stats endpoint so a counter that somehow drifted is corrected
    on the next observation instead of silently over-evicting forever.
    """
    global _CURRENT_MEM_BYTES
    _CURRENT_MEM_BYTES = sum(len(value) for value, _expires in _MEM_CACHE.values())
    return _CURRENT_MEM_BYTES


# ------------------------------------------------------------- Client Pool --
def reset_redis_client(new_url: str | None = None, enabled: bool | None = None) -> redis.Redis | None:
    """Reset and re-establish the Redis client connection pool with new settings."""
    global _redis_client, _redis_available
    with _redis_lock:
        if new_url is not None:
            setattr(settings, "REDIS_URL", new_url)
        if enabled is not None:
            setattr(settings, "REDIS_CACHE_ENABLED", enabled)

        if _redis_client is not None:
            try:
                _redis_client.close()
            except (redis.exceptions.RedisError, OSError, ValueError) as exc:  # the client is being discarded
                log.debug("redis_client_close_failed error_type=%s", type(exc).__name__)
            _redis_client = None
            _redis_available = False

    reset_redis_circuit()
    return _facade().get_redis_client()


def get_redis_client() -> redis.Redis | None:
    """Return a shared thread-safe Redis client instance, or None if unreachable."""
    global _redis_client, _redis_available
    if not getattr(settings, "REDIS_CACHE_ENABLED", True) or not getattr(settings, "REDIS_URL", None):
        return None

    if _redis_client is None:
        with _redis_lock:
            if _redis_client is None:
                try:
                    client = redis.Redis.from_url(
                        settings.REDIS_URL,
                        decode_responses=False,
                        socket_timeout=float(getattr(settings, "REDIS_COMMAND_TIMEOUT_SECONDS", 1.0)),
                        socket_connect_timeout=float(getattr(settings, "REDIS_CONNECT_TIMEOUT_SECONDS", 0.5)),
                    )
                    redis_circuit_call(client.ping)
                    _redis_client = client
                    _redis_available = True
                    log.info("Connected to Redis at %s",
                             _facade().masked_redis_url(settings.REDIS_URL))
                except RedisCircuitOpenError:
                    _redis_client = None
                    _redis_available = False
                except _REDIS_CALL_ERRORS as exc:  # unreachable Redis is a supported state
                    _redis_client = None
                    _redis_available = False
                    log.debug("redis_unavailable error_type=%s; using in-memory LRU cache",
                              type(exc).__name__)

    return _redis_client if _redis_available else None


# ------------------------------------------------------------- Tier 1 RAM Cache --
def get_l1_cache_stats() -> dict:
    """Return metrics for the Tier-1 In-Memory RAM LRU cache."""
    with _MEM_LOCK:
        # C-3: recompute rather than trust the running counter. Reporting a
        # drifted number would make the dashboard lie about how close the cache
        # is to its budget, and the same counter drives eviction.
        used_bytes = _mem_recount()
        count = len(_MEM_CACHE)
    return {
        "items_count": count,
        "max_items": _MEM_MAX_ITEMS,
        "used_bytes": used_bytes,
        "used_mb": round(used_bytes / (1024 * 1024), 2),
        "max_bytes": _MEM_MAX_BYTES,
        "max_mb": int(_MEM_MAX_BYTES / (1024 * 1024)),
        "utilization_pct": round((used_bytes / _MEM_MAX_BYTES) * 100, 1),
    }


def flush_l1_cache() -> int:
    """Clear all items from Tier-1 RAM cache."""
    global _CURRENT_MEM_BYTES
    with _MEM_LOCK:
        count = len(_MEM_CACHE)
        _MEM_CACHE.clear()
        _CURRENT_MEM_BYTES = 0
        return count


# ------------------------------------------------------------- Core Caching API --
def cache_get(key: str) -> bytes | None:
    """Retrieve raw bytes from in-memory RAM cache first, then Redis."""
    now = time.time()

    # 1. Tier 1: Check In-Memory RAM LRU
    with _MEM_LOCK:
        if key in _MEM_CACHE:
            val, exp = _MEM_CACHE[key]
            if exp > now:
                _MEM_CACHE.move_to_end(key)
                return val
            # C-3: an expired entry is a *removal* — releasing it through the
            # accounting helper keeps the byte counter in step with the cache.
            _mem_release(key)

    # 2. Tier 2: Check Redis
    client = _facade().get_redis_client()
    if not client:
        return None
    try:
        data = redis_circuit_call(client.get, key)
        if data:
            cache_set_mem(key, data, expire_seconds=300)
        return data
    except RedisCircuitOpenError:
        return None
    except _REDIS_CALL_ERRORS as exc:  # the cache is optional by design
        log.debug("redis_cache_get_failed error_type=%s", type(exc).__name__)
        return None


def cache_set_mem(key: str, value: bytes, expire_seconds: int = 600) -> None:
    """Store directly into Layer 1 In-Memory LRU RAM Cache."""
    global _CURRENT_MEM_BYTES
    if not isinstance(value, bytes):
        return
    val_size = len(value)
    if val_size > 16 * 1024 * 1024:  # do not store single items > 16MB in RAM
        return

    exp = time.time() + expire_seconds
    with _MEM_LOCK:
        # C-3: replacing an entry releases its bytes first, so the counter is
        # never left holding the old value *and* the new one.
        _mem_release(key)

        while (
            len(_MEM_CACHE) >= _MEM_MAX_ITEMS
            or (_CURRENT_MEM_BYTES + val_size) > _MEM_MAX_BYTES
        ) and _MEM_CACHE:
            _mem_release(next(iter(_MEM_CACHE)))  # LRU end of the OrderedDict

        _MEM_CACHE[key] = (value, exp)
        _CURRENT_MEM_BYTES += val_size


def cache_set(key: str, value: bytes | str, expire_seconds: int = 600) -> bool:
    """Store raw bytes or string into both In-Memory LRU Cache and Redis."""
    val_bytes = value.encode("utf-8") if isinstance(value, str) else value
    cache_set_mem(key, val_bytes, expire_seconds=expire_seconds)

    client = _facade().get_redis_client()
    if not client:
        return True
    try:
        redis_circuit_call(client.set, key, val_bytes, ex=expire_seconds)
        return True
    except RedisCircuitOpenError:
        return True
    except _REDIS_CALL_ERRORS as exc:  # a cache write must never fail the caller
        log.debug("redis_cache_set_failed error_type=%s", type(exc).__name__)
        return True


def cache_delete_pattern(pattern: str) -> int:
    """Delete all keys matching pattern from memory and Redis."""
    return _facade().flush_redis_cache(target="pattern", pattern=pattern).get("total_deleted", 0)
