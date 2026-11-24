"""Experimental HU-threshold measurements and DICOM SR generation.

The bundled inference kernels are deterministic threshold heuristics, not
validated diagnostic models. Their output must remain labeled as experimental
and requires clinician review before any clinical interpretation.
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.concurrency import run_blocking
from app.db.models import Instance, Series, Study
from app.services import ai_cache as _ai_cache
from app.services import query_service, storage_service, volume_service

log = logging.getLogger("paxs.ai")

# Retain the cache functions on this service facade for its existing callers.
_AI_CACHE = _ai_cache._AI_CACHE
_cache_inference = _ai_cache.cache_inference
_get_cached_inference = _ai_cache.get_cached_inference
ai_cache_stats = _ai_cache.ai_cache_stats
clear_ai_cache = _ai_cache.clear_ai_cache
get_cached_mask = _ai_cache.get_cached_mask

_SUPPORTED_HEURISTIC_TYPES = frozenset({
    "brain_hemorrhage", "lung_nodule", "bone_segmentation", "organ_volumetry",
})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _scope_key(tenant_id: int | None, include_all: bool) -> str:
    """Cache-key segment naming the caller's scope (C-6 parity).

    Two tenants may legitimately hold the same SeriesInstanceUID (the pre-VNA /
    merged-archive case C-6 already handles for the volume cache). Keying a
    process-global cache by UID alone answered a second tenant's request out of
    the first tenant's entry **without touching the database at all**, which
    silently bypassed the tenant scope every lookup in `run_inference` applies.
    """
    if include_all:
        return "all"
    if tenant_id is not None:
        return f"tenant:{tenant_id}"
    return "legacy"


def _inference_cache_key(tenant_id: int | None, include_all: bool, series_uid: str,
                         model_type: str, confidence_threshold: float) -> str:
    return f"{_scope_key(tenant_id, include_all)}:{series_uid}:{model_type}:{confidence_threshold}"


# --------------------------------------------------------------------------- #
# AI Volumetric Inference Engine
# --------------------------------------------------------------------------- #

async def run_inference(
    db: AsyncSession,
    series_uid: str,
    model_type: str = "brain_hemorrhage",
    confidence_threshold: float = 0.5,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> Dict[str, Any]:
    """Runs an experimental HU-threshold heuristic on a volumetric series.

    B-9: ``tenant_id``/``include_all`` are the caller's scope and are passed to
    every series lookup below. They default to the *scoped* behaviour (legacy
    NULL bucket), so a caller that forgets them cannot read another tenant's
    pixels — before this, the slice lookup was unscoped and any authenticated
    caller could run inference over (and read statistics from) any series.

    Supported identifiers select fixed threshold kernels; they are not
    validated diagnostic models and do not use ``confidence_threshold``.
    """
    _validate_model_type(model_type)
    cache_key = _inference_cache_key(
        tenant_id, include_all, series_uid, model_type, confidence_threshold
    )
    cached = _get_cached_inference(
        cache_key,
        tenant_id=tenant_id,
        include_all=include_all,
        series_uid=series_uid,
    )
    if cached is not None:
        return cached

    t0 = time.perf_counter()

    # 1. Load the volume inside the caller's scope (B-9).
    volume = await _load_and_validate_series_hu(
        db, series_uid, tenant_id=tenant_id, include_all=include_all
    )

    return await run_blocking(
        _compute_and_cache_inference, volume, series_uid, model_type,
        cache_key, tenant_id, include_all, t0,
    )


def _compute_and_cache_inference(
    volume: _SeriesVolume, series_uid: str, model_type: str, cache_key: str,
    tenant_id: int | None, include_all: bool, t0: float,
) -> Dict[str, Any]:
    mask = np.zeros((volume.slices, volume.rows, volume.cols), dtype=np.uint8)
    findings, summary_text, severity = _compute_wait_and_triage_metrics(
        model_type, volume, mask
    )

    result = _build_inference_result(
        series_uid=series_uid,
        model_type=model_type,
        volume=volume,
        mask=mask,
        findings=findings,
        summary_text=summary_text,
        severity=severity,
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )

    _cache_inference(cache_key, tenant_id, include_all, series_uid, result)

    # Return clean JSON-serializable dictionary (excluding raw numpy array)
    return {k: v for k, v in result.items() if not k.startswith("_")}


def _validate_model_type(model_type: str) -> None:
    """Reject unknown identifiers instead of silently running another kernel."""
    if model_type not in _SUPPORTED_HEURISTIC_TYPES:
        raise ValueError(f"Unsupported heuristic model_type: {model_type}")


@dataclass
class _SeriesVolume:
    """A decoded series plus the geometry every kernel measures against.

    ``voxel_vol_cm3`` is per-voxel volume in cm³ (mm³ / 1000): a kernel that
    multiplies a voxel count by it gets a physical volume without re-deriving
    the spacing, which is how the four models drifted apart before.
    """
    array: np.ndarray
    slices: int
    rows: int
    cols: int
    voxel_vol_cm3: float


async def _load_and_validate_series_hu(
    db: AsyncSession,
    series_uid: str,
    *,
    tenant_id: int | None,
    include_all: bool,
) -> _SeriesVolume:
    """Fetch, decode and validate the series' voxel block (B-9 scoped).

    Every failure is a ``ValueError`` naming the series: the router turns that
    into a 422, and the message must not contain pixel data or demographics
    (N-2) — a UID is the only identifier that appears here.
    """
    slice_list = await volume_service.series_slices(
        db, series_uid, tenant_id=tenant_id, include_all=include_all
    )
    if not slice_list:
        raise ValueError(f"Series {series_uid} is not volumetric or has no instances")

    info = await volume_service.volume_info(
        db, series_uid, tenant_id=tenant_id, include_all=include_all
    )
    slices = len(slice_list)
    rows = info.get("rows", 512) if info else 512
    cols = info.get("cols", 512) if info else 512
    spacing = (info.get("pixel_spacing") or [1.0, 1.0]) if info else [1.0, 1.0]
    slice_spacing = (info.get("spacing_along_axis") or info.get("slice_thickness") or 1.0) if info else 1.0

    voxel_vol_cm3 = (spacing[0] * spacing[1] * slice_spacing) / 1000.0  # mm3 to cm3

    vol_np = await run_blocking(volume_service.load_volume_array, slice_list)
    if vol_np is None or vol_np.size == 0:
        raise ValueError(f"Failed to assemble volume array for series {series_uid}")
    return _SeriesVolume(
        array=vol_np, slices=slices, rows=rows, cols=cols,
        voxel_vol_cm3=voxel_vol_cm3,
    )


def _compute_wait_and_triage_metrics(
    model_type: str, volume: _SeriesVolume, mask: np.ndarray
) -> tuple[List[Dict[str, Any]], str, str]:
    """Run one model kernel; returns ``(findings, summary, severity)``.

    Unknown identifiers fail closed; routing one to organ volumetry could
    produce a plausible but unrelated clinical-looking result.
    """
    _validate_model_type(model_type)
    kernels = {
        "brain_hemorrhage": _segment_brain_hemorrhage,
        "lung_nodule": _segment_lung_nodule,
        "bone_segmentation": _segment_bone,
        "organ_volumetry": _segment_organ,
    }
    return kernels[model_type](volume, mask)


def _segment_brain_hemorrhage(
    volume: _SeriesVolume, mask: np.ndarray
) -> tuple[List[Dict[str, Any]], str, str]:
    """Acute blood collections: 55 HU to 95 HU."""
    vol_np = volume.array
    blood_voxels = (vol_np >= 55.0) & (vol_np <= 95.0)
    total_blood_voxels = int(np.count_nonzero(blood_voxels))
    hematoma_vol_cm3 = total_blood_voxels * volume.voxel_vol_cm3

    if total_blood_voxels <= 50:
        return [], (
            "No acute intracranial hemorrhage or extra-axial collection identified. "
            "Parenchymal density within normal limits."
        ), "NORMAL"

    mask[blood_voxels] = 1
    mean_hu = float(np.mean(vol_np[blood_voxels]))
    max_hu = float(np.max(vol_np[blood_voxels]))
    std_hu = float(np.std(vol_np[blood_voxels]))
    confidence = min(0.99, 0.75 + (hematoma_vol_cm3 / 50.0) * 0.2)
    severity = "CRITICAL" if hematoma_vol_cm3 > 10.0 else "WARNING"

    z_indices, y_indices, x_indices = np.where(blood_voxels)
    cz = int(np.median(z_indices))
    cy = int(np.median(y_indices))
    cx = int(np.median(x_indices))

    findings = [{
        "id": "ICH-01",
        "finding_name": "Intracranial Hemorrhage / Acute Hematoma",
        "location": f"Axial Slice {cz + 1} (Z={cz})",
        "center_voxel": [cx, cy, cz],
        "volume_cm3": round(hematoma_vol_cm3, 2),
        "volume_ml": round(hematoma_vol_cm3, 2),
        "mean_hu": round(mean_hu, 1),
        "max_hu": round(max_hu, 1),
        "std_hu": round(std_hu, 1),
        "confidence": round(confidence, 3),
        "severity": severity,
        "radlex_code": "RID13164",  # Intracranial hemorrhage
        "snomed_code": "1386000",
    }]
    summary = (
        "Hyperdense acute hemorrhagic focus identified with estimated volume of "
        f"{round(hematoma_vol_cm3, 2)} cm³ (Mean {round(mean_hu, 1)} HU)."
    )
    return findings, summary, severity


def _segment_lung_nodule(
    volume: _SeriesVolume, mask: np.ndarray
) -> tuple[List[Dict[str, Any]], str, str]:
    """Solid & subsolid nodules: -400 HU to +100 HU inside thoracic bounds."""
    vol_np = volume.array
    nodule_voxels = (vol_np >= -400.0) & (vol_np <= 100.0)
    total_nodule = int(np.count_nonzero(nodule_voxels))
    nodule_vol_cm3 = total_nodule * volume.voxel_vol_cm3

    if total_nodule <= 30:
        return [], (
            "Lungs are clear without focal consolidation, pneumothorax, or dominant "
            "pulmonary nodule."
        ), "NORMAL"

    mask[nodule_voxels] = 1
    mean_hu = float(np.mean(vol_np[nodule_voxels]))
    confidence = 0.88
    severity = "WARNING" if nodule_vol_cm3 > 1.5 else "LOW"

    z_indices, y_indices, x_indices = np.where(nodule_voxels)
    cz = int(np.median(z_indices))
    cy = int(np.median(y_indices))
    cx = int(np.median(x_indices))

    findings = [{
        "id": "LUNG-01",
        "finding_name": "Pulmonary Lesion / Parenchymal Density",
        "location": f"Slice {cz + 1}",
        "center_voxel": [cx, cy, cz],
        "volume_cm3": round(nodule_vol_cm3, 2),
        "volume_ml": round(nodule_vol_cm3, 2),
        "mean_hu": round(mean_hu, 1),
        "max_hu": round(float(np.max(vol_np[nodule_voxels])), 1),
        "std_hu": round(float(np.std(vol_np[nodule_voxels])), 1),
        "confidence": confidence,
        "severity": severity,
        "radlex_code": "RID28473",
        "snomed_code": "26696008",
    }]
    summary = (
        "Pulmonary parenchymal density identified "
        f"({round(nodule_vol_cm3, 2)} cm³). Fleischner follow-up recommended."
    )
    return findings, summary, severity


def _segment_bone(
    volume: _SeriesVolume, mask: np.ndarray
) -> tuple[List[Dict[str, Any]], str, str]:
    """Cortical bone: > 300 HU."""
    vol_np = volume.array
    bone_voxels = vol_np >= 300.0
    total_bone = int(np.count_nonzero(bone_voxels))
    bone_vol_cm3 = total_bone * volume.voxel_vol_cm3
    mask[bone_voxels] = 1

    mean_hu = float(np.mean(vol_np[bone_voxels])) if total_bone > 0 else 0.0
    findings = [{
        "id": "BONE-01",
        "finding_name": "Skeletal & Cortical Bone Segmentation",
        "location": "Whole Volume",
        "center_voxel": [volume.cols // 2, volume.rows // 2, volume.slices // 2],
        "volume_cm3": round(bone_vol_cm3, 2),
        "volume_ml": round(bone_vol_cm3, 2),
        "mean_hu": round(mean_hu, 1),
        "max_hu": round(float(np.max(vol_np[bone_voxels])), 1) if total_bone > 0 else 0.0,
        "std_hu": round(float(np.std(vol_np[bone_voxels])), 1) if total_bone > 0 else 0.0,
        "confidence": 0.96,
        "severity": "NORMAL",
        "radlex_code": "RID28549",
        "snomed_code": "272673000",
    }]
    summary = (
        "Complete 3D osseous segmentation performed. Skeletal volume: "
        f"{round(bone_vol_cm3, 2)} cm³ (BMD Mean {round(mean_hu, 1)} HU)."
    )
    return findings, summary, "NORMAL"


def _segment_organ(
    volume: _SeriesVolume, mask: np.ndarray
) -> tuple[List[Dict[str, Any]], str, str]:
    """Experimental soft-tissue threshold: 30 HU to 120 HU."""
    vol_np = volume.array
    organ_voxels = (vol_np >= 30.0) & (vol_np <= 120.0)
    total_organ = int(np.count_nonzero(organ_voxels))
    organ_vol_cm3 = total_organ * volume.voxel_vol_cm3
    mask[organ_voxels] = 1

    mean_hu = float(np.mean(vol_np[organ_voxels])) if total_organ > 0 else 0.0
    findings = [{
        "id": "ORGAN-01",
        "finding_name": "Soft Tissue & Parenchymal Volumetry",
        "location": "Parenchymal Body",
        "center_voxel": [volume.cols // 2, volume.rows // 2, volume.slices // 2],
        "volume_cm3": round(organ_vol_cm3, 2),
        "volume_ml": round(organ_vol_cm3, 2),
        "mean_hu": round(mean_hu, 1),
        "max_hu": round(float(np.max(vol_np[organ_voxels])), 1) if total_organ > 0 else 0.0,
        "std_hu": round(float(np.std(vol_np[organ_voxels])), 1) if total_organ > 0 else 0.0,
        "confidence": 0.92,
        "severity": "NORMAL",
        "radlex_code": "RID49534",
        "snomed_code": "399384004",
    }]
    summary = f"Parenchymal organ volumetry computed: {round(organ_vol_cm3, 2)} cm³."
    return findings, summary, "NORMAL"


def _build_inference_result(
    *,
    series_uid: str,
    model_type: str,
    volume: _SeriesVolume,
    mask: np.ndarray,
    findings: List[Dict[str, Any]],
    summary_text: str,
    severity: str,
    elapsed_ms: float,
) -> Dict[str, Any]:
    """Assemble the cached result, including the private ``_mask_array``.

    The mask is stored under a leading underscore so the JSON response can drop
    it with one rule (``startswith("_")``) while ``get_cached_mask`` still finds
    it in the cache.
    """
    return {
        "series_instance_uid": series_uid,
        "model_type": model_type,
        "algorithm_method": "deterministic_hu_threshold",
        "algorithm_version": "paxs-hu-heuristics-v1",
        "validated_for_diagnosis": False,
        "confidence_is_calibrated": False,
        "severity_is_clinically_validated": False,
        "requires_clinician_review": True,
        "clinical_use_warning": (
            "Experimental quantitative heuristic only; not validated for diagnosis or triage. "
            "A qualified clinician must review the source images and all findings."
        ),
        "slices": volume.slices,
        "rows": volume.rows,
        "cols": volume.cols,
        "findings": findings,
        "summary": f"Experimental HU-threshold estimate only — not diagnostic: {summary_text}",
        "overall_severity": severity,
        "inference_time_ms": round(elapsed_ms, 2),
        "mask_bytes_length": mask.nbytes,
        "mask_voxels_count": int(np.count_nonzero(mask)),
        "mask_url": f"/api/v1/ai/mask/{series_uid}",
        "timestamp": _now().isoformat(),
        "_mask_array": mask,  # kept in cache for mask endpoint
    }


# --------------------------------------------------------------------------- #
# DICOM Structured Report (SR TID 1500) Generator
# --------------------------------------------------------------------------- #

async def generate_dicom_sr(
    db: AsyncSession,
    study_uid: str,
    series_uid: str,
    findings: List[Dict[str, Any]],
    summary: str,
    performed_by: str = "PAXS AI Diagnostic Copilot",
    tenant_id: int | None = None,
    include_all: bool = False,
) -> Dict[str, Any]:
    """Create an unverified heuristic measurement SR within the caller's scope."""
    import pydicom
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.sequence import Sequence
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    # 1. Fetch study and patient details
    stmt = (
        select(Study)
        .options(selectinload(Study.patient))
        .where(Study.study_instance_uid == study_uid)
    )
    stmt = query_service.apply_study_tenant_scope(stmt, tenant_id, include_all)
    study = (await db.execute(stmt)).scalars().first()
    if not study:
        raise LookupError("Study not found")

    source_series = await db.scalar(select(Series.id).where(
        Series.study_id == study.id,
        Series.series_instance_uid == series_uid,
    ))
    if source_series is None:
        raise LookupError("Series not found in study")

    sr_sop_instance_uid = generate_uid()
    sr_series_instance_uid = generate_uid()

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.88.33"  # Comprehensive 3D SR
    file_meta.MediaStorageSOPInstanceUID = sr_sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = "1.2.826.0.1.3680043.9.7433.1.1"
    file_meta.ImplementationVersionName = "PAXS_AI_SR_v1"

    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\x00" * 128)

    # Patient & Study demographics
    p_name = study.patient.patient_name if study.patient else "ANONYMOUS"
    p_id = study.patient.patient_id if study.patient else "UNKNOWN"
    p_dob = study.patient.patient_birth_date if study.patient else ""
    p_sex = study.patient.patient_sex if study.patient else "O"

    ds.PatientName = p_name or "ANONYMOUS"
    ds.PatientID = p_id or "UNKNOWN"
    ds.PatientBirthDate = p_dob or ""
    ds.PatientSex = p_sex or "O"
    ds.StudyInstanceUID = study_uid
    ds.StudyDate = study.study_date or datetime.now().strftime("%Y%m%d")
    ds.StudyTime = study.study_time or datetime.now().strftime("%H%M%S")
    ds.AccessionNumber = study.accession_number or ""
    ds.StudyDescription = study.study_description or "PAXS Experimental Heuristic Report"

    # SR Series specifics
    ds.Modality = "SR"
    ds.SeriesInstanceUID = sr_series_instance_uid
    ds.SeriesNumber = 9001
    ds.SeriesDescription = "PAXS Experimental Heuristic SR - UNVERIFIED"
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.88.33"
    ds.SOPInstanceUID = sr_sop_instance_uid
    ds.InstanceNumber = 1
    ds.ContentDate = datetime.now().strftime("%Y%m%d")
    ds.ContentTime = datetime.now().strftime("%H%M%S")
    ds.CompletionFlag = "COMPLETE"
    ds.VerificationFlag = "UNVERIFIED"
    ds.ContentDescription = "Experimental threshold measurements; clinician review required"

    def _create_code_item(val: str, scheme: str, meaning: str) -> Dataset:
        d = Dataset()
        d.CodeValue = val
        d.CodingSchemeDesignator = scheme
        d.CodeMeaning = meaning
        return d

    # Document Title (TID 1500 Measurement Report)
    ds.ValueType = "CONTAINER"
    ds.ContinuityOfContent = "SEPARATE"
    ds.ConceptNameCodeSequence = Sequence([
        _create_code_item("126000", "DCM", "Imaging Measurement Report")
    ])

    # Structured Content Items
    content_seq = []

    # Summary Text Item
    summary_item = Dataset()
    summary_item.RelationshipType = "CONTAINS"
    summary_item.ValueType = "TEXT"
    summary_item.ConceptNameCodeSequence = Sequence([
        _create_code_item("121070", "DCM", "Findings")
    ])
    summary_item.TextValue = summary
    content_seq.append(summary_item)

    warning_item = Dataset()
    warning_item.RelationshipType = "CONTAINS"
    warning_item.ValueType = "TEXT"
    warning_item.ConceptNameCodeSequence = Sequence([
        _create_code_item("121106", "DCM", "Comment")
    ])
    warning_item.TextValue = (
        f"Source SeriesInstanceUID: {series_uid}. "
        "EXPERIMENTAL, UNVERIFIED OUTPUT. These deterministic HU-threshold "
        "measurements are not validated for diagnosis or triage. A qualified "
        "clinician must review the source images and verify all content before "
        "clinical use."
    )
    content_seq.append(warning_item)

    # Quantitative Findings (TID 1500 Numeric Items)
    for f in findings:
        meas_item = Dataset()
        meas_item.RelationshipType = "CONTAINS"
        meas_item.ValueType = "NUM"
        meas_item.ConceptNameCodeSequence = Sequence([
            _create_code_item("121206", "DCM", f.get("finding_name", "Volume"))
        ])

        mval = Dataset()
        mval.NumericValue = str(f.get("volume_cm3", 0.0))
        mval.MeasurementUnitsCodeSequence = Sequence([
            _create_code_item("cm3", "UCUM", "cubic centimeter")
        ])
        meas_item.MeasuredValueSequence = Sequence([mval])
        content_seq.append(meas_item)

    ds.ContentSequence = Sequence(content_seq)

    # Serialize before staging; no bytes are visible at the canonical key yet.
    buffer = io.BytesIO()
    ds.save_as(buffer, enforce_file_format=True)
    sr_bytes = buffer.getvalue()
    rel_path = storage_service.instance_key(study_uid, sr_series_instance_uid, sr_sop_instance_uid)
    staged = await run_blocking(
        storage_service.stage_bytes,
        rel_path,
        sr_bytes,
        tenant_id=study.tenant_id,
        backend_name=study.storage_backend,
        require_isolated=True,
    )
    new_series = Series(
        study_id=study.id,
        series_instance_uid=sr_series_instance_uid,
        modality="SR",
        series_number=9001,
        series_description="PAXS AI Structured Report (TID 1500)",
    )
    new_inst: Instance | None = None
    committed = False
    try:
        await run_blocking(staged.verify)
        db.add(new_series)
        await db.flush()
        new_inst = Instance(
            series_id=new_series.id,
            sop_instance_uid=sr_sop_instance_uid,
            sop_class_uid=ds.SOPClassUID,
            instance_number=1,
            file_path=rel_path,
            storage_backend=study.storage_backend,
            file_size_bytes=len(sr_bytes),
        )
        db.add(new_inst)
        await db.commit()
        committed = True
        await run_blocking(staged.promote)
    except Exception as exc:
        await db.rollback()
        if committed:
            try:
                assert new_inst is not None
                await db.delete(new_inst)
                await db.delete(new_series)
                await db.commit()
            except Exception as cleanup_exc:  # compensation must preserve the publish error
                await db.rollback()
                log.error("AI SR metadata compensation failed error_type=%s", type(cleanup_exc).__name__)
        await run_blocking(staged.discard)
        log.error("AI SR registration failed error_type=%s", type(exc).__name__)
        raise

    return {
        "status": "SUCCESS",
        "sop_instance_uid": sr_sop_instance_uid,
        "series_instance_uid": sr_series_instance_uid,
        "source_series_instance_uid": series_uid,
        "modality": "SR",
        "file_size": len(sr_bytes),
        "created_at": _now().isoformat(),
    }


# --------------------------------------------------------------------------- #
# HL7 ORU^R01 AI Diagnostic Dispatcher
# --------------------------------------------------------------------------- #

async def dispatch_ai_hl7_report(
    db: AsyncSession,
    study_uid: str,
    findings: List[Dict[str, Any]],
    summary: str,
    performed_by: str = "PAXS AI Diagnostic Engine",
) -> Dict[str, Any]:
    """Reject automatic ORU distribution until authenticated review exists.

    The bundled inference kernels are unvalidated HU heuristics. This legacy
    internal entry point has no clinician identity or review attestation, so it
    must not create, finalize, or distribute a clinical report.
    """
    raise ValueError(
        "Experimental heuristic findings require clinician review before "
        "report finalization or ORU distribution"
    )
