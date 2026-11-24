"""Helpers for the DICOMweb endpoints: DICOM JSON (PS3.18 F), multipart
encoding/parsing and single-frame extraction.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import hashlib
import io
import json
import os
import re
import uuid
from dataclasses import dataclass
from email import policy

import pydicom
from pydicom.dataset import Dataset


# --------------------------------------------------------------------------- #
# DICOM JSON (application/dicom+json)
# --------------------------------------------------------------------------- #
_PIXEL_BULK_DATA_TAGS = frozenset({0x7FE00008, 0x7FE00009, 0x7FE00010})


def _pn(value: str | None) -> dict | None:
    return {"Alphabetic": value} if value else None


def _put(data: dict, tag: str, vr: str, value) -> None:
    """Emit one DICOM JSON element.

    List values (e.g. ModalitiesInStudy) are emitted as one Value entry per
    element — ``["CT"]``, never the Python repr ``["['CT']"]`` — which is what
    DICOMweb clients (PaxsViewer/PAXS qido.js) expect when they join/split
    on ``\\``.
    """
    if value is None or value == "":
        return
    if isinstance(value, (list, tuple)):
        values = [v for v in value if v is not None and v != ""]
        if not values:
            return
        if vr == "PN":
            data[tag] = {"vr": vr, "Value": [_pn(str(v)) for v in values]}
        else:
            data[tag] = {"vr": vr, "Value": [str(v) for v in values]}
        return
    if vr == "PN":
        data[tag] = {"vr": vr, "Value": [_pn(str(value))]}
    elif vr in ("IS", "US", "UL", "SS", "SL", "DS"):
        data[tag] = {"vr": vr, "Value": [value]}
    else:
        data[tag] = {"vr": vr, "Value": [str(value)]}


def study_to_dicom_json(study, patient, modalities: list[str] | None = None,
                        n_series: int | None = None, n_instances: int | None = None) -> dict:
    data: dict = {}
    _put(data, "00100010", "PN", getattr(patient, "patient_name", None))
    _put(data, "00100020", "LO", getattr(patient, "patient_id", None))
    _put(data, "00100030", "DA", getattr(patient, "patient_birth_date", None))
    _put(data, "00100040", "CS", getattr(patient, "patient_sex", None))
    _put(data, "00080020", "DA", study.study_date)
    _put(data, "00080030", "TM", study.study_time)
    _put(data, "00080050", "SH", study.accession_number)
    _put(data, "00080090", "PN", study.referring_physician)
    _put(data, "00081030", "LO", study.study_description)
    _put(data, "00080061", "CS", modalities)
    _put(data, "0020000D", "UI", study.study_instance_uid)
    _put(data, "00200010", "SH", study.study_id)
    _put(data, "00201206", "IS", n_series)
    _put(data, "00201208", "IS", n_instances)
    return data


def series_to_dicom_json(series, n_instances: int | None = None) -> dict:
    data: dict = {}
    _put(data, "00080060", "CS", series.modality)
    _put(data, "0008103E", "LO", series.series_description)
    _put(data, "0020000E", "UI", series.series_instance_uid)
    _put(data, "00200011", "IS", series.series_number)
    _put(data, "00180015", "CS", series.body_part_examined)
    _put(data, "00201209", "IS", n_instances)
    return data


def instance_to_dicom_json(instance, identity: tuple[str, str, str | None] | None = None) -> dict:
    """DICOM JSON from the indexed columns only (the object could not be read).

    ``identity`` is (StudyInstanceUID, SeriesInstanceUID, Modality) from the
    instance's series/study rows. Without it a viewer cannot even address the
    image (OHIF builds ``studies/undefined/series/undefined`` imageIds and never
    requests a frame, so the frame's 404 ``object-missing`` is never shown).
    """
    data: dict = {}
    if identity is not None:
        _put(data, "0020000D", "UI", identity[0])
        _put(data, "0020000E", "UI", identity[1])
        _put(data, "00080060", "CS", identity[2])
    _put(data, "00080016", "UI", instance.sop_class_uid)
    _put(data, "00080018", "UI", instance.sop_instance_uid)
    _put(data, "00200013", "IS", instance.instance_number)
    _put(data, "00280004", "CS", instance.photometric_interpretation)
    _put(data, "00280010", "US", instance.rows)
    _put(data, "00280011", "US", instance.columns)
    _put(data, "00280100", "US", instance.bits_allocated)
    _put(data, "00020010", "UI", instance.transfer_syntax_uid)
    return data


def dataset_to_dicom_json(ds: Dataset) -> dict:
    """Return WADO-RS metadata without inlining top-level pixel bulk data.

    Pixel frames are retrieved through the existing WADO-RS frame resources;
    serializing them here would duplicate the volume inside the JSON response.
    A fresh header dataset lets us skip encoding the bulk elements without
    copying large pixel buffers or changing the caller's source dataset.
    """
    metadata_ds = Dataset()
    metadata_ds.file_meta = ds.file_meta
    for tag in ds.keys():
        if tag not in _PIXEL_BULK_DATA_TAGS:
            metadata_ds.add(ds[tag])
    try:
        raw = metadata_ds.to_json_dict()
    except AttributeError:  # pragma: no cover - very old pydicom
        raise
    # DICOMweb uses uppercase hex tags; normalize just in case.
    data = {str(k).upper(): v for k, v in raw.items()}
    # to_json_dict() excludes the file meta group, but DICOMweb clients (PaxsViewer
    # image loader) need TransferSyntaxUID (0002,0010) to pick the right
    # decoder (e.g. JPEG Lossless vs baseline JPEG) before fetching frames.
    if "00020010" not in data and metadata_ds.file_meta is not None:
        ts_uid = getattr(metadata_ds.file_meta, "TransferSyntaxUID", None)
        if ts_uid:
            data["00020010"] = {"vr": "UI", "Value": [str(ts_uid)]}
    return data


def dataset_to_json_text(ds: Dataset) -> str:
    """One compact DICOM JSON object — the form stored in ``instances.metadata_json``.

    Compact separators and no ASCII escaping because the study-level /metadata
    response is these strings concatenated: a 4,000-instance study is ~48 MB at
    default spacing and the browser parses every byte of it. Storing the text
    (rather than a parsed dict) also means the endpoint can answer with a join —
    no per-instance ``json.loads`` and no re-serialisation.
    """
    return json.dumps(dataset_to_dicom_json(ds), separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Multipart (STOW-RS parse / WADO-RS build)
# --------------------------------------------------------------------------- #
def parse_multipart(body: bytes, content_type: str) -> list[tuple[str, bytes]]:
    """Parse a multipart/related body into [(content_type, payload), ...]."""
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
    msg = email.message_from_bytes(header.encode("latin-1", "replace") + body, policy=policy.default)
    if not msg.is_multipart():
        return [("application/octet-stream", body)]
    parts: list[tuple[str, bytes]] = []
    for part in msg.iter_parts():
        payload = part.get_payload(decode=True) or b""
        parts.append((part.get_content_type(), payload))
    return parts


class StowPartTooLarge(Exception):
    """Raised when a single STOW-RS part (or the whole body) exceeds its cap."""


class MalformedStowMultipart(Exception):
    """Raised when a multipart envelope or part ends before its delimiter."""


async def iter_stow_parts(stream, content_type: str,
                          max_part_bytes: int = 0,
                          max_total_bytes: int = 0):
    """Yield ``(content_type, payload_bytes)`` one multipart part at a time.

    Consumes ``stream`` (an async iterable of bytes, e.g. ``request.stream()``)
    incrementally: only the *current* part is buffered in memory, so a large
    multi-part upload never holds the whole body at once. Headers are parsed
    per part; the payload of each part is accumulated up to ``max_part_bytes``
    (0 = unlimited) and the running total up to ``max_total_bytes``
    (0 = unlimited) — exceeding either raises :class:`StowPartTooLarge` (the
    caller maps it to HTTP 413). A body without a boundary is treated as a
    single ``application/octet-stream`` part.
    """
    import re

    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    boundary = (m.group(1) or m.group(2) or "").strip() if m else ""
    if not boundary:
        # Not multipart — treat the whole body as one part.
        payload = bytearray()
        async for chunk in stream:
            payload.extend(chunk)
            if max_part_bytes and len(payload) > max_part_bytes:
                raise StowPartTooLarge(
                    f"Part exceeds {max_part_bytes} byte limit")
        yield ("application/octet-stream", bytes(payload))
        return

    marker = f"--{boundary}".encode("latin-1")
    buf = bytearray()
    total = 0

    async def _pull() -> bool:
        """Pull exactly one chunk into the buffer. False only at EOF.

        Every caller loop must call this once per iteration so progress is
        guaranteed (an empty/anew chunk can't spin the loop).
        """
        try:
            chunk = await anext(stream)
        except StopAsyncIteration:
            return False
        if chunk:
            buf.extend(chunk)
        return True

    # Skip the preamble up to the first boundary marker.
    while buf.find(marker) == -1:
        if not await _pull():
            return
    del buf[:buf.find(marker) + len(marker)]

    while True:
        # After a boundary marker: either the closing "--" or CRLF + headers.
        while len(buf) < 2 and await _pull():
            pass
        if not buf:
            return
        if buf.startswith(b"--"):
            return
        if not buf.startswith(b"\r\n"):
            raise StowPartTooLarge("Malformed multipart: expected CRLF after boundary")
        del buf[:2]

        # Part headers up to the blank line (\r\n\r\n).
        while True:
            idx = buf.find(b"\r\n\r\n")
            if idx != -1:
                break
            if not await _pull():
                raise StowPartTooLarge("Malformed multipart: unterminated part headers")
        header_block = bytes(buf[:idx])
        del buf[:idx + 4]

        ctype = "application/octet-stream"
        for line in header_block.split(b"\r\n"):
            if line.lower().startswith(b"content-type:"):
                ctype = line.split(b":", 1)[1].strip().decode("latin-1", "replace")
                break

        # Payload up to the next boundary marker (CRLF + marker). Everything
        # but a marker-length tail moves into the payload immediately, so only
        # a marker-sized window is ever held back while waiting for more data.
        payload = bytearray()
        next_sep = b"\r\n" + marker
        while True:
            idx = buf.find(next_sep)
            if idx != -1:
                payload.extend(buf[:idx])
                del buf[:idx + len(next_sep)]
                break
            # Hold back a full next_sep window (not just the marker) so a
            # separator split across chunks is never half-moved into the
            # payload; the rest of the buffer joins the payload immediately.
            keep = len(next_sep)
            if len(buf) > keep:
                payload.extend(buf[:-keep])
                del buf[:-keep]
            if max_part_bytes and len(payload) > max_part_bytes:
                raise StowPartTooLarge(
                    f"Part exceeds {max_part_bytes} byte limit")
            if not await _pull():
                raise MalformedStowMultipart(
                    "Multipart body ended before the next boundary"
                )

        total += len(payload)
        if max_part_bytes and len(payload) > max_part_bytes:
            raise StowPartTooLarge(
                f"Part exceeds {max_part_bytes} byte limit")
        if max_total_bytes and total > max_total_bytes:
            raise StowPartTooLarge(
                f"Request body exceeds {max_total_bytes} byte limit")
        yield (ctype, bytes(payload))


# --------------------------------------------------------------------------- #
# Spooled STOW-RS parsing (C-10)
# --------------------------------------------------------------------------- #
@dataclass
class SpooledPart:
    """A multipart part written to a temp file instead of held in memory.

    ``size`` and ``sha256`` are measured **while spooling** (incrementally, over
    the incoming chunks), so the ingest never has to read the part back to learn
    how big it is or what it hashes to — the two facts the stage-then-publish
    contract and the quota reservation both need.
    """

    content_type: str
    path: str
    size: int
    sha256: str

    def cleanup(self) -> None:
        """Remove the spooled file (idempotent; never raises)."""
        with contextlib.suppress(OSError):
            os.unlink(self.path)


def _new_spool_path(spool_dir: str) -> str:
    """A fresh ``.tmp_*`` path inside ``spool_dir`` (matches the sweeper prefix)."""
    return os.path.join(spool_dir, f".tmp_{uuid.uuid4().hex}")


def _multipart_boundary(content_type: str) -> str:
    """The boundary parameter of a ``multipart/*`` content type, or ``""``."""
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    return (m.group(1) or m.group(2) or "").strip() if m else ""


async def _spool_single_part(stream, spool_dir: str,
                             max_part_bytes: int) -> SpooledPart:
    """Spool a non-multipart body as one part, enforcing the ceiling as it arrives."""
    path = _new_spool_path(spool_dir)
    digest = hashlib.sha256()
    written = 0
    fh = await asyncio.to_thread(open, path, "wb")
    try:
        async for chunk in stream:
            if not chunk:
                continue
            digest.update(chunk)
            written += len(chunk)
            if max_part_bytes and written > max_part_bytes:
                raise StowPartTooLarge(f"Part exceeds {max_part_bytes} byte limit")
            await asyncio.to_thread(fh.write, chunk)
    except Exception:
        await asyncio.to_thread(fh.close)
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    await asyncio.to_thread(fh.close)
    return SpooledPart("application/octet-stream", path, written, digest.hexdigest())


async def _read_part_headers(buf: bytearray, pull) -> str | None:
    """Consume one boundary marker and its part headers; return the part type.

    Returns ``None`` at the closing boundary. ``buf`` holds
    the unconsumed remainder of the body; ``pull`` supplies more chunks.
    """
    while len(buf) < 2 and await pull():
        pass
    if not buf:
        raise MalformedStowMultipart("Multipart body ended before its closing boundary")
    if buf.startswith(b"--"):
        return None
    if not buf.startswith(b"\r\n"):
        raise MalformedStowMultipart("Expected CRLF after multipart boundary")
    del buf[:2]
    while True:
        idx = buf.find(b"\r\n\r\n")
        if idx != -1:
            break
        if not await pull():
            raise MalformedStowMultipart("Multipart part headers were not terminated")
    header_block = bytes(buf[:idx])
    del buf[:idx + 4]
    for line in header_block.split(b"\r\n"):
        if line.lower().startswith(b"content-type:"):
            return line.split(b":", 1)[1].strip().decode("latin-1", "replace")
    return "application/octet-stream"


async def _spool_until_boundary(fh, buf: bytearray, next_sep: bytes, pull,
                                digest, written: int,
                                max_part_bytes: int) -> tuple[int, bool]:
    """Write payload bytes to ``fh`` until the next boundary marker appears.

    Returns ``(bytes_written, saw_boundary)``. ``saw_boundary`` is ``False`` when
    the stream ended without a closing marker; callers must reject that part.
    Raises :class:`StowPartTooLarge` the moment the part exceeds its
    ceiling — *before* writing past it — so an oversized upload aborts early.

    Only a ``len(next_sep)``-sized tail is ever held back, so a separator split
    across two chunks is never half-written into the payload while memory stays
    at one chunk.
    """
    async def emit(piece: bytes) -> int:
        if not piece:
            return written
        digest.update(piece)
        total = written + len(piece)
        if max_part_bytes and total > max_part_bytes:
            raise StowPartTooLarge(f"Part exceeds {max_part_bytes} byte limit")
        await asyncio.to_thread(fh.write, piece)
        return total

    while True:
        idx = buf.find(next_sep)
        if idx != -1:
            written = await emit(bytes(buf[:idx]))
            del buf[:idx + len(next_sep)]
            return written, True
        keep = len(next_sep)
        if len(buf) > keep:
            piece = bytes(buf[:-keep])
            del buf[:-keep]
            written = await emit(piece)
        if not await pull():
            # Preserve the final bytes so the caller can clean up the spool and
            # report a malformed envelope without publishing a partial object.
            piece = bytes(buf)
            buf.clear()
            written = await emit(piece)
            return written, False


async def iter_stow_parts_spooled(stream, content_type: str, spool_dir: str,
                                  max_part_bytes: int = 0,
                                  max_total_bytes: int = 0):
    """Yield ``(content_type, SpooledPart)``, spooling each part to disk (C-10).

    The streaming counterpart of :func:`iter_stow_parts`. Instead of accumulating
    each part in a ``bytearray`` (which for a 500 MB multi-frame instance means
    500 MB of RSS per in-flight part), the payload is written straight to a temp
    file under ``spool_dir`` in bounded chunks while its SHA-256 is computed
    incrementally. Peak memory is one incoming chunk plus one write buffer.

    The per-part ceiling is enforced **as the bytes arrive**, so an oversized
    part is aborted before it completes: the spool file is removed and
    :class:`StowPartTooLarge` is raised (the caller maps it to HTTP 413). The
    same applies to the running total across parts (``max_total_bytes``).
    """
    boundary = _multipart_boundary(content_type)
    os.makedirs(spool_dir, exist_ok=True)

    if not boundary:
        # Not multipart — treat the whole body as one spooled part.
        yield ("application/octet-stream",
               await _spool_single_part(stream, spool_dir, max_part_bytes))
        return

    marker = f"--{boundary}".encode("latin-1")
    buf = bytearray()
    total = 0

    async def _pull() -> bool:
        """Pull exactly one chunk into the buffer. False only at EOF."""
        try:
            chunk = await anext(stream)
        except StopAsyncIteration:
            return False
        if chunk:
            buf.extend(chunk)
        return True

    # Skip the preamble up to the first boundary marker.
    while buf.find(marker) == -1:
        if not await _pull():
            raise MalformedStowMultipart("Multipart body has no opening boundary")
    del buf[:buf.find(marker) + len(marker)]

    while True:
        ctype = await _read_part_headers(buf, _pull)
        if ctype is None:
            return
        # Payload: write straight to the spool file as the bytes arrive.
        path = _new_spool_path(spool_dir)
        digest = hashlib.sha256()
        fh = await asyncio.to_thread(open, path, "wb")
        written = 0
        completed = False
        try:
            written, completed = await _spool_until_boundary(
                fh, buf, b"\r\n" + marker, _pull, digest, written, max_part_bytes,
            )
        finally:
            try:
                await asyncio.to_thread(fh.close)
            finally:
                if not completed:
                    # The client may disconnect (or cancel this request) while
                    # the part is being written. Always release the incomplete
                    # spool even if closing the file itself fails.
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(os.unlink, path)

        if not completed:
            raise MalformedStowMultipart(
                "Multipart body ended before the next boundary"
            )

        total += written
        if max_total_bytes and total > max_total_bytes:
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise StowPartTooLarge(
                f"Request body exceeds {max_total_bytes} byte limit")
        yield (ctype, SpooledPart(ctype, path, written, digest.hexdigest()))


def dataset_from_path(path: str, force: bool = False,
                      stop_before_pixels: bool = False) -> Dataset:
    """Parse a spooled DICOM file into a Dataset.

    ``stop_before_pixels=True`` reads only the metadata header, so the STOW
    handler can validate and index a multi-GB instance without ever paging its
    pixel data into memory (C-10).
    """
    return pydicom.dcmread(path, force=force, stop_before_pixels=stop_before_pixels)


def build_multipart(parts: list[bytes], boundary: str, part_type: str = "application/dicom") -> bytes:
    body = bytearray()
    for data in parts:
        body += f"--{boundary}\r\n".encode("ascii")
        body += f"Content-Type: {part_type}\r\n\r\n".encode("ascii")
        body += data
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("ascii")
    return bytes(body)


def new_boundary() -> str:
    return f"paxs-{uuid.uuid4().hex}"


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #
def extract_frame(ds: Dataset, frame_index: int) -> bytes:
    """Return a single-frame DICOM file for /frames/{n} (1-indexed).

    Multi-frame output is written as Explicit VR Little Endian so the declared
    transfer syntax always matches the (uncompressed) frame bytes we emit.
    """
    from pydicom.uid import ExplicitVRLittleEndian

    n_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    if frame_index < 1 or frame_index > n_frames:
        raise ValueError(f"frame index out of range (1..{n_frames})")

    if n_frames == 1:
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        return buf.getvalue()

    import numpy as np

    pixels = ds.pixel_array  # (frames, rows, cols[, 3])
    frame = pixels[frame_index - 1]

    single = ds.copy()  # keep everything, then override image attributes
    del single.PixelData
    single.NumberOfFrames = 1
    single.Rows = int(frame.shape[0])
    single.Columns = int(frame.shape[1])
    single.SamplesPerPixel = 3 if frame.ndim == 3 else 1
    single.PixelData = np.ascontiguousarray(frame).tobytes()
    # Reset the file meta so the file declares what it actually contains.
    if single.file_meta is not None:
        single.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    single.is_little_endian = True
    single.is_implicit_VR = False

    buf = io.BytesIO()
    single.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def dataset_from_bytes(data: bytes, force: bool = False) -> Dataset:
    return pydicom.dcmread(io.BytesIO(data), force=force)
