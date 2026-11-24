"""Pixel-data to image helpers shared by the rendered (WADO-RS) endpoint,
thumbnail generation and the viewer.

Handles grayscale (window/level + modality LUT + MONOCHROME1 inversion) and
colour (RGB / YBR_FULL) photometric interpretations.
"""

from __future__ import annotations

import io
import logging

import numpy as np
from PIL import Image

log = logging.getLogger("paxs.imaging")

try:  # pydicom 3.x
    from pydicom.pixels import apply_color_lut, apply_modality_lut, apply_voi_lut, convert_color_space, apply_presentation_lut
except ImportError:  # expected on pydicom 2.x — the legacy module is the documented source
    from pydicom.pixel_data_handlers.util import (  # type: ignore
        apply_color_lut,
        apply_modality_lut,
        apply_voi_lut,
        convert_color_space,
        apply_presentation_lut,
    )


def _is_color(ds) -> bool:
    pi = str(getattr(ds, "PhotometricInterpretation", ""))
    return pi in ("RGB", "YBR_FULL", "YBR_FULL_422", "YBR_PARTIAL_420", "YBR_ICT", "YBR_RCT", "PALETTE COLOR")


class FrameIndexError(ValueError):
    """The requested frame does not exist in this object (WADO-RS answers 404)."""


def _select_frame(pixels: np.ndarray, frame_index: int, frame_ndim: int) -> np.ndarray:
    """Pick frame ``frame_index`` from a multi-frame array (H-09); single frames pass through."""
    if pixels.ndim == frame_ndim:
        if frame_index != 0:
            raise FrameIndexError(f"frame_index {frame_index} out of range for 1 frame")
        return pixels
    frame_count = int(pixels.shape[0])
    if not 0 <= frame_index < frame_count:
        raise FrameIndexError(f"frame_index {frame_index} out of range for {frame_count} frames")
    return pixels[frame_index]


def _color_pixels(ds, frame_index: int = 0) -> tuple[np.ndarray, int]:
    """Decode color samples once, returning RGB pixels and their defined bit range."""
    photometric = str(getattr(ds, "PhotometricInterpretation", ""))
    if photometric.startswith("YBR") and hasattr(ds, "pixel_array_options"):
        ds.pixel_array_options(raw=True)
    pixels = ds.pixel_array
    if photometric == "PALETTE COLOR":
        pixels = _select_frame(pixels, frame_index, 2)
        pixels = apply_color_lut(pixels, ds)
        bits = int(ds.RedPaletteColorLookupTableDescriptor[2])
    else:
        pixels = _select_frame(pixels, frame_index, 3)
        if photometric.startswith("YBR"):
            pixels = convert_color_space(pixels, photometric, "RGB")
        bits = int(getattr(ds, "BitsStored", 8))
    if pixels.ndim != 3 or pixels.shape[-1] != 3:
        raise ValueError("Color pixel data must decode to an RGB image")
    if bits < 1 or bits > 16:
        raise ValueError("Unsupported color sample bit depth")
    if bits != 8 or pixels.dtype != np.uint8:
        maximum = float((1 << bits) - 1)
        pixels = np.rint(np.clip(pixels.astype(np.float64), 0, maximum) * (255.0 / maximum)).astype(np.uint8)
    return pixels, bits


def dataset_to_image(ds, window: str | None = None, max_size: int | None = None,
                     frame_index: int = 0) -> Image.Image:
    """Render frame ``frame_index`` (0-based) of a DICOM dataset to a PIL Image.

    H-09: rendering used to show frame 1 of every multi-frame object whatever
    was asked, and ignore Enhanced functional-group rescale/window. Raises
    ``ValueError`` naming the index when it is out of range.
    """
    if _is_color(ds):
        arr, _bits = _color_pixels(ds, frame_index)
        img = Image.fromarray(arr, mode="RGB")
    else:
        arr = _select_frame(ds.pixel_array, frame_index, 2)
        from app.services.pixel_pipeline import render_grayscale
        u8 = render_grayscale(ds, arr, window, frame_index=frame_index)
        img = Image.fromarray(u8, mode="L")

    if max_size:
        img.thumbnail((max_size, max_size), Image.LANCZOS)
    return img


def dataset_to_bytes(ds, fmt: str = "PNG", window: str | None = None,
                     max_size: int | None = None, frame_index: int = 0) -> bytes:
    """Encode frame ``frame_index`` of ``ds`` as PNG or JPEG bytes."""
    img = dataset_to_image(ds, window=window, max_size=max_size, frame_index=frame_index)
    buf = io.BytesIO()
    if fmt.upper() == "JPEG":
        img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=88)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue()
