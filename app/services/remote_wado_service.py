"""Streaming remote WADO-RS retrieval with an in-flight size cap (B-14 / FP-11).

The defect this replaces
------------------------
``dicomweb_client.wado_retrieve`` used to do::

    resp = client.get(f"/studies/{uid}")          # whole body into RAM
    if len(resp.content) > settings.DICOMWEB_CLIENT_MAX_BYTES:   # cap checked after
        raise ConnectionError(...)

``resp.content`` materialises the *entire* response before the guard runs, so a
hostile or misconfigured peer could drive the worker to OOM long before the
4 GiB check ever fired. The size limit was effectively a post-mortem.

The contract here
-----------------
* ``httpx.AsyncClient.stream("GET", …)`` — the response is consumed
  incrementally, never as one buffer.
* The ``Content-Length`` header is inspected **before the body is read**. A
  declared size above the cap raises :class:`WadoSizeLimitExceeded` immediately;
  leaving the stream context closes the connection rather than draining it.
* The body is read in fixed-size chunks (``WADO_STREAM_CHUNK_BYTES``, 64 KiB)
  and written straight to a temp file on disk. One chunk is the most that is
  ever resident in memory.
* A running counter aborts the transfer the moment the accumulated size passes
  the cap: the response (and so the socket) is closed and the partial temp file
  is removed — no truncated PHI left on disk, no unbounded read.

Nothing about the caller's contract changes: callers receive a
:class:`SpooledResponse` naming the temp file plus the verified byte count, and
must ``close()`` it (or use it as a context manager) to remove the spool.

Cap semantics
-------------
``MAX_WADO_INSTANCE_BYTES`` (512 MiB) is the **per-object** ceiling — it is what
:func:`stream_wado_instance` enforces, and what a multi-instance study retrieve
enforces **per part** through :func:`iter_spooled_multipart_parts`. A study-level
multipart body is legitimately larger than one instance, so that call site passes
``DICOMWEB_CLIENT_MAX_BYTES`` (4 GiB) as the whole-response ceiling while the
per-part cap keeps any single instance bounded.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

import httpx

from app.core.config import settings

log = logging.getLogger("paxs.dicomweb.remote_wado")

#: Accept header for a whole-study / whole-series retrieve.
MULTIPART_DICOM = 'multipart/related; type="application/dicom"'
#: Accept header for a single-instance retrieve (any transfer syntax).
SINGLE_DICOM = 'multipart/related; type="application/dicom"; transfer-syntax=*'

#: Prefix of a streamed temp file — never derived from a patient or object id.
SPOOL_PREFIX = "paxs-wado-"

#: Slack allowed for a part's MIME headers when bounding a streamed part.
_PART_HEADER_ALLOWANCE = 4096


class RemoteWadoError(ConnectionError):
    """A remote WADO-RS fetch failed (transport or protocol level).

    Subclasses ``ConnectionError`` to match what ``wado_retrieve`` has always
    raised for a non-200/206 reply, so existing callers keep working.
    """


class WadoSizeLimitExceeded(RemoteWadoError):
    """The remote response exceeded the configured byte cap (B-14).

    Carries the three numbers an operator needs, and nothing else — no path, no
    URL, no UID (N-2: diagnostics stay PHI-free).
    """

    def __init__(
        self,
        *,
        limit_bytes: int,
        declared_bytes: int | None = None,
        streamed_bytes: int = 0,
    ) -> None:
        self.limit_bytes = int(limit_bytes)
        self.declared_bytes = None if declared_bytes is None else int(declared_bytes)
        self.streamed_bytes = int(streamed_bytes)
        if declared_bytes is not None:
            detail = (
                f"declared Content-Length {self.declared_bytes} bytes exceeds the "
                f"{self.limit_bytes}-byte limit; body not read"
            )
        else:
            detail = (
                f"{self.streamed_bytes} bytes received, past the "
                f"{self.limit_bytes}-byte limit; transfer aborted"
            )
        super().__init__(f"remote WADO-RS response refused: {detail}")


@dataclass
class SpooledResponse:
    """A remote WADO-RS body spooled to a temp file on disk.

    ``size`` is the number of body bytes actually written and is always
    ``<= limit_bytes`` for the fetch that produced it.
    """

    path: Path
    size: int
    content_type: str
    status_code: int
    declared_bytes: int | None = None

    def open(self) -> BinaryIO:
        """Open the spooled body for reading."""
        return open(self.path, "rb")

    def read_bytes(self) -> bytes:
        """Load the spooled body.

        Only for bodies already known to be small (a single instance, bounded by
        ``MAX_WADO_INSTANCE_BYTES``); the multipart splitter reads the file
        instead of going through here.
        """
        return self.path.read_bytes()

    def close(self) -> None:
        """Remove the temp file. Idempotent; never raises."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning(
                "remote WADO-RS spool cleanup failed (error_type=%s)", type(exc).__name__
            )

    def __enter__(self) -> SpooledResponse:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.close()
        return False


def spool_dir() -> str:
    """Directory streamed bodies are spooled into.

    The active backend's staging directory (``STORAGE_ROOT/.tmp`` for local
    storage) so the spool lives inside the configured storage root and on the
    same filesystem as the archive; the system temp directory when the backend
    has no staging area of its own. Never falls back to an unbounded path.
    """
    from app.services import storage_service

    try:
        directory = storage_service.staging_dir()
    except (httpx.HTTPError, OSError, ValueError) as exc:  # a broken backend must not block retrieval
        log.debug("WADO spool directory unavailable (error_type=%s)", type(exc).__name__)
        directory = tempfile.gettempdir()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        log.debug("WADO spool directory unusable (error_type=%s)", type(exc).__name__)
        directory = tempfile.gettempdir()
    return directory


def _positive(value: Any, name: str) -> int:
    number = int(value)
    if number <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return number


def _declared_length(headers: Mapping[str, str]) -> int | None:
    """The peer's declared Content-Length, or None when absent or unparseable."""
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


class _TempSpool:
    """A temp file a streamed body is written into, one chunk at a time.

    ``mkstemp`` creates the file 0600 and the name carries no patient or object
    identifier, so a crashed retrieve cannot leave an enumerable PHI file
    behind. ``discard()`` is the only way out of a failed transfer.
    """

    def __init__(self, directory: str) -> None:
        fd, name = tempfile.mkstemp(prefix=SPOOL_PREFIX, suffix=".part", dir=directory)
        self.path = Path(name)
        self._fh = os.fdopen(fd, "wb")
        self._closed = False

    def write(self, chunk: bytes) -> None:
        self._fh.write(chunk)

    def finish(
        self, *, content_type: str, status_code: int, declared_bytes: int | None
    ) -> SpooledResponse:
        self._fh.flush()
        self._fh.close()
        self._closed = True
        return SpooledResponse(
            path=self.path,
            size=self.path.stat().st_size,
            content_type=content_type,
            status_code=status_code,
            declared_bytes=declared_bytes,
        )

    def discard(self) -> None:
        if not self._closed:
            try:
                self._fh.close()
            except OSError as exc:
                log.debug("WADO spool close failed (error_type=%s)", type(exc).__name__)
            self._closed = True
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("WADO spool cleanup failed (error_type=%s)", type(exc).__name__)


async def stream_wado_response(
    node: Any,
    resource_path: str,
    *,
    max_bytes: int | None = None,
    chunk_size: int | None = None,
    accept: str | None = None,
    request_timeout: float | None = None,
) -> SpooledResponse:
    """Stream one WADO-RS resource to a temp file, enforcing the cap in flight.

    ``max_bytes`` defaults to ``MAX_WADO_INSTANCE_BYTES`` (per instance).
    Callers retrieving a multi-instance multipart body pass the larger
    whole-response ceiling and enforce the per-part cap separately — see
    :func:`iter_spooled_multipart_parts`.

    ``request_timeout`` overrides ``DICOMWEB_CLIENT_TIMEOUT`` for this call.

    Raises :class:`WadoSizeLimitExceeded` — before reading anything when the
    peer declares an oversized ``Content-Length``, and mid-stream when it does
    not declare one and simply keeps sending. The response is closed either way,
    so the socket is torn down instead of drained.
    """
    # Imported lazily: the SSRF guard and header builder live with the rest of
    # the DICOMweb client, which imports this module in turn.
    from app.services.dicomweb_client import _headers, validate_web_url

    validate_web_url(node.web_url)  # per-call re-check (DNS rebinding guard)
    limit = _positive(settings.MAX_WADO_INSTANCE_BYTES if max_bytes is None else max_bytes,
                      "max_bytes")
    step = _positive(settings.WADO_STREAM_CHUNK_BYTES if chunk_size is None else chunk_size,
                     "chunk_size")
    headers = _headers(node)
    if accept:
        headers["Accept"] = accept
    url = f"{node.web_url.rstrip('/')}{resource_path}"
    read_timeout = (
        settings.DICOMWEB_CLIENT_TIMEOUT if request_timeout is None else float(request_timeout)
    )

    async with (
        httpx.AsyncClient(timeout=read_timeout, follow_redirects=False) as client,
        client.stream("GET", url, headers=headers) as response,
    ):
        if response.status_code not in (200, 206):
            raise RemoteWadoError(f"WADO-RS returned HTTP {response.status_code}")

        declared = _declared_length(response.headers)
        if declared is not None and declared > limit:
            # Refuse before the body is touched. Unwinding this `async with`
            # closes the response, which closes the connection — the peer is
            # never invited to push the body.
            raise WadoSizeLimitExceeded(limit_bytes=limit, declared_bytes=declared)

        return await _spool_body(response, limit=limit, step=step, declared=declared)


async def _spool_body(
    response: httpx.Response, *, limit: int, step: int, declared: int | None
) -> SpooledResponse:
    """Read the streamed body to a temp file, aborting the moment it passes ``limit``.

    Split out of :func:`stream_wado_response` so both stay inside the 60-line
    ceiling (AGENTS.md §3.1); the behaviour is unchanged — the partial spool is
    removed on every non-completing path, and the exception unwinds through the
    caller's ``async with`` so the socket is closed rather than drained.
    """
    spool = _TempSpool(spool_dir())
    completed = False
    try:
        total = 0
        async for chunk in response.aiter_bytes(step):
            total += len(chunk)
            if total > limit:
                # Past the cap: drop out of the loop (closing the response and
                # the socket) and delete the partial spool.
                raise WadoSizeLimitExceeded(limit_bytes=limit, streamed_bytes=total)
            spool.write(chunk)
        result = spool.finish(
            content_type=response.headers.get("content-type", ""),
            status_code=response.status_code,
            declared_bytes=declared,
        )
        completed = True
        return result
    finally:
        if not completed:
            spool.discard()


async def stream_wado_instance(
    node: Any,
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    *,
    max_bytes: int | None = None,
    chunk_size: int | None = None,
    request_timeout: float | None = None,
) -> SpooledResponse:
    """Retrieve a single instance, capped at ``MAX_WADO_INSTANCE_BYTES``.

    This is the per-object entry point: one instance is exactly one WADO-RS
    resource, so the declared-size pre-check applies to it directly. The caller
    owns the returned spool and must ``close()`` it.
    """
    for label, value in (
        ("study_uid", study_uid),
        ("series_uid", series_uid),
        ("sop_uid", sop_uid),
    ):
        if not value:
            raise ValueError(f"{label} is required for a WADO-RS instance retrieve")
    resource = (
        f"/studies/{quote(str(study_uid), safe='')}"
        f"/series/{quote(str(series_uid), safe='')}"
        f"/instances/{quote(str(sop_uid), safe='')}"
    )
    return await stream_wado_response(
        node, resource, max_bytes=max_bytes, chunk_size=chunk_size,
        accept=SINGLE_DICOM, request_timeout=request_timeout,
    )


# --------------------------------------------------------------------------- #
# Streaming multipart splitter over a spooled body
# --------------------------------------------------------------------------- #
def _multipart_boundary(content_type: str) -> str:
    match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    return (match.group(1) or match.group(2) or "").strip() if match else ""


def _iter_marker_segments(
    fh: BinaryIO, marker: bytes, chunk_size: int, max_segment: int | None
) -> Iterator[bytes]:
    """Yield the byte runs between successive ``marker`` occurrences.

    Only the tail since the last marker is retained, so the reader never holds
    more than one segment plus one chunk. When ``max_segment`` is given a
    growing tail (i.e. a part with no following boundary) aborts the split
    instead of buffering an oversized part.
    """
    carry = b""
    while True:
        chunk = fh.read(chunk_size)
        if not chunk:
            break
        segments = (carry + chunk).split(marker)
        carry = segments.pop()
        yield from segments
        if max_segment is not None and len(carry) > max_segment:
            raise WadoSizeLimitExceeded(limit_bytes=max_segment, streamed_bytes=len(carry))
    if carry:
        yield carry


def _strip_part_headers(segment: bytes) -> bytes:
    """Drop a MIME part's headers and its trailing CRLF, keeping the payload."""
    start = segment.find(b"\r\n\r\n")
    offset = 4
    if start == -1:
        start = segment.find(b"\n\n")
        offset = 2
    if start == -1:
        return b""
    data = segment[start + offset:]
    if data.endswith(b"\r\n"):
        data = data[:-2]
    return data


def _read_capped(path: Path, cap: int | None, chunk_size: int) -> bytes:
    """Read a whole spooled body, aborting the moment it passes ``cap``."""
    buffer = bytearray()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            buffer += chunk
            if cap is not None and len(buffer) > cap:
                raise WadoSizeLimitExceeded(limit_bytes=cap, streamed_bytes=len(buffer))
    return bytes(buffer)


def iter_spooled_multipart_parts(
    path: str | Path,
    content_type: str,
    *,
    max_part_bytes: int | None = None,
    chunk_size: int | None = None,
) -> Iterator[bytes]:
    """Yield each part's payload from a multipart body spooled on disk.

    Reads the file in chunks and keeps at most one part resident, so splitting a
    multi-gigabyte study response never reloads it into memory. ``max_part_bytes``
    (normally ``MAX_WADO_INSTANCE_BYTES``) bounds a single part — one instance —
    and raises :class:`WadoSizeLimitExceeded` rather than buffering past it.

    A non-multipart ``content_type`` means the body *is* the single instance.
    """
    step = _positive(settings.WADO_STREAM_CHUNK_BYTES if chunk_size is None else chunk_size,
                     "chunk_size")
    cap = None if max_part_bytes is None else _positive(max_part_bytes, "max_part_bytes")
    source = Path(path)

    boundary = _multipart_boundary(content_type)
    if not boundary:
        yield _read_capped(source, cap, step)
        return

    marker = f"--{boundary}".encode("latin-1")
    max_segment = None if cap is None else cap + _PART_HEADER_ALLOWANCE
    with open(source, "rb") as fh:
        for index, segment in enumerate(
            _iter_marker_segments(fh, marker, step, max_segment)
        ):
            if index == 0:
                continue  # preamble before the first boundary
            if segment.startswith(b"--"):
                return    # closing delimiter
            payload = _strip_part_headers(segment)
            if not payload:
                continue
            if cap is not None and len(payload) > cap:
                raise WadoSizeLimitExceeded(limit_bytes=cap, streamed_bytes=len(payload))
            yield payload
