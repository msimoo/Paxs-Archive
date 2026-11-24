"""Prior Study Linking REST API.

  GET /api/v1/priors/study/{study_uid} — Find ranked relevant prior studies for a current study
  GET /api/v1/priors/patient/{patient_id} — Find all prior studies for a patient
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_user,
    get_current_user_any,
    require_study_access,
    tenant_scope,
)
from app.db.models import User
from app.db.session import get_db
from app.services import prior_service

log = logging.getLogger("paxs.api.priors")

router = APIRouter(prefix="/api/v1/priors", tags=["Prior Study Linking"])


def _match_to_dict(m: prior_service.PriorStudyMatch) -> dict[str, Any]:
    return {
        "study_instance_uid": m.study_instance_uid,
        "patient_id": m.patient_id,
        "study_date": m.study_date,
        "study_time": m.study_time,
        "study_description": m.study_description,
        "modalities": m.modalities,
        "body_parts": m.body_parts,
        "relevance_score": m.relevance_score,
        "is_exact_modality_match": m.is_exact_modality_match,
        "is_exact_body_part_match": m.is_exact_body_part_match,
    }


@router.get("/study/{study_uid}", response_class=JSONResponse)
async def get_priors_for_study(
    study_uid: str,
    db: AsyncSession = Depends(get_db),
    # V-03: read by the viewer, which authenticates with the HttpOnly session
    # cookie — bearer-only `get_current_user` answered it 401 (AGENTS.md §7).
    user: User = Depends(get_current_user_any),
):
    """Find relevant prior imaging studies for a given study."""
    # Resolve through the tenant-scoped guard even for missing UIDs, so unknown
    # and foreign studies produce the same response and cannot be enumerated.
    await require_study_access(db, study_uid, user)
    _tenant_id, _include_all = tenant_scope(user)
    matches = await prior_service.find_prior_studies(
        db, study_instance_uid=study_uid,
        tenant_id=_tenant_id, include_all=_include_all,
    )
    return JSONResponse([_match_to_dict(m) for m in matches])


@router.get("/patient/{patient_id}", response_class=JSONResponse)
async def get_priors_for_patient(
    patient_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Find all prior imaging studies for a patient."""
    _tenant_id, _include_all = tenant_scope(user)
    matches = await prior_service.find_prior_studies(
        db, study_instance_uid="", patient_id=patient_id,
        tenant_id=_tenant_id, include_all=_include_all,
    )
    return JSONResponse([_match_to_dict(m) for m in matches])
