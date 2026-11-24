"""Reference grayscale rendering: stored padding, modality, VOI, presentation."""
from __future__ import annotations

import numpy as np
from pydicom.dataset import Dataset
from pydicom.pixels import apply_modality_lut, apply_voi_lut, apply_presentation_lut

from app.services.volume.calibration import resolve_frame_calibration


def _has_functional_groups(ds: Dataset) -> bool:
    """Enhanced (multi-frame functional group) object — calibration lives per frame."""
    return "PerFrameFunctionalGroupsSequence" in ds or "SharedFunctionalGroupsSequence" in ds


def _frame_voi_source(ds: Dataset, frame: int) -> Dataset:
    """Where the VOI window for ``frame`` lives (H-09): PerFrame, Shared, then top level.

    Enhanced CT/MR/PET carry WindowCenter/Width in FrameVOILUTSequence inside the
    functional groups; reading only the top level made auto-window fall back to
    min/max for every Enhanced object.
    """
    per_frame = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
    shared = getattr(ds, "SharedFunctionalGroupsSequence", None)
    groups = []
    if per_frame and 0 <= frame < len(per_frame):
        groups.append(per_frame[frame])
    if shared:
        groups.append(shared[0])
    for group in groups:
        voi_items = getattr(group, "FrameVOILUTSequence", None)
        item = voi_items[0] if voi_items else None
        if item is not None and "WindowCenter" in item and "WindowWidth" in item:
            return item
    return ds


def _modality_values(ds: Dataset, stored: np.ndarray, frame: int) -> np.ndarray:
    """Stored values to modality units, honouring functional-group rescale (H-09).

    Same PerFrame -> Shared -> top-level order as the volume path
    (``volume.calibration``), so a rendered slice and an MPR agree on HU.
    """
    if not _has_functional_groups(ds):
        return apply_modality_lut(stored, ds)
    calibration = resolve_frame_calibration(ds, frame)
    return stored.astype(np.float64) * calibration.slope + calibration.intercept


def linear_window(values: np.ndarray, center: float, width: float) -> np.ndarray:
    """Apply PS3.3 C.11.2 LINEAR, including its width-one threshold."""
    if not np.isfinite(center) or not np.isfinite(width) or width < 1:
        raise ValueError("Invalid DICOM LINEAR window")
    if width == 1:
        return np.where(values <= center-0.5, 0.0, 255.0)
    return np.clip((values-(center-0.5))/(width-1)+0.5, 0, 1)*255


def padding_mask(values: np.ndarray, ds: Dataset) -> np.ndarray:
    """Identify padding in stored-value space before rescale or LUT transforms."""
    if "PixelPaddingValue" not in ds:
        return np.zeros(values.shape, dtype=bool)
    a = float(ds.PixelPaddingValue)
    b = float(getattr(ds, "PixelPaddingRangeLimit", a))
    return (values >= min(a, b)) & (values <= max(a, b))


def _voi(values: np.ndarray, ds: Dataset, override: str | None,
         padding: np.ndarray) -> np.ndarray:
    """Keep the VOI's specified output range; never renormalize displayed samples."""
    if override:
        try:
            center, width = (float(value) for value in override.split(",")[:2])
        except (ValueError, TypeError):
            return _voi(values, ds, None, padding)
        if np.isfinite(center) and np.isfinite(width) and width >= 1:
            return linear_window(values, center, width)
        return _voi(values, ds, None, padding)
    if "VOILUTSequence" in ds:
        bits = int(ds.VOILUTSequence[0].LUTDescriptor[2])
        return apply_voi_lut(values, ds).astype(float)*255/(2**bits-1)
    if "WindowCenter" in ds and "WindowWidth" in ds:
        center = float(ds.WindowCenter[0] if ds[0x00281050].VM > 1 else ds.WindowCenter)
        width = float(ds.WindowWidth[0] if ds[0x00281051].VM > 1 else ds.WindowWidth)
        kind = str(getattr(ds, "VOILUTFunction", "LINEAR"))
        if kind == "LINEAR":
            return linear_window(values, center, width)
        if width <= 0:
            raise ValueError("Invalid DICOM VOI width")
        if kind == "SIGMOID":
            return 255/(1+np.exp(np.clip(-4*(values-center)/width, -700, 700)))
        if kind == "LINEAR_EXACT":
            return np.clip((values-center)/width+0.5, 0, 1)*255
        raise ValueError("Unsupported DICOM VOI function")
    valid = values[~padding & np.isfinite(values)]
    if valid.size == 0:
        return np.zeros(values.shape)
    low, high = float(valid.min()), float(valid.max())
    return linear_window(values, (low+high+1)/2, high-low+1)


def _display_inverted(ds: Dataset) -> bool:
    """Whether the minimum displayed value is white (H-09).

    MONOCHROME1 always displays inverted, whatever PresentationLUTShape says.
    DX/CR devices do emit MONOCHROME1 with IDENTITY, although the standard
    pairs MONOCHROME1 with INVERSE (PS3.3 C.8.11.1.1). Honouring IDENTITY there
    rendered bones black while the Cornerstone viewer, which inverts every
    MONOCHROME1 image, shows them white: a thumbnail became a negative of the
    image being read. For MONOCHROME2, INVERSE inverts (PS3.3 C.11.6.1).
    """
    if str(getattr(ds, "PhotometricInterpretation", "")) == "MONOCHROME1":
        return True
    return str(getattr(ds, "PresentationLUTShape", "")).strip().upper() == "INVERSE"


def render_grayscale(ds: Dataset, stored: np.ndarray, override: str | None,
                     frame_index: int = 0) -> np.ndarray:
    """Render without replacing invalid calibration/LUTs with guessed numbers.

    ``stored`` is the single frame ``frame_index``; the index selects that
    frame's functional-group rescale and window on Enhanced objects (H-09).
    """
    padding = padding_mask(stored, ds)
    values = _modality_values(ds, stored, frame_index)
    if not np.isfinite(values[~padding]).all():
        raise ValueError("Non-finite grayscale calibration or pixels")
    displayed = _voi(values, _frame_voi_source(ds, frame_index), override, padding)
    u8 = np.rint(np.clip(displayed, 0, 255)).astype(np.uint8)
    if "PresentationLUTSequence" in ds:
        result = apply_presentation_lut(u8, ds)
        bits = int(ds.PresentationLUTSequence[0].LUTDescriptor[2])
        result = np.rint(result.astype(float)*255/(2**bits-1))
        u8 = np.clip(result, 0, 255).astype(np.uint8)
    elif _display_inverted(ds):
        u8 = 255-u8
    u8[padding] = 0
    return u8
