"""Slab projections: MIP / MinIP / average / VRT.

``mip_project`` walks the volume along one axis and reduces each slab. The VRT
mode is a bounded ray-cast, not a full transfer-function renderer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from app.services.volume.cache import load_volume_array
from app.services.volume.mpr import window_for
from app.services.volume.slices import VolumeSlice

log = logging.getLogger("paxs.volume")

__all__ = [
    "_MIP_AXES",
    "_MIP_MODES",
    "MipResult",
    "_projection_spacing",
    "_vrt_raycast",
    "mip_project",
]


_MIP_AXES: dict[str, int] = {"z": 0, "y": 1, "x": 2}


_MIP_MODES: tuple[str, ...] = ("mip", "minip", "average", "vrt")


def _vrt_raycast(vol: np.ndarray, axis: int, wc: float, ww: float) -> np.ndarray:
    """Front-to-back alpha-composited volume render along ``axis``.

    A simple but legitimate server-side VRT: each voxel's opacity ramps from
    0 to 1 across the display window (smoothstep), color = the HU intensity
    itself, accumulated with classic Porter-Duff "over" compositing.  Returns
    the normalized composite intensity (float32, 0 where no voxel contributed
    within the window).
    """
    half = max(float(ww), 1e-6) / 2.0
    lo = float(wc) - half
    valid = np.isfinite(vol)
    t = (vol - lo) / (2.0 * half)
    t = np.clip(t, 0.0, 1.0)
    opacity = np.where(valid, t * t * (3.0 - 2.0 * t), 0.0)  # smoothstep
    color = np.where(valid & (opacity > 0.0), vol, 0.0)

    n = vol.shape[axis]
    acc_color = np.zeros(vol.shape[:axis] + vol.shape[axis + 1:], dtype=np.float32)
    acc_alpha = np.zeros_like(acc_color)
    for i in range(n):
        idx = (slice(None),) * axis + (i,)
        a = opacity[idx]
        c = color[idx]
        # front-to-back over: C' = C + (1 - A) * a * c ; A' = A + (1 - A) * a
        remain = 1.0 - acc_alpha
        acc_color += remain * a * c
        acc_alpha += remain * a
    out = np.divide(acc_color, acc_alpha, out=np.zeros_like(acc_color),
                    where=acc_alpha > 1e-8)
    return out.astype(np.float32)


@dataclass
class MipResult:
    """One projected image: float32 HU data + geometry metadata."""

    data: np.ndarray          # float32, 2D
    rows: int
    cols: int
    axis: str                 # z | x | y
    mode: str                 # mip | minip | average | vrt
    offset: int               # first stack slice of the slab
    thickness: int            # number of stack slices projected
    depth: int                # total stack depth along the axis
    spacing: list[float]      # [row(mm), col(mm)] of the output image
    window: tuple[float, float] | None  # resolved (center, width) used for vrt


def mip_project(
    slices: list[VolumeSlice],
    info: dict,
    axis: str = "z",
    mode: str = "mip",
    offset: int = 0,
    thickness: int | None = None,
    window_center: float | None = None,
    window_width: float | None = None,
    tenant_id: int | None = None,
) -> MipResult:
    """Project the volume along one axis over a slab of ``thickness`` slices.

    ``axis`` is the patient axis (z = stack / axial, x = sagittal columns,
    y = coronal rows).  ``offset`` is the 0-based first stack slice; without
    ``thickness`` the slab runs to the end of the stack.  Modes: ``mip``
    (maximum), ``minip`` (minimum), ``average`` (X-ray-like mean) and ``vrt``
    (alpha-composited volume render using the display window as the opacity
    ramp).  Raises ValueError/IndexError like ``reslice_plane`` for callers to
    map onto HTTP errors.
    """
    if axis not in _MIP_AXES:
        raise ValueError(f"axis must be one of {sorted(_MIP_AXES)} (got {axis!r})")
    if mode not in _MIP_MODES:
        raise ValueError(f"mode must be one of {_MIP_MODES} (got {mode!r})")

    vol = load_volume_array(
        slices,
        series_uid=info.get("series_instance_uid"),
        tenant_id=tenant_id,
        study_uid=info.get("study_instance_uid"),
    )
    ax = _MIP_AXES[axis]
    # The slab runs along the *projection* axis, so its depth is the stack
    # depth for z but the number of columns/rows for x/y.
    nz, ny, nx = vol.shape
    depth = {0: nz, 1: ny, 2: nx}[ax]
    offset = int(offset)
    if offset < 0 or offset >= depth:
        raise IndexError(f"offset {offset} out of range 0-{depth - 1}")
    end = depth if thickness is None else min(int(thickness) + offset, depth)
    if end <= offset:
        raise IndexError(f"thickness {thickness} leaves an empty slab")

    slab = np.take(vol, range(offset, end), axis=ax)
    valid = np.isfinite(slab)
    any_valid = np.any(valid, axis=ax)

    if mode == "mip":
        data = np.max(np.where(valid, slab, -np.inf), axis=ax)
        data = np.where(any_valid, data, np.nan)
    elif mode == "minip":
        data = np.min(np.where(valid, slab, np.inf), axis=ax)
        data = np.where(any_valid, data, np.nan)
    elif mode == "average":
        count = np.sum(valid, axis=ax)
        data = np.divide(
            np.sum(np.where(valid, slab, 0.0), axis=ax), count,
            out=np.full(count.shape, np.nan, dtype=np.float32), where=count > 0,
        )
    else:  # vrt
        wc, ww = window_for(info, slab, window_center, window_width)
        data = _vrt_raycast(slab, ax, wc, ww)
        data = data.astype(np.float32)

    row_sp, col_sp = _projection_spacing(info)
    slice_sp = float(info.get("slice_spacing") or info.get("slice_thickness") or 1.0)

    # The projection removes the axis dimension, so the output is shaped
    # like the corresponding native plane: z → (rows, cols), x → (depth,
    # rows), y → (depth, cols) — same orientation convention as the MPR
    # reslicer's axial/sagittal/coronal outputs.
    if axis == "z":
        rows, cols, spacing = ny, nx, [row_sp, col_sp]
    elif axis == "x":
        rows, cols, spacing = nz, ny, [slice_sp, row_sp]
    else:  # y
        rows, cols, spacing = nz, nx, [slice_sp, col_sp]

    window = None
    if mode == "vrt":
        wc, ww = window_for(info, slab, window_center, window_width)
        window = (float(wc), float(ww))
    return MipResult(
        data=np.ascontiguousarray(data, dtype=np.float32),
        rows=int(rows), cols=int(cols), axis=axis, mode=mode,
        offset=int(offset), thickness=int(end - offset), depth=depth,
        spacing=spacing, window=window,
    )


def _projection_spacing(info: dict) -> tuple[float, float]:
    spacing = info.get("pixel_spacing") or [1.0, 1.0]
    return float(spacing[0]), float(spacing[1])
