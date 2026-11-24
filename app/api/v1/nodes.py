"""DICOM node registry management API — register AE destinations so C-MOVE
and C-STORE forwarding (SCU) can resolve them, plus a C-ECHO connectivity
probe and a send-to-node trigger."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_user,
    get_current_user_any,
    require_admin,
    require_destructive_role,
)
from app.core.config import get_settings
from app.db.models import DicomNode, User
from app.db.session import get_db
from app.services import node_service, tenant_service
from app.services.audit_service import add_audit

log = logging.getLogger("paxs.api.nodes")

router = APIRouter(prefix="/api/v1/nodes", tags=["dicom-nodes"])
REMOTE_FIND_MAX_RESULTS = 200


def _log_remote_node_exception(operation: str, ae_title: str, exc: Exception) -> None:
    """Log the transport failure without peer response text or patient data."""
    log.warning(
        "Remote node operation failed (operation=%s, ae_title=%s, error_type=%s)",
        operation,
        ae_title,
        type(exc).__name__,
    )


class NodeIn(BaseModel):
    ae_title: str = Field(min_length=1, max_length=64, examples=["ARCHIVE_A"])
    host: str = Field(min_length=1, max_length=255, examples=["10.0.0.5"])
    port: int = Field(ge=1, le=65535, examples=[104])
    description: str | None = Field(default=None, max_length=255)
    enabled: bool = True
    # Optional DICOMweb REST transport (QIDO-RS / WADO-RS). Empty web_url =
    # DIMSE-only node. web_username/web_password enable HTTP Basic auth.
    web_url: str | None = Field(default=None, max_length=512)
    web_username: str | None = Field(default=None, max_length=128)
    web_password: str | None = Field(default=None, max_length=256)
    # B-16: path to this peer's PEM certificate. The SCU pins it as its trust
    # anchor when no global DICOM_TLS_CA is configured; without one of the two a
    # production SCU refuses the TLS association rather than trusting any peer.
    trust_cert: str | None = Field(default=None, max_length=512)


class SendScope(BaseModel):
    """Which instances to forward to a node. At least one scope is required."""
    study_uids: list[str] = Field(default_factory=list, examples=[["1.2.840.113619.2.408.77"]])
    series_uids: list[str] = Field(default_factory=list)
    instance_uids: list[str] = Field(default_factory=list)
    our_ae_title: str = Field(default="PAXS_SCU", min_length=1, max_length=16)


FIND_LEVELS = ("PATIENT", "STUDY", "SERIES", "INSTANCE")


class FindQuery(BaseModel):
    """A C-FIND query against a remote node.

    `filters` maps DICOM keywords to match values; an empty string requests
    the field be returned (DICOM C-FIND semantics), a trailing ``*`` is a
    wildcard. Examples: ``{"PatientID": "MRN*", "StudyDate": ""}``.
    """
    level: str = Field(default="STUDY", pattern="^(?i)(PATIENT|STUDY|SERIES|INSTANCE)$")
    filters: dict[str, str] = Field(default_factory=dict)
    our_ae_title: str = Field(default="PAXS_SCU", min_length=1, max_length=16)


class WebRetrieveQuery(FindQuery):
    """WADO-RS retrieve request — FindQuery plus lifecycle tiering options.

    ``auto_archive`` tiers the pulled study to the cold ``.archive/`` prefix
    immediately after ingest (Study Lifecycle Management — item 14), so a
    remote-study pull can land directly in cold storage instead of the hot
    tier. The global ``LIFECYCLE_AUTO_ARCHIVE_WADO_PULL`` setting does the
    same for every pull when enabled.
    """
    auto_archive: bool = Field(default=False, description="Tier the pulled study to cold storage (ARCHIVE) after ingest")


def _node_out(node: DicomNode) -> dict:
    from app.dicom.dcmtk_move import configured_for

    return {
        "ae_title": node.ae_title,
        "host": node.host,
        "port": node.port,
        "description": node.description,
        "enabled": node.enabled,
        "web_url": node.web_url,
        "trust_cert": node.trust_cert,
        "dcmtk_move": configured_for(node.trust_cert),
        "has_web_auth": bool(node.web_username or node.web_password),
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }


@router.get("")
async def list_nodes(db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user_any)):
    nodes = await node_service.list_nodes(db)
    return [_node_out(n) for n in nodes]


@router.post("", status_code=201)
async def create_node(body: NodeIn, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    if user.role != "ADMIN":
        raise HTTPException(403, "Only ADMIN can register DICOM nodes")
    if await node_service.node_exists(db, body.ae_title):
        raise HTTPException(409, f"Node '{body.ae_title}' already registered")
    if body.web_url:
        try:
            from app.services.dicomweb_client import validate_web_url

            body = body.model_copy(update={"web_url": validate_web_url(body.web_url)})
        except ValueError as exc:
            raise HTTPException(422, f"Invalid web_url: {exc}") from exc
    node = await node_service.create_node(db, body.model_dump())
    log.info("DICOM node %s (%s:%s) registered by %s",
             body.ae_title, body.host, body.port, user.username)
    return _node_out(node)


@router.put("/{ae_title}")
async def update_node(ae_title: str, body: NodeIn, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    if user.role != "ADMIN":
        raise HTTPException(403, "Only ADMIN can update DICOM nodes")
    node = await node_service.get_node(db, ae_title)
    if node is None:
        raise HTTPException(404, "Node not found")
    data = body.model_dump()
    if data.get("web_url"):
        try:
            from app.services.dicomweb_client import validate_web_url

            data["web_url"] = validate_web_url(data["web_url"])
        except ValueError as exc:
            raise HTTPException(422, f"Invalid web_url: {exc}") from exc
    await node_service.update_node(db, node, data)
    log.info("DICOM node %s updated by %s", ae_title, user.username)
    return _node_out(node)


@router.delete("/{ae_title}", status_code=204)
async def delete_node(ae_title: str, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    if user.role != "ADMIN":
        raise HTTPException(403, "Only ADMIN can delete DICOM nodes")
    node = await node_service.get_node(db, ae_title)
    if node is None:
        raise HTTPException(404, "Node not found")
    await node_service.delete_node(db, node)
    log.info("DICOM node %s removed by %s", ae_title, user.username)


@router.get("/dcmtk/status")
async def dcmtk_status(user: User = Depends(require_admin)):
    from app.dicom import dcmtk

    return await asyncio.to_thread(dcmtk.status)


@router.post("/{ae_title}/ping")
async def ping_node(ae_title: str, db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """C-ECHO (verification) against the registered node — checks reachability
    and that the peer really speaks DICOM."""
    node = await node_service.get_node(db, ae_title)
    if node is None:
        raise HTTPException(404, "Node not found")
    if not node.enabled:
        raise HTTPException(409, "Node is disabled")

    from app.dicom.sender import send_echo

    try:
        # Off the event loop: a C-ECHO opens a DIMSE association (up to ~30 s
        # against an unreachable peer) and must not stall every other request.
        settings = get_settings()
        trust_cert = getattr(node, "trust_cert", None)
        if settings.DCMTK_BIN_DIR and not (settings.DICOM_TLS_ENABLED or trust_cert):
            from app.dicom import dcmtk

            result = await asyncio.to_thread(dcmtk.send_echo, node.host, node.port, node.ae_title)
        else:
            result = await asyncio.to_thread(
                send_echo, node.host, node.port, node.ae_title, trust_cert=trust_cert,
            )
    except Exception as exc:  # peer libraries may include response data in exception text
        _log_remote_node_exception("C-ECHO", ae_title, exc)
        return {"ae_title": ae_title, "ok": False, "reason": "Remote C-ECHO failed"}
    return {"ae_title": ae_title, **result}


@router.post("/{ae_title}/send")
async def send_to_node(ae_title: str, body: SendScope,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_destructive_role)):
    """C-STORE SCU trigger: forward matching instances to a registered node.

    Accepts study / series / instance UID scopes (at least one). Non-admin
    users are VNA-scoped to their own tenant — forwarding a study outside the
    caller's tenant matches nothing. Every successful transfer is audit-logged
    as ``FORWARDED``.

    The DIMSE transfer runs in a worker thread so the event loop stays free.
    """
    if not (body.study_uids or body.series_uids or body.instance_uids):
        raise HTTPException(422, "Provide at least one of study_uids, series_uids or instance_uids")

    node = await node_service.get_node(db, ae_title)
    if node is None:
        raise HTTPException(404, "Node not found")
    if not node.enabled:
        raise HTTPException(409, "Node is disabled")

    from app.dicom.sender import send_to_node_sync

    tenant_id, include_all = tenant_service.scope_from_user(user)
    try:
        result = await asyncio.to_thread(
            send_to_node_sync, ae_title, body.study_uids, body.series_uids,
            body.instance_uids, body.our_ae_title, tenant_id, include_all,
        )
    except Exception as exc:  # transport exceptions may contain DICOM values or local paths
        _log_remote_node_exception("C-STORE", ae_title, exc)
        return {"ae_title": ae_title, "ok": False, "reason": "C-STORE send failed", "sent": 0, "failed": 0, "results": []}

    if not result.get("ok") and result.get("sent", 0) == 0 and result.get("failed", 0) == 0:
        # Node-level or scope-level failure (missing node handled above):
        # nothing was even attempted — report it as not-found (also keeps VNA
        # isolation opaque: a study in another tenant looks missing).
        raise HTTPException(404, result.get("reason") or "No matching instances")

    # Audit every instance that actually transferred.
    for item in result.get("results", []):
        if item.get("ok"):
            add_audit(db, item["sop_uid"], "FORWARDED",
                      performed_by=user.username,
                      study_instance_uid=item.get("study_uid"),
                      file_path=None)
    await db.flush()
    return {"ae_title": ae_title, **result}


@router.post("/{ae_title}/find")
async def find_on_node(ae_title: str, body: FindQuery,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_admin)):
    """C-FIND SCU trigger: query a remote node (Q/R information model) and
    return the matches. Runs in a worker thread; the DICOM transfer never
    blocks the event loop.

    The remote archive decides what it returns — this endpoint only proxies
    the C-FIND, so no local tenant scoping applies (matches are the peer's
    own data). A node that rejects the query surfaces as a 502.
    """
    node = await node_service.get_node(db, ae_title)
    if node is None:
        raise HTTPException(404, "Node not found")
    if not node.enabled:
        raise HTTPException(409, "Node is disabled")

    from app.dicom.sender import query_remote_node_sync

    try:
        result = await asyncio.to_thread(
            query_remote_node_sync, ae_title, body.level, body.filters,
            body.our_ae_title, REMOTE_FIND_MAX_RESULTS,
        )
    except Exception as exc:  # peer exceptions may include query or response values
        _log_remote_node_exception("C-FIND", ae_title, exc)
        return {"ae_title": ae_title, "ok": False, "reason": "Remote C-FIND failed", "matches": []}

    if not result.get("ok"):
        raise HTTPException(502, result.get("reason") or "Remote C-FIND failed")
    result["level"] = body.level.upper()
    result["ae_title"] = ae_title
    return result


@router.post("/{ae_title}/pull")
async def pull_from_node(ae_title: str, body: FindQuery,
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_admin)):
    """C-GET SCU trigger: pull matching instances from a remote node and
    ingest them into the local archive.

    Same query shape as `/find` (level + DICOM keyword filters, e.g.
    ``{"StudyInstanceUID": "1.2.840..."}``). Received instances are stored
    and indexed through the normal C-STORE pipeline (idempotent re-import);
    VNA: studies created by the pull are tagged with the caller's tenant.
    Every ingested instance is audit-logged as ``PULLED``. The DIMSE transfer
    runs in a worker thread so the event loop stays free.
    """
    node = await node_service.get_node(db, ae_title)
    if node is None:
        raise HTTPException(404, "Node not found")
    if not node.enabled:
        raise HTTPException(409, "Node is disabled")

    # C-21: end the request's transaction *before* the transfer is handed to a
    # worker thread. Every received instance is stored and indexed from that
    # thread in its own session, while this session still holds an open
    # transaction — a read is enough, because the engine's begin listener opens
    # one on the first statement and SQLite's rollback journal cannot be
    # upgraded to EXCLUSIVE while a reader holds SHARED. The worker then waits
    # out the whole ``busy_timeout`` (30 s), and the pull answers 502
    # "ingestion failed" for instances that were never actually at fault.
    # Nothing below re-reads ``node``, so an expiring commit is safe here.
    await db.commit()

    from app.dicom.sender import pull_from_node_sync

    tenant_id, include_all = tenant_service.scope_from_user(user)
    try:
        result = await asyncio.to_thread(
            pull_from_node_sync, ae_title, body.level, body.filters,
            body.our_ae_title, tenant_id, include_all=include_all,
        )
    except Exception as exc:  # peer exceptions may include query or response values
        _log_remote_node_exception("C-GET", ae_title, exc)
        return {"ae_title": ae_title, "ok": False, "reason": "Remote C-GET failed",
                "pulled": 0, "failed": 0, "results": []}

    if not result.get("ok"):
        raise HTTPException(502, result.get("reason") or "Remote C-GET failed")
    if result.get("pulled", 0) == 0 and result.get("failed", 0) > 0:
        # The transfer worked but nothing could be stored — surface it as a
        # failure so callers don't mistake it for an empty match.
        raise HTTPException(502, "Received instances failed to ingest")

    # Audit every instance that was actually ingested.
    for item in result.get("results", []):
        if item.get("ok"):
            add_audit(db, item["sop_uid"], "PULLED",
                      performed_by=user.username,
                      study_instance_uid=item.get("study_uid"),
                      file_path=None)
    await db.flush()
    return {"ae_title": ae_title, **result}


# --------------------------------------------------------------------------- #
# DICOMweb transport (QIDO-RS query / WADO-RS retrieve)
# --------------------------------------------------------------------------- #
async def _web_node(ae_title: str, db: AsyncSession) -> DicomNode:
    """Load a node and require it to be enabled with a DICOMweb base URL."""
    node = await node_service.get_node(db, ae_title)
    if node is None:
        raise HTTPException(404, "Node not found")
    if not node.enabled:
        raise HTTPException(409, "Node is disabled")
    if not node.web_url:
        raise HTTPException(409, "Node has no DICOMweb base URL (set web_url)")
    return node


@router.post("/{ae_title}/web/query")
async def web_query_node(ae_title: str, body: FindQuery,
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_admin)):
    """QIDO-RS query against a node's DICOMweb service (C-FIND's REST twin).

    Same query shape as ``/find`` (level + DICOM keyword filters). The remote
    DICOMweb service answers with ``application/dicom+json``; matches are
    normalized to the same dashboard row shape C-FIND returns. The HTTP
    request runs in a worker thread; ``web_url`` is SSRF-guarded at creation
    time and re-validated per call. Runs in a worker thread so the event
    loop stays free.
    """
    node = await _web_node(ae_title, db)
    from app.services.dicomweb_client import qido_query, qido_to_match_rows

    try:
        matches = await asyncio.to_thread(qido_query, node, body.level, body.filters)
    except ValueError as exc:  # SSRF guard re-check
        raise HTTPException(422, f"Unsafe web_url: {exc}") from exc
    except Exception as exc:  # peer exceptions may include query or response values
        _log_remote_node_exception("DICOMweb QIDO-RS", ae_title, exc)
        return {"ae_title": ae_title, "ok": False, "reason": "Remote QIDO-RS query failed", "matches": []}

    rows = qido_to_match_rows(body.level, matches)
    response = {
        "ae_title": ae_title,
        "ok": True,
        "transport": "dicomweb",
        "level": body.level.upper(),
        "matches": rows,
    }
    if len(matches) >= REMOTE_FIND_MAX_RESULTS:
        response["truncated"] = True
    return response


def _detached_node(node: DicomNode):
    """A plain, session-independent copy of the fields a web transfer needs.

    C-21: the WADO-RS retrieve hands the node to a worker thread while the
    request session is released (see ``web_retrieve_node``). Passing the ORM
    instance across that boundary would risk a lazy load on a detached object
    (``MissingGreenlet`` / ``DetachedInstanceError``) from a thread that has no
    greenlet to await it. Only these four fields are ever read by
    ``remote_wado_service`` / ``dicomweb_client``, so they are copied eagerly,
    while the session is still open.
    """
    import types

    return types.SimpleNamespace(
        ae_title=node.ae_title,
        web_url=node.web_url,
        web_username=node.web_username,
        web_password=node.web_password,
    )


@router.post("/{ae_title}/web/retrieve")
async def web_retrieve_node(ae_title: str, body: WebRetrieveQuery,
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_admin)):
    """WADO-RS retrieve from a node's DICOMweb service (C-GET's REST twin).

    Retrieves the study named by ``filters["StudyInstanceUID"]`` over
    ``GET /studies/{uid}`` (multipart/related; type="application/dicom") and
    ingests every instance through the normal pipeline (idempotent
    re-import). VNA: new studies are tagged with the caller's tenant.
    Each ingested instance is audit-logged as ``PULLED``. Runs in a worker
    thread so the event loop stays free.

    Lifecycle tiering: when ``auto_archive`` is set (or the global
    ``LIFECYCLE_AUTO_ARCHIVE_WADO_PULL`` setting is on), the pulled study is
    tiered to the cold ``.archive/`` prefix right after ingest — a WADO pull
    can land directly in cold storage.
    """
    node = await _web_node(ae_title, db)
    patient_id = ((body.filters or {}).get("PatientID") or "").strip()
    study_uid = ((body.filters or {}).get("StudyInstanceUID") or "").strip()

    if not study_uid and not patient_id:
        raise HTTPException(422, "PatientID or StudyInstanceUID is required for retrieve")

    from app.core.config import settings
    from app.services.dicomweb_client import qido_query, wado_retrieve
    from app.services.lifecycle_service import archive_study
    from app.services.remote_wado_service import WadoSizeLimitExceeded

    tenant_id, include_all = tenant_service.scope_from_user(user)
    auto_archive = body.auto_archive or settings.LIFECYCLE_AUTO_ARCHIVE_WADO_PULL

    # C-21: end the request's transaction *before* the ingest work is handed to
    # a worker thread. The ingest path opens its own session per instance and
    # commits it there, while the request session still holds an open
    # transaction — a read is enough, because the engine's begin listener opens
    # one on the first statement and SQLite's rollback journal cannot be
    # upgraded to EXCLUSIVE while a reader holds SHARED. The worker then waits
    # on a lock held by a request that is itself waiting on the worker, and the
    # retrieve fails with ``sqlite3.OperationalError: database is locked`` →
    # HTTP 502 on a pull that had actually succeeded.
    #
    # The node's fields are copied first so nothing in the thread touches the
    # ORM instance, and ``expire_on_commit=False`` keeps the rest of this handler
    # from re-reading it either.
    node = _detached_node(node)
    await db.commit()

    study_uids = [study_uid] if study_uid else []
    if not study_uids and patient_id:
        try:
            matches = await asyncio.to_thread(qido_query, node, "STUDY", {"PatientID": patient_id})
            for m in matches:
                # m is a QIDO JSON dataset dict
                uid_tag = m.get("0020000D", {}).get("Value", [])
                if uid_tag and uid_tag[0]:
                    study_uids.append(str(uid_tag[0]))
        except Exception:
            # The remote transport/parser exception may embed PHI or response
            # content; keep this network boundary diagnostic PHI-free.
            log.warning("QIDO-RS study lookup on remote node %s failed", ae_title)

    if not study_uids:
        raise HTTPException(404, "No matching studies found on the remote node")

    total_pulled = 0
    total_failed = 0
    all_results = []

    for suid in study_uids:
        try:
            res = await asyncio.to_thread(
                wado_retrieve, node, suid, tenant_id, include_all=include_all
            )
            total_pulled += res.get("pulled", 0)
            total_failed += res.get("failed", 0)
            all_results.extend(res.get("results", []))
        except WadoSizeLimitExceeded as exc:
            # B-14/FP-11: a peer that advertises (or streams) more than the cap
            # is a policy refusal, not a transient remote error. Surface it as
            # 413 and stop — retrying the next study would just repeat the DoS.
            log.warning("Remote WADO-RS retrieve refused by the size cap: %s", exc)
            raise HTTPException(413, str(exc)) from None
        except Exception as exc:  # peer exceptions may include response data or local paths
            _log_remote_node_exception("DICOMweb WADO-RS", ae_title, exc)

    # A retrieve only "succeeded" if at least one instance actually landed.
    # Zero parts back (empty multipart / study not found remotely) or an
    # exception on every study is a failure, not an empty success. Partial
    # success (>=1 pulled) stays 200 with per-instance failures in `results`.
    result = {"ok": total_pulled > 0, "pulled": total_pulled, "failed": total_failed, "results": all_results}

    if not result.get("ok"):
        raise HTTPException(502, result.get("reason") or "Remote WADO-RS returned no instances")

    # Audit every instance that was actually ingested.
    for item in result.get("results", []):
        if item.get("ok"):
            add_audit(db, item["sop_uid"], "PULLED",
                      performed_by=user.username,
                      study_instance_uid=item.get("study_uid"),
                      file_path=None)
    await db.commit()

    # Lifecycle tiering: tier the pulled study to cold storage on demand.
    # `archive_study` opens its own sync session — commit the request
    # transaction first so SQLite's single-writer lock isn't contended.
    archive_result = None
    if auto_archive:
        archive_result = await asyncio.to_thread(
            archive_study, study_uid, performed_by=f"wado-pull:{user.username}"
        )
        log.info("WADO pull %s auto-archived to cold tier: %s", study_uid, archive_result)

    return {
        "ae_title": ae_title, **result, "transport": "dicomweb",
        "auto_archived": bool(auto_archive and archive_result and archive_result.get("ok")),
        "archive_state": (archive_result or {}).get("state"),
        "archive_error": None if not auto_archive or (archive_result or {}).get("ok")
        else (archive_result or {}).get("error"),
    }


@router.post("/{ae_title}/web/upload")
async def web_upload_node(ae_title: str, body: SendScope,
                          accelerated: bool = False,
                          workers: int | None = Query(default=None, ge=1, le=16),
                          progress: bool = False,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_destructive_role)):
    """STOW-RS upload — send matching local instances to a node's DICOMweb
    service (``POST {web_url}/studies``, multipart/related).

    Same scope as ``/send`` (study / series / instance UIDs) but the transport
    is DICOMweb STOW-RS instead of a DIMSE C-STORE association — a peer that
    exposes a web service can receive studies without us ever opening a DIMSE
    association to it. Non-admin users are VNA-scoped to their own tenant
    (forwarding a study outside the caller's tenant matches nothing). Every
    transferred instance is audit-logged as ``FORWARDED``. The HTTP transfer
    runs in a worker thread so the event loop stays free.

    Transfer Accelerator (feature 19): ``?accelerated=1`` splits the study's
    instances into batches and pushes them over N parallel streamed STOW-RS
    connections (chunked transfer encoding) — the HTTP twin of C-STORE that
    can move a large study much faster than one sequential association.
    ``workers`` overrides the configured connection count (1-16).
    """
    if not (body.study_uids or body.series_uids or body.instance_uids):
        raise HTTPException(422, "Provide at least one of study_uids, series_uids or instance_uids")

    node = await _web_node(ae_title, db)  # enabled + has web_url

    from app.dicom.sender import resolve_keys_sync

    tenant_id, include_all = tenant_service.scope_from_user(user)
    targets = resolve_keys_sync(
        body.study_uids, body.series_uids, body.instance_uids,
        tenant_id=tenant_id, include_all=include_all,
    )
    if not targets:
        raise HTTPException(404, "No matching instances")

    if progress:
        # NDJSON stream lets the Transfer Accelerator panel show progress.
        return _stream_web_upload(
            SimpleNamespace(web_url=node.web_url,
                            web_username=node.web_username,
                            web_password=node.web_password),
            targets, accelerated=accelerated, workers=workers,
            db=db, performed_by=user.username, ae_title=ae_title,
        )

    from app.services.node_transfer_service import TransferCapacityError, run_stow_upload

    try:
        result = await run_stow_upload(
            node, targets, accelerated=accelerated, workers=workers
        )
    except TransferCapacityError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:  # SSRF guard re-check
        raise HTTPException(422, f"Unsafe web_url: {exc}") from exc
    except Exception as exc:  # remote transport exceptions may include peer response text
        log.warning("DICOMweb STOW upload to %s failed (error_type=%s)",
                    ae_title, type(exc).__name__)
        return {"ae_title": ae_title, "ok": False,
                "reason": "DICOMweb STOW transfer failed",
                "sent": 0, "failed": 0, "results": []}

    if result.get("sent", 0) == 0 and result.get("failed", 0) > 0:
        # The HTTP request worked but every instance was rejected (or the
        # remote answered with an error status) — surface it as a failure.
        raise HTTPException(502, result.get("reason") or "STOW-RS upload failed")

    # Audit every instance that actually transferred.
    for item in result.get("results", []):
        if item.get("ok"):
            add_audit(db, item["sop_uid"], "FORWARDED",
                      performed_by=user.username,
                      study_instance_uid=item.get("study_uid"),
                      file_path=None)
    await db.flush()
    return {"ae_title": ae_title, "transport": "dicomweb", **result}


def _stream_web_upload(
    node: Any, targets: list[dict], *, accelerated: bool, workers: int | None,
    db: AsyncSession, performed_by: str, ae_title: str,
) -> StreamingResponse:
    """Adapt the transfer service event iterator to an NDJSON HTTP response."""
    from app.services.node_transfer_service import (
        TransferCapacityError,
        stream_stow_events,
    )

    try:
        events = stream_stow_events(
            node, targets, accelerated=accelerated, workers=workers, db=db,
            performed_by=performed_by, ae_title=ae_title,
        )
    except TransferCapacityError as exc:
        raise HTTPException(503, str(exc)) from exc
    return StreamingResponse(events, media_type="application/x-ndjson")
