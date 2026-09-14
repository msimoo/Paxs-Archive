"""Prefetching Engine REST API.

  POST   /api/v1/prefetch/rules      — Create prefetch rule
  GET    /api/v1/prefetch/rules      — List prefetch rules
  GET    /api/v1/prefetch/rules/{id} — Get rule details
  PUT    /api/v1/prefetch/rules/{id} — Update rule
  DELETE /api/v1/prefetch/rules/{id} — Delete rule
  POST   /api/v1/prefetch/trigger    — Trigger manual prefetch for patient
  GET    /api/v1/prefetch/jobs       — List prefetch job logs
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.db.models import PrefetchJob, PrefetchRule, User
from app.db.session import get_db
from app.services import prefetch_service

log = logging.getLogger("paxs.api.prefetch")

router = APIRouter(prefix="/api/v1/prefetch", tags=["Prefetching Engine"])


class CreatePrefetchRuleRequest(BaseModel):
    name: str = Field(..., description="Unique rule name")
    description: str | None = Field(None, description="Optional description")
    enabled: bool = Field(True, description="Enable rule")
    priority: int = Field(10, description="Priority order (lower number = higher priority)")
    modality: str | None = Field("ALL", description="Modality filter (e.g. CT, MR, CR, ALL)")
    body_part_examined: str | None = Field(None, description="Optional BodyPart filter")
    max_prior_age_days: int = Field(730, description="Fetch priors within N days")
    max_priors_count: int = Field(3, description="Maximum number of prior studies to fetch")
    source_node_id: int = Field(..., description="Remote archive DicomNode ID")


class UpdatePrefetchRuleRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    modality: str | None = None
    body_part_examined: str | None = None
    max_prior_age_days: int | None = None
    max_priors_count: int | None = None
    source_node_id: int | None = None


class TriggerPrefetchRequest(BaseModel):
    patient_id: str = Field(..., description="Patient ID to prefetch priors for")
    worklist_step_id: int | None = Field(None, description="Optional associated WorklistStep ID")
    modality: str | None = Field(None, description="Optional modality filter")
    body_part_examined: str | None = Field(None, description="Optional body part filter")


def _rule_to_dict(r: PrefetchRule) -> dict[str, Any]:
    d = r.__dict__
    c_at = d.get("created_at")
    u_at = d.get("updated_at")
    node = d.get("source_node")
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "description": d.get("description"),
        "enabled": d.get("enabled", True),
        "priority": d.get("priority", 10),
        "modality": d.get("modality"),
        "body_part_examined": d.get("body_part_examined"),
        "max_prior_age_days": d.get("max_prior_age_days", 730),
        "max_priors_count": d.get("max_priors_count", 3),
        "source_node_id": d.get("source_node_id"),
        "source_node_ae_title": node.ae_title if node else None,
        "created_at": c_at.isoformat() if hasattr(c_at, "isoformat") else (str(c_at) if c_at else None),
        "updated_at": u_at.isoformat() if hasattr(u_at, "isoformat") else (str(u_at) if u_at else None),
    }


def _job_to_dict(j: PrefetchJob) -> dict[str, Any]:
    d = j.__dict__
    c_at = d.get("created_at")
    u_at = d.get("updated_at")
    node = d.get("source_node")
    return {
        "id": d.get("id"),
        "patient_id": d.get("patient_id"),
        "worklist_step_id": d.get("worklist_step_id"),
        "source_node_id": d.get("source_node_id"),
        "source_node_ae_title": node.ae_title if node else None,
        "status": d.get("status", "PENDING"),
        "priors_found": d.get("priors_found", 0),
        "priors_retrieved": d.get("priors_retrieved", 0),
        "error_message": d.get("error_message"),
        "created_at": c_at.isoformat() if hasattr(c_at, "isoformat") else (str(c_at) if c_at else None),
        "updated_at": u_at.isoformat() if hasattr(u_at, "isoformat") else (str(u_at) if u_at else None),
    }


@router.post("/rules", response_class=JSONResponse)
async def create_prefetch_rule(
    req: CreatePrefetchRuleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new automated prefetching rule."""
    try:
        rule = await prefetch_service.create_rule(db, req.model_dump())
        await db.commit()
        refreshed = await prefetch_service.get_rule(db, rule.id)
        return JSONResponse(_rule_to_dict(refreshed or rule), status_code=201)
    except Exception as exc:
        log.exception("Failed to create prefetch rule %s", req.name)
        raise HTTPException(400, f"Failed to create rule: {exc}") from exc


@router.get("/rules", response_class=JSONResponse)
async def list_prefetch_rules(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all active prefetch rules."""
    rules = await prefetch_service.list_rules(db)
    return JSONResponse([_rule_to_dict(r) for r in rules])


@router.get("/rules/{rule_id}", response_class=JSONResponse)
async def get_prefetch_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get details of a specific prefetch rule."""
    rule = await prefetch_service.get_rule(db, rule_id)
    if not rule:
        raise HTTPException(404, f"Prefetch rule #{rule_id} not found")
    return JSONResponse(_rule_to_dict(rule))


@router.put("/rules/{rule_id}", response_class=JSONResponse)
async def update_prefetch_rule(
    rule_id: int,
    req: UpdatePrefetchRuleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update a prefetch rule."""
    rule = await prefetch_service.get_rule(db, rule_id)
    if not rule:
        raise HTTPException(404, f"Prefetch rule #{rule_id} not found")

    update_data = req.model_dump(exclude_unset=True)
    for field, val in update_data.items():
        setattr(rule, field, val)

    await db.commit()
    refreshed = await prefetch_service.get_rule(db, rule_id)
    return JSONResponse(_rule_to_dict(refreshed or rule))


@router.delete("/rules/{rule_id}", response_class=JSONResponse)
async def delete_prefetch_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a prefetch rule."""
    deleted = await prefetch_service.delete_rule(db, rule_id)
    if not deleted:
        raise HTTPException(404, f"Prefetch rule #{rule_id} not found")
    await db.commit()
    return JSONResponse({"message": f"Prefetch rule #{rule_id} deleted successfully"})


@router.post("/trigger", response_class=JSONResponse)
async def trigger_prefetch(
    req: TriggerPrefetchRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Trigger manual prefetch evaluation for a patient."""
    jobs = await prefetch_service.evaluate_and_trigger_prefetch(
        db,
        patient_id=req.patient_id,
        worklist_step_id=req.worklist_step_id,
        modality=req.modality,
        body_part=req.body_part_examined,
    )
    await db.commit()
    return JSONResponse(
        {
            "message": f"Prefetch evaluation completed for patient {req.patient_id}",
            "jobs": [_job_to_dict(j) for j in jobs],
        }
    )


@router.get("/jobs", response_class=JSONResponse)
async def list_prefetch_jobs(
    patient_id: str | None = Query(None, description="Filter by Patient ID"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List prefetch job execution logs."""
    jobs = await prefetch_service.list_jobs(db, patient_id=patient_id)
    return JSONResponse([_job_to_dict(j) for j in jobs])
