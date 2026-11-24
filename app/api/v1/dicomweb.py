"""DICOMweb (PS3.18) REST API:

  QIDO-RS  GET  /dicom-web/studies[?filters]
           GET  /dicom-web/studies/{study}/series[?filters]
           GET  /dicom-web/studies/{study}/series/{series}/instances[?filters]
  WADO-RS  GET  /dicom-web/studies/{study}/metadata
           GET  /dicom-web/studies/{study}                       (multipart)
           GET  /dicom-web/studies/{study}/series/{series}       (multipart)
           GET  /dicom-web/studies/{study}/series/{series}/instances/{sop}
           GET  .../instances/{sop}/metadata
           GET  .../instances/{sop}/rendered?window=40,400&viewport=1024,1024
           GET  .../instances/{sop}/frames/{n}
  STOW-RS  POST /dicom-web/studies                               (multipart)
"""

from __future__ import annotations

import asyncio
import gzip
import time
import io
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    current_scope,
    current_share_study,
    get_optional_user,
    get_share_scope,
    require_dicomweb_access,
    require_study_access,
)
from app.core import dicom_uids
from app.core.concurrency import run_blocking
from app.core.config import settings
from app.core.response_admission import (
    AdmittedStreamingResponse,
    release_response_bytes,
    reserve_response_bytes,
)
from app.db.models import Instance, User
from app.db.session import AsyncSessionLocal, get_db
from app.dicom.parser import parse_metadata
from app.services import (
    content_version,
    indexing_service,
    query_service,
    storage_service,
    tenant_service,
    warm_service,
)
from app.services.dicomweb_service import (
    MalformedStowMultipart,
    StowPartTooLarge,
    build_multipart,
    dataset_from_path,
    dataset_to_dicom_json,
    dataset_to_json_text,
    instance_to_dicom_json,
    iter_stow_parts_spooled,
    new_boundary,
    series_to_dicom_json,
    study_to_dicom_json,
)
from app.services.indexing_service import CrossTenantIngestError, index_new_instance
from app.services.quota_service import QuotaExceeded

log = logging.getLogger("paxs.api.dicomweb")

#: U1.7: machine-readable reason on an otherwise unchanged 404, so the viewer can
#: say "indexed but the file is missing" instead of a generic "Image Unavailable".
ERROR_CODE_HEADER = "X-PAXS-Error-Code"
OBJECT_MISSING = "object-missing"


def _object_missing(sop_uid: str) -> HTTPException:
    """404 for an indexed instance whose stored object is gone.

    Only raised after the tenant-scoped lookup found the row (N-1), so the hint
    never reveals another tenant's UID. Status and body are the public
    "Instance not found" contract; only the header is added.
    """
    log.warning("indexed instance has no stored object sop=%s", sop_uid)
    return HTTPException(404, "Instance not found", headers={ERROR_CODE_HEADER: OBJECT_MISSING})

router = APIRouter(
    prefix="/dicom-web",
    tags=["DICOMweb"],
    # C03: the whole DICOMweb surface is guarded here, so every current and
    # future route is covered by construction. With DICOMWEB_PUBLIC_READ=false reads
    # require a bearer token or session cookie — or a valid patient share token,
    # which only ever grants read access to the study it names. Writes (STOW-RS)
    # always require a real identity in private mode.
    dependencies=[Depends(require_dicomweb_access)],
)

MAX_LIMIT = 1000


def _bk(row) -> str | None:
    """Named storage backend for an instance row (multi-storage); rows without
    one (e.g. presentation states) resolve to the env backend."""
    return getattr(row, "storage_backend", None)


def _paginate(params: dict) -> tuple[int, int]:
    try:
        limit = min(int(params.pop("limit", 100)), MAX_LIMIT)
    except (TypeError, ValueError) as exc:  # a client typo, not a server fault
        # QIDO paging is advisory: fall back to the documented default and keep
        # the reason visible at debug instead of failing the query.
        log.debug("qido_limit_invalid error_type=%s", type(exc).__name__)
        limit = 100
    try:
        offset = max(int(params.pop("offset", 0)), 0)
    except (TypeError, ValueError) as exc:  # a client typo, not a server fault
        log.debug("qido_offset_invalid error_type=%s", type(exc).__name__)
        offset = 0
    return limit, offset


def _keyset_cursor(raw: str | None) -> tuple[str | None, int] | None:
    """Parse an opaque keyset cursor ``"<study_date>|<id>"`` -> (date, id).

    ``study_date`` is the DA string (may be empty for NULL dates — they sort
    last in the default desc order); ``id`` is the study row id. Returns None
    when absent/malformed so callers can fall back to offset pagination.
    """
    if not raw:
        return None
    try:
        date_part, _, id_part = raw.rpartition("|")
        cursor_id = int(id_part)
    except (TypeError, ValueError, AttributeError):
        return None
    return (date_part or None, cursor_id)


# --------------------------------------------------------------------------- #
# Caching — pixel data / frames /
# rendered images can be cached aggressively (huge win for CT/MRI stacks:
# re-opening a study or scrolling back over slices already fetched hits the
# browser cache instead of round-tripping disk read + decode again).
#
# C-04: but a SOP UID is NOT immutable here: re-ingest, compression, lossy
# transcode and split/merge rewrite the bytes behind it. `immutable, max-age=1y`
# let a workstation keep showing pre-correction pixels for a year. Responses are
# now `no-cache` (store, but revalidate before reuse) with a strong ETag built
# from (instance id, content_version, representation): a revalidation is a DB
# row lookup and a 304, never an object read.
# --------------------------------------------------------------------------- #
REVALIDATE_CACHE_CONTROL = "private, no-cache"


def _etag_matches(request: Request, etag: str) -> bool:
    """RFC 9110 section 13.1.2 If-None-Match (weak comparison, list or ``*``)."""
    header = request.headers.get("if-none-match")
    if not header:
        return False
    if header.strip() == "*":
        return True
    bare = etag.removeprefix("W/")
    return any(part.strip().removeprefix("W/") == bare for part in header.split(","))


def _not_modified(request: Request, etag: str) -> Response | None:
    """A 304 for a client that already holds this representation, else None."""
    if _etag_matches(request, etag):
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": REVALIDATE_CACHE_CONTROL},
        )
    return None


def _streaming_instance_response(inst: Instance, etag: str) -> StreamingResponse:
    """Stream one stored Part-10 object under response-byte admission (H-10).

    ``read_bytes`` used to load the whole object (a tomosynthesis or cine
    instance can be 1-2 GB) before the first byte left. The caller has already
    answered a matching ``If-None-Match`` from the identity ETag, so a 304
    costs no object I/O at all.
    """
    reserved_bytes = max(0, int(inst.file_size_bytes or 0))
    reserve_response_bytes(reserved_bytes)
    try:
        return AdmittedStreamingResponse(
            storage_service.iter_bytes(inst.file_path, backend_name=_bk(inst)),
            media_type="application/dicom",
            reserved_bytes=reserved_bytes,
            headers={"ETag": etag, "Cache-Control": REVALIDATE_CACHE_CONTROL},
        )
    except BaseException:
        release_response_bytes(reserved_bytes)
        raise


def _cached_response(request: Request, data: bytes, media_type: str, etag: str) -> Response:
    """Response carrying the identity ETag; 304 when the client already has it."""
    not_modified = _not_modified(request, etag)
    if not_modified is not None:
        return not_modified
    headers = {"ETag": etag, "Cache-Control": REVALIDATE_CACHE_CONTROL}
    return Response(content=data, media_type=media_type, headers=headers)


# --------------------------------------------------------------------------- #
# WADO-URI (PS3.18 Annex A, simple HTTP retrieval for legacy clients)
# --------------------------------------------------------------------------- #
@router.get("/wado")
async def wado_uri(request: Request, db: AsyncSession = Depends(get_db)):
    """WADO-URI: retrieve one instance (or its rendered image) by UID query
    params, e.g.

      /dicom-web/wado?requestType=WADO&studyUID=...&seriesUID=...&objectUID=...
      /dicom-web/wado?requestType=WADO&studyUID=...&objectUID=...&contentType=image/jpeg

    Returns the DICOM part-10 object by default, or a rendered JPEG/PNG when
    contentType is image/jpeg or image/png.
    """
    params = request.query_params
    if str(params.get("requestType", "")).upper() != "WADO":
        raise HTTPException(400, "Missing/invalid requestType (must be WADO)")
    study_uid = params.get("studyUID") or params.get("StudyInstanceUID")
    series_uid = params.get("seriesUID") or params.get("SeriesInstanceUID")
    object_uid = params.get("objectUID") or params.get("SOPInstanceUID")
    if not object_uid:
        raise HTTPException(400, "Missing objectUID (SOP Instance UID)")

    _tenant_id, _include_all = current_scope()
    # C-02: a share-token caller's archive-wide scope is only valid inside the
    # one study the token names. Pin the lookup to it; a different studyUID in
    # the query string is a 404 (N-1), never a widening.
    share_study = current_share_study()
    if share_study is not None:
        if study_uid and study_uid != share_study:
            raise HTTPException(404, "Instance not found")
        study_uid = share_study
    inst = await query_service.find_instance(
        db, study_uid or None, series_uid or None, object_uid,
        tenant_id=_tenant_id, include_all=_include_all,
    )
    if inst is None:
        raise HTTPException(404, "Instance not found")
    if not await run_blocking(storage_service.exists, inst.file_path, backend_name=_bk(inst)):
        raise _object_missing(object_uid)

    # C04: WADO-URI takes the object UID from the query string, so the owning
    # study is resolved here and the caller's tenant (or share scope) is
    # enforced against it — a share link must never retrieve another study's
    # instance, and a tenant user must not reach across institutions.
    owning_study = await query_service.owning_study_uid(db, inst.sop_instance_uid)
    _wado_user = await get_optional_user(request, None)
    if owning_study is not None and not (_wado_user is None and settings.DICOMWEB_PUBLIC_READ):
        await require_study_access(
            db,
            owning_study,
            _wado_user,
            share_scope=None if settings.DICOMWEB_PUBLIC_READ else await get_share_scope(request),
        )

    content_type = params.get("contentType", "application/dicom").lower()
    if content_type in ("image/jpeg", "image/jpg", "image/png"):
        from app.services.imaging import dataset_to_bytes

        img_fmt = "PNG" if content_type == "image/png" else "JPEG"
        etag = content_version.entity_tag(inst, f"wado-uri:{img_fmt}")
        if (not_modified := _not_modified(request, etag)) is not None:
            return not_modified

        def _render() -> bytes:
            ds = storage_service.read_dataset(inst.file_path, backend_name=_bk(inst))
            return dataset_to_bytes(ds, fmt=img_fmt)

        try:
            data = await run_blocking(_render)
        except Exception as exc:
            log.warning("WADO URI render failed error_type=%s", type(exc).__name__)
            raise HTTPException(422, "Cannot render instance") from None
        return _cached_response(
            request, data, "image/png" if img_fmt == "PNG" else "image/jpeg", etag
        )

    # Audit: RETRIEVED (consistent with WADO-RS rendered).
    from app.services.audit_service import add_audit

    add_audit(db, inst.sop_instance_uid, "RETRIEVED", performed_by="wado-uri")

    # Default: the raw DICOM part-10 object.
    etag = content_version.entity_tag(inst, "dicom")
    if (not_modified := _not_modified(request, etag)) is not None:
        return not_modified
    response = _streaming_instance_response(inst, etag)
    response.headers["Content-Disposition"] = f'inline; filename="{inst.sop_instance_uid}.dcm"'
    return response


async def _anonymous_share_scope(request: Request, user) -> str | None:
    """Share scope for an anonymous caller in private DICOMweb mode, else None.

    A share token is a *fallback identity* for people with no account, never an
    override: an authenticated user keeps full visibility, and public mode is
    unchanged (so a stale share cookie cannot narrow a normal viewer session).
    """
    if settings.DICOMWEB_PUBLIC_READ or user is not None:
        return None
    return await get_share_scope(request)


def stow_tenant_scope(user) -> int | None:
    """The tenant a STOW-RS upload is written as (VNA).

    The tenant comes from the authenticated caller; admins and unassigned users
    stay in the legacy unscoped scope. This is the same value the staging and
    indexing calls pass, so the ownership check and the write cannot disagree.
    """
    if user is None or user.role == "ADMIN" or user.tenant_id is None:
        return None
    return user.tenant_id


async def _reject_cross_tenant_ingest(db: AsyncSession, ds, user) -> None:
    """Refuse an upload that targets another tenant's data (C04/A-9).

    Delegates to the indexing service so the study, series **and** instance
    rules live in exactly one place — `_reject_cross_tenant_ingest` used to
    compare only the study tenant, which let a replayed SOP/series UID through
    to re-point another institution's `file_path`.

    Checked *before* the object is staged, so a refused upload leaves nothing
    behind on disk and no row to roll back.
    """
    meta = {
        "study_instance_uid": str(getattr(ds, "StudyInstanceUID", "") or ""),
        "series_instance_uid": str(getattr(ds, "SeriesInstanceUID", "") or ""),
        "sop_instance_uid": str(getattr(ds, "SOPInstanceUID", "") or ""),
    }
    await indexing_service.assert_tenant_owns_ingest_target(
        db, meta, tenant_id=stow_tenant_scope(user),
        include_all=bool(user is not None and user.role == "ADMIN"),
    )


def _validate_ingest_uids(ds) -> dict[str, str]:
    """Validate an incoming dataset's mandatory identifiers; return them (A-17, C05).

    Returns the three Type 1 UIDs keyed by DICOM keyword.

    Raises:
        dicom_uids.Type1UIDError: a UID is missing, blank, a shared placeholder
            (``"UNKNOWN"``), or — unless ``DICOM_UID_STRICT_FORMAT`` is off —
            not a well-formed DICOM UID. The caller reports this in the
            FailedSOPSequence and refuses an all-invalid request with HTTP 400.
            This used to *skip* absent UIDs, which then fell back to a constant
            ``"UNKNOWN"`` study bucket and merged unrelated examinations.
        storage_service.InvalidStorageKey: a present UID is not a safe path
            segment. Still load-bearing when the format rule is relaxed.
    """
    uids = dicom_uids.validate_mandatory_uids(ds)
    for keyword, value in uids.items():
        storage_service.validate_component(value, field=keyword)
    return uids


def _nonempty_uid_value(source, keyword: str) -> str | None:
    """Read a UID value for a STOW failure reference without inventing one."""
    value = getattr(source, keyword, None) if source is not None else None
    text = str(value).strip() if value is not None else ""
    return text or None


# --------------------------------------------------------------------------- #
# QIDO-RS
# --------------------------------------------------------------------------- #
@router.get("/studies", response_class=JSONResponse)
async def qido_studies(request: Request, db: AsyncSession = Depends(get_db),
                      user: User | None = Depends(get_optional_user)):
    params = {k: v for k, v in request.query_params.items()}
    limit, offset = _paginate(params)
    params.pop("includefield", None)
    # A tag we cannot translate must not be dropped: dropping it widens the
    # query, and "widened" here means every patient in the tenant. Fail loudly
    # instead (the viewer's study browser asks for one patient's priors).
    unsupported = query_service.unsupported_query_filters(params, query_service.STUDY_FILTERS)
    if unsupported:
        raise HTTPException(400, f"Unsupported query attribute(s): {', '.join(unsupported)}")
    # A patient share token may only ever see the study it names, so an
    # explicit StudyInstanceUID filter is forced rather than trusted (C03).
    # Only for an *anonymous* caller in private mode: an authenticated user (or
    # public mode) must not be narrowed by a leftover share cookie.
    share_scope = await _anonymous_share_scope(request, user)
    if share_scope:
        params["StudyInstanceUID"] = share_scope
    # The viewer's deep-link / study-open flow queries QIDO with a specific
    # StudyInstanceUID — warm that study's thumbnails + frame caches in the
    # background so scrolling and re-opens are instant (fire-and-forget).
    if study_uid_param := params.get("StudyInstanceUID"):
        warm_service.schedule_warm(study_uid_param)
    tenant_id, include_all = tenant_service.scope_from_user(user)
    # Keyset pagination: ``?cursor=<study_date>|<id>`` walks pages without
    # OFFSET (which MySQL must re-scan from the start past ~1000 rows).
    # The cursor is the last row's (study_date, id); the predicate and ordering
    # live with the query builder (query_service.keyset_predicate / STUDY_ORDER)
    # so a page can never be ordered differently from the cursor that walks it.
    cursor = _keyset_cursor(params.pop("cursor", None))
    effective_offset = 0 if cursor is not None else offset
    studies, has_more = await query_service.qido_study_page(
        db, params, tenant_id=tenant_id, include_all=include_all,
        limit=limit, offset=effective_offset, cursor=cursor,
    )
    agg = await query_service.study_aggregates(db, [s.id for s in studies])
    results = [
        study_to_dicom_json(
            s, s.patient,
            modalities=agg.get(s.id, {}).get("modalities", []),
            n_series=agg.get(s.id, {}).get("series", 0),
            n_instances=agg.get(s.id, {}).get("instances", 0),
        )
        for s in studies
    ]
    # Keyset: a full page does not mean more rows exist — probe one row past
    # the window and only advertise the next cursor when it's actually there.
    # The cursor encodes the last visible row's (study_date, id), matching the
    # (date DESC, id DESC) ordering so the next page continues seamlessly.
    headers = {}
    if has_more and studies:
        last = studies[-1]
        headers["X-Next-Cursor"] = f"{last.study_date or ''}|{last.id}"
    return JSONResponse(results, media_type="application/dicom+json", headers=headers)


@router.get("/series", response_class=JSONResponse)
@router.get("/studies/{study_uid}/series", response_class=JSONResponse)
async def qido_series(request: Request, study_uid: str | None = None,
                      db: AsyncSession = Depends(get_db),
                      user: User | None = Depends(get_optional_user)):
    params = {k: v for k, v in request.query_params.items()}
    limit, offset = _paginate(params)
    params.pop("includefield", None)
    unsupported = query_service.unsupported_query_filters(params, query_service.SERIES_FILTERS)
    if unsupported:
        raise HTTPException(400, f"Unsupported query attribute(s): {', '.join(unsupported)}")
    share_scope = await _anonymous_share_scope(request, user)
    if share_scope:
        if study_uid is not None and study_uid != share_scope:
            raise HTTPException(404, "Study not found")
        study_uid = share_scope
    tenant_id, include_all = tenant_service.scope_from_user(user)
    series_list = await query_service.qido_series_page(
        db, study_uid, params, tenant_id=tenant_id, include_all=include_all,
        limit=limit, offset=offset,
    )
    counts = await query_service.series_instance_counts(db, [s.id for s in series_list])
    results = [series_to_dicom_json(s, counts.get(s.id, 0)) for s in series_list]
    return JSONResponse(results, media_type="application/dicom+json")


@router.get("/instances", response_class=JSONResponse)
@router.get("/studies/{study_uid}/instances", response_class=JSONResponse)
@router.get("/studies/{study_uid}/series/{series_uid}/instances", response_class=JSONResponse)
async def qido_instances(request: Request, study_uid: str | None = None,
                         series_uid: str | None = None,
                         db: AsyncSession = Depends(get_db),
                         user: User | None = Depends(get_optional_user)):
    params = {k: v for k, v in request.query_params.items()}
    limit, offset = _paginate(params)
    params.pop("includefield", None)
    unsupported = query_service.unsupported_query_filters(params, query_service.INSTANCE_FILTERS)
    if unsupported:
        raise HTTPException(400, f"Unsupported query attribute(s): {', '.join(unsupported)}")
    share_scope = await _anonymous_share_scope(request, user)
    if share_scope:
        if study_uid is not None and study_uid != share_scope:
            raise HTTPException(404, "Study not found")
        study_uid = share_scope
    tenant_id, include_all = tenant_service.scope_from_user(user)
    instances = await query_service.qido_instance_page(
        db, study_uid, series_uid, params, tenant_id=tenant_id,
        include_all=include_all, limit=limit, offset=offset,
    )
    results = [instance_to_dicom_json(i) for i in instances]
    return JSONResponse(results, media_type="application/dicom+json")


# --------------------------------------------------------------------------- #
# WADO-RS
# --------------------------------------------------------------------------- #
async def _instances_of(db: AsyncSession, study_uid: str,
                        series_uid: str | None = None,
                        with_metadata: bool = False) -> list[Instance]:
    # C04: the router-level guard published the caller's scope for this request.
    tenant_id, include_all = current_scope()
    return await query_service.instances_of(
        db, study_uid, series_uid, tenant_id=tenant_id, include_all=include_all,
        with_metadata=with_metadata,
    )


#: (StudyInstanceUID, SeriesInstanceUID, Modality) for the metadata column fallback.
SeriesIdentity = tuple[str, str, str | None]


def _stored_metadata(inst: Instance) -> str | None:
    """The ingest-time DICOM JSON, or None when absent or not loaded.

    `metadata_json` is deferred, so a caller that did not ask for it has nothing
    loaded — reading it would raise MissingGreenlet on the async session. "Not
    loaded" is treated as "not available": the fallback is correct, just slower.
    """
    loaded = "metadata_json" not in sa_inspect(inst).unloaded
    return inst.metadata_json if loaded else None


async def _fallback_identities(db: AsyncSession, instances: list[Instance]) -> dict[int, SeriesIdentity]:
    """Study/series UID + modality for series that may need the column fallback (one query, often none)."""
    needed = {inst.series_id for inst in instances if not _stored_metadata(inst)}
    return await query_service.series_identities(db, needed)


def _metadata_texts(instances: list[Instance], identities: dict[int, SeriesIdentity] | None = None) -> list[str]:
    """One compact DICOM JSON object per instance, in the order given.

    Stored text is used where present: `indexing_service` writes it at ingest,
    so a study that has been ingested since the column exists costs a join and
    no file access at all. Rows without it (pre-existing archive) fall back to
    reading and converting the object — identical output, ~20 ms each, which is
    what `scripts/backfill_instance_metadata.py` exists to eliminate.

    Objects that cannot be read degrade to the indexed columns (+ identity) and
    are reported in ONE warning per response — a missing study is an incident an
    operator must see, but not as thousands of lines (N-2: UIDs and counts only).
    """
    texts: list[str] = []
    failed: list[tuple[str, str]] = []
    for inst in instances:
        stored = _stored_metadata(inst)
        if stored:
            texts.append(stored)
            continue
        identity = (identities or {}).get(inst.series_id)
        text, error_type = _object_metadata_text(inst, identity)
        texts.append(text)
        if error_type is not None:
            failed.append((inst.sop_instance_uid, error_type))
    if failed:
        log.warning("wado_metadata_fallback instances=%d of=%d error_type=%s first_sop=%s",
                    len(failed), len(instances), failed[0][1], failed[0][0])
    return texts


def _object_metadata_text(inst: Instance, identity: SeriesIdentity | None) -> tuple[str, str | None]:
    """(DICOM JSON text, None) from the object, or (column fallback, error type) when unreadable.

    No `exists()` pre-check: it is a second stat per instance (1 ms × thousands,
    measured) and `read_dataset` already fails the same way on a missing object.
    """
    try:
        ds = storage_service.read_dataset(
            inst.file_path, stop_before_pixels=True, backend_name=_bk(inst)
        )
        return dataset_to_json_text(ds), None
    except Exception as exc:
        # Deliberately broad: any unreadable object (missing, truncated, bad
        # backend) still answers with the indexed columns instead of a 500.
        columns = instance_to_dicom_json(inst, identity) if identity else instance_to_dicom_json(inst)
        return json.dumps(columns, separators=(",", ":"), ensure_ascii=False), type(exc).__name__


def _metadata_text_from_object(inst: Instance, identity: SeriesIdentity | None = None) -> str:
    """Read one object's header and render it as DICOM JSON text (column fallback if unreadable)."""
    return _object_metadata_text(inst, identity)[0]


def _metadata_body(instances: list[Instance], identities: dict[int, SeriesIdentity] | None = None) -> bytes:
    """The WADO-RS response body: every instance's JSON object, concatenated.

    Concatenation rather than `json.dumps(list_of_dicts)` is the whole point —
    the stored strings are already valid objects, so a 4,034-instance study is
    assembled with a join instead of 4,034 serialisations (which measured at
    82 s, of which 54 s was `to_json_dict()` and re-encoding).
    """
    return ("[" + ",".join(_metadata_texts(instances, identities)) + "]").encode("utf-8")


def _metadata_response(instances: list[Instance], identities: dict[int, SeriesIdentity] | None = None) -> Response:
    """`application/dicom+json` response for a batch, from a worker thread."""
    return Response(content=_metadata_body(instances, identities), media_type="application/dicom+json")


async def _metadata_stamp(db: AsyncSession, study_uid: str, series_uid: str | None):
    """(count, sum(id), max(id), sum(content_version)) of the caller-visible rows."""
    tenant_id, include_all = current_scope()
    return await query_service.instance_stamp(
        db, study_uid, series_uid, tenant_id=tenant_id, include_all=include_all
    )


def _stamp_of(instances: list[Instance]) -> tuple[int, int, int, int]:
    """The same stamp, computed from the rows the body is built from."""
    return (len(instances), sum(i.id for i in instances), max(i.id for i in instances),
            sum(int(i.content_version or 0) for i in instances))


async def _metadata_cached_response(request: Request, db: AsyncSession, study_uid: str,
                                    series_uid: str | None, not_found: str) -> Response:
    """Speed plan: serve the gzipped /metadata body from `metadata_body_cache`.

    One aggregate query revalidates the cached body; it is rebuilt only when an
    instance was added, removed or rewritten (see the module for the rules).
    Bodies that needed the object-read fallback are not cached.
    """
    from app.services import metadata_body_cache

    tenant_id, include_all = current_scope()
    key = (tenant_id, bool(include_all), study_uid, series_uid)
    stamp = await _metadata_stamp(db, study_uid, series_uid)
    if stamp[0] == 0:
        raise HTTPException(404, not_found)
    entry = metadata_body_cache.get(key, stamp)
    if entry is None:
        instances = await _instances_of(db, study_uid, series_uid, with_metadata=True)
        if not instances:
            raise HTTPException(404, not_found)
        identities = await _fallback_identities(db, instances)
        if identities or any(not _stored_metadata(inst) for inst in instances):
            # Fallback rows read objects that may be missing: answer, don't cache.
            return await run_blocking(_metadata_response, instances, identities)
        body = await run_blocking(_metadata_body, instances, identities)
        entry = await run_blocking(metadata_body_cache.build, key, _stamp_of(instances), body)
    if (not_modified := _not_modified(request, entry.etag)) is not None:
        return not_modified
    headers = {"ETag": entry.etag, "Cache-Control": REVALIDATE_CACHE_CONTROL,
               "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(content=entry.gzipped, media_type="application/dicom+json",
                        headers=headers)
    body = await run_blocking(gzip.decompress, entry.gzipped)
    return Response(content=body, media_type="application/dicom+json", headers=headers)


@router.get("/studies/{study_uid}/metadata", response_class=JSONResponse)
async def wado_study_metadata(study_uid: str, request: Request,
                              db: AsyncSession = Depends(get_db)):
    """WADO-RS study-level metadata.

    Serialised on a worker thread: the join and the UTF-8 encode are CPU work on
    a body that reaches tens of MB, and the fallback path touches the disk.
    """
    # Viewers fetch study metadata right after opening it — warm caches so the
    # subsequent thumbnail/frame requests are already hot (fire-and-forget).
    response = await _metadata_cached_response(request, db, study_uid, None, "Study not found")
    warm_service.schedule_warm(study_uid)
    return response


@router.get("/studies/{study_uid}/series/{series_uid}/metadata", response_class=JSONResponse)
async def wado_series_metadata(study_uid: str, series_uid: str, request: Request,
                               db: AsyncSession = Depends(get_db)):
    """WADO-RS series-level metadata (PaxsViewer v3.13+ fetches this to build display sets)."""
    return await _metadata_cached_response(request, db, study_uid, series_uid, "Series not found")


def _multipart_response(files: list[bytes]) -> Response:
    boundary = new_boundary()
    body = build_multipart(files, boundary)
    return Response(
        content=body,
        media_type=f'multipart/related; type="application/dicom"; boundary={boundary}',
    )


def _multipart_reservation_bytes(instances: list[Instance], boundary: str) -> int:
    """Conservatively reserve DICOM bytes plus their multipart framing."""
    per_part = len(f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n\r\n".encode("ascii"))
    closing = len(f"--{boundary}--\r\n".encode("ascii"))
    return sum(max(0, int(inst.file_size_bytes or 0)) for inst in instances) + (
        len(instances) * per_part
    ) + closing


def _read_multipart_instance(inst: Instance) -> bytes | None:
    """Read one stored instance, preserving the study retrieve missing-file skip."""
    if not storage_service.exists(inst.file_path, backend_name=_bk(inst)):
        return None
    return storage_service.read_bytes(inst.file_path, backend_name=_bk(inst))


async def _multipart_instance_stream(
    instances: list[Instance], boundary: str
) -> AsyncIterator[bytes]:
    """Yield the established multipart framing while retaining one instance."""
    for inst in instances:
        payload = await run_blocking(_read_multipart_instance, inst)
        if payload is None:
            continue
        yield f"--{boundary}\r\n".encode("ascii")
        yield b"Content-Type: application/dicom\r\n\r\n"
        yield payload
        yield b"\r\n"
        del payload
    yield f"--{boundary}--\r\n".encode("ascii")


def _multipart_streaming_response(
    instances: list[Instance], boundary: str
) -> StreamingResponse:
    """Build an admitted DICOM multipart response without assembling its body."""
    reserved_bytes = _multipart_reservation_bytes(instances, boundary)
    reserve_response_bytes(reserved_bytes)
    try:
        return AdmittedStreamingResponse(
            _multipart_instance_stream(instances, boundary),
            media_type=f'multipart/related; type="application/dicom"; boundary={boundary}',
            reserved_bytes=reserved_bytes,
        )
    except BaseException:
        release_response_bytes(reserved_bytes)
        raise


@router.get("/studies/{study_uid}")
async def wado_study(study_uid: str, db: AsyncSession = Depends(get_db)):
    instances = await _instances_of(db, study_uid)
    if not instances:
        raise HTTPException(404, "Study not found")

    return _multipart_streaming_response(instances, new_boundary())


@router.get("/studies/{study_uid}/series/{series_uid}")
async def wado_series(study_uid: str, series_uid: str, db: AsyncSession = Depends(get_db)):
    instances = await _instances_of(db, study_uid, series_uid)
    if not instances:
        raise HTTPException(404, "Series not found")

    return _multipart_streaming_response(instances, new_boundary())


async def _get_instance(db: AsyncSession, study_uid: str, series_uid: str,
                        sop_uid: str, phase_ms: dict[str, float] | None = None) -> Instance:
    tenant_id, include_all = current_scope()
    started = time.perf_counter() if phase_ms is not None else None
    if phase_ms is not None:
        await db.connection()
        phase_ms["db_pool_wait"] = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
    inst = await query_service.find_instance(
        db, study_uid, series_uid, sop_uid,
        tenant_id=tenant_id, include_all=include_all,
    )
    if phase_ms is not None and started is not None:
        phase_ms["db_query_exec"] = (time.perf_counter() - started) * 1000
        phase_ms["db_lookup"] = phase_ms["db_pool_wait"] + phase_ms["db_query_exec"]
    if inst is None:
        raise HTTPException(404, "Instance not found")
    if not await run_blocking(storage_service.exists, inst.file_path, backend_name=_bk(inst)):
        raise _object_missing(sop_uid)
    return inst


async def _get_frame_instance(db: AsyncSession, study_uid: str, series_uid: str,
                              sop_uid: str, phase_ms: dict[str, float] | None = None):
    """S9: `_get_instance` for the per-frame route, served from a per-series,
    tenant-scoped, short-TTL snapshot (one DB query per series, not per frame).
    Same contract: 404 when not visible to the caller, the missing-object error
    when the stored file is gone."""
    from app.services.frame_instance_cache import frame_instances

    tenant_id, include_all = current_scope()
    started = time.perf_counter() if phase_ms is not None else None
    inst = await frame_instances.get(db, study_uid, series_uid, sop_uid,
                                     tenant_id=tenant_id, include_all=include_all)
    if phase_ms is not None and started is not None:
        phase_ms["db_lookup"] = (time.perf_counter() - started) * 1000
    if inst is None:
        raise HTTPException(404, "Instance not found")
    if not await run_blocking(storage_service.exists, inst.file_path, backend_name=_bk(inst)):
        raise _object_missing(sop_uid)
    return inst


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}")
async def wado_instance(study_uid: str, series_uid: str, sop_uid: str,
                        request: Request, db: AsyncSession = Depends(get_db)):
    inst = await _get_instance(db, study_uid, series_uid, sop_uid)
    accept = request.headers.get("accept", "")
    etag = content_version.entity_tag(inst, "dicom")
    if "multipart" in accept:
        # H-10: the admitted multipart stream the study/series routes use.
        return _multipart_streaming_response([inst], new_boundary())
    if (not_modified := _not_modified(request, etag)) is not None:
        return not_modified
    return _streaming_instance_response(inst, etag)


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/metadata",
            response_class=JSONResponse)
async def wado_instance_metadata(study_uid: str, series_uid: str, sop_uid: str,
                                 db: AsyncSession = Depends(get_db)):
    inst = await _get_instance(db, study_uid, series_uid, sop_uid)
    ds = await run_blocking(
        storage_service.read_dataset, inst.file_path, stop_before_pixels=True, backend_name=_bk(inst)
    )
    return JSONResponse(dataset_to_dicom_json(ds), media_type="application/dicom+json")


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/rendered")
async def wado_rendered(study_uid: str, series_uid: str, sop_uid: str,
                        request: Request, db: AsyncSession = Depends(get_db)):
    """Instance-level rendered resource: the first frame of a multi-frame object."""
    inst = await _get_instance(db, study_uid, series_uid, sop_uid)
    return await _rendered_response(request, db, inst, frame_index=0)


@router.get(
    "/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/{frame_list}/rendered"
)
async def wado_frame_rendered(study_uid: str, series_uid: str, sop_uid: str,
                              frame_list: str, request: Request,
                              db: AsyncSession = Depends(get_db)) -> Response:
    """Frame-level rendered resource (PS3.18 9.5, H-09).

    One 1-based frame per request: a rendered response is a single image, and
    a multipart of renders is a different (unsupported) representation, so a
    frame list is a 400 rather than a silent first-frame answer. A frame that
    does not exist is a 404, as on the raw ``/frames/{n}`` route.
    """
    try:
        frame_number = int(frame_list.strip())
    except ValueError:
        raise HTTPException(400, "Rendered frames take exactly one frame number") from None
    if frame_number < 1:
        raise HTTPException(404, "Frame numbers are 1-based")
    inst = await _get_instance(db, study_uid, series_uid, sop_uid)
    return await _rendered_response(request, db, inst, frame_index=frame_number - 1)


async def _rendered_response(request: Request, db: AsyncSession, inst: Instance,
                             frame_index: int) -> Response:
    """Render one frame of ``inst`` (window, viewport, GSPS), cached by version + frame."""
    sop_uid = inst.sop_instance_uid
    params = request.query_params
    window = params.get("window")
    viewport = params.get("viewport")
    presentation_uid = params.get("presentationUID")
    max_size = None
    if viewport:
        try:
            w, h = (int(x) for x in viewport.split(",")[:2])
            max_size = max(w, h) or None
        except (TypeError, ValueError) as exc:  # malformed client viewport
            log.debug("wado_rendered_viewport_invalid error_type=%s", type(exc).__name__)
            max_size = None

    accept = request.headers.get("accept", "image/png")
    fmt = "JPEG" if "jpeg" in accept else "PNG"

    rendered_type = "image/jpeg" if fmt == "JPEG" else "image/png"
    representation = (
        f"rendered:f{frame_index}:{window or 'auto'}:{max_size or 'full'}:"
        f"{presentation_uid or 'none'}:{fmt.lower()}"
    )
    etag = content_version.entity_tag(inst, representation)
    if (not_modified := _not_modified(request, etag)) is not None:
        return not_modified

    # --- Redis Cache lookup (C-04: keyed by the stored-object version) ---
    cache_key = content_version.rendered_key(
        sop_uid, content_version.version_of(inst), window or "auto",
        str(max_size or "full"), presentation_uid or "none", fmt.lower(),
        frame=frame_index,
    )
    from app.services.redis_service import cache_get, cache_set
    cached_bytes = await run_blocking(cache_get, cache_key)
    if cached_bytes:
        return _cached_response(request, cached_bytes, rendered_type, etag)

    # GSPS-aware rendering: if a presentationUID is given, load the GSPS
    # object and apply its display parameters (VOI LUT, spatial transforms,
    # annotations, shutters) on top of the image.
    gsps_content = None
    if presentation_uid:
        try:
            from app.services.gsps_service import parse_gsps

            ps_row = await query_service.presentation_state_by_sop_uid(db, presentation_uid)
            if ps_row and await run_blocking(storage_service.exists, ps_row.file_path, backend_name=_bk(ps_row)):
                gsps_ds = await run_blocking(
                    storage_service.read_dataset, ps_row.file_path, stop_before_pixels=True, backend_name=_bk(ps_row)
                )
                gsps_content = parse_gsps(gsps_ds)
                # Use GSPS window/level if no explicit window was requested.
                if not window and gsps_content.window_center is not None and gsps_content.window_width is not None:
                    window = f"{gsps_content.window_center},{gsps_content.window_width}"
        except Exception as exc:
            log.debug("GSPS lookup failed error_type=%s", type(exc).__name__)

    def _render() -> bytes:
        ds = storage_service.read_dataset(inst.file_path, backend_name=_bk(inst))
        from app.services.imaging import dataset_to_image

        img = dataset_to_image(ds, window=window, max_size=max_size, frame_index=frame_index)

        # Apply GSPS overlays (spatial transforms, shutters, annotations).
        if gsps_content:
            from app.services.gsps_service import apply_gsps_to_image

            img = apply_gsps_to_image(img, gsps_content)

        buf = io.BytesIO()
        if fmt.upper() == "JPEG":
            img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=88)
        else:
            img.save(buf, format="PNG")
        return buf.getvalue()

    from app.services.imaging import FrameIndexError

    try:
        data = await run_blocking(_render)
        await run_blocking(cache_set, cache_key, data, expire_seconds=600)
    except FrameIndexError:
        raise HTTPException(404, f"Frame {frame_index + 1} not found") from None
    except Exception as exc:
        log.warning("Rendered failed error_type=%s", type(exc).__name__)
        raise HTTPException(422, "Cannot render instance") from None

    # Audit: RETRIEVED
    from app.services.audit_service import add_audit

    add_audit(db, inst.sop_instance_uid, "RETRIEVED", performed_by="wado-rendered")
    return _cached_response(request, data, rendered_type, etag)


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/thumbnail")
async def wado_thumbnail(study_uid: str, series_uid: str, sop_uid: str,
                         request: Request, db: AsyncSession = Depends(get_db)):
    """WADO-RS rendered thumbnail (PaxsViewer/PAXs ``thumbnailRendering: 'wadors'``
    requests ``.../instances/{sop}/thumbnail`` to populate the study-list and
    series thumbnails). Serves a small PNG so viewers never 404 on thumbnails.

    Caching, fastest first: (1) the worker's pre-generated 192px PNG object;
    (2) the Redis render cache (keyed sop/window/size/format, 10 min TTL);
    (3) an on-the-fly pydicom read + PIL render. The browser also gets the
    identity ETag so repeated list renders revalidate with a cheap 304.
    """
    inst = await _get_instance(db, study_uid, series_uid, sop_uid)
    params = request.query_params
    window = params.get("window")
    max_size = None
    viewport = params.get("viewport")
    if viewport:
        try:
            w, h = (int(x) for x in viewport.split(",")[:2])
            max_size = max(w, h) or None
        except (TypeError, ValueError) as exc:  # malformed client viewport
            log.debug("wado_thumbnail_viewport_invalid error_type=%s", type(exc).__name__)
            max_size = None
    explicit_viewport = viewport is not None
    # Default thumbnail size keeps the payload small for list views.
    if not max_size:
        max_size = 256

    accept = request.headers.get("accept", "image/png")
    fmt = "JPEG" if "jpeg" in accept else "PNG"
    thumb_type = "image/jpeg" if fmt == "JPEG" else "image/png"
    etag = content_version.entity_tag(
        inst, f"thumbnail:{window or 'auto'}:{max_size}:{explicit_viewport}:{fmt.lower()}"
    )
    if (not_modified := _not_modified(request, etag)) is not None:
        return not_modified

    # Fast path: the worker pre-generates a 192px PNG thumbnail per instance at
    # ingest (.thumbnails/{sop}.png — see thumbnail_service). Study-list
    # rendering fires dozens of thumbnail requests per screen; serving the
    # pre-generated object avoids a pydicom read + PIL render on every one.
    # Used whenever no explicit windowing is requested and the requested size
    # fits inside the pre-generated thumbnail (or the client asked for none).
    if fmt == "PNG" and not window and (not explicit_viewport or max_size <= 192):
        key = storage_service.thumbnail_key(inst.sop_instance_uid)
        if await run_blocking(storage_service.exists, key, backend_name=_bk(inst)):
            data = await run_blocking(storage_service.read_bytes, key, backend_name=_bk(inst))
            return _cached_response(request, data, "image/png", etag)

    # Redis cache for the rendered path (the expensive pydicom read + PIL
    # render) — same pattern as /rendered. Keyed by sop/window/size/format;
    # keys carry the content version (C-04), so the TTL is purely eviction policy
    # (rendered/thumbnail bytes never change for a given key). Keeps study-list
    # and series thumbnails instant even when the pre-generated objects are
    # disabled (THUMBNAIL_ENABLED=false) or missing (decode failed at ingest).
    from app.services.redis_service import cache_get, cache_set

    cache_key = content_version.thumbnail_key(
        inst.sop_instance_uid, content_version.version_of(inst), window or "auto",
        str(max_size or "full"), fmt.lower(),
    )
    cached_bytes = await run_blocking(cache_get, cache_key)
    if cached_bytes:
        return _cached_response(request, cached_bytes, thumb_type, etag)

    def _render() -> bytes:
        ds = storage_service.read_dataset(inst.file_path, backend_name=_bk(inst))
        from app.services.imaging import dataset_to_image

        img = dataset_to_image(ds, window=window, max_size=max_size)
        buf = io.BytesIO()
        if fmt.upper() == "JPEG":
            img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=82)
        else:
            img.save(buf, format="PNG")
        return buf.getvalue()

    try:
        data = await run_blocking(_render)
        await run_blocking(cache_set, cache_key, data, expire_seconds=600)
    except Exception as exc:
        log.warning("Thumbnail failed error_type=%s", type(exc).__name__)
        raise HTTPException(422, "Cannot render thumbnail") from None
    return _cached_response(request, data, thumb_type, etag)


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/{frame_list}")
async def wado_frame(
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    frame_list: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """WADO-RS Frame Retrieval (PS 3.18) for single-frame or multi-frame stacks.

    By default ``frame_list`` is the standard file-order frame number (1-based,
    as stored in PixelData). With ``?zorder=1`` the numbers are instead
    interpreted as 0-based **z-order** indices of the volume stack — the same
    geometry as ``/api/v1/volume/{series}/info`` ``frame_map`` and
    ``/mpr/slice?plane=axial&index=N`` — so a thin client can toggle between
    raw frames and resliced planes of a multi-frame instance.
    """
    timing_enabled = settings.DICOMWEB_SERVER_TIMING_ENABLED
    phase_ms = ({"db_pool_wait": 0.0, "db_query_exec": 0.0, "db_lookup": 0.0,
                 "storage_read": 0.0, "dataset_read": 0.0,
                 "frame_extract": 0.0, "serialize": 0.0}
                if timing_enabled else None)

    async def measured_response(data: bytes, media_type: str, etag: str) -> Response:
        response = _cached_response(request, data, media_type, etag)
        if phase_ms is not None:
            response.headers["Server-Timing"] = ", ".join(
                f"{name};dur={phase_ms[name]:.3f}" for name in phase_ms
            )
        return response

    inst = await _get_frame_instance(db, study_uid, series_uid, sop_uid, phase_ms)
    zorder = str(request.query_params.get("zorder", "")).lower() in ("1", "true", "yes")
    try:
        parts = frame_list.split(",")
        if not parts or any(not part.strip() for part in parts):
            raise ValueError("empty frame number")
        indices = [int(part.strip()) for part in parts]
    except ValueError:
        raise HTTPException(404, "Invalid frame list") from None
    if not zorder and any(index < 1 for index in indices):
        raise HTTPException(404, "Frame numbers are 1-based")

    if zorder:
        # Translate z-order indices through the same expanded stack the volume
        # service uses (multi-frame instances are expanded into per-frame
        # slices ordered by position / dimension steps).
        from app.services import volume_service

        # B-9: resolve the volume stack in the *caller's* scope. This call used
        # to pass no scope, so `volume_bundle` fell back to its default
        # (tenant_id=None, include_all=False) — the legacy NULL bucket. The
        # owner of a tenant-tagged study therefore got 404 "Series has no
        # readable volume" from `?zorder=1`, and the lookup itself was unscoped.
        z_tenant_id, z_include_all = current_scope()
        bundle = await volume_service.volume_bundle(
            db, series_uid, tenant_id=z_tenant_id, include_all=z_include_all
        )
        if bundle is None:
            raise HTTPException(404, "Series has no readable volume")
        _, slices = bundle
        translated: list[int] = []
        for z in indices:
            if z < 0 or z >= len(slices):
                raise HTTPException(
                    404, f"Z-index {z} out of bounds (0-{len(slices) - 1})"
                )
            sl = slices[z]
            base_sop = sl.sop_uid.split("#")[0]
            if base_sop != sop_uid:
                raise HTTPException(
                    404, f"Z-index {z} belongs to instance {base_sop}, not {sop_uid}"
                )
            translated.append((sl.frame if sl.frame is not None else 0) + 1)
        indices = translated

    accept = request.headers.get("accept", "")
    transcode = str(request.query_params.get("transcode", "")).lower() in ("1", "true", "yes")
    ds = None
    source_path = None
    frame_index = None
    tenant_id, _include_all = current_scope()
    if settings.DICOM_FRAME_RANGE_READ_ENABLED and not transcode:
        from app.services import frame_range_service

        source_path = await run_blocking(
            storage_service.local_path, inst.file_path, backend_name=_bk(inst)
        )
        if source_path:
            started = time.perf_counter() if timing_enabled else None
            frame_index = await run_blocking(
                frame_range_service.get_frame_offset_index,
                source_path,
                tenant_id,
                settings.DICOM_FRAME_OFFSET_CACHE_MB * 1024 * 1024,
                settings.DICOM_FRAME_OFFSET_MAX_FRAGMENTS,
            )
            if started is not None and phase_ms is not None:
                phase_ms["dataset_read"] = (time.perf_counter() - started) * 1000

    if frame_index is not None:
        number_of_frames = frame_index.number_of_frames
    elif phase_ms is not None:
        ds, storage_ms, dataset_ms = await run_blocking(
            storage_service.read_dataset_with_timings, inst.file_path, False, _bk(inst)
        )
        phase_ms["storage_read"] = storage_ms
        phase_ms["dataset_read"] = dataset_ms
        number_of_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    else:
        ds = await run_blocking(storage_service.read_dataset, inst.file_path, backend_name=_bk(inst))
        number_of_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)

    if any(i < 1 or i > number_of_frames for i in indices):
        raise HTTPException(404, f"Frame(s) {frame_list} out of bounds (1-{number_of_frames})")

    # WADO-RS Frame Retrieval (PS 3.18) must return the RAW frame codestream —
    # the viewer decoders (openjpeg / libjpeg / charls wasm) receive those bytes
    # directly. Serving the whole DICOM file here (which the old branch did for
    # `*/*` or missing Accept — the fetch() default) breaks every viewer.
    # Only fall back to the full instance when a client EXPLICITLY requests
    # application/dicom and no raw octet-stream / multipart frame form.
    wants_full_dicom = (
        "application/dicom" in accept
        and "octet-stream" not in accept
        and "multipart" not in accept
        and accept.strip() not in ("", "*/*")
        and number_of_frames == 1
    )
    if wants_full_dicom:
        etag = content_version.entity_tag(inst, "dicom")
        if (not_modified := _not_modified(request, etag)) is not None:
            return not_modified
        started = time.perf_counter() if timing_enabled else None
        data = await run_blocking(storage_service.read_bytes, inst.file_path, backend_name=_bk(inst))
        if started is not None and phase_ms is not None:
            phase_ms["storage_read"] += (time.perf_counter() - started) * 1000
        return await measured_response(data, "application/dicom", etag)

    from app.services import wado_rs_bulk_service
    from app.services.redis_service import cache_get, cache_set

    # Fast memory cache key for this exact frame / transfer-syntax combination
    # PAXs's own generateAcceptHeader QUOTES the parameter it emits
    # (`multipart/related; type="application/octet-stream"; transfer-syntax="*"`),
    # so a bare `"transfer-syntax=*" in accept` test never matched the header the
    # viewer actually sends. Normalise away the quotes (and whitespace) before
    # testing so the wildcard is recognised in both spellings.
    accept_norm = accept.replace('"', "").replace(" ", "")
    viewer_mode = "transfer-syntax=*" in accept_norm
    cache_mode = "raw" if transcode else ("native" if viewer_mode else "def")
    # Key on the RESOLVED 1-based file indices (after ?zorder=1 translation),
    # not the raw URL list: `frames/2?zorder=1` may translate to file frame #1,
    # and caching under `frame_list` would poison the plain `frames/2` request.
    # Tenant authorization and the tenant-scoped instance lookup above must
    # remain before this shared SOP-keyed cache read.
    resolved = ",".join(str(i) for i in indices)
    cache_scope = f"tenant:{tenant_id}" if tenant_id is not None else "global"
    part_mode = "mp" if "multipart" in accept else "sp"
    version = content_version.version_of(inst)
    # C-04: the stored-object version is part of both the HTTP validator and
    # the Redis key, so a rewrite of this SOP can never be served from either.
    etag = content_version.entity_tag(inst, f"frames:{resolved}:{cache_mode}:{part_mode}")
    if (not_modified := _not_modified(request, etag)) is not None:
        return not_modified
    frame_cache_key = content_version.formatted_frame_key(
        cache_scope, sop_uid, version, resolved, cache_mode, part_mode
    )

    cached_payload = await run_blocking(cache_get, frame_cache_key)
    if cached_payload is not None:
        # Cache stores formatted payload + media-type header
        if b"||PAXS_MT||" in cached_payload:
            cached_data, cached_mt = cached_payload.split(b"||PAXS_MT||", 1)
            return await measured_response(
                cached_data, cached_mt.decode("utf-8", errors="ignore"), etag
            )
        return await measured_response(cached_payload, "application/octet-stream", etag)

    # S4 (plans/09): only results that were expensive to produce go to Redis.
    expensive = {"decoded": False}

    def _extract_frames() -> list[tuple[bytes, str]]:
        nonlocal ds
        if ds is None:
            ds = storage_service.read_dataset(inst.file_path, backend_name=_bk(inst))
        if transcode:
            expensive["decoded"] = True
            frames = wado_rs_bulk_service.decode_frames_to_raw(
                ds, indices, tenant_id=tenant_id, content_version=version
            )
        elif viewer_mode:
            try:
                frames = wado_rs_bulk_service.extract_frame_bytes(ds, indices)
            except ValueError as exc:  # documented fallback to raw decoding
                # The viewer path prefers already-compressed frame bytes; a
                # pixel-data layout it cannot slice is decoded instead.
                log.debug("wado_frame_bytes_unavailable error_type=%s; decoding raw",
                          type(exc).__name__)
                frames = []
            if not frames:
                expensive["decoded"] = True
                frames = wado_rs_bulk_service.decode_frames_to_raw(
                    ds, indices, tenant_id=tenant_id, content_version=version
                )
        else:
            frames = wado_rs_bulk_service.extract_frame_bytes(ds, indices)
        return frames

    # The decode (JPEG2000/JPEG-LS/RLE -> raw) is the expensive step for
    # compressed CT/MRI series — offloading it lets the viewer's dozens of
    # concurrent per-slice frame requests actually run in parallel instead of
    # queueing one-by-one behind the event loop.
    try:
        started = time.perf_counter() if timing_enabled else None
        frames = None
        if frame_index is not None and source_path is not None and not transcode:
            from app.services import frame_range_service

            frames = await run_blocking(
                frame_range_service.read_indexed_frames, source_path, frame_index, indices
            )
            if frames is None:
                frame_index = None
        if frames is None:
            frames = await run_blocking(_extract_frames)
        if started is not None and phase_ms is not None:
            phase_ms["frame_extract"] = (time.perf_counter() - started) * 1000
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not frames:
        raise HTTPException(404, f"Requested frame(s) {frame_list} not found")

    def _serialize() -> tuple[bytes, str]:
        if len(frames) == 1 and "multipart" not in accept:
            return frames[0]
        return wado_rs_bulk_service.build_multipart_related_payload(frames)

    started = time.perf_counter() if timing_enabled else None
    try:
        data, media_type = await run_blocking(_serialize)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if started is not None and phase_ms is not None:
        phase_ms["serialize"] = (time.perf_counter() - started) * 1000

    # Store in high-speed RAM cache and Redis for fast subsequent slice scrubbing.
    # The key MUST stay mode-qualified: the bare "paxs:frame:{sop}:{idx}" namespace
    # belongs to the decoded RAW-PIXEL cache (wado_rs_bulk_service / warm_service).
    # Writing a formatted body (multipart or native codestream) there made a later
    # ?transcode=1 request serve that body as if it were raw pixels.
    # S4 (plans/09): a frame sliced straight from a local file (range read or byte
    # extraction) is cheaper to re-read than to round-trip through Redis, and copying
    # every slice of every opened study into Redis evicted the decoded frames that
    # are worth keeping. Cache only decoded/transcoded bodies or non-local storage.
    worth_caching = expensive["decoded"] or source_path is None
    if worth_caching and len(data) <= 16 * 1024 * 1024:
        await run_blocking(
            cache_set,
            frame_cache_key,
            data + b"||PAXS_MT||" + media_type.encode("utf-8"),
            expire_seconds=1800,
        )

    return await measured_response(data, media_type, etag)


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/bulk/{tag_path}")
async def wado_bulk_data(
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    tag_path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """WADO-RS Bulk Data Retrieval (PS 3.18) for specific attributes (PixelData, OverlayData)."""
    inst = await _get_instance(db, study_uid, series_uid, sop_uid)

    from app.services import wado_rs_bulk_service

    def _extract() -> tuple[bytes, str]:
        ds = storage_service.read_dataset(inst.file_path, backend_name=_bk(inst))
        return wado_rs_bulk_service.extract_bulk_attribute(ds, tag_path)

    try:
        content_bytes, media_type = await run_blocking(_extract)
        return _cached_response(
            request, content_bytes, media_type,
            content_version.entity_tag(inst, f"bulk:{tag_path}"),
        )
    except KeyError as key_err:
        raise HTTPException(404, str(key_err)) from key_err
    except Exception as exc:
        log.warning("WADO bulk extraction failed error_type=%s", type(exc).__name__)
        raise HTTPException(400, "Failed to extract bulk data") from None


# --------------------------------------------------------------------------- #
# STOW-RS
# --------------------------------------------------------------------------- #
def _stow_response(stored: list[dict], failed: list[dict], study_uid: str | None,
                   study_existed: dict[str, bool] | None = None,
                   status_code: int = 200,
                   uid_errors: list[dict] | None = None,
                   legacy_response: bool = False) -> JSONResponse:
    base_uri = f"/dicom-web/studies/{study_uid}" if study_uid else "/dicom-web/studies"
    payload = {
        "00081190": {"vr": "UR", "Value": [base_uri]},
        "00081198": {"vr": "SQ", "Value": failed},
        "00081199": {"vr": "SQ", "Value": stored},
    }
    if legacy_response and uid_errors:
        # DICOM's FailedSOPSequence identifies the refused SOP and failure
        # reason; this PHI-free companion names the exact missing/malformed tag.
        payload["errors"] = uid_errors
    if legacy_response and study_existed is not None:
        # Per-study summary for the upload UI: how many studies are brand new
        # vs. already present in the archive (re-import / late slices), with
        # the instance count each.
        payload["summary"] = _stow_study_summary(stored, failed, study_existed)
    # H08: a quota refusal reports 507 Insufficient Storage for the batch.
    status = status_code if status_code != 200 else (200 if stored else 409)
    media_type = "application/json" if legacy_response else "application/dicom+json"
    return JSONResponse(payload, status_code=status, media_type=media_type)


def _stow_study_summary(
    stored: list[dict], failed: list[dict], study_existed: dict[str, bool]
) -> dict:
    """Group the stored items by StudyInstanceUID for the upload UI.

    Derived from the StudyInstanceUID tag that is now carried on every stored
    item (0020000D), so the counts cannot drift from what was actually stored.
    """
    by_study: dict[str, dict] = {}
    for item in stored:
        suid = (item.get("0020000D", {}).get("Value") or ["unknown"])[0]
        row = by_study.setdefault(
            suid, {"instances": 0, "existed": bool(study_existed.get(suid, False))}
        )
        row["instances"] += 1
    studies = [
        {
            "study_instance_uid": suid,
            "instances": row["instances"],
            "existed": row["existed"],
        }
        for suid, row in by_study.items()
    ]
    return {
        "instances_stored": len(stored),
        "instances_failed": len(failed),
        "studies_imported": sum(1 for s in studies if not s["existed"]),
        "studies_existed": sum(1 for s in studies if s["existed"]),
        "studies": studies,
    }


def _stow_stored_item(meta: dict) -> dict:
    """The StoredSOPInstanceSequence entry for one accepted part."""
    return {
        "00081150": {"vr": "UI", "Value": [meta.get("sop_class_uid") or "1.2.840.10008.1.1"]},
        "00081155": {"vr": "UI", "Value": [meta.get("sop_instance_uid") or "unknown"]},
        "0020000D": {"vr": "UI", "Value": [meta.get("study_instance_uid") or "unknown"]},
    }


def _stow_failed_item(reason: int) -> dict:
    """A FailedSOPSequence entry with the given failure reason."""
    return {
        "00081150": {"vr": "UI", "Value": ["1.2.840.10008.1.1"]},
        "00081155": {"vr": "UI", "Value": ["unknown"]},
        "00081197": {"vr": "US", "Value": [reason]},
    }


@dataclass
class _StowPartOutcome:
    """What happened to one STOW-RS part (C-10 streaming ingest).

    A small result object rather than a raise, because the batch must keep
    accepting later parts after one is refused: the router turns ``kind`` into
    the right sequence entry and the right final HTTP status.
    """

    kind: str  # "stored" | "uid_error" | "quota" | "failed"
    stored_item: dict | None = None
    failed_item: dict | None = None
    uid_error: dict | None = None
    quota_exc: QuotaExceeded | None = None


_STOW_PROMOTION_SEMAPHORE: asyncio.Semaphore | None = None


def _stow_promotion_semaphore() -> asyncio.Semaphore | None:
    """Process-wide cap on concurrent STOW promotions (H-01, N-5); None = disabled (0)."""
    global _STOW_PROMOTION_SEMAPHORE
    limit = settings.STOW_MAX_CONCURRENT_PROMOTIONS
    if limit <= 0:
        return None
    if _STOW_PROMOTION_SEMAPHORE is None:
        _STOW_PROMOTION_SEMAPHORE = asyncio.Semaphore(limit)
    return _STOW_PROMOTION_SEMAPHORE


async def _promote_stow_part(db: AsyncSession, staged, sop_instance_uid: str,
                             created: bool) -> None:
    """Publish a committed STOW part off the loop; compensate the row on failure.

    H-01: on S3, ``promote`` is an ``upload_fileobj`` of up to
    ``STOW_MAX_PART_BYTES``, so it runs in a worker thread and the number in
    flight is bounded. H-07: the row is already committed; when the promote
    raises, the row this request created is removed before the error reaches
    the per-part handler (which discards the spool and reports a failure).
    """
    semaphore = _stow_promotion_semaphore()
    try:
        if semaphore is None:
            await run_blocking(staged.promote)
        else:
            async with semaphore:
                await run_blocking(staged.promote)
    except Exception:
        try:
            await indexing_service.compensate_unpublished_instance(
                db, sop_instance_uid, created=created, performed_by="stow-rs",
            )
        except Exception as comp_exc:  # report the promote failure, not this one
            log.error("STOW compensation failed sop_instance_uid=%s error_type=%s",
                      sop_instance_uid, type(comp_exc).__name__)
            await db.rollback()
        raise


async def _ingest_stow_part(db: AsyncSession, part, user, study_existed: dict,
                            target_study_uid: str | None,
                            *, study_lock: asyncio.Lock | None = None
                            ) -> tuple[_StowPartOutcome, str | None]:
    """Adopt one spooled STOW part, index it, and publish it (C-10).

    Extracted so the router stays a thin stream consumer (AGENTS.md §2.1). The
    spooled part *is* the staged object: quota is reserved for its measured size,
    the dataset is read metadata-only (pixel data is never paged into memory),
    the row is indexed and committed, and only then is the spool promoted to its
    canonical key. Any failure discards the spool and releases the reservation,
    so a refused part leaves no object, no row and no temp file behind.

    Returns ``(outcome, target_study_uid)``. Raises ``HTTPException`` for the
    request-level refusals (400 invalid identifier, 409 cross-tenant conflict)
    that must surface verbatim rather than as a per-part failure.
    """
    staged = None
    try:
        # H-01: every sync parse/stat/hash/upload below runs off the event loop.
        ds = await run_blocking(dataset_from_path, part.path, stop_before_pixels=True)
        # A-17 + C05: refuse unusable Type 1 UIDs and path-dangerous
        # identifiers before anything is staged, indexed or written.
        uids = _validate_ingest_uids(ds)
        study_uid_meta = uids["StudyInstanceUID"]
        # A-9: ownership of the study/series/SOP UIDs, before staging.
        await _reject_cross_tenant_ingest(db, ds, user)
        ingest_tenant = stow_tenant_scope(user)
        if study_lock is None:
            if study_uid_meta not in study_existed:
                study_existed[study_uid_meta] = await query_service.study_exists(
                    db, study_uid_meta
                )
        else:
            async with study_lock:
                if study_uid_meta not in study_existed:
                    study_existed[study_uid_meta] = await query_service.study_exists(
                        db, study_uid_meta
                    )
        # Multi-storage: new studies go to the registered default backend
        # (None = env backend); late slices rejoin the study's recorded one.
        backend = await run_blocking(storage_service.resolve_study_backend, study_uid_meta)
        key = storage_service.instance_key(
            study_uid_meta, uids["SeriesInstanceUID"], uids["SOPInstanceUID"],
        )
        # H08 + H09 + C-10: reserve quota for the *measured* spool size, adopt
        # the spool as the staged object, index, commit, then publish.
        staged = await run_blocking(
            storage_service.stage_spooled,
            part.path, key, tenant_id=ingest_tenant, backend_name=backend,
            size=part.size, sha256=part.sha256,
        )
        meta = await run_blocking(parse_metadata, ds)
        if target_study_uid is None and meta.get("study_instance_uid"):
            target_study_uid = meta["study_instance_uid"]
        # VNA: STOW-RS is HTTP — the tenant comes from the caller's user
        # (admins/unassigned stay in the legacy unscoped scope).
        instance = await index_new_instance(
            db, meta, staged.key, staged.sha256, performed_by="stow-rs",
            tenant_id=ingest_tenant, storage_backend=backend,
            read_path=staged.staged_path or staged.key,
            # H09: hand over the dataset already parsed — the object is still
            # staged, so nothing may re-read the canonical key.
            ds=ds,
            file_size_bytes=staged.bytes_written,
            include_all=bool(user is not None and user.role == "ADMIN"),
        )
        # Commit per part, then publish: the object only becomes visible once
        # its row is durable.
        await db.commit()
        created = indexing_service.instance_was_created(instance)
        await _promote_stow_part(db, staged, uids["SOPInstanceUID"], created)
        staged = None
        return _StowPartOutcome("stored", stored_item=_stow_stored_item(meta)), target_study_uid
    except dicom_uids.Type1UIDError as exc:
        # A-17: refuse this part, but preserve STOW-RS per-part semantics so
        # later valid parts proceed. The companion detail names the offending
        # attribute without echoing patient data or hostile values.
        log.warning("STOW part rejected unusable Type 1 UID error_type=%s", type(exc).__name__)
        sop_class_uid = _nonempty_uid_value(ds, "SOPClassUID")
        if sop_class_uid is None:
            sop_class_uid = _nonempty_uid_value(
                getattr(ds, "file_meta", None), "MediaStorageSOPClassUID"
            )
        failed_item = dicom_uids.failed_sop_item(
            exc,
            sop_class_uid=sop_class_uid,
            sop_instance_uid=_nonempty_uid_value(ds, "SOPInstanceUID"),
        )
        return (
            _StowPartOutcome("uid_error", failed_item=failed_item, uid_error=exc.as_dict()),
            target_study_uid,
        )
    except QuotaExceeded as exc:
        # H08 + C-17: Out of Resources — reported per part with 0x0111; the
        # router decides 413 (tenant cap) vs 507 (full backend).
        log.warning("STOW part refused by quota: %s", exc)
        return _StowPartOutcome("quota", quota_exc=exc), target_study_uid
    except storage_service.InvalidStorageKey as exc:
        # C05: a malformed identifier is a bad request, never a partial failure.
        raise HTTPException(400, f"Invalid DICOM identifier: {exc}") from exc
    except CrossTenantIngestError as exc:
        # C04/A-9: well-formed but conflicts with another institution's data.
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - boundary guard: one bad part must not abort the batch
        log.warning("STOW part failed error_type=%s", type(exc).__name__)
        # H09: compensate — drop the staged spool and release quota. A
        # pre-existing canonical object is never touched.
        if staged is not None:
            await db.rollback()
            staged.discard()
        return _StowPartOutcome("failed"), target_study_uid
    finally:
        # Idempotent: the spool is already gone when it was promoted or
        # discarded, so this only removes a part refused before adoption.
        part.cleanup()


async def _ingest_stow_part_isolated(
    part, user, study_existed: dict, target_study_uid: str | None,
    study_lock: asyncio.Lock,
) -> tuple[_StowPartOutcome, str | None]:
    """Ingest one part with its own session so bounded workers never share ORM state."""
    async with AsyncSessionLocal() as part_db:
        return await _ingest_stow_part(
            part_db, part, user, study_existed, target_study_uid,
            study_lock=study_lock,
        )


def _stow_part_concurrency(db: AsyncSession) -> int:
    """Bounded STOW workers (PostgreSQL admits concurrent writers)."""
    return max(1, int(settings.STOW_MAX_CONCURRENT_PROMOTIONS))


async def _drain_stow_parts(
    pending: list[asyncio.Task[tuple[_StowPartOutcome, str | None]]],
) -> list[tuple[_StowPartOutcome, str | None]]:
    """Await every active part so request errors cannot leak ingest tasks."""
    if not pending:
        return []
    results = await asyncio.gather(*pending, return_exceptions=True)
    clean: list[tuple[_StowPartOutcome, str | None]] = []
    for result in results:
        if isinstance(result, BaseException):
            raise result
        clean.append(result)
    return clean


@router.post("/studies")
@router.post("/studies/{study_uid}")
async def stow_studies(request: Request, study_uid: str | None = None,
                       db: AsyncSession = Depends(get_db),
                       user: User | None = Depends(get_optional_user)) -> JSONResponse:
    """STOW-RS: ingest DICOM instances from a multipart/related POST.

    C-10: the body is streamed part-by-part and each part is **spooled to a temp
    file on disk** rather than accumulated in memory — a multi-GB series (or a
    single multi-frame instance) never becomes a multi-GB RSS spike. The spool
    file *is* the :class:`~app.services.storage_service.StagedObject`: it is
    promoted to its canonical key only after the part is indexed and the
    database commits. The per-instance ceiling
    (``MAX_INSTANCE_UPLOAD_BYTES``) is enforced as the bytes arrive, so an
    oversized part is aborted with HTTP 413 before it completes; the
    ``STOW_MAX_*_BYTES`` caps bound a part and the whole request the same way.

    Parts are processed as they land: each instance is spooled, indexed and
    published, so a single malformed part fails in isolation without aborting
    the rest of the upload.
    """
    content_type = request.headers.get("content-type", "")
    accept = request.headers.get("accept", "").lower()
    # Keep PAXS upload clients' extra summary fields behind ordinary JSON
    # negotiation. Standard DICOMweb peers receive only the DICOM JSON model;
    # adding UI fields at its top level makes valid STOW responses unparsable.
    legacy_response = (
        "application/json" in accept and "application/dicom+json" not in accept
    ) or bool(request.headers.get("origin") and "application/dicom+json" not in accept)
    stream = request.stream()
    # Empty-body guard: stream() yields nothing for an empty request. Pull the
    # first chunk separately so we can distinguish "no body" (400) from a
    # valid multipart envelope.
    try:
        first_chunk = await anext(stream)
    except StopAsyncIteration:
        raise HTTPException(400, "Empty request body") from None
    if not first_chunk:
        raise HTTPException(400, "Empty request body")

    async def _stream():
        yield first_chunk
        async for chunk in stream:
            yield chunk

    stored: list[dict] = []
    failed: list[dict] = []
    uid_errors: list[dict] = []
    uid_refused = False
    # C-17: the HTTP status the batch ends with when a part is refused for
    # quota. A tenant at its cap is a client condition (413 — this institution
    # may not store more); a full backend is an archive condition (507). The
    # *tenant* refusal outranks a backend one, so a tenant-limit 413 is not
    # masked by a later backend-limit 507.
    quota_refused = False
    quota_status = 507
    target_study_uid = study_uid
    # Whether each study UID already existed in the archive before this upload
    # started — checked once per unique study, so the response can tell the UI
    # which studies were newly imported vs. re-imported (already existed).
    study_existed: dict[str, bool] = {}
    study_lock = asyncio.Lock()
    pending_parts: list[asyncio.Task[tuple[_StowPartOutcome, str | None]]] = []
    part_results: list[tuple[_StowPartOutcome, str | None]] = []
    part_limit = _stow_part_concurrency(db)
    # C-10: spool parts into the destination backend's staging directory, so a
    # local promote is a same-filesystem rename (S3 uploads from the spool).
    spool_backend = (
        await run_blocking(storage_service.resolve_study_backend, study_uid)
        if study_uid else None
    )
    spool_dir = storage_service.staging_dir(spool_backend)
    # The hard per-instance ceiling: the strictest configured cap, so an
    # oversized part is refused whatever STOW_MAX_PART_BYTES is tuned to (the
    # 512 MiB default is the documented bound).
    per_part_cap = min(
        cap for cap in (settings.MAX_INSTANCE_UPLOAD_BYTES,
                        settings.STOW_MAX_PART_BYTES) if cap
    )
    try:
        async for ctype, part in iter_stow_parts_spooled(
            _stream(), content_type, spool_dir,
            max_part_bytes=per_part_cap,
            max_total_bytes=settings.STOW_MAX_TOTAL_BYTES,
        ):
            if not part.size:
                part.cleanup()
                continue
            if ctype != "application/dicom":
                # Be lenient: some clients label DICOM parts differently.
                try:
                    await run_blocking(
                        dataset_from_path, part.path, force=True, stop_before_pixels=True
                    )
                except Exception:  # noqa: BLE001 - boundary guard: a non-DICOM part is skipped
                    part.cleanup()
                    continue
            pending_parts.append(asyncio.create_task(
                _ingest_stow_part_isolated(
                    part, user, study_existed, target_study_uid, study_lock
                )
            ))
            if len(pending_parts) >= part_limit:
                part_results.extend(await _drain_stow_parts(pending_parts))
                pending_parts.clear()
        if pending_parts:
            part_results.extend(await _drain_stow_parts(pending_parts))
            pending_parts.clear()
        for outcome, result_study_uid in part_results:
            if target_study_uid is None and result_study_uid is not None:
                target_study_uid = result_study_uid
            if outcome.kind == "stored":
                stored.append(outcome.stored_item)
            elif outcome.kind == "uid_error":
                failed.append(outcome.failed_item)
                uid_errors.append(outcome.uid_error)
                uid_refused = True
            elif outcome.kind == "quota":
                quota_refused = True
                if outcome.quota_exc.tenant_limited:
                    quota_status = 413  # a tenant may not store more
                elif quota_status != 413:
                    quota_status = outcome.quota_exc.http_status
                failed.append(_stow_failed_item(0x0111))
            else:
                failed.append(_stow_failed_item(0xC212))
    except MalformedStowMultipart:
        raise HTTPException(400, "Malformed multipart body") from None
    except StowPartTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    except HTTPException:
        # Already a precise client error (e.g. the invalid-identifier 400 above).
        raise
    except Exception as exc:
        log.warning("STOW stream error error_type=%s", type(exc).__name__)
        raise HTTPException(400, "Malformed multipart body") from None

    return _stow_response(
        stored,
        failed,
        target_study_uid,
        study_existed,
        # C-17: 413 when a tenant-wide cap refused a part (a client condition),
        # 507 when a full backend did (an archive condition). An all-invalid
        # Type 1 UID request is a client error; mixed batches retain the normal
        # partial-STOW response and identify refused SOPs in 00081198.
        status_code=(
            quota_status if quota_refused
            else (400 if uid_refused and not stored else 200)
        ),
        uid_errors=uid_errors,
        legacy_response=legacy_response,
    )
