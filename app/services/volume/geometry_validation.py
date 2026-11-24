"""Pure validation and projection ordering for DICOM volume geometry."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from statistics import median
from typing import Any, Sequence

from pydicom.dataset import Dataset


class GeometryErrorCode(StrEnum):
    """Stable, machine-readable codes for rejected volume geometry."""

    MISSING_IPP = "MISSING_IPP"
    MISSING_IOP = "MISSING_IOP"
    MISSING_SPACING = "MISSING_SPACING"
    BAD_IOP = "BAD_IOP"
    DUPLICATE_POSITION = "DUPLICATE_POSITION"
    VARIANT_UNAVAILABLE = "VARIANT_UNAVAILABLE"
    IRREGULAR_SPACING = "IRREGULAR_SPACING"
    MIXED_FOR = "MIXED_FOR"
    UNSUPPORTED_GANTRY_TILT = "UNSUPPORTED_GANTRY_TILT"


class GeometryValidationError(ValueError):
    """Geometry error with a stable code and PHI-free, tag-oriented detail."""

    def __init__(self, code: GeometryErrorCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


@dataclass(frozen=True, slots=True)
class GeometryTolerances:
    """Strict numeric tolerances supplied by application configuration."""

    iop_tolerance: float = 1e-4
    position_tolerance_mm: float = 1e-3
    spacing_abs_tolerance_mm: float = 1e-3
    spacing_rel_tolerance: float = 1e-3

    def __post_init__(self) -> None:
        values = (
            (self.iop_tolerance, 0.1),
            (self.position_tolerance_mm, 1.0),
            (self.spacing_abs_tolerance_mm, 1.0),
            (self.spacing_rel_tolerance, 0.1),
        )
        if any(
            not math.isfinite(value) or value < 0 or value > maximum
            for value, maximum in values
        ):
            raise ValueError("geometry tolerance is outside its supported range")

    @classmethod
    def from_settings(cls, config: Any) -> GeometryTolerances:
        """Copy configured tolerance values without reading global state."""
        return cls(
            iop_tolerance=float(config.VOLUME_GEOMETRY_IOP_TOLERANCE),
            position_tolerance_mm=float(config.VOLUME_GEOMETRY_POSITION_TOLERANCE_MM),
            spacing_abs_tolerance_mm=float(
                config.VOLUME_GEOMETRY_SPACING_ABS_TOLERANCE_MM
            ),
            spacing_rel_tolerance=float(
                config.VOLUME_GEOMETRY_SPACING_REL_TOLERANCE
            ),
        )


@dataclass(frozen=True, slots=True)
class SliceGeometry:
    """Validated geometry for one single-frame instance or Enhanced frame."""

    source_index: int
    frame_index: int | None
    image_position_patient: tuple[float, float, float]
    image_orientation_patient: tuple[float, float, float, float, float, float]
    pixel_spacing: tuple[float, float]
    projected_position_mm: float


@dataclass(frozen=True, slots=True)
class SeriesGeometry:
    """Projection-sorted geometry retaining every source patient-space origin."""

    slices: tuple[SliceGeometry, ...]
    normal: tuple[float, float, float]
    slice_spacing_mm: float | None
    in_plane_offsets_mm: tuple[tuple[float, float, float], ...]
    has_in_plane_displacement: bool
    max_in_plane_displacement_mm: float = 0.0
    has_linear_gantry_tilt: bool = False
    gantry_tilt_deg: float | None = None


@dataclass(frozen=True, slots=True)
class _Candidate:
    source_index: int
    frame_index: int | None
    position: tuple[float, float, float]
    orientation: tuple[float, float, float, float, float, float]
    spacing: tuple[float, float]
    expected_slice_spacing_mm: float | None
    frame_of_reference_uid: str | None
    series_uid: str | None
    gantry_tilt_deg: float | None = None


def validate_series_geometry(
    datasets: Sequence[Dataset],
    *,
    tolerances: GeometryTolerances = GeometryTolerances(),
) -> SeriesGeometry:
    """Validate DICOM geometry and order slices by IPP projection on the normal.

    Oblique orientations and gantry tilt are retained through the normalized
    plane normal and each original IPP; no regular-stack origin is fabricated.
    """
    candidates = _collect_candidates(datasets, tolerances)
    if not candidates:
        raise GeometryValidationError(
            GeometryErrorCode.MISSING_IPP, "no image frames were supplied"
        )
    _reject_mixed_identity(candidates)

    reference_orientation = candidates[0].orientation
    reference_spacing = candidates[0].spacing
    for candidate in candidates[1:]:
        if any(
            abs(left - right) > tolerances.iop_tolerance
            for left, right in zip(candidate.orientation, reference_orientation)
        ):
            raise GeometryValidationError(
                GeometryErrorCode.BAD_IOP,
                "orientation differs within the supplied series",
            )
        if any(
            not _within_tolerance(left, right, tolerances)
            for left, right in zip(candidate.spacing, reference_spacing)
        ):
            raise GeometryValidationError(
                GeometryErrorCode.IRREGULAR_SPACING,
                "PixelSpacing differs within the supplied series",
            )

    row = reference_orientation[:3]
    column = reference_orientation[3:]
    normal = _unit_cross(row, column)
    projected = [
        (candidate, _dot(candidate.position, normal)) for candidate in candidates
    ]
    projected.sort(key=lambda item: item[1])

    positions = [position for _candidate, position in projected]
    differences = [right - left for left, right in zip(positions, positions[1:])]
    if any(
        difference <= tolerances.position_tolerance_mm
        for difference in differences
    ):
        raise GeometryValidationError(
            GeometryErrorCode.DUPLICATE_POSITION,
            "two or more frames have the same projected slice position",
        )

    declared_spacings = [
        item.expected_slice_spacing_mm for item in candidates
        if item.expected_slice_spacing_mm is not None
    ]
    if declared_spacings and any(
        not _within_tolerance(value, declared_spacings[0], tolerances)
        for value in declared_spacings[1:]
    ):
        raise GeometryValidationError(
            GeometryErrorCode.IRREGULAR_SPACING,
            "SpacingBetweenSlices differs within the supplied series",
        )
    slice_spacing = (
        declared_spacings[0] if declared_spacings else
        median(differences) if differences else None
    )
    if slice_spacing is not None:
        for index, difference in enumerate(differences):
            allowance = (
                tolerances.spacing_abs_tolerance_mm
                + tolerances.spacing_rel_tolerance * slice_spacing
            )
            if abs(difference - slice_spacing) > allowance:
                raise GeometryValidationError(
                    GeometryErrorCode.IRREGULAR_SPACING,
                    f"gap or irregular projected spacing after ordered frame {index}",
                )

    ordered = tuple(
        SliceGeometry(
            source_index=candidate.source_index,
            frame_index=candidate.frame_index,
            image_position_patient=candidate.position,
            image_orientation_patient=candidate.orientation,
            pixel_spacing=candidate.spacing,
            projected_position_mm=position,
        )
        for candidate, position in projected
    )
    origin = ordered[0].image_position_patient
    first_projection = ordered[0].projected_position_mm
    offsets = tuple(
        _in_plane_offset(item.image_position_patient, origin, normal,
                         item.projected_position_mm - first_projection)
        for item in ordered
    )
    offset_norms = [math.sqrt(_dot(offset, offset)) for offset in offsets]
    max_displacement = max(offset_norms, default=0.0)
    has_displacement = any(
        norm > tolerances.position_tolerance_mm for norm in offset_norms
    )
    ordered_candidates = [candidate for candidate, _position in projected]
    has_linear_tilt, tilt_deg = _analyze_gantry_tilt(
        ordered, ordered_candidates, offsets, max_displacement, has_displacement, tolerances
    )
    return SeriesGeometry(
        slices=ordered,
        normal=normal,
        slice_spacing_mm=slice_spacing,
        in_plane_offsets_mm=offsets,
        has_in_plane_displacement=has_displacement,
        max_in_plane_displacement_mm=max_displacement,
        has_linear_gantry_tilt=has_linear_tilt,
        gantry_tilt_deg=tilt_deg,
    )


def _collect_candidates(
    datasets: Sequence[Dataset], tolerances: GeometryTolerances
) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for source_index, dataset in enumerate(datasets):
        try:
            frame_count = int(getattr(dataset, "NumberOfFrames", 1) or 1)
        except (TypeError, ValueError, OverflowError) as exc:
            raise GeometryValidationError(
                GeometryErrorCode.MISSING_IPP,
                "NumberOfFrames must be a positive integer",
            ) from exc
        if frame_count < 1:
            raise GeometryValidationError(
                GeometryErrorCode.MISSING_IPP, "NumberOfFrames must be positive"
            )
        for frame_index in range(frame_count):
            position = _required_vector(
                _resolve_geometry_value(dataset, frame_index, "PlanePositionSequence",
                                        "ImagePositionPatient"),
                3,
                GeometryErrorCode.MISSING_IPP,
                "ImagePositionPatient",
            )
            orientation_value = _resolve_geometry_value(
                dataset, frame_index, "PlaneOrientationSequence",
                "ImageOrientationPatient",
            )
            if orientation_value is None:
                raise GeometryValidationError(
                    GeometryErrorCode.MISSING_IOP,
                    "ImageOrientationPatient is required",
                )
            orientation = _required_vector(
                orientation_value,
                6,
                GeometryErrorCode.BAD_IOP,
                "ImageOrientationPatient",
            )
            spacing = _required_vector(
                _resolve_geometry_value(dataset, frame_index,
                                        "PixelMeasuresSequence", "PixelSpacing"),
                2,
                GeometryErrorCode.MISSING_SPACING,
                "PixelSpacing",
            )
            expected_spacing = _optional_positive(
                _resolve_geometry_value(
                    dataset, frame_index, "PixelMeasuresSequence",
                    "SpacingBetweenSlices",
                )
            )
            gantry_tilt = _optional_finite(
                _resolve_geometry_value(
                    dataset, frame_index, "CTAcquisitionDetailsSequence",
                    "GantryDetectorTilt",
                )
            )
            _validate_orientation(orientation, tolerances)
            if any(value <= 0 for value in spacing):
                raise GeometryValidationError(
                    GeometryErrorCode.MISSING_SPACING,
                    "PixelSpacing values must be positive",
                )
            candidates.append(_Candidate(
                source_index=source_index,
                frame_index=frame_index if frame_count > 1 else None,
                position=position, orientation=orientation, spacing=spacing,
                expected_slice_spacing_mm=expected_spacing,
                frame_of_reference_uid=_optional_text(
                    getattr(dataset, "FrameOfReferenceUID", None)
                ),
                series_uid=_optional_text(getattr(dataset, "SeriesInstanceUID", None)),
                gantry_tilt_deg=gantry_tilt,
            ))
    return candidates


def _resolve_geometry_value(
    dataset: Dataset, frame_index: int, sequence_name: str, attribute: str
) -> Any:
    per_frame = getattr(dataset, "PerFrameFunctionalGroupsSequence", None)
    if per_frame and frame_index < len(per_frame):
        value = _functional_value(per_frame[frame_index], sequence_name, attribute)
        if value is not None:
            return value
    shared = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    if shared:
        value = _functional_value(shared[0], sequence_name, attribute)
        if value is not None:
            return value
    return getattr(dataset, attribute, None)


def _functional_value(group: Dataset, sequence_name: str, attribute: str) -> Any:
    sequence = getattr(group, sequence_name, None)
    if not sequence:
        return None
    return getattr(sequence[0], attribute, None)


def _required_vector(
    value: Any,
    length: int,
    code: GeometryErrorCode,
    tag: str,
) -> tuple[float, ...]:
    if value is None:
        raise GeometryValidationError(code, f"{tag} is required")
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        vector = ()
    if len(vector) != length or any(not math.isfinite(item) for item in vector):
        raise GeometryValidationError(code, f"{tag} must contain {length} finite values")
    return vector


def _optional_positive(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = abs(float(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeometryValidationError(
            GeometryErrorCode.IRREGULAR_SPACING,
            "SpacingBetweenSlices must be finite and non-zero when supplied",
        ) from exc
    if not math.isfinite(parsed) or parsed == 0:
        raise GeometryValidationError(
            GeometryErrorCode.IRREGULAR_SPACING,
            "SpacingBetweenSlices must be finite and non-zero when supplied",
        )
    return parsed


def _validate_orientation(
    orientation: tuple[float, ...], tolerances: GeometryTolerances
) -> None:
    tolerance = tolerances.iop_tolerance
    row = orientation[:3]
    column = orientation[3:]
    row_length = math.sqrt(_dot(row, row))
    column_length = math.sqrt(_dot(column, column))
    if (
        abs(row_length - 1.0) > tolerance
        or abs(column_length - 1.0) > tolerance
        or abs(_dot(row, column)) > tolerance
    ):
        raise GeometryValidationError(
            GeometryErrorCode.BAD_IOP,
            "ImageOrientationPatient must contain orthonormal direction cosines",
        )


def _reject_mixed_identity(candidates: Sequence[_Candidate]) -> None:
    frame_uids = {item.frame_of_reference_uid for item in candidates
                  if item.frame_of_reference_uid is not None}
    series_uids = {item.series_uid for item in candidates if item.series_uid is not None}
    if len(frame_uids) > 1 or len(series_uids) > 1:
        raise GeometryValidationError(
            GeometryErrorCode.MIXED_FOR,
            "frames contain more than one FrameOfReferenceUID or SeriesInstanceUID",
        )


def _within_tolerance(
    left: float, right: float, tolerances: GeometryTolerances
) -> bool:
    allowance = (
        tolerances.spacing_abs_tolerance_mm
        + tolerances.spacing_rel_tolerance * max(abs(left), abs(right))
    )
    return abs(left - right) <= allowance


def _unit_cross(
    left: tuple[float, ...], right: tuple[float, ...]
) -> tuple[float, float, float]:
    cross = (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )
    length = math.sqrt(_dot(cross, cross))
    if length == 0:
        raise GeometryValidationError(
            GeometryErrorCode.BAD_IOP, "direction cosines produce a zero normal"
        )
    return tuple(value / length for value in cross)


def _in_plane_offset(
    position: tuple[float, ...], origin: tuple[float, ...],
    normal: tuple[float, ...], projection_delta: float,
) -> tuple[float, float, float]:
    return tuple(
        position[index] - origin[index] - normal[index] * projection_delta
        for index in range(3)
    )


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _analyze_gantry_tilt(
    ordered: Sequence[SliceGeometry],
    candidates: Sequence[_Candidate],
    offsets: Sequence[tuple[float, float, float]],
    max_displacement: float,
    has_displacement: bool,
    tolerances: GeometryTolerances,
) -> tuple[bool, float | None]:
    """Classify whether in-plane IPP offsets represent linear gantry tilt."""
    declared_tilts = [
        item.gantry_tilt_deg for item in candidates if item.gantry_tilt_deg is not None
    ]
    consistent_declared: float | None = None
    if declared_tilts and all(abs(v - declared_tilts[0]) <= 0.5 for v in declared_tilts[1:]):
        consistent_declared = declared_tilts[0]

    if not has_displacement or len(ordered) < 2:
        return False, consistent_declared

    total_proj = ordered[-1].projected_position_mm - ordered[0].projected_position_mm
    end_offset = offsets[-1]
    end_norm = math.sqrt(_dot(end_offset, end_offset))
    if total_proj <= 0 or end_norm <= tolerances.position_tolerance_mm:
        return False, consistent_declared

    geometric_tilt_deg = math.degrees(math.atan2(end_norm, total_proj))
    # Real CT gantries physically tilt up to ~30-45 deg; reject pathological shear.
    if geometric_tilt_deg >= 60.0:
        return False, consistent_declared if consistent_declared else geometric_tilt_deg

    # Real CT headers round IPP to ~0.01-0.05 mm; allow a tight linearity floor
    # while still rejecting erratic/corrupt non-linear in-plane jumps.
    lin_tol = (
        max(tolerances.position_tolerance_mm * 10.0, 0.1)
        + tolerances.spacing_rel_tolerance * max_displacement
    )
    first_proj = ordered[0].projected_position_mm
    linear = True
    for item, offset in zip(ordered, offsets):
        frac = (item.projected_position_mm - first_proj) / total_proj
        residual = tuple(offset[d] - frac * end_offset[d] for d in range(3))
        if math.sqrt(_dot(residual, residual)) > lin_tol:
            linear = False
            break

    reported_tilt = (
        consistent_declared
        if consistent_declared is not None and abs(consistent_declared) > 0.05
        else geometric_tilt_deg
    )
    return linear, reported_tilt


def assess_reformat_safety(
    datasets: Sequence[Dataset],
    *,
    tolerances: GeometryTolerances = GeometryTolerances(),
    gantry_tilt_resample_enabled: bool = True,
) -> dict[str, Any]:
    """Return a structured, PHI-free summary of whether a stack is safe for MPR/3D."""
    try:
        geometry = validate_series_geometry(datasets, tolerances=tolerances)
    except GeometryValidationError as exc:
        return {
            "reformat_safe": False,
            "code": exc.code.value,
            "reason": exc.detail,
            "requires_deskew": False,
            "gantry_tilt_deg": None,
            "max_in_plane_displacement_mm": 0.0,
        }
    if geometry.slice_spacing_mm is None:
        return {
            "reformat_safe": False,
            "code": GeometryErrorCode.MISSING_SPACING.value,
            "reason": "at least two positioned slices are required to establish slice spacing",
            "requires_deskew": False,
            "gantry_tilt_deg": geometry.gantry_tilt_deg,
            "max_in_plane_displacement_mm": geometry.max_in_plane_displacement_mm,
        }
    if geometry.has_in_plane_displacement:
        if not gantry_tilt_resample_enabled or not geometry.has_linear_gantry_tilt:
            return {
                "reformat_safe": False,
                "code": GeometryErrorCode.UNSUPPORTED_GANTRY_TILT.value,
                "reason": (
                    "gantry-tilt volume resampling is disabled"
                    if not gantry_tilt_resample_enabled
                    else "non-linear per-slice in-plane displacement cannot be deskewed"
                ),
                "requires_deskew": True,
                "gantry_tilt_deg": geometry.gantry_tilt_deg,
                "max_in_plane_displacement_mm": geometry.max_in_plane_displacement_mm,
            }
    return {
        "reformat_safe": True,
        "code": None,
        "reason": None,
        "requires_deskew": geometry.has_in_plane_displacement,
        "gantry_tilt_deg": geometry.gantry_tilt_deg,
        "max_in_plane_displacement_mm": geometry.max_in_plane_displacement_mm,
    }
