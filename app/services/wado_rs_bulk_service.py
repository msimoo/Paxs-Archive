"""WADO-RS Bulk Data & Frame Retrieval Service (DICOMweb PS 3.18).

Extracts raw frame byte streams and bulk data elements (PixelData, OverlayData, WaveformData)
from DICOM instances for zero-footprint WebGL diagnostic viewers (CornerstoneJS, PaxsViewer) and AI pipelines.
"""

from __future__ import annotations

import io
import logging
from collections import OrderedDict

import numpy as np
from pydicom.dataset import Dataset

log = logging.getLogger("paxs.wado_bulk")

# Content-Type used when serving frames as raw (uncompressed) little-endian
# pixel bytes. Cornerstone's WADO-RS loader derives the decoder purely from the
# response Content-Type (getTransferSyntaxForContentType), so this routes the
# viewer to decodeLittleEndian — the pure-JS path that works in every browser —
# instead of the browser's openjpeg/libjpeg wasm (which has a known failure on
# large DX JPEG2000 codestreams, cornerstone3D #917).
RAW_FRAME_CONTENT_TYPE = "application/octet-stream; transfer-syntax=1.2.840.10008.1.2"

# Server-side decode cache: keyed by (SOPInstanceUID, frame index) so repeated
# viewer navigation / refreshes don't re-run the (expensive) pixel decode.
_FRAME_CACHE: OrderedDict[tuple[str, int], bytes] = OrderedDict()
_FRAME_CACHE_MAX = 16


def _cache_get(key: tuple[str, int]) -> bytes | None:
    val = _FRAME_CACHE.get(key)
    if val is not None:
        _FRAME_CACHE.move_to_end(key)
    return val


def _cache_put(key: tuple[str, int], value: bytes) -> None:
    _FRAME_CACHE[key] = value
    _FRAME_CACHE.move_to_end(key)
    while len(_FRAME_CACHE) > _FRAME_CACHE_MAX:
        _FRAME_CACHE.popitem(last=False)


def clear_frame_cache() -> None:
    """Drop the decoded-frame cache (used by tests / storage re-import)."""
    _FRAME_CACHE.clear()


def extract_frame_bytes(
    ds: Dataset, frame_indices: list[int]
) -> list[tuple[bytes, str]]:
    """Extract raw frame bytes and content-type for requested frame indices (1-indexed).

    Returns a list of (frame_bytes, content_type) tuples.
    """
    number_of_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    pixel_data = getattr(ds, "PixelData", None)

    if not pixel_data:
        raise ValueError("Dataset contains no PixelData")

    transfer_syntax = str(getattr(ds.file_meta, "TransferSyntaxUID", ""))

    frames = []

    is_compressed = bool(getattr(ds.file_meta, "TransferSyntaxUID", None) and getattr(ds.file_meta.TransferSyntaxUID, "is_compressed", False))

    # Every part's Content-Type must carry its real transfer-syntax (PS 3.18
    # WADO-RS): the viewer's loader picks the decoder (openjpeg / libjpeg-turbo
    # / charls wasm, or the raw little-endian path) from this parameter.
    ts_param = f"; transfer-syntax={transfer_syntax}" if transfer_syntax else ""

    # Case A: Encapsulated compressed frames (JPEG, JPEG-LS, JPEG2000, RLE)
    if is_compressed:
        try:
            from pydicom.encaps import generate_frames
            # pydicom 3.x: generate_frames() is a generator yielding one frame
            # at a time — materialize only the requested indices.
            all_frames = list(generate_frames(pixel_data, number_of_frames=number_of_frames))
            if ".4.90" in transfer_syntax or ".4.91" in transfer_syntax or ".4.201" in transfer_syntax or ".4.202" in transfer_syntax:
                mime = "image/jp2" + ts_param  # JPEG 2000 family
            elif ".4.50" in transfer_syntax or ".4.51" in transfer_syntax or ".4.57" in transfer_syntax or ".4.70" in transfer_syntax:
                mime = "image/jpeg" + ts_param  # JPEG family (incl. lossless Process 14)
            else:
                mime = "application/octet-stream" + ts_param  # RLE / other encapsulated
            for frame_idx in frame_indices:
                idx = frame_idx - 1
                if 0 <= idx < len(all_frames):
                    frames.append((all_frames[idx], mime))
            return frames
        except Exception:
            log.warning("Encapsulated frame extraction fallback to pixel_array unpacking")

    # Case B: Uncompressed raw pixel array
    try:
        arr = ds.pixel_array
        if arr.ndim == 2:  # Single 2D frame (Height, Width)
            frame_arr = arr
            frame_bytes = frame_arr.tobytes()
            frames.append((frame_bytes, "application/octet-stream" + ts_param))
        elif arr.ndim == 3:  # (Frames, Height, Width) or (Height, Width, Channels)
            if number_of_frames > 1:
                for frame_idx in frame_indices:
                    idx = frame_idx - 1
                    if 0 <= idx < arr.shape[0]:
                        frame_bytes = arr[idx].tobytes()
                        frames.append((frame_bytes, "application/octet-stream" + ts_param))
            else:
                # 2D RGB image (Height, Width, 3)
                frames.append((arr.tobytes(), "application/octet-stream" + ts_param))
        elif arr.ndim == 4:  # Multi-frame RGB (Frames, Height, Width, Channels)
            for frame_idx in frame_indices:
                idx = frame_idx - 1
                if 0 <= idx < arr.shape[0]:
                    frame_bytes = arr[idx].tobytes()
                    frames.append((frame_bytes, "application/octet-stream" + ts_param))
    except Exception as exc:
        log.exception("Failed to extract pixel array frames")
        raise ValueError(f"Failed to extract frame bytes: {exc}") from exc

    return frames


def decode_frames_to_raw(
    ds: Dataset, frame_indices: list[int]
) -> list[tuple[bytes, str]]:
    """Serve frames as raw uncompressed little-endian pixel bytes.

    Used when the client requests ``transfer-syntax=*`` (exactly what Cornerstone's
    WADO-RS loader sends): compressed codestreams (JPEG2000, JPEG Lossless, JPEG-LS,
    RLE) are decoded server-side with the installed pylibjpeg plugins and returned as
    ``application/octet-stream`` with an explicit uncompressed transfer-syntax.
    Uncompressed instances pass straight through. On any decode failure we fall back
    to the raw codestream so the request never hard-fails.
    """
    transfer_syntax = getattr(ds.file_meta, "TransferSyntaxUID", None)
    is_compressed = bool(
        transfer_syntax and getattr(transfer_syntax, "is_compressed", False)
    )

    if not is_compressed:
        frames = extract_frame_bytes(ds, frame_indices)
        return [(data, RAW_FRAME_CONTENT_TYPE) for data, _ct in frames]

    number_of_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    sop_uid = str(getattr(ds, "SOPInstanceUID", ""))

    # Serve from the decoded-frame cache / Redis FIRST — the per-request Dataset is
    # fresh, so the expensive ``ds.pixel_array`` (a 3072x2560 J2K decode takes
    # ~2.3s) must only run when at least one requested frame is missing.
    # Build the result in the requested order: cached frames are filled in
    # immediately, missing indices are decoded below and patched back in.
    from app.services.redis_service import cache_get, cache_set

    result: list[tuple[bytes, str] | None] = []
    missing: list[int] = []
    for frame_idx in frame_indices:
        cached = _cache_get((sop_uid, frame_idx))
        if cached is None:
            r_cached = cache_get(f"paxs:frame:{sop_uid}:{frame_idx}")
            if r_cached is not None:
                cached = r_cached
                _cache_put((sop_uid, frame_idx), cached)
        if cached is not None:
            result.append((cached, RAW_FRAME_CONTENT_TYPE))
        else:
            result.append(None)
            missing.append(frame_idx)

    if not missing:
        return [(data, ct) for data, ct in result if data is not None]

    try:
        arr = ds.pixel_array
    except Exception as exc:  # noqa: BLE001 - any decode failure falls back
        log.warning(
            "decode_frames_to_raw: pixel_array failed (%s); serving raw codestream",
            exc,
        )
        return extract_frame_bytes(ds, frame_indices)

    for frame_idx in missing:
        idx = frame_idx - 1
        frame_arr = arr
        if arr.ndim == 3 and number_of_frames > 1 and arr.shape[0] >= frame_idx:
            frame_arr = arr[idx]
        elif arr.ndim == 4 and arr.shape[0] >= frame_idx:
            frame_arr = arr[idx]
        elif arr.ndim >= 3 and number_of_frames > 1 and arr.shape[0] < frame_idx:
            raise ValueError(f"Frame {frame_idx} out of bounds")

        data = np.ascontiguousarray(frame_arr).tobytes()
        _cache_put((sop_uid, frame_idx), data)
        cache_set(f"paxs:frame:{sop_uid}:{frame_idx}", data, expire_seconds=3600)
        # Patch the first still-empty slot for this frame.
        pos = frame_indices.index(frame_idx)
        result[pos] = (data, RAW_FRAME_CONTENT_TYPE)

    return [(data, ct) for data, ct in result if data is not None]


def build_multipart_related_payload(
    frames: list[tuple[bytes, str]], boundary: str = "DICOMWEB_BOUNDARY"
) -> tuple[bytes, str]:
    """Format frame byte payloads as an HTTP multipart/related response body."""
    out = io.BytesIO()

    for frame_bytes, content_type in frames:
        out.write(f"--{boundary}\r\n".encode("ascii"))
        out.write(f"Content-Type: {content_type}\r\n".encode("ascii"))
        out.write(f"Content-Length: {len(frame_bytes)}\r\n\r\n".encode("ascii"))
        out.write(frame_bytes)
        out.write(b"\r\n")

    out.write(f"--{boundary}--\r\n".encode("ascii"))
    payload = out.getvalue()

    media_type = f'multipart/related; type="application/octet-stream"; boundary={boundary}'
    return payload, media_type


def extract_bulk_attribute(ds: Dataset, tag_path: str) -> tuple[bytes, str]:
    """Extract a specific DICOM bulk data attribute (e.g., PixelData, 7FE00010, OverlayData)."""
    tag_path_clean = tag_path.replace(",", "").replace(" ", "").upper()

    if tag_path_clean in ("PIXELDATA", "7FE00010"):
        val = getattr(ds, "PixelData", b"")
        return val, "application/octet-stream"

    if tag_path_clean in ("OVERLAYDATA", "60003000"):
        val = getattr(ds, "OverlayData", b"")
        return val, "application/octet-stream"

    # Generic element lookup by tag keyword or hex
    for elem in ds:
        elem_tag_hex = f"{elem.tag.group:04X}{elem.tag.element:04X}"
        if elem.keyword.upper() == tag_path_clean or elem_tag_hex == tag_path_clean:
            val = elem.value
            if isinstance(val, bytes):
                return val, "application/octet-stream"
            return str(val).encode("utf-8"), "text/plain"

    raise KeyError(f"Bulk data attribute '{tag_path}' not found in dataset")
