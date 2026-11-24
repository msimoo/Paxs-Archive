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

from app.api.v1.deps import get_current_user, require_csrf, require_destructive_role, require_study_access
from app.db.models import User
from app.db.session import get_db
from app.services import split_merge_service, tag_editor_service

log = logging.getLogger("paxs.api.split_merge")

router = APIRouter(prefix="/api/v1/studies", tags=["Study Split & Merge"])


class SplitStudyRequest(BaseModel):
    source_study_uid: str = Field(..., description="StudyInstanceUID of the study to split from")
    series_instance_uids: list[str] = Field(..., description="List of SeriesInstanceUIDs to move to new study")
    target_patient_id: str | None = Field(None, description="Optional target PatientID (creates or links patient)")
    target_patient_name: str | None = Field(None, description="Optional target PatientName")
    new_study_description: str | None = Field(None, description="Optional study description for new study")
    tenant_id: int | None = Field(
        None,
        description="Admin-only explicit tenant scope; required to assign an unscoped legacy study.",
    )


class MergeStudyRequest(BaseModel):
    source_study_uids: list[str] = Field(..., description="List of StudyInstanceUIDs to merge from")
    target_study_uid: str = Field(..., description="Target StudyInstanceUID to merge all series into")
    tenant_id: int | None = Field(
        None,
        description="Admin-only explicit tenant scope; required to assign unscoped legacy studies.",
    )


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
        tenant_id, assignment_id = await split_merge_service.resolve_operation_tenant(
            db,
            [req.source_study_uid],
            caller_tenant_id=user.tenant_id,
            is_admin=user.role == "ADMIN",
            requested_tenant_id=req.tenant_id,
        )
        new_study = await split_merge_service.split_series_to_new_study(
            db,
            source_study_uid=req.source_study_uid,
            series_uids=req.series_instance_uids,
            target_patient_id=req.target_patient_id,
            target_patient_name=req.target_patient_name,
            new_study_description=req.new_study_description,
            performed_by=user.username,
            tenant_id=tenant_id,
            assign_unassigned_tenant_id=assignment_id,
            is_admin=user.role == "ADMIN",
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
    except split_merge_service.StudyTenantScopeError as exc:
        status_code = 409 if user.role == "ADMIN" else 404
        raise HTTPException(status_code, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        log.error("Study split failed error_type=%s", type(exc).__name__)
        raise HTTPException(500, "Split failed") from None


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
        study_uids = [*req.source_study_uids, req.target_study_uid]
        tenant_id, assignment_id = await split_merge_service.resolve_operation_tenant(
            db,
            study_uids,
            caller_tenant_id=user.tenant_id,
            is_admin=user.role == "ADMIN",
            requested_tenant_id=req.tenant_id,
        )
        target_study = await split_merge_service.merge_studies(
            db,
            source_study_uids=req.source_study_uids,
            target_study_uid=req.target_study_uid,
            performed_by=user.username,
            tenant_id=tenant_id,
            assign_unassigned_tenant_id=assignment_id,
            is_admin=user.role == "ADMIN",
        )
        await db.commit()
        return JSONResponse(
            {
                "message": "Studies merged successfully",
                "target_study_instance_uid": target_study.study_instance_uid,
            },
            status_code=200,
        )
    except split_merge_service.StudyTenantScopeError as exc:
        status_code = 409 if user.role == "ADMIN" else 404
        raise HTTPException(status_code, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        log.error("Study merge failed error_type=%s", type(exc).__name__)
        raise HTTPException(500, "Merge failed") from None


class StudyTagsPatchRequest(BaseModel):
    patient_id: str | None = Field(default=None, max_length=64)
    patient_name: str | None = Field(default=None, max_length=192)
    patient_birth_date: str | None = Field(default=None, max_length=8)
    patient_sex: str | None = Field(default=None, max_length=4)
    accession_number: str | None = Field(default=None, max_length=64)
    study_date: str | None = Field(default=None, max_length=8)
    study_description: str | None = Field(default=None, max_length=255)
    referring_physician: str | None = Field(default=None, max_length=192)


@router.get("/{study_uid}/tags")
async def get_study_tags(
    study_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return editable patient/study DICOM tags and series inventory."""
    await require_study_access(db, study_uid, user)
    study = await tag_editor_service.load_study_with_series(db, study_uid)
    if study is None:
        raise HTTPException(404, "Study not found")
    return tag_editor_service.serialize_study_tags(study)


@router.patch("/{study_uid}/tags", dependencies=[Depends(require_csrf)])
async def edit_study_tags(
    study_uid: str,
    req: StudyTagsPatchRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
) -> dict[str, Any]:
    """Edit patient/study DICOM tags in both PostgreSQL and stored DICOM files."""
    await require_study_access(db, study_uid, user)
    study = await tag_editor_service.load_study_with_series(db, study_uid)
    if study is None:
        raise HTTPException(404, "Study not found")
    patch = tag_editor_service.TagPatch(**req.model_dump())
    try:
        result = await tag_editor_service.apply_study_tag_edits(
            db, study, patch, performed_by=user.username
        )
        await db.commit()
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception as exc:
        log.error("study_tag_edit_failed error_type=%s", type(exc).__name__)
        raise HTTPException(500, "Failed to update DICOM tags") from None
