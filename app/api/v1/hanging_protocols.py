"""Hanging Protocol REST API.

  POST   /api/v1/hanging-protocols      — Create hanging protocol
  GET    /api/v1/hanging-protocols      — List hanging protocols
  GET    /api/v1/hanging-protocols/match — Find best matching protocol for modality/body_part
  GET    /api/v1/hanging-protocols/{id}  — Get hanging protocol details
  DELETE /api/v1/hanging-protocols/{id}  — Delete hanging protocol
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1.deps import get_current_user, get_current_user_any, require_csrf, require_destructive_role
from app.db.models import HangingProtocol, User
from app.db.session import get_db
from app.services import hanging_protocol_service

log = logging.getLogger("paxs.api.hp")

router = APIRouter(prefix="/api/v1/hanging-protocols", tags=["Hanging Protocols"])


class CreateHangingProtocolRequest(BaseModel):
    name: str = Field(..., description="Unique protocol name")
    description: str | None = Field(None, description="Description")
    modality: str | None = Field(None, description="Modality filter (e.g. CT, MR, CR)")
    body_part_examined: str | None = Field(None, description="Body Part filter (e.g. CHEST, HEAD)")
    user_id: int | None = Field(None, description="Optional user ID owner")
    priority: int = Field(10, description="Priority rank (lower = higher priority)")
    layout_rows: int = Field(1, description="Viewport rows count")
    layout_columns: int = Field(2, description="Viewport columns count")
    configuration_json: str = Field("{}", description="JSON viewport settings (W/L, series order)")


class SaveUserLayoutRequest(BaseModel):
    modality: str = Field(..., min_length=1, max_length=16, pattern=r"^[A-Za-z0-9_-]+$")
    layout_rows: int = Field(..., ge=1, le=3)
    layout_columns: int = Field(..., ge=1, le=3)


def _hp_to_dict(hp: HangingProtocol) -> dict[str, Any]:
    d = hp.__dict__
    c_at = d.get("created_at")
    u_at = d.get("updated_at")
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "description": d.get("description"),
        "modality": d.get("modality"),
        "body_part_examined": d.get("body_part_examined"),
        "user_id": d.get("user_id"),
        "priority": d.get("priority", 10),
        "layout_rows": d.get("layout_rows", 1),
        "layout_columns": d.get("layout_columns", 2),
        "configuration_json": d.get("configuration_json", "{}"),
        "created_at": c_at.isoformat() if hasattr(c_at, "isoformat") else (str(c_at) if c_at else None),
        "updated_at": u_at.isoformat() if hasattr(u_at, "isoformat") else (str(u_at) if u_at else None),
    }


@router.post("", response_class=JSONResponse)
async def create_hanging_protocol(
    req: CreateHangingProtocolRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new hanging protocol layout rule."""
    try:
        data = req.model_dump()
        if user.role != "ADMIN":
            data["user_id"] = user.id
        hp = await hanging_protocol_service.create_protocol(db, data)
        await db.commit()
        return JSONResponse(_hp_to_dict(hp), status_code=201)
    except Exception as exc:  # API failures may contain PHI or filesystem details
        log.error("hanging_protocol_create_failed error_type=%s", type(exc).__name__)
        raise HTTPException(400, "Failed to create protocol") from exc


@router.get("", response_class=JSONResponse)
async def list_hanging_protocols(
    modality: str | None = Query(None, max_length=16),
    body_part: str | None = Query(None, max_length=64),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_any),
):
    """List global and caller-owned protocols matching supplied study metadata."""
    protocols = await hanging_protocol_service.list_protocols(
        db, user_id=user.id, modality=modality, body_part=body_part
    )
    return JSONResponse([_hp_to_dict(hp) for hp in protocols])


@router.post("/user-preferences", response_class=JSONResponse, dependencies=[Depends(require_csrf)])
async def save_user_layout_preference(
    req: SaveUserLayoutRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
) -> JSONResponse:
    """Save the authenticated radiologist's private layout default for a modality."""
    try:
        protocol = await hanging_protocol_service.save_user_preference(
            db,
            user_id=user.id,
            modality=req.modality,
            layout_rows=req.layout_rows,
            layout_columns=req.layout_columns,
        )
        await db.commit()
        return JSONResponse(_hp_to_dict(protocol))
    except SQLAlchemyError as exc:
        log.error("hanging_protocol_preference_failed error_type=%s", type(exc).__name__)
        raise HTTPException(500, "Failed to save layout preference") from None


@router.get("/match", response_class=JSONResponse)
async def match_hanging_protocol(
    modality: str | None = Query(None, description="Modality filter"),
    body_part: str | None = Query(None, description="Body Part filter"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Find best matching hanging protocol layout for current study context."""
    best = await hanging_protocol_service.match_best_protocol(
        db, modality=modality, body_part=body_part, user_id=user.id
    )
    if not best:
        # Fallback default 1x2 layout
        return JSONResponse(
            {
                "name": "Default 1x2 Layout",
                "layout_rows": 1,
                "layout_columns": 2,
                "configuration_json": "{}",
            }
        )
    return JSONResponse(_hp_to_dict(best))


@router.get("/{protocol_id}", response_class=JSONResponse)
async def get_hanging_protocol(
    protocol_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get details of a specific hanging protocol."""
    hp = await hanging_protocol_service.get_protocol(db, protocol_id)
    if not hp or (hp.user_id is not None and hp.user_id != user.id and user.role != "ADMIN"):
        raise HTTPException(404, f"Hanging Protocol #{protocol_id} not found")
    return JSONResponse(_hp_to_dict(hp))


@router.delete("/{protocol_id}", response_class=JSONResponse)
async def delete_hanging_protocol(
    protocol_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a hanging protocol."""
    protocol = await hanging_protocol_service.get_protocol(db, protocol_id)
    if not protocol or (protocol.user_id != user.id and user.role != "ADMIN"):
        raise HTTPException(404, f"Hanging Protocol #{protocol_id} not found")
    deleted = await hanging_protocol_service.delete_protocol(db, protocol_id)
    if not deleted:
        raise HTTPException(404, f"Hanging Protocol #{protocol_id} not found")
    await db.commit()
    return JSONResponse({"message": f"Hanging Protocol #{protocol_id} deleted successfully"})
