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

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_share_scope,
    require_series_access,
    require_series_read,
    tenant_scope,
)
from app.core.config import settings
from app.db.models import User
from app.db.session import get_db
from app.services import volume_service

log = logging.getLogger("paxs.api.volume")

router = APIRouter(prefix="/api/v1/volume", tags=["Volume"])


def _slice_range(raw: str | None, total: int) -> tuple[int, int]:
    """Parse `slices` into a half-open [start, end) range over `total` slices."""
    if not raw or raw.strip().lower() in ("", "all", "*"):
        return 0, total
    text = raw.strip().replace(":", "-")
    try:
        parts = text.split("-")
        if len(parts) == 1:
            idx = int(parts[0])
            start, end = idx, idx
        elif len(parts) == 2:
            start, end = int(parts[0]), int(parts[1])
        else:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(
            400, "slices must be 'all' or '<start>-<end>' (0-indexed, inclusive)"
        ) from None
    start = max(start, 0)
    end = min(end, total - 1)
    if start > end:
        raise HTTPException(416, f"Slice range out of bounds (0-{total - 1})")
    return start, end + 1


# ---------------------------------------------------------------------------
# Processing mode negotiation (plan_viewer_process.md §5.1, Phase 1)
# ---------------------------------------------------------------------------

def _resolve_mode(user: User | None) -> str:
    """Return the base processing mode after applying per-role overrides."""
    mode = (settings.VIEWER_PROCESSING_MODE or "hybrid").lower().strip()
    if mode not in ("server", "client", "hybrid"):
        mode = "hybrid"

    if user is not None and settings.VIEWER_PROCESSING_ROLE_MAP:
        import json
        try:
            role_map: dict[str, str] = json.loads(settings.VIEWER_PROCESSING_ROLE_MAP)
        except (json.JSONDecodeError, TypeError):
            role_map = {}
        role = getattr(user, "role", None) or ""
        override = role_map.get(role.upper())
        if override and override.lower() in ("server", "client", "hybrid"):
            mode = override.lower()

    return mode


def _negotiate_hybrid(
    webgl_score: int,
    rtt_ms: int,
) -> str:
    """Resolve the effective mode for a hybrid session."""
    # No WebGL at all → must use server
    if webgl_score == 0:
        return "server"

    # Server overloaded → push to client if remotely capable
    server_sessions = volume_service.active_server_session_count()
    cache_mb = volume_service.estimated_vol_cache_mb()
    server_overloaded = (
        server_sessions >= settings.HYBRID_MAX_SERVER_MPR_SESSIONS
        or cache_mb >= settings.HYBRID_SERVER_VOL_BUDGET_MB
    )
    if server_overloaded:
        return "client" if webgl_score >= 20 else "server"

    # Strong client
    if webgl_score >= settings.HYBRID_CLIENT_SCORE_THRESHOLD:
        return "client"

    # High latency → prefer client to avoid per-slice round-trips
    if rtt_ms > settings.HYBRID_RTT_THRESHOLD_MS and webgl_score >= 20:
        return "client"

    # Default: server
    return "server"


def _mode_endpoints(mode: str, series_uid: str) -> dict:
    """URL map the frontend should use for the resolved mode."""
    base = f"/api/v1/volume/{series_uid}"
    common = {"info": f"{base}/info", "negotiate": f"{base}/negotiate"}
    if mode == "server":
        return {
            **common,
            "mpr_slice": f"{base}/mpr/slice",
            "mip": f"{base}/mip",
        }
    if mode == "client":
        return {**common, "data": f"{base}/data"}
    # hybrid / split — expose everything
    return {
        **common,
        "data": f"{base}/data",
        "mpr_slice": f"{base}/mpr/slice",
        "mip": f"{base}/mip",
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
        }
    if mode == "client":
        return {
            "client_webgl_required": True,
            "server_renders_slices": False,
            "raw_volume_stream": True,
            "gpu_mpr": True,
            "gpu_mip": True,
        }
    # hybrid
    return {
        "client_webgl_required": False,
        "server_renders_slices": True,
        "raw_volume_stream": True,
        "gpu_mpr": True,
        "gpu_mip": True,
    }


@router.get("/{series_uid}/negotiate", response_class=JSONResponse)
async def volume_negotiate_endpoint(
    series_uid: str,
    request: Request,
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
    """
    if not settings.VOLUME_STREAMING_ENABLED:
        raise HTTPException(503, "Volume streaming is disabled")

    await require_series_access(
        db, series_uid, user,
        share_scope=await get_share_scope(request),
    )

    # 1. Resolve base mode (global setting + per-role override)
    mode = _resolve_mode(user)

    # 2. Hybrid negotiation
    effective_mode = mode
    q = request.query_params
    if mode == "hybrid":
        webgl_score = max(0, min(100, int(q.get("webgl_score", 0) or 0)))
        rtt_ms = max(0, int(q.get("rtt_ms", 0) or 0))
        effective_mode = _negotiate_hybrid(webgl_score, rtt_ms)

    # 3. Build response
    server_sessions = volume_service.active_server_session_count()

    body: dict = {
        "configured_mode": mode,
        "effective_mode": effective_mode,
        "series_uid": series_uid,
        "endpoints": _mode_endpoints(effective_mode, series_uid),
        "capabilities": _mode_capabilities(effective_mode),
    }
    if mode == "hybrid":
        body["hybrid_thresholds"] = {
            "client_score_threshold": settings.HYBRID_CLIENT_SCORE_THRESHOLD,
            "rtt_threshold_ms": settings.HYBRID_RTT_THRESHOLD_MS,
            "server_vol_budget_mb": settings.HYBRID_SERVER_VOL_BUDGET_MB,
            "max_server_sessions": settings.HYBRID_MAX_SERVER_MPR_SESSIONS,
        }

    return JSONResponse(body, headers={
        "X-PAXS-Processing-Mode": effective_mode,
        "X-PAXS-Server-Load": str(server_sessions),
        "Cache-Control": "no-store",
    })


@router.get("/{series_uid}/info", response_class=JSONResponse)
async def volume_info_endpoint(
    series_uid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_series_read),
):
    """Volume geometry/encoding metadata for MPR reconstruction."""
    if not settings.VOLUME_STREAMING_ENABLED:
        raise HTTPException(503, "Volume streaming is disabled")
    # C04: a series UID alone must not reach across institutions.
    await require_series_access(db, series_uid, user,
                                share_scope=await get_share_scope(request))
    tenant_id, include_all = (None, True) if user is None else tenant_scope(user)
    info = await volume_service.volume_info(
        db, series_uid, tenant_id=tenant_id, include_all=include_all
    )
    if info is None:
        raise HTTPException(404, "No readable volumetric series found")
    return JSONResponse(info)


@router.get("/{series_uid}/data")
async def volume_data_endpoint(
    series_uid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_series_read),
):
    """Stream the raw voxel block of a series (optionally a sub-range)."""
    if not settings.VOLUME_STREAMING_ENABLED:
        raise HTTPException(503, "Volume streaming is disabled")
    # C04: a series UID alone must not reach across institutions.
    await require_series_access(db, series_uid, user,
                                share_scope=await get_share_scope(request))
    tenant_id, include_all = (None, True) if user is None else tenant_scope(user)

    bundle = await volume_service.volume_bundle(
        db, series_uid, tenant_id=tenant_id, include_all=include_all
    )
    if bundle is None:
        raise HTTPException(404, "No readable volumetric series found")
    info, slices = bundle
    # Multi-frame / Enhanced instances are expanded into per-frame slices by
    # the service — no 422 needed; info["multi_frame"] is False for the
    # expanded stream (info["source_multi_frame"] records the origin).

    params = request.query_params
    start, end = _slice_range(params.get("slices"), info["slices"])
    count = end - start
    if count <= 0:
        raise HTTPException(416, f"Slice range out of bounds (0-{info['slices'] - 1})")
    if count > settings.VOLUME_MAX_SLICES:
        raise HTTPException(
            416, f"Slice range exceeds VOLUME_MAX_SLICES ({settings.VOLUME_MAX_SLICES})"
        )

    rescale = str(params.get("rescale", "")).lower() in ("1", "true", "yes")
    downsample = max(1, min(8, int(params.get("downsample", 1) or 1)))
    fmt = str(params.get("format", "raw")).lower()
    if fmt not in ("raw", "npy"):
        raise HTTPException(400, "format must be 'raw' or 'npy'")

    rows = info["rows"]
    cols = info["columns"]
    samples = info["samples_per_pixel"]

    # Declared geometry must equal the sampled array's shape. The sampler
    # strides with arr[::step, ::step] (ceil(n/step) samples), so the shared
    # strided_dim helper is used here instead of floor division (H13) — a
    # mismatch produced wrong X-Volume-Rows/Cols, a wrong .npy header shape and
    # a Content-Length that did not match the body.
    out_rows = volume_service.strided_dim(rows, downsample)
    out_cols = volume_service.strided_dim(cols, downsample)
    out_count = len(slices[start:end:downsample])

    def stream():
        if fmt == "npy":
            dtype = "float32" if rescale else info["dtype"]
            shape = (out_count, out_rows, out_cols, samples) if samples > 1 else (out_count, out_rows, out_cols)
            yield volume_service.npy_header(dtype, shape)
        for chunk in volume_service.iter_volume_bytes(
            slices, rescale=rescale, start=start, end=end, downsample=downsample
        ):
            yield chunk

    if fmt == "raw":
        payload_len = volume_service.volume_byte_length(
            slices, info["dtype"], rows, cols, samples, start=start, end=end, downsample=downsample
        )
        if rescale:
            payload_len = out_count * out_rows * out_cols * samples * 4
        headers = {
            "Content-Length": str(payload_len),
            "X-Volume-Slices": f"{out_count}",
            "X-Volume-Rows": f"{out_rows}",
            "X-Volume-Cols": f"{out_cols}",
            "X-Volume-Downsample": f"{downsample}",
            "X-Volume-Dtype": "float32" if rescale else info["dtype"],
            "Cache-Control": "private, no-store",
            "Accept-Ranges": "bytes",
            "Access-Control-Expose-Headers": "Content-Length, X-Volume-Slices, X-Volume-Rows, X-Volume-Cols, X-Volume-Downsample, X-Volume-Dtype",
        }
    else:
        dtype = "float32" if rescale else info["dtype"]
        payload_len = out_count * out_rows * out_cols * samples * np_dtype_itemsize(dtype)
        payload_len += len(volume_service.npy_header(
            dtype, (out_count, out_rows, out_cols, samples) if samples > 1 else (out_count, out_rows, out_cols)
        ))
        headers = {
            "Content-Length": str(payload_len),
            "X-Volume-Slices": f"{out_count}",
            "X-Volume-Rows": f"{out_rows}",
            "X-Volume-Cols": f"{out_cols}",
            "X-Volume-Downsample": f"{downsample}",
            "X-Volume-Dtype": dtype,
            "Cache-Control": "private, no-store",
            "Accept-Ranges": "bytes",
            "Access-Control-Expose-Headers": "Content-Length, X-Volume-Slices, X-Volume-Rows, X-Volume-Cols, X-Volume-Downsample, X-Volume-Dtype",
        }

    media_type = (
        "application/x-npy" if fmt == "npy"
        else "application/octet-stream"
    )

    r_key = f"paxs:vol:stream:{series_uid}:{start}_{end}_{downsample}_{int(rescale)}_{fmt}"
    canonical_key = (
        f"paxs:vol:stream:{series_uid}:{info['slices']}"
        if (start == 0 and end == info["slices"] and downsample == 1 and not rescale and fmt == "raw")
        else None
    )

    from app.services.redis_service import get_redis_client

    r_client = get_redis_client() if getattr(settings, "REDIS_CACHE_ENABLED", False) else None
    if r_client:
        try:
            cached = r_client.get(r_key) or (r_client.get(canonical_key) if canonical_key else None)
            if cached is not None and len(cached) == payload_len:
                headers["X-Cache"] = "HIT"
                return Response(
                    content=cached,
                    media_type=media_type,
                    headers=headers,
                )
        except Exception:
            pass

    def build_and_cache():
        chunks = []
        if fmt == "npy":
            dtype = "float32" if rescale else info["dtype"]
            shape = (
                (out_count, out_rows, out_cols, samples)
                if samples > 1
                else (out_count, out_rows, out_cols)
            )
            chunks.append(volume_service.npy_header(dtype, shape))
        for chunk in volume_service.iter_volume_bytes(
            slices, rescale=rescale, start=start, end=end, downsample=downsample
        ):
            chunks.append(chunk)
        data = b"".join(chunks)
        if r_client and len(data) <= 256 * 1024 * 1024:
            try:
                r_client.set(r_key, data, ex=3600)
                if canonical_key and canonical_key != r_key:
                    r_client.set(canonical_key, data, ex=3600)
            except Exception as exc:
                log.debug("Failed to cache volume in Redis: %s", exc)
        return data

    data = await asyncio.to_thread(build_and_cache)
    return Response(
        content=data,
        media_type=media_type,
        headers=headers,
    )


def np_dtype_itemsize(dtype: str) -> int:
    import numpy as np

    return np.dtype(dtype).itemsize


@router.get("/{series_uid}/mip")
async def volume_mip_endpoint(
    series_uid: str,
    request: Request,
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
    tenant_id, include_all = (None, True) if user is None else tenant_scope(user)

    bundle = await volume_service.volume_bundle(
        db, series_uid, tenant_id=tenant_id, include_all=include_all
    )
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
        result = await asyncio.to_thread(
            volume_service.mip_project,
            slices, info, axis=axis, mode=mode, offset=offset,
            thickness=thickness, window_center=wc_f, window_width=ww_f,
        )
    except HTTPException:
        raise
    except IndexError as exc:
        raise HTTPException(416, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    center, width = volume_service.window_for(info, result.data, wc_f, ww_f)
    slab = f"{result.offset}:{result.offset + result.thickness}"
    geo = {
        "X-MIP-Rows": str(result.rows),
        "X-MIP-Cols": str(result.cols),
        "X-MIP-Axis": result.axis,
        "X-MIP-Mode": result.mode,
        "X-MIP-Slab": slab,
        "X-MIP-Spacing": ",".join(f"{v:.4f}" for v in result.spacing),
        "X-MIP-Window": f"{center:.1f},{width:.1f}",
        "Cache-Control": "no-store",
    }

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


@router.get("/{series_uid}/mpr/slice")
async def volume_mpr_slice_endpoint(
    series_uid: str,
    request: Request,
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
    tenant_id, include_all = (None, True) if user is None else tenant_scope(user)

    bundle = await volume_service.volume_bundle(
        db, series_uid, tenant_id=tenant_id, include_all=include_all
    )
    if bundle is None:
        raise HTTPException(404, "No readable volumetric series found")
    info, slices = bundle
    # Multi-frame / Enhanced instances are expanded into per-frame slices by
    # the service — reslicing works on the expanded stack.

    q = request.query_params
    fmt = str(q.get("format", "png")).lower()
    if fmt not in ("png", "raw", "npy"):
        raise HTTPException(400, "format must be 'png', 'raw' or 'npy'")

    plane = str(q.get("plane", "axial")).lower()
    # reslice_plane materializes the volume (disk reads + decode + float32
    # stack + trilinear sampling) — offload to the thread pool so a slice
    # reslice never blocks the event loop for the node's other requests.
    try:
        if plane in ("axial", "sagittal", "coronal"):
            index = q.get("index")
            if index is None:
                raise HTTPException(400, f"index is required for plane={plane}")
            result = await asyncio.to_thread(
                volume_service.reslice_plane,
                slices, info, plane, index=int(index),
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
            result = await asyncio.to_thread(
                volume_service.reslice_plane,
                slices, info, "oblique",
                origin=[float(v) for v in origin.split(",")],
                row_vector=[float(v) for v in row.split(",")],
                col_vector=[float(v) for v in col.split(",")],
                size=size,
                max_pixels=settings.VOLUME_MPR_MAX_PIXELS,
            )
    except HTTPException:
        raise
    except IndexError as exc:
        raise HTTPException(416, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    wc = q.get("window_center")
    ww = q.get("window_width")
    wc_f = float(wc) if wc is not None else None
    ww_f = float(ww) if ww is not None else None
    try:
        center, width = volume_service.window_for(info, result.data, wc_f, ww_f)
    except (TypeError, ValueError):
        raise HTTPException(400, "window_center/window_width must be numbers") from None

    rows, cols = result.rows, result.cols
    geo = {
        "X-MPR-Rows": str(rows),
        "X-MPR-Cols": str(cols),
        "X-MPR-Plane": result.plane,
        "X-MPR-Spacing": ",".join(f"{v:.4f}" for v in result.spacing),
        "X-MPR-Window": f"{center:.1f},{width:.1f}",
        "Cache-Control": "no-store",
    }

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
