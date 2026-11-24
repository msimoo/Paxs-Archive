"""Key Image Notes (KIN / KO) REST API.

  POST /api/v1/kin                        — Create/mark key image notes
  GET  /api/v1/kin/study/{study_uid}      — List KIN for a study
  GET  /api/v1/kin/instance/{sop}/notes   — Find KIN referencing an image
  GET  /api/v1/kin/{sop_uid}              — Get KIN details

B-9: every route is tenant-scoped. A key image note carries no tenant of its
own, so its study decides who may read it; a note belonging to another
institution answers **404** — never 403 (which would confirm it exists) and
never a 200 carrying another institution's annotations. All SQL lives in
``query_service``/``kin_service`` so this router holds no unscoped statement.
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
    require_destructive_role,
    require_study_access,
    tenant_scope,
)
from app.db.models import KeyImageNote, User
from app.db.session import get_db
from app.services import kin_service, query_service

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
    user: User = Depends(require_destructive_role),
):
    """Create an audited KIN document for an ADMIN or RADIOLOGIST."""
    if not req.referenced_sop_instance_uids:
        raise HTTPException(400, "referenced_sop_instance_uids list cannot be empty")

    # B-9 (write side): a note may only be attached to a study the caller can
    # see. Without this, tenant A could plant annotations on tenant B's study.
    await require_study_access(db, req.study_instance_uid, user)
    tenant_id, include_all = tenant_scope(user)

    try:
        kin = await kin_service.create_key_image_note(
            db,
            study_instance_uid=req.study_instance_uid,
            series_instance_uid=req.series_instance_uid,
            referenced_sop_uids=req.referenced_sop_instance_uids,
            notes=req.notes,
            title_code=req.title_concept_code,
            author_name=user.username,
            tenant_id=tenant_id,
            include_all=include_all,
        )
        await db.commit()
        return JSONResponse(_kin_to_dict(kin), status_code=201)
    except Exception as exc:
        # Notes and DICOM identifiers can occur in database errors; never echo those details.
        log.error("kin_create_failed error_type=%s", type(exc).__name__)
        raise HTTPException(500, "Failed to create Key Image Note") from None


@router.get("/study/{study_uid}", response_class=JSONResponse)
async def list_kin_for_study(
    study_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all Key Image Notes in a study (404 for another tenant's study)."""
    # B-9: an inaccessible study is 404 here rather than an empty 200, so the
    # response cannot be used to probe for another tenant's study.
    await require_study_access(db, study_uid, user)
    tenant_id, include_all = tenant_scope(user)
    results = await kin_service.list_kin_for_study(
        db, study_uid, tenant_id=tenant_id, include_all=include_all
    )
    return JSONResponse([_kin_to_dict(k) for k in results])


@router.get("/instance/{sop_uid}/notes", response_class=JSONResponse)
async def find_kin_for_instance(
    sop_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Find Key Image Notes that reference a specific image instance (B-9 scoped)."""
    owning = await query_service.owning_study_uid(db, sop_uid)
    if owning:
        await require_study_access(db, owning, user)
    tenant_id, include_all = tenant_scope(user)
    results = await kin_service.find_kin_for_instance(
        db, sop_uid, tenant_id=tenant_id, include_all=include_all
    )
    return JSONResponse([_kin_to_dict(k) for k in results])


@router.get("/{sop_uid}", response_class=JSONResponse)
async def get_key_image_note(
    sop_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get details of a specific Key Image Note by its SOPInstanceUID (B-9 scoped)."""
    tenant_id, include_all = tenant_scope(user)
    kin = await kin_service.get_kin_by_sop(
        db, sop_uid, tenant_id=tenant_id, include_all=include_all
    )
    if not kin:
        # Same message for "absent" and "another tenant's" — never disclose which.
        raise HTTPException(404, f"Key Image Note '{sop_uid}' not found")
    return JSONResponse(_kin_to_dict(kin))
