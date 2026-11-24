"""Human-facing REST API for the web dashboard: stats, study browsing,
thumbnails, deletion. DICOMweb-standard endpoints live in dicomweb.py.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    assert_can_delete_studies,
    get_current_user,
    get_current_user_any,
    require_admin,
    require_destructive_role,
    require_instance_access,
    require_series_access,
    require_study_access,
    tenant_scope,
)
from app.core.config import resolve_public_base_url, settings
from app.core.concurrency import run_blocking
from app.db.models import User
from app.db.session import get_db, get_read_db
from app.services import (
    dicom_health_service,
    dashboard_service,
    query_service,
    storage_admin_service,
    storage_service,
    thumbnail_service,
    volume_service,
)
from app.services.audit_service import add_audit
from app.services.compression_service import codec_status

log = logging.getLogger("paxs.api.dashboard")

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

MODALITY_COLORS = {
    "CT": "#38bdf8", "MR": "#a78bfa", "CR": "#fbbf24", "DX": "#fb923c",
    "US": "#34d399", "NM": "#fb7185", "PT": "#f472b6", "XA": "#22d3ee",
    "OT": "#94a3b8", "MG": "#e879f9",
}


# DIMSE service capability matrix — the SOP classes the receiver advertises
# and the SCU helpers we ship. Surfaces every completed Core DICOM milestone
# (C-STORE / C-FIND / C-GET / C-MOVE / C-ECHO, MWL, MPPS, Storage Commitment)
# plus the web/integration layers on the admin dashboard.
DIMSE_SERVICES = [
    ("C-STORE", "1.2.840.10008.5.1.4.1.1", "SCP + SCU"),
    ("C-FIND (Study Root)", "1.2.840.10008.5.1.4.1.2.2.1", "SCP + SCU"),
    ("C-GET (Study Root)", "1.2.840.10008.5.1.4.1.2.2.3", "SCP + SCU"),
    ("C-MOVE (Study Root)", "1.2.840.10008.5.1.4.1.2.2.2", "SCP + SCU"),
    ("C-ECHO (Verification)", "1.2.840.10008.1.1", "SCP + SCU"),
    ("Modality Worklist (MWL)", "1.2.840.10008.5.1.4.31", "SCP"),
    ("MPPS", "1.2.840.10008.3.1.2.3.3", "SCP + SCU"),
    ("Storage Commitment", "1.2.840.10008.1.20.1", "SCP + SCU"),
]

WEB_SERVICES = [
    ("DICOMweb (QIDO/WADO/STOW)", "REST /dicom-web", True),
    ("HL7 MLLP (HIS ADT + ORM)", "MLLP", settings.HL7_ENABLED),
    ("HL7 ORU (report → EMR)", "MLLP out", settings.HL7_EMR_ENABLED),
    ("FHIR R4", "REST /fhir", settings.FHIR_ENABLED),
]


def _dicom_services_matrix(dicom_live: bool) -> list[dict]:
    """DIMSE service list whose enabled state comes from the live probe."""
    dimse_on = bool(dicom_live)
    return [
        {"name": name, "sop_class": sop, "roles": roles, "enabled": dimse_on}
        for (name, sop, roles) in DIMSE_SERVICES
    ]


def _script_count() -> int:
    """Number of custom server scripts on disk (roadmap #43)."""
    from app.services import scripting_service

    return len(scripting_service.list_scripts())


def _named_backend_count() -> int:
    """Number of registered named storage backends (multi-storage)."""
    return storage_admin_service.named_backend_count_sync()



@router.get("/health/live")
async def health_live():
    """Liveness probe (HAProxy httpchk) — the process is up."""
    return {"status": "ok", "instance": settings.PAXS_INSTANCE_ID}


@router.get("/health/ready")
async def health_ready():
    """Readiness probe — the node can serve traffic (DB reachable)."""
    try:
        await dashboard_service.ping_db()
    except Exception as exc:  # varied DB driver failures must not break liveness reporting
        log.error("readiness_check_failed error_type=%s", type(exc).__name__)
        return JSONResponse(
            {"status": "unavailable", "instance": settings.PAXS_INSTANCE_ID},
            status_code=503,
        )
    return {"status": "ready", "instance": settings.PAXS_INSTANCE_ID}


def _compression_block() -> dict:
    """Configured compression method plus per-codec availability.

    Shared by ``/health`` and ``/stats`` so the two never drift: the dashboard
    shows what the compression worker can actually use (RLE is built in;
    JPEG-LS / JPEG2000 / HTJ2K depend on installed codec packages).
    """
    return {
        "method": settings.DEFAULT_COMPRESSION,
        "enabled": settings.COMPRESSION_ENABLED,
        "codecs": codec_status(),
    }


def _volume_streaming_block() -> dict:
    """Volume/MIP capability block, shared by ``/health`` and ``/stats``."""
    return {
        "enabled": settings.VOLUME_STREAMING_ENABLED,
        "max_slices": settings.VOLUME_MAX_SLICES,
        "mpr_slice": settings.VOLUME_STREAMING_ENABLED,
        "mpr_max_pixels": settings.VOLUME_MPR_MAX_PIXELS,
        "mip": {
            "enabled": settings.VOLUME_STREAMING_ENABLED,
            "endpoint": "/api/v1/volume/{series_uid}/mip",
            "modes": ["mip", "minip", "average", "vrt"],
        },
    }


def _fusion_block() -> dict:
    """PET/CT fusion capability block, shared by ``/health`` and ``/stats``."""
    return {"enabled": True, "endpoint": "/api/v1/fusion/{study_uid}"}


def _connection_blocks(dicom_live: bool | None = None) -> dict:
    """Where the archive's inbound connections live (no secrets, no paths)."""
    return {
        "dicom": {
            "ae_title": settings.DICOM_AE_TITLE,
            "host": settings.DICOM_HOST,
            "port": settings.DICOM_PORT,
            "enabled": settings.DICOM_RECEIVER_ENABLED if dicom_live is None else dicom_live,
            "configured": settings.DICOM_RECEIVER_ENABLED,
        },
        "tls": {
            "enabled": settings.DICOM_TLS_ENABLED,
            "cert": str(settings.DICOM_TLS_CERT),
        },
        "hl7": {
            "enabled": settings.HL7_ENABLED,
            "host": settings.HL7_HOST,
            "port": settings.HL7_PORT,
        },
        "storage": {
            "backend": settings.STORAGE_BACKEND,
            "endpoint": settings.S3_ENDPOINT_URL or "aws",
            "bucket": settings.S3_BUCKET,
            "presign_expires": settings.S3_PRESIGN_EXPIRES if settings.STORAGE_BACKEND == "s3" else None,
            "backends": _named_backend_count(),
        },
        "fhir": {"enabled": settings.FHIR_ENABLED},
    }


def _live_web_services() -> list[dict]:
    """Compute live Web & integration layer status from current runtime settings."""
    from app.dicom import hl7 as hl7_mod

    hl7_live = getattr(hl7_mod, "_listener", None) is not None
    webhooks = [u.strip() for u in (settings.WEBHOOK_URLS or "").split(",") if u.strip()]
    return [
        {
            "name": "DICOMweb (QIDO/WADO/STOW)",
            "detail": f"REST /dicom-web · read={'public' if settings.DICOMWEB_PUBLIC_READ else 'auth'} · write={'public' if settings.DICOMWEB_PUBLIC_WRITE else 'auth'}",
            "enabled": True,
        },
        {
            "name": "HL7 MLLP (HIS ADT + ORM)",
            "detail": f"{settings.HL7_HOST}:{settings.HL7_PORT} ({'TLS' if settings.HL7_TLS_ENABLED else 'plain'} · {'running' if hl7_live else 'stopped'})",
            "enabled": bool(hl7_live or settings.HL7_ENABLED),
        },
        {
            "name": "HL7 ORU (report → EMR)",
            "detail": f"{settings.HL7_EMR_HOST}:{settings.HL7_EMR_PORT} ({settings.HL7_EMR_APPLICATION})",
            "enabled": bool(settings.HL7_EMR_ENABLED),
        },
        {
            "name": "FHIR R4",
            "detail": "REST /fhir (ImagingStudy, DiagnosticReport, Patient)",
            "enabled": bool(settings.FHIR_ENABLED),
        },
        {
            "name": "Image Fusion (PET/CT, MR/CT)",
            "detail": "REST /api/v1/fusion",
            "enabled": bool(settings.FUSION_ENABLED),
        },
        {
            "name": "Transfer Accelerator (STOW-RS)",
            "detail": f"{settings.TRANSFER_ACCELERATOR_WORKERS} workers · batch {settings.TRANSFER_ACCELERATOR_BATCH_SIZE}",
            "enabled": bool(settings.TRANSFER_ACCELERATOR_ENABLED),
        },
        {
            "name": "Webhooks",
            "detail": f"{len(webhooks)} endpoint(s) configured" if webhooks else "none configured",
            "enabled": bool(webhooks),
        },
    ]


def _services_block(dicom_probe: dict) -> dict:
    """The DIMSE + web service capability matrix."""
    return {
        "dicom": _dicom_services_matrix(bool(dicom_probe.get("live"))),
        "dicom_enabled": bool(dicom_probe.get("live")),
        "dicom_configured": settings.DICOM_RECEIVER_ENABLED,
        "dicom_probe": dicom_probe,
        "tls_enabled": settings.DICOM_TLS_ENABLED,
        "web": _live_web_services(),
    }


async def health_payload(request: Request | None = None) -> dict:
    """The health/status payload.

    ``request`` is **optional** so in-process callers that reuse this shape
    (``/api/v1/config`` renders it as the read-only block) do not have to
    synthesise one. When absent, ``public_base_url`` falls back to the
    configured ``PUBLIC_BASE_URL`` / loopback default instead of the request's
    host — see ``resolve_public_base_url``.
    """
    db_ok = False
    try:
        await dashboard_service.ping_db()
        db_ok = True
    except Exception as exc:  # health payload degrades to database=error for any probe failure
        log.error("health_check_failed error_type=%s", type(exc).__name__)
    dicom_probe = await asyncio.to_thread(dicom_health_service.probe_receiver)
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "instance": settings.PAXS_INSTANCE_ID,
        # Public base URL the dashboard uses to build absolute viewer/share
        # deep-links, so they resolve to the same origin regardless of which
        # host the operator opened the dashboard from (localhost vs 127.0.0.1
        # vs a reverse-proxy hostname). Never a bind address (V2-2).
        "public_base_url": resolve_public_base_url(request),
        "database": "ok" if db_ok else "error",
        **_connection_blocks(bool(dicom_probe.get("live"))),
        "compression": _compression_block(),
        "volume_streaming": _volume_streaming_block(),
        "fusion": _fusion_block(),
        "rate_limit_per_minute": settings.RATE_LIMIT_GLOBAL_PER_MINUTE,
        "services": _services_block(dicom_probe),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health")
async def health(request: Request, user: User = Depends(require_admin)):
    """Admin health/status. Delegates to ``health_payload`` so the shape has a
    single definition (``/api/v1/config`` reuses it as its read-only block)."""
    return await health_payload(request)


@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_read_db),
                user: User = Depends(get_current_user_any)):
    counts = await dashboard_service.entity_counts(db, user)
    features = await dashboard_service.feature_counts(db, user)
    features.update(await _feature_blocks(db, features, user))
    # Physical storage usage is archive-wide; do not disclose it in tenant views.
    # The cache may walk the storage tree, so keep the sync call off the loop.
    storage_bytes = None
    if user.role == "ADMIN":
        storage_bytes = await run_blocking(
            storage_service.cached_storage_usage_bytes
        )
    return {
        **counts,
        "storage_bytes": storage_bytes,
        "compression": _compression_block(),
        "features": features,
    }


async def _feature_blocks(db, features: dict, user: User) -> dict:
    """The settings-derived and in-process blocks of the ``features`` payload.

    Split out of ``stats`` so the endpoint reads as "count, then report": none
    of this is a query except the reading-worklist counters, and keeping it
    inline pushed the handler past the ceiling for no benefit.
    """
    dicom_probe = await asyncio.to_thread(dicom_health_service.probe_receiver)
    return {
        # Viewer processing distribution (roadmap #51, Phase 5 §5.2). These
        # counters are in-process module state, not DB rows: they make the
        # hybrid engine's own inputs observable, so the dashboard can show the
        # actual load and not just the configuration.
        "viewer_processing": _viewer_processing_telemetry(),
        # Radiologist reading worklist (roadmap #30) — Unread / Stat / Dictated.
        "reading": await dashboard_service.reading_counts(db, user),
        "scripts": {
            "count": _script_count(),
            "enabled": settings.SCRIPTS_ENABLED,
        },
        "replication": {
            "enabled": settings.DR_REPLICATION_ENABLED,
            "pending": features["replication"]["pending"],
            "replicated": features["replication"]["replicated"],
            "failed": features["replication"]["failed"],
        },
        "volume_streaming": _volume_streaming_block(),
        "fusion": _fusion_block(),
        "dicom_services": {
            "total": len(DIMSE_SERVICES),
            "enabled": len(DIMSE_SERVICES) if dicom_probe.get("live") else 0,
            "configured": settings.DICOM_RECEIVER_ENABLED,
            "live": bool(dicom_probe.get("live")),
            "tls": settings.DICOM_TLS_ENABLED,
        },
    }


def _viewer_processing_telemetry() -> dict:
    """Live viewer-processing counters, never failing the dashboard."""
    try:
        return {
            "active_server_sessions": volume_service.active_server_session_count(),
            "server_vol_cache_mb": volume_service.estimated_vol_cache_mb(),
            "configured_mode": settings.VIEWER_PROCESSING_MODE,
            "max_server_sessions": settings.HYBRID_MAX_SERVER_MPR_SESSIONS,
            "vol_budget_mb": settings.HYBRID_SERVER_VOL_BUDGET_MB,
            "streaming_enabled": settings.VOLUME_STREAMING_ENABLED,
            "client_score_threshold": settings.HYBRID_CLIENT_SCORE_THRESHOLD,
            "rtt_threshold_ms": settings.HYBRID_RTT_THRESHOLD_MS,
            "role_overrides": _role_override_summary(),
        }
    except Exception as exc:  # dashboard telemetry is best-effort; retain its zero fallback
        log.error("viewer_telemetry_failed error_type=%s", type(exc).__name__)
        return {
            "active_server_sessions": 0,
            "server_vol_cache_mb": 0,
            "configured_mode": settings.VIEWER_PROCESSING_MODE,
            "max_server_sessions": settings.HYBRID_MAX_SERVER_MPR_SESSIONS,
            "vol_budget_mb": settings.HYBRID_SERVER_VOL_BUDGET_MB,
        }


def _role_override_summary() -> dict:
    """Count of roles pinned by ``VIEWER_PROCESSING_ROLE_MAP``, by mode.

    Returns counts only — the admin dashboard needs "how many roles are pinned
    and to what", not the full map, and a count cannot leak a role name into a
    lower-privileged view of this endpoint.
    """
    try:
        from app.api.v1.volume import parse_role_map

        role_map = parse_role_map()
    except Exception:  # pragma: no cover
        return {"total": 0, "by_mode": {}}
    by_mode: dict[str, int] = {}
    for mode in role_map.values():
        by_mode[mode] = by_mode.get(mode, 0) + 1
    return {"total": len(role_map), "by_mode": by_mode}


@router.get("/audit")
async def list_audit(limit: int = 60, db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_destructive_role)):
    """Recent audit-trail events (tamper-evident SHA-256 fingerprints).

    Admin/radiologist only — the trail exposes SOP/study UIDs and file
    fingerprints, so it is gated behind require_destructive_role.
    """
    tenant_id, include_all = tenant_scope(user)
    return await dashboard_service.list_audit(db, limit, tenant_id, include_all)


@router.get("/studies")
async def list_studies(request: Request, db: AsyncSession = Depends(get_read_db),
                      user: User = Depends(get_current_user_any)):
    filters = dashboard_service.StudyFilters.from_query(dict(request.query_params))
    return await dashboard_service.list_studies(db, user, filters)


@router.get("/triage")
async def list_triage(db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user_any)):
    """List emergency findings visible in the caller's tenant scope."""
    tenant_id, include_all = tenant_scope(user)
    return await dashboard_service.list_triage(
        db, tenant_id=tenant_id, include_all=include_all
    )


@router.post("/triage/{flag_id}/review", status_code=200)
async def review_triage(flag_id: int, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user_any)):
    """Mark a visible triage finding as reviewed (seen by a clinician)."""
    tenant_id, include_all = tenant_scope(user)
    flag = await dashboard_service.get_triage_flag(
        db, flag_id, tenant_id=tenant_id, include_all=include_all
    )
    if flag is None:
        raise HTTPException(404, "Triage flag not found")
    flag.reviewed = True
    return {"ok": True, "id": flag.id}


@router.get("/studies/{study_uid}")
async def study_detail(study_uid: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user_any)):
    # C04: direct UID access must respect the tenant boundary.
    study = await require_study_access(db, study_uid, user)
    study = await dashboard_service.reload_study(db, study)
    series_list = await dashboard_service.series_of_study(db, study.id)
    counts = await query_service.series_instance_counts(
        db, [s.id for s in series_list]
    )
    return _study_detail_payload(study_uid, study, series_list, counts)


def _series_row(study_uid: str, series, count: int) -> dict:
    """One series row for the study drawer, with its thumbnail deep-link."""
    return {
        "series_uid": series.series_instance_uid,
        "modality": series.modality,
        "series_number": series.series_number,
        "description": series.series_description,
        "body_part": series.body_part_examined,
        "instances": count,
        "thumbnail": (
            f"/api/v1/studies/{study_uid}/series"
            f"/{series.series_instance_uid}/thumbnail.png"
            if count else None
        ),
    }


def _study_detail_payload(study_uid: str, study, series_list: list,
                          counts: dict) -> dict:
    """The study-drawer response: patient, study and per-series rows.

    ``study_uid`` is the request's spelling, kept verbatim for the thumbnail
    URLs exactly as before the split (a MySQL case-insensitive match can accept
    a UID that differs in case from the stored one).
    """
    return {
        "study_uid": study.study_instance_uid,
        "patient": {
            "name": study.patient.patient_name,
            "id": study.patient.patient_id,
            "birth_date": study.patient.patient_birth_date,
            "sex": study.patient.patient_sex,
            "phone": study.patient.patient_phone,
            "source": study.patient.source,
        },
        "study": {
            "date": study.study_date,
            "time": study.study_time,
            "description": study.study_description,
            "accession": study.accession_number,
            "referring_physician": study.referring_physician,
        },
        "series": [
            _series_row(study_uid, s, counts.get(s.id, 0)) for s in series_list
        ],
    }


@router.get("/studies/{study_uid}/series/{series_uid}/instances")
async def series_instances(study_uid: str, series_uid: str, db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user_any)):
    # C04: series-addressed access resolves through the study's tenant.
    await require_series_access(db, series_uid, user)
    tenant_id, include_all = tenant_scope(user)
    instances = await dashboard_service.instances_of_series(
        db, study_uid, series_uid, tenant_id, include_all
    )
    return [
        {
            "sop_uid": i.sop_instance_uid,
            "instance_number": i.instance_number,
            "rows": i.rows,
            "columns": i.columns,
            "photometric": i.photometric_interpretation,
            "transfer_syntax": i.transfer_syntax_uid,
            "file_size_bytes": i.file_size_bytes,
        }
        for i in instances
    ]


@router.get("/studies/{study_uid}/series/{series_uid}/thumbnail.png")
async def series_thumbnail(study_uid: str, series_uid: str,
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user_any)):
    await require_series_access(db, series_uid, user)
    tenant_id, include_all = tenant_scope(user)
    rows = await dashboard_service.instances_of_series(
        db, study_uid, series_uid, tenant_id, include_all, limit=1
    )
    inst = rows[0] if rows else None
    if inst is None:
        raise HTTPException(404, "No instances in series")

    thumb = storage_service.thumbnail_key(inst.sop_instance_uid)
    if not await asyncio.to_thread(
        storage_service.exists, thumb, backend_name=inst.storage_backend
    ):
        ok = await asyncio.to_thread(
            thumbnail_service.generate_thumbnail,
            inst.file_path,
            thumb,
            backend_name=inst.storage_backend,
        )
        if not ok:
            # A preview is optional UI decoration; unsupported/corrupt pixel
            # data must not turn a study-list image request into a client error
            # or be replaced with a misleading synthetic medical image.
            return Response(
                content=thumbnail_service.empty_thumbnail_png(),
                media_type="image/png",
                headers={"Cache-Control": "no-store"},
            )
    data = await asyncio.to_thread(
        storage_service.read_bytes, thumb, backend_name=inst.storage_backend
    )
    return Response(content=data, media_type="image/png")


@router.get("/storage/presign/{study_uid}/{series_uid}/{sop_uid}")
async def presign_instance(study_uid: str, series_uid: str, sop_uid: str,
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """Short-lived direct-download URL for one instance.

    S3/MinIO backend: a presigned GET URL. Local backend: the app's own
    WADO-URI endpoint (served directly). Lets heavy downloads bypass the API
    process when object storage is enabled.
    """
    # C04: a presigned URL is direct object access — guard it like any read.
    inst = await require_instance_access(db, study_uid, series_uid, sop_uid, user)
    if not storage_service.exists(inst.file_path):
        raise HTTPException(404, "Instance not found")

    url = storage_service.presigned_url(inst.file_path, backend_name=inst.storage_backend)
    if url is None:
        url = (f"{resolve_public_base_url()}/dicom-web/wado"
               f"?requestType=WADO&objectUID={inst.sop_instance_uid}")
    return {
        "sop_instance_uid": inst.sop_instance_uid,
        "backend": inst.storage_backend or settings.STORAGE_BACKEND,
        "url": url,
        "expires_in": settings.S3_PRESIGN_EXPIRES if settings.STORAGE_BACKEND == "s3" else None,
    }


def _effective_backend(inst, study_backend: str | None) -> str | None:
    """The backend that actually holds ``inst``'s object (A-20).

    ``instances.storage_backend`` is per row; a study can span backends after
    replication or tiering. Rows that predate per-instance attribution fall back
    to the study's backend.
    """
    return getattr(inst, "storage_backend", None) or study_backend


def _fingerprint_instances(rows, study_backend: str | None) -> list[str | None]:
    """SHA-256 of each instance object, read from the backend that holds it.

    ``rows`` is ``(sop_instance_uid, file_path, size, backend_name)`` tuples —
    plain values rather than ORM instances, because this runs on a worker thread
    and after the session's read transaction has been closed.

    Runs off the event loop (C-22): hashing a study means reading every object
    off disk (or S3), which must never happen on the loop that serves other
    viewers' pixel fetches. A missing or unreadable object yields ``None`` — the
    delete is idempotent, so the audit row is still written, just without a
    fingerprint.

    N-2: the warning names the SOP Instance UID and the exception type, never
    the object path (a legacy absolute path can carry PHI).
    """
    out: list[str | None] = []
    for sop_uid, file_path, _size, backend_name in rows:
        try:
            out.append(
                storage_service.sha256_of(
                    file_path, backend_name=backend_name or study_backend
                )
            )
        except (OSError, storage_service.StorageError) as exc:
            log.warning(
                "audit fingerprint unavailable for %s (%s)",
                sop_uid, type(exc).__name__,
            )
            out.append(None)
    return out


@router.delete("/studies/{study_uid}", status_code=204)
async def delete_study(study_uid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(require_destructive_role)):
    """Permanently delete a study, its series, instances and stored objects.

    **ADMIN only.** ``require_destructive_role`` is the outer gate (it keeps the
    PHYSICIAN case a flat 403 before any lookup); the ADMIN narrowing happens
    *after* the tenant-scoped study lookup so an out-of-scope UID still answers
    404 rather than 403 (N-1) — see :func:`deps.assert_can_delete_studies`.
    """
    study = await _authorize_delete(db, study_uid, user)
    plan = _delete_plan(study, await dashboard_service.instances_of_study(db, study.id))

    # Close the read transaction before touching storage: crediting a quota
    # opens its OWN sync connection, and under SQLite's rollback-journal mode an
    # open reader blocks that writer outright ("database is locked"), which left
    # the counters high. Nothing has been written yet, so no work is discarded.
    await db.commit()

    # C-22: fingerprinting reads and hashes every object — off the event loop.
    study.lifecycle_state = "PURGING"
    await db.commit()
    digests = await asyncio.to_thread(
        _fingerprint_instances, plan.rows, plan.study_backend
    )
    try:
        await asyncio.to_thread(_release_objects, study_uid, plan)
    except Exception as exc:  # noqa: BLE001 - storage backends fail with driver-specific exceptions
        # Keep metadata as a retryable marker when storage is unavailable or a
        # recursive cleanup fails after only part of the study was removed.
        log.error(
            "study_delete_storage_failed study_uid=%s error_type=%s",
            study_uid, type(exc).__name__,
        )
        raise HTTPException(
            409, "Study objects could not be deleted; metadata was kept for retry"
        ) from None

    for (sop_uid, _path, _size, _bk), digest in zip(plan.rows, digests, strict=True):
        # The hash was computed on the worker thread above; passing it in (and
        # omitting file_path) keeps add_audit from hashing again on the loop.
        add_audit(db, sop_uid, "DELETED", performed_by=user.username,
                  study_instance_uid=study_uid, sha256=digest)

    await dashboard_service.delete_study_rows(
        db, study_id=plan.study_id, study_uid=study_uid,
        delete_signed_reports=True, performed_by=user.username,
    )
    _defer_study_deleted(db, study_uid, user.username)


async def _authorize_delete(db, study_uid: str, user):
    """Gate a study delete: in-scope and ADMIN-only.

    C04: a destructive role authorises the *action*, not another tenant's data,
    so the tenant-scoped lookup runs first and ``assert_can_delete_studies``
    (ADMIN-only) second — an out-of-scope UID is a 404, never a 403 (N-1).

    Signed reports are deleted later in the same ADMIN-only transaction as the
    study row. Other roles never reach that path.
    """
    study = await require_study_access(db, study_uid, user)
    assert_can_delete_studies(user)
    return study


@dataclass(frozen=True)
class _DeletePlan:
    """Everything the storage phase needs, snapshotted as plain values.

    The values are copied out before ``db.commit()`` because that boundary
    expires the ORM objects, and the storage work runs on a worker thread.
    """

    study_id: int
    study_backend: str | None
    study_tenant: int | None
    #: ``(sop_instance_uid, file_path, size, backend_name)`` per instance.
    rows: list[tuple[str, str, int, str | None]]
    #: backend name -> ``[(file_path, size), …]``
    by_backend: dict[str | None, list[tuple[str, int]]]


def _delete_plan(study, instances: list) -> _DeletePlan:
    """Group a study's objects by the backend that actually holds each one.

    A-20: an archive can keep one study's instances on several backends
    (replication targets, tiering), and the effective backend is per row —
    ``instances.storage_backend``, falling back to the study's for rows that
    predate per-instance attribution. Deleting only ``study.storage_backend``
    left every secondary-backend object on disk as orphaned PHI and credited the
    quota release to the wrong store.
    """
    study_backend = study.storage_backend
    rows = [
        (inst.sop_instance_uid, inst.file_path,
         int(inst.file_size_bytes or 0), _effective_backend(inst, study_backend))
        for inst in instances
    ]
    by_backend: dict[str | None, list[tuple[str, int]]] = {}
    for sop_uid, file_path, size, backend_name in rows:
        if file_path:
            by_backend.setdefault(backend_name, []).append((file_path, size))
        # Thumbnails live in `.thumbnails/`, outside the study prefix. Include
        # them explicitly so an admin delete cannot leave derived PHI behind.
        if sop_uid:
            by_backend.setdefault(backend_name, []).append(
                (storage_service.thumbnail_key(sop_uid), 0)
            )
    return _DeletePlan(
        study_id=study.id,
        study_backend=study_backend,
        study_tenant=study.tenant_id,
        rows=rows,
        by_backend=by_backend,
    )


def _release_objects(study_uid: str, plan: _DeletePlan) -> int:
    """Unlink every object, crediting each backend its own bytes (C-17 + A-20).

    Deleting before the row deletes is the idempotent order: if the DB work
    fails, a retry finds the objects already gone, ``delete_file`` credits
    nothing (it measures before unlinking), and the rows are removed — the retry
    converges instead of double-crediting the same bytes.
    """
    released = 0
    for backend_name, objects in plan.by_backend.items():
        released += storage_service.delete_study_files(
            study_uid, backend_name=backend_name,
            objects=objects, tenant_id=plan.study_tenant,
        )
        storage_service.invalidate_usage_cache(backend_name=backend_name)
    return released


def _defer_study_deleted(db, study_uid: str, performed_by: str) -> None:
    """Announce the delete only if this transaction commits (C-22).

    Firing the hook inline announced "StudyDeleted" for a delete that a failed
    commit then rolled back — subscribers archived/purged a study that still
    existed. ``defer_after_commit`` runs it on the commit ``get_db`` performs and
    drops it on rollback.
    """
    from app.services.scripting_service import fire as script_fire

    storage_service.defer_after_commit(
        db,
        lambda: script_fire(
            "StudyDeleted", study_uid=study_uid, performed_by=performed_by
        ),
    )
