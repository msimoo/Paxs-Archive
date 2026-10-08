"""Human-facing REST API for the web dashboard: stats, study browsing,
thumbnails, deletion. DICOMweb-standard endpoints live in dicomweb.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import case, delete as sa_delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import resolve_public_base_url, settings
from app.services.compression_service import codec_status
from app.db.models import (
    AuditLog,
    ClinicalProtocol,
    CompressionJob,
    CriticalResult,
    DicomNode,
    Instance,
    KeyImageNote,
    LossyCompressionRule,
    MigrationJob,
    Patient,
    PatientShare,
    PerformedProcedureStep,
    RadiologyReport,
    ReplicationRecord,
    RoutingQueueItem,
    RoutingRule,
    ScheduledProcedureStep,
    Series,
    StorageCommitRequest,
    Study,
    StudyLifecycleRule,
    Tenant,
    TriageFlag,
    User,
)
from app.db.session import AsyncSessionLocal, get_db, get_read_db
from app.services import (
    query_service,
    storage_service,
    tenant_service,
    thumbnail_service,
    volume_service,
)
from app.services.audit_service import add_audit
from app.api.v1.deps import (
    get_current_user,
    get_current_user_any,
    require_admin,
    require_destructive_role,
    require_instance_access,
    require_series_access,
    require_study_access,
    tenant_scope,
)

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
    ("MPPS", "1.2.840.10008.3.1.2.3.3", "SCP"),
    ("Storage Commitment", "1.2.840.10008.1.20.1", "SCP + SCU"),
]

WEB_SERVICES = [
    ("DICOMweb (QIDO/WADO/STOW)", "REST /dicom-web", True),
    ("HL7 MLLP (HIS ADT + ORM)", "MLLP", settings.HL7_ENABLED),
    ("HL7 ORU (report → EMR)", "MLLP out", settings.HL7_EMR_ENABLED),
    ("FHIR R4", "REST /fhir", settings.FHIR_ENABLED),
]


def _dicom_services_matrix() -> list[dict]:
    """Live DIMSE service list — enabled when the DIMSE receiver is up."""
    dimse_on = settings.DICOM_RECEIVER_ENABLED
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
    from sqlalchemy import func, select

    from app.db.models import StorageBackendConfig
    from app.db.session import SyncSessionLocal

    with SyncSessionLocal() as session:
        return int(session.execute(select(func.count(StorageBackendConfig.id))).scalar_one())



@router.get("/health/live")
async def health_live():
    """Liveness probe (HAProxy httpchk) — the process is up."""
    return {"status": "ok", "instance": settings.PAXS_INSTANCE_ID}


@router.get("/health/ready")
async def health_ready():
    """Readiness probe — the node can serve traffic (DB reachable)."""
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("SELECT 1"))
    except Exception:
        log.exception("readiness check failed")
        return JSONResponse(
            {"status": "unavailable", "instance": settings.PAXS_INSTANCE_ID},
            status_code=503,
        )
    return {"status": "ready", "instance": settings.PAXS_INSTANCE_ID}


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
        async with AsyncSessionLocal() as s:
            await s.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        log.exception("health check failed")
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
        "dicom": {
            "ae_title": settings.DICOM_AE_TITLE,
            "host": settings.DICOM_HOST,
            "port": settings.DICOM_PORT,
            "enabled": settings.DICOM_RECEIVER_ENABLED,
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
        "compression": {
            "method": settings.DEFAULT_COMPRESSION,
            "enabled": settings.COMPRESSION_ENABLED,
            # Per-method codec availability — which lossless encoders the
            # compression worker can actually use (RLE built-in, JPEG-LS /
            # JPEG2000 / HTJ2K depend on installed codec packages).
            "codecs": codec_status(),
        },
        "volume_streaming": {
            "enabled": settings.VOLUME_STREAMING_ENABLED,
            "max_slices": settings.VOLUME_MAX_SLICES,
            "mpr_slice": settings.VOLUME_STREAMING_ENABLED,
            "mpr_max_pixels": settings.VOLUME_MPR_MAX_PIXELS,
            "mip": {
                "enabled": settings.VOLUME_STREAMING_ENABLED,
                "endpoint": "/api/v1/volume/{series_uid}/mip",
                "modes": ["mip", "minip", "average", "vrt"],
            },
        },
        "fusion": {
            "enabled": True,
            "endpoint": "/api/v1/fusion/{study_uid}",
        },
        "rate_limit_per_minute": settings.RATE_LIMIT_GLOBAL_PER_MINUTE,
        "services": {
            "dicom": _dicom_services_matrix(),
            "dicom_enabled": settings.DICOM_RECEIVER_ENABLED,
            "tls_enabled": settings.DICOM_TLS_ENABLED,
            "web": [
                {"name": name, "detail": detail, "enabled": enabled}
                for (name, detail, enabled) in WEB_SERVICES
            ],
        },
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
    async def _count(model, *where):
        stmt = select(func.count(model.id))
        if where:
            stmt = stmt.where(*where)
        return (await db.execute(stmt)).scalar_one()

    patients = await _count(Patient)
    studies = await _count(Study)
    series = await _count(Series)
    instances = await _count(Instance)
    users = await _count(User)
    nodes = await _count(DicomNode)
    shares = await _count(
        PatientShare,
        PatientShare.expires_at >= datetime.now(timezone.utc).replace(tzinfo=None),
    )
    pending_jobs = await _count(CompressionJob, CompressionJob.status == "PENDING")

    # Live counters for every completed feature module (workflow A + enterprise B + parallel tracks).
    worklist = {
        "scheduled": await _count(ScheduledProcedureStep, ScheduledProcedureStep.status == "SCHEDULED"),
        "in_progress": await _count(ScheduledProcedureStep, ScheduledProcedureStep.status == "IN PROGRESS"),
        "completed": await _count(ScheduledProcedureStep, ScheduledProcedureStep.status == "COMPLETED"),
        "canceled": await _count(ScheduledProcedureStep, ScheduledProcedureStep.status == "CANCELED"),
    }
    mpps = {
        "in_progress": await _count(PerformedProcedureStep, PerformedProcedureStep.status == "IN PROGRESS"),
        "completed": await _count(PerformedProcedureStep, PerformedProcedureStep.status == "COMPLETED"),
        "discontinued": await _count(PerformedProcedureStep, PerformedProcedureStep.status == "DISCONTINUED"),
    }
    commitments = {
        "pending": await _count(StorageCommitRequest, StorageCommitRequest.status == "PENDING"),
        "completed": await _count(StorageCommitRequest, StorageCommitRequest.status == "COMPLETED"),
        "failed": await _count(StorageCommitRequest, StorageCommitRequest.status == "FAILED"),
    }
    critical = {
        "pending": await _count(CriticalResult, CriticalResult.status == "PENDING_ACK"),
        "acknowledged": await _count(CriticalResult, CriticalResult.status == "ACKNOWLEDGED"),
    }
    routing = {
        "total": await _count(RoutingRule),
        "enabled": await _count(RoutingRule, RoutingRule.enabled.is_(True)),
        "queue_pending": await _count(RoutingQueueItem, RoutingQueueItem.status == "PENDING"),
        "queue_failed": await _count(RoutingQueueItem, RoutingQueueItem.status == "FAILED"),
        "queue_completed": await _count(RoutingQueueItem, RoutingQueueItem.status == "COMPLETED"),
    }
    lossy = {"rules": await _count(LossyCompressionRule)}
    lifecycle = {
        "rules": await _count(StudyLifecycleRule),
        "archived": await _count(Study, Study.lifecycle_state == "ARCHIVED"),
        "online": await _count(Study, Study.lifecycle_state == "ONLINE"),
    }
    migration = {
        "total": await _count(MigrationJob),
        "running": await _count(MigrationJob, MigrationJob.status == "RUNNING"),
    }
    kin = {"notes": await _count(KeyImageNote)}
    triage = {"unreviewed": await _count(TriageFlag, TriageFlag.reviewed.is_(False))}
    protocols = {"count": await _count(ClinicalProtocol)}
    audit = {"events": await _count(AuditLog)}
    # Radiologist reading worklist (roadmap #30) — Unread / Stat / Dictated.
    try:
        from app.services import reading_worklist_service as _rwl

        _r = await _rwl.reader_counts(db, None, include_all=True)
        reading = {
            "unread": _r["unread"],
            "dictated": _r["dictated"],
            "read": _r["read"],
            "stat": _r["stat"],
        }
    except Exception:  # pragma: no cover — never let stats fail the dashboard
        log.exception("reading worklist counts failed")
        reading = {"unread": 0, "dictated": 0, "read": 0, "stat": 0}
    reports = {
        "total": await _count(RadiologyReport),
        "draft": await _count(RadiologyReport, RadiologyReport.status == "DRAFT"),
        "finalized": await _count(RadiologyReport, RadiologyReport.status == "FINALIZED"),
        "oru_pending": await _count(RadiologyReport, RadiologyReport.oru_status == "PENDING"),
        "oru_sent": await _count(RadiologyReport, RadiologyReport.oru_status == "SENT"),
        "oru_failed": await _count(RadiologyReport, RadiologyReport.oru_status == "FAILED"),
    }
    tenants = {
        "total": await _count(Tenant),
        "active": await _count(Tenant, Tenant.is_active.is_(True)),
    }

    # Viewer processing distribution (roadmap #51, Phase 5 §5.2). These counters
    # are in-process module state, not DB rows: `active_server_sessions` is the
    # live thin-client count the hybrid engine load-balances on, and
    # `server_vol_cache_mb` is the currently materialised volume cache. Reading
    # them here makes the engine's own inputs observable — without this the
    # dashboard could only show the *configuration*, never the actual load.
    try:
        viewer_processing = {
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
    except Exception:  # pragma: no cover — never let telemetry fail the dashboard
        log.exception("viewer processing telemetry failed")
        viewer_processing = {
            "active_server_sessions": 0,
            "server_vol_cache_mb": 0,
            "configured_mode": settings.VIEWER_PROCESSING_MODE,
            "max_server_sessions": settings.HYBRID_MAX_SERVER_MPR_SESSIONS,
            "vol_budget_mb": settings.HYBRID_SERVER_VOL_BUDGET_MB,
        }

    # storage usage walks the storage tree (O(objects)) — serve from the TTL
    # cache, and never block the event loop on a cold cache (to_thread).
    storage_bytes = await asyncio.to_thread(
        storage_service.cached_storage_usage_bytes
    )

    return {
        "patients": patients,
        "studies": studies,
        "series": series,
        "instances": instances,
        "storage_bytes": storage_bytes,
        "users": users,
        "nodes": nodes,
        "active_shares": shares,
        "pending_jobs": pending_jobs,
        "compression": {
            "method": settings.DEFAULT_COMPRESSION,
            "enabled": settings.COMPRESSION_ENABLED,
            # Per-method codec availability — which lossless encoders the
            # compression worker can actually use (RLE built-in, JPEG-LS /
            # JPEG2000 / HTJ2K depend on installed codec packages).
            "codecs": codec_status(),
        },
        "features": {
            "dicom_services": {
                "total": len(DIMSE_SERVICES),
                "enabled": len(DIMSE_SERVICES) if settings.DICOM_RECEIVER_ENABLED else 0,
                "tls": settings.DICOM_TLS_ENABLED,
            },
            "worklist": worklist,
            "mpps": mpps,
            "commitments": commitments,
            "critical_results": critical,
            "routing": routing,
            "lossy_compression": lossy,
            "lifecycle": lifecycle,
            "migration": migration,
            "kin": kin,
            "triage": triage,
            "protocols": protocols,
            "audit": audit,
            "tenants": tenants,
            "reports": reports,
            "reading": reading,
            "scripts": {
                "count": _script_count(),
                "enabled": settings.SCRIPTS_ENABLED,
            },
            "replication": {
                "enabled": settings.DR_REPLICATION_ENABLED,
                "pending": await _count(ReplicationRecord, ReplicationRecord.status == "PENDING"),
                "replicated": await _count(ReplicationRecord, ReplicationRecord.status == "REPLICATED"),
                "failed": await _count(ReplicationRecord, ReplicationRecord.status == "FAILED"),
            },
            "volume_streaming": {
                "enabled": settings.VOLUME_STREAMING_ENABLED,
                "max_slices": settings.VOLUME_MAX_SLICES,
                "mpr_slice": settings.VOLUME_STREAMING_ENABLED,
                "mpr_max_pixels": settings.VOLUME_MPR_MAX_PIXELS,
                "mip": {
                    "enabled": settings.VOLUME_STREAMING_ENABLED,
                    "endpoint": "/api/v1/volume/{series_uid}/mip",
                    "modes": ["mip", "minip", "average", "vrt"],
                },
            },
            "fusion": {
                "enabled": True,
                "endpoint": "/api/v1/fusion/{study_uid}",
            },
            "viewer_processing": viewer_processing,
        },
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
    rows = (
        await db.execute(
            select(AuditLog)
            .order_by(AuditLog.id.desc())
            .limit(min(max(limit, 1), 200))
        )
    ).scalars().all()
    return [
        {
            "id": e.id,
            "action": e.action,
            "sop_instance_uid": e.sop_instance_uid,
            "study_instance_uid": e.study_instance_uid,
            "sha256": e.sha256_hash,
            "performed_by": e.performed_by,
            "performed_at": e.performed_at.isoformat() if e.performed_at else None,
        }
        for e in rows
    ]


@router.get("/studies")
async def list_studies(request: Request, db: AsyncSession = Depends(get_read_db),
                      user: User = Depends(get_current_user_any)):
    params = dict(request.query_params)
    page = max(int(params.get("page", 1)), 1)
    limit = min(max(int(params.get("limit", 20)), 1), 100)
    search = params.get("search", "").strip()
    modality = params.get("modality", "").strip()
    date_from = params.get("from", "").strip()
    date_to = params.get("to", "").strip()
    sort = params.get("sort", "date").strip().lower() or "date"
    order = params.get("order", "desc").strip().lower()
    state_filter = params.get("state", "").strip().upper()
    show_critical = params.get("critical", "").strip().lower() in ("1", "true")
    show_reported = params.get("reported", "").strip().lower() in ("1", "true")
    show_oru = params.get("oru", "").strip().lower() in ("1", "true")
    show_his = params.get("his", "").strip().lower() in ("1", "true")

    base = select(Study).options(joinedload(Study.patient)).join(Patient, Study.patient_id == Patient.id)
    count_stmt = select(func.count(Study.id)).join(Patient, Study.patient_id == Patient.id)

    # VNA scope: admins see every tenant (plus an optional ?tenant= filter);
    # other signed-in users see their own tenant's studies; guests see the
    # legacy unscoped data only (pre-VNA archives keep working unchanged).
    tenant_id, include_all = tenant_service.scope_from_user(user)
    if not include_all:
        cond = (Study.tenant_id.is_(None) if tenant_id is None
                else Study.tenant_id == tenant_id)
        base = base.where(cond)
        count_stmt = count_stmt.where(cond)
    elif user is not None and params.get("tenant"):
        t = params.get("tenant", "").strip()
        if t.isdigit():
            base = base.where(Study.tenant_id == int(t))
            count_stmt = count_stmt.where(Study.tenant_id == int(t))
        elif t.lower() in ("none", "null"):
            base = base.where(Study.tenant_id.is_(None))
            count_stmt = count_stmt.where(Study.tenant_id.is_(None))

    if search:
        like = f"%{search}%"
        cond = or_(
            Patient.patient_name.like(like),
            Patient.patient_id.like(like),
            Study.study_description.like(like),
            Study.accession_number.like(like),
        )
        base = base.where(cond)
        count_stmt = count_stmt.where(cond)
    if modality:
        sub = select(Series.study_id).where(Series.modality == modality)
        base = base.where(Study.id.in_(sub))
        count_stmt = count_stmt.where(Study.id.in_(sub))
    if date_from and date_to:
        base = base.where(Study.study_date.between(date_from, date_to))
        count_stmt = count_stmt.where(Study.study_date.between(date_from, date_to))
    elif date_from:
        base = base.where(Study.study_date >= date_from)
        count_stmt = count_stmt.where(Study.study_date >= date_from)
    elif date_to:
        base = base.where(Study.study_date <= date_to)
        count_stmt = count_stmt.where(Study.study_date <= date_to)

    # --- per-status counters (respect all filters except the state/critical
    # toggles themselves, so the chips work as independent filters) ---
    # Subqueries shared by the counters and their toggle filters (single
    # source of truth — they can never drift apart).
    finalized_ids = select(RadiologyReport.study_id).where(
        RadiologyReport.status == "FINALIZED")
    oru_sent_ids = select(RadiologyReport.study_id).where(
        RadiologyReport.oru_status == "SENT")
    counts = {
        "total": (await db.execute(count_stmt)).scalar_one(),
        "online": (await db.execute(
            count_stmt.where(Study.lifecycle_state == "ONLINE")
        )).scalar_one(),
        "archived": (await db.execute(
            count_stmt.where(Study.lifecycle_state == "ARCHIVED")
        )).scalar_one(),
        "critical": (await db.execute(
            count_stmt.where(Study.study_instance_uid.in_(
                select(TriageFlag.study_instance_uid)
            ))
        )).scalar_one(),
        # Studies with a finalized radiology report (E2 — HL7 ORU).
        "reported": (await db.execute(
            count_stmt.where(Study.id.in_(finalized_ids))
        )).scalar_one(),
        # Of those, the ones whose ORU^R01 was delivered to the EMR.
        "oru_sent": (await db.execute(
            count_stmt.where(Study.id.in_(oru_sent_ids))
        )).scalar_one(),
        # Studies whose patient was registered by the hospital HIS (HL7 ADT).
        "his": (await db.execute(
            count_stmt.where(Patient.source == "his")
        )).scalar_one(),
    }

    if state_filter:
        base = base.where(Study.lifecycle_state == state_filter)
        count_stmt = count_stmt.where(Study.lifecycle_state == state_filter)
    if show_critical:
        tri = Study.study_instance_uid.in_(select(TriageFlag.study_instance_uid))
        base = base.where(tri)
        count_stmt = count_stmt.where(tri)
    if show_reported:
        rep_ids = Study.id.in_(finalized_ids)
        base = base.where(rep_ids)
        count_stmt = count_stmt.where(rep_ids)
    if show_oru:
        oru_ids = Study.id.in_(oru_sent_ids)
        base = base.where(oru_ids)
        count_stmt = count_stmt.where(oru_ids)
    if show_his:
        his_cond = Patient.source == "his"
        base = base.where(his_cond)
        count_stmt = count_stmt.where(his_cond)

    total = (await db.execute(count_stmt)).scalar_one()

    # --- sorting ---
    if sort == "patient":
        col = Patient.patient_name
        base = base.order_by(col.is_(None), col.asc() if order == "asc" else col.desc(), Study.id.desc())
    elif sort == "accession":
        col = Study.accession_number
        base = base.order_by(col.is_(None), col.asc() if order == "asc" else col.desc(), Study.id.desc())
    elif sort == "series":
        sub = (select(Series.study_id, func.count(Series.id).label("n"))
               .group_by(Series.study_id)).subquery()
        base = base.outerjoin(sub, sub.c.study_id == Study.id)
        n = sub.c.n
        base = base.order_by(n.is_(None), n.asc() if order == "asc" else n.desc(), Study.id.desc())
    elif sort == "instances":
        sub = (select(Series.study_id, func.count(Instance.id).label("n"))
               .join(Instance, Instance.series_id == Series.id)
               .group_by(Series.study_id)).subquery()
        base = base.outerjoin(sub, sub.c.study_id == Study.id)
        n = sub.c.n
        base = base.order_by(n.is_(None), n.asc() if order == "asc" else n.desc(), Study.id.desc())
    elif sort == "modality":
        # Sort by the study's first (alphabetically smallest) modality code.
        sub = (select(Series.study_id, func.min(Series.modality).label("m"))
               .group_by(Series.study_id)).subquery()
        base = base.outerjoin(sub, sub.c.study_id == Study.id)
        m = sub.c.m
        base = base.order_by(m.is_(None), m.asc() if order == "asc" else m.desc(), Study.id.desc())
    elif sort == "created":
        base = base.order_by(Study.created_at.is_(None),
                             Study.created_at.asc() if order == "asc" else Study.created_at.desc(),
                             Study.id.desc())
    else:  # date (default)
        base = base.order_by(Study.study_date.is_not(None).desc(),
                             Study.study_date.asc() if order == "asc" else Study.study_date.desc(),
                             Study.id.desc())

    studies = (
        await db.execute(
            base.limit(limit).offset((page - 1) * limit)
        )
    ).scalars().unique().all()

    study_ids = [s.id for s in studies]
    agg = await query_service.study_aggregates(db, study_ids)
    tenant_names = await tenant_service.get_tenant_names(db)
    # First series (lowest id) of every study in a single query (no N+1).
    first_series_rows = (
        await db.execute(
            select(Series.study_id, Series.series_instance_uid)
            .where(Series.study_id.in_(study_ids))
            .order_by(Series.id)
        )
    ).all()
    first_by_study: dict[int, str] = {}
    for sid, series_uid in first_series_rows:
        first_by_study.setdefault(sid, series_uid)

    # Report state per study (row badge: draft / finalized / EMR delivery).
    report_rows = (
        await db.execute(
            select(RadiologyReport.study_id, RadiologyReport.id,
                   RadiologyReport.status, RadiologyReport.oru_status)
            .where(RadiologyReport.study_id.in_(study_ids))
        )
    ).all()
    report_by_study = {r.study_id: r for r in report_rows}

    items = []
    for s in studies:
        a = agg.get(s.id, {})
        first_series_uid = first_by_study.get(s.id)
        rep = report_by_study.get(s.id)
        thumb = None
        if first_series_uid is not None:
            thumb = f"/api/v1/studies/{s.study_instance_uid}/series/{first_series_uid}/thumbnail.png"
        items.append({
            "study_uid": s.study_instance_uid,
            "patient_name": s.patient.patient_name,
            "patient_id": s.patient.patient_id,
            "patient_source": s.patient.source,
            # C07: real demographics for the study drawer (never fabricated).
            "patient_birth_date": s.patient.patient_birth_date if s.patient else None,
            "patient_sex": s.patient.patient_sex if s.patient else None,
            "tenant_id": s.tenant_id,
            "tenant_name": tenant_names.get(s.tenant_id),
            "study_date": s.study_date,
            "study_time": s.study_time,
            "description": s.study_description,
            "accession": s.accession_number,
            "modalities": a.get("modalities", []),
            "n_series": a.get("series", 0),
            "n_instances": a.get("instances", 0),
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "lifecycle_state": s.lifecycle_state,
            "thumbnail": thumb,
            # Radiology report state (badge on the row).
            "report_id": rep.id if rep else None,
            "report_status": rep.status if rep else None,
            "oru_status": rep.oru_status if rep else None,
        })
    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "sort": sort,
        "order": order,
        "counts": counts,
    }


@router.get("/triage")
async def list_triage(db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user_any)):
    """Unreviewed triage findings across the archive (emergency queue)."""
    # Severity must sort HIGH -> MEDIUM -> LOW (alphabetical would put
    # MEDIUM before HIGH).
    severity_rank = case(
        (TriageFlag.severity == "HIGH", 0),
        (TriageFlag.severity == "MEDIUM", 1),
        (TriageFlag.severity == "LOW", 2),
        else_=3,
    )
    flags = (
        await db.execute(
            select(TriageFlag, Study.study_instance_uid, Study.study_date,
                   Patient.patient_name, Patient.patient_id)
            .join(Study, Study.study_instance_uid == TriageFlag.study_instance_uid)
            .join(Patient, Patient.id == Study.patient_id)
            .where(TriageFlag.reviewed.is_(False))
            .order_by(severity_rank, TriageFlag.created_at.desc())
            .limit(50)
        )
    ).all()
    return [
        {
            "id": f.id,
            "finding": f.finding,
            "severity": f.severity,
            "confidence": f.confidence,
            "summary": f.summary,
            "detector": f.detector,
            "created_at": f.created_at.isoformat() if f.created_at else None,
            "study_uid": study_uid,
            "study_date": study_date,
            "patient_name": patient_name,
            "patient_id": patient_id,
        }
        for (f, study_uid, study_date, patient_name, patient_id) in flags
    ]


@router.post("/triage/{flag_id}/review", status_code=200)
async def review_triage(flag_id: int, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user_any)):
    """Mark a triage finding as reviewed (seen by a clinician)."""
    flag = (
        await db.execute(select(TriageFlag).where(TriageFlag.id == flag_id))
    ).scalar_one_or_none()
    if flag is None:
        raise HTTPException(404, "Triage flag not found")
    flag.reviewed = True
    return {"ok": True, "id": flag.id}


@router.get("/studies/{study_uid}")
async def study_detail(study_uid: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user_any)):
    # C04: direct UID access must respect the tenant boundary.
    study = await require_study_access(db, study_uid, user)
    study = (
        await db.execute(
            select(Study).options(joinedload(Study.patient))
            .where(Study.id == study.id)
        )
    ).scalar_one()

    series_list = (
        await db.execute(
            select(Series).where(Series.study_id == study.id)
            .order_by(Series.series_number.is_not(None).desc(), Series.series_number, Series.id)
        )
    ).scalars().all()
    counts = await query_service.series_instance_counts(db, [s.id for s in series_list])

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
            {
                "series_uid": s.series_instance_uid,
                "modality": s.modality,
                "series_number": s.series_number,
                "description": s.series_description,
                "body_part": s.body_part_examined,
                "instances": counts.get(s.id, 0),
                "thumbnail": (
                    f"/api/v1/studies/{study_uid}/series/{s.series_instance_uid}/thumbnail.png"
                    if counts.get(s.id, 0) else None
                ),
            }
            for s in series_list
        ],
    }


@router.get("/studies/{study_uid}/series/{series_uid}/instances")
async def series_instances(study_uid: str, series_uid: str, db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user_any)):
    # C04: series-addressed access resolves through the study's tenant.
    await require_series_access(db, series_uid, user)
    tenant_id, include_all = tenant_scope(user)
    instances = (
        await db.execute(
            query_service.instance_query(
                study_uid, series_uid, tenant_id=tenant_id, include_all=include_all
            )
            .order_by(Instance.instance_number.is_not(None).desc(), Instance.instance_number, Instance.id)
        )
    ).scalars().all()
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
    inst = (
        await db.execute(
            query_service.instance_query(
                study_uid, series_uid, tenant_id=tenant_id, include_all=include_all
            )
            .order_by(Instance.instance_number.is_not(None).desc(), Instance.instance_number, Instance.id)
            .limit(1)
        )
    ).scalars().first()
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
            raise HTTPException(422, "Could not generate thumbnail")
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


@router.delete("/studies/{study_uid}", status_code=204)
async def delete_study(study_uid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(require_destructive_role)):
    # C04: a destructive role authorises the *action*, not another tenant's data.
    study = await require_study_access(db, study_uid, user)

    instances = (
        await db.execute(select(Instance).where(Instance.series_id.in_(
            select(Series.id).where(Series.study_id == study.id)
        )))
    ).scalars().all()

    for inst in instances:
        add_audit(db, inst.sop_instance_uid, "DELETED", performed_by=user.username,
                  study_instance_uid=study_uid, file_path=inst.file_path)

    # Remove triage flags for this study (they are keyed by UID, not FK).
    await db.execute(sa_delete(TriageFlag).where(
        TriageFlag.study_instance_uid == study_uid))

    storage_service.delete_study_files(study_uid, backend_name=study.storage_backend)
    storage_service.invalidate_usage_cache(backend_name=study.storage_backend)
    await db.delete(study)

    # Custom server scripts hook
    from app.services.scripting_service import fire as script_fire

    script_fire("StudyDeleted", study_uid=study_uid, performed_by=user.username)
