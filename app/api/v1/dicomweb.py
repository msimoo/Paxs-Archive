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
import hashlib
import io
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    current_scope,
    get_optional_user,
    get_share_scope,
    require_dicomweb_access,
    require_study_access,
)
from app.core.config import settings
from app.db.models import Instance, Series, Study, User
from app.db.session import get_db
from app.dicom.parser import parse_metadata
from app.services import query_service, storage_service, tenant_service, warm_service
from app.services.quota_service import QuotaExceeded
from app.services.dicomweb_service import (
    StowPartTooLarge,
    build_multipart,
    dataset_from_bytes,
    dataset_to_dicom_json,
    instance_to_dicom_json,
    iter_stow_parts,
    new_boundary,
    series_to_dicom_json,
    study_to_dicom_json,
)
from app.services.indexing_service import CrossTenantIngestError, index_new_instance

log = logging.getLogger("paxs.api.dicomweb")

router = APIRouter(
    prefix="/dicom-web",
    tags=["DICOMweb"],
    # C03: the whole DICOMweb surface is guarded here, so every current and
    # future route is covered by construction. With DICOMWEB_PUBLIC=false reads
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
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(int(params.pop("offset", 0)), 0)
    except (TypeError, ValueError):
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


def _keyset_predicate(cursor: tuple[str | None, int]):
    """WHERE clause for rows *after* `cursor` in the QIDO studies ordering
    ``(study_date IS NOT NULL DESC, study_date DESC, id DESC)``.

    Non-null dates come first (newest first, id tiebreak); NULL dates sort
    last. Rows after a (date, id) cursor are therefore:
      date IS NULL  OR  date < cursor_date  OR  (date = cursor_date AND id < cursor_id)
    and after a NULL-date cursor, only later NULL-date rows with a smaller id.
    """
    cursor_date, cursor_id = cursor
    if cursor_date is None:
        return and_(
            Study.study_date.is_(None),
            Study.id < cursor_id,
        )
    return or_(
        Study.study_date.is_(None),
        Study.study_date < cursor_date,
        and_(Study.study_date == cursor_date, Study.id < cursor_id),
    )


# --------------------------------------------------------------------------- #
# Caching — instances are immutable once stored, so pixel data / frames /
# rendered images can be cached aggressively (huge win for CT/MRI stacks:
# re-opening a study or scrolling back over slices already fetched hits the
# browser cache instead of round-tripping disk read + decode again).
# --------------------------------------------------------------------------- #
IMMUTABLE_CACHE_CONTROL = "private, max-age=31536000, immutable"


def _etag_for(data: bytes) -> str:
    return 'W/"' + hashlib.md5(data).hexdigest() + '"'


def _cached_response(request: Request, data: bytes, media_type: str) -> Response:
    """Response with a content-hash ETag; short-circuits to 304 when the
    client's If-None-Match already matches (no body sent at all)."""
    etag = _etag_for(data)
    headers = {"ETag": etag, "Cache-Control": IMMUTABLE_CACHE_CONTROL}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
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
    inst = (
        await db.execute(
            query_service.instance_query(
                study_uid or None,
                series_uid or None,
                {"SOPInstanceUID": object_uid},
                tenant_id=_tenant_id,
                include_all=_include_all,
            )
        )
    ).scalar_one_or_none()
    if inst is None or not await asyncio.to_thread(storage_service.exists, inst.file_path, backend_name=_bk(inst)):
        raise HTTPException(404, "Instance not found")

    # C04: WADO-URI takes the object UID from the query string, so the owning
    # study is resolved here and the caller's tenant (or share scope) is
    # enforced against it — a share link must never retrieve another study's
    # instance, and a tenant user must not reach across institutions.
    owning_study = (
        await db.execute(
            select(Study.study_instance_uid)
            .join(Series, Series.study_id == Study.id)
            .join(Instance, Instance.series_id == Series.id)
            .where(Instance.id == inst.id)
        )
    ).scalar_one_or_none()
    _wado_user = await get_optional_user(request, None)
    if owning_study is not None and not (_wado_user is None and settings.DICOMWEB_PUBLIC):
        await require_study_access(
            db,
            owning_study,
            _wado_user,
            share_scope=None if settings.DICOMWEB_PUBLIC else await get_share_scope(request),
        )

    content_type = params.get("contentType", "application/dicom").lower()
    if content_type in ("image/jpeg", "image/jpg", "image/png"):
        from app.services.imaging import dataset_to_bytes

        img_fmt = "PNG" if content_type == "image/png" else "JPEG"

        def _render() -> bytes:
            ds = storage_service.read_dataset(inst.file_path, backend_name=_bk(inst))
            return dataset_to_bytes(ds, fmt=img_fmt)

        try:
            data = await asyncio.to_thread(_render)
        except Exception as exc:
            raise HTTPException(422, f"Cannot render instance: {exc}") from exc
        return _cached_response(request, data, "image/png" if img_fmt == "PNG" else "image/jpeg")

    # Audit: RETRIEVED (consistent with WADO-RS rendered).
    from app.services.audit_service import add_audit

    add_audit(db, inst.sop_instance_uid, "RETRIEVED", performed_by="wado-uri")

    # Default: the raw DICOM part-10 object.
    data = await asyncio.to_thread(storage_service.read_bytes, inst.file_path, backend_name=_bk(inst))
    response = _cached_response(request, data, "application/dicom")
    response.headers["Content-Disposition"] = f'inline; filename="{object_uid}.dcm"'
    return response


async def _anonymous_share_scope(request: Request, user) -> str | None:
    """Share scope for an anonymous caller in private DICOMweb mode, else None.

    A share token is a *fallback identity* for people with no account, never an
    override: an authenticated user keeps full visibility, and public mode is
    unchanged (so a stale share cookie cannot narrow a normal viewer session).
    """
    if settings.DICOMWEB_PUBLIC or user is not None:
        return None
    return await get_share_scope(request)


async def _reject_cross_tenant_ingest(db: AsyncSession, study_uid: str, user) -> None:
    """C04: refuse to append an instance to another tenant's study.

    Checked *before* the object is written, so a rejected ingest leaves nothing
    behind on disk. Mirrors the tenant STOW derives for the study itself
    (admins and unassigned users stay in the legacy unscoped scope).
    """
    if user is None or user.role == "ADMIN" or user.tenant_id is None:
        return
    existing = (
        await db.execute(
            select(Study.tenant_id).where(Study.study_instance_uid == study_uid)
        )
    ).scalar_one_or_none()
    if existing is not None and existing != user.tenant_id:
        raise CrossTenantIngestError(
            f"study {study_uid} belongs to another tenant; refusing to append to it"
        )


def _validate_ingest_uids(ds) -> None:
    """Reject dangerous UIDs in an incoming dataset before it is stored (C05).

    Only *path-dangerous* values are refused (dot segments, separators, control
    characters, Windows device names, overlong values). Technically
    non-standard-but-harmless UIDs that real archives contain are still accepted,
    so legitimate clinical data keeps ingesting.

    Raises:
        storage_service.InvalidStorageKey: when a present UID is unsafe.
    """
    for attr in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"):
        value = getattr(ds, attr, None)
        if value in (None, ""):
            # Missing UIDs keep the pre-existing deterministic fallback.
            continue
        storage_service.validate_component(str(value), field=attr)


# --------------------------------------------------------------------------- #
# QIDO-RS
# --------------------------------------------------------------------------- #
@router.get("/studies", response_class=JSONResponse)
async def qido_studies(request: Request, db: AsyncSession = Depends(get_db),
                      user: User | None = Depends(get_optional_user)):
    params = {k: v for k, v in request.query_params.items()}
    limit, offset = _paginate(params)
    params.pop("includefield", None)
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
    base = query_service.study_query(params, tenant_id=tenant_id, include_all=include_all)
    # Keyset pagination: ``?cursor=<study_date>|<id>`` walks pages without
    # OFFSET (which MySQL must re-scan from the start past ~1000 rows).
    # The cursor is the last row's (study_date, id) — see _keyset_predicate.
    cursor = _keyset_cursor(params.pop("cursor", None))
    if cursor is not None:
        base = base.where(_keyset_predicate(cursor))
    effective_offset = 0 if cursor is not None else offset
    stmt = (
        base.order_by(Study.study_date.is_not(None).desc(), Study.study_date.desc(), Study.id.desc())
        .limit(limit)
        .offset(effective_offset)
    )
    studies = (await db.execute(stmt)).scalars().unique().all()
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
    if len(results) == limit and results:
        has_more = (
            await db.execute(
                base.order_by(
                    Study.study_date.is_not(None).desc(),
                    Study.study_date.desc(),
                    Study.id.desc(),
                )
                .limit(1)
                .offset(effective_offset + limit)
            )
        ).first()
        if has_more is not None:
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
    share_scope = await _anonymous_share_scope(request, user)
    if share_scope:
        if study_uid is not None and study_uid != share_scope:
            raise HTTPException(404, "Study not found")
        study_uid = share_scope
    tenant_id, include_all = tenant_service.scope_from_user(user)
    stmt = (
        query_service.series_query(study_uid, params, tenant_id=tenant_id, include_all=include_all)
        .order_by(Series.series_number.is_not(None).desc(), Series.series_number, Series.id)
        .limit(limit)
        .offset(offset)
    )
    series_list = (await db.execute(stmt)).scalars().all()
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
    share_scope = await _anonymous_share_scope(request, user)
    if share_scope:
        if study_uid is not None and study_uid != share_scope:
            raise HTTPException(404, "Study not found")
        study_uid = share_scope
    tenant_id, include_all = tenant_service.scope_from_user(user)
    stmt = (
        query_service.instance_query(study_uid, series_uid, params,
                                     tenant_id=tenant_id, include_all=include_all)
        .order_by(Instance.instance_number.is_not(None).desc(), Instance.instance_number, Instance.id)
        .limit(limit)
        .offset(offset)
    )
    instances = (await db.execute(stmt)).scalars().all()
    results = [instance_to_dicom_json(i) for i in instances]
    return JSONResponse(results, media_type="application/dicom+json")


# --------------------------------------------------------------------------- #
# WADO-RS
# --------------------------------------------------------------------------- #
async def _instances_of(db: AsyncSession, study_uid: str, series_uid: str | None = None):
    # C04: the router-level guard published the caller's scope for this request.
    tenant_id, include_all = current_scope()
    return (
        await db.execute(
            query_service.instance_query(
                study_uid, series_uid, tenant_id=tenant_id, include_all=include_all
            )
        )
    ).scalars().all()


def _read_instances_metadata(instances) -> list[dict]:
    """Blocking metadata read for a batch of instances (disk + pydicom parse
    per instance) — run via asyncio.to_thread so a large CT/MRI study
    (hundreds of instances) doesn't block the event loop while its metadata
    is assembled."""
    results = []
    for inst in instances:
        if storage_service.exists(inst.file_path, backend_name=_bk(inst)):
            try:
                ds = storage_service.read_dataset(inst.file_path, stop_before_pixels=True, backend_name=_bk(inst))
                results.append(dataset_to_dicom_json(ds))
                continue
            except Exception:
                log.debug("metadata read failed for %s", inst.file_path)
        results.append(instance_to_dicom_json(inst))
    return results


@router.get("/studies/{study_uid}/metadata", response_class=JSONResponse)
async def wado_study_metadata(study_uid: str, db: AsyncSession = Depends(get_db)):
    instances = await _instances_of(db, study_uid)
    if not instances:
        raise HTTPException(404, "Study not found")
    # Viewers fetch study metadata right after opening it — warm caches so the
    # subsequent thumbnail/frame requests are already hot (fire-and-forget).
    warm_service.schedule_warm(study_uid)
    results = await asyncio.to_thread(_read_instances_metadata, instances)
    return JSONResponse(results, media_type="application/dicom+json")


@router.get("/studies/{study_uid}/series/{series_uid}/metadata", response_class=JSONResponse)
async def wado_series_metadata(study_uid: str, series_uid: str,
                               db: AsyncSession = Depends(get_db)):
    """WADO-RS series-level metadata (PaxsViewer v3.13+ fetches this to build display sets)."""
    instances = await _instances_of(db, study_uid, series_uid)
    if not instances:
        raise HTTPException(404, "Series not found")
    results = await asyncio.to_thread(_read_instances_metadata, instances)
    return JSONResponse(results, media_type="application/dicom+json")


def _multipart_response(files: list[bytes]) -> Response:
    boundary = new_boundary()
    body = build_multipart(files, boundary)
    return Response(
        content=body,
        media_type=f'multipart/related; type="application/dicom"; boundary={boundary}',
    )


@router.get("/studies/{study_uid}")
async def wado_study(study_uid: str, db: AsyncSession = Depends(get_db)):
    instances = await _instances_of(db, study_uid)
    if not instances:
        raise HTTPException(404, "Study not found")

    def _read_all() -> list[bytes]:
        out = []
        for inst in instances:
            if storage_service.exists(inst.file_path, backend_name=_bk(inst)):
                out.append(storage_service.read_bytes(inst.file_path, backend_name=_bk(inst)))
        return out

    files = await asyncio.to_thread(_read_all)
    return _multipart_response(files)


@router.get("/studies/{study_uid}/series/{series_uid}")
async def wado_series(study_uid: str, series_uid: str, db: AsyncSession = Depends(get_db)):
    instances = await _instances_of(db, study_uid, series_uid)
    if not instances:
        raise HTTPException(404, "Series not found")

    def _read_all() -> list[bytes]:
        return [storage_service.read_bytes(i.file_path, backend_name=_bk(i)) for i in instances
                if storage_service.exists(i.file_path, backend_name=_bk(i))]

    files = await asyncio.to_thread(_read_all)
    return _multipart_response(files)


async def _get_instance(db: AsyncSession, study_uid: str, series_uid: str, sop_uid: str) -> Instance:
    tenant_id, include_all = current_scope()
    inst = (
        await db.execute(
            query_service.instance_query(
                study_uid,
                series_uid,
                {"SOPInstanceUID": sop_uid},
                tenant_id=tenant_id,
                include_all=include_all,
            )
        )
    ).scalar_one_or_none()
    if inst is None:
        raise HTTPException(404, "Instance not found")
    if not await asyncio.to_thread(storage_service.exists, inst.file_path, backend_name=_bk(inst)):
        raise HTTPException(404, "Instance not found")
    return inst


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}")
async def wado_instance(study_uid: str, series_uid: str, sop_uid: str,
                        request: Request, db: AsyncSession = Depends(get_db)):
    inst = await _get_instance(db, study_uid, series_uid, sop_uid)
    data = await asyncio.to_thread(storage_service.read_bytes, inst.file_path, backend_name=_bk(inst))
    accept = request.headers.get("accept", "")
    if "multipart" in accept:
        return _multipart_response([data])
    return _cached_response(request, data, "application/dicom")


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/metadata",
            response_class=JSONResponse)
async def wado_instance_metadata(study_uid: str, series_uid: str, sop_uid: str,
                                 db: AsyncSession = Depends(get_db)):
    inst = await _get_instance(db, study_uid, series_uid, sop_uid)
    ds = await asyncio.to_thread(
        storage_service.read_dataset, inst.file_path, stop_before_pixels=True, backend_name=_bk(inst)
    )
    return JSONResponse(dataset_to_dicom_json(ds), media_type="application/dicom+json")


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/rendered")
async def wado_rendered(study_uid: str, series_uid: str, sop_uid: str,
                        request: Request, db: AsyncSession = Depends(get_db)):
    inst = await _get_instance(db, study_uid, series_uid, sop_uid)
    params = request.query_params
    window = params.get("window")
    viewport = params.get("viewport")
    presentation_uid = params.get("presentationUID")
    max_size = None
    if viewport:
        try:
            w, h = (int(x) for x in viewport.split(",")[:2])
            max_size = max(w, h) or None
        except (TypeError, ValueError):
            max_size = None

    accept = request.headers.get("accept", "image/png")
    fmt = "JPEG" if "jpeg" in accept else "PNG"

    # --- Redis Cache lookup ---
    cache_key = f"paxs:rendered:{sop_uid}:{window or 'auto'}:{max_size or 'full'}:{presentation_uid or 'none'}:{fmt.lower()}"
    from app.services.redis_service import cache_get, cache_set
    cached_bytes = cache_get(cache_key)
    if cached_bytes:
        return _cached_response(request, cached_bytes, "image/jpeg" if fmt == "JPEG" else "image/png")

    # GSPS-aware rendering: if a presentationUID is given, load the GSPS
    # object and apply its display parameters (VOI LUT, spatial transforms,
    # annotations, shutters) on top of the image.
    gsps_content = None
    if presentation_uid:
        try:
            from sqlalchemy import select as sa_select
            from app.db.models import PresentationState
            from app.services.gsps_service import parse_gsps

            ps = await db.execute(
                sa_select(PresentationState).where(
                    PresentationState.sop_instance_uid == presentation_uid
                )
            )
            ps_row = ps.scalar_one_or_none()
            if ps_row and await asyncio.to_thread(storage_service.exists, ps_row.file_path, backend_name=_bk(ps_row)):
                gsps_ds = await asyncio.to_thread(
                    storage_service.read_dataset, ps_row.file_path, stop_before_pixels=True, backend_name=_bk(ps_row)
                )
                gsps_content = parse_gsps(gsps_ds)
                # Use GSPS window/level if no explicit window was requested.
                if not window and gsps_content.window_center is not None and gsps_content.window_width is not None:
                    window = f"{gsps_content.window_center},{gsps_content.window_width}"
        except Exception:
            log.debug("GSPS lookup failed for %s", presentation_uid, exc_info=True)

    def _render() -> bytes:
        ds = storage_service.read_dataset(inst.file_path, backend_name=_bk(inst))
        from app.services.imaging import dataset_to_image

        img = dataset_to_image(ds, window=window, max_size=max_size)

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

    try:
        data = await asyncio.to_thread(_render)
        cache_set(cache_key, data, expire_seconds=600)
    except Exception as exc:
        log.warning("Rendered failed for %s: %s", inst.sop_instance_uid, exc)
        raise HTTPException(422, f"Cannot render instance: {exc}") from exc

    # Audit: RETRIEVED
    from app.services.audit_service import add_audit

    add_audit(db, inst.sop_instance_uid, "RETRIEVED", performed_by="wado-rendered")
    return _cached_response(request, data, "image/jpeg" if fmt == "JPEG" else "image/png")


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/thumbnail")
async def wado_thumbnail(study_uid: str, series_uid: str, sop_uid: str,
                         request: Request, db: AsyncSession = Depends(get_db)):
    """WADO-RS rendered thumbnail (PaxsViewer/PAXs ``thumbnailRendering: 'wadors'``
    requests ``.../instances/{sop}/thumbnail`` to populate the study-list and
    series thumbnails). Serves a small PNG so viewers never 404 on thumbnails.

    Caching, fastest first: (1) the worker's pre-generated 192px PNG object;
    (2) the Redis render cache (keyed sop/window/size/format, 10 min TTL);
    (3) an on-the-fly pydicom read + PIL render. The browser also gets the
    immutable ETag so repeated list renders hit the HTTP cache.
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
        except (TypeError, ValueError):
            max_size = None
    explicit_viewport = viewport is not None
    # Default thumbnail size keeps the payload small for list views.
    if not max_size:
        max_size = 256

    accept = request.headers.get("accept", "image/png")
    fmt = "JPEG" if "jpeg" in accept else "PNG"

    # Fast path: the worker pre-generates a 192px PNG thumbnail per instance at
    # ingest (.thumbnails/{sop}.png — see thumbnail_service). Study-list
    # rendering fires dozens of thumbnail requests per screen; serving the
    # pre-generated object avoids a pydicom read + PIL render on every one.
    # Used whenever no explicit windowing is requested and the requested size
    # fits inside the pre-generated thumbnail (or the client asked for none).
    if fmt == "PNG" and not window and (not explicit_viewport or max_size <= 192):
        key = storage_service.thumbnail_key(inst.sop_instance_uid)
        if await asyncio.to_thread(storage_service.exists, key, backend_name=_bk(inst)):
            data = await asyncio.to_thread(storage_service.read_bytes, key, backend_name=_bk(inst))
            return _cached_response(request, data, "image/png")

    # Redis cache for the rendered path (the expensive pydicom read + PIL
    # render) — same pattern as /rendered. Keyed by sop/window/size/format;
    # instances are immutable once stored, so the TTL is purely eviction policy
    # (rendered/thumbnail bytes never change for a given key). Keeps study-list
    # and series thumbnails instant even when the pre-generated objects are
    # disabled (THUMBNAIL_ENABLED=false) or missing (decode failed at ingest).
    from app.services.redis_service import cache_get, cache_set

    cache_key = (
        f"paxs:thumb:{inst.sop_instance_uid}:{window or 'auto'}:"
        f"{max_size or 'full'}:{fmt.lower()}"
    )
    cached_bytes = cache_get(cache_key)
    if cached_bytes:
        return _cached_response(request, cached_bytes, "image/jpeg" if fmt == "JPEG" else "image/png")

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
        data = await asyncio.to_thread(_render)
        cache_set(cache_key, data, expire_seconds=600)
    except Exception as exc:
        log.warning("Thumbnail failed for %s: %s", inst.sop_instance_uid, exc)
        raise HTTPException(422, f"Cannot render thumbnail: {exc}") from exc
    return _cached_response(request, data, "image/jpeg" if fmt == "JPEG" else "image/png")


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/{frame_list}")
async def wado_frame(
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    frame_list: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """WADO-RS Frame Retrieval (PS 3.18) for single-frame or multi-frame stacks.

    By default ``frame_list`` is the standard file-order frame number (1-based,
    as stored in PixelData). With ``?zorder=1`` the numbers are instead
    interpreted as 0-based **z-order** indices of the volume stack — the same
    geometry as ``/api/v1/volume/{series}/info`` ``frame_map`` and
    ``/mpr/slice?plane=axial&index=N`` — so a thin client can toggle between
    raw frames and resliced planes of a multi-frame instance.
    """
    inst = await _get_instance(db, study_uid, series_uid, sop_uid)
    ds = await asyncio.to_thread(storage_service.read_dataset, inst.file_path, backend_name=_bk(inst))

    try:
        indices = [int(x.strip()) for x in frame_list.split(",") if x.strip().isdigit()]
        if not indices:
            indices = [1]
    except Exception:
        indices = [1]

    zorder = str(request.query_params.get("zorder", "")).lower() in ("1", "true", "yes")
    if zorder:
        # Translate z-order indices through the same expanded stack the volume
        # service uses (multi-frame instances are expanded into per-frame
        # slices ordered by position / dimension steps). Re-parse the raw list
        # as signed ints so negative / malformed values are rejected instead of
        # silently collapsing to frame 1.
        try:
            indices = [int(x.strip()) for x in frame_list.split(",") if x.strip()]
            if not indices:
                indices = [1]
        except ValueError:
            raise HTTPException(404, f"Invalid z-order frame list '{frame_list}'") from None
        from app.services import volume_service

        bundle = await volume_service.volume_bundle(db, series_uid)
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

    number_of_frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    if any(i < 1 or i > number_of_frames for i in indices):
        raise HTTPException(404, f"Frame(s) {frame_list} out of bounds (1-{number_of_frames})")

    accept = request.headers.get("accept", "")
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
        data = await asyncio.to_thread(storage_service.read_bytes, inst.file_path, backend_name=_bk(inst))
        return _cached_response(request, data, "application/dicom")

    from app.services import wado_rs_bulk_service
    from app.services.redis_service import cache_get, cache_set

    # Fast memory cache key for this exact frame / transfer-syntax combination
    transcode = str(request.query_params.get("transcode", "")).lower() in ("1", "true", "yes")
    # OHIF's own generateAcceptHeader QUOTES the parameter it emits
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
    resolved = ",".join(str(i) for i in indices)
    frame_cache_key = f"paxs:frame:{sop_uid}:{resolved}:{cache_mode}:{'mp' if 'multipart' in accept else 'sp'}"

    cached_payload = cache_get(frame_cache_key)
    if cached_payload is not None:
        # Cache stores formatted payload + media-type header
        if b"||PAXS_MT||" in cached_payload:
            cached_data, cached_mt = cached_payload.split(b"||PAXS_MT||", 1)
            return _cached_response(request, cached_data, cached_mt.decode("utf-8", errors="ignore"))
        return _cached_response(request, cached_payload, "application/octet-stream")

    def _extract() -> tuple[bytes | None, str | None]:
        if transcode:
            frames = wado_rs_bulk_service.decode_frames_to_raw(ds, indices)
        elif viewer_mode:
            try:
                frames = wado_rs_bulk_service.extract_frame_bytes(ds, indices)
            except ValueError:
                frames = []
            if not frames:
                frames = wado_rs_bulk_service.decode_frames_to_raw(ds, indices)
        else:
            frames = wado_rs_bulk_service.extract_frame_bytes(ds, indices)
        if not frames:
            return None, None
        if len(frames) == 1 and "multipart" not in accept:
            return frames[0][0], frames[0][1]
        return wado_rs_bulk_service.build_multipart_related_payload(frames)

    # The decode (JPEG2000/JPEG-LS/RLE -> raw) is the expensive step for
    # compressed CT/MRI series — offloading it lets the viewer's dozens of
    # concurrent per-slice frame requests actually run in parallel instead of
    # queueing one-by-one behind the event loop.
    try:
        data, media_type = await asyncio.to_thread(_extract)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if data is None or media_type is None:
        raise HTTPException(404, f"Requested frame(s) {frame_list} not found")

    # Store in high-speed RAM cache and Redis for fast subsequent slice scrubbing.
    # The key MUST stay mode-qualified: the bare "paxs:frame:{sop}:{idx}" namespace
    # belongs to the decoded RAW-PIXEL cache (wado_rs_bulk_service / warm_service).
    # Writing a formatted body (multipart or native codestream) there made a later
    # ?transcode=1 request serve that body as if it were raw pixels.
    if len(data) <= 16 * 1024 * 1024:
        cache_set(frame_cache_key, data + b"||PAXS_MT||" + media_type.encode("utf-8"), expire_seconds=1800)

    return _cached_response(request, data, media_type)


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
        content_bytes, media_type = await asyncio.to_thread(_extract)
        return _cached_response(request, content_bytes, media_type)
    except KeyError as key_err:
        raise HTTPException(404, str(key_err)) from key_err
    except Exception as exc:
        raise HTTPException(400, f"Failed to extract bulk data: {exc}") from exc


# --------------------------------------------------------------------------- #
# STOW-RS
# --------------------------------------------------------------------------- #
def _stow_response(stored: list[dict], failed: list[dict], study_uid: str | None,
                   study_existed: dict[str, bool] | None = None,
                   status_code: int = 200) -> JSONResponse:
    base_uri = f"/dicom-web/studies/{study_uid}" if study_uid else "/dicom-web/studies"
    payload = {
        "00081190": {"vr": "UR", "Value": [base_uri]},
        "00081198": {"vr": "SQ", "Value": failed},
        "00081199": {"vr": "SQ", "Value": stored},
    }
    if study_existed is not None:
        # Per-study summary for the upload UI: how many studies are brand new
        # vs. already present in the archive (re-import / late slices), with
        # the instance count each. Derived from the StudyInstanceUID tag that
        # is now carried on every stored item (0020000D).
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
        payload["summary"] = {
            "instances_stored": len(stored),
            "instances_failed": len(failed),
            "studies_imported": sum(1 for s in studies if not s["existed"]),
            "studies_existed": sum(1 for s in studies if s["existed"]),
            "studies": studies,
        }
    # H08: a quota refusal reports 507 Insufficient Storage for the batch.
    status = status_code if status_code != 200 else (200 if stored else 409)
    return JSONResponse(payload, status_code=status, media_type="application/dicom+json")


@router.post("/studies")
@router.post("/studies/{study_uid}")
async def stow_studies(request: Request, study_uid: str | None = None,
                       db: AsyncSession = Depends(get_db),
                       user: User | None = Depends(get_optional_user)):
    """STOW-RS: ingest DICOM instances from a multipart/related POST.

    The body is streamed part-by-part (one part in memory at a time — a
    multi-GB upload never buffers the whole request), with per-part and
    total caps from the ``STOW_MAX_*_BYTES`` settings rejecting oversized
    uploads with HTTP 413. Parts are processed as they land: each instance is
    saved, indexed and reported, so a single malformed part fails in
    isolation without aborting the rest of the upload.
    """
    content_type = request.headers.get("content-type", "")
    stream = request.stream()
    # Empty-body guard: stream() yields nothing for an empty request. Pull the
    # first chunk separately so we can distinguish "no body" (400) from a
    # valid multipart envelope.
    try:
        first_chunk = await anext(stream)
    except StopAsyncIteration:
        raise HTTPException(400, "Empty request body")
    if not first_chunk:
        raise HTTPException(400, "Empty request body")

    async def _stream():
        yield first_chunk
        async for chunk in stream:
            yield chunk

    stored: list[dict] = []
    failed: list[dict] = []
    quota_refused = False
    staged = None
    target_study_uid = study_uid
    # Whether each study UID already existed in the archive before this upload
    # started — checked once per unique study, so the response can tell the UI
    # which studies were newly imported vs. re-imported (already existed).
    study_existed: dict[str, bool] = {}
    try:
        async for ctype, data in iter_stow_parts(
            _stream(), content_type,
            max_part_bytes=settings.STOW_MAX_PART_BYTES,
            max_total_bytes=settings.STOW_MAX_TOTAL_BYTES,
        ):
            if not data:
                continue
            if ctype != "application/dicom":
                # Be lenient: some clients label DICOM parts differently.
                try:
                    dataset_from_bytes(data, force=True)
                except Exception:
                    continue
            try:
                ds = dataset_from_bytes(data)
                # C05: hard-reject dangerous identifiers at the door (400) so
                # they can never reach the storage key builder.
                _validate_ingest_uids(ds)
                study_uid_meta = str(getattr(ds, "StudyInstanceUID", "")) or "UNKNOWN"
                await _reject_cross_tenant_ingest(db, study_uid_meta, user)
                if study_uid_meta not in study_existed:
                    exists = (
                        await db.execute(
                            select(Study.id)
                            .where(Study.study_instance_uid == study_uid_meta)
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    study_existed[study_uid_meta] = exists is not None
                # Multi-storage: write new studies to the registered default
                # backend (None = env backend); late slices rejoin the study's
                # recorded one.
                backend = storage_service.resolve_study_backend(
                    str(getattr(ds, "StudyInstanceUID", "UNKNOWN"))
                )
                # H08 + H09: reserve quota, stage the object, index, commit,
                # and only then publish it. A failure discards the staged file
                # and releases the reservation, leaving any existing object
                # untouched.
                staged = storage_service.stage_dataset_with_quota(
                    ds,
                    tenant_id=(user.tenant_id if user and user.role != "ADMIN" else None),
                    backend_name=backend,
                )
                path, sha256 = staged.key, staged.sha256
                meta = parse_metadata(ds)
                if target_study_uid is None and meta.get("study_instance_uid"):
                    target_study_uid = meta["study_instance_uid"]
                # VNA: STOW-RS is HTTP — the tenant comes from the caller's
                # user (admins/unassigned stay in the legacy unscoped scope).
                await index_new_instance(
                    db, meta, path, sha256, performed_by="stow-rs",
                    tenant_id=(user.tenant_id if user and user.role != "ADMIN" else None),
                    storage_backend=backend,
                    read_path=staged.staged_path or staged.key,
                    # H09: hand over the dataset we already parsed — the object
                    # is still staged, so nothing may re-read the canonical key.
                    ds=ds,
                    # …and the size measured while staging.
                    file_size_bytes=staged.bytes_written,
                )
                # Commit per part, then publish: the object only becomes visible
                # once its row is durable, and a later part cannot roll this one
                # back after the file has been published.
                await db.commit()
                staged.promote()
                stored.append({
                    "00081150": {"vr": "UI", "Value": [meta.get("sop_class_uid") or "1.2.840.10008.1.1"]},
                    "00081155": {"vr": "UI", "Value": [meta.get("sop_instance_uid") or "unknown"]},
                    "0020000D": {"vr": "UI", "Value": [meta.get("study_instance_uid") or "unknown"]},
                })
            except QuotaExceeded as exc:
                # H08: Out of Resources — recorded per part with 0x0111 and
                # surfaced as HTTP 507 once the batch completes.
                log.warning("STOW part refused by quota: %s", exc)
                quota_refused = True
                failed.append({
                    "00081150": {"vr": "UI", "Value": ["1.2.840.10008.1.1"]},
                    "00081155": {"vr": "UI", "Value": ["unknown"]},
                    "00081197": {"vr": "US", "Value": [0x0111]},
                })
            except storage_service.InvalidStorageKey as exc:
                # C05: a malformed identifier is a bad request, never a partial
                # failure — refuse the whole STOW rather than store anything.
                raise HTTPException(400, f"Invalid DICOM identifier: {exc}") from exc
            except CrossTenantIngestError as exc:
                # C04: writing into another institution's study is refused too.
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:
                log.warning("STOW part failed: %s", exc)
                # H09: compensate — drop the staged file and release quota. A
                # pre-existing canonical object is never touched.
                if staged is not None:
                    await db.rollback()
                    staged.discard()
                    staged = None
                failed.append({
                    "00081150": {"vr": "UI", "Value": ["1.2.840.10008.1.1"]},
                    "00081155": {"vr": "UI", "Value": ["unknown"]},
                    "00081197": {"vr": "US", "Value": [0xC212]},
                })
    except StowPartTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    except HTTPException:
        # Already a precise client error (e.g. the invalid-identifier 400 above).
        raise
    except Exception as exc:
        log.warning("STOW stream error: %s", exc)
        raise HTTPException(400, f"Malformed multipart body: {exc}") from exc

    return _stow_response(
        stored,
        failed,
        target_study_uid,
        study_existed,
        # 507 Insufficient Storage when any part was refused for quota.
        status_code=507 if quota_refused else 200,
    )
