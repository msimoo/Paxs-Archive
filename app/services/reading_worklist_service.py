"""Radiologist reading worklist (roadmap #30 — Unread / Stat / Dictated).

The MWL SCP engine (modality worklist) schedules *exams*; this module builds
the *reader* task lists on top of the archive:

  UNREAD    — study has instances but no radiology report and has not been
              marked read (open the viewer, dictate, finalize).
  DICTATED  — a report exists in DRAFT state (dictated, awaiting finalize).
  READ      — a FINALIZED report exists (or the study was explicitly marked
              read) — the "done" pile.
  STAT      — urgent: manually flagged (stat_flag), has a pending critical
              result, or carries an unreviewed HIGH emergency-triage finding.
              STAT is a filter overlay: a study can be STAT *and* UNREAD /
              DICTATED / READ at the same time.

Every query is VNA-tenant scoped exactly like the dashboard study list
(admins see all tenants; signed-in users see their own tenant; guests see
legacy unscoped data only).
"""

from __future__ import annotations

import logging

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.db.models import (
    CriticalResult,
    Instance,
    RadiologyReport,
    Series,
    Study,
    TriageFlag,
)
from app.services import query_service, reading_claim_service, tenant_service

log = logging.getLogger("paxs.worklist_tasks")

# The three reader states exposed in the UI (plus READ which the tabs also
# count). STAT is a separate overlay, not a lifecycle state.
READER_STATES = ("UNREAD", "DICTATED", "READ")
STAT_SOURCES = ("FLAG", "CRITICAL", "TRIAGE")


def _report_subquery():
    return select(RadiologyReport.study_id, RadiologyReport.status)


def _has_instances_subquery():
    """study ids that have at least one stored instance."""
    return (
        select(Series.study_id)
        .join(Instance, Instance.series_id == Series.id)
        .group_by(Series.study_id)
    )


def _stat_study_ids_subquery():
    """study UIDs that are urgent by any source (manual flag / pending
    critical result / unreviewed HIGH triage)."""
    critical_uids = select(CriticalResult.study_instance_uid).where(
        CriticalResult.status == "PENDING_ACK"
    )
    triage_uids = select(TriageFlag.study_instance_uid).where(
        TriageFlag.reviewed.is_(False), TriageFlag.severity == "HIGH"
    )
    return (
        select(Study.id)
        .where(
            or_(
                Study.stat_flag.is_(True),
                Study.study_instance_uid.in_(critical_uids),
                Study.study_instance_uid.in_(triage_uids),
            )
        )
    )


async def reader_counts(session: AsyncSession, user, include_all: bool | None = None) -> dict:
    """Per-state counts (respecting tenant scope).

    ``include_all`` forces archive-wide counts (used by the dashboard /stats
    endpoint whose other counters are unscoped); when None the caller's
    VNA scope applies.
    """
    tenant_id, scoped_all = tenant_service.scope_from_user(user)
    if include_all is None:
        include_all = scoped_all
    cond = query_service.tenant_predicate(Study.tenant_id, tenant_id, include_all)

    has_instances = _has_instances_subquery()
    stat_ids = _stat_study_ids_subquery()

    base = select(Study.id).where(Study.id.in_(has_instances))
    if cond is not None:
        base = base.where(cond)

    finalized = select(RadiologyReport.study_id).where(
        RadiologyReport.status == "FINALIZED")
    drafted = select(RadiologyReport.study_id).where(
        RadiologyReport.status == "DRAFT")

    async def _count(*extra):
        stmt = base
        for e in extra:
            stmt = stmt.where(e)
        return (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    unread = await _count(
        Study.id.notin_(finalized), Study.id.notin_(drafted), Study.read_at.is_(None)
    )
    dictated = await _count(Study.id.in_(drafted))
    read = await _count(
        or_(Study.id.in_(finalized), Study.read_at.is_not(None))
    )
    stat_total = await _count(Study.id.in_(stat_ids))

    return {
        "unread": int(unread),
        "dictated": int(dictated),
        "read": int(read),
        "stat": int(stat_total),
        # STAT split by source (for the panel detail).
        "stat_by_source": {
            "flagged": int(await _count(Study.stat_flag.is_(True))),
            "critical": int(await _count(
                Study.study_instance_uid.in_(
                    select(CriticalResult.study_instance_uid).where(
                        CriticalResult.status == "PENDING_ACK")
                )
            )),
            "triage": int(await _count(
                Study.study_instance_uid.in_(
                    select(TriageFlag.study_instance_uid).where(
                        TriageFlag.reviewed.is_(False), TriageFlag.severity == "HIGH")
                )
            )),
        },
    }


async def list_reader_tasks(session: AsyncSession, state: str, user,
                            page: int = 1, limit: int = 25,
                            radiologist: str | None = None) -> dict:
    """Studies in a given reader state (or the STAT overlay), newest first."""
    state = (state or "UNREAD").upper()
    if state not in READER_STATES and state != "STAT":
        raise ValueError(f"state must be one of {READER_STATES + ('STAT',)}")

    tenant_id, include_all = tenant_service.scope_from_user(user)
    cond = query_service.tenant_predicate(Study.tenant_id, tenant_id, include_all)

    finalized = select(RadiologyReport.study_id).where(
        RadiologyReport.status == "FINALIZED")
    drafted = select(RadiologyReport.study_id).where(
        RadiologyReport.status == "DRAFT")

    stmt = (
        select(Study)
        .options(joinedload(Study.patient), selectinload(Study.series_list))
        .where(Study.id.in_(_has_instances_subquery()))
    )
    if cond is not None:
        stmt = stmt.where(cond)

    if state == "STAT":
        stmt = stmt.where(Study.id.in_(_stat_study_ids_subquery()))
    elif state == "UNREAD":
        stmt = stmt.where(Study.id.notin_(finalized), Study.id.notin_(drafted),
                         Study.read_at.is_(None))
    elif state == "DICTATED":
        stmt = stmt.where(Study.id.in_(drafted))
    elif state == "READ":
        stmt = stmt.where(
            or_(Study.id.in_(finalized), Study.read_at.is_not(None))
        )

    if radiologist and radiologist.strip():
        stmt = stmt.where(Study.assigned_radiologist == radiologist.strip())

    total = int(
        (await session.execute(
            select(func.count()).select_from(stmt.subquery())
        )).scalar_one()
    )
    stmt = stmt.order_by(
        Study.read_at.is_not(None),
        Study.study_date.is_not(None).desc(),
        Study.study_date.desc(),
        Study.id.desc(),
    )
    studies = (
        await session.execute(
            stmt.limit(limit).offset((page - 1) * limit)
        )
    ).scalars().unique().all()

    study_ids = [s.id for s in studies]
    report_rows = (
        await session.execute(
            select(RadiologyReport.study_id, RadiologyReport.id,
                   RadiologyReport.status, RadiologyReport.oru_status)
            .where(RadiologyReport.study_id.in_(study_ids))
        )
    ).all() if study_ids else []
    report_by_study = {r.study_id: r for r in report_rows}

    stat_uids = set(
        (await session.execute(select(Study.study_instance_uid).where(
            Study.id.in_(study_ids),
            Study.id.in_(_stat_study_ids_subquery()),
        ))).scalars().all()
    ) if study_ids else set()

    # Series/instance aggregates + first-series thumbnail — same shape as the
    # main dashboard study list so the Reading worklist can reuse the upgraded
    # row design (modality rail, count pills, thumbnail skeleton/fade).
    agg = await query_service.study_aggregates(session, study_ids)
    first_series_rows = (
        await session.execute(
            select(Series.study_id, Series.series_instance_uid)
            .where(Series.study_id.in_(study_ids))
            .order_by(Series.id)
        )
    ).all() if study_ids else []
    first_by_study: dict[int, str] = {}
    for sid, series_uid in first_series_rows:
        first_by_study.setdefault(sid, series_uid)

    items = []
    username = getattr(user, "username", None)
    for s in studies:
        rep = report_by_study.get(s.id)
        claim = reading_claim_service.state_of(s, username)
        a = agg.get(s.id, {})
        first_series_uid = first_by_study.get(s.id)
        thumb = None
        if first_series_uid is not None:
            thumb = f"/api/v1/studies/{s.study_instance_uid}/series/{first_series_uid}/thumbnail.png"
        items.append({
            "study_uid": s.study_instance_uid,
            "patient_name": s.patient.patient_name,
            "patient_id": s.patient.patient_id,
            "patient_source": s.patient.source,
            "study_date": s.study_date,
            "study_time": s.study_time,
            "description": s.study_description,
            "accession": s.accession_number,
            "modality": a.get("modalities", []) or _study_modalities(s),
            "n_series": a.get("series", 0),
            "n_instances": a.get("instances", 0),
            "thumbnail": thumb,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "lifecycle_state": s.lifecycle_state,
            "stat": s.study_instance_uid in stat_uids,
            "stat_flag": s.stat_flag,
            "assigned_radiologist": s.assigned_radiologist,
            "read_at": s.read_at.isoformat() if s.read_at else None,
            "read_by": s.read_by,
            # Reading claim: only a live claim is reported (a stale one is free).
            "claimed_by": claim.claimed_by if claim.active else None,
            "claimed_at": claim.claimed_at.isoformat() if claim.active and claim.claimed_at else None,
            "claimed_by_me": claim.mine,
            "report_id": rep.id if rep else None,
            "report_status": rep.status if rep else None,
            "oru_status": rep.oru_status if rep else None,
            "task_state": _task_state(s, rep),
        })
    return {"items": items, "total": total, "page": page, "limit": limit}


def _study_modalities(s: Study) -> list[str]:
    return sorted({se.modality for se in s.series_list if se.modality})


def _task_state(s: Study, rep) -> str:
    if rep is not None and rep.status == "FINALIZED":
        return "READ"
    if rep is not None and rep.status == "DRAFT":
        return "DICTATED"
    return "READ" if s.read_at is not None else "UNREAD"


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #

async def _study_for_action(
    session: AsyncSession, study_uid: str, tenant_id: int | None, include_all: bool,
) -> Study | None:
    """Resolve an action target through the caller's study tenant boundary."""
    statement = query_service.apply_study_tenant_scope(
        select(Study).where(Study.study_instance_uid == study_uid),
        tenant_id,
        include_all,
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def set_stat(
    session: AsyncSession, study_uid: str, on: bool, *,
    tenant_id: int | None = None, include_all: bool = False,
) -> Study:
    """Set a study's STAT flag only inside the supplied tenant scope."""
    study = await _study_for_action(session, study_uid, tenant_id, include_all)
    if study is None:
        raise ValueError("Study not found")
    study.stat_flag = bool(on)
    return study


async def assign_radiologist(session: AsyncSession, study_uid: str,
                             radiologist: str | None, *,
                             tenant_id: int | None = None,
                             include_all: bool = False) -> Study:
    """Assign a reader only to a study inside the supplied tenant scope."""
    study = await _study_for_action(session, study_uid, tenant_id, include_all)
    if study is None:
        raise ValueError("Study not found")
    study.assigned_radiologist = (radiologist or "").strip() or None
    return study


async def mark_read(session: AsyncSession, study_uid: str,
                    performed_by: str, *, tenant_id: int | None = None,
                    include_all: bool = False) -> Study:
    """Mark a scoped study read by the authenticated actor."""
    from datetime import datetime, timezone

    study = await _study_for_action(session, study_uid, tenant_id, include_all)
    if study is None:
        raise ValueError("Study not found")
    study.read_at = datetime.now(timezone.utc).replace(tzinfo=None)
    study.read_by = performed_by
    return study


async def mark_unread(
    session: AsyncSession, study_uid: str, *,
    tenant_id: int | None = None, include_all: bool = False,
) -> Study:
    """Reopen a read state only for a study in the supplied tenant scope."""
    study = await _study_for_action(session, study_uid, tenant_id, include_all)
    if study is None:
        raise ValueError("Study not found")
    study.read_at = None
    study.read_by = None
    return study
