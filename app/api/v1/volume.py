"""2D MPR backend prep (roadmap #26) — volume metadata + raw volume streaming.

The WebGL MPR frontend consumes two endpoints:

  GET /api/v1/volume/{series_uid}/info   — geometry/encoding metadata
  GET /api/v1/volume/{series_uid}/data   — the raw voxel block (streamed)

`/data` supports:

  ?slices=<start>-<end>   stream a sub-range (0-indexed, inclusive) — lets a
                          viewer progressively load a large stack
  ?rescale=1              server-side rescale to float32 (HU for CT) via each
                          slice's RescaleSlope/RescaleIntercept
  ?format=npy             numpy .npy container instead of raw little-endian

Auth matches the DICOMweb routers: a valid bearer token scopes the caller to
their tenant (VNA), guests stay readable (the viewer is same-origin).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    SHARE_COOKIE_NAME,
    SHARE_TOKEN_HEADER,
    get_share_scope,
    require_series_access,
    require_series_read,
    tenant_scope,
)
from app.core.config import settings
from app.core.concurrency import run_blocking
from app.core.response_admission import (
    AdmittedStreamingResponse,
    release_response_bytes,
    reserve_response_bytes,
)
from app.db.models import User
from app.db.session import get_db
from app.services import volume_service
from app.services.volume.geometry_validation import GeometryValidationError
from app.services.volume.slices import VolumeVariant

log = logging.getLogger("paxs.api.volume")

router = APIRouter(prefix="/api/v1/volume", tags=["Volume"])


def _geometry_http_exception(error: GeometryValidationError) -> HTTPException:
    """Translate rejected MPR geometry to a stable, PHI-free HTTP contract."""
    return HTTPException(
        status_code=422,
        detail={"code": error.code.value, "reason": error.detail},
    )


# ---------------------------------------------------------------------------
# Processing mode negotiation (plans/legacy/archive-notes/plan_viewer_process.md §5.1, Phase 1)
# ---------------------------------------------------------------------------

_VALID_MODES = ("server", "client", "hybrid")


def _viewer_session_identity(request: Request, user: User | None) -> str:
    """Identify a viewer scope without storing a share token or trusting proxy headers."""
    if user is not None:
        return f"user:{user.id}"
    share_token = request.headers.get(SHARE_TOKEN_HEADER) or request.cookies.get(SHARE_COOKIE_NAME)
    if share_token:
        digest = hashlib.sha256(share_token.encode("utf-8")).hexdigest()
        return f"share:{digest}"
    peer = request.client.host if request.client is not None else "unknown"
    return f"peer:{peer}"


def _global_mode() -> str:
    """The configured global mode, coerced to a known value."""
    mode = (settings.VIEWER_PROCESSING_MODE or "hybrid").lower().strip()
    return mode if mode in _VALID_MODES else "hybrid"


def _normalise_role_map(parsed: dict[object, object]) -> dict[str, str]:
    """Keep only string roles mapped to known viewer processing modes."""
    role_map: dict[str, str] = {}
    for role, mode in parsed.items():
        if not isinstance(role, str) or not isinstance(mode, str):
            continue
        normalised_mode = mode.lower().strip()
        if normalised_mode in _VALID_MODES:
            role_map[role.upper().strip()] = normalised_mode
    return role_map


def parse_role_map() -> dict[str, str]:
    """Parse ``VIEWER_PROCESSING_ROLE_MAP`` into an upper-cased role→mode dict.

    Never raises:  the setting is free-text an administrator types into a JSON
    textarea, so a typo, a trailing comma or an empty string is an expected
    condition rather than a server fault.  Anything unusable degrades to ``{}``
    (no overrides → the global mode applies), with a single warning so the
    misconfiguration is still visible in the log.

    Only entries whose value is a known mode are kept, so a map like
    ``{"RADIOLOGIST": "clinet"}`` cannot pin a role to a mode the rest of the
    system does not understand.
    """
    raw = settings.VIEWER_PROCESSING_ROLE_MAP
    if not raw or not str(raw).strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        # Log the *problem*, never the whole value verbatim at a level that
        # could carry a credential; a role map holds no PHI by construction but
        # an operator may paste something they should not have.
        log.warning(
            "VIEWER_PROCESSING_ROLE_MAP is not valid JSON — ignoring per-role "
            "overrides and using the global mode"
        )
        return {}
    if not isinstance(parsed, dict):
        log.warning(
            "VIEWER_PROCESSING_ROLE_MAP must be a JSON object of role→mode — "
            "ignoring per-role overrides"
        )
        return {}

    return _normalise_role_map(parsed)


def resolve_role_override(user: User | None) -> str | None:
    """Return the mode this user's role is pinned to, or ``None``.

    ``None`` means "no override applies" — either the map is unset/empty, the
    user is anonymous, their role is absent, or their role is not mapped.  All
    four cases are the same decision (fall through to the global/hybrid
    resolution), so they deliberately share one return value.
    """
    if user is None:
        return None
    role_map = parse_role_map()
    if not role_map:
        return None
    role = (getattr(user, "role", None) or "")
    if not isinstance(role, str) or not role.strip():
        return None
    return role_map.get(role.upper().strip())


def _resolve_mode(user: User | None) -> str:
    """Return the base processing mode after applying per-role overrides.

    Kept as the single source of truth for the *endpoint guards* (which only
    need the answer).  ``resolve_mode_detail`` returns the reasoning alongside
    it for the negotiate payload and the audit log.
    """
    return resolve_mode_detail(user)["mode"]


def resolve_mode_detail(user: User | None) -> dict:
    """Resolve the base mode *and* explain how it was reached.

    Returns ``{"mode", "override", "global_mode", "role"}``:

      * ``mode``        — the base mode after any override (one of the 3 modes).
      * ``override``    — the mapped mode when a role override applied, else None.
      * ``global_mode`` — the configured mode before any override.
      * ``role``        — the caller's role (never a patient attribute), or None.

    Splitting the explanation out of ``_resolve_mode`` keeps the guard call
    sites unchanged while letting the negotiate endpoint disclose *why* a mode
    was chosen — which is the difference between a support ticket and a fix.
    """
    global_mode = _global_mode()
    role = (getattr(user, "role", None) or "") if user is not None else None
    override = resolve_role_override(user)
    mode = override if override else global_mode
    return {
        "mode": mode,
        "override": override,
        "global_mode": global_mode,
        "role": role or None,
    }


# The score below which a browser is too weak to resample anything locally, even
# at preview resolution.  20 is the plan's floor for "remotely capable" (§7.1):
# below it WebGL2 exists but a downsample=4 preview would still stall on every
# window/level change, so those clients are better off on the server pipeline.
HYBRID_CLIENT_MIN_SCORE = 20

# Modes the hybrid engine may resolve to, in descending order of preference.
HYBRID_MODES = ("client", "split", "server")


def resolve_hybrid_mode(
    webgl_score: int,
    rtt_ms: int,
    *,
    active_server_sessions: int | None = None,
    server_vol_cache_mb: int | None = None,
    gpu_vram_mb: int = 0,
    hardware_concurrency: int = 0,
) -> str:
    """Resolve the effective processing mode for a hybrid session (§7.1).

    Returns one of ``"client"``, ``"split"`` or ``"server"``.

    Decision order matters and is the whole point of the engine:

    1. **No WebGL2 at all** → ``server``.  A browser that cannot make a 3D
       texture cannot participate in either local pipeline, so the question is
       already answered.
    2. **Server overloaded** → ``client`` when the browser is at least minimally
       capable, else ``server``.  Shedding server RAM onto a client that cannot
       carry it would just move the stall, not remove it.
    3. **Strong client** (score ≥ ``HYBRID_CLIENT_SCORE_THRESHOLD``) → ``client``.
    4. **High latency** (RTT > ``HYBRID_RTT_THRESHOLD_MS``) on a capable client
       → ``client``.  A slow link punishes *every* server-rendered slice, so a
       merely adequate GPU is still the faster path.
    5. **Marginal client** (``HYBRID_CLIENT_MIN_SCORE`` ≤ score < threshold) →
       ``split``: a coarse local preview for orientation, server-refined slices
       once the user settles.
    6. **Otherwise** → ``server``.

    ``active_server_sessions``/``server_vol_cache_mb`` default to live process
    state so callers cannot forget to pass them; the explicit arguments exist so
    tests can pin the load numbers deterministically.
    """
    if active_server_sessions is None:
        active_server_sessions = volume_service.active_server_session_count()
    if server_vol_cache_mb is None:
        server_vol_cache_mb = volume_service.estimated_vol_cache_mb()

    # 1. No WebGL2 → the client cannot render anything locally.
    if webgl_score <= 0:
        return "server"

    # 2. Server overloaded → shed onto the client if it can carry the load.
    session_limit = max(0, settings.HYBRID_MAX_SERVER_MPR_SESSIONS)
    volume_limit_mb = volume_service.server_volume_shed_budget_mb()
    server_overloaded = (
        (session_limit > 0 and active_server_sessions >= session_limit)
        or (volume_limit_mb > 0 and server_vol_cache_mb >= volume_limit_mb)
    )
    if server_overloaded:
        return "client" if webgl_score >= HYBRID_CLIENT_MIN_SCORE else "server"

    # 3. Strong client — full local pipeline.
    if webgl_score >= settings.HYBRID_CLIENT_SCORE_THRESHOLD:
        return "client"

    # 4. High latency — prefer local GPU work over per-slice round-trips.
    if rtt_ms > settings.HYBRID_RTT_THRESHOLD_MS and webgl_score >= HYBRID_CLIENT_MIN_SCORE:
        return "client"

    # 5. Marginal client — coarse local preview + server-refined fine slices.
    if HYBRID_CLIENT_MIN_SCORE <= webgl_score < settings.HYBRID_CLIENT_SCORE_THRESHOLD:
        return "split"

    # 6. Default.
    return "server"


def _negotiate_hybrid(webgl_score: int, rtt_ms: int) -> str:
    """Backwards-compatible thin wrapper over :func:`resolve_hybrid_mode`.

    Kept so Phase 1 call sites and tests that only care about the
    client/server decision keep working unchanged.
    """
    return resolve_hybrid_mode(webgl_score, rtt_ms)


# ---------------------------------------------------------------------------
# Server-mode endpoint guards (plans/legacy/archive-notes/plan_viewer_process.md §5.2 and §8 Phase 2)
#
# Endpoints are never hard-blocked.  A rejected request returns 503 plus a
# machine-readable X-PAXS-Mode-Hint header so the viewer can re-route itself,
# and `?force=1` always bypasses the guard for diagnostics — a misconfigured
# deployment must never be unable to fetch raw voxels it is authorized for.
# ---------------------------------------------------------------------------

SERVER_MODE_HINT = "server"
RAW_STREAMING_DISABLED = "Raw volume streaming is disabled in server processing mode"


def _server_mode_active(user: User | None) -> bool:
    """True when the *effective* mode is ``server`` for this caller.

    Uses :func:`_resolve_mode`, so a per-role override in
    ``VIEWER_PROCESSING_ROLE_MAP`` is honoured exactly as it is by the
    negotiation endpoint.  ``hybrid`` deliberately does NOT trip the guard:
    the hybrid client decides for itself and may legitimately stream raw
    voxels, so only an explicit server resolution disables the endpoint.
    """
    return _resolve_mode(user) == "server"


def _force_requested(request: Request) -> bool:
    """``?force=1`` (also ``true``/``yes``) bypasses a mode guard."""
    return str(request.query_params.get("force", "")).strip().lower() in (
        "1", "true", "yes",
    )


def _reject_raw_streaming(series_uid: str, endpoint: str) -> HTTPException:
    """The 503 raised by raw voxel endpoints while server mode is active."""
    return HTTPException(
        status_code=503,
        detail=RAW_STREAMING_DISABLED,
        headers={
            "X-PAXS-Mode-Hint": SERVER_MODE_HINT,
            "Cache-Control": "no-store",
        },
    )


def _mode_hint_headers(mode: str) -> dict[str, str]:
    """Headers echoed on server-mode responses (including successes)."""
    return {"X-PAXS-Mode-Hint": mode}


# ---------------------------------------------------------------------------
# B-10 — the two explicit volume budgets
#
# `VOLUME_MAX_SLICES` bounds the *slice count*, which is not a size: 4096 slices
# of a 512x512 16-bit volume is ~2 GiB, and `/data` joined every chunk into one
# `bytes` object — a single request could allocate and hold the whole thing.
# `/tiles` had no cap of its own at all, so `range=0-4095` rendered thousands of
# PNGs in one call. Both budgets are checked from *declared* values, before any
# buffer is allocated or any frame is rendered.
# ---------------------------------------------------------------------------

def enforce_volume_byte_cap(payload_len: int) -> None:
    """413 when the response body would exceed ``VOLUME_MAX_BYTES``.

    Called with the exact declared length (``Content-Length``), so the refusal
    is deterministic rather than a function of how the body happens to be built.
    413 is the honest status: the request is well-formed, it just asks for more
    than this node will materialise. ``0`` disables the cap (N-5: an explicit
    zero means disabled, and is never silently replaced by a default).
    """
    max_bytes = int(getattr(settings, "VOLUME_MAX_BYTES", 0) or 0)
    if max_bytes and payload_len > max_bytes:
        raise HTTPException(
            413,
            f"Requested volume payload is {payload_len} bytes, above "
            f"VOLUME_MAX_BYTES ({max_bytes}). Reduce the slice range or "
            f"increase downsample.",
        )


def enforce_tile_cap(count: int) -> None:
    """413 when a ``/tiles`` range asks for more than ``VOLUME_MAX_TILES``.

    A tile is a reslice plus a PNG encode plus its share of the concatenated
    response — far more expensive per unit than a raw slice, which is why it
    gets its own budget instead of inheriting ``VOLUME_MAX_SLICES``.
    """
    max_tiles = int(getattr(settings, "VOLUME_MAX_TILES", 0) or 0)
    if max_tiles and count > max_tiles:
        raise HTTPException(
            413,
            f"Requested {count} tiles, above VOLUME_MAX_TILES ({max_tiles}). "
            "Narrow the range or request fewer frames.",
        )


# ---------------------------------------------------------------------------
# Server load reporting (plans/legacy/archive-notes/plan_viewer_process.md §7.3, Phase 4 step 4.2)
#
# Every volume endpoint advertises the server's current load so a hybrid client
# can re-evaluate mid-session (e.g. stop promoting secondary viewports onto the
# server pipeline when it is saturated) without a separate metrics request.
# The values are *ratios* (`<current>/<limit>`) rather than bare integers so the
# client needs no knowledge of this server's configured budget to interpret them.
# ---------------------------------------------------------------------------

LOAD_HEADERS_EXPOSED = (
    "X-PAXS-Server-Load, X-PAXS-Vol-Cache-MB, X-PAXS-Mode-Hint, "
    "X-PAXS-Processing-Mode, X-PAXS-Fallback, Access-Control-Expose-Headers"
)


def server_load_headers() -> dict[str, str]:
    """Current server load as ``<active>/<max>`` and ``<mb>/<budget>`` ratios."""
    active = volume_service.active_server_session_count()
    cache_mb = volume_service.estimated_vol_cache_mb()
    volume_limit_mb = volume_service.server_volume_shed_budget_mb()
    return {
        "X-PAXS-Server-Load": f"{active}/{settings.HYBRID_MAX_SERVER_MPR_SESSIONS}",
        "X-PAXS-Vol-Cache-MB": f"{cache_mb}/{volume_limit_mb}",
    }


def server_overloaded() -> bool:
    """True when the server is at/over either hybrid load limit.

    Used by the mid-session re-evaluation path: a saturated server must not be
    handed *additional* work even though the session already negotiated.
    """
    session_limit = max(0, settings.HYBRID_MAX_SERVER_MPR_SESSIONS)
    volume_limit_mb = volume_service.server_volume_shed_budget_mb()
    return (
        (session_limit > 0 and volume_service.active_server_session_count() >= session_limit)
        or (
            volume_limit_mb > 0
            and volume_service.estimated_vol_cache_mb() >= volume_limit_mb
        )
    )


# ---------------------------------------------------------------------------
# Client-mode endpoint guards (plans/legacy/archive-notes/plan_viewer_process.md §3.2 and §8 Phase 3)
#
# The mirror image of the server-mode guard above.  In explicit ``client`` mode
# the workstation owns reslicing and projection, so the server-side slicers are
# disabled to keep the thin-server contract honest: a client that silently fell
# back to /mpr/slice would look like it was working while quietly doing all its
# work on the server, defeating the whole point of the mode.
#
# As with server mode, nothing is hard-blocked — the 503 carries
# ``X-PAXS-Mode-Hint: client`` so the viewer can re-route, and ``?force=1``
# always bypasses for diagnostics.  ``hybrid`` never trips this guard: a hybrid
# session legitimately uses both pipelines.
# ---------------------------------------------------------------------------

CLIENT_MODE_HINT = "client"
SERVER_SLICING_DISABLED = "Server-side slicing is disabled in client processing mode"


def _client_mode_active(user: User | None) -> bool:
    """True when the *effective* mode is explicitly ``client`` for this caller.

    ``hybrid`` deliberately does NOT trip the guard — the hybrid client decides
    per request and may legitimately call the server slicers, so only an
    explicit client resolution disables them.
    """
    return _resolve_mode(user) == "client"


def _reject_server_slicing(endpoint: str) -> HTTPException:
    """The 503 raised by server-side slicing endpoints while client mode is active."""
    return HTTPException(
        status_code=503,
        detail=SERVER_SLICING_DISABLED,
        headers={
            "X-PAXS-Mode-Hint": CLIENT_MODE_HINT,
            "Cache-Control": "no-store",
        },
    )


def _mode_endpoints(mode: str, series_uid: str, variant: VolumeVariant = "auto") -> dict:
    """URL map the frontend should use for the resolved mode."""
    base = f"/api/v1/volume/{series_uid}"
    query = f"?variant={variant}" if variant != "auto" else ""
    common = {"info": f"{base}/info{query}", "negotiate": f"{base}/negotiate{query}"}
    if mode == "server":
        return {
            **common,
            "mpr_slice": f"{base}/mpr/slice{query}",
            "mip": f"{base}/mip{query}",
            "rendered": f"{base}/mpr/slice{query}{'&' if query else '?'}format=png",
        }
    if mode == "client":
        return {**common, "data": f"{base}/data{query}"}
    # hybrid / split — expose everything (a split session legitimately uses both
    # the coarse raw stream and the server-refined slice endpoint).
    return {
        **common,
        "data": f"{base}/data{query}",
        "mpr_slice": f"{base}/mpr/slice{query}",
        "mip": f"{base}/mip{query}",
    }


def _mode_capabilities(mode: str) -> dict:
    """Describe what the frontend should expect from the resolved mode."""
    if mode == "server":
        return {
            "client_webgl_required": False,
            "server_renders_slices": True,
            "raw_volume_stream": False,
            "gpu_mpr": False,
            "gpu_mip": False,
            "split_preview": False,
        }
    if mode == "client":
        return {
            "client_webgl_required": True,
            "server_renders_slices": False,
            "raw_volume_stream": True,
            "gpu_mpr": True,
            "gpu_mip": True,
            "split_preview": False,
        }
    if mode == "split":
        # Coarse local preview + server-refined fine slices (§7.2).
        return {
            "client_webgl_required": True,
            "server_renders_slices": True,
            "raw_volume_stream": True,
            "gpu_mpr": False,
            "gpu_mip": False,
            "split_preview": True,
        }
    # hybrid — both pipelines available
    return {
        "client_webgl_required": False,
        "server_renders_slices": True,
        "raw_volume_stream": True,
        "gpu_mpr": True,
        "gpu_mip": True,
        "split_preview": True,
    }


@router.get("/{series_uid}/negotiate", response_class=JSONResponse)
async def volume_negotiate_endpoint(
    series_uid: str,
    request: Request,
    webgl_score: int = Query(default=0, ge=0, le=100),
    rtt_ms: int = Query(default=0, ge=0, le=600000),
    variant: VolumeVariant = "auto",
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_series_read),
):
    """Negotiate the viewer processing mode for a series.

    Returns the resolved mode (server/client/hybrid) along with the
    endpoint URLs and capabilities the frontend should use.  If the mode
    is ``hybrid``, the response includes the thresholds the client should
    use for its capability self-assessment.

    Query params (client reports)::

      ?webgl_score=N             client's WebGL capability score (0-100)
      ?rtt_ms=N                  measured RTT to this server in ms
      ?gpu_vram_mb=N             estimated GPU VRAM in MB (informational)
      ?hardware_concurrency=N    navigator.hardwareConcurrency (informational)
      ?variant=auto|original|lossy  stack selection carried by returned URLs
    """
    if not settings.VOLUME_STREAMING_ENABLED:
        raise HTTPException(503, "Volume streaming is disabled")

    await require_series_access(
        db, series_uid, user,
        share_scope=await get_share_scope(request),
    )

    # 1. Resolve base mode (global setting + per-role override).
    #    `resolve_mode_detail` also reports *why*, which drives the
    #    `role_override` payload field and the audit log below.
    resolution = resolve_mode_detail(user)
    mode = resolution["mode"]

    # 2. Hybrid negotiation — the dynamic decision engine (§7.1).
    #    A role mapped explicitly to "hybrid" is permitted to run the normal
    #    dynamic scoring (step 5.1): pinning a role to hybrid means "let the
    #    engine decide for this user", not "pin to a fixed pipeline".
    effective_mode = mode
    if mode == "hybrid":
        effective_mode = resolve_hybrid_mode(webgl_score, rtt_ms)

    # 3. Build response
    #    `capabilities` is derived from the *configured* mode for a hybrid
    #    session: split mode needs both pipelines, so advertising them from the
    #    resolved mode alone would tell a split client it has no coarse stream.
    capability_mode = mode if mode == "hybrid" else effective_mode

    body: dict = {
        "configured_mode": mode,
        "effective_mode": effective_mode,
        "series_uid": series_uid,
        "endpoints": _mode_endpoints(effective_mode, series_uid, variant),
        "capabilities": _mode_capabilities(capability_mode),
    }
    # Step 5.1 — disclose the override so a user can tell "my role pinned this"
    # apart from "the engine chose this", which are very different support cases.
    if resolution["override"]:
        body["role_override"] = {
            "role": resolution["role"],
            "mapped_mode": resolution["override"],
            "global_mode": resolution["global_mode"],
        }
    if mode == "hybrid":
        body["hybrid_thresholds"] = {
            "client_score_threshold": settings.HYBRID_CLIENT_SCORE_THRESHOLD,
            "min_client_score": HYBRID_CLIENT_MIN_SCORE,
            "rtt_threshold_ms": settings.HYBRID_RTT_THRESHOLD_MS,
            "server_vol_budget_mb": settings.HYBRID_SERVER_VOL_BUDGET_MB,
            "effective_server_vol_budget_mb": volume_service.server_volume_shed_budget_mb(),
            "max_server_sessions": settings.HYBRID_MAX_SERVER_MPR_SESSIONS,
        }
        body["hybrid_decision"] = {
            "webgl_score": webgl_score,
            "rtt_ms": rtt_ms,
            "resolved": effective_mode,
        }

    # Step 5.4 — structured audit trail. Series UID + role + mode vocabulary
    # only: no patient identifiers and no filesystem paths ever reach the log,
    # because a negotiate fires on every viewer open and the log is not a
    # clinical record.
    volume_service.log_mode_decision(
        series_uid=series_uid,
        configured_mode=mode,
        effective_mode=effective_mode,
        role=resolution["role"],
        reason=_negotiation_reason(resolution, webgl_score, rtt_ms, effective_mode),
    )

    headers = {
        "X-PAXS-Processing-Mode": effective_mode,
        "Cache-Control": "no-store",
        "Access-Control-Expose-Headers": LOAD_HEADERS_EXPOSED,
    }
    if resolution["override"]:
        headers["X-PAXS-Role-Override"] = resolution["role"] or ""
    headers.update(server_load_headers())
    return JSONResponse(body, headers=headers)


def _negotiation_reason(
    resolution: dict, webgl_score: int, rtt_ms: int, effective_mode: str
) -> str:
    """A short, PHI-free explanation of how ``effective_mode`` was reached.

    Kept terse and machine-greppable: an operator scanning the log for "why is
    this workstation on the server pipeline" can filter on the reason token
    without parsing prose.
    """
    if resolution["override"]:
        return f"role_override:{resolution['role']}->{resolution['override']}"
    if resolution["mode"] != "hybrid":
        return f"global:{resolution['mode']}"
    return f"hybrid:score={webgl_score},rtt={rtt_ms}ms->{effective_mode}"


@router.get("/{series_uid}/info", response_class=JSONResponse)
async def volume_info_endpoint(
    series_uid: str,
    request: Request,
    variant: VolumeVariant = "auto",
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_series_read),
):
    """Volume geometry/encoding metadata for MPR reconstruction.

    Auto uses the full series unless verified lossy copies overlap originals.
    Explicit variants select a verified original or lossy copy group.
    """
    if not settings.VOLUME_STREAMING_ENABLED:
        raise HTTPException(503, "Volume streaming is disabled")
    # C04: a series UID alone must not reach across institutions.
    await require_series_access(db, series_uid, user,
                                share_scope=await get_share_scope(request))
    tenant_id, include_all = (None, True) if user is None else tenant_scope(user)
    try:
        info = await volume_service.volume_info(
            db, series_uid, tenant_id=tenant_id, include_all=include_all, variant=variant
        )
    except GeometryValidationError as exc:
        raise _geometry_http_exception(exc) from None
    if info is None:
        raise HTTPException(404, "No readable volumetric series found")
    # §7.3 — /info is the probe endpoint the viewer already polls, so it is the
    # natural carrier for the load signal the hybrid engine re-reads mid-session.
    headers = {"Access-Control-Expose-Headers": LOAD_HEADERS_EXPOSED}
    headers.update(server_load_headers())
    response_info = dict(info)
    response_info["client_mpr_limits"] = {
        "max_slices": settings.VOLUME_MPR_CLIENT_MAX_SLICES,
        "max_volume_bytes": settings.VOLUME_MPR_CLIENT_MAX_VOLUME_MB * 1024 * 1024,
        "target_slices": settings.VOLUME_MPR_CLIENT_OVERVIEW_TARGET_SLICES,
        "max_downsample": volume_service.MAX_DOWNSAMPLE,
    }
    response_info["server_mpr_overview"] = {
        "downsample": settings.VOLUME_MPR_OVERVIEW_DOWNSAMPLE,
        "max_primary_slices": settings.VOLUME_MPR_CLIENT_MAX_SLICES,
    }
    return JSONResponse(response_info, headers=headers)


def _stream_headers(plan: volume_service.VolumeStreamPlan, mode: str) -> dict[str, str]:
    """Response headers for a raw stream, from the plan's declared dimensions.

    One builder for both formats: raw and NPY differ only in the dtype label and
    in that ``mode=server`` adds the mode hint. Two copies of a nine-key dict is
    how the two branches drifted apart before (Q-6).
    """
    headers = {
        "Content-Length": str(plan.payload_len),
        "X-Volume-Slices": f"{plan.out_count}",
        "X-Volume-Rows": f"{plan.out_rows}",
        "X-Volume-Cols": f"{plan.out_cols}",
        "X-Volume-Downsample": f"{plan.downsample}",
        "X-Volume-Dtype": plan.dtype,
        "X-Volume-Derived-Resampled": "true" if plan.derived_resampled else "false",
        "X-Volume-Gantry-Tilt-Corrected": "true" if plan.gantry_tilt_corrected else "false",
        "Cache-Control": "private, no-store",
        # V-13: no Range support here, so advertise none (RFC 9110 §14.3).
        "Accept-Ranges": "none",
        "Access-Control-Expose-Headers": (
            "Content-Length, X-Volume-Slices, X-Volume-Rows, X-Volume-Cols, "
            "X-Volume-Downsample, X-Volume-Dtype, X-Volume-Derived-Resampled, "
            "X-Volume-Gantry-Tilt-Corrected, " + LOAD_HEADERS_EXPOSED
        ),
    }
    if plan.fmt == "raw" and mode == "server":
        headers.update(_mode_hint_headers(mode))
    # §7.3 — advertise server load on every volume response so a hybrid client
    # can re-evaluate mid-session without a dedicated metrics call.
    headers.update(server_load_headers())
    return headers


@router.get("/{series_uid}/data")
async def volume_data_endpoint(
    series_uid: str,
    request: Request,
    variant: VolumeVariant = "auto",
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_series_read),
) -> Response:
    """Stream the raw voxel block of a series (optionally a sub-range).

    In ``server`` processing mode the raw stream is disabled — the thin client
    renders from ``/mpr/slice`` and ``/tiles`` instead (plans/legacy/archive-notes/plan_viewer_process.md
    §5.2).  Pass ``?force=1`` to bypass the guard.

    Q-6: the request is *planned* by ``volume_service.plan_volume_stream`` —
    window, sampled geometry, exact payload length and cache keys — and rendered
    by ``render_volume_stream``. This handler owns only the HTTP surface: the ACL
    and mode guards, the byte cap, the headers and the cache-hit status.
    """
    if not settings.VOLUME_STREAMING_ENABLED:
        raise HTTPException(503, "Volume streaming is disabled")
    # C04: a series UID alone must not reach across institutions.
    await require_series_access(db, series_uid, user,
                                share_scope=await get_share_scope(request))

    # Phase 2 §5.2 — mode-aware guard. Raw voxel streaming is a client-mode
    # capability; in server mode the viewer must use the rendered endpoints.
    # Checked after the ACL so an unauthorized caller never learns the mode.
    mode = _resolve_mode(user)
    if mode == "server" and not _force_requested(request):
        raise _reject_raw_streaming(series_uid, "data")

    tenant_id, include_all = (None, True) if user is None else tenant_scope(user)
    try:
        plan = await volume_service.plan_volume_stream(
            db, series_uid,
            tenant_id=tenant_id,
            include_all=include_all,
            params=dict(request.query_params),
            max_slices=settings.VOLUME_MAX_SLICES,
            variant=variant,
        )
    except volume_service.VolumeStreamError as exc:
        raise HTTPException(exc.http_status, str(exc)) from None
    except GeometryValidationError as exc:
        raise _geometry_http_exception(exc) from None

    # B-10: the byte ceiling is checked against the declared body length before
    # the payload is built.
    enforce_volume_byte_cap(plan.payload_len)
    headers = _stream_headers(plan, mode)
    reserve_response_bytes(plan.payload_len)
    try:
        cached = await volume_service.read_cached_stream(plan)
    except BaseException:
        # A cancelled Redis lookup also releases the reservation before no
        # response object has taken ownership of it.
        release_response_bytes(plan.payload_len)
        raise
    if cached is not None:
        headers["X-Cache"] = "HIT"
        chunks = (
            cached[offset:offset + volume_service.STREAM_CHUNK_BYTES]
            for offset in range(0, len(cached), volume_service.STREAM_CHUNK_BYTES)
        )
    else:
        chunks = volume_service.render_volume_stream(plan)
    headers["Content-Length"] = str(plan.payload_len)
    try:
        return AdmittedStreamingResponse(
            chunks,
            media_type=plan.media_type,
            headers=headers,
            reserved_bytes=plan.payload_len,
        )
    except BaseException:
        release_response_bytes(plan.payload_len)
        raise


@router.get("/{series_uid}/mip")
async def volume_mip_endpoint(
    series_uid: str,
    request: Request,
    variant: VolumeVariant = "auto",
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_series_read),
):
    """3D VRT / MIP (roadmap #27) — server-side maximum-intensity projection.

    Projects the volume along a patient axis over a slab and returns one
    2D image:

      * ``axis=z|x|y`` — projection axis (z = axial stack, default)
      * ``offset=N`` — first stack slice of the slab (default 0)
      * ``thickness=N`` — slab depth in slices (default: rest of the stack)
      * ``mode=mip|minip|average|vrt`` — max (default) | min | mean | an
        alpha-composited volume render whose opacity ramp comes from the
        display window
      * ``window_center`` / ``window_width`` — override the display window
        (used for PNG rendering and the vrt opacity ramp)
      * ``format=png|raw|npy`` — PNG (8-bit W/L render, default) | raw float32
        Hounsfield projection (little-endian) | numpy .npy container

    Responses include ``X-MIP-Rows`` / ``X-MIP-Cols`` / ``X-MIP-Axis`` /
    ``X-MIP-Mode`` / ``X-MIP-Slab`` geometry headers.
    """
    if not settings.VOLUME_STREAMING_ENABLED:
        raise HTTPException(503, "Volume streaming is disabled")
    # C04: a series UID alone must not reach across institutions.
    await require_series_access(db, series_uid, user,
                                share_scope=await get_share_scope(request))

    # §3.2 / Phase 3: in explicit client mode the workstation projects the
    # volume itself, so refuse before materialising it — the whole point is to
    # keep the server lightweight. ACL is checked first so an unauthorized
    # caller still gets 403/404 rather than a mode hint that leaks existence.
    mip_mode = _resolve_mode(user)
    if mip_mode == "client" and not _force_requested(request):
        raise _reject_server_slicing("mip")

    tenant_id, include_all = (None, True) if user is None else tenant_scope(user)

    try:
        bundle = await volume_service.volume_bundle(
            db, series_uid, tenant_id=tenant_id, include_all=include_all, variant=variant
        )
    except GeometryValidationError as exc:
        raise _geometry_http_exception(exc) from None
    if bundle is None:
        raise HTTPException(404, "No readable volumetric series found")
    info, slices = bundle
    # Multi-frame / Enhanced instances are expanded into per-frame slices by
    # the service — projection works on the expanded stack.

    q = request.query_params
    fmt = str(q.get("format", "png")).lower()
    if fmt not in ("png", "raw", "npy"):
        raise HTTPException(400, "format must be 'png', 'raw' or 'npy'")

    axis = str(q.get("axis", "z")).lower()
    mode = str(q.get("mode", "mip")).lower()

    def _int_param(name: str, default: int | None) -> int | None:
        raw = q.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{name} must be an integer") from None

    offset = _int_param("offset", 0) or 0
    thickness = _int_param("thickness", None)
    wc = q.get("window_center")
    ww = q.get("window_width")
    wc_f = float(wc) if wc is not None else None
    ww_f = float(ww) if ww is not None else None

    # mip_project materializes the whole volume (disk reads + pydicom decode
    # + float32 stack) — run it in the thread pool so a heavy projection never
    # blocks the event loop for the node's other requests.
    try:
        result = await run_blocking(
            volume_service.mip_project,
            slices, info, axis=axis, mode=mode, offset=offset,
            thickness=thickness, window_center=wc_f, window_width=ww_f,
            tenant_id=tenant_id,
        )
    except HTTPException:
        raise
    except volume_service.VolumeAssemblyCapacityError as exc:
        raise HTTPException(503, str(exc)) from exc
    except IndexError as exc:
        raise HTTPException(416, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    center, width = volume_service.window_for(info, result.data, wc_f, ww_f)
    slab = f"{result.offset}:{result.offset + result.thickness}"
    geo = {
        "X-PAXS-Mode-Hint": mip_mode,
        "X-MIP-Rows": str(result.rows),
        "X-MIP-Cols": str(result.cols),
        "X-MIP-Axis": result.axis,
        "X-MIP-Mode": result.mode,
        "X-MIP-Slab": slab,
        "X-MIP-Spacing": ",".join(f"{v:.4f}" for v in result.spacing),
        "X-MIP-Window": f"{center:.1f},{width:.1f}",
        "X-MIP-Derived-Resampled": "true" if info.get("derived_resampled") else "false",
        "X-MIP-Gantry-Tilt-Corrected": "true" if info.get("gantry_tilt_corrected") else "false",
        "Cache-Control": "no-store",
        "Access-Control-Expose-Headers": (
            "Content-Length, X-PAXS-Mode-Hint, X-MIP-Rows, X-MIP-Cols, "
            "X-MIP-Axis, X-MIP-Mode, X-MIP-Slab, X-MIP-Spacing, X-MIP-Window, "
            "X-MIP-Derived-Resampled, X-MIP-Gantry-Tilt-Corrected, "
            + LOAD_HEADERS_EXPOSED
        ),
    }
    geo.update(server_load_headers())   # §7.3

    if fmt == "raw":
        payload = volume_service.nan_to_float32(result.data).tobytes()
        return Response(content=payload, media_type="application/octet-stream",
                        headers=geo | {"Content-Length": str(len(payload))})
    if fmt == "npy":
        arr = volume_service.nan_to_float32(result.data)
        header = volume_service.npy_header("float32", arr.shape)
        payload = header + arr.tobytes()
        return Response(content=payload, media_type="application/x-npy",
                        headers=geo | {"Content-Length": str(len(payload))})

    png = volume_service.render_plane_png(result.data, center, width)
    return Response(content=png, media_type="image/png",
                    headers=geo | {"Content-Length": str(len(png))})


@router.get("/{series_uid}/tiles")
async def volume_tiles_endpoint(
    series_uid: str,
    request: Request,
    variant: VolumeVariant = "auto",
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_series_read),
):
    """Batch pre-rendered slice frames for thin-client prefetching (plan §5.4).

    One round-trip returns a whole window of PNG slices, so a server-mode
    viewer can scroll without paying a request per slice:

      * ``plane=axial|sagittal|coronal`` — native plane (default ``axial``)
      * ``range=<start>-<end>`` — inclusive 0-indexed slice range, or a single
        ``N`` (also accepts ``N:M`` / ``all``)
      * ``window_center`` / ``window_width`` — display window applied to every
        frame; omitted → each frame keeps the series window
      * ``size=N`` — requested edge length in px (default 512)
      * ``inline=1`` — emit every frame as its own ``image/png`` part instead of
        the default compact ``X-PAXS-Tile-*`` header framing

    The default framing was chosen for the browser: ``multipart/mixed`` is not
    reliably readable by ``fetch()``, whereas a single ``application/octet-stream``
    body plus self-describing ``X-PAXS-Tile-Offset`` / ``X-PAXS-Tile-Length``
    header lists lets the thin client slice the buffer into individual PNGs with
    zero extra parsing.  The frame list is always in ascending index order.
    """
    if not settings.VOLUME_STREAMING_ENABLED:
        raise HTTPException(503, "Volume streaming is disabled")
    # C04: a series UID alone must not reach across institutions.
    await require_series_access(db, series_uid, user,
                                share_scope=await get_share_scope(request))
    tenant_id, include_all = (None, True) if user is None else tenant_scope(user)

    try:
        bundle = await volume_service.volume_bundle(
            db, series_uid, tenant_id=tenant_id, include_all=include_all, variant=variant
        )
    except GeometryValidationError as exc:
        raise _geometry_http_exception(exc) from None
    if bundle is None:
        raise HTTPException(404, "No readable volumetric series found")
    info, slices = bundle

    q = request.query_params
    plane = str(q.get("plane", "axial")).lower()
    if plane not in ("axial", "sagittal", "coronal"):
        raise HTTPException(400, "plane must be 'axial', 'sagittal' or 'coronal'")

    max_index = volume_service.plane_index_max(info, plane)
    started = q.get("range")
    if started is None:
        started = q.get("start")
    if started is None:
        # No range given → the whole plane, capped by the batch budget.
        start, end = 0, max_index
    else:
        text = str(q.get("range", started)).strip().replace(":", "-")
        # A negative start ("-2-3") splits into three empty-leading parts, so
        # re-join them instead of treating the request as malformed — the
        # bounds check below is what reports an out-of-range start.
        try:
            parts = [p for p in text.split("-") if p != ""]
            negative = text.startswith("-")
            if negative:
                parts = ["-" + parts[0]] + parts[1:]
            if len(parts) == 1:
                start = end = int(parts[0])
            elif len(parts) == 2:
                start, end = int(parts[0]), int(parts[1])
            else:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(
                400, "range must be '<start>-<end>' (0-indexed, inclusive)"
            ) from None

    if start < 0 or start > max_index:
        raise HTTPException(416, f"{plane} index {start} out of range 0-{max_index}")
    end = max(start, min(end, max_index))

    # B-10: an independent tile budget (see `enforce_tile_cap`).
    enforce_tile_cap(end - start + 1)

    def _float_param(name: str) -> float | None:
        raw = q.get(name)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{name} must be a number") from None

    wc_f = _float_param("window_center")
    ww_f = _float_param("window_width")

    try:
        size = int(q.get("size", 512) or 512)
    except (TypeError, ValueError):
        raise HTTPException(400, "size must be an integer") from None
    if size <= 0:
        raise HTTPException(400, "size must be a positive integer")

    # render_tiles materializes the volume (disk reads + decode + float32 stack)
    # and rasterizes up to VOLUME_MAX_SLICES PNGs — run it off the event loop.
    try:
        frames = await run_blocking(
            volume_service.render_tiles,
            series_uid, plane, start, end, wc_f, ww_f, size, slices, info,
            tenant_id,
        )
    except HTTPException:
        raise
    except volume_service.VolumeAssemblyCapacityError as exc:
        raise HTTPException(503, str(exc)) from exc
    except IndexError as exc:
        raise HTTPException(416, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Server-mode session bookkeeping (plan §5.3): a batch prefetch belongs to a
    # thin client that has no local volume, so it counts as an active MPR
    # session. Gated on the effective mode for the same reason as /mpr/slice —
    # off-mode prefetching must not inflate the hybrid load signal or leave
    # session entries nothing releases.
    tiles_mode = _resolve_mode(user)
    if tiles_mode == "server":
        volume_service.register_server_session(
            series_uid, _viewer_session_identity(request, user)
        )

    first, last = start, start + len(frames) - 1
    geo = {
        "X-PAXS-Mode-Hint": tiles_mode,
        "X-MPR-Plane": plane,
        "X-MPR-Range": f"{first}-{last}",
        "X-MPR-Tiles": str(len(frames)),
        "X-MPR-Rows": str(volume_service.plane_dims(info, plane)[0]),
        "X-MPR-Cols": str(volume_service.plane_dims(info, plane)[1]),
        "X-MPR-Spacing": ",".join(
            f"{v:.4f}" for v in volume_service.plane_spacing(info, plane)
        ),
        "X-MPR-Index-0": str(first),
        "X-MPR-Derived-Resampled": "true" if info.get("derived_resampled") else "false",
        "X-MPR-Gantry-Tilt-Corrected": "true" if info.get("gantry_tilt_corrected") else "false",
        "Cache-Control": "no-store",
    }
    if wc_f is not None and ww_f is not None:
        geo["X-MPR-Window"] = f"{wc_f:.1f},{ww_f:.1f}"

    inline = str(q.get("inline", "")).strip().lower() in ("1", "true", "yes")
    if inline:
        # Self-describing but heavier: one image/png part per frame.
        boundary = "paxs-tiles-%d-%d" % (first, last)
        parts: list[bytes] = []
        for i, frame in enumerate(frames):
            parts.append(
                ("--%s\r\nContent-Type: image/png\r\n"
                 "Content-Length: %d\r\n"
                 "X-MPR-Index: %d\r\n\r\n" % (boundary, len(frame), first + i)
                 ).encode("ascii")
            )
            parts.append(frame)
            parts.append(b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode("ascii"))
        payload = b"".join(parts)
        geo["Content-Type"] = f"multipart/mixed; boundary={boundary}"
        return Response(
            content=payload,
            media_type=f"multipart/mixed; boundary={boundary}",
            headers=geo | {"Content-Length": str(len(payload))},
        )

    # Default framing: concatenated PNGs plus explicit offset/length lists, so
    # the client slices the buffer without re-parsing PNG structure.
    payload = b"".join(frames)
    offsets: list[str] = []
    lengths: list[str] = []
    cursor = 0
    for frame in frames:
        offsets.append(str(cursor))
        lengths.append(str(len(frame)))
        cursor += len(frame)

    geo["X-PAXS-Tile-Offset"] = ",".join(offsets)
    geo["X-PAXS-Tile-Length"] = ",".join(lengths)
    geo.update(server_load_headers())   # §7.3
    geo["Access-Control-Expose-Headers"] = (
        "Content-Length, X-PAXS-Mode-Hint, X-MPR-Plane, X-MPR-Range, "
        "X-MPR-Tiles, X-MPR-Rows, X-MPR-Cols, X-MPR-Spacing, X-MPR-Window, "
        "X-MPR-Index-0, X-MPR-Derived-Resampled, X-MPR-Gantry-Tilt-Corrected, "
        "X-PAXS-Tile-Offset, X-PAXS-Tile-Length, "
        + LOAD_HEADERS_EXPOSED
    )
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers=geo | {"Content-Length": str(len(payload))},
    )


@router.get("/{series_uid}/mpr/slice")
async def volume_mpr_slice_endpoint(
    series_uid: str,
    request: Request,
    overview: bool = Query(default=False),
    variant: VolumeVariant = "auto",
    stack_fingerprint: Annotated[str | None, Query(pattern=r"^[0-9a-f]{16}$")] = None,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_series_read),
):
    """Server-side MPR slice reslicing — thin-client fallback for WebGL MPR.

    Reslices the volume on the fly and returns ONE rendered slice:

      * native planes: ``?plane=axial|sagittal|coronal&index=N``
      * oblique plane: ``?origin=x,y,z&row=dx,dy,dz&col=dx,dy,dz&size=WxH``
        (patient coordinates, mm; row/col are unit direction cosines; the
        plane grid is centred on origin with PixelSpacing-sized samples)
      * ``window_center`` / ``window_width`` — override the display window
      * ``format=png|raw|npy`` — PNG (8-bit W/L render, default) | raw float32
        Hounsfield block (little-endian) | numpy .npy container

    Responses include ``X-MPR-Rows`` / ``X-MPR-Cols`` geometry headers.
    """
    if not settings.VOLUME_STREAMING_ENABLED:
        raise HTTPException(503, "Volume streaming is disabled")
    # C04: a series UID alone must not reach across institutions.
    await require_series_access(db, series_uid, user,
                                share_scope=await get_share_scope(request))

    # §3.2 / Phase 3: in explicit client mode the workstation reslices from its
    # own WebGL2 3D texture, so refuse before materialising the volume. ACL is
    # checked first so an unauthorized caller still gets 403/404 rather than a
    # mode hint that leaks series existence.
    slice_mode = _resolve_mode(user)
    if slice_mode == "client" and not _force_requested(request):
        raise _reject_server_slicing("mpr/slice")

    tenant_id, include_all = (None, True) if user is None else tenant_scope(user)

    try:
        bundle = await volume_service.volume_bundle(
            db, series_uid, tenant_id=tenant_id, include_all=include_all, variant=variant
        )
    except GeometryValidationError as exc:
        raise _geometry_http_exception(exc) from None
    if bundle is None:
        raise HTTPException(404, "No readable volumetric series found")
    info, slices = bundle
    if stack_fingerprint is not None and stack_fingerprint != info.get("stack_fingerprint"):
        raise HTTPException(409, "The volume stack changed. Request current volume info.")
    # Multi-frame / Enhanced instances are expanded into per-frame slices by
    # the service — reslicing works on the expanded stack.

    q = request.query_params
    fmt = str(q.get("format", "png")).lower()
    if fmt not in ("png", "raw", "npy"):
        raise HTTPException(400, "format must be 'png', 'raw' or 'npy'")

    plane = str(q.get("plane", "axial")).lower()
    use_overview = (
        overview
        and plane in ("axial", "sagittal", "coronal")
        and info["slices"] > settings.VOLUME_MPR_CLIENT_MAX_SLICES
    )
    sample_step = (
        settings.VOLUME_MPR_OVERVIEW_DOWNSAMPLE if use_overview else 1
    )
    # reslice_plane materializes the volume (disk reads + decode + float32
    # stack + trilinear sampling) — offload to the thread pool so a slice
    # reslice never blocks the event loop for the node's other requests.
    try:
        if plane in ("axial", "sagittal", "coronal"):
            index = q.get("index")
            if index is None:
                raise HTTPException(400, f"index is required for plane={plane}")
            if plane == "axial" and stack_fingerprint is not None:
                # The verified stack already orders these native slices.
                # Its first preview only needs one decoded source frame.
                result = await run_blocking(
                    volume_service.reslice_native_plane_streamed,
                    slices, info, plane, int(index),
                    max_pixels=settings.VOLUME_MPR_MAX_PIXELS, downsample=sample_step,
                )
            else:
                result = await run_blocking(
                    volume_service.reslice_plane,
                    slices, info, plane, index=int(index),
                    tenant_id=tenant_id, downsample=sample_step,
                )
        else:
            origin = q.get("origin")
            row = q.get("row")
            col = q.get("col")
            if not (origin and row and col):
                raise HTTPException(
                    400, "oblique planes require origin=x,y,z&row=dx,dy,dz&col=dx,dy,dz"
                )
            size_raw = str(q.get("size", "")).lower().replace("x", ",")
            size = None
            if size_raw:
                parts = [p for p in size_raw.split(",") if p]
                try:
                    if len(parts) == 1:
                        n = int(parts[0])
                        size = (n, n)
                    elif len(parts) == 2:
                        size = (int(parts[0]), int(parts[1]))
                except (TypeError, ValueError):
                    raise HTTPException(400, "size must be WxH or a single N")
            result = await run_blocking(
                volume_service.reslice_plane,
                slices, info, "oblique",
                origin=[float(v) for v in origin.split(",")],
                row_vector=[float(v) for v in row.split(",")],
                col_vector=[float(v) for v in col.split(",")],
                size=size,
                max_pixels=settings.VOLUME_MPR_MAX_PIXELS,
                tenant_id=tenant_id,
            )
    except volume_service.VolumeAssemblyTooLargeError as exc:
        if plane not in ("axial", "sagittal", "coronal"):
            raise HTTPException(503, str(exc)) from exc
        index = q.get("index")
        try:
            result = await run_blocking(
                volume_service.reslice_native_plane_streamed,
                slices, info, plane, int(index),
                max_pixels=settings.VOLUME_MPR_MAX_PIXELS,
                downsample=sample_step,
            )
        except IndexError as plane_error:
            raise HTTPException(416, str(plane_error)) from plane_error
        except ValueError as plane_error:
            raise HTTPException(400, str(plane_error)) from plane_error
    except HTTPException:
        raise
    except volume_service.VolumeAssemblyCapacityError as exc:
        raise HTTPException(503, str(exc)) from exc
    except IndexError as exc:
        raise HTTPException(416, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if plane in ("axial", "sagittal", "coronal"):
        source_rows, source_cols = volume_service.plane_dims(info, plane)
        expected_shape = (
            (source_rows + sample_step - 1) // sample_step,
            (source_cols + sample_step - 1) // sample_step,
        )
        actual_shape = tuple(int(value) for value in result.data.shape)
        if (result.rows, result.cols) != expected_shape or actual_shape != expected_shape:
            raise HTTPException(500, "MPR sample factor and returned geometry disagree")

    wc = q.get("window_center")
    ww = q.get("window_width")
    wc_f = float(wc) if wc is not None else None
    ww_f = float(ww) if ww is not None else None
    try:
        center, width = volume_service.window_for(info, result.data, wc_f, ww_f)
    except (TypeError, ValueError):
        raise HTTPException(400, "window_center/window_width must be numbers") from None

    rows, cols = result.rows, result.cols
    # Server-mode session bookkeeping (plan §5.3): rasterizing a plane server-side
    # *is* server work, so the hybrid engine can see the real load. Registered
    # only when this session is actually being served in server mode — in
    # client/hybrid mode `/mpr/slice` is an occasional on-demand fetch, not a
    # live thin-client session, and counting it would both inflate the load
    # signal and leak entries that nothing ever releases.
    if slice_mode == "server":
        volume_service.register_server_session(
            series_uid, _viewer_session_identity(request, user)
        )
    geo = {
        "X-PAXS-Mode-Hint": slice_mode,
        "X-MPR-Rows": str(rows),
        "X-MPR-Cols": str(cols),
        "X-MPR-Plane": result.plane,
        "X-MPR-Spacing": ",".join(f"{v:.4f}" for v in result.spacing),
        "X-MPR-Window": f"{center:.1f},{width:.1f}",
        "X-MPR-Downsample": str(sample_step),
        "X-MPR-Non-Primary": "true" if sample_step > 1 else "false",
        "X-MPR-Derived-Resampled": "true" if info.get("derived_resampled") else "false",
        "X-MPR-Gantry-Tilt-Corrected": "true" if info.get("gantry_tilt_corrected") else "false",
        "Cache-Control": "no-store",
        "Access-Control-Expose-Headers": (
            "Content-Length, X-PAXS-Mode-Hint, X-MPR-Rows, X-MPR-Cols, "
            "X-MPR-Plane, X-MPR-Spacing, X-MPR-Window, X-MPR-Downsample, "
            "X-MPR-Non-Primary, X-MPR-Derived-Resampled, "
            "X-MPR-Gantry-Tilt-Corrected, " + LOAD_HEADERS_EXPOSED
        ),
    }
    geo.update(server_load_headers())   # §7.3

    if fmt == "raw":
        payload = volume_service.nan_to_float32(result.data).tobytes()
        return Response(content=payload, media_type="application/octet-stream",
                        headers=geo | {"Content-Length": str(len(payload))})
    if fmt == "npy":
        arr = volume_service.nan_to_float32(result.data)
        header = volume_service.npy_header("float32", arr.shape)
        payload = header + arr.tobytes()
        return Response(content=payload, media_type="application/x-npy",
                        headers=geo | {"Content-Length": str(len(payload))})

    png = volume_service.render_plane_png(result.data, center, width)
    return Response(content=png, media_type="image/png",
                    headers=geo | {"Content-Length": str(len(png))})
