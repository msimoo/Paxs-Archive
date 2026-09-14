"""Lossless DICOM compression with graceful encoder fallback.

Supported methods:
  * RLE        — built into pydicom, always available
  * JPEG-LS    — needs `pyjpegls`
  * JPEG2000   — needs `pylibjpeg-openjpeg` (or imagecodecs)
  * HTJ2K      — needs `imagecodecs` (bundles the openjph codec)

pydicom < 3.1 registers no HTJ2K encoder class, so when the configured
method is HTJ2K we encode the frames with ``imagecodecs.htj2k_encode``
ourselves and write the encapsulated file with the HTJ2K Lossless transfer
syntax — the same thing pydicom >= 3.1's encoder does internally.

If the requested encoder is unavailable we fall back to RLE so the archive
always keeps working. Compression writes to a temp file and atomically
replaces the original — a crash never leaves a half-written instance.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pydicom
from pydicom.uid import (
    HTJ2KLossless,
    JPEG2000Lossless,
    JPEGLSLossless,
    RLELossless,
)

log = logging.getLogger("paxs.compression")

COMPRESSION_METHODS: dict[str, str] = {
    "RLE": str(RLELossless),
    "JPEG-LS": str(JPEGLSLossless),
    "JPEG2000": str(JPEG2000Lossless),
    "HTJ2K": str(HTJ2KLossless),
}

_ALIASES = {
    "JPEGLS": "JPEG-LS",
    "JPEG_LS": "JPEG-LS",
    "J2K": "JPEG2000",
    "JPEG2000_LOSSLESS": "JPEG2000",
}


def normalize_method(method: str) -> str:
    method = (method or "RLE").strip().upper()
    return _ALIASES.get(method, method)


def _imagecodecs_htj2k_available() -> bool:
    """True when the imagecodecs HTJ2K codec (bundled openjph) is installed."""
    try:
        import imagecodecs

        return callable(getattr(imagecodecs, "htj2k_encode", None))
    except Exception:
        return False


def _pydicom_htj2k_encoder() -> bool:
    """True when pydicom itself registers a usable HTJ2K encoder (>= 3.1)."""
    try:
        from pydicom import pixels as _pixels

        encoder = getattr(_pixels.encoders, "HTJ2KLosslessEncoder", None)
        if encoder is None:
            return False
        return bool(encoder.is_available)
    except Exception:
        return False


def encoder_available(transfer_syntax_uid: str) -> bool:
    """True when this archive can actually encode this transfer syntax.

    RLE / JPEG-LS / JPEG2000 map to pydicom encoder classes (resolved
    defensively — not every class exists in every pydicom version). HTJ2K is
    available via pydicom's own encoder on pydicom >= 3.1, or via the
    imagecodecs path used on pydicom 3.0.x — either one counts.
    """
    if transfer_syntax_uid == str(HTJ2KLossless):
        return _pydicom_htj2k_encoder() or _imagecodecs_htj2k_available()
    try:
        from pydicom import pixels as _pixels

        encoders_mod = _pixels.encoders
    except ImportError:
        return False
    names = {
        str(RLELossless): "RLELosslessEncoder",
        str(JPEGLSLossless): "JPEGLSLosslessEncoder",
        str(JPEG2000Lossless): "JPEG2000LosslessEncoder",
    }
    encoder_name = names.get(transfer_syntax_uid)
    if encoder_name is None:
        return False
    encoder = getattr(encoders_mod, encoder_name, None)
    if encoder is None:
        return False
    try:
        return bool(encoder.is_available)
    except Exception:
        return False


def _compress_htj2k(ds) -> None:
    """Encode every frame losslessly as HTJ2K and rewrite the dataset in place
    with the HTJ2K Lossless transfer syntax (pydicom < 3.1 has no HTJ2K
    encoder class — this is the encapsulation its encoder performs internally
    on newer pydicom). Mutates ``ds``: PixelData becomes the encapsulated
    codestream and file_meta.TransferSyntaxUID switches to HTJ2K Lossless.
    """
    import numpy as np

    from pydicom.encaps import encapsulate

    import imagecodecs  # lazy — import must work when the codec is absent

    arr = np.asarray(ds.pixel_array)
    rgb = ds.PhotometricInterpretation in ("RGB", "YBR_FULL", "YBR_FULL_422", "YBR_ICT", "YBR_RCT")
    if arr.ndim == 2:
        frames = [arr]
    elif arr.ndim == 3 and rgb:
        # Single-frame color: (Rows, Cols, 3) — encode as one RGB frame.
        frames = [arr]
    else:
        # Multi-frame grayscale: (F, R, C); color multi-frame: (F, R, C, 3).
        frames = [np.asarray(f) for f in arr]

    encoded = []
    for frame in frames:
        is_rgb = frame.ndim == 3 and frame.shape[-1] in (3, 4)
        encoded.append(imagecodecs.htj2k_encode(
            frame, level=0, reversible=True, rgb=is_rgb or None,
        ))

    ds.PixelData = encapsulate(encoded)
    ds.file_meta.TransferSyntaxUID = HTJ2KLossless
    if hasattr(ds, "NumberOfFrames") and ds.NumberOfFrames != len(frames):
        ds.NumberOfFrames = len(frames)


_CODEC_NOTES = {
    "RLE": "built into pydicom — always available",
    "JPEG-LS": "requires pyjpegls",
    "JPEG2000": "requires pylibjpeg-openjpeg or imagecodecs",
    "HTJ2K": "requires imagecodecs (bundles the openjph codec)",
}


def codec_status() -> list[dict]:
    """Availability of every lossless compression method the worker can use.

    Returns one entry per method in :data:`COMPRESSION_METHODS` with the
    target transfer-syntax UID, whether pydicom has a usable encoder
    installed, and the runtime note (which package provides it). This is the
    ground truth the worker relies on: :func:`compress_file` falls back to
    RLE whenever ``available`` is false for the configured method, so an
    admin can see at a glance that ``DEFAULT_COMPRESSION=HTJ2K`` is currently
    degrading to RLE until the codec package is installed.
    """
    out: list[dict] = []
    for method, uid in COMPRESSION_METHODS.items():
        out.append({
            "method": method,
            "transfer_syntax_uid": str(uid),
            "available": encoder_available(str(uid)),
            "note": _CODEC_NOTES.get(method, ""),
        })
    return out


class CompressionError(RuntimeError):
    pass


def compress_file(path: str | Path, method: str = "RLE") -> dict:
    """Compress a DICOM file losslessly in place. Returns stats dict."""
    path = Path(path)
    original_size = path.stat().st_size

    method = normalize_method(method)
    target_uid = COMPRESSION_METHODS.get(method, str(RLELossless))

    ds = pydicom.dcmread(str(path))
    current_ts = str(getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", "") or "")

    if current_ts == target_uid:
        return {
            "original_bytes": original_size,
            "compressed_bytes": original_size,
            "saved_percent": 0.0,
            "transfer_syntax_uid": target_uid,
            "method": method,
            "already_compressed": True,
        }

    if not encoder_available(target_uid):
        log.warning("Encoder for %s unavailable, falling back to RLE", method)
        method = "RLE"
        target_uid = str(RLELossless)

    if not encoder_available(target_uid):
        raise CompressionError("No available encoder for RLE compression")

    try:
        if target_uid == str(HTJ2KLossless) and not _pydicom_htj2k_encoder():
            # pydicom 3.0.x: no HTJ2K encoder class — encode via imagecodecs.
            # _compress_htj2k only mutates `ds` after every frame encodes, so a
            # mid-encode failure leaves the dataset intact for an RLE retry.
            try:
                _compress_htj2k(ds)
            except Exception as exc:
                log.warning("HTJ2K encode failed (%s) — falling back to RLE", exc)
                method = "RLE"
                target_uid = str(RLELossless)
                from pydicom.uid import UID

                ds.compress(UID(target_uid))
        else:
            from pydicom.uid import UID

            ds.compress(UID(target_uid))
    except Exception as exc:  # e.g. missing decode handler for the source file
        raise CompressionError(f"Cannot compress {path.name}: {exc}") from exc

    tmp = path.with_suffix(path.suffix + ".tmp")
    ds.save_as(str(tmp), enforce_file_format=True)
    os.replace(tmp, path)

    compressed_size = path.stat().st_size
    saved = ((original_size - compressed_size) / original_size * 100.0) if original_size else 0.0
    return {
        "original_bytes": original_size,
        "compressed_bytes": compressed_size,
        "saved_percent": round(saved, 2),
        "transfer_syntax_uid": target_uid,
        "method": method,
        "already_compressed": False,
    }
