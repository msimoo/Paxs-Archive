"""DICOM frame rescale and pixel-padding helpers for volume decoders."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from pydicom.dataset import Dataset


@dataclass(frozen=True)
class FrameCalibration:
    """Resolved scalar transform and raw stored-value padding interval."""

    slope: float
    intercept: float
    valid: bool
    padding_low: float | None
    padding_high: float | None
    rescale_type: str | None


def _sequence_item(group: object, name: str) -> object | None:
    sequence = getattr(group, name, None)
    return sequence[0] if sequence else None


def _resolved_value(dataset: Dataset, frame: int, name: str) -> tuple[float, bool]:
    """Resolve one transform attribute in PerFrame, Shared, dataset order."""
    per_frame = getattr(dataset, "PerFrameFunctionalGroupsSequence", None)
    shared = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    sources = []
    if per_frame and 0 <= frame < len(per_frame):
        sources.append(_sequence_item(per_frame[frame], "PixelValueTransformationSequence"))
    if shared:
        sources.append(_sequence_item(shared[0], "PixelValueTransformationSequence"))
    sources.append(dataset)
    for source in sources:
        if source is None or not hasattr(source, name):
            continue
        try:
            value = float(getattr(source, name))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value, True
    return (1.0 if name == "RescaleSlope" else 0.0), False


def _resolved_type(dataset: Dataset, frame: int) -> str | None:
    """Resolve optional rescale units using the same functional-group order."""
    per_frame = getattr(dataset, "PerFrameFunctionalGroupsSequence", None)
    shared = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    sources = []
    if per_frame and 0 <= frame < len(per_frame):
        sources.append(_sequence_item(per_frame[frame], "PixelValueTransformationSequence"))
    if shared:
        sources.append(_sequence_item(shared[0], "PixelValueTransformationSequence"))
    sources.append(dataset)
    for source in sources:
        value = getattr(source, "RescaleType", None) if source is not None else None
        if value:
            return str(value).strip().upper()
    return None


def resolve_frame_calibration(dataset: Dataset, frame: int) -> FrameCalibration:
    """Resolve frame transform and raw padding range without inventing validity."""
    slope, has_slope = _resolved_value(dataset, frame, "RescaleSlope")
    intercept, has_intercept = _resolved_value(dataset, frame, "RescaleIntercept")
    if slope == 0:
        has_slope = False
    padding_low = padding_high = None
    try:
        padding_low = float(dataset.PixelPaddingValue)
        padding_high = float(getattr(dataset, "PixelPaddingRangeLimit", padding_low))
        if not (math.isfinite(padding_low) and math.isfinite(padding_high)):
            padding_low = padding_high = None
        else:
            padding_low, padding_high = min(padding_low, padding_high), max(padding_low, padding_high)
    except (AttributeError, TypeError, ValueError):
        pass
    return FrameCalibration(
        slope=slope,
        intercept=intercept,
        valid=has_slope and has_intercept,
        padding_low=padding_low,
        padding_high=padding_high,
        rescale_type=_resolved_type(dataset, frame),
    )


def apply_frame_calibration(pixels: np.ndarray, calibration: FrameCalibration) -> np.ndarray:
    """Calibrate raw pixels and replace DICOM padding samples with NaN."""
    result = pixels.astype(np.float32, copy=True)
    if calibration.slope != 1.0:
        np.multiply(result, np.float32(calibration.slope), out=result)
    if calibration.intercept != 0.0:
        np.add(result, np.float32(calibration.intercept), out=result)
    if calibration.padding_low is not None and calibration.padding_high is not None:
        padding = (pixels >= calibration.padding_low) & (pixels <= calibration.padding_high)
        result[padding] = np.nan
    return result
