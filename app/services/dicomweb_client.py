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

The client runs sync (httpx) so the REST layer can wrap it in ``to_thread``
exactly like the DIMSE SCU helpers.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse

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

    with _client(node) as client:
        resp = client.get(path, params=params)
    if resp.status_code != 200:
        raise ConnectionError(f"QIDO-RS returned HTTP {resp.status_code}")

    data = resp.json()
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
def wado_retrieve(node, study_uid: str, tenant_id: int | None = None) -> dict:
    """Retrieve every instance of a study via WADO-RS and ingest it locally.

    ``GET {web_url}/studies/{uid}`` with ``Accept: multipart/related;
    type="application/dicom"`` returns one DICOM part per instance; each part
    is saved + indexed through the normal pipeline (idempotent re-import),
    tagged with `tenant_id` (VNA) for new studies. Returns the same summary
    shape as ``pull_from_node_sync`` (per-instance results for auditing).
    """
    if not study_uid:
        raise ValueError("study_uid is required for WADO-RS retrieve")

    validate_web_url(node.web_url)  # per-call re-check (DNS rebinding guard)
    pulled = failed = 0
    results: list[dict] = []
    headers = {**_headers(node), "Accept": 'multipart/related; type="application/dicom"'}
    with httpx.Client(
        base_url=node.web_url, headers=headers,
        timeout=settings.DICOMWEB_CLIENT_TIMEOUT, follow_redirects=False,
    ) as client:
        resp = client.get(f"/studies/{urllib.parse.quote(study_uid, safe='')}")
        if resp.status_code not in (200, 206):
            raise ConnectionError(f"WADO-RS returned HTTP {resp.status_code}")
        if len(resp.content) > settings.DICOMWEB_CLIENT_MAX_BYTES:
            raise ConnectionError(
                f"WADO-RS response exceeds DICOMWEB_CLIENT_MAX_BYTES "
                f"({settings.DICOMWEB_CLIENT_MAX_BYTES})"
            )

        for part in _iter_multipart_parts(resp.content, resp.headers.get("content-type", "")):
            ds = _dataset_from_bytes(part)
            if ds is None:
                failed += 1
                results.append({"sop_uid": "", "study_uid": study_uid,
                                "ok": False, "reason": "unparseable part"})
                continue
            item = {
                "sop_uid": str(getattr(ds, "SOPInstanceUID", "")),
                "study_uid": study_uid,
                "ok": False,
            }
            try:
                _ingest_dicomweb_dataset(ds, tenant_id)
                item["ok"] = True
                pulled += 1
            except Exception as exc:
                item["reason"] = str(exc)
                failed += 1
            results.append(item)

    return {"ok": failed == 0 and pulled > 0, "pulled": pulled, "failed": failed,
            "results": results}


def _iter_multipart_parts(body: bytes, content_type: str):
    """Split a multipart/related body into the raw bytes of each part."""
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


def _ingest_dicomweb_dataset(ds, tenant_id: int | None = None) -> None:
    """Save + index one WADO-RS-received instance (mirrors C-STORE ingest)."""
    import asyncio

    from app.dicom.parser import parse_metadata
    from app.db.session import AsyncSessionLocal
    from app.services import storage_service
    from app.services.indexing_service import index_new_instance

    # Multi-storage: pulled slices join the study's recorded backend.
    backend = storage_service.resolve_study_backend(
        str(getattr(ds, "StudyInstanceUID", "UNKNOWN"))
    )
    path, sha256 = storage_service.save_dataset(ds, backend_name=backend)
    meta = parse_metadata(ds)

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await index_new_instance(
                    session, meta, path, sha256,
                    performed_by="dicomweb-pull", tenant_id=tenant_id,
                    storage_backend=backend,
                )

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# STOW-RS (upload / forward)
# --------------------------------------------------------------------------- #
def _stow_failed_sops(resp: httpx.Response) -> set[str]:
    """SOP UIDs the remote explicitly reported as failed in a STOW-RS body.

    STOW-RS responses are ``application/dicom+json``; a partial failure lists
    the rejected instances under ``FailedSOPSequence`` (keyword or tag-number
    form). A 200/202 with an empty/absent body means every instance landed.
    """
    if not resp.content:
        return set()
    try:
        payload = resp.json()
    except Exception:
        return set()
    if not isinstance(payload, dict):
        return set()
    failed: set[str] = set()
    for key in ("FailedSOPSequence", "00081198"):
        seq = payload.get(key)
        if not isinstance(seq, list):
            continue
        for item in seq:
            if not isinstance(item, dict):
                continue
            sop = item.get("ReferencedSOPInstanceUID") or item.get("00081155")
            if isinstance(sop, dict):  # tag-number form: {"Value": [...]}
                values = sop.get("Value")
                sop = values[0] if isinstance(values, list) and values else None
            if sop:
                failed.add(str(sop))
    return failed


def _multipart_body_chunks(targets: list[dict], boundary: str,
                           chunk_size: int = 1024 * 1024,
                           counter: list[int] | None = None):
    """Yield a STOW-RS multipart/related body in chunks, one instance at a
    time, so httpx can POST it with ``Transfer-Encoding: chunked`` (the
    Transfer Accelerator's streamed ingest path — no temp file, no single
    in-memory body copy). Each instance's bytes are read from the active
    storage backend on demand; `counter`, when given, receives the total
    payload byte count (exclusive of multipart framing)."""
    from app.services import storage_service

    total = 0
    for target in targets:
        yield f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
        data = storage_service.read_bytes(target["key"], backend_name=target.get("backend"))
        total += len(data)
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]
        yield b"\r\n"
    yield f"--{boundary}--\r\n".encode()
    if counter is not None:
        counter.append(total)


def stow_upload(node, targets: list[dict], stream: bool = False) -> dict:
    """STOW-RS upload — send local instances to a remote DICOMweb service.

    `targets` is the :func:`app.dicom.sender.resolve_keys_sync` shape:
    ``[{"sop_uid", "key", "study_uid"}, ...]``. Each instance's bytes are
    read from the active storage backend (S3 objects materialize in memory)
    and POSTed as one ``multipart/related; type="application/dicom"`` part to
    ``{web_url}/studies`` — the REST twin of C-STORE forwarding.

    ``stream=True`` (Transfer Accelerator) streams the body with chunked
    transfer encoding straight from the backend — no temp file, no single
    in-memory copy — so the receiver ingests parts as they arrive.

    Returns ``{"ok", "sent", "failed", "http_status", "results",
    "streamed", "bytes"}`` with per-instance results for auditing; HTTP 409
    (duplicates on the remote) is treated as success, and instances named in
    the response's ``FailedSOPSequence`` are marked failed. Raises ValueError
    when the node is unsafe (SSRF guard re-check).

    Memory (non-stream): the multipart body is written part-by-part into a
    temp file (no list of per-part byte strings), so only one full body copy
    is ever in RAM — same profile as the C-STORE forward path, which also
    materializes on disk.
    """
    import os
    import tempfile
    import uuid

    from app.services import storage_service

    if not targets:
        raise ValueError("no instances to upload")

    validate_web_url(node.web_url)  # per-call re-check (DNS rebinding guard)
    boundary = f"paxs-stow-{uuid.uuid4().hex[:16]}"

    if stream:
        counter: list[int] = []
        content = _multipart_body_chunks(targets, boundary, counter=counter)
        body_bytes = None
    else:
        counter = []
        fd, tmp_path = tempfile.mkstemp(prefix="paxs-stow-", suffix=".mp")
        try:
            with os.fdopen(fd, "wb") as fh:
                for target in targets:
                    fh.write(f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode())
                    data = storage_service.read_bytes(target["key"], backend_name=target.get("backend"))
                    counter.append(len(data))
                    fh.write(data)
                    fh.write(b"\r\n")
                fh.write(f"--{boundary}--\r\n".encode())
            with open(tmp_path, "rb") as fh:
                body_bytes = fh.read()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        content = body_bytes

    headers = {
        **_headers(node),
        "Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}',
    }
    with httpx.Client(
        base_url=node.web_url, headers=headers,
        timeout=settings.DICOMWEB_CLIENT_TIMEOUT, follow_redirects=False,
    ) as client:
        resp = client.post("/studies", content=content)

    ok_status = resp.status_code in (200, 202, 409)
    reason = None if ok_status else f"STOW-RS returned HTTP {resp.status_code}"
    failed_sops = _stow_failed_sops(resp) if ok_status else set()

    sent = failed = 0
    results: list[dict] = []
    for target in targets:
        item = {
            "sop_uid": target["sop_uid"],
            "study_uid": target.get("study_uid"),
            "ok": False,
        }
        if ok_status and target["sop_uid"] not in failed_sops:
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
        "bytes": sum(counter),
        "results": results,
    }
