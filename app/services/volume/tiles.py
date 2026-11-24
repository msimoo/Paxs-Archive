"""Multi-slice PNG tile rendering for the viewer's stack view.

One request renders a slab of planes; the result is cached per
``tile_cache_key`` in-process and in Redis. ``_TileInFlight`` collapses
concurrent requests for the same tile so a viewer scroll does not launch N
identical renders.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

from app.core.config import settings
from app.services.volume.assembly import _tenant_token, stack_fingerprint
from app.services.volume.cache import VolumeAssemblyCapacityError, _VOL_CACHE_LOCK
from app.services.volume.mpr import (
    RenderedPlane,
    plane_dims,
    plane_index_max,
    plane_spacing,
    render_plane_png,
    reslice_plane,
    window_for,
)
from app.services.volume.slices import VolumeSlice

log = logging.getLogger("paxs.volume")

__all__ = [
    "_TILE_CACHE",
    "_TILE_CACHE_MAX",
    "_TILE_INFLIGHT",
    "_TILE_INFLIGHT_LOCK",
    "_TILE_INFLIGHT_MAX",
    "_TILE_INFLIGHT_WAIT_SECONDS",
    "_TILE_REDIS_MAX_BYTES",
    "_TILE_REDIS_TTL",
    "_TileInFlight",
    "_redis_get_bytes",
    "_redis_set_bytes",
    "_tile_cache_put",
    "_tile_from_cache",
    "_tile_inflight_abort",
    "_tile_inflight_begin",
    "render_plane_tile",
    "render_tiles",
    "tile_cache_key",
]


_TILE_CACHE: OrderedDict[str, RenderedPlane] = OrderedDict()


_TILE_CACHE_MAX = 256


_TILE_REDIS_TTL = 600          # matches the plan's rendered-image tier


_TILE_REDIS_MAX_BYTES = 4 * 1024 * 1024


_TILE_INFLIGHT_WAIT_SECONDS = 20.0


_TILE_INFLIGHT: dict[tuple, _TileInFlight] = {}


_TILE_INFLIGHT_LOCK = threading.Lock()


_TILE_INFLIGHT_MAX = 64


class _TileInFlight:
    """One in-progress tile render shared by every waiter for the same key.

    Duplicated rather than imported (``ThreadPoolExecutor`` subclasses ``Future``
    only when concurrent.futures provides it) — this carries the PNG payload and
    the outcome timestamp the *waiters* need, which a bare Future does not.
    """

    __slots__ = ("done_at", "error", "event", "png")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.png: bytes | None = None
        self.error: BaseException | None = None
        self.done_at = 0.0

    def resolve(self, png: bytes | None, error: BaseException | None) -> None:
        self.png = png
        self.error = error
        self.done_at = time.monotonic()
        self.event.set()

    def wait(self, timeout: float) -> bytes | None:
        """Block until the owner finishes; None if it finished with an error."""
        self.event.wait(timeout)
        if not self.event.is_set():
            # The owner is still grinding.  Callers fall through to their own
            # render rather than blocking a request thread indefinitely.
            return None
        return self.png


def _tile_inflight_begin(key: tuple):
    """Return ``(owner, shared)`` — owner True means this caller renders."""
    now = time.monotonic()
    with _TILE_INFLIGHT_LOCK:
        # Reap completed / abandoned entries so the dict cannot grow without
        # bound when a renderer dies without resolving its entry.
        for dead in [k for k, shared in _TILE_INFLIGHT.items()
                     if shared.event.is_set() and now - shared.done_at > 1.0]:
            _TILE_INFLIGHT.pop(dead, None)
        shared = _TILE_INFLIGHT.get(key)
        if shared is not None and not shared.event.is_set():
            return False, shared
        if len(_TILE_INFLIGHT) >= _TILE_INFLIGHT_MAX:
            completed = [
                candidate for candidate, work in _TILE_INFLIGHT.items()
                if work.event.is_set()
            ]
            if completed:
                oldest = min(completed, key=lambda candidate: _TILE_INFLIGHT[candidate].done_at)
                _TILE_INFLIGHT.pop(oldest, None)
            else:
                raise VolumeAssemblyCapacityError(
                    "Active tile render limit reached; retry after capacity is available"
                )
        owner = _TileInFlight()
        _TILE_INFLIGHT[key] = owner
        return True, owner


def _tile_inflight_abort(key: tuple) -> None:
    """Owner path: drop the entry without publishing a payload."""
    with _TILE_INFLIGHT_LOCK:
        _TILE_INFLIGHT.pop(key, None)


def tile_cache_key(
    series_uid: str,
    plane: str,
    index: int,
    window_center: float | None = None,
    window_width: float | None = None,
    tenant_id: int | None = None,
    frame_hash: str = "",
) -> str:
    """Stable identity of a rendered slice tile (tenant-scoped, C-6).

    Window values are rounded to one decimal so a slider drag does not mint a
    new key on every mouse-move sample.  ``auto`` keeps an un-windowed render
    (the series window resolved by :func:`window_for`) distinct from an
    explicitly requested window that happens to equal it.

    The tenant prefix is what stops a rendered tile from one institution being
    served to another that happens to hold the same SeriesInstanceUID.
    """
    if window_center is None or window_width is None:
        window = "auto"
    else:
        window = f"{round(float(window_center), 1)}:{round(float(window_width), 1)}"
    return (
        f"paxs:tile:tenant:{_tenant_token(tenant_id)}:{series_uid}"
        f":{frame_hash}:{plane}:{int(index)}:{window}"
    )


def render_plane_tile(
    slices: list[VolumeSlice],
    info: dict,
    plane: str,
    index: int,
    window_center: float | None = None,
    window_width: float | None = None,
    tenant_id: int | None = None,
) -> RenderedPlane:
    """Reslice ONE native plane and rasterize it to PNG (L1 + L2 cached).

    Returns the PNG bytes together with the *resolved* window and the reslice
    geometry, so a caller never has to recompute ``window_for`` (an autoscale
    over the whole plane) just to report the window it displayed.

    C-6: every cache tier (the L1 tile LRU, the Redis tile key, the in-flight
    registry) is keyed with ``tenant_id``, so a tile rendered for one tenant is
    never served to another holding the same SeriesInstanceUID.

    Raises ``ValueError`` for an unknown plane and ``IndexError`` for an index
    outside the volume — both are turned into HTTP 400/416 by the router.
    """
    plane = str(plane).lower()
    if plane not in ("axial", "sagittal", "coronal"):
        raise ValueError("plane must be 'axial', 'sagittal' or 'coronal'")
    index = int(index)
    if plane == "axial" and not 0 <= index < int(info["slices"]):
        raise IndexError(f"axial index {index} out of range 0-{int(info['slices']) - 1}")
    if plane == "sagittal" and not 0 <= index < int(info["columns"]):
        raise IndexError(f"sagittal index {index} out of range 0-{int(info['columns']) - 1}")
    if plane == "coronal" and not 0 <= index < int(info["rows"]):
        raise IndexError(f"coronal index {index} out of range 0-{int(info['rows']) - 1}")

    series_uid = info.get("series_instance_uid") or ""
    key = tile_cache_key(
        series_uid, plane, index, window_center, window_width, tenant_id=tenant_id,
        frame_hash=info.get("stack_fingerprint") or stack_fingerprint(slices),
    )

    # L1: in-process tile cache.
    with _VOL_CACHE_LOCK:
        hit = _TILE_CACHE.get(key)
        if hit is not None:
            _TILE_CACHE.move_to_end(key)
            return hit

    # L2: Redis tile cache, shared across nodes.
    if getattr(settings, "REDIS_CACHE_ENABLED", False):
        png = _redis_get_bytes(key)
        if png:
            rendered = _tile_from_cache(png, info, plane, index, window_center)
            _tile_cache_put(key, rendered)
            return rendered

    # Coalesce concurrent identical renders (one raster, N waiters).
    inflight_key = key
    owner, shared = _tile_inflight_begin(inflight_key)
    if not owner:
        png = shared.wait(_TILE_INFLIGHT_WAIT_SECONDS)
        if png:
            return _tile_from_cache(png, info, plane, index, window_center)
        if not shared.event.is_set():
            raise VolumeAssemblyCapacityError(
                "Tile render is still active; retry after capacity is available"
            )
        # Owner failed: this request may take over after the failed entry is removed.
        owner, shared = _tile_inflight_begin(inflight_key)
        if not owner:
            raise VolumeAssemblyCapacityError(
                "Tile render is being retried; retry after capacity is available"
            )

    try:
        result = reslice_plane(slices, info, plane, index=index, tenant_id=tenant_id)
        center, width = window_for(info, result.data, window_center, window_width)
        png = render_plane_png(result.data, center, width)
    except BaseException:
        if owner:
            _tile_inflight_abort(inflight_key)
            shared.resolve(None, None)
        raise

    rendered = RenderedPlane(
        png=png, plane=plane, index=index, center=center, width=width,
        rows=result.rows, cols=result.cols,
        spacing=list(result.spacing) if result.spacing else [1.0, 1.0],
        origin=list(result.origin) if result.origin else None,
    )
    _tile_cache_put(key, rendered)
    if getattr(settings, "REDIS_CACHE_ENABLED", False) and len(png) <= _TILE_REDIS_MAX_BYTES:
        _redis_set_bytes(key, png, _TILE_REDIS_TTL)
    if owner:
        shared.resolve(png, None)
    return rendered


def _tile_from_cache(
    png: bytes,
    info: dict,
    plane: str,
    index: int,
    window_center: float | None,
) -> RenderedPlane:
    """Rebuild a RenderedPlane for a cached PNG.

    Geometry is derivable from the volume metadata without touching voxels;
    the window is echoed back from the request that produced the cache entry
    (an explicit window) or left to the caller when it was auto-resolved.
    """
    rows, cols = plane_dims(info, plane)
    spacing = plane_spacing(info, plane)
    return RenderedPlane(
        png=png, plane=plane, index=int(index),
        center=None if window_center is None else float(window_center),
        width=None,
        rows=rows, cols=cols, spacing=spacing, origin=None,
    )


def _tile_cache_put(key: str, rendered: RenderedPlane) -> None:
    with _VOL_CACHE_LOCK:
        _TILE_CACHE[key] = rendered
        _TILE_CACHE.move_to_end(key)
        while len(_TILE_CACHE) > _TILE_CACHE_MAX:
            _TILE_CACHE.popitem(last=False)


def _redis_get_bytes(key: str) -> bytes | None:
    """Best-effort binary GET on the shared pooled Redis client."""
    from app.services.redis_service import get_redis_client, redis_circuit_call

    client = get_redis_client()
    if client is None:
        return None
    try:
        return redis_circuit_call(client.get, key)
    except Exception as exc:  # noqa: BLE001 - boundary guard: best-effort cache read; logged type-only
        # Redis is an optional acceleration tier; a failed read is a safe cache
        # miss, while the warning preserves operational visibility.
        log.warning("Redis tile-cache GET failed (error_type=%s); treating as cache miss",
                    type(exc).__name__)
        return None


def _redis_set_bytes(key: str, payload: bytes, ttl: int) -> None:
    """Best-effort binary SET on the shared pooled Redis client."""
    from app.services.redis_service import get_redis_client, redis_circuit_call

    client = get_redis_client()
    if client is None:
        return
    try:
        redis_circuit_call(client.set, key, payload, ex=ttl)
    except Exception as exc:  # noqa: BLE001 - boundary guard: best-effort cache write; logged type-only
        # Redis is only a shared cache; the in-process render remains valid.
        log.warning("Redis tile-cache SET failed (error_type=%s)", type(exc).__name__)


def render_tiles(
    series_uid: str,
    plane: str,
    start_idx: int,
    end_idx: int,
    window_center: float,
    window_width: float,
    size: int,
    slices: list[VolumeSlice],
    info: dict,
    tenant_id: int | None = None,
) -> list[bytes]:
    """Batch-render PNG slice frames for a prefetch window (plan §5.4).

    Returns one PNG per slice in the **inclusive** ``[start_idx, end_idx]``
    range, in ascending index order.  Each frame is served from — and written
    to — the shared L1 tile cache and the Redis L2 cache, so a scroll through a
    previously prefetched range costs no volume materialization.

    Frames are rasterized at the plane's *native* resolution.  ``render_plane_png``
    deliberately performs no resampling — a diagnostic viewer must never be shown
    an interpolated up-sample — so ``size`` is a bounded hint: it selects the
    viewer's requested viewport but never up-scales beyond the real data.

    ``slices``/``info`` are passed in rather than re-queried, so the router
    performs its tenant/ACL check exactly once for the whole batch.
    ``tenant_id`` scopes every cache tier the batch touches (C-6).
    """
    plane = str(plane).lower()
    if plane not in ("axial", "sagittal", "coronal"):
        raise ValueError("plane must be 'axial', 'sagittal' or 'coronal'")

    start = max(0, int(start_idx))
    max_index = plane_index_max(info, plane)
    end = min(int(end_idx), max_index)
    if start > max_index:
        raise IndexError(f"{plane} index {start} out of range 0-{max_index}")
    if end < start:
        end = start

    # A single range request is capped so one HTTP call cannot flatten the
    # node: VOLUME_MAX_SLICES is the same budget /data enforces per request.
    limit = max(1, int(getattr(settings, "VOLUME_MAX_SLICES", 512)))
    if end - start + 1 > limit:
        end = start + limit - 1

    return [
        render_plane_tile(
            slices, info, plane, index,
            window_center=window_center, window_width=window_width,
            tenant_id=tenant_id,
        ).png
        for index in range(start, end + 1)
    ]
