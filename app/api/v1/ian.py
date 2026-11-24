"""Instance Availability Notification (IAN) REST API.

  POST /api/v1/ian/notify             — Trigger IAN N-CREATE notification for a study
  GET  /api/v1/ian/notifications      — List IAN notification log records
  GET  /api/v1/ian/notifications/{id} — Get specific IAN notification details
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, tenant_scope
from app.db.models import User
from app.db.session import get_db
from app.services import ian_service

log = logging.getLogger("paxs.api.ian")

router = APIRouter(prefix="/api/v1/ian", tags=["Instance Availability Notification (IAN)"])


class TriggerIANRequest(BaseModel):
    study_instance_uid: str = Field(..., description="Target StudyInstanceUID")
    target_ae_title: str | None = Field(None, description="Optional target AE title (notifies all enabled nodes if None)")


def _notif_to_dict(n: Any) -> dict[str, Any]:
    d = n.__dict__
    sent_at = d.get("sent_at")
    created_at = d.get("created_at")
    updated_at = d.get("updated_at")
    return {
        "id": d.get("id"),
        "study_instance_uid": d.get("study_instance_uid"),
        "target_ae_title": d.get("target_ae_title"),
        "instance_availability": d.get("instance_availability", "ONLINE"),
        "status": d.get("status", "PENDING"),
        "payload_json": d.get("payload_json", "{}"),
        "error_message": "IAN delivery failed" if d.get("error_message") else None,
        "sent_at": sent_at.isoformat() if hasattr(sent_at, "isoformat") else (str(sent_at) if sent_at else None),
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else (str(created_at) if created_at else None),
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else (str(updated_at) if updated_at else None),
    }


@router.post("/notify", response_class=JSONResponse)
async def trigger_ian_notification(
    req: TriggerIANRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JSONResponse:
    """Trigger N-CREATE Instance Availability Notification for a study."""
    tenant_id, include_all = tenant_scope(user)
    try:
        notifs = await ian_service.notify_study_available(
            db,
            study_instance_uid=req.study_instance_uid,
            target_ae_title=req.target_ae_title,
            tenant_id=tenant_id,
            include_all=include_all,
        )
        await db.commit()
        return JSONResponse(
            {
                "message": f"IAN notification processed for study {req.study_instance_uid}",
                "notifications": [_notif_to_dict(n) for n in notifs],
            },
            status_code=200,
        )
    except Exception as exc:
        log.error("IAN notification failed error_type=%s", type(exc).__name__)
        raise HTTPException(500, "IAN notification failed") from None


@router.get("/notifications", response_class=JSONResponse)
async def list_ian_notifications(
    study_instance_uid: str | None = Query(None, description="Filter by StudyInstanceUID"),
    status: str | None = Query(None, description="Filter by status (SENT / FAILED / PENDING)"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JSONResponse:
    """List IAN notification logs."""
    tenant_id, include_all = tenant_scope(user)
    results = await ian_service.list_notifications(
        db,
        study_instance_uid=study_instance_uid,
        status=status,
        tenant_id=tenant_id,
        include_all=include_all,
    )
    return JSONResponse([_notif_to_dict(n) for n in results])


@router.get("/notifications/{notif_id}", response_class=JSONResponse)
async def get_ian_notification(
    notif_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JSONResponse:
    """Get details of a specific IAN notification record."""
    tenant_id, include_all = tenant_scope(user)
    notif = await ian_service.get_notification(
        db, notif_id, tenant_id=tenant_id, include_all=include_all
    )
    if not notif:
        raise HTTPException(404, f"IAN notification #{notif_id} not found")
    return JSONResponse(_notif_to_dict(notif))
