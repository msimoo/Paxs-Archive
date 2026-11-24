"""GSPS (Presentation State) REST API.

  GET  /api/v1/gsps/{sop_uid}                           — single GSPS by UID
  GET  /api/v1/gsps/study/{study_uid}                   — all GSPS in a study
  GET  /api/v1/gsps/instance/{sop_uid}/presentations    — GSPS referencing an image
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user_any, require_study_access, tenant_scope
from app.db.session import get_db
from app.services import gsps_service, query_service

log = logging.getLogger("paxs.api.gsps")

router = APIRouter(prefix="/api/v1/gsps", tags=["Presentation State (GSPS)"])


def _ps_to_dict(ps) -> dict:
    return {
        "id": ps.id,
        "sop_instance_uid": ps.sop_instance_uid,
        "study_instance_uid": ps.study_instance_uid,
        "series_instance_uid": ps.series_instance_uid,
        "presentation_label": ps.presentation_label,
        "presentation_description": ps.presentation_description,
        "presentation_creation_date": ps.presentation_creation_date,
        "presentation_creation_time": ps.presentation_creation_time,
        "presentation_creator": ps.presentation_creator,
        "window_center": ps.window_center,
        "window_width": ps.window_width,
        "rotation": ps.rotation,
        "horizontal_flip": ps.horizontal_flip,
        "referenced_sop_instance_uids": ps.referenced_sop_instance_uids,
        "file_path": ps.file_path,
    }


@router.get("/{sop_uid}")
async def get_gsps(sop_uid: str, db: AsyncSession = Depends(get_db),
                   _user=Depends(get_current_user_any)):
    """Retrieve a single GSPS by its SOP Instance UID."""
    tenant_id, include_all = tenant_scope(_user)
    ps = await gsps_service.get_by_uid(
        db, sop_uid, tenant_id=tenant_id, include_all=include_all
    )
    if ps is None:
        raise HTTPException(404, "Presentation state not found")
    # C04: a presentation state belongs to a study — check its tenant.
    return JSONResponse(_ps_to_dict(ps))


@router.get("/study/{study_uid}")
async def list_gsps_for_study(study_uid: str, db: AsyncSession = Depends(get_db),
                              _user=Depends(get_current_user_any)):
    """List all GSPS objects belonging to a study."""
    # C04
    await require_study_access(db, study_uid, _user)
    tenant_id, include_all = tenant_scope(_user)
    results = await gsps_service.list_for_study(
        db, study_uid, tenant_id=tenant_id, include_all=include_all
    )
    return JSONResponse([_ps_to_dict(ps) for ps in results])


@router.get("/instance/{sop_uid}/presentations")
async def find_presentations_for_instance(
    sop_uid: str,
    study_uid: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user_any),
):
    """Find all GSPS objects that reference a given image instance."""
    tenant_id, include_all = tenant_scope(_user)
    # C04: check the study when given, else resolve the instance's study.
    if study_uid:
        await require_study_access(db, study_uid, _user)
    else:
        owning = await query_service.owning_study_uid(db, sop_uid)
        if owning:
            await require_study_access(db, owning, _user)
    results = await gsps_service.find_for_instance(
        db, sop_uid, study_uid, tenant_id=tenant_id, include_all=include_all
    )
    return JSONResponse([_ps_to_dict(ps) for ps in results])
