"""Radiation Dose Monitoring (RDSR) REST API.

  GET /api/v1/dose/study/{study_uid}     — Get radiation dose reports for a study
  GET /api/v1/dose/patient/{patient_id}  — Calculate cumulative lifetime dose for a patient
  GET /api/v1/dose/analytics             — Get aggregate dose statistics across the caller's scope
  GET /api/v1/dose/report/{sop_uid}      — Get specific RDSR report details

B-9: every route is tenant-scoped. A dose report carries no tenant of its own,
so its study decides who may read it; another institution's report answers
**404** (never 403, never a 200 carrying their dose metrics). The two aggregate
endpoints are scoped too — archive-wide only for an ADMIN — because per-patient
and per-archive dose profiles are clinical data. All SQL lives in
``query_service``/``dose_service``, so this router holds no unscoped statement.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_study_access, tenant_scope
from app.db.models import RadiationDoseReport, User
from app.db.session import get_db
from app.services import dose_service

log = logging.getLogger("paxs.api.dose")

router = APIRouter(prefix="/api/v1/dose", tags=["Radiation Dose Monitoring (RDSR)"])


def _rdsr_to_dict(r: RadiationDoseReport) -> dict[str, Any]:
    d = r.__dict__
    c_at = d.get("created_at")
    return {
        "id": d.get("id"),
        "sop_instance_uid": d.get("sop_instance_uid"),
        "study_instance_uid": d.get("study_instance_uid"),
        "series_instance_uid": d.get("series_instance_uid"),
        "patient_id": d.get("patient_id"),
        "modality": d.get("modality", "CT"),
        "total_dlp_mgy_cm": d.get("total_dlp_mgy_cm"),
        "total_ctdivol_mgy": d.get("total_ctdivol_mgy"),
        "total_dose_rp_mgy": d.get("total_dose_rp_mgy"),
        "irradiation_events_count": d.get("irradiation_events_count", 0),
        "manufacturer": d.get("manufacturer"),
        "device_model": d.get("device_model"),
        "raw_payload_json": d.get("raw_payload_json"),
        "file_path": d.get("file_path"),
        "created_at": c_at.isoformat() if hasattr(c_at, "isoformat") else (str(c_at) if c_at else None),
    }


@router.get("/study/{study_uid}", response_class=JSONResponse)
async def get_study_dose(
    study_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get all Radiation Dose Structured Reports for a study (404 for another tenant's study)."""
    # B-9: an inaccessible study is 404 here rather than an empty 200, so the
    # response cannot be used to probe for another tenant's study.
    await require_study_access(db, study_uid, user)
    tenant_id, include_all = tenant_scope(user)
    reports = await dose_service.get_dose_for_study(
        db, study_uid, tenant_id=tenant_id, include_all=include_all
    )
    return JSONResponse([_rdsr_to_dict(r) for r in reports])


@router.get("/patient/{patient_id}", response_class=JSONResponse)
async def get_patient_cumulative_dose(
    patient_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Cumulative lifetime radiation exposure for a patient within the caller's scope."""
    tenant_id, include_all = tenant_scope(user)
    summary = await dose_service.get_patient_cumulative_dose(
        db, patient_id, tenant_id=tenant_id, include_all=include_all
    )
    return JSONResponse(summary)


@router.get("/analytics", response_class=JSONResponse)
async def get_dose_analytics(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Dose statistics across the caller's scope (archive-wide only for an ADMIN)."""
    tenant_id, include_all = tenant_scope(user)
    analytics = await dose_service.get_dose_analytics(
        db, tenant_id=tenant_id, include_all=include_all
    )
    return JSONResponse(analytics)


@router.get("/report/{sop_uid}", response_class=JSONResponse)
async def get_dose_report(
    sop_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get details of a specific RDSR report by SOPInstanceUID (B-9 scoped)."""
    tenant_id, include_all = tenant_scope(user)
    report = await dose_service.get_dose_report_by_sop(
        db, sop_uid, tenant_id=tenant_id, include_all=include_all
    )
    if not report:
        # Same message for "absent" and "another tenant's" — never disclose which.
        raise HTTPException(404, f"Radiation Dose Report '{sop_uid}' not found")
    return JSONResponse(_rdsr_to_dict(report))
