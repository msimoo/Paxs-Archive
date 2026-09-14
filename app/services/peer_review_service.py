"""Peer Review / QA Service — RADPEER quality assurance workflow.

Assigns studies to reviewing radiologists (excluding the original reader),
tracks RADPEER scores (1-4), and provides aggregate QA analytics.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PeerReviewCase, Study
from app.services import audit_service

log = logging.getLogger("paxs.peer_review")


async def create_case(
    session: AsyncSession,
    *,
    study_instance_uid: str,
    original_reader: str,
    patient_id: str | None = None,
    modality: str | None = None,
    body_part_examined: str | None = None,
) -> PeerReviewCase:
    """Create a new peer review case for a study."""
    # Auto-populate metadata from study table if not provided
    if not patient_id or not modality:
        stmt = select(Study).where(Study.study_instance_uid == study_instance_uid)
        study = (await session.execute(stmt)).scalar_one_or_none()
        if study:
            patient_id = patient_id or getattr(study, "patient_id_str", None)
            modality = modality or getattr(study, "modalities_in_study", None)

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

    audit_service.add_audit(
        session, sop_instance_uid=None, action="PEER_REVIEW_CREATED",
        performed_by=original_reader, study_instance_uid=study_instance_uid,
    )
    log.info("Peer review case %d created for study %s", case.id, study_instance_uid)
    return case


async def assign_reviewer(
    session: AsyncSession,
    *,
    case_id: int,
    reviewer: str,
) -> PeerReviewCase:
    """Assign a reviewer to an existing peer review case."""
    case = await session.get(PeerReviewCase, case_id)
    if case is None:
        raise ValueError(f"Peer review case {case_id} not found")
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
) -> PeerReviewCase:
    """Submit a RADPEER score (1-4) for a peer review case."""
    if score not in (1, 2, 3, 4):
        raise ValueError(f"RADPEER score must be 1-4, got {score}")

    case = await session.get(PeerReviewCase, case_id)
    if case is None:
        raise ValueError(f"Peer review case {case_id} not found")
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
) -> list[PeerReviewCase]:
    """List peer review cases with optional filters."""
    stmt = select(PeerReviewCase).order_by(PeerReviewCase.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(PeerReviewCase.status == status)
    if reviewer:
        stmt = stmt.where(PeerReviewCase.reviewer == reviewer)
    return list((await session.execute(stmt)).scalars().all())


async def get_case(session: AsyncSession, case_id: int) -> PeerReviewCase | None:
    """Get a single peer review case by ID."""
    return await session.get(PeerReviewCase, case_id)


async def get_score_summary(session: AsyncSession) -> dict[str, Any]:
    """Aggregate RADPEER score statistics across all completed cases."""
    from sqlalchemy import Integer as SaInteger

    stmt = (
        select(
            sa_func.count(PeerReviewCase.id).label("total_completed"),
            sa_func.avg(PeerReviewCase.score).label("avg_score"),
        )
        .where(PeerReviewCase.status == "COMPLETED")
    )
    row = (await session.execute(stmt)).one()

    # Count significant discrepancies (score >= 3) separately
    disc_stmt = (
        select(sa_func.count(PeerReviewCase.id))
        .where(PeerReviewCase.status == "COMPLETED")
        .where(PeerReviewCase.score >= 3)
    )
    disc_count = (await session.execute(disc_stmt)).scalar() or 0

    return {
        "total_completed": row.total_completed or 0,
        "avg_score": round(float(row.avg_score or 0), 2),
        "significant_discrepancies": disc_count,
    }
