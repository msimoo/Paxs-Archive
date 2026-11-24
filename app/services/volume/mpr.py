"""Oblique / orthogonal slice resampling (MPR).

Everything here works in **patient coordinates**: a requested plane is defined
by an origin and two direction cosines, and each output pixel is trilinearly
sampled from the volume. There is no index-based fallback — a geometry that
cannot be resolved is refused rather than guessed, because a wrong plane is a
wrong anatomical claim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from app.core.config import settings
from app.services.volume.geometry_validation import (
    GeometryErrorCode,
    GeometryValidationError,
)

from app.services import storage_service
from app.services.volume.calibration import apply_frame_calibration, resolve_frame_calibration
from app.services.volume.cache import load_volume_array
from app.services.volume.deskew import build_deskew_plan_from_slices, deskew_slice_2d
from app.services.volume.slices import VolumeSlice, _float

log = logging.getLogger("paxs.volume")

__all__ = [
    "RenderedPlane",
    "ResliceResult",
    "_patient_transform",
    "_patient_transform_spacing",
    "_sample_grid",
    "_trilinear",
    "_unit",
    "nan_to_float32",
    "plane_dims",
    "plane_index_max",
    "plane_spacing",
    "render_plane_png",
    "reslice_native_plane_streamed",
    "reslice_plane",
    "window_for",
]


def plane_dims(info: dict, plane: str) -> tuple[int, int]:
    """``(n_v, n_u)`` of a native plane — the shape the server reslices.

    axial: u = i (cols), v = j (rows)      → (rows,  cols)
    sagittal: u = j (rows), v = k (slices) → (slices, rows)
    coronal: u = i (cols), v = k (slices)  → (slices, cols)
    """
    plane = str(plane).lower()
    if plane == "axial":
        return int(info["rows"]), int(info["columns"])
    if plane == "sagittal":
        return int(info["slices"]), int(info["rows"])
    if plane == "coronal":
        return int(info["slices"]), int(info["columns"])
    raise ValueError("plane must be 'axial', 'sagittal' or 'coronal'")


def plane_spacing(info: dict, plane: str) -> list[float]:
    """``[row_mm, col_mm]`` of a resliced native plane, in the same (v, u)
    order as :func:`plane_dims` — mirrors ``ResliceResult.spacing``."""
    row_sp, col_sp = _patient_transform_spacing(info)
    slice_sp = _patient_transform_slice_spacing(info)
    plane = str(plane).lower()
    if plane == "axial":
        return [row_sp, col_sp]
    if plane == "sagittal":
        return [slice_sp, row_sp]
    if plane == "coronal":
        return [slice_sp, col_sp]
    raise ValueError("plane must be 'axial', 'sagittal' or 'coronal'")


def _patient_transform_spacing(info: dict) -> tuple[float, float]:
    """Return validated in-plane spacing or raise a typed geometry error."""
    spacing = info.get("pixel_spacing")
    if not isinstance(spacing, (list, tuple)) or len(spacing) != 2:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_SPACING, "two PixelSpacing values are required"
        )
    try:
        row_sp, col_sp = (float(value) for value in spacing)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_SPACING, "PixelSpacing values must be numeric"
        ) from exc
    if not np.isfinite(row_sp) or not np.isfinite(col_sp) or row_sp <= 0 or col_sp <= 0:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_SPACING, "PixelSpacing values must be positive and finite"
        )
    return row_sp, col_sp


def plane_index_max(info: dict, plane: str) -> int:
    """Highest valid slice index for a native plane."""
    plane = str(plane).lower()
    if plane == "axial":
        return int(info["slices"]) - 1
    if plane == "sagittal":
        return int(info["columns"]) - 1
    if plane == "coronal":
        return int(info["rows"]) - 1
    raise ValueError("plane must be 'axial', 'sagittal' or 'coronal'")


def _patient_transform(info: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float, float]:
    """Patient-space geometry of the volume stack.

    Returns ``(ipp, Rinv, normal, row_sp, col_sp, slice_sp)`` where the
    forward transform is ``P = ipp + R @ [i, j, k]`` (R columns: col direction
    × col spacing, row direction × row spacing, normal × slice spacing) and
    ``Rinv`` maps patient mm → voxel indices ``[i, j, k]``.
    """
    iop = info.get("image_orientation_patient")
    if not isinstance(iop, (list, tuple)) or len(iop) != 6:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_IOP, "ImageOrientationPatient is required"
        )
    try:
        orientation = np.asarray([float(value) for value in iop], dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeometryValidationError(
            GeometryErrorCode.BAD_IOP, "ImageOrientationPatient must be numeric"
        ) from exc
    if not np.all(np.isfinite(orientation)):
        raise GeometryValidationError(
            GeometryErrorCode.BAD_IOP, "ImageOrientationPatient values must be finite"
        )
    row_vec = orientation[:3]
    col_vec = orientation[3:]
    row_norm = float(np.linalg.norm(row_vec))
    col_norm = float(np.linalg.norm(col_vec))
    iop_tolerance = settings.VOLUME_GEOMETRY_IOP_TOLERANCE
    if (abs(row_norm - 1.0) > iop_tolerance
            or abs(col_norm - 1.0) > iop_tolerance
            or abs(float(np.dot(row_vec, col_vec))) > iop_tolerance):
        raise GeometryValidationError(
            GeometryErrorCode.BAD_IOP,
            "ImageOrientationPatient must contain orthonormal direction cosines",
        )
    normal = np.cross(row_vec, col_vec)
    ipp_value = info.get("image_position_patient")
    if not isinstance(ipp_value, (list, tuple)) or len(ipp_value) != 3:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_IPP, "ImagePositionPatient is required"
        )
    try:
        ipp = np.array([float(v) for v in ipp_value], dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_IPP, "ImagePositionPatient must be numeric"
        ) from exc
    if not np.all(np.isfinite(ipp)):
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_IPP, "ImagePositionPatient values must be finite"
        )
    row_sp, col_sp = _patient_transform_spacing(info)
    slice_sp = _patient_transform_slice_spacing(info)
    # DICOM PS3.3 C.7.6.2.1.1:
    # - i (column index) advances along row_vec (iop[:3]) by col_sp
    # - j (row index) advances along col_vec (iop[3:6]) by row_sp
    # - k (slice index) advances along normal by slice_sp
    r = np.column_stack([row_vec * col_sp, col_vec * row_sp, normal * slice_sp])
    try:
        inverse = np.linalg.inv(r)
    except np.linalg.LinAlgError as exc:
        raise GeometryValidationError(
            GeometryErrorCode.BAD_IOP, "patient-space voxel transform is singular"
        ) from exc
    return ipp, inverse, normal, row_sp, col_sp, slice_sp


def _patient_transform_slice_spacing(info: dict) -> float:
    """Return validated through-plane spacing; thickness is not a substitute."""
    slice_value = info.get("slice_spacing")
    try:
        slice_sp = float(slice_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_SPACING, "validated slice spacing is required"
        ) from exc
    if not np.isfinite(slice_sp) or slice_sp <= 0:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_SPACING, "validated slice spacing must be positive and finite"
        )
    return slice_sp


def _trilinear(vol: np.ndarray, i: np.ndarray, j: np.ndarray, k: np.ndarray) -> np.ndarray:
    """Vectorised trilinear sampling of ``vol[k, j, i]``; OOB → NaN."""
    nz, ny, nx = vol.shape
    i0 = np.floor(i).astype(np.int64)
    j0 = np.floor(j).astype(np.int64)
    k0 = np.floor(k).astype(np.int64)
    i1, j1, k1 = i0 + 1, j0 + 1, k0 + 1
    # A sample point is valid when it lies inside [0, n-1]; the corner reads
    # are clamped, so a point exactly on the last voxel (e.g. k = nz-1) is
    # valid even though k1 == nz.
    valid = (
        (i >= 0) & (i <= nx - 1) &
        (j >= 0) & (j <= ny - 1) &
        (k >= 0) & (k <= nz - 1)
    )

    def g(ix, jx, kx):
        ix = np.clip(ix, 0, nx - 1)
        jx = np.clip(jx, 0, ny - 1)
        kx = np.clip(kx, 0, nz - 1)
        return vol[kx, jx, ix]

    fi, fj, fk = i - i0, j - j0, k - k0
    c000 = g(i0, j0, k0)
    c100 = g(i1, j0, k0)
    c010 = g(i0, j1, k0)
    c110 = g(i1, j1, k0)
    c001 = g(i0, j0, k1)
    c101 = g(i1, j0, k1)
    c011 = g(i0, j1, k1)
    c111 = g(i1, j1, k1)
    values = (c000, c100, c010, c110, c001, c101, c011, c111)
    weights = (
        (1-fi)*(1-fj)*(1-fk), fi*(1-fj)*(1-fk),
        (1-fi)*fj*(1-fk), fi*fj*(1-fk),
        (1-fi)*(1-fj)*fk, fi*(1-fj)*fk,
        (1-fi)*fj*fk, fi*fj*fk,
    )
    weighted = [np.where(np.isfinite(value), value * weight, 0.0)
                for value, weight in zip(values, weights)]
    valid_weights = [np.where(np.isfinite(value), weight, 0.0)
                     for value, weight in zip(values, weights)]
    weight_sum = sum(valid_weights)
    out = np.divide(
        sum(weighted), weight_sum,
        out=np.full_like(weight_sum, np.nan, dtype=np.float64),
        where=weight_sum > 0,
    )
    return np.where(valid, out, np.nan)


@dataclass
class ResliceResult:
    """One resliced plane: HU float32 data + geometry metadata."""

    data: np.ndarray          # float32 (n_v, n_u); NaN outside the volume
    rows: int                 # n_v
    cols: int                 # n_u
    spacing: list[float]      # [row(mm), col(mm)] of the output plane
    plane: str                # axial | sagittal | coronal | oblique
    index: int | None         # native-plane index (None for oblique)
    origin: list[float] | None
    row_vector: list[float] | None
    col_vector: list[float] | None


@dataclass
class RenderedPlane:
    """A native plane rasterized server-side, for thin-client viewing.

    Carries the resolved window so that the caller reporting ``X-MPR-Window``
    does not have to re-run ``window_for`` (an autoscale over the plane).
    ``center``/``width`` are None when the caller requested an auto window and
    the response came from a cache entry whose window is not re-derivable
    without voxels.
    """

    png: bytes
    plane: str
    index: int
    center: float | None
    width: float | None
    rows: int
    cols: int
    spacing: list[float]
    origin: list[float] | None


def _sample_grid(vol: np.ndarray, ipp: np.ndarray, rinv: np.ndarray,
                 origin: np.ndarray, u_vec: np.ndarray, v_vec: np.ndarray,
                 n_u: int, n_v: int, sp_u: float, sp_v: float,
                 centered: bool = False) -> np.ndarray:
    """Sample a plane: ``data[v, u] = P(u, v)`` where
    ``P = origin + u*sp_u*u_vec + v*sp_v*v_vec`` mapped through the inverse
    patient→voxel transform.

    ``centered=False`` anchors the grid at ``origin`` (u = 0 → voxel 0 along
    that axis) so native planes land exactly on voxel centres; ``centered=True``
    centres the grid on ``origin`` (for oblique planes defined by a point).
    """
    if centered:
        u = (np.arange(n_u) - (n_u - 1) / 2.0) * sp_u
        v = (np.arange(n_v) - (n_v - 1) / 2.0) * sp_v
    else:
        u = np.arange(n_u) * sp_u
        v = np.arange(n_v) * sp_v
    uu, vv = np.meshgrid(u, v)
    p = origin[None, None, :] + uu[..., None] * u_vec[None, None, :] + vv[..., None] * v_vec[None, None, :]
    vox = (p - ipp[None, None, :]) @ rinv.T
    return _trilinear(vol, vox[..., 0], vox[..., 1], vox[..., 2])


def reslice_plane(
    slices: list[VolumeSlice],
    info: dict,
    plane: str,
    index: int | None = None,
    origin: list[float] | None = None,
    row_vector: list[float] | None = None,
    col_vector: list[float] | None = None,
    size: tuple[int, int] | None = None,
    max_pixels: int = 4_000_000,
    tenant_id: int | None = None,
    downsample: int = 1,
) -> ResliceResult:
    """Reslice one plane of the volume (server-side, trilinear).

    ``tenant_id`` scopes the underlying volume cache (C-6) so one institution's
    materialised voxels are never reused for another.
    """
    nz = int(info["slices"])
    ny = int(info["rows"])
    nx = int(info["columns"])
    vol = load_volume_array(
        slices,
        series_uid=info.get("series_instance_uid"),
        tenant_id=tenant_id,
        study_uid=info.get("study_instance_uid"),
    )
    ipp, rinv, normal, row_sp, col_sp, slice_sp = _patient_transform(info)
    # unit direction cosines of the source stack (from ImageOrientationPatient)
    iop = info["image_orientation_patient"]
    stack_row = np.array([float(v) for v in iop[:3]])
    stack_col = np.array([float(v) for v in iop[3:6]])

    plane = plane.lower()
    sample_step = int(downsample)
    if sample_step < 1:
        raise ValueError("downsample must be a positive integer")
    if plane == "oblique" and sample_step != 1:
        raise ValueError("downsample is supported only for native orthogonal planes")

    if plane in ("axial", "sagittal", "coronal"):
        if index is None:
            raise ValueError(f"index is required for plane={plane}")
        index = int(index)
        if plane == "axial":
            if not 0 <= index < nz:
                raise IndexError(f"axial index {index} out of range 0-{nz - 1}")
            data = vol[index].copy()
            return _sample_native_result(ResliceResult(
                data=data, rows=ny, cols=nx,
                spacing=[row_sp, col_sp], plane="axial", index=index,
                origin=[float(v) for v in (ipp + index * slice_sp * normal)],
                row_vector=[float(v) for v in stack_row],
                col_vector=[float(v) for v in stack_col],
            ), sample_step)
        if plane == "sagittal":
            if not 0 <= index < nx:
                raise IndexError(f"sagittal index {index} out of range 0-{nx - 1}")
            # plane spans rows (j) × slices (k): data[k, j] = vol[k, j, index]
            origin_pt = ipp + index * col_sp * stack_row
            data = np.ascontiguousarray(vol[:, :, index], dtype=np.float32)
            return _sample_native_result(ResliceResult(
                data=data, rows=nz, cols=ny,
                spacing=[slice_sp, row_sp], plane="sagittal", index=index,
                origin=[float(v) for v in origin_pt],
                row_vector=[float(v) for v in stack_col],
                col_vector=[float(v) for v in normal],
            ), sample_step)
        # coronal: spans columns (i) × slices (k): data[k, i] = vol[k, index, i]
        if not 0 <= index < ny:
            raise IndexError(f"coronal index {index} out of range 0-{ny - 1}")
        origin_pt = ipp + index * row_sp * stack_col
        data = np.ascontiguousarray(vol[:, index, :], dtype=np.float32)
        return _sample_native_result(ResliceResult(
            data=data, rows=nz, cols=nx,
            spacing=[slice_sp, col_sp], plane="coronal", index=index,
            origin=[float(v) for v in origin_pt],
            row_vector=[float(v) for v in stack_row],
            col_vector=[float(v) for v in normal],
        ), sample_step)

    # ---- oblique plane (patient space) ----
    if origin is None or row_vector is None or col_vector is None:
        raise ValueError("oblique planes require origin, row and col")
    o = np.array([float(v) for v in origin])
    r = _unit(row_vector)
    c = _unit(col_vector)
    if size is None:
        size = (nx, ny)
    n_u, n_v = int(size[0]), int(size[1])
    if n_u <= 0 or n_v <= 0 or n_u * n_v > max_pixels:
        raise ValueError(f"plane size {n_u}x{n_v} exceeds max_pixels={max_pixels}")
    data = _sample_grid(vol, ipp, rinv, o, r, c, n_u, n_v, col_sp, row_sp,
                        centered=True)
    return ResliceResult(
        data=data, rows=n_v, cols=n_u,
        spacing=[row_sp, col_sp], plane="oblique", index=None,
        origin=[float(v) for v in o],
        row_vector=[float(v) for v in r],
        col_vector=[float(v) for v in c],
    )


def _sample_native_result(result: ResliceResult, sample_step: int) -> ResliceResult:
    """Stride both raster axes and scale its physical spacing by the same factor."""
    if sample_step == 1:
        return result
    sampled = result.data[::sample_step, ::sample_step]
    return ResliceResult(
        data=sampled,
        rows=int(sampled.shape[0]),
        cols=int(sampled.shape[1]),
        spacing=[float(value) * sample_step for value in result.spacing],
        plane=result.plane,
        index=result.index,
        origin=result.origin,
        row_vector=result.row_vector,
        col_vector=result.col_vector,
    )


def reslice_native_plane_streamed(
    slices: list[VolumeSlice], info: dict, plane: str, index: int,
    *, max_pixels: int, downsample: int = 1,
) -> ResliceResult:
    """Extract one native plane while retaining at most one source frame.

    This capacity fallback keeps Server/Split MPR usable when a complete
    float32 volume cannot fit the configured assembly budget. Orthogonal native
    planes are exact index selections from the same ordered source stack used
    by :func:`reslice_plane`; oblique reslicing still requires the full-volume
    path and remains fail-closed when that volume cannot be assembled.
    """
    plane = str(plane).lower()
    if plane not in ("axial", "sagittal", "coronal"):
        raise ValueError("streamed extraction supports native orthogonal planes only")
    rows, cols = int(info["rows"]), int(info["columns"])
    depth = len(slices)
    if depth <= 0:
        raise ValueError("native plane extraction requires at least one source frame")
    if int(info.get("samples_per_pixel", 1)) != 1:
        raise ValueError("streamed native planes require single-sample pixels")
    sample_step = max(1, int(downsample))
    if plane == "axial":
        out_rows = (rows + sample_step - 1) // sample_step
        out_cols = (cols + sample_step - 1) // sample_step
        limit = depth
    elif plane == "sagittal":
        out_rows = (depth + sample_step - 1) // sample_step
        out_cols = (rows + sample_step - 1) // sample_step
        limit = cols
    else:
        out_rows = (depth + sample_step - 1) // sample_step
        out_cols = (cols + sample_step - 1) // sample_step
        limit = rows
    if not 0 <= index < limit:
        raise IndexError(f"{plane} index {index} out of range 0-{limit - 1}")
    if out_rows <= 0 or out_cols <= 0 or out_rows * out_cols > max_pixels:
        raise ValueError(f"{plane} plane exceeds VOLUME_MPR_MAX_PIXELS ({max_pixels})")

    ipp, _rinv, normal, row_sp, col_sp, slice_sp = _patient_transform(info)
    iop = info["image_orientation_patient"]
    stack_row = np.asarray(iop[:3], dtype=np.float64)
    stack_col = np.asarray(iop[3:6], dtype=np.float64)
    source_rows = int(info.get("source_rows") or rows)
    source_cols = int(info.get("source_columns") or cols)
    deskew_plan = build_deskew_plan_from_slices(slices, (source_rows, source_cols))
    expected_source_shape = (
        (deskew_plan.source_rows, deskew_plan.source_cols)
        if deskew_plan is not None and deskew_plan.active
        else (rows, cols)
    )
    data = np.empty((out_rows, out_cols), dtype=np.float32)
    source_path = None
    dataset = None
    selected = [(0, index, slices[index])] if plane == "axial" else (
        (output_index, source_index, slices[source_index])
        for output_index, source_index in enumerate(range(0, depth, sample_step))
    )
    for output_index, source_index, source in selected:
        if source.file_path != source_path:
            dataset = storage_service.read_dataset(
                source.file_path, backend_name=source.backend
            )
            source_path = source.file_path
        pixels = dataset.pixel_array
        if source.frame is not None:
            if pixels.ndim < 3 or source.frame >= pixels.shape[0]:
                raise ValueError("native plane frame is outside the decoded instance")
            pixels = pixels[source.frame]
        if tuple(pixels.shape[:2]) != expected_source_shape:
            raise ValueError("native plane source dimensions do not match its volume")
        calibration = source.calibration or resolve_frame_calibration(
            dataset, source.frame or 0
        )
        if deskew_plan is not None and deskew_plan.active:
            calibrated_full = apply_frame_calibration(pixels, calibration)
            deskewed = deskew_slice_2d(calibrated_full, deskew_plan, source_index)
            if plane == "axial":
                calibrated = deskewed[::sample_step, ::sample_step]
            elif plane == "sagittal":
                calibrated = deskewed[::sample_step, index]
            else:
                calibrated = deskewed[index, ::sample_step]
            del calibrated_full, deskewed
        else:
            if plane == "axial":
                source_plane = pixels[::sample_step, ::sample_step]
            elif plane == "sagittal":
                source_plane = pixels[::sample_step, index]
            else:
                source_plane = pixels[index, ::sample_step]
            calibrated = apply_frame_calibration(source_plane, calibration)
        target = data if plane == "axial" else data[output_index]
        target[...] = calibrated
        del pixels

    if plane == "axial":
        origin = ipp + index * slice_sp * normal
        spacing = [row_sp * sample_step, col_sp * sample_step]
        row_vector, col_vector = stack_row, stack_col
    elif plane == "sagittal":
        origin = ipp + index * col_sp * stack_row
        spacing = [slice_sp * sample_step, row_sp * sample_step]
        row_vector, col_vector = stack_col, normal
    else:
        origin = ipp + index * row_sp * stack_col
        spacing = [slice_sp * sample_step, col_sp * sample_step]
        row_vector, col_vector = stack_row, normal
    return ResliceResult(
        data=data, rows=out_rows, cols=out_cols, spacing=spacing, plane=plane,
        index=index, origin=[float(v) for v in origin],
        row_vector=[float(v) for v in row_vector],
        col_vector=[float(v) for v in col_vector],
    )


def _unit(vec: list[float] | None) -> np.ndarray:
    if vec is None:
        return np.array([0.0, 0.0, 0.0])
    v = np.array([float(x) for x in vec])
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 1e-12 else np.array([0.0, 0.0, 0.0])


def window_for(info: dict, data: np.ndarray,
               window_center: float | None = None,
               window_width: float | None = None) -> tuple[float, float]:
    """Resolve the window (center, width) — explicit params win, then the
    series window, then a robust autoscale of the plane's 2nd/98th percentile."""
    if window_center is not None and window_width is not None:
        return float(window_center), float(window_width)
    if info.get("window_center") is not None and info.get("window_width") is not None:
        return float(info["window_center"]), float(info["window_width"])
    finite = data[np.isfinite(data)]
    if info.get("pixel_padding_min") is not None:
        finite = finite[(finite < info["pixel_padding_min"]) | (finite > info["pixel_padding_max"])]
    if finite.size == 0:
        return 0.0, 1.0
    lo, hi = np.percentile(finite, [2, 98])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo, hi = float(finite.min()), float(finite.max())
        if hi <= lo:
            return 0.0, 1.0
    return (lo + hi + 1) / 2.0, hi - lo + 1


def nan_to_float32(data: np.ndarray) -> np.ndarray:
    """Preserve missing raw voxels as NaN; zero is a real calibrated CT value."""
    return np.where(np.isfinite(data), data, np.nan).astype(np.float32)


def render_plane_png(data: np.ndarray, window_center: float,
                     window_width: float) -> bytes:
    """Render the plane to 8-bit grayscale PNG with the given window."""
    import io as _io

    from PIL import Image
    from app.services.pixel_pipeline import linear_window
    u8 = linear_window(data, float(window_center), float(window_width))
    u8 = np.nan_to_num(u8, nan=0, posinf=255, neginf=0)
    img = Image.fromarray(np.rint(u8).astype(np.uint8), mode="L")
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
