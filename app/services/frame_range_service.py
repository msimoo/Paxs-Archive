"""P8: bounded indexes for byte-identical, per-frame DICOM range reads."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import os
from pathlib import Path
import struct
import sys
import threading

import pydicom
from pydicom.uid import DeflatedExplicitVRLittleEndian


@dataclass(frozen=True, slots=True)
class FrameByteRange:
    """One contiguous fragment payload in the stored DICOM object."""

    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class FrameOffsetIndex:
    """File identity and byte ranges for one multi-frame DICOM instance."""

    number_of_frames: int
    transfer_syntax: str
    content_type: str
    source_size: int
    source_mtime_ns: int
    native_pixel_start: int | None = None
    native_frame_length: int = 0
    encapsulated_ranges: tuple[tuple[FrameByteRange, ...], ...] = ()

    @property
    def cache_weight(self) -> int:
        """Conservative estimate of retained Python index objects in bytes."""
        return sys.getsizeof(self) + sum(
            sys.getsizeof(frame) + sum(sys.getsizeof(span) for span in frame)
            for frame in self.encapsulated_ranges
        )


_FRAME_INDEX_CACHE: OrderedDict[tuple[int, str, int, int], FrameOffsetIndex] = OrderedDict()
_FRAME_INDEX_CACHE_BYTES = 0
_FRAME_INDEX_LOCK = threading.Lock()
# Files (by identity) whose layout cannot be sliced safely — bounded, no PHI.
_UNINDEXABLE: OrderedDict[tuple[int, str, int, int], None] = OrderedDict()
_UNINDEXABLE_MAX = 65536
_LONG_VR = {b"OB", b"OD", b"OF", b"OL", b"OV", b"OW", b"SQ", b"UC", b"UR", b"UT", b"UN"}
_INDEX_TAGS = [
    "NumberOfFrames", "Rows", "Columns", "SamplesPerPixel", "PhotometricInterpretation",
    "BitsAllocated", "BitsStored", "PixelRepresentation", "PlanarConfiguration",
]


def clear_frame_index_cache() -> None:
    """Clear the process-local index cache for tests and storage replacement."""
    global _FRAME_INDEX_CACHE_BYTES
    with _FRAME_INDEX_LOCK:
        _FRAME_INDEX_CACHE.clear()
        _UNINDEXABLE.clear()
        _FRAME_INDEX_CACHE_BYTES = 0


def get_frame_offset_index(
    path: str | os.PathLike[str], tenant_id: int | None, cache_budget_bytes: int,
    max_fragments: int,
) -> FrameOffsetIndex | None:
    """Return a tenant-scoped index, or ``None`` when layout is not safe to slice."""
    try:
        source = Path(path).resolve(strict=True)
        stat = source.stat()
    except OSError:
        return None
    key = (int(tenant_id or 0), os.fspath(source), stat.st_size, stat.st_mtime_ns)
    with _FRAME_INDEX_LOCK:
        cached = _FRAME_INDEX_CACHE.get(key)
        if cached is not None:
            _FRAME_INDEX_CACHE.move_to_end(key)
            return cached
        if key in _UNINDEXABLE:
            # Same file identity (size + mtime) was already found unsafe to
            # slice; skip the header parse and go straight to the full read.
            return None

    index = _build_frame_offset_index(source, stat.st_size, stat.st_mtime_ns, max_fragments)
    if index is None:
        with _FRAME_INDEX_LOCK:
            _UNINDEXABLE[key] = None
            while len(_UNINDEXABLE) > _UNINDEXABLE_MAX:
                _UNINDEXABLE.popitem(last=False)
    elif cache_budget_bytes > 0 and index.cache_weight <= cache_budget_bytes:
        _cache_index(key, index, cache_budget_bytes)
    return index


def _cache_index(
    key: tuple[int, str, int, int], index: FrameOffsetIndex, budget: int
) -> None:
    global _FRAME_INDEX_CACHE_BYTES
    with _FRAME_INDEX_LOCK:
        previous = _FRAME_INDEX_CACHE.pop(key, None)
        if previous is not None:
            _FRAME_INDEX_CACHE_BYTES -= previous.cache_weight
        _FRAME_INDEX_CACHE[key] = index
        _FRAME_INDEX_CACHE_BYTES += index.cache_weight
        while _FRAME_INDEX_CACHE_BYTES > budget:
            _old_key, old_index = _FRAME_INDEX_CACHE.popitem(last=False)
            _FRAME_INDEX_CACHE_BYTES -= old_index.cache_weight


def read_indexed_frames(
    path: str | os.PathLike[str], index: FrameOffsetIndex, frame_indices: list[int]
) -> list[tuple[bytes, str]] | None:
    """Read selected frame payloads only; return ``None`` if file identity changed."""
    source = Path(path)
    result: list[tuple[bytes, str]] = []
    try:
        with source.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            if stat.st_size != index.source_size or stat.st_mtime_ns != index.source_mtime_ns:
                return None
            for frame_number in frame_indices:
                if frame_number < 1 or frame_number > index.number_of_frames:
                    raise ValueError("requested frame is out of range")
                spans = _frame_ranges(index, frame_number)
                frame = _read_ranges(stream, spans)
                if frame is None:
                    return None
                result.append((frame, index.content_type))
    except OSError:
        return None
    return result


def _frame_ranges(index: FrameOffsetIndex, frame_number: int) -> tuple[FrameByteRange, ...]:
    if index.native_pixel_start is not None:
        offset = index.native_pixel_start + (frame_number - 1) * index.native_frame_length
        return (FrameByteRange(offset, index.native_frame_length),)
    return index.encapsulated_ranges[frame_number - 1]


def _read_ranges(stream, ranges: tuple[FrameByteRange, ...]) -> bytes | None:
    output = bytearray()
    for span in ranges:
        stream.seek(span.offset)
        chunk = stream.read(span.length)
        if len(chunk) != span.length:
            return None
        output.extend(chunk)
    return bytes(output)


def _build_frame_offset_index(
    path: Path, source_size: int, source_mtime_ns: int, max_fragments: int,
) -> FrameOffsetIndex | None:
    try:
        with path.open("rb") as stream:
            dataset = pydicom.dcmread(stream, stop_before_pixels=True, specific_tags=_INDEX_TAGS)
            # S2 (viewer speed plan): single-frame instances — one file per CT
            # slice, the common case — are indexed too. They used to return
            # None here, so every /frames/1 parsed the header for nothing and
            # then re-read and re-parsed the whole file.
            number_of_frames = int(getattr(dataset, "NumberOfFrames", 1) or 1)
            if number_of_frames < 1:
                return None
            transfer_syntax = getattr(dataset.file_meta, "TransferSyntaxUID", None)
            if transfer_syntax is None or str(transfer_syntax) == str(DeflatedExplicitVRLittleEndian):
                return None
            header = _pixel_data_header(stream, dataset)
            if header is None:
                return None
            value_offset, value_length = header
            syntax = str(transfer_syntax)
            if bool(getattr(transfer_syntax, "is_compressed", False)):
                ranges = _encapsulated_ranges(
                    stream, value_offset, value_length, number_of_frames,
                    source_size, max_fragments,
                )
                if ranges is None:
                    return None
                return FrameOffsetIndex(
                    number_of_frames, syntax, _compressed_content_type(syntax),
                    source_size, source_mtime_ns, encapsulated_ranges=ranges,
                )
            frame_length = _native_frame_length(dataset)
            expected_length = number_of_frames * frame_length
            # Pixel Data value has one even-length pad byte only when total pixel bytes are odd.
            if frame_length <= 0 or value_length != expected_length + (expected_length & 1):
                return None
            return FrameOffsetIndex(
                number_of_frames, syntax,
                f"application/octet-stream; transfer-syntax={syntax}",
                source_size, source_mtime_ns,
                native_pixel_start=value_offset, native_frame_length=frame_length,
            )
    except (OSError, EOFError, ValueError, TypeError, struct.error):
        return None


def _native_frame_length(dataset) -> int:
    if not bool(getattr(dataset, "is_little_endian", False)):
        return 0
    if int(getattr(dataset, "SamplesPerPixel", 1) or 1) != 1:
        return 0
    if str(getattr(dataset, "PhotometricInterpretation", "")) not in ("MONOCHROME1", "MONOCHROME2"):
        return 0
    bits_allocated = int(getattr(dataset, "BitsAllocated", 0) or 0)
    if bits_allocated not in (8, 16, 32, 64):
        return 0
    if int(getattr(dataset, "BitsStored", 0) or 0) != bits_allocated:
        return 0
    rows = int(getattr(dataset, "Rows", 0) or 0)
    columns = int(getattr(dataset, "Columns", 0) or 0)
    return rows * columns * (bits_allocated // 8)


def _pixel_data_header(stream, dataset) -> tuple[int, int] | None:
    element_offset = stream.tell()
    header = stream.read(8)
    if len(header) != 8 or struct.unpack("<HH", header[:4]) != (0x7FE0, 0x0010):
        return None
    if bool(getattr(dataset, "is_implicit_VR", False)):
        value_length = struct.unpack("<I", header[4:8])[0]
        return element_offset + 8, value_length
    vr = header[4:6]
    if vr in _LONG_VR:
        extra = stream.read(4)
        if len(extra) != 4 or header[6:8] != b"\0\0":
            return None
        value_length = struct.unpack("<I", extra)[0]
        return element_offset + 12, value_length
    return element_offset + 8, struct.unpack("<H", header[6:8])[0]


def _encapsulated_ranges(
    stream, value_offset: int, value_length: int, number_of_frames: int,
    source_size: int, max_fragments: int,
) -> tuple[tuple[FrameByteRange, ...], ...] | None:
    if value_length != 0xFFFFFFFF:
        return None
    stream.seek(value_offset)
    bot_header = stream.read(8)
    if len(bot_header) != 8 or struct.unpack("<HH", bot_header[:4]) != (0xFFFE, 0xE000):
        return None
    bot_length = struct.unpack("<I", bot_header[4:8])[0]
    if bot_length % 4 or bot_length > number_of_frames * 4:
        return None
    bot_data = stream.read(bot_length)
    if len(bot_data) != bot_length:
        return None
    offsets = tuple(struct.unpack(f"<{bot_length // 4}I", bot_data)) if bot_length else ()
    first_fragment_offset = stream.tell()
    fragments: list[tuple[int, FrameByteRange]] = []
    while stream.tell() + 8 <= source_size:
        item_offset = stream.tell()
        item = stream.read(8)
        if len(item) != 8:
            return None
        group, element = struct.unpack("<HH", item[:4])
        item_length = struct.unpack("<I", item[4:8])[0]
        if (group, element) == (0xFFFE, 0xE0DD):
            if item_length != 0:
                return None
            return _group_fragments(offsets, fragments, number_of_frames)
        if (group, element) != (0xFFFE, 0xE000) or item_length in (0, 0xFFFFFFFF):
            return None
        if len(fragments) >= max_fragments:
            return None
        data_offset = stream.tell()
        if data_offset + item_length > source_size:
            return None
        fragments.append((item_offset - first_fragment_offset, FrameByteRange(data_offset, item_length)))
        stream.seek(item_length, os.SEEK_CUR)
    return None


def _group_fragments(
    offsets: tuple[int, ...], fragments: list[tuple[int, FrameByteRange]], number_of_frames: int,
) -> tuple[tuple[FrameByteRange, ...], ...] | None:
    if not fragments:
        return None
    if offsets:
        positions = {offset: index for index, (offset, _span) in enumerate(fragments)}
        starts = [positions.get(offset) for offset in offsets]
        if len(offsets) != number_of_frames or starts[0] != 0 or any(
            start is None or (position and start <= starts[position - 1])
            for position, start in enumerate(starts)
        ):
            return None
        boundaries = [int(start) for start in starts] + [len(fragments)]
    elif len(fragments) == number_of_frames:
        boundaries = list(range(number_of_frames + 1))
    elif number_of_frames == 1:
        boundaries = [0, len(fragments)]
    else:
        return None
    grouped = tuple(
        tuple(span for _offset, span in fragments[boundaries[i]:boundaries[i + 1]])
        for i in range(number_of_frames)
    )
    return grouped if all(group for group in grouped) else None


def _compressed_content_type(transfer_syntax: str) -> str:
    if any(suffix in transfer_syntax for suffix in (".4.90", ".4.91", ".4.201", ".4.202")):
        mime = "image/jp2"
    elif any(suffix in transfer_syntax for suffix in (".4.50", ".4.51", ".4.57", ".4.70")):
        mime = "image/jpeg"
    else:
        mime = "application/octet-stream"
    return f"{mime}; transfer-syntax={transfer_syntax}"
