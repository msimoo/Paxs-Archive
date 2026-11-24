"""Auto-routing Rules REST API.

  POST   /api/v1/routing/rules             — Create auto-routing rule
  GET    /api/v1/routing/rules             — List auto-routing rules
  GET    /api/v1/routing/rules/{id}        — Get rule details
  PUT    /api/v1/routing/rules/{id}        — Update rule
  DELETE /api/v1/routing/rules/{id}        — Delete rule
  POST   /api/v1/routing/evaluate/{study}  — Manually evaluate & route a study
  GET    /api/v1/routing/queue             — List send-queue items
  POST   /api/v1/routing/queue/{id}/retry  — Re-queue a FAILED item
  POST   /api/v1/routing/queue/run         — Trigger a queue sweep now
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_user,
    require_admin,
    require_destructive_role,
)
from app.db.models import RoutingRule, User
from app.db.session import get_db
from app.services import routing_service

log = logging.getLogger("paxs.api.routing")

router = APIRouter(prefix="/api/v1/routing", tags=["Auto-routing Rules Engine"])


class CreateRoutingRuleRequest(BaseModel):
    name: str = Field(..., description="Unique rule name")
    description: str | None = Field(None, description="Optional description")
    enabled: bool = Field(True, description="Enable rule evaluation")
    priority: int = Field(10, description="Priority order (lower number = higher priority)")
    destination_node_id: int = Field(..., description="Target DicomNode ID")
    modality: str | None = Field(None, description="Optional Modality filter (e.g. CT, MR)")
    body_part_examined: str | None = Field(None, description="Optional BodyPart filter (e.g. HEAD, CHEST)")
    source_aet: str | None = Field(None, description="Optional Calling AE Title filter")
    study_description_pattern: str | None = Field(None, description="Optional StudyDescription regex/substring filter")
    delay_seconds: int = Field(0, description="Optional dispatch delay in seconds")


class UpdateRoutingRuleRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    destination_node_id: int | None = None
    modality: str | None = None
    body_part_examined: str | None = None
    source_aet: str | None = None
    study_description_pattern: str | None = None
    delay_seconds: int | None = None


def _rule_to_dict(rule: RoutingRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "description": rule.description,
        "enabled": rule.enabled,
        "priority": rule.priority,
        "destination_node_id": rule.destination_node_id,
        "destination_ae_title": rule.destination_node.ae_title if rule.destination_node else None,
        "modality": rule.modality,
        "body_part_examined": rule.body_part_examined,
        "source_aet": rule.source_aet,
        "study_description_pattern": rule.study_description_pattern,
        "delay_seconds": rule.delay_seconds,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }


@router.post("/rules", response_class=JSONResponse)
async def create_routing_rule(
    req: CreateRoutingRuleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Create a new auto-routing rule."""
    try:
        rule = await routing_service.create_rule(db, req.model_dump())
        await db.commit()
        # Fetch with joined relationship
        refreshed = await routing_service.get_rule(db, rule.id)
        return JSONResponse(_rule_to_dict(refreshed or rule), status_code=201)
    except Exception as exc:  # unexpected persistence errors may contain PHI or paths
        log.error("routing_rule_create_failed error_type=%s", type(exc).__name__)
        raise HTTPException(400, "Failed to create routing rule") from None


@router.get("/rules", response_class=JSONResponse)
async def list_routing_rules(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all auto-routing rules ordered by priority."""
    rules = await routing_service.list_rules(db)
    return JSONResponse([_rule_to_dict(r) for r in rules])


@router.get("/rules/{rule_id}", response_class=JSONResponse)
async def get_routing_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get details of a specific routing rule."""
    rule = await routing_service.get_rule(db, rule_id)
    if not rule:
        raise HTTPException(404, f"Routing rule #{rule_id} not found")
    return JSONResponse(_rule_to_dict(rule))


@router.put("/rules/{rule_id}", response_class=JSONResponse)
async def update_routing_rule(
    rule_id: int,
    req: UpdateRoutingRuleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Update an auto-routing rule."""
    rule = await routing_service.get_rule(db, rule_id)
    if not rule:
        raise HTTPException(404, f"Routing rule #{rule_id} not found")

    update_data = req.model_dump(exclude_unset=True)
    for field, val in update_data.items():
        setattr(rule, field, val)

    await db.commit()
    refreshed = await routing_service.get_rule(db, rule_id)
    return JSONResponse(_rule_to_dict(refreshed or rule))


@router.delete("/rules/{rule_id}", response_class=JSONResponse)
async def delete_routing_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Delete an auto-routing rule."""
    deleted = await routing_service.delete_rule(db, rule_id)
    if not deleted:
        raise HTTPException(404, f"Routing rule #{rule_id} not found")
    await db.commit()
    return JSONResponse({"message": f"Routing rule #{rule_id} deleted successfully"})


@router.post("/evaluate/{study_uid}", response_class=JSONResponse)
async def evaluate_study_routing(
    study_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Manually evaluate auto-routing rules for a study and enqueue dispatch."""
    is_admin = user.role.upper() == "ADMIN"
    count = await routing_service.evaluate_and_route_study(
        study_uid, tenant_id=user.tenant_id, include_all=is_admin
    )
    return JSONResponse(
        {"message": f"Evaluated rules for study {study_uid}", "matched_rules_count": count}
    )


@router.get("/queue", response_class=JSONResponse)
async def list_routing_queue(
    status: str | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List Smart Router send-queue items (optionally by status)."""
    is_admin = user.role.upper() == "ADMIN"
    items = await asyncio.to_thread(
        routing_service.list_queue, status, limit, user.tenant_id, is_admin
    )
    return JSONResponse(items)


@router.post("/queue/{queue_id}/retry", response_class=JSONResponse)
async def retry_routing_queue_item(
    queue_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Re-queue a FAILED Smart Router dispatch for immediate retry."""
    is_admin = user.role.upper() == "ADMIN"
    res = await asyncio.to_thread(
        routing_service.retry_queue_item, queue_id, user.tenant_id, is_admin
    )
    if not res.get("ok"):
        raise HTTPException(400, res.get("reason", "Retry failed"))
    return JSONResponse({"message": "Queue item re-queued", "item": res["item"]})


@router.post("/queue/run", response_class=JSONResponse)
async def run_routing_queue(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Trigger or coalesce with the active bounded Smart Router queue sweep."""
    if not routing_service.start_manual_queue_sweep():
        raise HTTPException(503, "Routing queue sweep could not be scheduled")
    return JSONResponse({"message": "Routing queue sweep started"})
