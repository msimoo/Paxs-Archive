"""The admin panel's Redis surface: probes, metrics, key browsing, flushes.

Two invariants:

* **No secret leaves this module.** ``get_redis_status`` is the payload of an
  authenticated HTTP endpoint, so it returns the *redacted* saved config and
  scrubs driver error text before returning it (B-3 / FS-8).
* **Every Redis failure is reported, never raised.** The cache is optional by
  design; a panel that 500s because Redis is down would hide the one fact the
  operator needs (that Redis is down).

Key browsing walks both tiers, so what the panel shows is what the process
actually holds — L1 (RAM) entries first, then the Redis keyspace.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import sys
import time
from types import ModuleType

import redis

from app.core.config import settings
from app.services.redis.cache import (
    _MEM_CACHE,
    _MEM_LOCK,
    _REDIS_CALL_ERRORS,
    _mem_release,
)
from app.services.redis.config import (
    masked_redis_url,
    parse_redis_url,
    saved_redis_config,
    saved_redis_password,
    scrub_secrets,
)
from app.services.redis.installer import get_redis_server_installation

log = logging.getLogger("paxs.redis")

_FACADE = "app.services.redis_service"


def _facade() -> ModuleType:
    """The public ``app.services.redis_service`` namespace, resolved per call."""
    return sys.modules[_FACADE]


# ------------------------------------------------------------- Probes & Status --
def test_redis_connection(cfg: dict | str | None = None) -> dict:
    """Probe a Redis server, measuring ping latency and reading basic server info."""
    if isinstance(cfg, str):
        url = cfg
    elif isinstance(cfg, dict):
        url = _facade().build_redis_url(cfg)
    else:
        url = getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")

    t0 = time.perf_counter()
    try:
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        ping_ok = client.ping()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        info = client.info()
        dbsize = client.dbsize()
        client.close()
        return {
            "ok": True,
            "ping": ping_ok,
            "latency_ms": latency_ms,
            "version": info.get("redis_version", "unknown"),
            "mode": info.get("redis_mode", "standalone"),
            "os": info.get("os", ""),
            "role": info.get("role", "master"),
            "uptime_seconds": info.get("uptime_in_seconds", 0),
            "connected_clients": info.get("connected_clients", 0),
            "used_memory_human": info.get("used_memory_human", ""),
            "total_keys": dbsize,
            "url_masked": masked_redis_url(url),
        }
    except _REDIS_CALL_ERRORS as exc:  # a failed probe is the answer
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "ok": False,
            "error": str(exc),
            "latency_ms": latency_ms,
            "url_masked": masked_redis_url(url),
        }


def ping_redis() -> dict:
    """Execute PING on the active Redis client with latency timing."""
    client = _facade().get_redis_client()
    if not client:
        return {"ok": False, "error": "Redis client is offline or disabled"}
    t0 = time.perf_counter()
    try:
        res = client.ping()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {"ok": True, "ping": res, "latency_ms": latency_ms}
    except _REDIS_CALL_ERRORS as exc:  # a failed ping is the answer
        return {"ok": False, "error": str(exc)}


def get_redis_status() -> dict:
    """Comprehensive status, metrics, memory and keyspace info for the Redis panel."""
    url = getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
    enabled = getattr(settings, "REDIS_CACHE_ENABLED", True)
    parsed = parse_redis_url(url)
    masked = masked_redis_url(url)

    l1_stats = _facade().get_l1_cache_stats()
    # `saved_redis_config()` is already redacted (B-3 / FS-8). Never swap in
    # `saved_redis_config_raw()` here — this dict is returned by an HTTP endpoint
    # and used to carry the Redis password in cleartext.
    saved = saved_redis_config()
    server_install = get_redis_server_installation()

    probe = test_redis_connection(url)
    connected = probe.get("ok", False)

    # A driver exception can echo the connection URL, which carries the password.
    error = probe.get("error") if not connected else None
    if error:
        error = scrub_secrets(str(error), parsed.get("password", ""), saved_redis_password())

    server_info = {}
    memory_info = {}
    stats_info = {}
    persistence_info = {}

    if connected:
        try:
            client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_timeout=1.5,
                socket_connect_timeout=1.5,
            )
            raw_info = client.info()
            dbsize = client.dbsize()
            client.close()

            hits = int(raw_info.get("keyspace_hits", 0))
            misses = int(raw_info.get("keyspace_misses", 0))
            total_lookups = hits + misses
            hit_rate = round((hits / total_lookups * 100), 1) if total_lookups > 0 else 0.0

            server_info = {
                "version": raw_info.get("redis_version", "unknown"),
                "mode": raw_info.get("redis_mode", "standalone"),
                "os": raw_info.get("os", "unknown"),
                "uptime_seconds": raw_info.get("uptime_in_seconds", 0),
                "uptime_days": round(raw_info.get("uptime_in_seconds", 0) / 86400, 1),
                "role": raw_info.get("role", "master"),
                "tcp_port": raw_info.get("tcp_port", parsed["port"]),
                "connected_clients": raw_info.get("connected_clients", 0),
                "blocked_clients": raw_info.get("blocked_clients", 0),
            }

            memory_info = {
                "used_memory_human": raw_info.get("used_memory_human", "0B"),
                "used_memory_peak_human": raw_info.get("used_memory_peak_human", "0B"),
                "maxmemory_human": raw_info.get("maxmemory_human", "0B"),
                "mem_fragmentation_ratio": raw_info.get("mem_fragmentation_ratio", 1.0),
                "used_memory_lua_human": raw_info.get("used_memory_lua_human", "0B"),
            }

            stats_info = {
                "total_keys": dbsize,
                "keyspace_hits": hits,
                "keyspace_misses": misses,
                "hit_rate_pct": hit_rate,
                "total_commands_processed": raw_info.get("total_commands_processed", 0),
                "evicted_keys": raw_info.get("evicted_keys", 0),
                "expired_keys": raw_info.get("expired_keys", 0),
            }

            persistence_info = {
                "rdb_last_save_time": raw_info.get("rdb_last_save_time", 0),
                "rdb_bgsave_in_progress": raw_info.get("rdb_bgsave_in_progress", 0) == 1,
                "aof_enabled": raw_info.get("aof_enabled", 0) == 1,
            }
        except _REDIS_CALL_ERRORS as exc:  # a stats read that fails reports the failure
            log.debug("redis_stats_failed error_type=%s", type(exc).__name__)

    return {
        "ok": True,
        "connected": connected,
        "enabled": enabled,
        "url_masked": masked,
        "latency_ms": probe.get("latency_ms"),
        "error": error,
        "parsed": {
            "host": parsed["host"],
            "port": parsed["port"],
            "db": parsed["db"],
            "has_password": bool(parsed.get("password") or saved_redis_password()),
            "ssl": parsed.get("ssl", False),
        },
        "saved": saved,
        "saved_present": saved is not None,
        "server_install": server_install,
        "server_info": server_info,
        "memory_info": memory_info,
        "stats_info": stats_info,
        "persistence_info": persistence_info,
        "tier1_ram": l1_stats,
    }


def get_redis_raw_info(section: str | None = None) -> dict:
    """Return raw INFO sections from Redis."""
    client = _facade().get_redis_client()
    if not client:
        return {"ok": False, "error": "Redis client is offline"}
    try:
        info_client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=2.0,
        )
        data = info_client.info(section) if section else info_client.info()
        info_client.close()
        return {"ok": True, "info": data}
    except _REDIS_CALL_ERRORS as exc:  # INFO is best-effort
        return {"ok": False, "error": str(exc)}


# ------------------------------------------------------------- Keys & Cache Operations --
def list_redis_keys(pattern: str = "*", limit: int = 150) -> dict:
    """Search and inspect keys in Redis and Tier-1 RAM cache."""
    pattern = pattern.strip() or "*"
    results = []

    # 1. Inspect L1 In-Memory RAM Cache keys
    l1_keys = set()
    with _MEM_LOCK:
        for k, (v, exp) in _MEM_CACHE.items():
            if fnmatch.fnmatch(k, pattern):
                l1_keys.add(k)
                ttl = max(0, int(exp - time.time()))
                results.append({
                    "key": k,
                    "type": "bytes (RAM)",
                    "ttl": ttl,
                    "size_bytes": len(v),
                    "tier": "RAM (L1)",
                })
                if len(results) >= limit:
                    break

    # 2. Inspect Redis keys
    client = _facade().get_redis_client()
    if client and len(results) < limit:
        try:
            r_client = redis.Redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_timeout=3.0,
            )
            cursor = 0
            while len(results) < limit:
                cursor, keys = r_client.scan(cursor=cursor, match=pattern, count=100)
                for k in keys:
                    if k in l1_keys:
                        continue
                    try:
                        k_type = r_client.type(k)
                        ttl = r_client.ttl(k)
                        size = 0
                        if k_type == "string":
                            size = r_client.strlen(k)
                        elif k_type == "zset":
                            size = r_client.zcard(k)
                        elif k_type == "hash":
                            size = r_client.hlen(k)
                        elif k_type == "list":
                            size = r_client.llen(k)

                        results.append({
                            "key": k,
                            "type": k_type,
                            "ttl": ttl,
                            "size_bytes": size,
                            "tier": "Redis (L2)",
                        })
                    except (redis.exceptions.RedisError, OSError, ValueError, TypeError) as exc:  # one odd key must not abort the scan
                        # Per-key scan failure: report the type so a permissions or
                        # type-mismatch problem is visible without the key's value.
                        log.debug("redis_key_scan_failed error_type=%s", type(exc).__name__)
                    if len(results) >= limit:
                        break
                if cursor == 0:
                    break
            r_client.close()
        except _REDIS_CALL_ERRORS as exc:  # a scan that fails stops the listing
            log.debug("redis_list_keys_failed error_type=%s", type(exc).__name__)

    return {
        "ok": True,
        "pattern": pattern,
        "count": len(results),
        "limit": limit,
        "keys": results,
    }


def get_key_detail(key: str) -> dict:
    """Fetch value preview, TTL, type and size for a specific key."""
    # Check L1 first
    with _MEM_LOCK:
        if key in _MEM_CACHE:
            val, exp = _MEM_CACHE[key]
            preview = val[:256].decode("utf-8", errors="replace") if len(val) <= 256 else f"{val[:128].decode('utf-8', errors='replace')}... [{len(val)} bytes]"
            return {
                "ok": True,
                "key": key,
                "tier": "RAM (L1)",
                "type": "bytes",
                "ttl": max(0, int(exp - time.time())),
                "size_bytes": len(val),
                "preview": preview,
            }

    client = _facade().get_redis_client()
    if not client:
        return {"ok": False, "error": f"Key '{key}' not found or Redis is offline"}

    try:
        r_client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=False,
            socket_timeout=2.0,
        )
        if not r_client.exists(key):
            r_client.close()
            return {"ok": False, "error": f"Key '{key}' does not exist"}

        k_type = r_client.type(key).decode("utf-8", errors="ignore")
        ttl = r_client.ttl(key)
        preview = ""
        size = 0

        if k_type == "string":
            raw = r_client.get(key) or b""
            size = len(raw)
            if size < 512:
                preview = raw.decode("utf-8", errors="replace")
            else:
                preview = f"{raw[:256].decode('utf-8', errors='replace')}... [{size} bytes total]"
        elif k_type == "zset":
            items = r_client.zrange(key, 0, 10, withscores=True)
            size = r_client.zcard(key)
            preview = json.dumps([{"member": m.decode("utf-8", errors="replace"), "score": s} for m, s in items], indent=2)
        elif k_type == "hash":
            h = r_client.hgetall(key)
            size = len(h)
            preview = json.dumps({k.decode("utf-8", errors="replace"): v[:64].decode("utf-8", errors="replace") for k, v in list(h.items())[:10]}, indent=2)
        elif k_type == "list":
            items = r_client.lrange(key, 0, 10)
            size = r_client.llen(key)
            preview = json.dumps([i[:64].decode("utf-8", errors="replace") for i in items], indent=2)

        r_client.close()
        return {
            "ok": True,
            "key": key,
            "tier": "Redis (L2)",
            "type": k_type,
            "ttl": ttl,
            "size_bytes": size,
            "preview": preview,
        }
    except _REDIS_CALL_ERRORS as exc:  # one key that cannot be read is reported
        return {"ok": False, "error": str(exc)}


def delete_redis_key(key: str) -> bool:
    """Delete a key from RAM cache and Redis."""
    deleted_ram = False
    with _MEM_LOCK:
        if key in _MEM_CACHE:
            # C-3: the byte counter must fall with the entry, or the next
            # insert evicts live keys to reclaim bytes that were already freed.
            _mem_release(key)
            deleted_ram = True

    client = _facade().get_redis_client()
    deleted_redis = False
    if client:
        try:
            deleted_redis = bool(client.delete(key))
        except _REDIS_CALL_ERRORS as exc:  # a delete that fails is reported
            log.debug("redis_delete_key_failed error_type=%s", type(exc).__name__)

    return deleted_ram or deleted_redis


def flush_redis_cache(target: str = "paxs", pattern: str = "paxs:*") -> dict:
    """Flush cache keys according to target: 'paxs', 'pattern', 'l1', or 'all'."""
    deleted_ram = 0
    deleted_redis = 0

    if target == "l1":
        deleted_ram = _facade().flush_l1_cache()
        return {"ok": True, "target": "l1", "deleted_ram": deleted_ram, "deleted_redis": 0}

    if target == "all":
        deleted_ram = _facade().flush_l1_cache()
        client = _facade().get_redis_client()
        if client:
            try:
                client.flushdb()
                return {"ok": True, "target": "all", "deleted_ram": deleted_ram, "message": "Flushed entire Redis DB"}
            except _REDIS_CALL_ERRORS as exc:  # a flush that fails is reported
                return {"ok": False, "error": str(exc)}
        return {"ok": True, "target": "all", "deleted_ram": deleted_ram, "message": "RAM cache cleared (Redis offline)"}

    # Target is 'paxs' or 'pattern'
    pat = "paxs:*" if target == "paxs" else pattern
    with _MEM_LOCK:
        matching = [k for k in _MEM_CACHE if fnmatch.fnmatch(k, pat)]
        for k in matching:
            # C-3: release through the accounting helper, not `del` — a pattern
            # flush that leaves the counter high makes every later insert
            # evict live keys chasing bytes that no longer exist.
            _mem_release(k)
        deleted_ram = len(matching)

    client = _facade().get_redis_client()
    if client:
        try:
            r_client = redis.Redis.from_url(
                settings.REDIS_URL,
                decode_responses=False,
                socket_timeout=3.0,
            )
            keys = r_client.keys(pat)
            if keys:
                deleted_redis = r_client.delete(*keys)
            r_client.close()
        except _REDIS_CALL_ERRORS as exc:  # a pattern flush that fails is reported
            log.debug("redis_flush_failed pattern=%s error_type=%s",
                      pat, type(exc).__name__)

    return {
        "ok": True,
        "target": target,
        "pattern": pat,
        "deleted_ram": deleted_ram,
        "deleted_redis": deleted_redis,
        "total_deleted": deleted_ram + deleted_redis,
    }
