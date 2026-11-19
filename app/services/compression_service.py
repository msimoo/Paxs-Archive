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
import tempfile
from pathlib import Path

import pydicom
from app.core.dicom_diagnostics import decode_context

from pydicom.uid import (
    ExplicitVRLittleEndian,
    HTJ2KLossless,
    JPEG2000Lossless,
    JPEGLSLossless,
    RLELossless,
    UID,
)

log = logging.getLogger("paxs.compression")
SOURCE_METHOD = "AS_SOURCE"
NATIVE_METHOD = "UNCOMPRESSED"
THUMBNAIL_METHOD = "THUMBNAIL"

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
    """True when imagecodecs exposes its bundled OpenJPH HTJ2K encoder."""
    try:
        import imagecodecs

        return callable(getattr(imagecodecs, "htj2k_encode", None))
    except Exception as exc:
        # Codec probing is optional, but unexpected loader errors remain visible.
        log.debug("imagecodecs HTJ2K probe failed (error_type=%s)", type(exc).__name__)
        return False


def _pydicom_htj2k_encoder() -> bool:
    """True when pydicom itself registers a usable HTJ2K encoder (>= 3.1)."""
    try:
        from pydicom import pixels as _pixels

        encoder = getattr(_pixels.encoders, "HTJ2KLosslessEncoder", None)
        if encoder is None:
            return False
        return bool(encoder.is_available)
    except Exception as exc:
        log.debug("pydicom HTJ2K encoder probe failed (error_type=%s)", type(exc).__name__)
        return False


def encoder_available(transfer_syntax_uid: str) -> bool:
    """True when this archive can actually encode this transfer syntax.

    RLE / JPEG-LS / JPEG2000 map to pydicom encoder classes (resolved
    defensively — not every class exists in every pydicom version). HTJ2K is
    available via pydicom's own encoder on pydicom >= 3.1, or via the
    imagecodecs path used on pydicom 3.0.x — either one counts. Probe failures
    are logged without dataset content and reported as unavailable so callers
    can select the lossless RLE fallback.
    """
    if transfer_syntax_uid == str(HTJ2KLossless):
        return _pydicom_htj2k_encoder() or _imagecodecs_htj2k_available()
    try:
        from pydicom import pixels as _pixels

        encoders_mod = _pixels.encoders
    except ImportError as exc:
        log.debug("pydicom pixel encoder registry unavailable (error_type=%s)",
                  type(exc).__name__)
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
    except Exception as exc:
        log.debug("pydicom encoder availability probe failed for %s (error_type=%s)",
                  encoder_name, type(exc).__name__)
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

    if ds.PhotometricInterpretation not in ("MONOCHROME1", "MONOCHROME2"):
        raise CompressionError("The bundled HTJ2K encoder supports monochrome DICOM images here")
    # The bundled encoder derives codestream precision from the numpy dtype.
    # Underfilled containers (e.g. 12 stored bits in 16) require a different encoder.
    if ds.BitsStored != ds.BitsAllocated:
        raise CompressionError("The bundled HTJ2K encoder cannot preserve this pixel precision")
    arr = np.asarray(ds.pixel_array)
    frames = [arr] if arr.ndim == 2 else arr
    encoded = [imagecodecs.htj2k_encode(frame, level=0, reversible=True) for frame in frames]

    ds.PixelData = encapsulate(encoded)
    ds["PixelData"].is_undefined_length = True
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


def compression_window_open(timing: str, start: str, end: str, local_time: str) -> bool:
    if timing != "scheduled" or start == end:
        return True
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


class CompressionError(RuntimeError):
    pass


def compress_file(path: str | Path, method: str = "RLE") -> dict:
    """Compress a DICOM file losslessly in place; return stats or CompressionError.

    Encoder/read/publish failures preserve the original file and emit PHI-free
    diagnostics. Only lossless codec fallback is permitted.
    """
    path = Path(path)
    try:
        original_size = path.stat().st_size
        ds = pydicom.dcmread(str(path))
    except Exception as exc:
        # The input boundary includes decoder/plugin and filesystem failures;
        # filenames and raw exception messages are not safe diagnostic context.
        log.error("Compression source read failed (error_type=%s); dataset_unreadable",
                  type(exc).__name__)
        raise CompressionError(
            f"Cannot read DICOM compression source (error_type={type(exc).__name__})"
        ) from None

    method = normalize_method(method)
    # Keep diagnostic method names in the known-codec vocabulary; unknown input
    # has always selected RLE and must not be echoed into a PHI-free error log.
    if method not in COMPRESSION_METHODS and method not in {SOURCE_METHOD, NATIVE_METHOD}:
        method = "RLE"
    current_ts = str(getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", "") or "")
    target_uid = COMPRESSION_METHODS.get(method, str(ExplicitVRLittleEndian))

    if (current_ts == target_uid or method == SOURCE_METHOD
            or (method == NATIVE_METHOD and current_ts and not UID(current_ts).is_compressed)):
        return {
            "original_bytes": original_size,
            "compressed_bytes": original_size,
            "saved_percent": 0.0,
            "transfer_syntax_uid": current_ts,
            "method": method,
            "already_compressed": True,
        }

    if method != NATIVE_METHOD and not encoder_available(target_uid):
        log.warning("Encoder for %s unavailable, falling back to RLE", method)
        method = "RLE"
        target_uid = str(RLELossless)

    if method != NATIVE_METHOD and not encoder_available(target_uid):
        raise CompressionError("No available encoder for RLE compression")

    try:
        if current_ts and UID(current_ts).is_compressed:
            ds.decompress(as_rgb=False, generate_instance_uid=False)
        if method == NATIVE_METHOD:
            # Decompression writes native pixels and preserves prior lossy flags.
            target_uid = str(ds.file_meta.TransferSyntaxUID)
        elif target_uid == str(HTJ2KLossless) and not _pydicom_htj2k_encoder():
            # pydicom 3.0.x: no HTJ2K encoder class — encode via imagecodecs.
            # _compress_htj2k only mutates `ds` after every frame encodes, so a
            # mid-encode failure leaves the dataset intact for an RLE retry.
            try:
                _compress_htj2k(ds)
            except Exception as exc:
                # Both encoders are lossless; retrying with RLE is safe, but
                # retain visibility without logging third-party error text.
                log.warning(
                    "HTJ2K encode failed (error_type=%s); falling back to lossless RLE | %s",
                    type(exc).__name__,
                    decode_context(ds),
                )
                method = "RLE"
                target_uid = str(RLELossless)
                ds.compress(UID(target_uid), generate_instance_uid=False)
        else:
            ds.compress(UID(target_uid), generate_instance_uid=False)
    except Exception as exc:  # e.g. missing decode handler for the source file
        # Do not log the path or exception text: codec errors can include source
        # values. The diagnostic helper emits only UIDs and pixel-layout data.
        log.error(
            "Lossless DICOM compression failed (method=%s, error_type=%s) | %s",
            method,
            type(exc).__name__,
            decode_context(ds),
        )
        raise CompressionError(
            f"Cannot compress DICOM instance using {method} "
            f"(error_type={type(exc).__name__})"
        ) from None

    # A unique sibling file stays on the same filesystem for atomic replace;
    # failed writes never overwrite the source and never leave a PHI temp file
    # silently. This also avoids two workers sharing a fixed '.tmp' filename.
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
        ds.save_as(str(tmp), enforce_file_format=True)
        os.replace(tmp, path)
    except Exception as exc:
        log.error("Compressed instance publish failed (error_type=%s) | %s",
                  type(exc).__name__, decode_context(ds))
        raise CompressionError(
            f"Cannot publish compressed DICOM instance (error_type={type(exc).__name__})"
        ) from None
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError as exc:
                log.error("Compression temporary file cleanup failed (error_type=%s)",
                          type(exc).__name__)

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
