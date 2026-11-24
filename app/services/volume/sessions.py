"""Server-side session accounting and the viewer-mode decision log.

``_ACTIVE_SERVER_SESSIONS`` is a bounded map of live server-rendered sessions
(``_SESSION_TIMEOUT`` sweeps expired entries on every touch). ``log_mode_decision``
is the single sink the viewer-mode tests capture — it must stay on the
``paxs.volume`` logger.
"""

from __future__ import annotations

import hashlib
import logging
import time

from app.core.config import settings
from app.services.volume.cache import _VOL_CACHE, _VOL_CACHE_LOCK

log = logging.getLogger("paxs.volume")

__all__ = [
    "_ACTIVE_SERVER_SESSIONS",
    "_SESSION_TIMEOUT",
    "_estimated_vol_cache_mb",
    "_evict_expired_sessions_locked",
    "active_server_session_count",
    "estimated_vol_cache_mb",
    "log_mode_decision",
    "register_server_session",
    "release_server_session",
    "server_volume_shed_budget_mb",
]


_ACTIVE_SERVER_SESSIONS: dict[str, float] = {}


_SESSION_TIMEOUT = 300  # seconds


def _session_key(series_uid: str, viewer_identity: str | None) -> str:
    """Return a PHI-free registry key, preserving the legacy single-key form."""
    if viewer_identity is None:
        return series_uid
    raw_key = f"{viewer_identity}\0{series_uid}".encode("utf-8")
    return hashlib.sha256(raw_key).hexdigest()


def _session_limit() -> int:
    """Read the configured session cap, where zero disables this counter."""
    return max(0, int(settings.HYBRID_MAX_SERVER_MPR_SESSIONS))


def register_server_session(
    series_uid: str, viewer_identity: str | None = None
) -> None:
    """Mark a caller/series MPR context active, bounded by the configured cap."""
    limit = _session_limit()
    with _VOL_CACHE_LOCK:
        if limit == 0:
            _ACTIVE_SERVER_SESSIONS.clear()
            return
        _evict_expired_sessions_locked()
        _trim_sessions_locked(limit)
        key = _session_key(series_uid, viewer_identity)
        if key not in _ACTIVE_SERVER_SESSIONS:
            while len(_ACTIVE_SERVER_SESSIONS) >= limit:
                oldest = min(_ACTIVE_SERVER_SESSIONS, key=_ACTIVE_SERVER_SESSIONS.get)
                _ACTIVE_SERVER_SESSIONS.pop(oldest, None)
        _ACTIVE_SERVER_SESSIONS[key] = time.monotonic()


def release_server_session(
    series_uid: str, viewer_identity: str | None = None
) -> None:
    """Remove a caller/series MPR context, if it is still registered."""
    with _VOL_CACHE_LOCK:
        _ACTIVE_SERVER_SESSIONS.pop(_session_key(series_uid, viewer_identity), None)


def active_server_session_count() -> int:
    """Return the number of non-expired server-side MPR sessions."""
    with _VOL_CACHE_LOCK:
        limit = _session_limit()
        if limit == 0:
            _ACTIVE_SERVER_SESSIONS.clear()
            return 0
        _evict_expired_sessions_locked()
        _trim_sessions_locked(limit)
        return len(_ACTIVE_SERVER_SESSIONS)


def _evict_expired_sessions_locked() -> None:
    """Remove entries older than ``_SESSION_TIMEOUT``.  Caller must hold lock."""
    now = time.monotonic()
    expired = [k for k, ts in _ACTIVE_SERVER_SESSIONS.items()
               if now - ts > _SESSION_TIMEOUT]
    for k in expired:
        _ACTIVE_SERVER_SESSIONS.pop(k, None)


def _trim_sessions_locked(limit: int) -> None:
    """Drop least-recently-touched keys until the configured cap is met."""
    while len(_ACTIVE_SERVER_SESSIONS) > limit:
        oldest = min(_ACTIVE_SERVER_SESSIONS, key=_ACTIVE_SERVER_SESSIONS.get)
        _ACTIVE_SERVER_SESSIONS.pop(oldest, None)


def estimated_vol_cache_mb() -> int:
    """Estimated total size of the in-process materialised volume cache (MB).

    Public name used by the hybrid engine (§7.1) and the load headers (§7.3).
    """
    with _VOL_CACHE_LOCK:
        return sum(v.nbytes for v in _VOL_CACHE.values()) // (1024 * 1024)


def server_volume_shed_budget_mb() -> int:
    """Return the effective policy threshold, clamped by the hard L1 cache cap.

    Either zero disables cache-based shedding. Clamping avoids a policy budget
    above the actual cache ceiling, a threshold that could never be reached.
    """
    policy_budget = max(0, int(settings.HYBRID_SERVER_VOL_BUDGET_MB))
    cache_budget = max(0, int(settings.VOLUME_CACHE_BUDGET_MB))
    if not policy_budget or not cache_budget:
        return 0
    return min(policy_budget, cache_budget)


_estimated_vol_cache_mb = estimated_vol_cache_mb


def log_mode_decision(
    series_uid: str,
    configured_mode: str,
    effective_mode: str,
    role: str | None,
    reason: str,
) -> None:
    """Emit one structured audit line for a viewer mode negotiation (§5.4).

    Deliberately narrow: series UID, mode vocabulary, role name and a
    machine-greppable reason token.  **No patient identifiers** (name, MRN,
    birth date) and **no filesystem paths** are ever passed here, because this
    fires on every viewer open and the application log is not a clinical
    record — writing PHI into it would create an uncontrolled copy outside the
    audit trail's own retention and access rules.

    A ``logging`` record is also not the place for the raw series UID at DEBUG
    verbosity, so this is a single INFO line with no ``extra`` payload.
    """
    log.info(
        "Viewer mode negotiated: series=%s, configured=%s, effective=%s, "
        "role=%s, reason=%s",
        series_uid,
        configured_mode,
        effective_mode,
        role or "anonymous",
        reason,
    )
