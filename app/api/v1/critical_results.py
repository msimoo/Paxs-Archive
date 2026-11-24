"""Critical Results Tracking REST API.

  POST /api/v1/critical-results                — Flag a new critical finding on a study
  POST /api/v1/critical-results/{id}/acknowledge — Formally acknowledge a finding
  GET  /api/v1/critical-results                — Query critical findings
  GET  /api/v1/critical-results/study/{study_uid} — Query findings for a specific study
  GET  /api/v1/critical-results/{id}           — Get critical finding details
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_study_access, tenant_scope
from app.db.models import CriticalResult, User
from app.db.session import get_db
from app.services import critical_results_service

log = logging.getLogger("paxs.api.critical_results")

router = APIRouter(prefix="/api/v1/critical-results", tags=["Critical Results Tracking"])


class CreateCriticalResultRequest(BaseModel):
    study_instance_uid: str = Field(..., description="Target StudyInstanceUID")
    description: str = Field(..., description="Clinical description of the critical finding")
    finding_level: str = Field("CRITICAL_RED", description="Severity: CRITICAL_RED / URGENT_ORANGE / ACTIONABLE_YELLOW")
    series_instance_uid: str | None = Field(None, description="Optional SeriesInstanceUID")
    sop_instance_uid: str | None = Field(None, description="Optional SOPInstanceUID")
    patient_id: str | None = Field(None, description="Optional PatientID")
    communicated_to: str | None = Field(None, description="Name/Role of physician communicated to")
    communication_channel: str | None = Field(None, description="PHONE / WEBSOCKET / SMS / EMAIL")


class AcknowledgeCriticalResultRequest(BaseModel):
    notes: str | None = Field(None, description="Optional acknowledgement notes/remarks")


def _result_to_dict(res: CriticalResult) -> dict[str, Any]:
    return {
        "id": res.id,
        "study_instance_uid": res.study_instance_uid,
        "series_instance_uid": res.series_instance_uid,
        "sop_instance_uid": res.sop_instance_uid,
        "patient_id": res.patient_id,
        "finding_level": res.finding_level,
        "description": res.description,
        "reported_by": res.reported_by,
        "communicated_to": res.communicated_to,
        "communication_channel": res.communication_channel,
        "status": res.status,
        "acknowledged_by": res.acknowledged_by,
        "acknowledged_at": res.acknowledged_at.isoformat() if res.acknowledged_at else None,
        "acknowledgement_notes": res.acknowledgement_notes,
        "created_at": res.created_at.isoformat() if res.created_at else None,
    }


@router.post("", response_class=JSONResponse)
async def create_critical_result(
    req: CreateCriticalResultRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Flag a critical or urgent radiology finding on a study."""
    # B-9: a finding is a study-keyed object. Prove the caller may see the study
    # before creating one, so a foreign study UID answers 404 (never a row that
    # discloses another institution's patient identifier).
    await require_study_access(db, req.study_instance_uid, user)
    tenant_id, include_all = tenant_scope(user)
    try:
        res = await critical_results_service.create_critical_result(
            db,
            study_instance_uid=req.study_instance_uid,
            description=req.description,
            reported_by=user.username,
            finding_level=req.finding_level,
            series_instance_uid=req.series_instance_uid,
            sop_instance_uid=req.sop_instance_uid,
            patient_id=req.patient_id,
            communicated_to=req.communicated_to,
            communication_channel=req.communication_channel,
            tenant_id=tenant_id,
            include_all=include_all,
        )
        await db.commit()
        # MySQL has no RETURNING: the server_default columns are expired by the
        # commit, so refresh before serializing (avoids MissingGreenlet).
        await db.refresh(res)
        return JSONResponse(_result_to_dict(res), status_code=201)
    except Exception as exc:
        # Database and DICOM failures may include clinical values; return only a stable message.
        log.error("critical_result_create_failed error_type=%s", type(exc).__name__)
        raise HTTPException(500, "Failed to create critical result") from None


@router.post("/{result_id}/acknowledge", response_class=JSONResponse)
async def acknowledge_critical_result(
    result_id: int,
    req: AcknowledgeCriticalResultRequest = AcknowledgeCriticalResultRequest(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Formally acknowledge receipt of a critical finding."""
    tenant_id, include_all = tenant_scope(user)
    try:
        res = await critical_results_service.acknowledge_critical_result(
            db,
            result_id=result_id,
            acknowledged_by=user.username,
            notes=req.notes,
            tenant_id=tenant_id,
            include_all=include_all,
        )
        await db.commit()
        return JSONResponse(_result_to_dict(res), status_code=200)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        # Acknowledgement notes and database parameters are clinical data; do not log or echo them.
        log.error("critical_result_acknowledge_failed error_type=%s", type(exc).__name__)
        raise HTTPException(500, "Acknowledgement failed") from None


@router.get("", response_class=JSONResponse)
async def list_critical_results(
    status: str | None = Query(None, description="Filter by status (PENDING_ACK / ACKNOWLEDGED)"),
    finding_level: str | None = Query(None, description="Filter by finding level (CRITICAL_RED / URGENT_ORANGE)"),
    patient_id: str | None = Query(None, description="Filter by PatientID"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Query critical results with filters."""
    tenant_id, include_all = tenant_scope(user)
    results = await critical_results_service.list_critical_results(
        db,
        status=status,
        finding_level=finding_level,
        patient_id=patient_id,
        tenant_id=tenant_id,
        include_all=include_all,
    )
    return JSONResponse([_result_to_dict(r) for r in results])


@router.get("/study/{study_uid}", response_class=JSONResponse)
async def get_study_critical_results(
    study_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all critical findings associated with a specific study."""
    # B-9: a foreign study UID must 404, not return an empty list — an empty 200
    # would confirm the UID exists and leave the caller guessing.
    await require_study_access(db, study_uid, user)
    tenant_id, include_all = tenant_scope(user)
    results = await critical_results_service.list_critical_results(
        db,
        study_instance_uid=study_uid,
        tenant_id=tenant_id,
        include_all=include_all,
    )
    return JSONResponse([_result_to_dict(r) for r in results])


@router.get("/{result_id}", response_class=JSONResponse)
async def get_critical_result(
    result_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get details of a specific critical result."""
    tenant_id, include_all = tenant_scope(user)
    res = await critical_results_service.get_critical_result(
        db, result_id, tenant_id=tenant_id, include_all=include_all
    )
    if not res:
        raise HTTPException(404, f"Critical result #{result_id} not found")
    return JSONResponse(_result_to_dict(res))
