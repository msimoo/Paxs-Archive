"""2D MPR backend prep (roadmap #26) — volume geometry metadata + raw volume
streaming for the WebGL MPR frontend.

A web viewer cannot do Multi-Planar Reconstruction from individual frame
requests alone: it needs the whole stack's *geometry* (so it can place cutting
planes in patient space) and its *raw voxel data* (so it can reslice).  This
service provides both:

  * ``volume_info`` — rows/cols/slices, pixel + slice spacing, orientation
    label, per-slice z-order, rescale LUT, window, dtype — everything required
    to rebuild the volume client-side.
  * ``iter_volume_bytes`` — the contiguous raw voxel block (little-endian),
    optionally rescaled to Hounsfield Units (float32), or as a numpy ``.npy``
    stream for tooling.

Slices are ordered by ``ImagePositionPatient`` along the dominant axis
(z for axial, x for sagittal, y for coronal); series without position info
fall back to InstanceNumber order.  Decoding is streaming (one slice at a
time) so a 512-slice stack never has to be materialized server-side.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from collections import OrderedDict
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cache_stats
from app.core.config import settings
from app.db.models import Instance, Series, Study
from app.services import query_service, storage_service

log = logging.getLogger("paxs.volume")

# SOP classes that carry no image voxels — a PR/KO/SR series must never be
# streamed as a volume.  Classic image IODs (CT/MR/CR/DX/US/XA/NM/PET/OT)
# all live outside this set.
NON_IMAGE_SOP_CLASSES: frozenset[str] = frozenset({
    "1.2.840.10008.5.1.4.1.1.11.1",   # Grayscale Softcopy Presentation State
    "1.2.840.10008.5.1.4.1.1.11.2",   # Color Softcopy Presentation State
    "1.2.840.10008.5.1.4.1.1.11.3",   # Pseudo-Color Softcopy Presentation State
    "1.2.840.10008.5.1.4.1.1.11.4",   # Blending Softcopy Presentation State
    "1.2.840.10008.5.1.4.1.1.88.11",  # Basic Text SR
    "1.2.840.10008.5.1.4.1.1.88.22",  # Key Object Selection Document
    "1.2.840.10008.5.1.4.1.1.88.33",  # Comprehensive SR
    "1.2.840.10008.5.1.4.1.1.88.67",  # Radiation Dose SR
    "1.2.840.10008.5.1.4.1.1.88.69",  # Radiopharmaceutical Radiation Dose SR
    "1.2.840.10008.5.1.4.1.1.481.1",  # RT Image (planning scan, not patient data)
    "1.2.840.10008.5.1.4.1.1.481.2",  # RT Dose
    "1.2.840.10008.5.1.4.1.1.481.3",  # RT Structure Set
    "1.2.840.10008.5.1.4.1.1.481.5",  # RT Plan
})

# PixelSpacing = [row spacing, column spacing] per DICOM.
_ROW_SPACING, _COL_SPACING = 0, 1


def dtype_for(bits_allocated: int, pixel_representation: int) -> str:
    """NumPy dtype string for a DICOM bit depth + signedness."""
    if bits_allocated <= 8:
        return "int8" if pixel_representation else "uint8"
    if bits_allocated <= 16:
        return "int16" if pixel_representation else "uint16"
    return "int32" if pixel_representation else "uint32"


def orientation_label(iop: list[float] | None) -> str:
    """AXIAL / SAGITTAL / CORONAL / OBLIQUE from ImageOrientationPatient.

    ImageOrientationPatient is the 6-tuple (row direction cosines, column
    direction cosines).  The plane normal is their cross product: a normal
    (almost) parallel to patient z is axial, to x is sagittal, to y is
    coronal; anything else is oblique.
    """
    if not iop or len(iop) < 6:
        return "UNKNOWN"
    row_x, row_y, row_z, col_x, col_y, col_z = (float(v) for v in iop[:6])
    nx = row_y * col_z - row_z * col_y
    ny = row_z * col_x - row_x * col_z
    nz = row_x * col_y - row_y * col_x
    x, y, z = abs(nx), abs(ny), abs(nz)
    axis = max(x, y, z)
    if axis < 0.5:
        return "OBLIQUE"
    if z == axis:
        return "AXIAL"
    if x == axis:
        return "SAGITTAL"
    return "CORONAL"


def dominant_axis(iop: list[float] | None) -> int:
    """Index (0=x, 1=y, 2=z) of the patient axis along which slices stack."""
    return {"AXIAL": 2, "SAGITTAL": 0, "CORONAL": 1,
            "OBLIQUE": 2, "UNKNOWN": 2}[orientation_label(iop)]


def strided_dim(n: int, step: int) -> int:
    """Samples a strided read (``x[::step]``) yields for ``n`` items.

    This mirrors Python/numpy slicing exactly — ``len(range(0, n, step))`` is
    ``ceil(n / step)`` — and is the single source of truth for the downsampled
    geometry of ``/api/v1/volume/{series}/data``: the declared
    ``X-Volume-Rows``/``X-Volume-Cols``, the ``.npy`` header shape and the
    ``Content-Length`` must all agree with ``sampled_array.shape``.

    Mixing this with floor division made odd dimensions declare 2x3 while the
    sampler produced 3x4 (see ``plan_9_2026.md`` H13).  ``step < 1`` is clamped
    to 1 (an un-downsampled read).
    """
    n = int(n)
    step = max(1, int(step or 1))
    if n <= 0:
        return 0
    return -(-n // step)  # ceil without float rounding


def npy_header(dtype: str, shape: tuple[int, ...]) -> bytes:
    """NumPy 1.0 file-format header for ``shape`` of ``dtype`` (streaming-safe).

    Uses numpy's own header serializer: the dict keys must be emitted as
    Python literals (``False``/``True``, not JSON ``false``/``true``) because
    numpy >= 2.0 parses headers with ``ast.literal_eval``.
    """
    buf = io.BytesIO()
    header = {
        "descr": np.dtype(dtype).str,
        "fortran_order": False,
        "shape": tuple(int(s) for s in shape),
    }
    np.lib.format.write_array_header_1_0(buf, header)
    return buf.getvalue()


@dataclass
class VolumeSlice:
    """One slice in a sorted volume stack.

    A slice is normally one instance; for a multi-frame / Enhanced instance it
    is one *frame* of that instance (``frame`` is the 0-based frame index and
    ``sop_uid`` carries a ``#<frame>`` suffix so every slice stays unique).
    """

    sop_uid: str
    file_path: str
    backend: str | None
    position: list[float] | None   # ImagePositionPatient (patient coords, mm)
    iop: list[float] | None        # ImageOrientationPatient
    z: float                       # dominant-axis coordinate (or +inf when absent)
    instance_number: int | None
    frame: int | None = None       # frame index inside a multi-frame instance

    @property
    def has_position(self) -> bool:
        return self.position is not None


async def series_slices(db: AsyncSession, series_uid: str, *,
                        tenant_id: int | None = None,
                        include_all: bool = False) -> list[VolumeSlice]:
    """All readable image instances of a series, sorted into stack order.

    Ordering: by ImagePositionPatient along the dominant axis when positions
    are present (stable for ties), else by InstanceNumber / row id.
    """
    stmt = (
        query_service.instance_query(
            None, series_uid, tenant_id=tenant_id, include_all=include_all
        )
        .order_by(
            Instance.instance_number.is_not(None).desc(),
            Instance.instance_number,
            Instance.id,
        )
    )
    rows = (await db.execute(stmt)).scalars().all()

    loaded: list[VolumeSlice] = []
    for inst in rows:
        if not inst.file_path or not storage_service.exists(
            inst.file_path, backend_name=inst.storage_backend
        ):
            continue
        if str(inst.sop_class_uid or "") in NON_IMAGE_SOP_CLASSES:
            continue
        try:
            # Per-instance header read (disk I/O + pydicom parse) — offload so a
            # big series' first bundle build never blocks the event loop.
            ds = await asyncio.to_thread(
                storage_service.read_dataset,
                inst.file_path,
                stop_before_pixels=True,
                backend_name=inst.storage_backend,
            )
        except Exception:
            log.debug("volume header read failed for %s", inst.file_path, exc_info=True)
            continue
        pos = getattr(ds, "ImagePositionPatient", None)
        position = [float(v) for v in pos] if pos else None
        iop_vals = _instance_orientation(ds)

        nf = int(getattr(ds, "NumberOfFrames", 1) or 1)
        if nf > 1:
            # Multi-frame / Enhanced instance → expand into per-frame slices
            # so the volume is a proper z-ordered stack. Per-frame positions
            # come from PerFrameFunctionalGroupsSequence; without them we fall
            # back to the base position offset along the plane normal. Enhanced
            # CT instances that omit PlanePositionSequence still carry a
            # Position dimension (DimensionIndexSequence → per-frame
            # DimensionIndexValues in FrameContentSequence) whose step values
            # define the true stack order even when file order is scrambled.
            per_frame_pos = _per_frame_positions(ds, nf)
            dim_steps = None
            if all(p is None for p in per_frame_pos):
                dim_steps = _dimension_step_order(ds, nf)
            base_iop = iop_vals or [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
            normal = _normal_of(base_iop)
            thickness = _float(getattr(ds, "SliceThickness", None), 1.0) or 1.0
            for f in range(nf):
                fp = per_frame_pos[f]
                step = dim_steps[f] if dim_steps is not None else f
                if fp is None and position is not None:
                    fp = [
                        position[0] + normal[0] * step * thickness,
                        position[1] + normal[1] * step * thickness,
                        position[2] + normal[2] * step * thickness,
                    ]
                elif fp is None and dim_steps is not None:
                    # No base position either — synthesize a stack-order
                    # position from the dimension steps alone (relative
                    # ordering and spacing still correct).
                    fp = [
                        normal[0] * step * thickness,
                        normal[1] * step * thickness,
                        normal[2] * step * thickness,
                    ]
                loaded.append(VolumeSlice(
                    sop_uid=f"{inst.sop_instance_uid}#{f}",
                    file_path=inst.file_path,
                    backend=inst.storage_backend,
                    position=fp,
                    iop=iop_vals,
                    z=math.inf,
                    instance_number=inst.instance_number,
                    frame=f,
                ))
            continue

        loaded.append(VolumeSlice(
            sop_uid=inst.sop_instance_uid,
            file_path=inst.file_path,
            backend=inst.storage_backend,
            position=position,
            iop=iop_vals,
            z=math.inf,
            instance_number=inst.instance_number,
        ))

    if not loaded:
        return []

    # Default to patient z (axial) when no instance carries an orientation
    # (e.g. legacy series) — order by ImagePositionPatient[2] in that case.
    axis = 2
    for s in loaded:
        if s.iop:
            axis = dominant_axis(s.iop)
            break
    for s in loaded:
        if s.position is not None:
            s.z = float(s.position[axis])

    # Stable sort: positioned slices by z; unpositioned slices keep their
    # (InstanceNumber) relative order and sink to the end.
    loaded.sort(key=lambda s: (s.z == math.inf, s.z))
    return loaded


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _instance_orientation(ds) -> list[float] | None:
    """ImageOrientationPatient, resolving Enhanced-class sequences when the
    top-level attribute is absent (Shared/PerFrameFunctionalGroups)."""
    top = getattr(ds, "ImageOrientationPatient", None)
    if top is not None:
        return [float(v) for v in top]
    for seq_attr in ("SharedFunctionalGroupsSequence", "PerFrameFunctionalGroupsSequence"):
        seq = getattr(ds, seq_attr, None)
        if seq and len(seq):
            plane = getattr(seq[0], "PlaneOrientationSequence", None)
            if plane and len(plane):
                try:
                    return [float(v) for v in plane[0].ImageOrientationPatient]
                except (AttributeError, TypeError, ValueError):
                    pass
    return None


def _normal_of(iop: list[float] | None) -> list[float]:
    """Unit plane normal from ImageOrientationPatient (cross of row/col)."""
    if not iop or len(iop) < 6:
        return [0.0, 0.0, 1.0]
    rx, ry, rz = (float(v) for v in iop[:3])
    cx, cy, cz = (float(v) for v in iop[3:6])
    nx, ny, nz = ry * cz - rz * cy, rz * cx - rx * cz, rx * cy - ry * cx
    norm = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return [nx / norm, ny / norm, nz / norm]


def _per_frame_positions(ds, nf: int) -> list[list[float] | None]:
    """Per-frame ImagePositionPatient from PerFrameFunctionalGroupsSequence.

    Returns a list of ``nf`` positions (None when a frame carries none) for a
    multi-frame instance. Falls back to all-None when the sequence is absent.
    """
    pffg = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
    if pffg is None or len(pffg) < nf:
        return [None] * nf
    out: list[list[float] | None] = []
    for item in pffg[:nf]:
        pos = None
        plane = getattr(item, "PlanePositionSequence", None)
        if plane and len(plane):
            try:
                pos = [float(v) for v in plane[0].ImagePositionPatient]
            except (AttributeError, TypeError, ValueError):
                pos = None
        out.append(pos)
    return out


# Dimension pointers that define the "Position" dimension of an Enhanced
# instance: ImagePositionPatient (0020,0032) or, for stacked acquisitions,
# InStackPositionNumber (0020,9057).
_POSITION_DIMENSION_POINTERS = ((0x0020, 0x0032), (0x0020, 0x9057))


def _dimension_step_order(ds, nf: int) -> list[int] | None:
    """Per-frame stack-order steps from DimensionIndexValues (0020,9157).

    Enhanced CT instances that omit PlanePositionSequence still declare a
    Position dimension: DimensionIndexSequence (0020,9222) lists dimensions
    with their DimensionIndexPointer, and each frame's FrameContentSequence
    (0020,9111) carries one DimensionIndexValues entry per dimension. Returns
    the per-frame step values for the Position dimension in file order, or
    None when the dimension cannot be resolved (caller keeps the raw frame
    index fallback).
    """
    dim_seq = getattr(ds, "DimensionIndexSequence", None)
    if not dim_seq or not len(dim_seq):
        return None
    position_idx = None
    for i, dim in enumerate(dim_seq):
        ptr = getattr(dim, "DimensionIndexPointer", None)
        # pydicom returns a BaseTag for AT elements — it compares equal to a
        # (group, element) tuple but is not itself iterable.
        if ptr is not None and ptr == _POSITION_DIMENSION_POINTERS[0]:
            position_idx = i
            break
    if position_idx is None:
        return None
    pffg = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
    if pffg is None or len(pffg) < nf:
        return None
    steps: list[int] = []
    for item in pffg[:nf]:
        fcs = getattr(item, "FrameContentSequence", None)
        dims = None
        if fcs and len(fcs):
            dims = getattr(fcs[0], "DimensionIndexValues", None)
        if dims is None:
            return None
        # Single-valued UL round-trips as a plain int, multi-valued as a list.
        try:
            value = dims if isinstance(dims, int) else dims[position_idx]
            steps.append(int(value))
        except (TypeError, ValueError, IndexError):
            return None
    return steps


def _window_value(ds, attr: str) -> float | None:
    value = getattr(ds, attr, None)
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return _float(value, math.nan) if value is not None else None


def _slice_spacing(slices: list[VolumeSlice], axis: int,
                   fallback: float | None) -> float | None:
    """Mean distance between consecutive positioned slices along ``axis``."""
    positions = [s.position[axis] for s in slices if s.position is not None]
    if len(positions) >= 2:
        deltas = [abs(b - a) for a, b in zip(positions, positions[1:]) if b != a]
        if deltas:
            return float(sum(deltas) / len(deltas))
    return fallback


# In-memory LRU cache for volume_bundle metadata so /info and /data endpoints
# don't re-scan and re-parse disk headers on every client request.
_BUNDLE_CACHE: dict[str, tuple[dict, list[VolumeSlice]]] = {}
_BUNDLE_CACHE_MAX = 32


async def volume_bundle(db: AsyncSession, series_uid: str, *,
                        tenant_id: int | None = None,
                        include_all: bool = False) -> tuple[dict, list[VolumeSlice]] | None:
    """``(info_dict, sorted_slices)`` for a series, or None when it has no
    readable volumetric instances."""
    if series_uid in _BUNDLE_CACHE:
        return _BUNDLE_CACHE[series_uid]

    slices = await series_slices(
        db, series_uid, tenant_id=tenant_id, include_all=include_all
    )
    if not slices:
        return None

    first = slices[0]
    ds = storage_service.read_dataset(
        first.file_path, stop_before_pixels=True, backend_name=first.backend
    )
    series_row = (
        await db.execute(select(Series, Study.study_instance_uid)
                         .join(Study, Series.study_id == Study.id)
                         .where(Series.series_instance_uid == series_uid))
    ).one_or_none()

    rows = int(getattr(ds, "Rows", 0) or 0)
    cols = int(getattr(ds, "Columns", 0) or 0)
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    bits_allocated = int(getattr(ds, "BitsAllocated", 16) or 16)
    bits_stored = int(getattr(ds, "BitsStored", bits_allocated) or bits_allocated)
    pixel_rep = int(getattr(ds, "PixelRepresentation", 0) or 0)
    dtype = dtype_for(bits_allocated, pixel_rep)
    itemsize = np.dtype(dtype).itemsize

    pixel_spacing = getattr(ds, "PixelSpacing", None)
    spacing = [float(v) for v in pixel_spacing] if pixel_spacing else None
    thickness = _float(getattr(ds, "SliceThickness", None), math.nan)
    thickness = None if math.isnan(thickness) else thickness

    iop = _instance_orientation(ds)
    axis = dominant_axis(iop)
    spacing_along_axis = _slice_spacing(slices, axis, thickness)

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

    info: dict = {
        "series_instance_uid": series_uid,
        "study_instance_uid": series_row[1] if series_row else None,
        "modality": getattr(series_row[0], "modality", None) if series_row else None,
        "series_number": getattr(series_row[0], "series_number", None) if series_row else None,
        "series_description": getattr(series_row[0], "series_description", None) if series_row else None,
        "rows": rows,
        "columns": cols,
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
        "image_position_patient": slices[0].position,
        "image_orientation_patient": iop,
        "orientation": orientation_label(iop),
        "isotropic": isotropic,
        "rescale_slope": _float(getattr(ds, "RescaleSlope", 1), 1.0),
        "rescale_intercept": _float(getattr(ds, "RescaleIntercept", 0), 0.0),
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
    if len(_BUNDLE_CACHE) >= _BUNDLE_CACHE_MAX:
        _BUNDLE_CACHE.pop(next(iter(_BUNDLE_CACHE)))
    _BUNDLE_CACHE[series_uid] = (info, slices)
    return info, slices


async def volume_info(db: AsyncSession, series_uid: str, *,
                      tenant_id: int | None = None,
                      include_all: bool = False) -> dict | None:
    bundle = await volume_bundle(
        db, series_uid, tenant_id=tenant_id, include_all=include_all
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
    cache: dict = {}

    for s in selected_slices:
        try:
            arr = _frame_array(s, cache)
        except Exception as exc:  # noqa: BLE001 - surface any decode failure
            from app.core.dicom_diagnostics import decode_context

            # `_frame_array` caches the Dataset it read, so on a decode failure
            # the dataset is usually still available for context. When it is not
            # (the read itself failed) fall back to the slice identity.
            ds = cache.get(s.file_path) if cache else None
            log.error(
                "cannot decode slice %s (%s: %s) | %s",
                s.sop_uid, type(exc).__name__, exc,
                decode_context(ds, frame=s.frame) if ds is not None
                else f"sop={s.sop_uid} frame={s.frame} dataset_unreadable",
            )
            raise ValueError(f"cannot decode slice {s.sop_uid}: {exc}") from exc
        if tuple(arr.shape) != expected_shape:
            raise ValueError(
                f"slice {s.sop_uid} shape {tuple(arr.shape)} diverges from "
                f"stack shape {expected_shape} — not a uniform volume"
            )
        if step > 1:
            arr = arr[::step, ::step]
        if rescale:
            ds = cache.get(s.file_path) or storage_service.read_dataset(
                s.file_path, backend_name=s.backend
            )
            slope = _float(getattr(ds, "RescaleSlope", 1), 1.0)
            intercept = _float(getattr(ds, "RescaleIntercept", 0), 0.0)
            arr = arr.astype(np.float32) * np.float32(slope) + np.float32(intercept)
        yield np.ascontiguousarray(arr).tobytes()


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


# ---------------------------------------------------------------------------
# Server-side MPR slice reslicing (thin-client fallback for WebGL MPR)
# ---------------------------------------------------------------------------
#
# GET /api/v1/volume/{series}/mpr/slice reslices the volume on the fly and
# returns ONE rendered slice — PNG (8-bit W/L) or raw float32 Hounsfield
# values. This is the thin-client fallback for browsers that cannot run the
# WebGL2 reslicing module: the server does the geometry.
#
# Plane definitions — the CANONICAL orientation convention shared with the
# WebGL viewer (app/static/viewer/paxs-mpr.js + paxs-mpr-geometry.js):
#   * native planes  — plane=axial|sagittal|coronal & index=N
#       axial    (const k)  data[j, i] = vol[k, j, i]   rows=ny cols=nx
#                           u = i (X) horizontal, v = j (Y) vertical
#       sagittal (const i)  data[k, j] = vol[k, j, i]   rows=nz cols=ny
#                           u = j (Y) horizontal, v = k (Z) vertical
#       coronal  (const j)  data[k, i] = vol[k, j, i]   rows=nz cols=nx
#                           u = i (X) horizontal, v = k (Z) vertical
#     (Earlier revisions of this comment wrote the sagittal/coronal indices in
#      the transposed order, contradicting the implementation below. The
#      ResliceResult.spacing of each plane — [vertical, horizontal] mm — is the
#      machine-readable statement of the same convention.)
#   * oblique       — origin=x,y,z & row=dx,dy,dz & col=dx,dy,dz (patient
#       coordinates, mm). row/col are unit direction cosines; the plane is
#       sampled on a grid centred on `origin` whose per-pixel spacing comes
#       from the volume's PixelSpacing (row spacing along `row`, col spacing
#       along `col`), with `size` samples.
#
# Sampling uses the patient→voxel transform built from ImageOrientationPatient
# + ImagePositionPatient + spacing, so oblique planes and non-axial source
# volumes are handled uniformly with trilinear interpolation (native planes
# on native volumes land exactly on voxel centres and are lossless).


# Fast LRU memory cache for materialised volume arrays so repeated MPR reslicing
# and server-side slice extraction do not re-read and re-decode all DICOM instances from disk.
# Accessed from the worker thread pool (MPR/MIP run via asyncio.to_thread), so
# mutations are guarded by a lock — a racing eviction must never corrupt the dict.
# L1 volume cache. An OrderedDict so eviction is LEAST-RECENTLY-USED rather than
# insertion-ordered (V3-6): the old `dict` + `next(iter(...))` evicted the oldest
# *inserted* volume, so a series in constant use could be dropped ahead of one
# touched once long ago. `_TILE_CACHE` below already works this way.
#
# Bounded by BYTES (settings.VOLUME_CACHE_BUDGET_MB), not entry count. The old
# 8-entry cap was smaller than HYBRID_MAX_SERVER_MPR_SESSIONS (20), so the cache
# was guaranteed to thrash under exactly the load it was built for.
_VOL_CACHE: "OrderedDict[str, np.ndarray]" = OrderedDict()
_VOL_CACHE_LOCK = threading.Lock()


def volume_cache_budget_bytes() -> int:
    """Current budget from settings (re-read each call so an override applies live).

    ``0`` means **caching disabled** and is returned as 0, not masked with a
    default: an ``or`` fallback would silently turn a deliberate disable into the
    full default budget.
    """
    raw = getattr(settings, "VOLUME_CACHE_BUDGET_MB", 1024)
    try:
        mb = int(raw)
    except (TypeError, ValueError):
        mb = 1024
    return max(0, mb) * 1024 * 1024


def volume_cache_stats() -> dict[str, int]:
    """Counters + gauges for the L1 volume cache (used by tests and /metrics)."""
    return cache_stats.snapshot()["volume"]


def _vol_cache_put_locked(cache_key: str, vol: np.ndarray) -> None:
    """Insert into L1 with byte accounting + LRU eviction. Caller holds the lock.

    The total is recomputed from the dict rather than tracked as a running
    delta: ``_VOL_CACHE.clear()`` is called directly by tests and by cache
    resets, and a stale counter would then evict everything or nothing.
    """
    budget = volume_cache_budget_bytes()
    if budget <= 0:
        # Caching disabled (VOLUME_CACHE_BUDGET_MB=0): store nothing and report
        # an empty cache rather than retaining one oversized entry.
        cache_stats.set_gauges("volume", bytes=0, entries=0)
        return

    _VOL_CACHE.pop(cache_key, None)
    _VOL_CACHE[cache_key] = vol
    cache_stats.incr("volume", "inserts")

    total = sum(int(v.nbytes) for v in _VOL_CACHE.values())
    # Never evict the entry just inserted: one volume larger than the whole
    # budget would otherwise be dropped immediately, so the cache would never
    # hold anything and every request would re-decode.
    while len(_VOL_CACHE) > 1 and total > budget:
        _evicted_key, evicted = _VOL_CACHE.popitem(last=False)
        total -= int(evicted.nbytes)
        cache_stats.incr("volume", "evictions")
    cache_stats.set_gauges("volume", bytes=total, entries=len(_VOL_CACHE))

# --------------------------------------------------------------------------- #
# V3-1 — in-flight volume materialisation registry.
#
# The L1/L2 lookup in load_volume_array is cheap and locked, but the decode loop
# beneath it is neither: it re-reads and re-decodes every slice from disk and
# stacks the result into a fresh float32 array (~500 MB for a 512x512x500 CT).
# Without a registry, N concurrent /mpr/slice or /tiles requests for one
# uncached series all miss the cache and all pay that cost in parallel.
#
# Callers run load_volume_array via asyncio.to_thread, i.e. on a worker thread,
# so the synchronisation primitive must be a threading.Event (not asyncio.Event,
# which is not thread-safe and cannot be waited on from the pool).
# --------------------------------------------------------------------------- #
_IN_FLIGHT_VOLUMES: dict[str, threading.Event] = {}
_IN_FLIGHT_VOLUMES_LOCK = threading.Lock()

# How long a waiter will block for the owner before giving up and decoding the
# volume itself. Generous (a large series over slow storage is genuinely slow)
# but finite, so a crashed owner can never wedge every later request.
_IN_FLIGHT_WAIT_SECONDS = 180.0

# How many times one caller will wait for somebody else's materialisation before
# decoding the volume itself. Bounds the loop when an owner repeatedly fails, so
# a pathological series cannot spin its callers forever.
_IN_FLIGHT_MAX_WAITS = 3

# Rendered-tile caches (plan_viewer_process.md §5.4, server mode).
#
# A server-mode viewer requests one PNG per slice per window, so the same plane
# is rasterized repeatedly as the user scrolls back and forth.  L1 keeps whole
# PNGs keyed by series+plane+index+window (``tile_cache_key``); L2 is the Redis
# tier shared across nodes.  The LRU holds rendered tiles only — it is not a
# second volume cache, and it is bounded independently of ``_VOL_CACHE`` so a
# long server-mode session cannot evict the volumes other sessions depend on.
_TILE_CACHE: "OrderedDict[str, RenderedPlane]" = OrderedDict()
_TILE_CACHE_MAX = 256
_TILE_REDIS_TTL = 600          # matches the plan's rendered-image tier
_TILE_REDIS_MAX_BYTES = 4 * 1024 * 1024
_TILE_INFLIGHT_WAIT_SECONDS = 20.0

# Redis payload header: 3 x int32 (nz, ny, nx) then float32 voxels.
_VOL_REDIS_TTL = 3600

# ---------------------------------------------------------------------------
# Server-side MPR session tracking (plan_viewer_process.md §5.3)
#
# Lightweight bookkeeping so the hybrid negotiation endpoint can gauge server
# load.  Entries auto-expire after _SESSION_TIMEOUT seconds of inactivity.
# Thread-safe: mutations are guarded by the same _VOL_CACHE_LOCK.
# ---------------------------------------------------------------------------
_ACTIVE_SERVER_SESSIONS: dict[str, float] = {}
_SESSION_TIMEOUT = 300  # seconds

# Server-mode tile stampede guard.  Volume materialization is already cached by
# load_volume_array (L1 + L2), but concurrent requests for the *same* slice
# would still each materialize the full volume and rasterize the same PNG.
# Request coalescing here means N concurrent viewers of one slice pay for one
# render.  Bounded by _TILE_INFLIGHT_MAX so a hostile fan-out cannot pin
# unbounded heap (entries are keyed by series+plane+slice, not by caller).
_TILE_INFLIGHT: dict[tuple, "_TileInFlight"] = {}
_TILE_INFLIGHT_LOCK = threading.Lock()
_TILE_INFLIGHT_MAX = 64


class _TileInFlight:
    """One in-progress tile render shared by every waiter for the same key.

    Duplicated rather than imported (``ThreadPoolExecutor`` subclasses ``Future``
    only when concurrent.futures provides it) — this carries the PNG payload and
    the outcome timestamp the *waiters* need, which a bare Future does not.
    """

    __slots__ = ("event", "png", "error", "done_at")

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

    def wait(self, timeout: float) -> "bytes | None":
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
            oldest = min(_TILE_INFLIGHT, key=lambda k: _TILE_INFLIGHT[k].done_at)
            _TILE_INFLIGHT.pop(oldest, None)
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
) -> str:
    """Stable identity of a rendered slice tile.

    Window values are rounded to one decimal so a slider drag does not mint a
    new key on every mouse-move sample.  ``auto`` keeps an un-windowed render
    (the series window resolved by :func:`window_for`) distinct from an
    explicitly requested window that happens to equal it.
    """
    if window_center is None or window_width is None:
        window = "auto"
    else:
        window = f"{round(float(window_center), 1)}:{round(float(window_width), 1)}"
    return f"paxs:tile:{series_uid}:{plane}:{int(index)}:{window}"


def render_plane_tile(
    slices: list[VolumeSlice],
    info: dict,
    plane: str,
    index: int,
    window_center: float | None = None,
    window_width: float | None = None,
) -> "RenderedPlane":
    """Reslice ONE native plane and rasterize it to PNG (L1 + L2 cached).

    Returns the PNG bytes together with the *resolved* window and the reslice
    geometry, so a caller never has to recompute ``window_for`` (an autoscale
    over the whole plane) just to report the window it displayed.

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
    key = tile_cache_key(series_uid, plane, index, window_center, window_width)

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
        # Owner failed or is slow: fall through and render independently rather
        # than propagating someone else's failure to this caller.

    try:
        result = reslice_plane(slices, info, plane, index=index)
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
) -> "RenderedPlane":
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


def _tile_cache_put(key: str, rendered: "RenderedPlane") -> None:
    with _VOL_CACHE_LOCK:
        _TILE_CACHE[key] = rendered
        _TILE_CACHE.move_to_end(key)
        while len(_TILE_CACHE) > _TILE_CACHE_MAX:
            _TILE_CACHE.popitem(last=False)


def _redis_get_bytes(key: str) -> bytes | None:
    """Best-effort binary GET on the shared pooled Redis client."""
    from app.services.redis_service import get_redis_client

    client = get_redis_client()
    if client is None:
        return None
    try:
        return client.get(key)
    except Exception:
        return None


def _redis_set_bytes(key: str, payload: bytes, ttl: int) -> None:
    """Best-effort binary SET on the shared pooled Redis client."""
    from app.services.redis_service import get_redis_client

    client = get_redis_client()
    if client is None:
        return
    try:
        client.set(key, payload, ex=ttl)
    except Exception:
        pass


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
    slice_sp = float(info.get("slice_spacing") or info.get("slice_thickness") or 1.0)
    plane = str(plane).lower()
    if plane == "axial":
        return [row_sp, col_sp]
    if plane == "sagittal":
        return [slice_sp, row_sp]
    if plane == "coronal":
        return [slice_sp, col_sp]
    raise ValueError("plane must be 'axial', 'sagittal' or 'coronal'")


def _patient_transform_spacing(info: dict) -> tuple[float, float]:
    """``(row_spacing, col_spacing)`` in mm, defaulting to 1.0 like the MPR path."""
    spacing = info.get("pixel_spacing") or [1.0, 1.0]
    row_sp = float(spacing[0]) if len(spacing) > 0 else 1.0
    col_sp = float(spacing[1]) if len(spacing) > 1 else row_sp
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
        ).png
        for index in range(start, end + 1)
    ]


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


def register_server_session(series_uid: str) -> None:
    """Mark *series_uid* as having an active server-side MPR session."""
    with _VOL_CACHE_LOCK:
        _ACTIVE_SERVER_SESSIONS[series_uid] = time.monotonic()
        _evict_expired_sessions_locked()


def release_server_session(series_uid: str) -> None:
    """Remove the session entry for *series_uid* (if present)."""
    with _VOL_CACHE_LOCK:
        _ACTIVE_SERVER_SESSIONS.pop(series_uid, None)


def active_server_session_count() -> int:
    """Return the number of non-expired server-side MPR sessions."""
    with _VOL_CACHE_LOCK:
        _evict_expired_sessions_locked()
        return len(_ACTIVE_SERVER_SESSIONS)


def _evict_expired_sessions_locked() -> None:
    """Remove entries older than ``_SESSION_TIMEOUT``.  Caller must hold lock."""
    now = time.monotonic()
    expired = [k for k, ts in _ACTIVE_SERVER_SESSIONS.items()
               if now - ts > _SESSION_TIMEOUT]
    for k in expired:
        _ACTIVE_SERVER_SESSIONS.pop(k, None)


def estimated_vol_cache_mb() -> int:
    """Estimated total size of the in-process materialised volume cache (MB).

    Public name used by the hybrid engine (§7.1) and the load headers (§7.3).
    """
    with _VOL_CACHE_LOCK:
        return sum(v.nbytes for v in _VOL_CACHE.values()) // (1024 * 1024)


# Spec-name alias (plan_viewer_process.md §5.1 spells it `_estimated_vol_cache_mb`).
# Kept so code written against the plan — and the plan's own snippets — resolve.
_estimated_vol_cache_mb = estimated_vol_cache_mb


def _vol_redis_get(r_key: str) -> np.ndarray | None:
    """Fetch a cached volume from the shared pooled Redis client (best-effort)."""
    from app.services.redis_service import get_redis_client

    client = get_redis_client()  # shared pool, 1.5s socket timeouts
    if client is None:
        return None
    try:
        cached_bytes = client.get(r_key)
        if not cached_bytes or len(cached_bytes) < 12:
            return None
        nz, ny, nx = np.frombuffer(cached_bytes[:12], dtype=np.int32)
        vol = np.frombuffer(cached_bytes[12:], dtype=np.float32).reshape((nz, ny, nx))
        return vol
    except Exception:
        return None


def _vol_redis_set(r_key: str, vol: np.ndarray) -> None:
    """Write a volume to the shared pooled Redis client (best-effort)."""
    from app.services.redis_service import get_redis_client

    client = get_redis_client()
    if client is None:
        return
    try:
        shape_hdr = np.array(vol.shape, dtype=np.int32).tobytes()
        client.set(r_key, shape_hdr + vol.tobytes(), ex=_VOL_REDIS_TTL)
    except Exception:
        pass


def load_volume_array(slices: list[VolumeSlice], series_uid: str | None = None) -> np.ndarray:
    """Materialize the stack as one float32 HU array, shape (nz, ny, nx).

    Supports multi-node Redis binary cache with local LRU fallback. Multi-frame
    / Enhanced instances are expanded into per-frame slices, so each entry here
    is one frame of the source instance. This is CPU + disk heavy (every slice
    is read and decoded); callers run it via ``asyncio.to_thread``.

    Concurrent callers for the *same* series are coalesced (V3-1): the first one
    materialises the volume and the rest block on a ``threading.Event`` and then
    read the finished array out of ``_VOL_CACHE``, instead of each re-reading and
    re-stacking ~500 MB of voxels in parallel.
    """
    if not slices:
        return np.empty((0, 0, 0), dtype=np.float32)
    cache_key = f"array:{series_uid}:{len(slices)}" if series_uid else f"array:{slices[0].sop_uid}:{len(slices)}"
    alt_cache_key = f"array:{slices[0].sop_uid}:{len(slices)}"

    def _cached_any() -> np.ndarray | None:
        with _VOL_CACHE_LOCK:
            # Resolve WHICH key hit before touching recency: move_to_end raises
            # KeyError for an absent key, and the hit may have come via the
            # alternate key.
            key = cache_key if cache_key in _VOL_CACHE else (
                alt_cache_key if alt_cache_key in _VOL_CACHE else None
            )
            if key is None:
                cache_stats.incr("volume", "misses")
                return None
            _VOL_CACHE.move_to_end(key, last=True)   # V3-6: LRU, not insertion order
            cache_stats.incr("volume", "hits")
            return _VOL_CACHE[key]

    # 1. Check local fast LRU cache
    cached = _cached_any()
    if cached is not None:
        return cached

    # 2. Check Redis cluster cache if available (shared pooled client)
    if getattr(settings, "REDIS_CACHE_ENABLED", False):
        vol = _vol_redis_get(f"paxs:vol:{cache_key}")
        if vol is None and alt_cache_key != cache_key:
            vol = _vol_redis_get(f"paxs:vol:{alt_cache_key}")
        if vol is not None:
            with _VOL_CACHE_LOCK:
                _vol_cache_put_locked(cache_key, vol)
            return vol

    # 3. Single-flight (V3-1): claim ownership of this cache_key, or wait for
    #    the thread that already owns it. The loop matters — after a waiter is
    #    woken it must RE-CHECK the cache and, if the owner failed without
    #    caching anything, try to claim the key itself. Exactly one thread
    #    materialises per round; the rest read the finished array out of
    #    _VOL_CACHE. (Unconditionally re-claiming on wake made every waiter an
    #    owner and re-introduced the parallel decode this exists to prevent.)
    owns_event = False
    event: threading.Event | None = None
    waits = 0
    while True:
        with _IN_FLIGHT_VOLUMES_LOCK:
            current = _IN_FLIGHT_VOLUMES.get(cache_key)
            if current is None:
                event = threading.Event()
                _IN_FLIGHT_VOLUMES[cache_key] = event
                owns_event = True
                break
            event = current
        if waits >= _IN_FLIGHT_MAX_WAITS:
            # The owner keeps failing. Stop coalescing and decode it ourselves
            # rather than spinning (and without touching the other owner's event).
            owns_event = False
            break
        waits += 1
        event.wait(timeout=_IN_FLIGHT_WAIT_SECONDS)
        cached = _cached_any()
        if cached is not None:
            return cached

    try:
        first = storage_service.read_dataset(
            slices[0].file_path, backend_name=slices[0].backend
        )
        expected_shape = _expected_shape(first)
        arrays: list[np.ndarray] = []
        cache: dict = {}
        for s in slices:
            arr = _frame_array(s, cache)
            if tuple(arr.shape) != expected_shape:
                raise ValueError(
                    f"slice {s.sop_uid} shape {tuple(arr.shape)} diverges from "
                    f"stack shape {expected_shape} — not a uniform volume"
                )
            ds = cache.get(s.file_path) or storage_service.read_dataset(
                s.file_path, backend_name=s.backend
            )
            slope = _float(getattr(ds, "RescaleSlope", 1), 1.0)
            intercept = _float(getattr(ds, "RescaleIntercept", 0), 0.0)
            arrays.append(arr.astype(np.float32) * np.float32(slope) + np.float32(intercept))

        vol = np.stack(arrays, axis=0)
        with _VOL_CACHE_LOCK:
            _vol_cache_put_locked(cache_key, vol)

        # Populate Redis cluster cache (best-effort, shared pooled client)
        if getattr(settings, "REDIS_CACHE_ENABLED", False):
            _vol_redis_set(f"paxs:vol:{cache_key}", vol)
            if series_uid and alt_cache_key != cache_key:
                _vol_redis_set(f"paxs:vol:{alt_cache_key}", vol)

        return vol
    finally:
        # Always release waiters and drop the claim, success or failure — an
        # exception here must not leave every later caller blocked until the
        # timeout expires. Only the thread that REGISTERED the event may release
        # it, so a non-owner can never prematurely wake another owner's waiters.
        if owns_event and event is not None:
            with _IN_FLIGHT_VOLUMES_LOCK:
                if _IN_FLIGHT_VOLUMES.get(cache_key) is event:
                    _IN_FLIGHT_VOLUMES.pop(cache_key, None)
            event.set()


def _patient_transform(info: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float, float]:
    """Patient-space geometry of the volume stack.

    Returns ``(ipp, Rinv, normal, row_sp, col_sp, slice_sp)`` where the
    forward transform is ``P = ipp + R @ [i, j, k]`` (R columns: col direction
    × col spacing, row direction × row spacing, normal × slice spacing) and
    ``Rinv`` maps patient mm → voxel indices ``[i, j, k]``.
    """
    iop = info.get("image_orientation_patient") or [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    row_vec = np.array([float(v) for v in iop[:3]])
    col_vec = np.array([float(v) for v in iop[3:6]])
    normal = np.cross(row_vec, col_vec)
    ipp = np.array([float(v) for v in info.get("image_position_patient") or [0, 0, 0]])
    spacing = info.get("pixel_spacing") or [1.0, 1.0]
    row_sp = float(spacing[0])
    col_sp = float(spacing[1])
    slice_sp = float(info.get("slice_spacing") or info.get("slice_thickness") or 1.0)
    r = np.column_stack([col_vec * col_sp, row_vec * row_sp, normal * slice_sp])
    return ipp, np.linalg.inv(r), normal, row_sp, col_sp, slice_sp


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
    out = (
        c000 * (1 - fi) * (1 - fj) * (1 - fk) +
        c100 * fi * (1 - fj) * (1 - fk) +
        c010 * (1 - fi) * fj * (1 - fk) +
        c110 * fi * fj * (1 - fk) +
        c001 * (1 - fi) * (1 - fj) * fk +
        c101 * fi * (1 - fj) * fk +
        c011 * (1 - fi) * fj * fk +
        c111 * fi * fj * fk
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
) -> ResliceResult:
    """Reslice one plane of the volume (server-side, trilinear)."""
    nz = int(info["slices"])
    ny = int(info["rows"])
    nx = int(info["columns"])
    vol = load_volume_array(slices, series_uid=info.get("series_instance_uid"))
    ipp, rinv, normal, row_sp, col_sp, slice_sp = _patient_transform(info)
    # unit direction cosines of the source stack (from ImageOrientationPatient)
    iop = info.get("image_orientation_patient") or [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    stack_row = np.array([float(v) for v in iop[:3]])
    stack_col = np.array([float(v) for v in iop[3:6]])

    plane = plane.lower()

    if plane in ("axial", "sagittal", "coronal"):
        if index is None:
            raise ValueError("index is required for plane=%s" % plane)
        index = int(index)
        if plane == "axial":
            if not 0 <= index < nz:
                raise IndexError(f"axial index {index} out of range 0-{nz - 1}")
            data = vol[index].copy()
            return ResliceResult(
                data=data, rows=ny, cols=nx,
                spacing=[row_sp, col_sp], plane="axial", index=index,
                origin=[float(v) for v in (ipp + index * slice_sp * normal)],
                row_vector=[float(v) for v in stack_row],
                col_vector=[float(v) for v in stack_col],
            )
        if plane == "sagittal":
            if not 0 <= index < nx:
                raise IndexError(f"sagittal index {index} out of range 0-{nx - 1}")
            # plane spans rows (j) × slices (k): data[k, j] = vol[k, j, index]
            origin_pt = ipp + index * col_sp * stack_col
            data = _sample_grid(vol, ipp, rinv, origin_pt,
                                stack_row, normal, ny, nz, row_sp, slice_sp)
            return ResliceResult(
                data=data, rows=nz, cols=ny,
                spacing=[slice_sp, row_sp], plane="sagittal", index=index,
                origin=[float(v) for v in origin_pt],
                row_vector=[float(v) for v in stack_row],
                col_vector=[float(v) for v in normal],
            )
        # coronal: spans columns (i) × slices (k): data[k, i] = vol[k, index, i]
        if not 0 <= index < ny:
            raise IndexError(f"coronal index {index} out of range 0-{ny - 1}")
        origin_pt = ipp + index * row_sp * stack_row
        data = _sample_grid(vol, ipp, rinv, origin_pt,
                            stack_col, normal, nx, nz, col_sp, slice_sp)
        return ResliceResult(
            data=data, rows=nz, cols=nx,
            spacing=[slice_sp, col_sp], plane="coronal", index=index,
            origin=[float(v) for v in origin_pt],
            row_vector=[float(v) for v in stack_col],
            col_vector=[float(v) for v in normal],
        )

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
    data = _sample_grid(vol, ipp, rinv, o, c, r, n_u, n_v, col_sp, row_sp,
                        centered=True)
    return ResliceResult(
        data=data, rows=n_v, cols=n_u,
        spacing=[row_sp, col_sp], plane="oblique", index=None,
        origin=[float(v) for v in o],
        row_vector=[float(v) for v in c],
        col_vector=[float(v) for v in r],
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
    if finite.size == 0:
        return 0.0, 1.0
    lo, hi = np.percentile(finite, [2, 98])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo, hi = float(finite.min()), float(finite.max())
        if hi <= lo:
            return 0.0, 1.0
    return (lo + hi) / 2.0, hi - lo


def nan_to_float32(data: np.ndarray) -> np.ndarray:
    """Replace NaN (out-of-volume samples) with 0 and return float32 bytes."""
    return np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def render_plane_png(data: np.ndarray, window_center: float,
                     window_width: float) -> bytes:
    """Render the plane to 8-bit grayscale PNG with the given window."""
    import io as _io
    from PIL import Image
    half = float(window_width) / 2.0
    t = (data - (float(window_center) - half)) / max(float(window_width), 1e-6)
    t = np.nan_to_num(t, nan=0.0, posinf=1.0, neginf=0.0)
    u8 = np.clip(t, 0.0, 1.0) * 255.0
    img = Image.fromarray(u8.astype(np.uint8), mode="L")
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 3D VRT / MIP support (roadmap #27) — maximum-intensity projection and a
# lightweight alpha-compositing volume render, both server-side.
# ---------------------------------------------------------------------------
#
# GET /api/v1/volume/{series}/mip projects the volume along one of the three
# patient axes and returns a single 2D image — the classic MIP slab used in
# angiography, plus minIP / average (X-ray-like) and a basic "VRT" mode that
# ray-casts front-to-back with an opacity ramp derived from the display
# window.  The projection is computed on the same float32 Hounsfield volume
# as the MPR reslicer (``load_volume_array``), so it inherits the per-frame
# expansion of multi-frame / Enhanced instances and the exact same geometry
# (ImageOrientationPatient + ImagePositionPatient + spacing).

# Projection axis → numpy axis of the (nz, ny, nx) volume.
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
    t = (vol - lo) / (2.0 * half)
    t = np.clip(t, 0.0, 1.0)
    opacity = t * t * (3.0 - 2.0 * t)          # smoothstep
    color = np.where(opacity > 0.0, vol, 0.0)

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

    vol = load_volume_array(slices, series_uid=info.get("series_instance_uid"))
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

    if mode == "mip":
        data = np.max(slab, axis=ax)
    elif mode == "minip":
        data = np.min(slab, axis=ax)
    elif mode == "average":
        data = np.mean(slab, axis=ax)
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
