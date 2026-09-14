"""Pixel-data to image helpers shared by the rendered (WADO-RS) endpoint,
thumbnail generation and the viewer.

Handles grayscale (window/level + modality LUT + MONOCHROME1 inversion) and
colour (RGB / YBR_FULL) photometric interpretations.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

try:  # pydicom 3.x
    from pydicom.pixels import apply_modality_lut, apply_voi_lut, convert_color_space
except ImportError:  # pydicom 2.x fallback
    from pydicom.pixel_data_handlers.util import (  # type: ignore
        apply_modality_lut,
        apply_voi_lut,
        convert_color_space,
    )


def _is_color(ds) -> bool:
    pi = str(getattr(ds, "PhotometricInterpretation", ""))
    return pi in ("RGB", "YBR_FULL", "YBR_FULL_422", "YBR_PARTIAL_420", "YBR_ICT", "YBR_RCT")


def _normalize_u8(arr: np.ndarray) -> np.ndarray:
    """Percentile-clip + map to 0..255 uint8."""
    arr = np.asarray(arr, dtype=np.float64)
    lo, hi = np.percentile(arr, (1, 99))
    if hi <= lo:
        hi = lo + 1.0
    out = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
    return (out * 255.0).astype(np.uint8)


def dataset_to_image(ds, window: str | None = None, max_size: int | None = None) -> Image.Image:
    """Render a DICOM dataset to a PIL Image (auto window/level applied)."""
    arr = ds.pixel_array

    if _is_color(ds):
        if str(getattr(ds, "PhotometricInterpretation", "")).startswith("YBR"):
            arr = convert_color_space(arr, ds.PhotometricInterpretation, "RGB")
        if arr.dtype != np.uint8:
            arr = _normalize_u8(arr)
        img = Image.fromarray(arr, mode="RGB")
    else:
        # CT/MR: modality LUT (rescale slope/intercept) then VOI windowing.
        try:
            arr = apply_modality_lut(arr, ds)
        except Exception:
            arr = np.asarray(arr, dtype=np.float64)
        if window:
            try:
                center, width = (float(x) for x in window.split(",")[:2])
            except (ValueError, AttributeError):
                center, width = None, None
            if center is not None and width and width > 0:
                lo, hi = center - width / 2.0, center + width / 2.0
                arr = np.clip(arr, lo, hi)
                u8 = ((arr - lo) / (hi - lo) * 255.0).astype(np.uint8)
            else:
                u8 = _normalize_u8(arr)
        else:
            try:
                arr = apply_voi_lut(arr, ds)
            except Exception:
                pass
            u8 = _normalize_u8(arr)
        if str(getattr(ds, "PhotometricInterpretation", "")) == "MONOCHROME1":
            u8 = 255 - u8
        img = Image.fromarray(u8, mode="L")

    if max_size:
        img.thumbnail((max_size, max_size), Image.LANCZOS)
    return img


def dataset_to_bytes(ds, fmt: str = "PNG", window: str | None = None,
                     max_size: int | None = None) -> bytes:
    img = dataset_to_image(ds, window=window, max_size=max_size)
    buf = io.BytesIO()
    if fmt.upper() == "JPEG":
        img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=88)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue()
