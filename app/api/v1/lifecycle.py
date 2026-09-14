"""Study Lifecycle Management REST API (item 14).

  POST   /api/v1/lifecycle/rules              — Create lifecycle rule
  GET    /api/v1/lifecycle/rules              — List lifecycle rules
  GET    /api/v1/lifecycle/rules/{id}         — Rule details
  PUT    /api/v1/lifecycle/rules/{id}         — Update rule
  DELETE /api/v1/lifecycle/rules/{id}         — Delete rule
  POST   /api/v1/lifecycle/run                — Manually run rule evaluation
  POST   /api/v1/lifecycle/studies/{uid}/archive — Archive one study (cold tier)
  POST   /api/v1/lifecycle/studies/{uid}/restore — Restore one study (hot tier)
  POST   /api/v1/lifecycle/studies/{uid}/purge    — Permanently delete a study
  GET    /api/v1/lifecycle/studies            — List studies by lifecycle state

Rule writes and the destructive purge are gated: purge requires
`require_destructive_role` (ADMIN / RADIOLOGIST); the rest require a signed-in
user. File operations run through `asyncio.to_thread` (sync backend session).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_destructive_role
from app.db.models import Patient, Study, StudyLifecycleRule, User
from app.db.session import get_db
from app.services import lifecycle_service

log = logging.getLogger("paxs.api.lifecycle")

router = APIRouter(prefix="/api/v1/lifecycle", tags=["Study Lifecycle Management"])


class CreateLifecycleRuleRequest(BaseModel):
    name: str = Field(..., description="Unique rule name")
    description: str | None = Field(None)
    enabled: bool = Field(True)
    priority: int = Field(10, description="Lower number = higher priority")
    action: str = Field("ARCHIVE", description="ARCHIVE / RESTORE / PURGE")
    min_age_days: int = Field(365, description="Minimum study age in days")
    modality: str | None = Field(None, description="Optional Modality filter (CT, MR)")
    body_part_examined: str | None = Field(None, description="Optional Body Part filter")


class UpdateLifecycleRuleRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    action: str | None = None
    min_age_days: int | None = None
    modality: str | None = None
    body_part_examined: str | None = None


def _rule_to_dict(rule: StudyLifecycleRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "description": rule.description,
        "enabled": rule.enabled,
        "priority": rule.priority,
        "action": rule.action,
        "min_age_days": rule.min_age_days,
        "modality": rule.modality,
        "body_part_examined": rule.body_part_examined,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }


@router.post("/rules", status_code=201, response_class=JSONResponse)
async def create_rule(body: CreateLifecycleRuleRequest,
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    try:
        rule = await lifecycle_service.create_rule(db, body.model_dump())
        await db.commit()
        await db.refresh(rule)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        log.exception("Failed to create lifecycle rule %s", body.name)
        raise HTTPException(400, f"Failed to create rule: {exc}") from exc
    return JSONResponse(_rule_to_dict(rule), status_code=201)


@router.get("/rules", response_class=JSONResponse)
async def list_rules(db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    rules = await lifecycle_service.list_rules(db)
    return JSONResponse([_rule_to_dict(r) for r in rules])


@router.get("/rules/{rule_id}", response_class=JSONResponse)
async def get_rule(rule_id: int, db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    rule = await lifecycle_service.get_rule(db, rule_id)
    if not rule:
        raise HTTPException(404, f"Lifecycle rule #{rule_id} not found")
    return JSONResponse(_rule_to_dict(rule))


@router.put("/rules/{rule_id}", response_class=JSONResponse)
async def update_rule(rule_id: int, body: UpdateLifecycleRuleRequest,
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    rule = await lifecycle_service.get_rule(db, rule_id)
    if not rule:
        raise HTTPException(404, f"Lifecycle rule #{rule_id} not found")
    data = body.model_dump(exclude_unset=True)
    if data.get("action"):
        action = data["action"].upper()
        if action not in lifecycle_service.VALID_ACTIONS:
            raise HTTPException(422, f"action must be one of {lifecycle_service.VALID_ACTIONS}")
        data["action"] = action
    for field, val in data.items():
        setattr(rule, field, val)
    await db.commit()
    refreshed = await lifecycle_service.get_rule(db, rule_id)
    return JSONResponse(_rule_to_dict(refreshed or rule))


@router.delete("/rules/{rule_id}", response_class=JSONResponse)
async def delete_rule(rule_id: int, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    deleted = await lifecycle_service.delete_rule(db, rule_id)
    if not deleted:
        raise HTTPException(404, f"Lifecycle rule #{rule_id} not found")
    await db.commit()
    return JSONResponse({"message": f"Lifecycle rule #{rule_id} deleted"})


@router.post("/run", response_class=JSONResponse)
async def run_lifecycle(rule_id: int | None = Query(default=None),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Evaluate enabled lifecycle rules now (or a single rule by id)."""
    result = await asyncio.to_thread(
        lifecycle_service.evaluate_and_run, rule_id, f"api:{user.username}"
    )
    return JSONResponse({
        "message": "Lifecycle run completed",
        "scanned": result["scanned"],
        "archived": result["archived"],
        "restored": result["restored"],
        "purged": result["purged"],
    })


@router.post("/studies/{study_uid}/archive", response_class=JSONResponse)
async def archive_one(study_uid: str, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    result = await asyncio.to_thread(
        lifecycle_service.archive_study, study_uid, f"api:{user.username}"
    )
    if not result["ok"]:
        raise HTTPException(404, result.get("error", "Study not found"))
    return JSONResponse({"study_uid": study_uid, **result})


@router.post("/studies/{study_uid}/restore", response_class=JSONResponse)
async def restore_one(study_uid: str, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    result = await asyncio.to_thread(
        lifecycle_service.restore_study, study_uid, f"api:{user.username}"
    )
    if not result["ok"]:
        raise HTTPException(404, result.get("error", "Study not found"))
    return JSONResponse({"study_uid": study_uid, **result})


@router.post("/studies/{study_uid}/purge", response_class=JSONResponse)
async def purge_one(study_uid: str, db: AsyncSession = Depends(get_db),
                    user: User = Depends(require_destructive_role)):
    result = await asyncio.to_thread(
        lifecycle_service.purge_study, study_uid, f"api:{user.username}"
    )
    if not result["ok"]:
        raise HTTPException(404, result.get("error", "Study not found"))
    return JSONResponse({"study_uid": study_uid, **result})


@router.get("/studies", response_class=JSONResponse)
async def list_studies(state: str = Query(default="ARCHIVED"),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """List studies by lifecycle state (default: the cold tier)."""
    state = (state or "ARCHIVED").upper()
    rows = (
        await db.execute(
            select(Study, Patient.patient_id, Patient.patient_name)
            .join(Patient, Study.patient_id == Patient.id)
            .where(Study.lifecycle_state == state)
            .order_by(Study.study_date.desc(), Study.id.desc())
            .limit(200)
        )
    ).all()
    return JSONResponse([
        {
            "study_uid": s.study_instance_uid,
            "patient_id": pid,
            "patient_name": pname,
            "study_date": s.study_date,
            "study_description": s.study_description,
            "accession": s.accession_number,
            "lifecycle_state": s.lifecycle_state,
            "archived_at": s.archived_at.isoformat() if s.archived_at else None,
        }
        for (s, pid, pname) in rows
    ])
