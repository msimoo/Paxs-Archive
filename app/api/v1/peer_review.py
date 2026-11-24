"""Peer Review / QA REST API — RADPEER quality assurance endpoints.

Endpoints for creating, assigning, scoring, and querying peer review cases.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_destructive_role, tenant_scope
from app.db.models import User
from app.db.session import get_db
from app.services import peer_review_service

log = logging.getLogger("paxs.api.peer_review")

router = APIRouter(prefix="/api/v1/peer-review", tags=["peer-review"])


# ── Request schemas ──────────────────────────────────────────────────────────

class CreateCaseRequest(BaseModel):
    study_instance_uid: str
    original_reader: str
    patient_id: str | None = None
    modality: str | None = None
    body_part_examined: str | None = None


class SampleRequest(BaseModel):
    percent: float = Field(5.0, gt=0, le=100, description="Share of signed reports to sample")
    days: int = Field(30, ge=1, le=365, description="Look-back window for finalized reports")
    limit: int = Field(25, ge=1, le=100, description="Most cases to open in one call")


class AssignReviewerRequest(BaseModel):
    reviewer: str


class SubmitScoreRequest(BaseModel):
    score: int = Field(..., ge=1, le=4, description="RADPEER score 1-4")
    comments: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _case_out(case) -> dict:
    return {
        "id": case.id,
        "study_instance_uid": case.study_instance_uid,
        "patient_id": case.patient_id,
        "modality": case.modality,
        "body_part_examined": case.body_part_examined,
        "original_reader": case.original_reader,
        "reviewer": case.reviewer,
        "status": case.status,
        "score": case.score,
        "comments": case.comments,
        "assigned_at": case.assigned_at.isoformat() if case.assigned_at else None,
        "completed_at": case.completed_at.isoformat() if case.completed_at else None,
        "created_at": case.created_at.isoformat() if case.created_at else None,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_case(
    body: CreateCaseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new peer review case for a study."""
    tenant_id, include_all = tenant_scope(user)
    try:
        case = await peer_review_service.create_case(
            db,
            study_instance_uid=body.study_instance_uid,
            original_reader=body.original_reader,
            patient_id=body.patient_id,
            modality=body.modality,
            body_part_examined=body.body_part_examined,
            tenant_id=tenant_id,
            include_all=include_all,
        )
    except LookupError:
        raise HTTPException(404, "Study not found") from None
    return _case_out(case)


@router.post("/sample", status_code=201)
async def sample_cases(
    body: SampleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Open cases for a random sample of recently signed reports (RADPEER)."""
    tenant_id, include_all = tenant_scope(user)
    cases = await peer_review_service.sample_finalized_reports(
        db, percent=body.percent, days=body.days, limit=body.limit,
        tenant_id=tenant_id, include_all=include_all,
    )
    return {"created": len(cases), "cases": [_case_out(c) for c in cases]}


@router.get("")
async def list_cases(
    status: str | None = None,
    reviewer: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List peer review cases with optional status/reviewer filter."""
    tenant_id, include_all = tenant_scope(user)
    cases = await peer_review_service.list_cases(
        db, status=status, reviewer=reviewer,
        tenant_id=tenant_id, include_all=include_all,
    )
    return [_case_out(c) for c in cases]


@router.get("/summary")
async def score_summary(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get aggregate RADPEER score statistics."""
    tenant_id, include_all = tenant_scope(user)
    return await peer_review_service.get_score_summary(db, tenant_id, include_all)


@router.get("/{case_id}")
async def get_case(
    case_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get a single peer review case by ID."""
    tenant_id, include_all = tenant_scope(user)
    case = await peer_review_service.get_case(db, case_id, tenant_id, include_all)
    if case is None:
        raise HTTPException(404, "Peer review case not found")
    return _case_out(case)


@router.post("/{case_id}/assign")
async def assign_reviewer(
    case_id: int,
    body: AssignReviewerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Assign a reviewer to a peer review case."""
    tenant_id, include_all = tenant_scope(user)
    try:
        case = await peer_review_service.assign_reviewer(
            db, case_id=case_id, reviewer=body.reviewer,
            tenant_id=tenant_id, include_all=include_all,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if case is None:
        raise HTTPException(404, "Peer review case not found")
    return _case_out(case)


@router.post("/{case_id}/score")
async def submit_score(
    case_id: int,
    body: SubmitScoreRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Submit a RADPEER score (1-4) for a peer review case."""
    tenant_id, include_all = tenant_scope(user)
    try:
        case = await peer_review_service.submit_score(
            db, case_id=case_id, score=body.score, comments=body.comments,
            tenant_id=tenant_id, include_all=include_all,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if case is None:
        raise HTTPException(404, "Peer review case not found")
    return _case_out(case)
