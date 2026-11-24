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
  GET    /api/v1/lifecycle/orphaned-objects   — Objects whose deletion failed (A-15)
  POST   /api/v1/lifecycle/orphaned-objects/retry — Retry those deletions (A-15)

Rule writes and the destructive purge are gated: the purge and every rule write
require ``require_admin`` (stricter than the destructive-role set, which also
admits RADIOLOGIST); the read endpoints require a signed-in user. File operations
run through `asyncio.to_thread` (sync backend session).

A purge is all-or-nothing (A-14/A-15): if any object cannot be unlinked the study
metadata is deliberately **kept**, the failures are queued in
``orphaned_objects``, and the endpoint answers 409 — the study still exists, so
404 would be a lie and 200 would hide an operator's work item.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_user,
    require_admin,
    tenant_scope,
)
from app.db.models import StudyLifecycleRule, User
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
                      user: User = Depends(require_admin)):
    try:
        rule = await lifecycle_service.create_rule(db, body.model_dump())
        await db.commit()
        await db.refresh(rule)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # persistence failures may contain PHI or storage details
        log.error("lifecycle_rule_create_failed error_type=%s", type(exc).__name__)
        raise HTTPException(400, "Failed to create rule") from None
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
                      user: User = Depends(require_admin)):
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
                      user: User = Depends(require_admin)):
    deleted = await lifecycle_service.delete_rule(db, rule_id)
    if not deleted:
        raise HTTPException(404, f"Lifecycle rule #{rule_id} not found")
    await db.commit()
    return JSONResponse({"message": f"Lifecycle rule #{rule_id} deleted"})


@router.post("/run", response_class=JSONResponse)
async def run_lifecycle(rule_id: int | None = Query(default=None),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_admin)):
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
                      user: User = Depends(require_admin)):
    result = await asyncio.to_thread(
        lifecycle_service.archive_study, study_uid, f"api:{user.username}"
    )
    if not result["ok"]:
        raise HTTPException(404, result.get("error", "Study not found"))
    return JSONResponse({"study_uid": study_uid, **result})


@router.post("/studies/{study_uid}/restore", response_class=JSONResponse)
async def restore_one(study_uid: str, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_admin)):
    result = await asyncio.to_thread(
        lifecycle_service.restore_study, study_uid, f"api:{user.username}"
    )
    if not result["ok"]:
        raise HTTPException(404, result.get("error", "Study not found"))
    return JSONResponse({"study_uid": study_uid, **result})


@router.post("/studies/{study_uid}/purge", response_class=JSONResponse)
async def purge_one(study_uid: str, db: AsyncSession = Depends(get_db),
                    user: User = Depends(require_admin)):
    result = await asyncio.to_thread(
        lifecycle_service.purge_study, study_uid, f"api:{user.username}",
        allow_signed_report=True,
    )
    if not result["ok"]:
        if result.get("state") == "PURGING":
            # A-14/A-15: not every object could be unlinked, so the metadata was
            # deliberately kept and the failures queued. 409 — the study is still
            # here and an operator has a work item; 404 would be a lie.
            raise HTTPException(409, result.get("error", "Purge refused"))
        raise HTTPException(404, result.get("error", "Study not found"))
    return JSONResponse({"study_uid": study_uid, **result})


@router.get("/orphaned-objects", response_class=JSONResponse)
async def list_orphaned_objects(include_resolved: bool = Query(default=False),
                                user: User = Depends(require_admin)):
    """A-15: objects whose deletion failed, awaiting an operator's retry."""
    rows = await asyncio.to_thread(
        lifecycle_service.list_orphaned_objects, include_resolved
    )
    return JSONResponse({"count": len(rows), "objects": rows})


@router.post("/orphaned-objects/retry", response_class=JSONResponse)
async def retry_orphaned_objects(user: User = Depends(require_admin)):
    """A-15: re-attempt every outstanding failed deletion."""
    return JSONResponse(
        await asyncio.to_thread(lifecycle_service.retry_orphaned_deletions)
    )


@router.get("/studies", response_class=JSONResponse)
async def list_studies(state: str = Query(default="ARCHIVED"),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """List studies by lifecycle state (default: the cold tier)."""
    state = (state or "ARCHIVED").upper()
    tenant_id, include_all = tenant_scope(user)
    rows = await lifecycle_service.list_studies_by_lifecycle_state(
        db, state, tenant_id, include_all
    )
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
