"""Lightweight in-memory rate limiting (no Redis required).

A sliding-window counter keyed by client IP. Kept deliberately simple and
dependency-free; swap for slowapi/redis when running multiple API workers.

Usage as a FastAPI dependency:

    from app.api.ratelimit import rate_limit
    @router.post("/login", dependencies=[Depends(rate_limit("auth", 10, 60))])
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request
from app.core.concurrency import run_blocking

log = logging.getLogger("paxs.ratelimit")

# window_key -> {client_key: deque[timestamps]}
_WINDOWS: dict[str, dict[str, deque[float]]] = defaultdict(dict)
_LOCK = Lock()
_CLEANUP_TICK = 0


def _trusted_proxies() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse the configured trusted proxy IPs and CIDRs.

    Invalid entries are ignored rather than broadening trust. Hostnames and
    wildcards are deliberately unsupported: X-Forwarded-For is a network-peer
    trust decision and must be tied to an IP address or subnet.
    """
    from app.core.config import settings

    raw = getattr(settings, "TRUSTED_PROXIES", "") or ""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError as exc:  # a bare address is accepted as a /32 or /128
            log.debug("trusted_proxy_cidr_parse_failed error_type=%s", type(exc).__name__)
            try:
                address = ipaddress.ip_address(item)
            except ValueError as exc:  # invalid entry is dropped, never widened
                # An ignored entry narrows trust, so make the operator's typo
                # visible — but never echo the entry or the message (N-2).
                log.warning("trusted_proxy_entry_ignored error_type=%s",
                            type(exc).__name__)
                continue
            network = ipaddress.ip_network(
                f"{address}/{address.max_prefixlen}", strict=False
            )
        networks.append(network)
    return tuple(networks)


def _in_trusted_proxy(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        address.version == network.version and address in network
        for network in _trusted_proxies()
    )


def _parse_forwarded_chain(value: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address] | None:
    """Parse every XFF element; a malformed chain is ignored in its entirety."""
    parts = value.split(",")
    if not parts or len(parts) > 32:
        return None
    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for part in parts:
        text = part.strip()
        if not text or "%" in text:
            return None
        try:
            parsed.append(ipaddress.ip_address(text))
        except ValueError:
            return None
    return parsed


def _client_key(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer

    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not _in_trusted_proxy(peer_address):
        return peer

    chain = _parse_forwarded_chain(forwarded)
    if chain is None:
        return peer

    # Walk from the app-facing end of the chain. Skip any configured proxy
    # hops; the first untrusted address is the client observed by that chain.
    # This avoids trusting a client-supplied leftmost XFF value when a proxy
    # appends its own peer address.
    for address in reversed(chain):
        if not _in_trusted_proxy(address):
            return str(address)
    return peer


def _local_bucket(
    window_key: str, client_key: str, now: float, window_seconds: int,
    capacity: int,
) -> deque[float]:
    """Return a bounded local bucket, reclaiming expired client entries first."""
    capacity = max(0, int(capacity))
    if capacity == 0:
        _WINDOWS.clear()
        raise HTTPException(503, "Rate limiting is unavailable")

    bucket = _WINDOWS[window_key]
    stamps = bucket.get(client_key)
    if stamps is not None:
        return stamps
    if len(bucket) >= capacity:
        stale_before = now - window_seconds * 4
        for old_key, old_stamps in list(bucket.items()):
            if not old_stamps or old_stamps[-1] <= stale_before:
                del bucket[old_key]
        if len(bucket) >= capacity:
            raise HTTPException(
                429,
                "Rate limiter capacity reached",
                headers={"Retry-After": str(window_seconds)},
            )
    stamps = deque()
    bucket[client_key] = stamps
    return stamps


def rate_limit(window_key: str, limit: int, window_seconds: int):
    """Return a dependency that allows `limit` requests per `window_seconds`
    per client IP within the named window.

    No-op (returns without limiting) while ``RATE_LIMIT_ENABLED`` is false,
    so tests and local dev can disable it without code changes.
    """
    from app.core.config import settings

    async def dependency(request: Request) -> None:
        global _CLEANUP_TICK
        if not settings.RATE_LIMIT_ENABLED:
            return
        key = _client_key(request)
        now = time.time()
        # Redis distributed rate limiting (fast-path skip when disabled or breaker open)
        from app.services.redis_service import get_redis_client, is_redis_ready_fast
        r_client = await run_blocking(get_redis_client) if is_redis_ready_fast() else None
        if r_client:
            try:
                r_key = f"paxs:ratelimit:{window_key}:{key}"
                pipe = r_client.pipeline()
                pipe.zremrangebyscore(r_key, 0, now - window_seconds)
                pipe.zcard(r_key)
                pipe.zadd(r_key, {str(now): now})
                pipe.expire(r_key, window_seconds + 5)
                from app.services.redis_service import redis_circuit_call
                res = await run_blocking(redis_circuit_call, pipe.execute)
                current_count = res[1]
                if current_count >= limit:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Rate limit exceeded — retry in {window_seconds}s",
                        headers={"Retry-After": str(window_seconds)},
                    )
                return
            except HTTPException:
                raise
            except Exception as exc:
                log.debug("Redis rate limit fallback: %s", exc)

        with _LOCK:
            stamps = _local_bucket(
                window_key, key, now, window_seconds,
                settings.RATE_LIMIT_MAX_LOCAL_CLIENT_BUCKETS,
            )
            # Drop entries older than the window.
            while stamps and now - stamps[0] >= window_seconds:
                stamps.popleft()
            if len(stamps) >= limit:
                retry = int(window_seconds - (now - stamps[0])) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit exceeded — retry in {retry}s",
                    headers={"Retry-After": str(retry)},
                )
            stamps.append(now)
            # Opportunistic cleanup: drop empty per-client buckets so a
            # one-shot IP doesn't pin a timestamp forever. Runs on a counter
            # to avoid paying for it on every single request.
            _CLEANUP_TICK += 1
            if _CLEANUP_TICK % 256 == 0:
                for wk, bucket in list(_WINDOWS.items()):
                    for ck, dq in list(bucket.items()):
                        if not dq:
                            del bucket[ck]
                        elif dq and now - dq[-1] >= window_seconds * 4:
                            del bucket[ck]

    return dependency


def reset_rate_limits() -> None:
    """Clear all rate-limit state (used by tests)."""
    global _CLEANUP_TICK
    with _LOCK:
        _WINDOWS.clear()
        _CLEANUP_TICK = 0
