"""Clinical Protocols Library REST API (roadmap #31).

  POST   /api/v1/clinical-protocols             — Create protocol
  GET    /api/v1/clinical-protocols             — List protocols
  GET    /api/v1/clinical-protocols/match       — Best match for study context
  GET    /api/v1/clinical-protocols/suggest     — Ranked candidate list
  GET    /api/v1/clinical-protocols/{id}        — Get protocol
  PATCH  /api/v1/clinical-protocols/{id}        — Update protocol
  DELETE /api/v1/clinical-protocols/{id}        — Delete protocol
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_destructive_role
from app.db.models import ClinicalProtocol, User
from app.db.session import get_db
from app.services import clinical_protocol_service as cps

log = logging.getLogger("paxs.api.clinical_protocols")

router = APIRouter(prefix="/api/v1/clinical-protocols", tags=["Clinical Protocols"])


class ProtocolRequest(BaseModel):
    name: str = Field(..., max_length=128)
    description: str | None = Field(default=None, max_length=512)
    standardized_name: str | None = Field(default=None, max_length=255)
    ris_code: str | None = Field(default=None, max_length=64)
    modality: str | None = Field(default=None, max_length=16)
    body_part_examined: str | None = Field(default=None, max_length=64)
    study_description_pattern: str | None = Field(default=None, max_length=255)
    priority: int = Field(default=10, ge=0, le=1000)
    enabled: bool = True


class ProtocolUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    standardized_name: str | None = Field(default=None, max_length=255)
    ris_code: str | None = Field(default=None, max_length=64)
    modality: str | None = Field(default=None, max_length=16)
    body_part_examined: str | None = Field(default=None, max_length=64)
    study_description_pattern: str | None = Field(default=None, max_length=255)
    priority: int | None = Field(default=None, ge=0, le=1000)
    enabled: bool | None = None


def _cp_to_dict(cp: ClinicalProtocol) -> dict[str, Any]:
    # Read from __dict__ (like _hp_to_dict) — attribute access after an
    # UPDATE flush would lazily refresh the server-side `updated_at` and
    # raise MissingGreenlet outside the async greenlet.
    d = cp.__dict__
    c_at = d.get("created_at")
    u_at = d.get("updated_at")
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "description": d.get("description"),
        "standardized_name": d.get("standardized_name"),
        "ris_code": d.get("ris_code"),
        "modality": d.get("modality"),
        "body_part_examined": d.get("body_part_examined"),
        "study_description_pattern": d.get("study_description_pattern"),
        "priority": d.get("priority", 10),
        "enabled": d.get("enabled", True),
        "created_at": c_at.isoformat() if hasattr(c_at, "isoformat") else (str(c_at) if c_at else None),
        "updated_at": u_at.isoformat() if hasattr(u_at, "isoformat") else (str(u_at) if u_at else None),
    }


@router.post("", response_class=JSONResponse)
async def create_protocol(req: ProtocolRequest, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_destructive_role)):
    try:
        cp = await cps.create_protocol(db, req.model_dump())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return JSONResponse(_cp_to_dict(cp), status_code=201)


@router.get("", response_class=JSONResponse)
async def list_protocols(db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    protocols = await cps.list_protocols(db)
    return JSONResponse([_cp_to_dict(cp) for cp in protocols])


@router.get("/match", response_class=JSONResponse)
async def match_protocol(study_description: str | None = Query(None),
                         modality: str | None = Query(None),
                         body_part: str | None = Query(None),
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """Best matching protocol for a study context (or null)."""
    best = await cps.match_best_protocol(
        db, study_description=study_description,
        modality=modality, body_part=body_part,
    )
    if best is None:
        return JSONResponse({"match": None})
    return JSONResponse({"match": _cp_to_dict(best)})


@router.get("/suggest", response_class=JSONResponse)
async def suggest_protocols(study_description: str | None = Query(None),
                            modality: str | None = Query(None),
                            body_part: str | None = Query(None),
                            limit: int = Query(10, ge=1, le=50),
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """Ranked candidate protocols for the study context."""
    rows = await cps.suggest_matches(
        db, study_description=study_description,
        modality=modality, body_part=body_part, limit=limit,
    )
    return JSONResponse({"items": rows})


@router.get("/{protocol_id}", response_class=JSONResponse)
async def get_protocol(protocol_id: int, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    cp = await cps.get_protocol(db, protocol_id)
    if cp is None:
        raise HTTPException(404, f"Protocol #{protocol_id} not found")
    return JSONResponse(_cp_to_dict(cp))


@router.patch("/{protocol_id}", response_class=JSONResponse)
async def update_protocol(protocol_id: int, req: ProtocolUpdate,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_destructive_role)):
    try:
        cp = await cps.update_protocol(db, protocol_id, req.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if cp is None:
        raise HTTPException(404, f"Protocol #{protocol_id} not found")
    return JSONResponse(_cp_to_dict(cp))


@router.delete("/{protocol_id}", response_class=JSONResponse)
async def delete_protocol(protocol_id: int, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_destructive_role)):
    deleted = await cps.delete_protocol(db, protocol_id)
    if not deleted:
        raise HTTPException(404, f"Protocol #{protocol_id} not found")
    return JSONResponse({"message": f"Protocol #{protocol_id} deleted"})
