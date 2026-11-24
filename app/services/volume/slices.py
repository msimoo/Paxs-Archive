"""Slice discovery and ordering: which instances form a volume, in what order.

Slices are ordered by projecting ``ImagePositionPatient`` onto the validated
plane normal. Incomplete or inconsistent geometry is rejected; instance order
is never used to invent patient-space coordinates. ``NON_IMAGE_SOP_CLASSES``
keeps a PR/KO/SR series from being streamed as a volume.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import json
from collections.abc import AsyncIterator, Sequence
import logging
import math
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Literal

import numpy as np
from pydicom.dataset import Dataset
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.config import settings
from app.core.concurrency import BlockingCapacityError, run_blocking
from app.db.models import Instance
from app.services import query_service, storage_service
from app.services.volume.geometry_validation import (
    GeometryErrorCode,
    GeometryTolerances,
    GeometryValidationError,
    SeriesGeometry,
    SliceGeometry,
    validate_series_geometry,
)
from app.services.volume.calibration import FrameCalibration, resolve_frame_calibration

log = logging.getLogger("paxs.volume")
VolumeVariant = Literal["auto", "original", "lossy"]

__all__ = [
    "NON_IMAGE_SOP_CLASSES",
    "_COL_SPACING",
    "_POSITION_DIMENSION_POINTERS",
    "_ROW_SPACING",
    "VolumeSlice",
    "_dimension_step_order",
    "_float",
    "_instance_orientation",
    "_normal_of",
    "_per_frame_positions",
    "_slice_spacing",
    "_window_value",
    "dominant_axis",
    "dtype_for",
    "npy_header",
    "orientation_label",
    "series_slices",
    "strided_dim",
    "unique_slices",
]


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


_ROW_SPACING, _COL_SPACING = 0, 1


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
    if iop is None or len(iop) != 6:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_IOP, "ImageOrientationPatient is required"
        )
    return {"AXIAL": 2, "SAGITTAL": 0, "CORONAL": 1,
            "OBLIQUE": 2}[orientation_label(iop)]


def strided_dim(n: int, step: int) -> int:
    """Samples a strided read (``x[::step]``) yields for ``n`` items.

    This mirrors Python/numpy slicing exactly — ``len(range(0, n, step))`` is
    ``ceil(n / step)`` — and is the single source of truth for the downsampled
    geometry of ``/api/v1/volume/{series}/data``: the declared
    ``X-Volume-Rows``/``X-Volume-Cols``, the ``.npy`` header shape and the
    ``Content-Length`` must all agree with ``sampled_array.shape``.

    Mixing this with floor division made odd dimensions declare 2x3 while the
    sampler produced 3x4 (see ``plans/legacy/archive-notes/plan_9_2026.md`` H13).  ``step < 1`` is clamped
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
    file_size_bytes: int | None = None
    decoded_instance_bytes: int | None = None
    geometry: SliceGeometry | None = None
    series_geometry: SeriesGeometry | None = None
    calibration: FrameCalibration | None = None

    @property
    def has_position(self) -> bool:
        return self.position is not None


def unique_slices(slices: list[VolumeSlice]) -> list[VolumeSlice]:
    """Drop duplicate frame identities, preserving order (B-10).

    A slice is identified by ``(sop_uid, frame)`` — for a multi-frame instance
    the UID already carries the ``#<frame>`` suffix, so this is exactly the
    frame identity. A duplicate would be decoded, emitted and counted twice,
    inflating the payload and every byte estimate derived from it (including the
    ``VOLUME_MAX_BYTES`` check). Order is preserved so slice ordering is
    unchanged for every non-duplicated stack.
    """
    seen: set[tuple[str, int | None]] = set()
    out: list[VolumeSlice] = []
    for s in slices:
        key = (s.sop_uid, s.frame)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


async def _read_instance_header(inst: Instance) -> object:
    """Read one header outside the event loop, rejecting missing sources.

    The existence check keeps cached metadata from masking a missing object;
    stored DICOM JSON avoids reparsing an already indexed header from disk.
    """
    try:
        if not inst.file_path:
            raise FileNotFoundError("Volume source object is missing")
        metadata_json = getattr(inst, "metadata_json", None)
        if metadata_json:
            # Geometry discovery uses indexed metadata; pixel endpoints verify
            # objects when they actually read frames. Avoid 5,500 redundant
            # storage stat calls before an overview can be shown.
            return await run_blocking(_header_from_metadata_json, metadata_json)
        if not await run_blocking(
            storage_service.exists, inst.file_path, backend_name=inst.storage_backend
        ):
            raise FileNotFoundError("Volume source object is missing")
        return await run_blocking(
            storage_service.read_dataset, inst.file_path,
            stop_before_pixels=True, backend_name=inst.storage_backend,
        )
    except BlockingCapacityError:
        raise
    except Exception as exc:  # noqa: BLE001 - boundary guard; log type only to keep PHI out
        log.error(
            "Volume header read failed for SOP %s (error_type=%s); refusing partial volume",
            inst.sop_instance_uid,
            type(exc).__name__,
        )
        raise RuntimeError(
            "Cannot assemble a complete volume because a DICOM header is unreadable"
        ) from None


def _header_from_metadata_json(metadata_json: str) -> object:
    """Decode volume geometry without building full pydicom objects per slice.

    Single-frame stacks need only top-level geometry and pixel-layout tags.
    Enhanced multi-frame objects retain pydicom's complete functional-group
    handling. The fast path reduces cold MPR metadata setup overhead.
    """
    tags = json.loads(metadata_json)
    number_of_frames = _metadata_json_value(tags, "00280008", 1)
    if int(number_of_frames or 1) > 1:
        return Dataset.from_json(metadata_json)

    field_tags = {
        "SOPInstanceUID": "00080018",
        "ImageType": "00080008",
        "LossyImageCompression": "00282110",
        "SourceImageSequence": "00082112",
        "ImagePositionPatient": "00200032",
        "ImageOrientationPatient": "00200037",
        "FrameOfReferenceUID": "00200052",
        "SeriesInstanceUID": "0020000E",
        "Rows": "00280010",
        "Columns": "00280011",
        "SamplesPerPixel": "00280002",
        "BitsAllocated": "00280100",
        "PixelRepresentation": "00280103",
        "NumberOfFrames": "00280008",
        "PixelSpacing": "00280030",
        "SliceThickness": "00180050",
        "SpacingBetweenSlices": "00180088",
        "GantryDetectorTilt": "00181120",
        "RescaleSlope": "00281053",
        "RescaleIntercept": "00281052",
        "RescaleType": "00281054",
        "PixelPaddingValue": "00280120",
        "PixelPaddingRangeLimit": "00280121",
    }
    attributes = {name: _metadata_json_value(tags, tag) for name, tag in field_tags.items()}
    return SimpleNamespace(**attributes)


def _metadata_json_value(tags: dict, tag: str, default: object = None) -> object:
    """Read a DICOM JSON element value, returning ``default`` when absent."""
    element = tags.get(tag)
    if not isinstance(element, dict):
        return default
    values = element.get("Value")
    if not isinstance(values, list) or not values:
        return default
    return values[0] if len(values) == 1 else values


def _image_type(header: object) -> str:
    value = getattr(header, "ImageType", None)
    if not isinstance(value, Sequence) or not value:
        return ""
    first = value.split("\\", 1)[0] if isinstance(value, str) else value[0]
    return str(first).strip().upper()


def _source_uid(header: object) -> str | None:
    sequence = getattr(header, "SourceImageSequence", None)
    if isinstance(sequence, dict):
        sequence = [sequence]
    if not isinstance(sequence, Sequence) or len(sequence) != 1:
        return None
    reference = sequence[0]
    value = (
        _metadata_json_value(reference, "00081155")
        if isinstance(reference, dict)
        else getattr(reference, "ReferencedSOPInstanceUID", None)
    )
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _same_slice(source: object, copy: object, tolerances: GeometryTolerances) -> bool:
    for name in ("FrameOfReferenceUID", "SeriesInstanceUID", "Rows", "Columns"):
        value = getattr(source, name, None)
        if not value or value != getattr(copy, name, None):
            return False
    for name, size, tolerance in (
        ("ImagePositionPatient", 3, tolerances.position_tolerance_mm),
        ("ImageOrientationPatient", 6, tolerances.iop_tolerance),
        ("PixelSpacing", 2, tolerances.spacing_abs_tolerance_mm),
    ):
        left = getattr(source, name, None)
        right = getattr(copy, name, None)
        try:
            if len(left) != size or len(right) != size or not all(
                abs(float(a) - float(b)) <= tolerance for a, b in zip(left, right)
            ):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _split_lossy_copies(headers: list, tolerances: GeometryTolerances) -> tuple[list[int], list[int]] | None:
    """Split only when every derived image is a verified copy of a local original."""
    originals: dict[str, int] = {}
    copies: list[int] = []
    for index, header in enumerate(headers):
        if int(getattr(header, "NumberOfFrames", 1) or 1) != 1:
            return None
        lossy = str(getattr(header, "LossyImageCompression", "")).strip() == "01"
        image_type = _image_type(header)
        if lossy and image_type == "DERIVED":
            copies.append(index)
        elif not lossy and image_type == "ORIGINAL":
            uid = str(getattr(header, "SOPInstanceUID", "") or "").strip()
            if not uid or uid in originals:
                return None
            originals[uid] = index
        else:
            return None
    if not originals or not copies:
        return None

    referenced: set[str] = set()
    for index in copies:
        source_uid = _source_uid(headers[index])
        if source_uid is None or source_uid not in originals or source_uid in referenced:
            return None
        if not _same_slice(headers[originals[source_uid]], headers[index], tolerances):
            return None
        referenced.add(source_uid)
    return list(originals.values()), copies


def select_volume_geometry(
    headers: list, tolerances: GeometryTolerances, variant: VolumeVariant = "auto"
) -> SeriesGeometry:
    """Validate the requested stack and retain source indices from the original list."""
    duplicate_error = None
    if variant == "auto":
        try:
            return validate_series_geometry(headers, tolerances=tolerances)
        except GeometryValidationError as exc:
            if exc.code != GeometryErrorCode.DUPLICATE_POSITION:
                raise
            duplicate_error = exc

    groups = _split_lossy_copies(headers, tolerances)
    if groups is None:
        if duplicate_error is not None:
            raise duplicate_error
        raise GeometryValidationError(
            GeometryErrorCode.VARIANT_UNAVAILABLE, "no verified original and lossy copy groups"
        )
    indices = groups[1] if variant == "lossy" else groups[0]
    geometry = validate_series_geometry(
        [headers[index] for index in indices], tolerances=tolerances
    )
    return replace(geometry, slices=tuple(
        replace(frame, source_index=indices[frame.source_index]) for frame in geometry.slices
    ))


async def _iter_volume_headers(
    instances: list[Instance],
) -> AsyncIterator[tuple[Instance, Dataset]]:
    """Yield bounded header batches, using stored DICOM JSON when available."""
    concurrency = settings.VOLUME_HEADER_READ_CONCURRENCY
    loaded: list[tuple[Instance, Dataset]] = []
    for start in range(0, len(instances), concurrency):
        batch = instances[start:start + concurrency]
        results = await asyncio.gather(
            *(_read_instance_header(inst) for inst in batch),
            return_exceptions=True,
        )
        for inst, result in zip(batch, results):
            if isinstance(result, BaseException):
                raise result
            yield inst, result


async def series_slices(db: AsyncSession, series_uid: str, *,
                        tenant_id: int | None = None,
                        include_all: bool = False,
                        variant: VolumeVariant = "auto") -> list[VolumeSlice]:
    """Return only image slices with complete, validated patient geometry.

    Projection along the validated plane normal determines order. Missing or
    inconsistent geometry raises ``GeometryValidationError``; volume callers
    must never infer patient coordinates from instance order or filenames.
    A separately requested lossy stack requires verified source references.
    """
    stmt = (
        query_service.instance_query(
            None, series_uid, tenant_id=tenant_id, include_all=include_all
        )
        .options(undefer(Instance.metadata_json))
        .order_by(
            Instance.instance_number.is_not(None).desc(),
            Instance.instance_number,
            Instance.id,
        )
    )
    rows = (await db.execute(stmt)).scalars().all()

    image_rows = [inst for inst in rows
                  if str(inst.sop_class_uid or "") not in NON_IMAGE_SOP_CLASSES]
    loaded: list[tuple[VolumeSlice, int, int | None]] = []
    geometry_headers: list[Dataset] = []
    async for inst, ds in _iter_volume_headers(image_rows):
        source_index = len(geometry_headers)
        geometry_headers.append(ds)
        nf = int(getattr(ds, "NumberOfFrames", 1) or 1)
        rows = int(getattr(ds, "Rows", 0) or 0)
        cols = int(getattr(ds, "Columns", 0) or 0)
        samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
        bits = int(getattr(ds, "BitsAllocated", 16) or 16)
        representation = int(getattr(ds, "PixelRepresentation", 0) or 0)
        source_itemsize = np.dtype(dtype_for(bits, representation)).itemsize
        decoded_instance_bytes = rows * cols * samples * source_itemsize * nf
        file_size_bytes = getattr(inst, "file_size_bytes", None)
        if nf > 1:
            # Enhanced geometry is resolved by the validator per frame. Do not
            # synthesize positions from frame order or SliceThickness.
            for f in range(nf):
                item = VolumeSlice(
                    sop_uid=f"{inst.sop_instance_uid}#{f}",
                    file_path=inst.file_path,
                    backend=inst.storage_backend,
                    position=None,
                    iop=None,
                    z=math.inf,
                    instance_number=inst.instance_number,
                    frame=f,
                    file_size_bytes=file_size_bytes,
                    decoded_instance_bytes=decoded_instance_bytes,
                    calibration=resolve_frame_calibration(ds, f),
                )
                loaded.append((item, source_index, f))
            continue

        item = VolumeSlice(
            sop_uid=inst.sop_instance_uid,
            file_path=inst.file_path,
            backend=inst.storage_backend,
            position=None,
            iop=None,
            z=math.inf,
            instance_number=inst.instance_number,
            file_size_bytes=file_size_bytes,
            decoded_instance_bytes=decoded_instance_bytes,
            calibration=resolve_frame_calibration(ds, 0),
        )
        loaded.append((item, source_index, None))

    if not loaded:
        return []

    geometry = select_volume_geometry(
        geometry_headers, GeometryTolerances.from_settings(settings), variant
    )
    indexed = {
        (source_index, frame_index): item
        for item, source_index, frame_index in loaded
    }
    ordered: list[VolumeSlice] = []
    for validated in geometry.slices:
        item = indexed[(validated.source_index, validated.frame_index)]
        item.position = list(validated.image_position_patient)
        item.iop = list(validated.image_orientation_patient)
        item.z = validated.projected_position_mm
        item.geometry = validated
        item.series_geometry = geometry
        ordered.append(item)
    return ordered


def _float(value, default: float) -> float:
    """Parse optional numeric DICOM metadata, falling back when it is malformed.

    Safe sentinel: callers supply a conservative geometry/display default for
    optional tags; a malformed value cannot be treated as a decoded voxel.
    """
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        log.debug("Invalid optional numeric metadata; using fallback (error_type=%s)",
                  type(exc).__name__)
        return default


def _instance_orientation(ds) -> list[float] | None:
    """Resolve ImageOrientationPatient from top-level or Enhanced sequences.

    Returns None when no usable orientation is present. Geometry consumers
    pass that value to the validator and fail closed.
    """
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
                except (AttributeError, TypeError, ValueError) as exc:
                    log.debug(
                        "Invalid %s ImageOrientationPatient metadata; trying fallback sequence (error_type=%s)",
                        seq_attr,
                        type(exc).__name__,
                    )
    return None


def _normal_of(iop: list[float] | None) -> list[float]:
    """Unit plane normal from ImageOrientationPatient (cross of row/col)."""
    if not iop or len(iop) < 6:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_IOP, "ImageOrientationPatient is required"
        )
    rx, ry, rz = (float(v) for v in iop[:3])
    cx, cy, cz = (float(v) for v in iop[3:6])
    nx, ny, nz = ry * cz - rz * cy, rz * cx - rx * cz, rx * cy - ry * cx
    norm = math.sqrt(nx * nx + ny * ny + nz * nz)
    if not math.isfinite(norm) or norm == 0:
        raise GeometryValidationError(
            GeometryErrorCode.BAD_IOP, "ImageOrientationPatient has no valid normal"
        )
    return [nx / norm, ny / norm, nz / norm]


def _per_frame_positions(ds, nf: int) -> list[list[float] | None]:
    """Read per-frame IPP values without synthesizing absent coordinates."""
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
            except (AttributeError, TypeError, ValueError) as exc:
                log.debug(
                    "Invalid per-frame ImagePositionPatient metadata (error_type=%s)",
                    type(exc).__name__,
                )
                pos = None
        out.append(pos)
    return out


_POSITION_DIMENSION_POINTERS = ((0x0020, 0x0032), (0x0020, 0x9057))


def _dimension_step_order(ds, nf: int) -> list[int] | None:
    """Read Enhanced per-frame stack steps from DimensionIndexValues.

    Returns the per-frame Position-dimension steps, or ``None`` when metadata
    is absent/malformed. ``None`` is a safe sentinel because the caller then
    uses the source frame index as relative stack order rather than dropping
    any frame.
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
        except (TypeError, ValueError, IndexError) as exc:
            log.debug(
                "Invalid Enhanced DimensionIndexValues; using source frame order (error_type=%s)",
                type(exc).__name__,
            )
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
                   ) -> float | None:
    """Mean distance between consecutive positioned slices along ``axis``."""
    positions = [s.position[axis] for s in slices if s.position is not None]
    if len(positions) >= 2:
        deltas = [abs(b - a) for a, b in itertools.pairwise(positions) if b != a]
        if deltas:
            return float(sum(deltas) / len(deltas))
    return None
