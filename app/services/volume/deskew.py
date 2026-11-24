"""Pure-NumPy gantry-tilt deskewing into an orthogonal regular patient-space grid.

DICOM CT stacks acquired with a tilted gantry share a uniform ImageOrientationPatient
and uniform projected slice spacing along the plane normal, while each slice's
ImagePositionPatient translates linearly in-plane along the column/row axes.

Without deskewing, treating the stack as an orthogonal array ``vol[k, j, i]``
shears every sagittal, coronal, oblique and 3D reconstruction. This module
computes the exact regular orthogonal bounding grid in patient space and
resamples each slice in-plane with vectorized pure-NumPy bilinear interpolation
(preserving NaN padding and never fabricating tissue outside the scanned FOV).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from app.core.config import settings
from app.services.volume.geometry_validation import (
    GeometryErrorCode,
    GeometryValidationError,
    SeriesGeometry,
    _dot,
)
from app.services.volume.slices import VolumeSlice

__all__ = [
    "DeskewPlan",
    "build_deskew_plan",
    "build_deskew_plan_from_slices",
    "deskew_raw_slice_2d",
    "deskew_slice_2d",
]


@dataclass(frozen=True, slots=True)
class DeskewPlan:
    """Deterministic orthogonal-grid plan for a validated volume stack."""

    active: bool
    source_rows: int
    source_cols: int
    out_rows: int
    out_cols: int
    shift_i_px: tuple[float, ...]
    shift_j_px: tuple[float, ...]
    origin_offset_i_px: int
    origin_offset_j_px: int
    orthogonal_ipp: tuple[float, float, float]
    gantry_tilt_deg: float | None
    max_in_plane_displacement_mm: float


def build_deskew_plan(
    geometry: SeriesGeometry,
    source_rows: int,
    source_cols: int,
    *,
    enabled: bool | None = None,
    max_expansion_factor: float | None = None,
) -> DeskewPlan:
    """Compute a regular orthogonal grid plan for ``geometry``.

    Non-tilted stacks return an inactive identity plan with zero overhead.
    Linearly sheared gantry-tilted stacks produce an expanded bounding-box plan
    when ``enabled`` is True; non-linear displacements, disabled resampling, or
    excessive expansion fail closed with ``UNSUPPORTED_GANTRY_TILT``.
    """
    source_rows = int(source_rows)
    source_cols = int(source_cols)
    if source_rows <= 0 or source_cols <= 0:
        raise ValueError("source_rows and source_cols must be positive")
    first_slice = geometry.slices[0]
    ipp0 = first_slice.image_position_patient
    if not geometry.has_in_plane_displacement:
        zeros = (0.0,) * len(geometry.slices)
        return DeskewPlan(
            active=False,
            source_rows=source_rows,
            source_cols=source_cols,
            out_rows=source_rows,
            out_cols=source_cols,
            shift_i_px=zeros,
            shift_j_px=zeros,
            origin_offset_i_px=0,
            origin_offset_j_px=0,
            orthogonal_ipp=ipp0,
            gantry_tilt_deg=geometry.gantry_tilt_deg,
            max_in_plane_displacement_mm=geometry.max_in_plane_displacement_mm,
        )

    if enabled is None:
        enabled = bool(getattr(settings, "VOLUME_GANTRY_TILT_RESAMPLE_ENABLED", True))
    if not enabled:
        raise GeometryValidationError(
            GeometryErrorCode.UNSUPPORTED_GANTRY_TILT,
            "gantry-tilt volume resampling is disabled",
        )
    if not geometry.has_linear_gantry_tilt:
        raise GeometryValidationError(
            GeometryErrorCode.UNSUPPORTED_GANTRY_TILT,
            "non-linear per-slice in-plane displacement cannot be deskewed into a regular volume",
        )

    # DICOM PS3.3 C.7.6.2.1.1:
    # - iop[:3] is the direction cosine along a row (increasing column index i, spacing col_sp)
    # - iop[3:6] is the direction cosine down a column (increasing row index j, spacing row_sp)
    iop = first_slice.image_orientation_patient
    row_dir = iop[:3]
    col_dir = iop[3:6]
    row_sp, col_sp = first_slice.pixel_spacing

    shift_i_px = tuple(_dot(offset, row_dir) / col_sp for offset in geometry.in_plane_offsets_mm)
    shift_j_px = tuple(_dot(offset, col_dir) / row_sp for offset in geometry.in_plane_offsets_mm)

    min_i = math.floor(min(shift_i_px))
    max_i = math.ceil(max(shift_i_px))
    min_j = math.floor(min(shift_j_px))
    max_j = math.ceil(max(shift_j_px))

    out_cols = source_cols + (max_i - min_i)
    out_rows = source_rows + (max_j - min_j)

    if max_expansion_factor is None:
        max_expansion_factor = float(
            getattr(settings, "VOLUME_GANTRY_TILT_MAX_EXPANSION_FACTOR", 3.0)
        )
    source_area = source_rows * source_cols
    if out_rows * out_cols > max(int(math.ceil(source_area * max_expansion_factor)), source_area):
        raise GeometryValidationError(
            GeometryErrorCode.UNSUPPORTED_GANTRY_TILT,
            "gantry-tilt deskew grid exceeds the configured expansion limit",
        )

    orthogonal_ipp = tuple(
        ipp0[axis] + min_i * col_sp * row_dir[axis] + min_j * row_sp * col_dir[axis]
        for axis in range(3)
    )
    return DeskewPlan(
        active=True,
        source_rows=source_rows,
        source_cols=source_cols,
        out_rows=out_rows,
        out_cols=out_cols,
        shift_i_px=shift_i_px,
        shift_j_px=shift_j_px,
        origin_offset_i_px=min_i,
        origin_offset_j_px=min_j,
        orthogonal_ipp=orthogonal_ipp,
        gantry_tilt_deg=geometry.gantry_tilt_deg,
        max_in_plane_displacement_mm=geometry.max_in_plane_displacement_mm,
    )


def build_deskew_plan_from_slices(
    slices: list[VolumeSlice],
    source_shape: tuple[int, int],
    *,
    enabled: bool | None = None,
    max_expansion_factor: float | None = None,
) -> DeskewPlan | None:
    """Resolve a deskew plan from ``slices[0].series_geometry``, or None if absent."""
    if not slices:
        return None
    geometry = slices[0].series_geometry
    if geometry is None or not geometry.has_in_plane_displacement:
        return None
    return build_deskew_plan(
        geometry,
        source_shape[0],
        source_shape[1],
        enabled=enabled,
        max_expansion_factor=max_expansion_factor,
    )


def deskew_slice_2d(
    pixels: np.ndarray,
    plan: DeskewPlan,
    slice_index: int,
    *,
    fill_value: float = math.nan,
) -> np.ndarray:
    """Resample one calibrated 2D (or 2D+samples) slice onto the orthogonal grid.

    Every output pixel ``(j_out, i_out)`` maps to source coordinate::

        i_src = i_out + (origin_offset_i_px - shift_i_px[slice_index])
        j_src = j_out + (origin_offset_j_px - shift_j_px[slice_index])

    Because the translation is constant across the slice, bilinear interpolation
    reduces to at most four contiguous slice views with scalar weights, preserving
    NaN padding masks without creating 2D coordinate meshgrids.
    """
    arr = np.asarray(pixels, dtype=np.float32)
    if not plan.active:
        return arr

    if arr.shape[:2] != (plan.source_rows, plan.source_cols):
        raise ValueError(
            f"slice shape {tuple(arr.shape[:2])} does not match deskew source "
            f"{(plan.source_rows, plan.source_cols)}"
        )

    delta_i = float(plan.origin_offset_i_px) - float(plan.shift_i_px[slice_index])
    delta_j = float(plan.origin_offset_j_px) - float(plan.shift_j_px[slice_index])

    out_shape = (plan.out_rows, plan.out_cols) + arr.shape[2:]
    out = np.full(out_shape, fill_value, dtype=np.float32)

    # Valid output indices satisfy: 0 <= idx + delta <= src_dim - 1
    eps = 1e-7
    i_out_start = max(0, math.ceil(-delta_i - eps))
    i_out_end = min(plan.out_cols, math.floor((plan.source_cols - 1) - delta_i + eps) + 1)
    j_out_start = max(0, math.ceil(-delta_j - eps))
    j_out_end = min(plan.out_rows, math.floor((plan.source_rows - 1) - delta_j + eps) + 1)
    if i_out_start >= i_out_end or j_out_start >= j_out_end:
        return out

    i_base = math.floor(delta_i)
    j_base = math.floor(delta_j)
    fi = delta_i - i_base
    fj = delta_j - j_base
    if fi < eps:
        fi = 0.0
    elif fi > 1.0 - eps:
        i_base += 1
        fi = 0.0
    if fj < eps:
        fj = 0.0
    elif fj > 1.0 - eps:
        j_base += 1
        fj = 0.0

    # Fast exact-copy path when the shift is an integer pixel offset (zero blur).
    if fi == 0.0 and fj == 0.0:
        src_j0 = j_out_start + j_base
        src_j1 = j_out_end + j_base
        src_i0 = i_out_start + i_base
        src_i1 = i_out_end + i_base
        region = arr[src_j0:src_j1, src_i0:src_i1]
        if math.isnan(fill_value):
            out[j_out_start:j_out_end, i_out_start:i_out_end] = region
        else:
            out[j_out_start:j_out_end, i_out_start:i_out_end] = np.where(
                np.isfinite(region), region, fill_value
            )
        return out

    # General sub-pixel bilinear shift with NaN-aware weight normalization.
    corners = (
        (0, 0, (1.0 - fi) * (1.0 - fj)),
        (0, 1, fi * (1.0 - fj)),
        (1, 0, (1.0 - fi) * fj),
        (1, 1, fi * fj),
    )
    region_shape = (j_out_end - j_out_start, i_out_end - i_out_start) + arr.shape[2:]
    weighted_sum = np.zeros(region_shape, dtype=np.float64)
    weight_total = np.zeros(region_shape, dtype=np.float64)

    for dj, di, weight in corners:
        if weight <= 0.0:
            continue
        src_j = np.clip(
            np.arange(j_out_start, j_out_end, dtype=np.int64) + j_base + dj,
            0,
            plan.source_rows - 1,
        )
        src_i = np.clip(
            np.arange(i_out_start, i_out_end, dtype=np.int64) + i_base + di,
            0,
            plan.source_cols - 1,
        )
        sample = arr[src_j[:, None], src_i[None, :]]
        finite = np.isfinite(sample)
        weighted_sum += np.where(finite, sample.astype(np.float64) * weight, 0.0)
        weight_total += np.where(finite, weight, 0.0)

    blended = np.divide(
        weighted_sum,
        weight_total,
        out=np.full(region_shape, fill_value, dtype=np.float64),
        where=weight_total > 0.0,
    )
    out[j_out_start:j_out_end, i_out_start:i_out_end] = blended.astype(np.float32)
    return out


def deskew_raw_slice_2d(
    pixels: np.ndarray,
    plan: DeskewPlan,
    slice_index: int,
    *,
    padding_value: float | None = None,
) -> np.ndarray:
    """Deskew a raw (un-rescaled) integer slice while preserving its stored dtype."""
    if not plan.active:
        return pixels
    dtype = pixels.dtype
    if padding_value is None:
        if np.issubdtype(dtype, np.signedinteger):
            fill = float(np.iinfo(dtype).min)
        elif np.issubdtype(dtype, np.unsignedinteger):
            fill = 0.0
        else:
            fill = math.nan
    else:
        fill = float(padding_value)
    deskewed = deskew_slice_2d(pixels, plan, slice_index, fill_value=fill)
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        deskewed = np.nan_to_num(deskewed, nan=fill, posinf=float(info.max), neginf=float(info.min))
        return np.clip(np.rint(deskewed), info.min, info.max).astype(dtype)
    return deskewed.astype(dtype)
