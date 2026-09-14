"""Helpers for the DICOMweb endpoints: DICOM JSON (PS3.18 F), multipart
encoding/parsing and single-frame extraction.
"""

from __future__ import annotations

import email
import io
import uuid
from email import policy

import pydicom
from pydicom.dataset import Dataset

# --------------------------------------------------------------------------- #
# DICOM JSON (application/dicom+json)
# --------------------------------------------------------------------------- #
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


def instance_to_dicom_json(instance) -> dict:
    data: dict = {}
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
    """Full-fidelity DICOM JSON from a Dataset (used by WADO-RS /metadata)."""
    try:
        raw = ds.to_json_dict()
    except AttributeError:  # pragma: no cover - very old pydicom
        raise
    # DICOMweb uses uppercase hex tags; normalize just in case.
    data = {str(k).upper(): v for k, v in raw.items()}
    # to_json_dict() excludes the file meta group, but DICOMweb clients (PaxsViewer
    # image loader) need TransferSyntaxUID (0002,0010) to pick the right
    # decoder (e.g. JPEG Lossless vs baseline JPEG) before fetching frames.
    if "00020010" not in data and ds.file_meta is not None:
        ts_uid = getattr(ds.file_meta, "TransferSyntaxUID", None)
        if ts_uid:
            data["00020010"] = {"vr": "UI", "Value": [str(ts_uid)]}
    return data


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
                # EOF without a closing boundary: flush what remains.
                payload.extend(buf)
                buf.clear()
                break

        total += len(payload)
        if max_part_bytes and len(payload) > max_part_bytes:
            raise StowPartTooLarge(
                f"Part exceeds {max_part_bytes} byte limit")
        if max_total_bytes and total > max_total_bytes:
            raise StowPartTooLarge(
                f"Request body exceeds {max_total_bytes} byte limit")
        yield (ctype, bytes(payload))


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
