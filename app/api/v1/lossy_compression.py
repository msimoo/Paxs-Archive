"""Lossy Compression Engine REST API.

  POST   /api/v1/compression/lossy/rules      — Create lossy compression rule
  GET    /api/v1/compression/lossy/rules      — List lossy compression rules
  GET    /api/v1/compression/lossy/rules/{id} — Get rule details
  PUT    /api/v1/compression/lossy/rules/{id} — Update rule
  DELETE /api/v1/compression/lossy/rules/{id} — Delete rule
  POST   /api/v1/compression/lossy/run        — Manually trigger lossy compression job
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_user,
    require_admin,
    require_destructive_role,
)
from app.db.models import LossyCompressionRule, User
from app.db.session import get_db
from app.services import lossy_compression_service

log = logging.getLogger("paxs.api.lossy_compression")

router = APIRouter(prefix="/api/v1/compression/lossy", tags=["Lossy Compression Engine"])


class CreateLossyRuleRequest(BaseModel):
    name: str = Field(..., description="Unique rule name")
    description: str | None = Field(None, description="Optional description")
    enabled: bool = Field(True, description="Enable rule evaluation")
    priority: int = Field(10, description="Priority order (lower number = higher priority)")
    min_age_days: int = Field(365, description="Minimum study age in days before transcoding")
    modality: str | None = Field(None, description="Optional Modality filter (e.g. CT, MR)")
    body_part_examined: str | None = Field(None, description="Optional BodyPart filter (e.g. CHEST, HEAD)")
    transfer_syntax_uid: str = Field(
        "1.2.840.10008.1.2.4.81",
        description=(
            "Target lossy TransferSyntaxUID. Supported (an installed encoder must "
            "exist): JPEG-LS Near-Lossless (1.2.840.10008.1.2.4.81) or "
            "JPEG 2000 Lossy (1.2.840.10008.1.2.4.91). JPEG Baseline (…4.50) is "
            "not encodable and is rejected."
        ),
    )
    quality_factor: int = Field(85, description="JPEG quality factor (1..100)")


class UpdateLossyRuleRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    min_age_days: int | None = None
    modality: str | None = None
    body_part_examined: str | None = None
    transfer_syntax_uid: str | None = None
    quality_factor: int | None = None


def _rule_to_dict(rule: LossyCompressionRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "description": rule.description,
        "enabled": rule.enabled,
        "priority": rule.priority,
        "min_age_days": rule.min_age_days,
        "modality": rule.modality,
        "body_part_examined": rule.body_part_examined,
        "transfer_syntax_uid": rule.transfer_syntax_uid,
        "quality_factor": rule.quality_factor,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }


@router.post("/rules", response_class=JSONResponse)
async def create_lossy_rule(
    req: CreateLossyRuleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Create a new lossy compression policy rule."""
    try:
        rule = await lossy_compression_service.create_rule(db, req.model_dump())
        await db.commit()
        return JSONResponse(_rule_to_dict(rule), status_code=201)
    except lossy_compression_service.LossyCompressionError as exc:
        # Unsupported/unencodable target — reject before it can be stored.
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # unexpected persistence errors may contain PHI or paths
        log.error("lossy_rule_create_failed error_type=%s", type(exc).__name__)
        raise HTTPException(400, "Failed to create rule") from None


@router.get("/rules", response_class=JSONResponse)
async def list_lossy_rules(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all lossy compression policy rules."""
    rules = await lossy_compression_service.list_rules(db)
    return JSONResponse([_rule_to_dict(r) for r in rules])


@router.get("/rules/{rule_id}", response_class=JSONResponse)
async def get_lossy_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get details of a specific lossy compression rule."""
    rule = await lossy_compression_service.get_rule(db, rule_id)
    if not rule:
        raise HTTPException(404, f"Lossy compression rule #{rule_id} not found")
    return JSONResponse(_rule_to_dict(rule))


@router.put("/rules/{rule_id}", response_class=JSONResponse)
async def update_lossy_rule(
    rule_id: int,
    req: UpdateLossyRuleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Update a lossy compression rule."""
    rule = await lossy_compression_service.get_rule(db, rule_id)
    if not rule:
        raise HTTPException(404, f"Lossy compression rule #{rule_id} not found")

    update_data = req.model_dump(exclude_unset=True)
    if update_data.get("transfer_syntax_uid") is not None:
        try:
            lossy_compression_service.validate_target_transfer_syntax(
                update_data["transfer_syntax_uid"]
            )
        except lossy_compression_service.LossyCompressionError as exc:
            raise HTTPException(400, str(exc)) from exc
    for field, val in update_data.items():
        setattr(rule, field, val)

    await db.commit()
    refreshed = await lossy_compression_service.get_rule(db, rule_id)
    return JSONResponse(_rule_to_dict(refreshed or rule))


@router.delete("/rules/{rule_id}", response_class=JSONResponse)
async def delete_lossy_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Delete a lossy compression rule."""
    deleted = await lossy_compression_service.delete_rule(db, rule_id)
    if not deleted:
        raise HTTPException(404, f"Lossy compression rule #{rule_id} not found")
    await db.commit()
    return JSONResponse({"message": f"Lossy compression rule #{rule_id} deleted successfully"})


@router.post("/run", response_class=JSONResponse)
async def trigger_lossy_compression_run(
    rule_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Trigger an on-demand lossy compression evaluation & transcoding run.

    Fail-closed: while ``LOSSY_COMPRESSION_ENABLED`` is false the run is a no-op
    (``disabled: true``, zero processed) and no object is modified.
    """
    res = await lossy_compression_service.evaluate_and_run_lossy_compression(db, rule_id)
    await db.commit()
    return JSONResponse(
        {
            "message": (
                "Lossy compression is disabled (LOSSY_COMPRESSION_ENABLED=false)"
                if res.get("disabled")
                else "Lossy compression run completed"
            ),
            "processed_instances": res.get("processed", 0),
            "skipped_instances": res.get("skipped", 0),
            "disabled": bool(res.get("disabled", False)),
        }
    )
