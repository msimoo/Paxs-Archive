"""PHI-free context for DICOM decode failures.

WHY
---
A decode failure is almost always a *codec* problem: the archive has no handler
for the instance's transfer syntax, or the syntax implies a pixel layout the
handler cannot express (16-bit colour, multi-sample JPEG 2000, near-lossless
JPEG-LS, …). The previous logs said only "pixel_array failed" — which left the
operator to open the file and find out which syntax it was. During the viewer
audit that cost real time (see V1-4 in PAXS_VIEWER_DIAGNOSTIC_AUDIT.md).

Every decode-failure log now carries the transfer syntax *with its human name*
plus the pixel layout, so the codec is identifiable from the log line alone:

    transfer_syntax=1.2.840.10008.1.2.4.80 (JPEG-LS Lossless)
    modality=CT bits=16/16 samples=1 photometric=MONOCHROME2 512x512 frames=1
    series=1.2.840… sop=1.2.840… frame=3

Only DICOM identifiers and pixel-layout attributes are emitted — never patient
demographics, never filesystem paths (matching the archive's existing logging
policy for the viewer paths).
"""

from __future__ import annotations

# Fallback labels for the syntaxes this archive produces or is most likely to
# receive, used only when pydicom does not recognise the UID (pydicom's
# ``UID.name`` is authoritative when it knows the value).
_COMMON_LABELS = {
    "1.2.840.10008.1.2": "Implicit VR Little Endian",
    "1.2.840.10008.1.2.1": "Explicit VR Little Endian",
    "1.2.840.10008.1.2.1.99": "Deflated Explicit VR Little Endian",
    "1.2.840.10008.1.2.2": "Explicit VR Big Endian",
    "1.2.840.10008.1.2.4.50": "JPEG Baseline (Process 1)",
    "1.2.840.10008.1.2.4.51": "JPEG Extended (Process 2 & 4)",
    "1.2.840.10008.1.2.4.57": "JPEG Lossless (Process 14)",
    "1.2.840.10008.1.2.4.70": "JPEG Lossless SV1",
    "1.2.840.10008.1.2.4.80": "JPEG-LS Lossless",
    "1.2.840.10008.1.2.4.81": "JPEG-LS Lossy (Near-Lossless)",
    "1.2.840.10008.1.2.4.90": "JPEG 2000 Lossless Only",
    "1.2.840.10008.1.2.4.91": "JPEG 2000",
    "1.2.840.10008.1.2.4.92": "JPEG 2000 Part 2 Multi-component Lossless",
    "1.2.840.10008.1.2.4.93": "JPEG 2000 Part 2 Multi-component",
    "1.2.840.10008.1.2.4.201": "HTJ2K Lossless",
    "1.2.840.10008.1.2.4.202": "HTJ2K Lossless RPCL",
    "1.2.840.10008.1.2.4.203": "HTJ2K",
    "1.2.840.10008.1.2.5": "RLE Lossless",
    "1.2.840.10008.1.2.6.1": "RFC 2557 MIME Encapsulation",
    "1.2.840.10008.1.2.7.1": "SMPTE ST 2110-20 Uncompressed",
    "1.2.840.10008.1.2.8.1": "Deflated Image Frame Compression",
}

_UNKNOWN = {"", "Unknown UID", "unknown"}


def transfer_syntax_label(uid) -> str:
    """``"1.2.840.10008.1.2.4.80 (JPEG-LS Lossless)"`` for a UID or UID string.

    Returns ``"unset"`` when the dataset has no transfer syntax, which is itself
    a useful signal (a bare/part-10-less dataset decodes differently).
    """
    if uid is None:
        return "unset"
    value = str(uid).strip()
    if not value:
        return "unset"
    name = ""
    try:
        name = str(getattr(uid, "name", "") or "").strip()
    except Exception:  # pragma: no cover — a UID that cannot report its name
        name = ""
    if name in _UNKNOWN:
        name = _COMMON_LABELS.get(value, "")
    return f"{value} ({name})" if name else value


def _attr(ds, name: str):
    """Read an attribute from the dataset OR its file_meta, or None."""
    for source in (ds, getattr(ds, "file_meta", None)):
        if source is None:
            continue
        try:
            value = getattr(source, name, None)
        except Exception:  # pragma: no cover
            value = None
        if value is not None and str(value) != "":
            return value
    return None


def decode_context(ds, *, frame: int | None = None,
                   series_uid: str | None = None) -> str:
    """One-line, PHI-free decode-failure context.

    Groups the three things needed to identify a codec problem:

    1. **transfer syntax** (value + name) — which decoder should have run;
    2. **pixel layout** — bits allocated/stored, samples per pixel, photometric
       interpretation, geometry. This is what distinguishes the cases a decoder
       silently mishandles (16-bit colour, multi-sample, near-lossless);
    3. **identity** — series / SOP instance UID and the frame index.
    """
    file_meta = getattr(ds, "file_meta", None)
    ts = getattr(file_meta, "TransferSyntaxUID", None) if file_meta is not None else None

    bits_alloc = _attr(ds, "BitsAllocated")
    bits_stored = _attr(ds, "BitsStored")
    samples = _attr(ds, "SamplesPerPixel")
    photometric = _attr(ds, "PhotometricInterpretation")
    rows = _attr(ds, "Rows")
    cols = _attr(ds, "Columns")
    n_frames = _attr(ds, "NumberOfFrames")

    parts = [f"transfer_syntax={transfer_syntax_label(ts)}"]

    layout = []
    modality = _attr(ds, "Modality")
    if modality:
        layout.append(f"modality={modality}")
    if bits_alloc is not None:
        layout.append(f"bits={bits_alloc}/{bits_stored if bits_stored is not None else '?'}")
    if samples is not None:
        layout.append(f"samples={samples}")
    if photometric:
        layout.append(f"photometric={photometric}")
    if rows is not None and cols is not None:
        layout.append(f"{rows}x{cols}")
    if n_frames is not None:
        layout.append(f"frames={n_frames}")
    if layout:
        parts.append(" ".join(str(p) for p in layout))

    ident = []
    series = series_uid or _attr(ds, "SeriesInstanceUID")
    if series:
        ident.append(f"series={series}")
    sop = _attr(ds, "SOPInstanceUID")
    if sop:
        ident.append(f"sop={sop}")
    if frame is not None:
        ident.append(f"frame={frame}")
    if ident:
        parts.append(" ".join(str(p) for p in ident))

    return " | ".join(parts)
