"""Volume assembly: the bundle (geometry + slice rows) and its cache keys.

``volume_bundle`` is what every other part of the pipeline asks for: the ordered
slice rows plus the geometry the client needs to rebuild the volume. The
``*_cache_key`` helpers are **tenant-scoped** — a key that ignores the tenant
would let one institution's viewer read another's cached volume (FA-1).
"""

from __future__ import annotations

import hashlib
import logging
import math
import sys
import threading
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import fields, is_dataclass
from types import ModuleType

import numpy as np
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core import cache_stats
from app.core.config import settings
from app.core.concurrency import run_blocking
from app.db.models import Series, Study
from app.services import storage_service
from app.services.volume.slices import (
    _COL_SPACING,
    _ROW_SPACING,
    VolumeSlice,
    VolumeVariant,
    _float,
    _window_value,
    dtype_for,
    orientation_label,
    strided_dim,
)
from app.services.volume.geometry_validation import (
    GeometryErrorCode,
    GeometryValidationError,
)
from app.services.volume.calibration import apply_frame_calibration, resolve_frame_calibration
from app.services.volume.deskew import (
    build_deskew_plan,
    build_deskew_plan_from_slices,
    deskew_raw_slice_2d,
    deskew_slice_2d,
)

log = logging.getLogger("paxs.volume")

_FACADE = "app.services.volume_service"


def _facade() -> ModuleType:
    """The public ``app.services.volume_service`` namespace, resolved per call.

    The voxel pipeline used to live in one module, so the regression suite
    patches ``volume_service._frame_array`` and expects the stream to observe
    the substitution. Looking the helper up through the facade at call time
    keeps that seam working now that the implementation is a package.
    """
    return sys.modules[_FACADE]


__all__ = [
    "_BUNDLE_CACHE",
    "_BUNDLE_CACHE_MAX",
    "_expected_shape",
    "_frame_array",
    "_tenant_token",
    "iter_volume_bytes",
    "stack_fingerprint",
    "volume_bundle",
    "volume_byte_length",
    "volume_cache_key",
    "volume_info",
    "volume_redis_key",
    "volume_redis_scan_pattern",
]


_BundleKey = tuple[str, bool, str, VolumeVariant]
_BUNDLE_CACHE: OrderedDict[_BundleKey, tuple[dict, list[VolumeSlice]]] = OrderedDict()
_BUNDLE_CACHE_LOCK = threading.RLock()
_BUNDLE_CACHE_SIZES: dict[_BundleKey, int] = {}
_BUNDLE_CACHE_BYTES = 0


_BUNDLE_CACHE_MAX = 32


def bundle_cache_key(
    tenant_id: int | None, include_all: bool, series_uid: str, variant: VolumeVariant = "auto"
) -> _BundleKey:
    """Build the tenant-scoped bundle key used by all cache operations."""
    return (_tenant_token(tenant_id), bool(include_all), series_uid, variant)


def _bundle_cache_budget_bytes() -> int:
    """Read the configured bundle-cache byte budget; zero disables caching."""
    return max(0, int(getattr(settings, "VOLUME_BUNDLE_CACHE_BUDGET_MB", 128))) * 1024 * 1024


def _retained_size(value: object, seen: set[int] | None = None) -> int:
    """Estimate retained Python and NumPy bytes without counting shared objects twice."""
    visited = seen if seen is not None else set()
    identity = id(value)
    if identity in visited:
        return 0
    visited.add(identity)
    size = sys.getsizeof(value)
    if isinstance(value, np.ndarray):
        retained = max(size, int(value.nbytes))
        base = value.base
        return retained + _retained_size(base, visited) if isinstance(base, np.ndarray) else retained
    if isinstance(value, dict):
        return size + sum(_retained_size(key, visited) + _retained_size(item, visited)
                          for key, item in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        return size + sum(_retained_size(item, visited) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return size + sum(_retained_size(getattr(value, item.name), visited)
                          for item in fields(value))
    return size


def _sync_bundle_cache_budget_locked(budget: int) -> None:
    """Trim old entries after a live budget change; caller holds cache lock."""
    global _BUNDLE_CACHE_BYTES
    for stale_key in set(_BUNDLE_CACHE_SIZES) - set(_BUNDLE_CACHE):
        _BUNDLE_CACHE_BYTES -= _BUNDLE_CACHE_SIZES.pop(stale_key, 0)
    while _BUNDLE_CACHE and (budget <= 0 or _BUNDLE_CACHE_BYTES > budget):
        key, _ = _BUNDLE_CACHE.popitem(last=False)
        _BUNDLE_CACHE_BYTES -= _BUNDLE_CACHE_SIZES.pop(key, 0)
        cache_stats.incr("bundle", "evictions")
    cache_stats.set_gauges("bundle", bytes=max(0, _BUNDLE_CACHE_BYTES), entries=len(_BUNDLE_CACHE))


def _bundle_cache_get(key: _BundleKey) -> tuple[dict, list[VolumeSlice]] | None:
    """Read and promote one tenant-scoped bundle cache entry."""
    with _BUNDLE_CACHE_LOCK:
        _sync_bundle_cache_budget_locked(_bundle_cache_budget_bytes())
        entry = _BUNDLE_CACHE.get(key)
        if entry is None:
            cache_stats.incr("bundle", "misses")
            return None
        _BUNDLE_CACHE.move_to_end(key)
        cache_stats.incr("bundle", "hits")
        return entry


def _bundle_cache_put(key: _BundleKey, info: dict,
                      slices: list[VolumeSlice]) -> bool:
    """Insert one bundle if it fits; evict least recently used entries to budget."""
    global _BUNDLE_CACHE_BYTES
    budget = _bundle_cache_budget_bytes()
    entry = (info, slices)
    size = _retained_size(entry)
    with _BUNDLE_CACHE_LOCK:
        old_size = _BUNDLE_CACHE_SIZES.pop(key, 0)
        _BUNDLE_CACHE.pop(key, None)
        _BUNDLE_CACHE_BYTES = max(0, _BUNDLE_CACHE_BYTES - old_size)
        if budget <= 0 or size > budget:
            _sync_bundle_cache_budget_locked(budget)
            return False
        while _BUNDLE_CACHE and _BUNDLE_CACHE_BYTES + size > budget:
            old_key, _ = _BUNDLE_CACHE.popitem(last=False)
            _BUNDLE_CACHE_BYTES -= _BUNDLE_CACHE_SIZES.pop(old_key, 0)
            cache_stats.incr("bundle", "evictions")
        _BUNDLE_CACHE[key] = entry
        _BUNDLE_CACHE_SIZES[key] = size
        _BUNDLE_CACHE_BYTES += size
        cache_stats.incr("bundle", "inserts")
        cache_stats.set_gauges("bundle", bytes=_BUNDLE_CACHE_BYTES, entries=len(_BUNDLE_CACHE))
        return True


def bundle_cache_stats() -> dict[str, int]:
    """Return byte, entry, and hit/miss metrics for the bundle LRU."""
    with _BUNDLE_CACHE_LOCK:
        _sync_bundle_cache_budget_locked(_bundle_cache_budget_bytes())
        stats = cache_stats.snapshot()["bundle"]
        return {**stats, "budget_bytes": _bundle_cache_budget_bytes()}


def clear_bundle_cache() -> None:
    """Clear bundle entries and byte accounting, primarily for tests/reload."""
    global _BUNDLE_CACHE_BYTES
    with _BUNDLE_CACHE_LOCK:
        _BUNDLE_CACHE.clear()
        _BUNDLE_CACHE_SIZES.clear()
        _BUNDLE_CACHE_BYTES = 0
        cache_stats.set_gauges("bundle", bytes=0, entries=0)


def invalidate_volume_bundle_cache(*, tenant_id: int | None = None,
                                   study_uid: str | None = None,
                                   series_uid: str | None = None) -> int:
    """Invalidate matching bundles after writes or explicit study/series changes."""
    global _BUNDLE_CACHE_BYTES
    tenant_token = _tenant_token(tenant_id) if tenant_id is not None else None
    removed = 0
    with _BUNDLE_CACHE_LOCK:
        for key, (info, _slices) in list(_BUNDLE_CACHE.items()):
            if tenant_token is not None and key[0] != tenant_token:
                continue
            if series_uid is not None and key[2] != series_uid:
                continue
            if study_uid is not None and info.get("study_instance_uid") != study_uid:
                continue
            _BUNDLE_CACHE.pop(key, None)
            _BUNDLE_CACHE_BYTES -= _BUNDLE_CACHE_SIZES.pop(key, 0)
            removed += 1
        _BUNDLE_CACHE_BYTES = max(0, _BUNDLE_CACHE_BYTES)
        cache_stats.set_gauges("bundle", bytes=_BUNDLE_CACHE_BYTES, entries=len(_BUNDLE_CACHE))
    return removed


@event.listens_for(Session, "before_flush")
def _mark_bundle_cache_dirty(session: Session, _flush_context: object,
                             _instances: object) -> None:
    """Remember database writes so cached bundle metadata is cleared after commit."""
    changed = (*session.new, *session.dirty, *session.deleted)
    if any(type(row).__name__ in {"Study", "Series", "Instance"} for row in changed):
        session.info["paxs_bundle_cache_dirty"] = True


@event.listens_for(Session, "do_orm_execute")
def _mark_bundle_cache_bulk_write(state: object) -> None:
    """Invalidate bundle metadata after ORM bulk writes to image ownership rows."""
    if not (state.is_insert or state.is_update or state.is_delete):
        return
    statement = state.statement
    table = getattr(statement, "table", None)
    if getattr(table, "name", None) in {"study", "series", "instance"}:
        state.session.info["paxs_bundle_cache_dirty"] = True


@event.listens_for(Session, "after_commit")
def _invalidate_bundles_after_commit(session: Session) -> None:
    """Drop cached study/series metadata only after a successful DB commit."""
    if session.info.pop("paxs_bundle_cache_dirty", False):
        clear_bundle_cache()


@event.listens_for(Session, "after_rollback")
def _forget_bundle_dirty_marker(session: Session) -> None:
    """Discard pending invalidation after rollback; old committed data remains valid."""
    session.info.pop("paxs_bundle_cache_dirty", None)


def _public(name: str):
    """Look a collaborator up on the public facade (``volume_service``).

    Used only where a test replaces that name on the facade; a module-local
    reference would silently ignore the patch (FA-3 compatibility seam).
    """
    from app.services import volume_service as api

    return getattr(api, name)


async def volume_bundle(db: AsyncSession, series_uid: str, *,
                        tenant_id: int | None = None,
                        include_all: bool = False,
                        variant: VolumeVariant = "auto") -> tuple[dict, list[VolumeSlice]] | None:
    """``(info_dict, sorted_slices)`` for a series, or None when it has no
    readable volumetric instances.

    **C-6:** the metadata cache is keyed by the *resolving scope*
    (``tenant_id`` + ``include_all``) as well as the series UID. Keying it by
    ``series_uid`` alone meant that once tenant A resolved a series, tenant B's
    identical request was answered from A's cached slices — a cross-tenant PHI
    disclosure that never even reached the tenant-scoped query.
    """
    cache_key = bundle_cache_key(tenant_id, include_all, series_uid, variant)
    cached = _bundle_cache_get(cache_key)
    if cached is not None:
        return cached

    # Resolved through the public facade rather than this module's own binding:
    # `tests/test_concurrency_and_cache.py` replaces
    # `volume_service.series_slices` to feed a synthetic series, and a
    # module-local call would bypass that seam (FA-3 keeps it late-bound).
    slices = await _public("series_slices")(
        db, series_uid, tenant_id=tenant_id, include_all=include_all, variant=variant
    )
    if not slices:
        return None

    geometry = slices[0].series_geometry
    if geometry is None:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_IPP,
            "validated series geometry is required for volume assembly",
        )
    if geometry.slice_spacing_mm is None:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_SPACING,
            "at least two positioned slices are required to establish slice spacing",
        )

    first = slices[0]
    ds = await run_blocking(
        storage_service.read_dataset,
        first.file_path,
        stop_before_pixels=True,
        backend_name=first.backend,
    )
    series_row = (
        await db.execute(select(Series, Study.study_instance_uid)
                         .join(Study, Series.study_id == Study.id)
                         .where(Series.series_instance_uid == series_uid))
    ).one_or_none()

    source_rows = int(getattr(ds, "Rows", 0) or 0)
    source_cols = int(getattr(ds, "Columns", 0) or 0)
    deskew_plan = build_deskew_plan(
        geometry,
        source_rows,
        source_cols,
        enabled=bool(getattr(settings, "VOLUME_GANTRY_TILT_RESAMPLE_ENABLED", True)),
        max_expansion_factor=float(
            getattr(settings, "VOLUME_GANTRY_TILT_MAX_EXPANSION_FACTOR", 3.0)
        ),
    )
    rows = deskew_plan.out_rows
    cols = deskew_plan.out_cols
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    bits_allocated = int(getattr(ds, "BitsAllocated", 16) or 16)
    bits_stored = int(getattr(ds, "BitsStored", bits_allocated) or bits_allocated)
    pixel_rep = int(getattr(ds, "PixelRepresentation", 0) or 0)
    dtype = dtype_for(bits_allocated, pixel_rep)
    itemsize = np.dtype(dtype).itemsize

    spacing = list(geometry.slices[0].pixel_spacing)
    thickness = _float(getattr(ds, "SliceThickness", None), math.nan)
    thickness = None if math.isnan(thickness) else thickness

    iop = list(geometry.slices[0].image_orientation_patient)
    spacing_along_axis = geometry.slice_spacing_mm

    # Multi-frame / Enhanced instances are expanded into per-frame slices by
    # series_slices, so the streamed volume is a uniform per-frame stack and
    # the old "multi-frame → 422" rejection no longer applies: ``multi_frame``
    # reports the *expanded* stream (always False); the source is exposed as
    # ``source_multi_frame`` for clients that want to know.
    source_multi_frame = int(getattr(ds, "NumberOfFrames", 1) or 1) > 1

    isotropic = False
    if spacing and len(spacing) >= 2 and spacing[0] > 0:
        row_col_match = abs(spacing[_ROW_SPACING] - spacing[_COL_SPACING]) / spacing[_ROW_SPACING] < 0.01
        slab_match = (spacing_along_axis is not None
                      and abs(spacing_along_axis - spacing[_ROW_SPACING]) / spacing[_ROW_SPACING] < 0.05)
        isotropic = bool(row_col_match and slab_match)

    frame_types = {s.calibration.rescale_type for s in slices if s.calibration and s.calibration.rescale_type}
    volume_rescale_type = next(iter(frame_types)) if len(frame_types) == 1 else (
        "MIXED" if len(frame_types) > 1 else None
    )

    info: dict = {
        "series_instance_uid": series_uid,
        "volume_variant": variant,
        "stack_fingerprint": stack_fingerprint(slices),
        "study_instance_uid": series_row[1] if series_row else None,
        "modality": getattr(series_row[0], "modality", None) if series_row else None,
        "series_number": getattr(series_row[0], "series_number", None) if series_row else None,
        "series_description": getattr(series_row[0], "series_description", None) if series_row else None,
        "rows": rows,
        "columns": cols,
        "source_rows": source_rows,
        "source_columns": source_cols,
        "slices": len(slices),
        "samples_per_pixel": samples,
        "photometric_interpretation": str(getattr(ds, "PhotometricInterpretation", "MONOCHROME2")),
        "bits_allocated": bits_allocated,
        "bits_stored": bits_stored,
        "pixel_representation": pixel_rep,
        "dtype": dtype,
        "itemsize": itemsize,
        "pixel_spacing": spacing[:2] if spacing else None,
        "slice_thickness": thickness,
        "slice_spacing": spacing_along_axis,
        "image_position_patient": list(deskew_plan.orthogonal_ipp),
        "image_orientation_patient": iop,
        "orientation": orientation_label(iop),
        "isotropic": isotropic,
        "derived_resampled": deskew_plan.active,
        "gantry_tilt_corrected": deskew_plan.active,
        "gantry_tilt_deg": (
            round(float(geometry.gantry_tilt_deg), 3)
            if geometry.gantry_tilt_deg is not None
            else None
        ),
        "max_in_plane_displacement_mm": round(float(geometry.max_in_plane_displacement_mm), 3),
        "reformat_safe": True,
        "calibration_valid": (
            getattr(series_row[0], "modality", None) == "CT"
            and all(s.calibration is not None and s.calibration.valid for s in slices)
            if series_row else False
        ),
        "pixel_padding_min": None,
        "pixel_padding_max": None,
        "rescale_slope": slices[0].calibration.slope if slices[0].calibration else 1.0,
        "rescale_intercept": slices[0].calibration.intercept if slices[0].calibration else 0.0,
        "rescale_slope_by_slice": [s.calibration.slope if s.calibration else 1.0 for s in slices],
        "rescale_intercept_by_slice": [s.calibration.intercept if s.calibration else 0.0 for s in slices],
        "rescale_type": volume_rescale_type,
        "pixel_padding_by_slice": [
            None if s.calibration is None or s.calibration.padding_low is None
            else [s.calibration.padding_low, s.calibration.padding_high]
            for s in slices
        ],
        "window_center": _window_value(ds, "WindowCenter"),
        "window_width": _window_value(ds, "WindowWidth"),
        "multi_frame": False,            # expanded per-frame stream
        "source_multi_frame": source_multi_frame,
        "instance_order": [s.sop_uid for s in slices],
        # Per-slice geometry shared with the DICOMweb WADO-RS frames endpoint:
        # frame_map[i] is the (SOP Instance UID, 1-based file-order frame
        # number) that serves the SAME physical slice as the i-th z-ordered
        # volume slice — i.e. /mpr/slice?plane=axial&index=i ↔ WADO-RS
        # .../instances/{sop}/frames/{frame}. Lets thin clients toggle between
        # raw frames (WADO-RS) and resliced planes of the same multi-frame
        # instance without re-deriving the ordering.
        "frame_map": [
            {"sop_instance_uid": s.sop_uid.split("#")[0],
             "frame": (s.frame if s.frame is not None else 0) + 1}
            for s in slices
        ],
    }
    _bundle_cache_put(cache_key, info, slices)
    return info, slices


async def volume_info(db: AsyncSession, series_uid: str, *,
                      tenant_id: int | None = None,
                      include_all: bool = False,
                      variant: VolumeVariant = "auto") -> dict | None:
    bundle = await volume_bundle(
        db, series_uid, tenant_id=tenant_id, include_all=include_all, variant=variant
    )
    return bundle[0] if bundle else None


def _frame_array(s: VolumeSlice, cache: dict | None = None):
    """The 2D (or 2D+samples) pixel array of one slice, extracting the frame
    for multi-frame instances. ``cache`` (dict keyed by file_path) avoids
    re-reading the same instance for consecutive frames of one file."""
    ds = None
    if cache is not None and s.file_path in cache:
        ds = cache[s.file_path]
    if ds is None:
        ds = storage_service.read_dataset(s.file_path, backend_name=s.backend)
        if cache is not None:
            cache[s.file_path] = ds
    arr = ds.pixel_array
    if s.frame is not None:
        if arr.ndim < 3 or s.frame >= arr.shape[0]:
            raise ValueError(
                f"frame {s.frame} out of range for {s.sop_uid} (shape {tuple(arr.shape)})"
            )
        arr = arr[s.frame]
    return arr


def iter_volume_bytes(
    slices: list[VolumeSlice],
    rescale: bool = False,
    start: int = 0,
    end: int | None = None,
    downsample: int = 1,
) -> Iterator[bytes]:
    """Yield the raw voxel bytes of ``slices[start:end]``, one slice per chunk.

    When ``downsample > 1``, spatial sampling downsamples along Z, Y, and X axes,
    reducing payload volume by downsample^3 for instant coarse-level streaming.
    Without ``rescale`` the original stored values are emitted verbatim
    (little-endian, dtype from the header) — the frontend applies the rescale
    LUT itself.  With ``rescale`` each slice is converted to float32 via its
    own RescaleSlope/RescaleIntercept (Hounsfield Units for CT).
    """
    if not slices:
        return
    end = len(slices) if end is None else min(end, len(slices))
    step = max(1, int(downsample or 1))
    selected_slices = slices[start:end:step]
    if not selected_slices:
        return

    first = storage_service.read_dataset(
        selected_slices[0].file_path, backend_name=selected_slices[0].backend
    )
    expected_shape = _expected_shape(first)
    del first
    deskew_plan = build_deskew_plan_from_slices(slices, expected_shape[:2])
    cache: dict = {}

    for pos, s in enumerate(selected_slices):
        slice_index = start + pos * step
        try:
            arr = _facade()._frame_array(s, cache)
        except Exception as exc:  # noqa: BLE001 - boundary guard: surface any decode failure
            from app.core.dicom_diagnostics import decode_context

            # `_frame_array` caches the Dataset it read, so on a decode failure
            # the dataset is usually still available for context. When it is not
            # (the read itself failed) fall back to the slice identity.
            ds = cache.get(s.file_path) if cache else None
            log.error(
                "Cannot decode volume slice (error_type=%s) | %s",
                type(exc).__name__,
                decode_context(ds, frame=s.frame) if ds is not None
                else f"sop={s.sop_uid} frame={s.frame} dataset_unreadable",
            )
            # Do not include decoder exception text: third-party codec errors
            # may echo dataset values. Suppress the cause in the public message.
            raise ValueError(
                f"Cannot decode volume slice SOP {s.sop_uid} frame={s.frame}"
            ) from None
        if tuple(arr.shape) != expected_shape:
            raise ValueError(
                f"slice {s.sop_uid} shape {tuple(arr.shape)} diverges from "
                f"stack shape {expected_shape} — not a uniform volume"
            )
        if rescale:
            ds = cache.get(s.file_path) or storage_service.read_dataset(
                s.file_path, backend_name=s.backend
            )
            calibration = s.calibration or resolve_frame_calibration(ds, s.frame or 0)
            arr = apply_frame_calibration(arr, calibration)
            if deskew_plan is not None and deskew_plan.active:
                arr = deskew_slice_2d(arr, deskew_plan, slice_index)
        elif deskew_plan is not None and deskew_plan.active:
            padding_val = (
                s.calibration.padding_low
                if s.calibration is not None and s.calibration.padding_low is not None
                else None
            )
            arr = deskew_raw_slice_2d(arr, deskew_plan, slice_index, padding_value=padding_val)
        if step > 1:
            arr = arr[::step, ::step]
        yield np.ascontiguousarray(arr).tobytes()
        # The generator stays suspended at yield. Drop both the Dataset and its
        # decoded pixel_array before reading another SOP instance.
        cache.clear()
        del arr
        if rescale:
            del ds


def _expected_shape(ds) -> tuple[int, ...]:
    rows = int(getattr(ds, "Rows", 0) or 0)
    cols = int(getattr(ds, "Columns", 0) or 0)
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    if samples > 1:
        return (rows, cols, samples)
    return (rows, cols)


def volume_byte_length(slices: list[VolumeSlice], dtype: str,
                       rows: int, cols: int, samples: int,
                       start: int = 0, end: int | None = None,
                       downsample: int = 1) -> int:
    """Total payload bytes for the given slice range (no rescale).

    Must stay byte-exact with ``iter_volume_bytes`` for the same arguments
    (H13): the sampler strides with ``arr[::step, ::step]``, so the output
    geometry is ``strided_dim`` — never floor division.
    """
    end = len(slices) if end is None else min(end, len(slices))
    step = max(1, int(downsample or 1))
    count = len(slices[start:end:step])
    out_rows = strided_dim(rows, step)
    out_cols = strided_dim(cols, step)
    per_slice = out_rows * out_cols * samples * np.dtype(dtype).itemsize
    return count * per_slice


def _tenant_token(tenant_id: int | None) -> str:
    """Render a tenant id for use inside a cache key (C-6).

    ``None`` is the legacy/unscoped bucket and gets the literal ``"none"`` — a
    distinct token, never collapsed into a real tenant id, so an unscoped read
    can never hit (or populate) a tenant's entry.
    """
    if tenant_id is None:
        return "none"
    try:
        return str(int(tenant_id))
    except (TypeError, ValueError) as exc:
        # A non-numeric tenant identifier is still a *distinct* scope; keep it
        # verbatim rather than dropping the scope and risking a collision.
        # Type only — the token names a tenant, which is not ours to log.
        log.debug("scope_token_non_numeric error_type=%s", type(exc).__name__)
        return str(tenant_id)


def stack_fingerprint(slices: list[VolumeSlice]) -> str:
    """Stable digest of a stack's exact frame composition (C-6).

    The frame identity is the set of ``(SOPInstanceUID, frame)`` pairs, so two
    different stacks that happen to have the same slice count get different
    keys. A bare count (what the old key used) made a partially-arrived series
    and a complete one collide.
    """
    digest = hashlib.sha256()
    digest.update(b"paxs-volume-calibration-padding-v2\0")
    geometry = slices[0].series_geometry if slices else None
    if geometry is not None and geometry.has_in_plane_displacement:
        digest.update(b"deskew-v1:")
        digest.update(
            repr((
                bool(getattr(settings, "VOLUME_GANTRY_TILT_RESAMPLE_ENABLED", True)),
                geometry.has_linear_gantry_tilt,
                tuple(tuple(round(v, 4) for v in offset) for offset in geometry.in_plane_offsets_mm),
            )).encode("ascii")
        )
        digest.update(b"\0")
    for s in slices:
        digest.update(str(s.sop_uid).encode("utf-8"))
        digest.update(b"#")
        digest.update(str(s.frame if s.frame is not None else -1).encode("ascii"))
        calibration = s.calibration
        if calibration is not None:
            digest.update(repr((
                calibration.slope, calibration.intercept, calibration.valid,
                calibration.padding_low, calibration.padding_high,
                calibration.rescale_type,
            )).encode("ascii"))
        digest.update(b"|")
    return digest.hexdigest()[:16]


def volume_cache_key(
    tenant_id: int | None,
    study_uid: str | None,
    series_uid: str | None,
    frame_hash: str,
) -> str:
    """Tenant-scoped identity of a materialised volume (C-6).

    Format: ``tenant:{tenant_id}:volume:{study_uid}:{series_uid}:{frame_hash}``.

    The tenant is the **first** component on purpose: it is what makes a
    cross-tenant hit impossible even when two institutions hold an identical
    StudyInstanceUID/SeriesInstanceUID pair (a real case in multi-institutional
    testing, and exactly how tenant B used to read tenant A's pre-rendered
    volume out of the shared cache).
    """
    return (
        f"tenant:{_tenant_token(tenant_id)}:volume:"
        f"{study_uid or 'none'}:{series_uid or 'none'}:{frame_hash}"
    )


def volume_redis_key(cache_key: str) -> str:
    """The Redis key for a volume cache key (``paxs:vol:{cache_key}``)."""
    return f"paxs:vol:{cache_key}"


def volume_redis_scan_pattern(tenant_id: int | None, series_uid: str) -> str:
    """Redis SCAN pattern matching every cached volume of one tenant+series.

    Tenant-scoped so a warm pass (or an operator inspecting the cache) only ever
    sees the calling tenant's entries.
    """
    return f"paxs:vol:tenant:{_tenant_token(tenant_id)}:volume:*:{series_uid}:*"
