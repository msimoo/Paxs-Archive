"""Fusion (roadmap #29) REST API — Frame-of-Reference aligned series pairs.

  GET /api/v1/fusion/{study_uid}

Returns the study's frames of reference, every same-FoR series pair (the
overlay matrix for PET/CT, MRI/US …), per-series volume geometry, and the
unregistered series. Auth matches the volume/DICOMweb routers: a valid
bearer token scopes to the caller's tenant (VNA); guests stay readable for
the same-origin viewer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_optional_user
from app.db.models import User
from app.db.session import get_db
from app.services import fusion_service, tenant_service

log = logging.getLogger("paxs.api.fusion")

router = APIRouter(prefix="/api/v1/fusion", tags=["Fusion"])


@router.get("/{study_uid}")
async def fusion_manifest(
    study_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """FoR groups + aligned series pairs + per-series geometry for a study."""
    tenant_id, include_all = tenant_service.scope_from_user(user)
    manifest = await fusion_service.study_frame_of_references(
        db, study_uid, tenant_id=tenant_id, include_all=include_all,
    )
    if manifest.get("not_found"):
        raise HTTPException(404, "Study not found")
    return manifest
