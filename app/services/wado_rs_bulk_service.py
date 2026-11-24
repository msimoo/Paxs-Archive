"""WADO-RS Bulk Data & Frame Retrieval Service (DICOMweb PS 3.18).

Extracts raw frame byte streams and bulk data elements (PixelData, OverlayData, WaveformData)
from DICOM instances for zero-footprint WebGL diagnostic viewers (CornerstoneJS, PaxsViewer) and AI pipelines.
"""

from __future__ import annotations

import io
import logging
import threading
from collections import OrderedDict

import numpy as np
from pydicom.dataset import Dataset

from app.core import cache_stats
from app.core.config import settings
from app.core.dicom_diagnostics import decode_context

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
#
# V1-2: bounded by BYTES (settings.DICOM_FRAME_CACHE_MB), not entry count. The
# old 16-entry cap was ~8 MB for 512x512 CT frames, so a 200-image study scrubbed
# across three planes evicted continuously and re-decoded on nearly every revisit
# (a 3072x2560 J2K frame costs ~2.3 s). A byte budget scales with image size
# instead of assuming small frames.
#
# Locked: the warm service decodes on a background thread while request handlers
# decode on the event loop, and byte accounting is a read-modify-write.
_FRAME_CACHE: OrderedDict[tuple[str, str, int], bytes] = OrderedDict()
_FRAME_CACHE_LOCK = threading.Lock()


def frame_cache_budget_bytes() -> int:
    """Current budget from settings (re-read each call so an override applies live).

    ``0`` means **caching disabled** and is returned as 0, not masked with a
    default — an ``or`` fallback would silently turn a deliberate disable into
    the full default budget.
    """
    raw = getattr(settings, "DICOM_FRAME_CACHE_MB", 256)
    try:
        mb = int(raw)
    except (TypeError, ValueError) as exc:
        # An unparseable override falls back to the documented 256 MB budget;
        # 0 stays 0 below, so a deliberate disable is never masked.
        log.debug("frame_cache_budget_invalid error_type=%s", type(exc).__name__)
        mb = 256
    return max(0, mb) * 1024 * 1024


def frame_cache_stats() -> dict[str, int]:
    """Counters + gauges for this cache (used by tests and /metrics)."""
    return cache_stats.snapshot()["frame"]


def _cache_get(key: tuple[str, str, int, int]) -> bytes | None:
    with _FRAME_CACHE_LOCK:
        val = _FRAME_CACHE.get(key)
        if val is None:
            cache_stats.incr("frame", "misses")
            return None
        _FRAME_CACHE.move_to_end(key)
        cache_stats.incr("frame", "hits")
        return val


def _cache_put(key: tuple[str, str, int, int], value: bytes) -> None:
    budget = frame_cache_budget_bytes()
    if budget <= 0:
        # Caching disabled (DICOM_FRAME_CACHE_MB=0): store nothing rather than
        # retaining one oversized entry.
        cache_stats.set_gauges("frame", bytes=0, entries=0)
        return
    with _FRAME_CACHE_LOCK:
        _FRAME_CACHE.pop(key, None)
        _FRAME_CACHE[key] = value
        cache_stats.incr("frame", "inserts")
        # Recompute from the dict rather than tracking a delta: another module
        # (or a test) may clear the cache directly, and a stale running total
        # would then evict everything or nothing.
        total = sum(len(v) for v in _FRAME_CACHE.values())
        # Never evict the entry just inserted: a single frame larger than the
        # whole budget would otherwise be dropped immediately and the cache would
        # do nothing at all.
        while len(_FRAME_CACHE) > 1 and total > budget:
            _evicted_key, evicted = _FRAME_CACHE.popitem(last=False)
            total -= len(evicted)
            cache_stats.incr("frame", "evictions")
        cache_stats.set_gauges("frame", bytes=total, entries=len(_FRAME_CACHE))


def clear_frame_cache() -> None:
    """Drop the decoded-frame cache (used by tests / storage re-import)."""
    with _FRAME_CACHE_LOCK:
        _FRAME_CACHE.clear()
        cache_stats.set_gauges("frame", bytes=0, entries=0)


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
        except Exception as exc:  # noqa: BLE001 - boundary guard: fall back to pixel_array
            # Not necessarily an error, but the reason matters: this is the
            # first place a transfer syntax the encapsulation layer cannot walk
            # shows up, so record the syntax and the cause together.
            log.warning(
                "encapsulated_frame_extraction_failed error_type=%s; "
                "fallback=pixel_array | %s",
                type(exc).__name__, decode_context(ds),
            )

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
        log.error(
            "pixel_array_frame_decode_failed error_type=%s | %s",
            type(exc).__name__, decode_context(ds),
        )
        raise ValueError(f"Failed to extract frame bytes: {exc}") from exc

    return frames


def decode_frames_to_raw(
    ds: Dataset, frame_indices: list[int], tenant_id: int | None = None,
    content_version: int = 1,
) -> list[tuple[bytes, str]]:
    """Serve frames as raw uncompressed little-endian pixel bytes.

    Used when the client requests ``transfer-syntax=*`` (exactly what Cornerstone's
    WADO-RS loader sends): compressed codestreams (JPEG2000, JPEG Lossless, JPEG-LS,
    RLE) are decoded server-side with the installed pylibjpeg plugins and returned as
    ``application/octet-stream`` with an explicit uncompressed transfer-syntax.
    Uncompressed instances pass straight through. On any decode failure we fall back
    to the raw codestream so the request never hard-fails.

    ``content_version`` (C-04) is the instance's stored-object version; it is part
    of every cache key, so frames decoded before a rewrite are never served after.
    """
    from app.services import content_version as cv

    transfer_syntax = getattr(ds.file_meta, "TransferSyntaxUID", None)
    is_compressed = bool(
        transfer_syntax and getattr(transfer_syntax, "is_compressed", False)
    )

    if not is_compressed:
        frames = extract_frame_bytes(ds, frame_indices)
        return [(data, RAW_FRAME_CONTENT_TYPE) for data, _ct in frames]

    number_of_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    sop_uid = str(getattr(ds, "SOPInstanceUID", ""))
    tenant_key = str(tenant_id) if tenant_id is not None else ""

    # Serve from the decoded-frame cache / Redis FIRST — the per-request Dataset is
    # fresh, so the expensive ``ds.pixel_array`` (a 3072x2560 J2K decode takes
    # ~2.3s) must only run when at least one requested frame is missing.
    # Build the result in the requested order: cached frames are filled in
    # immediately, missing indices are decoded below and patched back in.
    from app.services.redis_service import cache_get, cache_set

    result: list[tuple[bytes, str] | None] = []
    missing: list[int] = []
    for frame_idx in frame_indices:
        cache_key = (tenant_key, sop_uid, content_version, frame_idx)
        redis_key = cv.raw_frame_key(tenant_id, sop_uid, content_version, frame_idx)
        cached = _cache_get(cache_key)
        if cached is None:
            r_cached = cache_get(redis_key)
            if r_cached is not None:
                cached = r_cached
                _cache_put(cache_key, cached)
        if cached is not None:
            result.append((cached, RAW_FRAME_CONTENT_TYPE))
        else:
            result.append(None)
            missing.append(frame_idx)

    if not missing:
        return [(data, ct) for data, ct in result if data is not None]

    try:
        arr = ds.pixel_array
    except Exception as exc:  # noqa: BLE001 - boundary guard: any decode failure falls back
        # The primary codec-diagnosis site: an installed pylibjpeg plugin is
        # missing or cannot handle this syntax. The context names the syntax and
        # the pixel layout, so the gap is identifiable without opening the file.
        log.warning(
            "decode_frames_to_raw: pixel_array failed (%s: %s); "
            "serving raw codestream | %s",
            type(exc).__name__, exc, decode_context(ds),
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
        _cache_put((tenant_key, sop_uid, content_version, frame_idx), data)
        redis_key = cv.raw_frame_key(tenant_id, sop_uid, content_version, frame_idx)
        cache_set(redis_key, data, expire_seconds=3600)
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


#: V-11: MPEG transfer syntaxes whose PixelData is one encapsulated video stream.
VIDEO_TRANSFER_SYNTAX_MEDIA_TYPES = {
    "1.2.840.10008.1.2.4.100": "video/mpeg",   # MPEG2 Main Profile / Main Level
    "1.2.840.10008.1.2.4.101": "video/mpeg",   # MPEG2 Main Profile / High Level
    "1.2.840.10008.1.2.4.102": "video/mp4",    # MPEG-4 AVC/H.264 High Profile
    "1.2.840.10008.1.2.4.103": "video/mp4",    # MPEG-4 AVC/H.264 BD-compatible
    "1.2.840.10008.1.2.4.104": "video/mp4",    # MPEG-4 AVC/H.264 2D video
    "1.2.840.10008.1.2.4.105": "video/mp4",    # MPEG-4 AVC/H.264 3D video
    "1.2.840.10008.1.2.4.106": "video/mp4",    # MPEG-4 AVC/H.264 Stereo
    "1.2.840.10008.1.2.4.107": "video/mp4",    # HEVC/H.265 Main Profile
    "1.2.840.10008.1.2.4.108": "video/mp4",    # HEVC/H.265 Main 10 Profile
}

#: V-11: encapsulated-document media types served under their own type. Only
#: PDF: an archived CDA/XML or anything else served inline could execute script
#: in the archive's origin, so every other type is an opaque download.
SAFE_DOCUMENT_MEDIA_TYPES = {"application/pdf"}


def encapsulated_document(ds: Dataset) -> tuple[bytes, str]:
    """Bytes of (0042,0011) with a safe media type (V-11, PS3.3 C.24.2).

    ``MIMETypeOfEncapsulatedDocument`` is data from the sender, never trusted
    as-is: PDF is served as ``application/pdf`` so the viewer's PDF viewport can
    show it; anything else is ``application/octet-stream``. A trailing pad byte
    after ``%%EOF`` is harmless to PDF readers and is kept.
    """
    document = getattr(ds, "EncapsulatedDocument", None)
    if document is None:
        raise KeyError("Bulk data attribute 'EncapsulatedDocument' not found in dataset")
    declared = str(getattr(ds, "MIMETypeOfEncapsulatedDocument", "") or "").strip().lower()
    if not declared and bytes(document[:5]) == b"%PDF-":
        declared = "application/pdf"
    media_type = declared if declared in SAFE_DOCUMENT_MEDIA_TYPES else "application/octet-stream"
    return bytes(document), media_type


def video_stream(ds: Dataset) -> tuple[bytes, str] | None:
    """The playable MPEG stream inside an MPEG-encapsulated PixelData, else None (V-11).

    An MPEG object's PixelData is the whole video split across encapsulation
    fragments (PS3.5 A.4.5-A.4.7); returning the raw element would hand the
    browser item tags and the offset table instead of a video.
    """
    transfer_syntax = str(getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", "") or "")
    media_type = VIDEO_TRANSFER_SYNTAX_MEDIA_TYPES.get(transfer_syntax)
    pixel_data = getattr(ds, "PixelData", None)
    if media_type is None or not pixel_data:
        return None
    from pydicom.encaps import generate_frames

    # The whole video is one "frame" of fragments; generate_frames skips the
    # Basic Offset Table item that generate_fragments would yield first.
    return b"".join(generate_frames(pixel_data, number_of_frames=1)), media_type


def extract_bulk_attribute(ds: Dataset, tag_path: str) -> tuple[bytes, str]:
    """Extract a specific DICOM bulk data attribute (e.g., PixelData, 7FE00010, OverlayData)."""
    tag_path_clean = tag_path.replace(",", "").replace(" ", "").upper()

    if tag_path_clean in ("ENCAPSULATEDDOCUMENT", "00420011"):
        return encapsulated_document(ds)

    if tag_path_clean in ("PIXELDATA", "7FE00010"):
        video = video_stream(ds)
        if video is not None:
            return video
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
