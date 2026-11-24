"""Dashboard queries: counters, study listing, audit, triage (Q-3 / Q-6).

Everything ``api/v1/dashboard.py`` used to build inline lives here. The router
keeps the HTTP shape; this module owns the statements, the tenant scope and the
aggregation, so ``stats`` (190 lines) and ``list_studies`` (228) are now a parse,
a delegation and a response.

Scope is never decided here: every study-bearing statement goes through
:func:`query_service.apply_study_tenant_scope` (FA-1), and the caller passes the
``(tenant_id, include_all)`` pair from ``tenant_service.scope_from_user``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, or_, select, text
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

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
from app.services import audit_service, query_service, tenant_service
from app.core.roles import DICTATION_LISTEN_ROLES
from app.services.dashboard_rows import page_rows as _page_rows

_TRUE_VALUES = frozenset({"1", "true"})
_MAX_LIMIT = 100


def _utcnow() -> datetime:
    """Naive UTC 'now' — the columns these counters compare against are naive."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Health probes
# --------------------------------------------------------------------------- #
async def ping_db() -> None:
    """One round-trip to the database; raises when it is unreachable.

    Lives here so the probes hold no statement of their own — ``SELECT 1`` is
    still SQL, and a router that runs it is a router that may grow another one.
    """
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT 1"))


# --------------------------------------------------------------------------- #
# /stats
# --------------------------------------------------------------------------- #
async def _count(db: AsyncSession, model: type[Any], *where: Any) -> int:
    stmt = select(func.count(model.id))
    if where:
        stmt = stmt.where(*where)
    return (await db.execute(stmt)).scalar_one()


async def _scoped_uid_count(
    db: AsyncSession, model: type[Any], uid_column: Any,
    user: User | None, *where: Any,
) -> int:
    """Count a study-UID child only inside the caller's Study/Patient scope."""
    tenant_id, include_all = tenant_service.scope_from_user(user)
    statement = query_service.apply_study_uid_tenant_scope(
        select(func.count(model.id)).where(*where),
        model, uid_column, tenant_id, include_all,
    )
    return int((await db.execute(statement)).scalar_one())


async def _scoped_study_count(
    db: AsyncSession, user: User | None, *where: Any,
) -> int:
    """Count studies after enforcing both Study and linked Patient ownership."""
    tenant_id, include_all = tenant_service.scope_from_user(user)
    statement = query_service.apply_study_tenant_scope(
        select(func.count(Study.id)).where(*where), tenant_id, include_all
    )
    return int((await db.execute(statement)).scalar_one())


async def _scoped_patient_count(
    db: AsyncSession, user: User | None, *where: Any,
) -> int:
    """Count patient rows in the caller's tenant or explicit legacy scope."""
    tenant_id, include_all = tenant_service.scope_from_user(user)
    statement = query_service.apply_tenant_scope(
        select(func.count(Patient.id)).where(*where),
        Patient.tenant_id, tenant_id, include_all,
    )
    return int((await db.execute(statement)).scalar_one())


async def _scoped_report_count(
    db: AsyncSession, user: User | None, *where: Any,
) -> int:
    """Count reports only when the owning study and patient match the caller."""
    tenant_id, include_all = tenant_service.scope_from_user(user)
    if include_all:
        return await _count(db, RadiologyReport, *where)
    statement = (
        select(func.count(RadiologyReport.id))
        .join(Study, Study.id == RadiologyReport.study_id)
        .where(*where)
    )
    statement = query_service.apply_study_tenant_scope(
        statement, tenant_id, include_all
    )
    return int((await db.execute(statement)).scalar_one())


async def _scoped_tenant_count(
    db: AsyncSession, model: type[Any], user: User | None, *where: Any,
) -> int:
    """Count a direct tenant-owned entity through the canonical predicate."""
    tenant_id, include_all = tenant_service.scope_from_user(user)
    statement = query_service.apply_tenant_scope(
        select(func.count(model.id)).where(*where),
        model.tenant_id, tenant_id, include_all,
    )
    return int((await db.execute(statement)).scalar_one())


async def _scoped_patient_child_count(
    db: AsyncSession, model: type[Any], patient_id_column: Any,
    user: User | None, *where: Any,
) -> int:
    """Count patient-linked workflow rows under their patient's tenant."""
    tenant_id, include_all = tenant_service.scope_from_user(user)
    if include_all:
        return await _count(db, model, *where)
    statement = (
        select(func.count(model.id))
        .join(Patient, Patient.id == patient_id_column)
        .where(*where)
    )
    scope = query_service.tenant_predicate(Patient.tenant_id, tenant_id, include_all)
    if scope is not None:
        statement = statement.where(scope)
    return int((await db.execute(statement)).scalar_one())


async def _admin_count(
    db: AsyncSession, model: type[Any], user: User | None, *where: Any,
) -> int:
    """Keep archive-wide, non-tenant workflow counters out of tenant responses."""
    if getattr(user, "role", None) != "ADMIN":
        return 0
    return await _count(db, model, *where)


async def entity_counts(db: AsyncSession, user: User | None) -> dict[str, int]:
    """Return clinical entity counts inside the caller's tenant scope."""
    tenant_id, include_all = tenant_service.scope_from_user(user)
    series_stmt = select(func.count(Series.id)).join(Study, Series.study_id == Study.id)
    instance_stmt = (
        select(func.count(Instance.id))
        .join(Series, Instance.series_id == Series.id)
        .join(Study, Series.study_id == Study.id)
    )
    job_stmt = (
        select(func.count(CompressionJob.id))
        .join(Instance, CompressionJob.instance_id == Instance.id)
        .join(Series, Instance.series_id == Series.id)
        .join(Study, Series.study_id == Study.id)
        .where(CompressionJob.status == "PENDING")
    )
    series_stmt = query_service.apply_study_tenant_scope(
        series_stmt, tenant_id, include_all
    )
    instance_stmt = query_service.apply_study_tenant_scope(
        instance_stmt, tenant_id, include_all
    )
    job_stmt = query_service.apply_study_tenant_scope(job_stmt, tenant_id, include_all)
    return {
        "patients": await _scoped_patient_count(db, user),
        "studies": await _scoped_study_count(db, user),
        "series": int((await db.execute(series_stmt)).scalar_one()),
        "instances": int((await db.execute(instance_stmt)).scalar_one()),
        "users": await _scoped_tenant_count(db, User, user),
        "nodes": await _admin_count(db, DicomNode, user),
        "active_shares": await _scoped_uid_count(
            db, PatientShare, PatientShare.study_instance_uid, user,
            PatientShare.expires_at >= _utcnow(),
        ),
        "pending_jobs": int((await db.execute(job_stmt)).scalar_one()),
    }


async def _worklist_counts(db: AsyncSession, user: User | None) -> dict[str, int]:
    """Count scheduled steps through their owning Patient tenant."""
    statuses = ("SCHEDULED", "IN PROGRESS", "COMPLETED", "CANCELED")
    return {
        status.lower().replace(" ", "_"): await _scoped_patient_child_count(
            db, ScheduledProcedureStep, ScheduledProcedureStep.patient_id, user,
            ScheduledProcedureStep.status == status,
        ) for status in statuses
    }


async def _mpps_counts(db: AsyncSession, user: User | None) -> dict[str, int]:
    """Count MPPS rows through their owning Patient tenant."""
    statuses = ("IN PROGRESS", "COMPLETED", "DISCONTINUED")
    return {
        status.lower().replace(" ", "_"): await _scoped_patient_child_count(
            db, PerformedProcedureStep, PerformedProcedureStep.patient_id, user,
            PerformedProcedureStep.status == status,
        ) for status in statuses
    }


async def _commitment_counts(db: AsyncSession, user: User | None) -> dict[str, int]:
    """Count commitment rows using their tenant ownership column."""
    statuses = ("PENDING", "COMPLETED", "FAILED")
    return {
        status.lower(): await _scoped_tenant_count(
            db, StorageCommitRequest, user, StorageCommitRequest.status == status
        ) for status in statuses
    }


async def _critical_result_counts(db: AsyncSession, user: User | None) -> dict[str, int]:
    """Count critical results by the linked study UID scope."""
    return {
        "pending": await _scoped_uid_count(
            db, CriticalResult, CriticalResult.study_instance_uid, user,
            CriticalResult.status == "PENDING_ACK",
        ),
        "acknowledged": await _scoped_uid_count(
            db, CriticalResult, CriticalResult.study_instance_uid, user,
            CriticalResult.status == "ACKNOWLEDGED",
        ),
    }


async def _routing_counts(db: AsyncSession, user: User | None) -> dict[str, int]:
    """Expose global rules only to ADMIN and queue counts to their tenant."""
    queue = {
        f"queue_{status.lower()}": await _scoped_tenant_count(
            db, RoutingQueueItem, user, RoutingQueueItem.status == status
        ) for status in ("PENDING", "FAILED", "COMPLETED")
    }
    return {
        "total": await _admin_count(db, RoutingRule, user),
        "enabled": await _admin_count(db, RoutingRule, user, RoutingRule.enabled.is_(True)),
        **queue,
    }


async def _report_counts(db: AsyncSession, user: User | None) -> dict[str, int]:
    """Count reports through the owning Study and Patient rows."""
    conditions = {
        "total": (),
        "draft": (RadiologyReport.status == "DRAFT",),
        "finalized": (RadiologyReport.status == "FINALIZED",),
        "oru_pending": (RadiologyReport.oru_status == "PENDING",),
        "oru_sent": (RadiologyReport.oru_status == "SENT",),
        "oru_failed": (RadiologyReport.oru_status == "FAILED",),
    }
    return {
        key: await _scoped_report_count(db, user, *filters)
        for key, filters in conditions.items()
    }


async def _study_state_counts(db: AsyncSession, user: User | None) -> dict[str, int]:
    """Count lifecycle states inside the caller's study scope."""
    return {
        status.lower(): await _scoped_study_count(
            db, user, Study.lifecycle_state == status
        ) for status in ("ARCHIVED", "ONLINE")
    }


async def _replication_counts(db: AsyncSession, user: User | None) -> dict[str, int]:
    """Count replication state only for studies visible to the caller."""
    statuses = ("PENDING", "REPLICATED", "FAILED")
    return {
        status.lower(): await _scoped_uid_count(
            db, ReplicationRecord, ReplicationRecord.study_instance_uid, user,
            ReplicationRecord.status == status,
        ) for status in statuses
    }


async def feature_counts(db: AsyncSession, user: User | None) -> dict[str, Any]:
    """Return tenant-scoped clinical counters and ADMIN-only system counters."""
    return {
        "worklist": await _worklist_counts(db, user),
        "mpps": await _mpps_counts(db, user),
        "commitments": await _commitment_counts(db, user),
        "critical_results": await _critical_result_counts(db, user),
        "routing": await _routing_counts(db, user),
        "lossy_compression": {"rules": await _admin_count(db, LossyCompressionRule, user)},
        "lifecycle": {
            "rules": await _admin_count(db, StudyLifecycleRule, user),
            **await _study_state_counts(db, user),
        },
        "migration": {
            "total": await _admin_count(db, MigrationJob, user),
            "running": await _admin_count(db, MigrationJob, user, MigrationJob.status == "RUNNING"),
        },
        "kin": {"notes": await _scoped_uid_count(
            db, KeyImageNote, KeyImageNote.study_instance_uid, user)},
        "triage": {"unreviewed": await _scoped_uid_count(
            db, TriageFlag, TriageFlag.study_instance_uid, user,
            TriageFlag.reviewed.is_(False))},
        "protocols": {"count": await _admin_count(db, ClinicalProtocol, user)},
        "audit": {"events": await _scoped_uid_count(
            db, AuditLog, AuditLog.study_instance_uid, user)},
        "reports": await _report_counts(db, user),
        "tenants": {
            "total": await _admin_count(db, Tenant, user),
            "active": await _admin_count(db, Tenant, user, Tenant.is_active.is_(True)),
        },
        "replication": await _replication_counts(db, user),
    }


async def reading_counts(db: AsyncSession, user: User | None) -> dict[str, int]:
    """Radiologist reading-worklist counters (roadmap #30).

    Never allowed to fail the dashboard: a broken reading-worklist query must
    not turn ``/stats`` into a 500 when every other counter is fine.
    """
    from app.services import reading_worklist_service

    try:
        r = await reading_worklist_service.reader_counts(db, user)
        return {"unread": r["unread"], "dictated": r["dictated"],
                "read": r["read"], "stat": r["stat"]}
    except Exception:
        import logging

        logging.getLogger("paxs.dashboard").exception(
            "reading worklist counts failed"
        )
        return {"unread": 0, "dictated": 0, "read": 0, "stat": 0}


# --------------------------------------------------------------------------- #
# /studies
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StudyFilters:
    """A parsed ``/studies`` query string.

    ``from_query`` is the single place that knows the parameter names, the
    defaults and the clamping rules, so the router cannot disagree with the
    service about what ``page=0`` or ``limit=9999`` means.
    """

    page: int = 1
    limit: int = 20
    search: str = ""
    modality: str = ""
    date_from: str = ""
    date_to: str = ""
    sort: str = "date"
    order: str = "desc"
    state: str = ""
    critical: bool = False
    reported: bool = False
    oru: bool = False
    his: bool = False
    tenant: str = ""

    @classmethod
    def from_query(cls, params: dict) -> StudyFilters:
        def _int(name: str, default: int) -> int:
            try:
                return int(params.get(name, default))
            except (TypeError, ValueError):
                return default

        def _flag(name: str) -> bool:
            return str(params.get(name, "")).strip().lower() in _TRUE_VALUES

        return cls(
            page=max(_int("page", 1), 1),
            limit=min(max(_int("limit", 20), 1), _MAX_LIMIT),
            search=str(params.get("search", "")).strip(),
            modality=str(params.get("modality", "")).strip(),
            date_from=str(params.get("from", "")).strip(),
            date_to=str(params.get("to", "")).strip(),
            sort=str(params.get("sort", "date")).strip().lower() or "date",
            order=str(params.get("order", "desc")).strip().lower(),
            state=str(params.get("state", "")).strip().upper(),
            critical=_flag("critical"),
            reported=_flag("reported"),
            oru=_flag("oru"),
            his=_flag("his"),
            tenant=str(params.get("tenant", "")).strip(),
        )


def _scoped_statements(user, filters: StudyFilters):
    """The page query and the count query, both tenant-scoped."""
    base = (
        select(Study)
        .options(joinedload(Study.patient))
        .join(Patient, Study.patient_id == Patient.id)
    )
    count_stmt = select(func.count(Study.id)).join(Patient, Study.patient_id == Patient.id)

    # VNA scope: admins see every tenant (plus an optional ?tenant= filter);
    # other signed-in users see their own tenant's studies; guests see the
    # legacy unscoped data only (pre-VNA archives keep working unchanged).
    tenant_id, include_all = tenant_service.scope_from_user(user)
    if not include_all:
        base = query_service.apply_study_tenant_scope(base, tenant_id, include_all)
        count_stmt = query_service.apply_study_tenant_scope(count_stmt, tenant_id, include_all)
    elif user is not None and filters.tenant:
        if filters.tenant.isdigit():
            base = base.where(Study.tenant_id == int(filters.tenant))
            count_stmt = count_stmt.where(Study.tenant_id == int(filters.tenant))
        elif filters.tenant.lower() in ("none", "null"):
            base = query_service.apply_study_tenant_scope(base, None, False)
            count_stmt = query_service.apply_study_tenant_scope(count_stmt, None, False)
    return base, count_stmt


def _search_filters(filters: StudyFilters) -> list:
    """Conditions shared by the page query and every counter."""
    conds: list = []
    if filters.search:
        like = f"%{filters.search}%"
        conds.append(or_(
            Patient.patient_name.ilike(like),
            Patient.patient_id.ilike(like),
            Study.study_description.ilike(like),
            Study.accession_number.ilike(like),
        ))
    if filters.modality:
        conds.append(Study.id.in_(
            select(Series.study_id).where(Series.modality == filters.modality)
        ))
    if filters.date_from and filters.date_to:
        conds.append(Study.study_date.between(filters.date_from, filters.date_to))
    elif filters.date_from:
        conds.append(Study.study_date >= filters.date_from)
    elif filters.date_to:
        conds.append(Study.study_date <= filters.date_to)
    return conds


def _finalized_ids():
    return select(RadiologyReport.study_id).where(
        RadiologyReport.status == "FINALIZED"
    )


def _oru_sent_ids():
    return select(RadiologyReport.study_id).where(
        RadiologyReport.oru_status == "SENT"
    )


def _triage_ids():
    return select(TriageFlag.study_instance_uid)


async def _status_counts(db, count_stmt) -> dict:
    """Per-status chip counters.

    Computed *before* the state/critical/reported/oru/his toggles are applied, so
    each chip counts what it would show if you clicked it, independently of the
    others. The subqueries are shared with the toggles so the chip and its filter
    can never disagree.
    """
    return {
        "total": (await db.execute(count_stmt)).scalar_one(),
        "online": (await db.execute(
            count_stmt.where(Study.lifecycle_state == "ONLINE")
        )).scalar_one(),
        "archived": (await db.execute(
            count_stmt.where(Study.lifecycle_state == "ARCHIVED")
        )).scalar_one(),
        "critical": (await db.execute(
            count_stmt.where(Study.study_instance_uid.in_(_triage_ids()))
        )).scalar_one(),
        # Studies with a finalized radiology report (E2 — HL7 ORU).
        "reported": (await db.execute(
            count_stmt.where(Study.id.in_(_finalized_ids()))
        )).scalar_one(),
        # Of those, the ones whose ORU^R01 was delivered to the EMR.
        "oru_sent": (await db.execute(
            count_stmt.where(Study.id.in_(_oru_sent_ids()))
        )).scalar_one(),
        # Studies whose patient was registered by the hospital HIS (HL7 ADT).
        "his": (await db.execute(
            count_stmt.where(Patient.source == "his")
        )).scalar_one(),
    }


def _toggle_filters(filters: StudyFilters) -> list:
    conds: list = []
    if filters.state:
        conds.append(Study.lifecycle_state == filters.state)
    if filters.critical:
        conds.append(Study.study_instance_uid.in_(_triage_ids()))
    if filters.reported:
        conds.append(Study.id.in_(_finalized_ids()))
    if filters.oru:
        conds.append(Study.id.in_(_oru_sent_ids()))
    if filters.his:
        conds.append(Patient.source == "his")
    return conds


def _apply_sort(base, filters: StudyFilters):
    """Order the page query. ``NULL``s sort last in both directions."""
    desc = filters.order != "asc"
    tail = Study.id.desc()

    if filters.sort == "patient":
        col = Patient.patient_name
        return base.order_by(col.is_(None), col.desc() if desc else col.asc(), tail)
    if filters.sort == "accession":
        col = Study.accession_number
        return base.order_by(col.is_(None), col.desc() if desc else col.asc(), tail)
    if filters.sort in ("series", "instances"):
        counter = Series.id if filters.sort == "series" else Instance.id
        sub = select(Series.study_id, func.count(counter).label("n"))
        if filters.sort == "instances":
            sub = sub.join(Instance, Instance.series_id == Series.id)
        sub = sub.group_by(Series.study_id).subquery()
        base = base.outerjoin(sub, sub.c.study_id == Study.id)
        n = sub.c.n
        return base.order_by(n.is_(None), n.desc() if desc else n.asc(), tail)
    if filters.sort == "modality":
        # The study's first (alphabetically smallest) modality code.
        sub = (
            select(Series.study_id, func.min(Series.modality).label("m"))
            .group_by(Series.study_id)
            .subquery()
        )
        base = base.outerjoin(sub, sub.c.study_id == Study.id)
        m = sub.c.m
        return base.order_by(m.is_(None), m.desc() if desc else m.asc(), tail)
    if filters.sort == "created":
        col = Study.created_at
        return base.order_by(col.is_(None), col.desc() if desc else col.asc(), tail)
    # date (default): newest first, but undated studies still appear.
    col = Study.study_date
    return base.order_by(
        col.is_not(None).desc(), col.desc() if desc else col.asc(), tail
    )


async def list_studies(db, user, filters: StudyFilters) -> dict:
    """One page of the study browser, plus the status-chip counters."""
    base, count_stmt = _scoped_statements(user, filters)
    shared = _search_filters(filters)
    if shared:
        base = base.where(*shared)
        count_stmt = count_stmt.where(*shared)

    counts = await _status_counts(db, count_stmt)

    toggles = _toggle_filters(filters)
    if toggles:
        base = base.where(*toggles)
        count_stmt = count_stmt.where(*toggles)
    total = (await db.execute(count_stmt)).scalar_one()

    base = _apply_sort(base, filters)
    studies = (await db.execute(
        base.limit(filters.limit).offset((filters.page - 1) * filters.limit)
    )).scalars().unique().all()

    return {
        "items": await _page_rows(
            db, list(studies), include_dictations=user.role in DICTATION_LISTEN_ROLES
        ),
        "total": total,
        "page": filters.page,
        "limit": filters.limit,
        "sort": filters.sort,
        "order": filters.order,
        "counts": counts,
    }


# --------------------------------------------------------------------------- #
# /audit
# --------------------------------------------------------------------------- #
async def list_audit(
    db,
    limit: int = 60,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[dict]:
    """Recent in-scope clinical audit events, newest first."""
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 200))
    if not include_all:
        stmt = query_service.apply_study_uid_tenant_scope(
            stmt, AuditLog, AuditLog.study_instance_uid, tenant_id, False
        ).where(AuditLog.config_key.is_(None))
    rows = (await db.execute(stmt)).scalars().all()
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


# --------------------------------------------------------------------------- #
# /triage
# --------------------------------------------------------------------------- #
#: Severity must sort HIGH -> MEDIUM -> LOW; alphabetical would put MEDIUM first.
_SEVERITY_RANK = case(
    (TriageFlag.severity == "HIGH", 0),
    (TriageFlag.severity == "MEDIUM", 1),
    (TriageFlag.severity == "LOW", 2),
    else_=3,
)


async def list_triage(
    db, *, tenant_id: int | None, include_all: bool
) -> list[dict]:
    """List emergency findings with the owning study's tenant boundary."""
    statement = (
        select(TriageFlag, Study.study_instance_uid, Study.study_date,
               Patient.patient_name, Patient.patient_id)
        .join(Study, Study.study_instance_uid == TriageFlag.study_instance_uid)
        .join(Patient, Patient.id == Study.patient_id)
        .where(TriageFlag.reviewed.is_(False))
    )
    statement = query_service.apply_study_tenant_scope(
        statement, tenant_id, include_all
    )
    flags = (await db.execute(
        statement.order_by(_SEVERITY_RANK, TriageFlag.created_at.desc()).limit(50)
    )).all()
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


async def get_triage_flag(
    db, flag_id: int, *, tenant_id: int | None, include_all: bool
) -> TriageFlag | None:
    """Fetch one triage result only when its linked study is in caller scope."""
    statement = query_service.apply_study_uid_tenant_scope(
        select(TriageFlag).where(TriageFlag.id == flag_id),
        TriageFlag,
        TriageFlag.study_instance_uid,
        tenant_id,
        include_all,
    )
    return (await db.execute(statement)).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Study detail / series
# --------------------------------------------------------------------------- #
async def reload_study(db, study: Study) -> Study:
    """Re-select a study with its patient eager-loaded."""
    return (await db.execute(
        select(Study).options(joinedload(Study.patient)).where(Study.id == study.id)
    )).scalar_one()


async def series_of_study(db, study_id: int) -> list[Series]:
    """Series in display order: numbered series first, then by id."""
    return list((await db.execute(
        select(Series).where(Series.study_id == study_id)
        .order_by(Series.series_number.is_not(None).desc(),
                  Series.series_number, Series.id)
    )).scalars().all())


async def instances_of_series(db, study_uid: str, series_uid: str,
                              tenant_id: int | None, include_all: bool,
                              *, limit: int | None = None) -> list[Instance]:
    """Instances of a series in display order, tenant-scoped."""
    stmt = (
        query_service.instance_query(
            study_uid, series_uid, tenant_id=tenant_id, include_all=include_all
        )
        .order_by(Instance.instance_number.is_not(None).desc(),
                  Instance.instance_number, Instance.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------- #
# Study deletion (C-17 / C-19 / C-22)
# --------------------------------------------------------------------------- #
async def instances_of_study(db, study_id: int) -> list[Instance]:
    """Every instance of a study, for the delete's per-backend grouping."""
    return list((await db.execute(
        select(Instance).where(Instance.series_id.in_(
            select(Series.id).where(Series.study_id == study_id)
        ))
    )).scalars().all())


async def delete_study_rows(
    db, *, study_id: int, study_uid: str, delete_signed_reports: bool = False,
    performed_by: str | None = None,
) -> None:
    """Remove the study's dependent rows and then the study itself.

    Order matters twice:

    * triage flags are keyed by UID (no FK), so nothing cascades them;
    * ``reports.study_id`` is ``ON DELETE RESTRICT`` and does not consider
      report status, so reports must be removed explicitly. Signed reports are
      removed only after the ADMIN-only route has authorized this operation.

    ``series.study_id`` and ``instances.series_id`` are ``ON DELETE CASCADE``
    **in the migration**, so the subtree goes in one statement. The study is
    addressed by id rather than through the ORM object, which the transaction
    boundary in the router has already expired.
    """
    from app.services import lifecycle_service

    await db.execute(sa_delete(TriageFlag).where(
        TriageFlag.study_instance_uid == study_uid))
    report_filter = [RadiologyReport.study_id == study_id]
    if not delete_signed_reports:
        report_filter.append(
            RadiologyReport.status.notin_(lifecycle_service.SIGNED_REPORT_STATUSES)
        )
    reports = list((await db.execute(
        select(RadiologyReport).where(*report_filter)
    )).scalars().all())
    if delete_signed_reports and performed_by:
        for _report in reports:
            audit_service.add_audit(
                db.sync_session,
                sop_instance_uid=None,
                action="REPORT_DELETED",
                performed_by=performed_by,
                study_instance_uid=study_uid,
            )
    for report in reports:
        await db.delete(report)
    await db.execute(sa_delete(Study).where(Study.id == study_id))
