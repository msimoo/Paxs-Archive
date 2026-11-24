"""Peer Review / QA Service — RADPEER quality assurance workflow.

Assigns studies to reviewing radiologists (excluding the original reader),
tracks RADPEER scores (1-4), and provides aggregate QA analytics.
"""

from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import PeerReviewCase, RadiologyReport, Study
from app.services import audit_service, query_service

log = logging.getLogger("paxs.peer_review")


def _audit_case_created(
    session: AsyncSession, case: PeerReviewCase, reader: str, study_uid: str
) -> None:
    """Record the audit event and PHI-minimized creation log."""
    audit_service.add_audit(
        session,
        sop_instance_uid=None,
        action="PEER_REVIEW_CREATED",
        performed_by=reader,
        study_instance_uid=study_uid,
    )
    log.info("Peer review case %d created for study %s", case.id, study_uid)


def _scoped_case_stmt(
    stmt: Select,
    tenant_id: int | None,
    include_all: bool,
) -> Select:
    """Scope cases through the owning study and its linked patient."""
    return query_service.apply_study_uid_tenant_scope(
        stmt, PeerReviewCase, PeerReviewCase.study_instance_uid,
        tenant_id, include_all,
    )


async def _get_case_in_scope(
    session: AsyncSession,
    case_id: int,
    tenant_id: int | None,
    include_all: bool,
) -> PeerReviewCase | None:
    """Return a peer review case only when its owning study is in scope."""
    stmt = _scoped_case_stmt(
        select(PeerReviewCase).where(PeerReviewCase.id == case_id),
        tenant_id, include_all,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_case(
    session: AsyncSession,
    *,
    study_instance_uid: str,
    original_reader: str,
    patient_id: str | None = None,
    modality: str | None = None,
    body_part_examined: str | None = None,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> PeerReviewCase:
    """Create a review case only for a study in the caller's tenant scope."""
    stmt = query_service.apply_study_tenant_scope(
        select(Study).options(joinedload(Study.patient)).where(
            Study.study_instance_uid == study_instance_uid
        ),
        tenant_id,
        include_all,
    )
    study = (await session.execute(stmt)).scalar_one_or_none()
    if study is None and not include_all:
        raise LookupError("Study not found")
    if study is not None:
        patient_id = study.patient.patient_id if study.patient is not None else None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    case = PeerReviewCase(
        study_instance_uid=study_instance_uid,
        patient_id=patient_id,
        modality=modality,
        body_part_examined=body_part_examined,
        original_reader=original_reader,
        status="PENDING",
        created_at=now,
        updated_at=now,
    )
    session.add(case)
    await session.flush()

    _audit_case_created(session, case, original_reader, study_instance_uid)
    return case


async def sample_finalized_reports(
    session: AsyncSession,
    *,
    percent: float,
    days: int,
    limit: int,
    tenant_id: int | None = None,
    include_all: bool = False,
    rng: random.Random | None = None,
) -> list[PeerReviewCase]:
    """Open review cases for a random sample of recently signed reports.

    The RADPEER programme needs cases nobody chose: ``percent`` of the reports
    finalized in the last ``days`` days that are not in peer review yet, at most
    ``limit``, each opened PENDING with the signing radiologist as the original
    reader. A non-empty pool always yields at least one case.
    """
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    reviewed = select(PeerReviewCase.study_instance_uid)
    stmt = query_service.apply_study_tenant_scope(
        select(RadiologyReport.study_instance_uid, RadiologyReport.radiologist)
        .join(Study, Study.id == RadiologyReport.study_id)
        .where(
            RadiologyReport.status == "FINALIZED",
            RadiologyReport.finalized_at >= since,
            RadiologyReport.radiologist != "",
            RadiologyReport.study_instance_uid.notin_(reviewed),
        ),
        tenant_id,
        include_all,
    )
    pool = sorted({(row[0], row[1]) for row in (await session.execute(stmt)).all()})
    if not pool:
        return []
    count = min(limit, len(pool), max(1, math.ceil(len(pool) * percent / 100)))
    # SystemRandom: the sample must not be predictable by the readers it audits.
    chosen = (rng or random.SystemRandom()).sample(pool, count)
    return [
        await create_case(
            session, study_instance_uid=study_uid, original_reader=reader,
            tenant_id=tenant_id, include_all=include_all,
        )
        for study_uid, reader in chosen
    ]


async def assign_reviewer(
    session: AsyncSession,
    *,
    case_id: int,
    reviewer: str,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> PeerReviewCase | None:
    """Assign a reviewer to an existing peer review case."""
    case = await _get_case_in_scope(session, case_id, tenant_id, include_all)
    if case is None:
        return None
    if case.reviewer == reviewer:
        raise ValueError("Reviewer is already assigned to this case")
    if case.original_reader == reviewer:
        raise ValueError("Reviewer cannot be the same as the original reader")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    case.reviewer = reviewer
    case.status = "ASSIGNED"
    case.assigned_at = now
    case.updated_at = now
    await session.flush()

    audit_service.add_audit(
        session, sop_instance_uid=None, action="PEER_REVIEW_ASSIGNED",
        performed_by=reviewer, study_instance_uid=case.study_instance_uid,
    )
    log.info("Peer review case %d assigned to %s", case_id, reviewer)
    return case


async def submit_score(
    session: AsyncSession,
    *,
    case_id: int,
    score: int,
    comments: str | None = None,
    reviewer: str | None = None,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> PeerReviewCase | None:
    """Submit a RADPEER score (1-4) for a peer review case."""
    if score not in (1, 2, 3, 4):
        raise ValueError(f"RADPEER score must be 1-4, got {score}")

    case = await _get_case_in_scope(session, case_id, tenant_id, include_all)
    if case is None:
        return None
    if case.status == "COMPLETED":
        raise ValueError("Case is already completed")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    case.score = score
    case.comments = comments
    case.status = "COMPLETED"
    case.completed_at = now
    case.updated_at = now
    if reviewer:
        case.reviewer = reviewer
    await session.flush()

    audit_service.add_audit(
        session, sop_instance_uid=None, action="PEER_REVIEW_SCORED",
        performed_by=case.reviewer or "unknown",
        study_instance_uid=case.study_instance_uid,
    )
    log.info("Peer review case %d scored %d by %s", case_id, score, case.reviewer)
    return case


async def list_cases(
    session: AsyncSession,
    *,
    status: str | None = None,
    reviewer: str | None = None,
    limit: int = 100,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[PeerReviewCase]:
    """List peer review cases with optional filters."""
    stmt = select(PeerReviewCase).order_by(PeerReviewCase.id.desc()).limit(limit)
    stmt = _scoped_case_stmt(stmt, tenant_id, include_all)
    if status:
        stmt = stmt.where(PeerReviewCase.status == status)
    if reviewer:
        stmt = stmt.where(PeerReviewCase.reviewer == reviewer)
    return list((await session.execute(stmt)).scalars().all())


async def get_case(
    session: AsyncSession,
    case_id: int,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> PeerReviewCase | None:
    """Get a single peer review case by ID."""
    return await _get_case_in_scope(session, case_id, tenant_id, include_all)


async def get_score_summary(
    session: AsyncSession,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> dict[str, Any]:
    """Aggregate RADPEER scores only within the caller's tenant scope."""
    scoped = _scoped_case_stmt(select(PeerReviewCase), tenant_id, include_all)
    completed = scoped.where(PeerReviewCase.status == "COMPLETED").subquery()
    row = (await session.execute(select(
        sa_func.count(completed.c.id).label("total_completed"),
        sa_func.avg(completed.c.score).label("avg_score"),
    ))).one()
    disc_count = (await session.execute(select(sa_func.count(completed.c.id)).where(
        completed.c.score >= 3
    ))).scalar() or 0

    return {
        "total_completed": row.total_completed or 0,
        "avg_score": round(float(row.avg_score or 0), 2),
        "significant_discrepancies": disc_count,
    }
