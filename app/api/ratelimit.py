"""Lightweight in-memory rate limiting (no Redis required).

A sliding-window counter keyed by client IP. Kept deliberately simple and
dependency-free; swap for slowapi/redis when running multiple API workers.

Usage as a FastAPI dependency:

    from app.api.ratelimit import rate_limit
    @router.post("/login", dependencies=[Depends(rate_limit("auth", 10, 60))])
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request

log = logging.getLogger("paxs.ratelimit")

# window_key -> {client_key: deque[timestamps]}
_WINDOWS: dict[str, dict[str, deque[float]]] = defaultdict(dict)
_LOCK = Lock()
_CLEANUP_TICK = 0


def _trusted_proxies() -> set[str]:
    """Peers whose X-Forwarded-For may be believed (H07).

    Defaults to loopback: a reverse proxy running on the same host. Any other
    peer's header is ignored, because a client that can set it could otherwise
    rotate the value per request and never hit the cap.
    """
    from app.core.config import settings

    raw = getattr(settings, "TRUSTED_PROXIES", "") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def _client_key(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and peer in _trusted_proxies():
        # Left-most entry is the original client as seen by the trusted proxy.
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return peer


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
        # Redis distributed rate limiting (fallback to in-memory deque if Redis is down)
        from app.services.redis_service import get_redis_client
        r_client = get_redis_client()
        if r_client:
            try:
                r_key = f"paxs:ratelimit:{window_key}:{key}"
                pipe = r_client.pipeline()
                pipe.zremrangebyscore(r_key, 0, now - window_seconds)
                pipe.zcard(r_key)
                pipe.zadd(r_key, {str(now): now})
                pipe.expire(r_key, window_seconds + 5)
                res = pipe.execute()
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
            bucket = _WINDOWS[window_key]
            stamps = bucket.setdefault(key, deque())
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
