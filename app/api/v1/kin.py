"""Key Image Notes (KIN / KO) REST API.

  POST /api/v1/kin                        — Create/mark key image notes
  GET  /api/v1/kin/study/{study_uid}      — List KIN for a study
  GET  /api/v1/kin/instance/{sop}/notes   — Find KIN referencing an image
  GET  /api/v1/kin/{sop_uid}              — Get KIN details
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.db.models import KeyImageNote, User
from app.db.session import get_db
from app.services import kin_service

log = logging.getLogger("paxs.api.kin")

router = APIRouter(prefix="/api/v1/kin", tags=["Key Image Notes (KIN)"])


class CreateKINRequest(BaseModel):
    study_instance_uid: str = Field(..., description="Target StudyInstanceUID")
    series_instance_uid: str = Field(..., description="Target SeriesInstanceUID")
    referenced_sop_instance_uids: list[str] = Field(..., description="List of bookmarked SOPInstanceUIDs")
    notes: str | None = Field(None, description="Radiologist notes/remarks")
    title_concept_code: str = Field("113000", description="Title Code: 113000=Of Interest, 113001=Quality Control, 113004=Teaching")


def _kin_to_dict(kin: KeyImageNote) -> dict[str, Any]:
    return {
        "id": kin.id,
        "sop_instance_uid": kin.sop_instance_uid,
        "study_instance_uid": kin.study_instance_uid,
        "series_instance_uid": kin.series_instance_uid,
        "title_concept_code": kin.title_concept_code,
        "title_concept_text": kin.title_concept_text,
        "notes": kin.notes,
        "author_name": kin.author_name,
        "referenced_sop_instance_uids": kin.referenced_sop_instance_uids,
        "file_path": kin.file_path,
        "created_at": kin.created_at.isoformat() if kin.created_at else None,
    }


@router.post("", response_class=JSONResponse)
async def create_key_image_note(
    req: CreateKINRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create and bookmark key images with notes (creates a DICOM KO document)."""
    if not req.referenced_sop_instance_uids:
        raise HTTPException(400, "referenced_sop_instance_uids list cannot be empty")

    try:
        kin = await kin_service.create_key_image_note(
            db,
            study_instance_uid=req.study_instance_uid,
            series_instance_uid=req.series_instance_uid,
            referenced_sop_uids=req.referenced_sop_instance_uids,
            notes=req.notes,
            title_code=req.title_concept_code,
            author_name=user.username,
        )
        await db.commit()
        return JSONResponse(_kin_to_dict(kin), status_code=201)
    except Exception as exc:
        log.exception("Failed to create Key Image Note for study %s", req.study_instance_uid)
        raise HTTPException(500, f"Failed to create Key Image Note: {exc}") from exc


@router.get("/study/{study_uid}", response_class=JSONResponse)
async def list_kin_for_study(
    study_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all Key Image Notes in a study."""
    results = await kin_service.list_kin_for_study(db, study_uid)
    return JSONResponse([_kin_to_dict(k) for k in results])


@router.get("/instance/{sop_uid}/notes", response_class=JSONResponse)
async def find_kin_for_instance(
    sop_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Find Key Image Notes that reference a specific image instance."""
    results = await kin_service.find_kin_for_instance(db, sop_uid)
    return JSONResponse([_kin_to_dict(k) for k in results])


@router.get("/{sop_uid}", response_class=JSONResponse)
async def get_key_image_note(
    sop_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get details of a specific Key Image Note by its SOPInstanceUID."""
    stmt = select(KeyImageNote).where(KeyImageNote.sop_instance_uid == sop_uid)
    kin = (await db.execute(stmt)).scalar_one_or_none()
    if not kin:
        raise HTTPException(404, f"Key Image Note '{sop_uid}' not found")
    return JSONResponse(_kin_to_dict(kin))
