"""Remote-node DICOMweb client — QIDO-RS query and WADO-RS retrieve over HTTP.

Gives registered ``dicom_nodes`` an alternate transport to DIMSE: a peer that
exposes a DICOMweb service (Orthanc, dcm4chee, a second PAXS…) can be queried
with ``GET {web_url}/studies?PatientID=…`` (QIDO-RS) and retrieved with
``GET {web_url}/studies/{uid}`` (WADO-RS), which this client translates into
the same JSON match shape the C-FIND endpoint returns, and ingests received
instances through the normal C-STORE pipeline (idempotent re-import).

Security — SSRF guard: ``validate_web_url`` enforces
  * scheme is http or https only,
  * no credentials embedded in the URL (Basic auth is configured separately
    on the node row),
  * the hostname resolves, and the resolved address is not loopback,
    link-local, private, or a cloud-metadata range — unless the operator
    opts out with ``DICOMWEB_CLIENT_ALLOW_PRIVATE`` (test peers on 127.0.0.1).
  * an optional connection/read timeout caps every request.

Security — bounded retrieve (B-14 / FP-11): a WADO-RS **pull** never buffers the
response. ``wado_retrieve`` streams the body through
:mod:`app.services.remote_wado_service`, which refuses a peer that declares an
oversized ``Content-Length`` before reading anything, cuts the transfer off
mid-flight the moment the cap is passed, and spools verified chunks to a temp
file on disk. See that module for the cap semantics.

The client runs sync (httpx) so the REST layer can wrap it in ``to_thread``
exactly like the DIMSE SCU helpers.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import tempfile
import urllib.parse
from collections.abc import Iterator
from typing import BinaryIO

import httpx

from app.core.config import settings

log = logging.getLogger("paxs.dicomweb.client")

# Cloud metadata / link-local ranges must never be reachable through an admin
# -configured web_url unless explicitly allowed (SSRF / meta-leak protection).
_BLOCKED_NETS = [
    ipaddress.ip_network("169.254.0.0/16"),   # link-local + AWS/GCP/Azure metadata
    ipaddress.ip_network("100.100.100.0/24"),  # Alibaba metadata
]


def validate_web_url(url: str | None, allow_private: bool | None = None) -> str:
    """Validate a remote DICOMweb base URL. Returns the normalized URL.

    Raises ValueError with a human-readable reason when the URL is unsafe or
    malformed — callers surface it as a 4xx with the message intact.
    """
    if not url or not url.strip():
        raise ValueError("web_url is required for DICOMweb transport")
    url = url.strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("web_url must use http:// or https://")
    if parsed.username or parsed.password:
        raise ValueError("web_url must not embed credentials — set web_username/web_password instead")
    if not parsed.hostname:
        raise ValueError("web_url has no host")

    allow = settings.DICOMWEB_CLIENT_ALLOW_PRIVATE if allow_private is None else allow_private
    if not allow:
        _reject_blocked_host(parsed.hostname)
    return url


def _reject_blocked_host(hostname: str) -> None:
    """Resolve `hostname` and raise ValueError if any address is unsafe."""
    try:
        addrs = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"web_url host does not resolve: {hostname}") from exc
    for addr in addrs:
        ip = ipaddress.ip_address(addr[4][0])
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
            or any(ip in net for net in _BLOCKED_NETS)
        ):
            raise ValueError(
                f"web_url resolves to a protected address ({ip}) — "
                "loopback/private/link-local ranges are blocked by the SSRF guard"
            )


def _headers(node) -> dict:
    headers = {"Accept": "application/dicom+json"}
    if node.web_username or node.web_password:
        import base64

        raw = f"{node.web_username or ''}:{node.web_password or ''}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
    return headers


def _client(node) -> httpx.Client:
    return httpx.Client(
        base_url=node.web_url,
        headers=_headers(node),
        timeout=settings.DICOMWEB_CLIENT_TIMEOUT,
        follow_redirects=False,
    )


# --------------------------------------------------------------------------- #
# QIDO-RS (query)
# --------------------------------------------------------------------------- #
def _qido_path(level: str, filters: dict | None) -> str:
    """Build the QIDO-RS resource path for a query level.

    SERIES/INSTANCE resources live under a study/series, so the caller's
    StudyInstanceUID / SeriesInstanceUID filters become path segments (they
    are consumed here and must not also be sent as query params). A missing
    required UID raises ValueError — the API surfaces it as a 422.
    """
    level = (level or "STUDY").upper()
    if level == "SERIES":
        study = (filters or {}).get("StudyInstanceUID") or (filters or {}).get("0020000D")
        if not study:
            raise ValueError("SERIES-level QIDO requires filters[StudyInstanceUID]")
        return f"/studies/{urllib.parse.quote(str(study), safe='')}/series"
    if level == "INSTANCE":
        study = (filters or {}).get("StudyInstanceUID") or (filters or {}).get("0020000D")
        series = (filters or {}).get("SeriesInstanceUID") or (filters or {}).get("0020000E")
        if not study or not series:
            raise ValueError(
                "INSTANCE-level QIDO requires filters[StudyInstanceUID] and "
                "filters[SeriesInstanceUID]"
            )
        return (f"/studies/{urllib.parse.quote(str(study), safe='')}/series"
                f"/{urllib.parse.quote(str(series), safe='')}/instances")
    return "/studies"


# Query keys that become path segments (SERIES/INSTANCE levels) — removed from
# the query string once consumed by _qido_path.
_PATH_KEYS = ("StudyInstanceUID", "0020000D", "SeriesInstanceUID", "0020000E")


def qido_query(node, level: str = "STUDY", filters: dict | None = None) -> list[dict]:
    """QIDO-RS query against `node.web_url`.

    DICOMweb maps query keys to query-string parameters (tag keywords or
    dotted group/element numbers); unlike C-FIND there is no ``return all
    fields`` wildcard, so we request ``includefield=all`` and let the server
    return what it supports. Returns the raw match list (JSON) — callers turn
    it into the dashboard's C-FIND-shaped rows.

    SSRF: the web_url is re-validated on every call (not just at node
    creation) so a DNS change cannot sneak a private target past the guard.
    """
    validate_web_url(node.web_url)  # per-call re-check (DNS rebinding guard)
    path = _qido_path(level, filters)
    params: dict[str, str] = {"includefield": "all", "limit": "200"}
    for keyword, value in (filters or {}).items():
        if value is None or keyword in _PATH_KEYS:
            continue
        params[keyword] = str(value)

    max_bytes = settings.DICOMWEB_QIDO_MAX_BYTES
    if max_bytes < 0:
        raise ValueError("DICOMWEB_QIDO_MAX_BYTES must be zero or greater")
    body = bytearray()
    with _client(node) as client:
        with client.stream("GET", path, params=params) as resp:
            if resp.status_code != 200:
                raise ConnectionError(f"QIDO-RS returned HTTP {resp.status_code}")
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and max_bytes and int(declared) > max_bytes:
                raise ConnectionError("QIDO-RS response exceeds configured byte limit")
            for chunk in resp.iter_bytes():
                if max_bytes and len(body) + len(chunk) > max_bytes:
                    raise ConnectionError("QIDO-RS response exceeds configured byte limit")
                body.extend(chunk)

    data = json.loads(body)
    if isinstance(data, dict):
        data = data.get("datasets", data.get("results", []))
    return list(data) if isinstance(data, list) else []


def _qido_value(match: dict, keyword: str) -> str | None:
    """Extract a value from a QIDO match: {keyword: {"Value": [...]}} or bare.

    Multi-valued elements are joined with a backslash (DICOM multi-value
    convention), matching the C-FIND ``_normalize_match`` row shape so the
    dashboard renders both transports identically."""
    cell = match.get(keyword)
    if isinstance(cell, dict):
        values = cell.get("Value")
        if isinstance(values, list) and values:
            return "\\".join(str(v) for v in values)
        return None
    if cell is not None:
        return str(cell)
    return None


def _qido_study_uid(match: dict) -> str | None:
    uid = _qido_value(match, "StudyInstanceUID")
    if uid:
        return uid
    return _qido_value(match, "0020000D")


def _qido_series_uid(match: dict) -> str | None:
    return _qido_value(match, "SeriesInstanceUID") or _qido_value(match, "0020000E")


def qido_to_match_rows(level: str, matches: list[dict]) -> list[dict]:
    """Normalize QIDO-RS JSON matches to the C-FIND dashboard row shape."""
    rows: list[dict] = []
    for m in matches:
        row = {}
        for keyword in (
            "PatientName", "PatientID", "PatientBirthDate", "PatientSex",
            "StudyInstanceUID", "StudyDate", "StudyTime", "AccessionNumber",
            "ModalitiesInStudy", "StudyDescription", "ReferringPhysicianName",
            "SeriesInstanceUID", "SeriesNumber", "Modality", "SeriesDescription",
            "SOPInstanceUID", "SOPClassUID", "InstanceNumber",
        ):
            value = _qido_value(m, keyword)
            if value is not None:
                row[keyword] = value
        # Fallback for servers that only echo tag-number keys.
        if "StudyInstanceUID" not in row:
            uid = _qido_study_uid(m)
            if uid:
                row["StudyInstanceUID"] = uid
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# WADO-RS (retrieve)
# --------------------------------------------------------------------------- #
def wado_retrieve(node, study_uid: str, tenant_id: int | None = None,
                  on_progress=None, include_all: bool = False) -> dict:
    """Retrieve every instance of a study via WADO-RS and ingest it locally.

    ``GET {web_url}/studies/{uid}`` with ``Accept: multipart/related;
    type="application/dicom"`` returns one DICOM part per instance; each part
    is saved + indexed through the normal pipeline (idempotent re-import),
    tagged with `tenant_id` (VNA) for new studies. Returns the same summary
    shape as ``pull_from_node_sync`` (per-instance results for auditing).

    B-14/FP-11: the response is **streamed** (never ``resp.content``) and the
    byte count is enforced while it arrives, so an oversized body is aborted
    rather than buffered. ``DICOMWEB_CLIENT_MAX_BYTES`` is the whole-response
    ceiling — a multi-instance study legitimately exceeds one instance — and
    each part is additionally bounded by ``MAX_WADO_INSTANCE_BYTES``, which is
    the per-instance cap.
    """
    import asyncio

    from app.services import remote_wado_service

    if not study_uid:
        raise ValueError("study_uid is required for WADO-RS retrieve")

    expected_total = 0
    if on_progress is not None:
        try:
            qido_matches = qido_query(node, "STUDY", {"StudyInstanceUID": study_uid})
            if qido_matches:
                count_value = _qido_value(qido_matches[0], "NumberOfStudyRelatedInstances")
                expected_total = int(count_value) if count_value and count_value.isdigit() else 0
        except Exception as exc:
            # The count is a UI estimate; WADO remains authoritative for retrieval.
            log.info("QIDO progress estimate unavailable (error_type=%s)", type(exc).__name__)
        on_progress(expected_total, 0, 0)

    spooled = asyncio.run(
        remote_wado_service.stream_wado_response(
            node,
            f"/studies/{urllib.parse.quote(study_uid, safe='')}",
            max_bytes=settings.DICOMWEB_CLIENT_MAX_BYTES,
            accept=remote_wado_service.MULTIPART_DICOM,
        )
    )
    try:
        return _ingest_spooled_study(
            spooled, study_uid, tenant_id, on_progress, expected_total,
            include_all=include_all,
        )
    finally:
        # The spool holds PHI bytes: it is removed on every path, success or not.
        spooled.close()


def _ingest_spooled_study(spooled, study_uid: str, tenant_id: int | None,
                          on_progress=None, total_items: int = 0,
                          include_all: bool = False) -> dict:
    """Split a spooled multipart study body and ingest every instance part.

    The body is read from the temp file in chunks (see
    ``remote_wado_service.iter_spooled_multipart_parts``) so a large study is
    never reloaded into RAM just to be split; only one instance is resident at a
    time, bounded by ``MAX_WADO_INSTANCE_BYTES``.
    """
    from app.services import remote_wado_service

    pulled = failed = 0
    results: list[dict] = []
    parts = remote_wado_service.iter_spooled_multipart_parts(
        spooled.path,
        spooled.content_type,
        max_part_bytes=settings.MAX_WADO_INSTANCE_BYTES,
    )
    for part in parts:
        ds = _dataset_from_bytes(part)
        if ds is None:
            failed += 1
            results.append({"sop_uid": "", "study_uid": study_uid,
                            "ok": False, "reason": "unparseable part"})
            if on_progress is not None:
                on_progress(total_items, pulled, failed)
            continue
        item = {
            "sop_uid": str(getattr(ds, "SOPInstanceUID", "")),
            "study_uid": study_uid,
            "ok": False,
        }
        try:
            _ingest_dicomweb_dataset(ds, tenant_id, include_all=include_all)
            item["ok"] = True
            pulled += 1
        except (httpx.HTTPError, OSError, ValueError) as exc:  # one bad instance must not abort the pull
            # Exception text can contain PHI, response fragments, or local paths.
            log.warning("dicomweb_pull_instance_failed error_type=%s", type(exc).__name__)
            item["reason"] = "Instance ingestion failed"
            failed += 1
        results.append(item)
        if on_progress is not None:
            on_progress(total_items, pulled, failed)

    return {"ok": failed == 0 and pulled > 0, "pulled": pulled, "failed": failed,
            "results": results}


def _iter_multipart_parts(body: bytes, content_type: str):
    """Split an in-memory multipart/related body into the raw bytes of each part.

    For a body already resident in RAM (tests, and callers that have their own
    bound). A **retrieved** body goes through
    ``remote_wado_service.iter_spooled_multipart_parts`` instead, which splits
    the spooled file without loading it.
    """
    import re

    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    boundary = (m.group(1) or m.group(2) or "").strip() if m else ""
    if not boundary:
        # Not multipart — treat the whole body as a single part.
        yield body
        return
    marker = f"--{boundary}".encode("latin-1")
    parts = body.split(marker)
    for part in parts[1:]:  # drop leading preamble
        if part.startswith(b"--") or not part.strip():
            continue
        # Skip headers (up to the first blank CRLFCRLF).
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            header_end = part.find(b"\n\n")
        if header_end == -1:
            continue
        data = part[header_end + (4 if part[header_end:header_end+4] == b"\r\n\r\n" else 2):]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        yield data


def _dataset_from_bytes(data: bytes):
    import io

    import pydicom

    try:
        return pydicom.dcmread(io.BytesIO(data), force=True)
    except Exception:
        log.warning("DICOMweb retrieve: could not parse a multipart part (%d bytes)", len(data))
        return None


def _ingest_dicomweb_dataset(ds, tenant_id: int | None = None,
                             include_all: bool = False) -> None:
    """Stage, index, and publish one WADO-RS-received instance.

    The DB row commits before the staged object becomes canonical, matching the
    C-STORE and DIMSE C-GET ingest paths so a failed index never publishes an
    untracked DICOM object.
    """
    import asyncio

    from app.dicom.parser import parse_metadata
    from app.db.session import AsyncSessionLocal
    from app.services import storage_service
    from app.services.indexing_service import index_new_instance

    # Multi-storage: pulled slices join the study's recorded backend.
    backend = storage_service.resolve_study_backend(
        str(getattr(ds, "StudyInstanceUID", "UNKNOWN"))
    )
    meta = parse_metadata(ds)
    staged = storage_service.stage_dataset_with_quota(ds, tenant_id, backend)
    try:
        staged.verify()
        async def _run() -> None:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    await index_new_instance(
                        session, meta, staged.key, staged.sha256,
                        performed_by="dicomweb-pull", tenant_id=tenant_id,
                        storage_backend=backend,
                        read_path=staged.staged_path or staged.key,
                        ds=ds,
                        file_size_bytes=staged.bytes_written,
                        include_all=include_all,
                    )

        asyncio.run(_run())
        staged.promote()
    except Exception:
        staged.discard()
        raise


# --------------------------------------------------------------------------- #
# STOW-RS (upload / forward)
# --------------------------------------------------------------------------- #
def _stow_response_sops(resp: httpx.Response) -> tuple[set[str], set[str]]:
    """Return success and failure SOP UIDs explicitly reported by STOW-RS."""
    if not resp.content:
        return set(), set()
    try:
        payload = resp.json()
    except ValueError:
        return set(), set()
    if not isinstance(payload, dict):
        return set(), set()
    def read_sequence(keys: tuple[str, ...]) -> set[str]:
        uids: set[str] = set()
        for key in keys:
            seq = payload.get(key)
            if not isinstance(seq, list):
                continue
            for item in seq:
                if not isinstance(item, dict):
                    continue
                sop = item.get("ReferencedSOPInstanceUID") or item.get("00081155")
                if isinstance(sop, dict):
                    values = sop.get("Value")
                    sop = values[0] if isinstance(values, list) and values else None
                if sop:
                    uids.add(str(sop))
        return uids
    return (read_sequence(("ReferencedSOPSequence", "00081199")),
            read_sequence(("FailedSOPSequence", "00081198")))


def _multipart_body_chunks(targets: list[dict], boundary: str,
                           chunk_size: int = 1024 * 1024,
                           counter: list[int] | None = None) -> Iterator[bytes]:
    """Yield multipart bytes from backend-native object streams in fixed chunks."""
    from app.services import storage_service

    total = 0
    for target in targets:
        yield f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
        stream = storage_service.read_stream(target["key"], backend_name=target.get("backend"))
        try:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                yield chunk
        finally:
            stream.close()
        yield b"\r\n"
    yield f"--{boundary}--\r\n".encode()
    if counter is not None:
        counter.append(total)


def _write_multipart_spool(targets: list[dict], boundary: str,
                           output: BinaryIO, counter: list[int],
                           chunk_size: int = 1024 * 1024) -> int:
    """Write the ordinary STOW body to disk and return its exact framed size."""
    from app.services import storage_service

    for target in targets:
        output.write(f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode())
        stream = storage_service.read_stream(target["key"], backend_name=target.get("backend"))
        try:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                output.write(chunk)
                counter[0] += len(chunk)
        finally:
            stream.close()
        output.write(b"\r\n")
    output.write(f"--{boundary}--\r\n".encode())
    return output.tell()


def _validate_stow_object_sizes(targets: list[dict]) -> None:
    """Reject an oversized upload before any instance bytes reach the peer."""
    from app.services import storage_service

    total = 0
    for target in targets:
        object_size = storage_service.size(target["key"], backend_name=target.get("backend"))
        if settings.STOW_MAX_PART_BYTES and object_size > settings.STOW_MAX_PART_BYTES:
            raise ValueError("STOW-RS instance exceeds configured part size limit")
        total += object_size
        if settings.STOW_MAX_TOTAL_BYTES and total > settings.STOW_MAX_TOTAL_BYTES:
            raise ValueError("STOW-RS request exceeds configured total size limit")


def _file_body_chunks(body_file: BinaryIO, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Read a prepared multipart spool for httpx in bounded chunks."""
    body_file.seek(0)
    while True:
        chunk = body_file.read(chunk_size)
        if not chunk:
            return
        yield chunk


def _post_stow(client: httpx.Client, content: Iterator[bytes], headers: dict,
               content_length: int | None = None) -> httpx.Response:
    """POST a streamed STOW body and cap the peer's JSON response size."""
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    response_limit = settings.DICOMWEB_STOW_MAX_RESPONSE_BYTES
    if response_limit < 0:
        raise ValueError("DICOMWEB_STOW_MAX_RESPONSE_BYTES must be zero or greater")
    with client.stream("POST", "/studies", content=content, headers=headers) as response:
        payload = bytearray()
        for chunk in response.iter_bytes():
            if response_limit and len(payload) + len(chunk) > response_limit:
                raise ConnectionError("STOW-RS response exceeds configured byte limit")
            payload.extend(chunk)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=bytes(payload),
            request=response.request,
        )


def stow_upload(node, targets: list[dict], stream: bool = False) -> dict:
    """STOW-RS upload — send local instances to a remote DICOMweb service.

    `targets` is the :func:`app.dicom.sender.resolve_keys_sync` shape:
    ``[{"sop_uid", "key", "study_uid"}, ...]``. Each instance's bytes are
    read from the active storage backend in bounded chunks and POSTed as one
    ``multipart/related; type="application/dicom"`` part to
    ``{web_url}/studies`` — the REST twin of C-STORE forwarding.

    ``stream=True`` (Transfer Accelerator) streams body bytes from native
    backend streams. The standard path spools multipart framing to disk and
    streams that file with Content-Length. Neither path buffers full objects.

    Returns ``{"ok", "sent", "failed", "http_status", "results",
    "streamed", "bytes"}`` with per-instance results for auditing. Raises ValueError
    when the node is unsafe (SSRF guard re-check).

    Both paths use bounded chunks for object reads and HTTP request writes; the
    remote response is capped before JSON parsing.
    """
    import uuid

    if not targets:
        raise ValueError("no instances to upload")

    validate_web_url(node.web_url)  # per-call re-check (DNS rebinding guard)
    _validate_stow_object_sizes(targets)
    boundary = f"paxs-stow-{uuid.uuid4().hex[:16]}"

    headers = {
        **_headers(node),
        "Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}',
    }
    with httpx.Client(
        base_url=node.web_url, headers=headers,
        timeout=settings.DICOMWEB_CLIENT_TIMEOUT, follow_redirects=False,
    ) as client:
        if stream:
            counter: list[int] = []
            content = _multipart_body_chunks(targets, boundary, counter=counter)
            resp = _post_stow(client, content, headers)
        else:
            counter = [0]
            with tempfile.TemporaryFile(mode="w+b") as body_file:
                content_length = _write_multipart_spool(targets, boundary, body_file, counter)
                resp = _post_stow(
                    client, _file_body_chunks(body_file), headers,
                    content_length=content_length,
                )

    ok_status = resp.status_code in (200, 202)
    reason = None if ok_status else f"STOW-RS returned HTTP {resp.status_code}"
    succeeded_sops, failed_sops = _stow_response_sops(resp) if ok_status else (set(), set())

    sent = failed = 0
    results: list[dict] = []
    for target in targets:
        item = {
            "sop_uid": target["sop_uid"],
            "study_uid": target.get("study_uid"),
            "ok": False,
        }
        succeeded = target["sop_uid"] in succeeded_sops
        accepted_without_report = resp.status_code == 200 and not resp.content
        if (ok_status and target["sop_uid"] not in failed_sops
                and (succeeded or accepted_without_report)):
            item["ok"] = True
            sent += 1
        else:
            item["reason"] = reason or "rejected by remote STOW-RS response"
            failed += 1
        results.append(item)

    return {
        "ok": failed == 0 and sent > 0,
        "sent": sent,
        "failed": failed,
        "http_status": resp.status_code,
        "reason": reason,
        "streamed": bool(stream),
        "bytes": counter[0] if counter else 0,
        "results": results,
    }
