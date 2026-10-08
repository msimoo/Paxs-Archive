"""Study Split and Merge REST API.

  POST /api/v1/studies/split — Split series into a new/different study
  POST /api/v1/studies/merge — Merge series/studies into a target study
"""

from __future__ import annotations

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
from app.db.models import User
from app.db.session import get_db
from app.services import split_merge_service

log = logging.getLogger("paxs.api.split_merge")

router = APIRouter(prefix="/api/v1/studies", tags=["Study Split & Merge"])


class SplitStudyRequest(BaseModel):
    source_study_uid: str = Field(..., description="StudyInstanceUID of the study to split from")
    series_instance_uids: list[str] = Field(..., description="List of SeriesInstanceUIDs to move to new study")
    target_patient_id: str | None = Field(None, description="Optional target PatientID (creates or links patient)")
    target_patient_name: str | None = Field(None, description="Optional target PatientName")
    new_study_description: str | None = Field(None, description="Optional study description for new study")


class MergeStudyRequest(BaseModel):
    source_study_uids: list[str] = Field(..., description="List of StudyInstanceUIDs to merge from")
    target_study_uid: str = Field(..., description="Target StudyInstanceUID to merge all series into")


@router.post("/split", response_class=JSONResponse)
async def split_study(
    req: SplitStudyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Split specific series from a study into a new study."""
    if not req.series_instance_uids:
        raise HTTPException(400, "series_instance_uids list cannot be empty")

    try:
        new_study = await split_merge_service.split_series_to_new_study(
            db,
            source_study_uid=req.source_study_uid,
            series_uids=req.series_instance_uids,
            target_patient_id=req.target_patient_id,
            target_patient_name=req.target_patient_name,
            new_study_description=req.new_study_description,
            performed_by=user.username,
        )
        await db.commit()
        return JSONResponse(
            {
                "message": "Study split successfully",
                "new_study_instance_uid": new_study.study_instance_uid,
                "patient_id": new_study.patient.patient_id,
            },
            status_code=200,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        log.exception("Failed to split study %s", req.source_study_uid)
        raise HTTPException(500, f"Split failed: {exc}") from exc


@router.post("/merge", response_class=JSONResponse)
async def merge_studies(
    req: MergeStudyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Merge source studies into a target study."""
    if not req.source_study_uids:
        raise HTTPException(400, "source_study_uids list cannot be empty")

    try:
        target_study = await split_merge_service.merge_studies(
            db,
            source_study_uids=req.source_study_uids,
            target_study_uid=req.target_study_uid,
            performed_by=user.username,
        )
        await db.commit()
        return JSONResponse(
            {
                "message": "Studies merged successfully",
                "target_study_instance_uid": target_study.study_instance_uid,
            },
            status_code=200,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        log.exception("Failed to merge studies into %s", req.target_study_uid)
        raise HTTPException(500, f"Merge failed: {exc}") from exc
