"""PAXS Clinical AI & Diagnostic Intelligence Endpoints."""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_admin
from app.core.licensing import LicenseManager, get_license_manager
from app.db.models import User
from app.db.session import get_db
from app.services import ai_service

router = APIRouter(prefix="/api/v1/ai", tags=["Clinical AI"])


class InferenceRequest(BaseModel):
    series_instance_uid: str = Field(..., description="Target volumetric series UID to analyze")
    model_type: str = Field("brain_hemorrhage", description="Diagnostic AI model type (e.g., brain_hemorrhage, lung_nodule, bone_segmentation, organ_volumetry)")
    confidence_threshold: float = Field(0.5, ge=0.1, le=0.99, description="Confidence detection cutoff")


class GenerateSRRequest(BaseModel):
    study_instance_uid: str = Field(..., description="Study UID to attach the DICOM SR to")
    series_instance_uid: str = Field(..., description="Analyzed series UID")
    findings: List[Dict[str, Any]] = Field(default_factory=list, description="List of quantitative findings")
    summary: str = Field(..., description="Clinical diagnostic impression text")


@router.get("/models")
async def get_available_ai_models(
    lic: LicenseManager = Depends(get_license_manager),
    _user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Returns the list of clinical AI models available under the active license."""
    all_models = [
        {
            "id": "brain_hemorrhage",
            "name": "Intracranial Hemorrhage Triage (ICH)",
            "modality": "CT",
            "feature_required": "ich_triage",
            "licensed": lic.has_feature("ich_triage") or lic.has_feature("ai_pipeline"),
            "description": "Automated acute hematoma detection & volumetric segmentation (>55 HU).",
        },
        {
            "id": "lung_nodule",
            "name": "Lung-RADS Nodule Volumetry",
            "modality": "CT",
            "feature_required": "lung_rads",
            "licensed": lic.has_feature("lung_rads") or lic.has_feature("ai_pipeline"),
            "description": "Pulmonary parenchymal nodule detection and 3D doubling volume measurement.",
        },
        {
            "id": "brain_seg",
            "name": "Brain Volumetry & MS Lesion Segmentation",
            "modality": "MRI",
            "feature_required": "brain_seg",
            "licensed": lic.has_feature("brain_seg") or lic.has_feature("ai_pipeline"),
            "description": "Multi-sequence volumetric brain segmentation and lesion burden tracking.",
        },
        {
            "id": "bone_segmentation",
            "name": "Skeletal & Spine Cortical Bone Segmentation",
            "modality": "CT",
            "feature_required": "bone_age_fracture",
            "licensed": lic.has_feature("bone_age_fracture") or lic.has_feature("cobb_angle") or lic.has_feature("ai_pipeline"),
            "description": "Automated 3D osseous contouring (>300 HU) and vertebral alignment.",
        },
        {
            "id": "organ_volumetry",
            "name": "Parenchymal Organ Volumetry",
            "modality": "CT / MR",
            "feature_required": "mpr_3d",
            "licensed": lic.has_feature("mpr_3d") or lic.has_feature("ai_pipeline"),
            "description": "Soft tissue parenchymal volume computation and organ contouring.",
        },
    ]

    return {
        "models": all_models,
        "ai_pipeline_enabled": lic.has_feature("ai_pipeline"),
        "total_models": len(all_models),
    }


@router.post("/infer")
async def run_ai_inference(
    req: InferenceRequest,
    db: AsyncSession = Depends(get_db),
    lic: LicenseManager = Depends(get_license_manager),
    _user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Runs automated clinical AI computer vision inference on a volumetric series."""
    if not lic.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Valid license required for clinical AI operations")

    try:
        result = await ai_service.run_inference(
            db=db,
            series_uid=req.series_instance_uid,
            model_type=req.model_type,
            confidence_threshold=req.confidence_threshold,
        )
        return {
            "success": True,
            "data": result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"AI inference error: {exc}")


@router.get("/mask/{series_instance_uid}")
async def get_segmentation_mask(
    series_instance_uid: str,
    _user: User = Depends(get_current_user),
) -> Response:
    """Returns raw binary 3D segmentation mask (Uint8Array) for 3D overlay rendering."""
    mask = ai_service.get_cached_mask(series_instance_uid)
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

    try:
        sr_result = await ai_service.generate_dicom_sr(
            db=db,
            study_uid=req.study_instance_uid,
            series_uid=req.series_instance_uid,
            findings=req.findings,
            summary=req.summary,
            performed_by=f"{user.full_name or user.username} via PAXS AI",
        )
        return {
            "success": True,
            "sr_instance": sr_result,
        }
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to generate DICOM SR: {exc}")
