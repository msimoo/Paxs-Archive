"""Experimental quantitative HU-threshold endpoints."""

from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_user,
    require_admin,
    require_series_access,
    require_study_access,
    tenant_scope,
)
from app.core.licensing import LicenseManager, get_license_manager
from app.db.models import User
from app.db.session import get_db
from app.services import ai_service

router = APIRouter(prefix="/api/v1/ai", tags=["Experimental Quantitative Heuristics"])
log = logging.getLogger("paxs.api.ai")


class InferenceRequest(BaseModel):
    series_instance_uid: str = Field(..., description="Target volumetric series UID to analyze")
    model_type: str = Field("brain_hemorrhage", description="Deterministic HU-threshold heuristic identifier; not a validated diagnostic model")
    confidence_threshold: float = Field(0.5, ge=0.1, le=0.99, description="Legacy parameter; current heuristics do not use or calibrate this threshold")


class GenerateSRRequest(BaseModel):
    study_instance_uid: str = Field(..., description="Study UID to attach the DICOM SR to")
    series_instance_uid: str = Field(..., description="Analyzed series UID")
    findings: List[Dict[str, Any]] = Field(default_factory=list, description="List of quantitative findings")
    summary: str = Field(..., description="Clinician-authored text; do not treat heuristic output as a diagnosis")


@router.get("/models")
async def get_available_ai_models(
    lic: LicenseManager = Depends(get_license_manager),
    _user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Returns licensed heuristic identifiers, not clinically validated models."""
    all_models = [
        {
            "id": "brain_hemorrhage",
            "name": "Intracranial HU Threshold Volume Estimate",
            "modality": "CT",
            "feature_required": "ich_triage",
            "licensed": lic.has_feature("ich_triage") or lic.has_feature("ai_pipeline"),
            "description": "Experimental deterministic HU threshold estimate (55–95 HU); not validated to detect or exclude hemorrhage.",
        },
        {
            "id": "lung_nodule",
            "name": "Lung HU Threshold Volume Estimate",
            "modality": "CT",
            "feature_required": "lung_rads",
            "licensed": lic.has_feature("lung_rads") or lic.has_feature("ai_pipeline"),
            "description": "Experimental threshold measurement (-400 to 100 HU); not a Lung-RADS assessment or nodule detector.",
        },
        {
            "id": "brain_seg",
            "name": "Brain Segmentation (Unavailable)",
            "modality": "MRI",
            "feature_required": "brain_seg",
            "licensed": lic.has_feature("brain_seg") or lic.has_feature("ai_pipeline"),
            "description": "No corresponding inference kernel is implemented; inference requests using this identifier are rejected.",
            "implemented": False,
        },
        {
            "id": "bone_segmentation",
            "name": "Bone HU Threshold Mask",
            "modality": "CT",
            "feature_required": "bone_age_fracture",
            "licensed": lic.has_feature("bone_age_fracture") or lic.has_feature("cobb_angle") or lic.has_feature("ai_pipeline"),
            "description": "Experimental voxel threshold mask (>300 HU); not validated for bone segmentation, fracture, or alignment assessment.",
        },
        {
            "id": "organ_volumetry",
            "name": "Soft Tissue HU Threshold Volume",
            "modality": "CT / MR",
            "feature_required": "mpr_3d",
            "licensed": lic.has_feature("mpr_3d") or lic.has_feature("ai_pipeline"),
            "description": "Experimental soft-tissue threshold measurement (30–120 HU); does not identify or contour a specific organ.",
        },
    ]

    for model in all_models:
        model.setdefault("implemented", True)
        model.update({
            "method": "deterministic HU threshold rules",
            "validated_for_diagnosis": False,
            "confidence_is_calibrated": False,
            "severity_is_clinically_validated": False,
            "requires_clinician_review": True,
        })

    return {
        "models": all_models,
        "ai_pipeline_enabled": lic.has_feature("ai_pipeline"),
        "clinical_validation_status": "not_validated_for_diagnosis",
        "notice": "These experimental quantitative heuristics are not diagnostic or triage systems. A qualified clinician must review the source images and any derived measurements.",
        "total_models": len(all_models),
    }


@router.post("/infer")
async def run_ai_inference(
    req: InferenceRequest,
    db: AsyncSession = Depends(get_db),
    lic: LicenseManager = Depends(get_license_manager),
    _user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Runs an experimental HU-threshold heuristic on a volumetric series."""
    # B-9: resolve series access *before* the licence gate. A series belonging to
    # another institution must answer 404 regardless of licence state, so the
    # refusal cannot be used to probe for it.
    await require_series_access(db, req.series_instance_uid, _user)
    if not lic.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Active license required for heuristic inference")

    tenant_id, include_all = tenant_scope(_user)
    try:
        result = await ai_service.run_inference(
            db=db,
            series_uid=req.series_instance_uid,
            model_type=req.model_type,
            confidence_threshold=req.confidence_threshold,
            tenant_id=tenant_id,
            include_all=include_all,
        )
        return {
            "success": True,
            "data": result,
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        # Broad catch is deliberate at the HTTP boundary; never return DB or
        # storage exception text, which can contain sensitive metadata/paths.
        log.error("AI inference failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="AI inference failed") from exc


@router.get("/mask/{series_instance_uid}")
async def get_segmentation_mask(
    series_instance_uid: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> Response:
    """Returns raw binary 3D segmentation mask (Uint8Array) for 3D overlay rendering."""
    # B-9: the mask cache is keyed by series UID alone, so access must be proven
    # before it is read — otherwise a foreign series UID would be served another
    # institution's segmentation mask.
    await require_series_access(db, series_instance_uid, _user)
    # ...and the mask is stored under the caller's scope, so a series UID that
    # two tenants both hold never resolves to the other tenant's voxels.
    mask_tenant_id, mask_include_all = tenant_scope(_user)
    mask = ai_service.get_cached_mask(
        series_instance_uid, tenant_id=mask_tenant_id, include_all=mask_include_all
    )
    if mask is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No cached segmentation mask for this series. Run /infer first.")

    return Response(
        content=mask.tobytes(),
        media_type="application/octet-stream",
        headers={
            "X-Mask-Shape": f"{mask.shape[0]},{mask.shape[1]},{mask.shape[2]}",
            "X-Mask-Dtype": str(mask.dtype),
        }
    )


@router.post("/sr")
async def generate_and_save_sr(
    req: GenerateSRRequest,
    db: AsyncSession = Depends(get_db),
    lic: LicenseManager = Depends(get_license_manager),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Generates and permanently archives a DICOM Structured Report (TID 1500)."""
    if not lic.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Active license required for DICOM SR creation")

    # B-9: a generated SR is archived *into* the named study, so the caller must
    # be able to see that study — otherwise tenant A could write an SR into
    # tenant B's study.
    await require_study_access(db, req.study_instance_uid, user)
    tenant_id, include_all = tenant_scope(user)

    try:
        sr_result = await ai_service.generate_dicom_sr(
            db=db,
            study_uid=req.study_instance_uid,
            series_uid=req.series_instance_uid,
            findings=req.findings,
            summary=req.summary,
            performed_by=f"{user.full_name or user.username} via PAXS AI",
            tenant_id=tenant_id,
            include_all=include_all,
        )
        return {
            "success": True,
            "sr_instance": sr_result,
        }
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study or series not found") from exc
    except Exception as exc:
        # Broad catch is deliberate at the HTTP boundary; exception strings
        # can include PHI or storage paths and must not reach the client.
        log.error("AI SR generation failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="DICOM SR generation failed") from exc
