"""Radiation Dose Monitoring (RDSR) Service.

Parses DICOM Radiation Dose Structured Reports (SOP Class 1.2.840.10008.5.1.4.1.1.88.67),
extracts exposure metrics (DLP, CTDIvol, Dose RP, irradiation events), indexes them in DB,
and calculates patient cumulative dose metrics.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import pydicom
from pydicom.dataset import Dataset
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RadiationDoseReport
from app.services import audit_service, query_service

log = logging.getLogger("paxs.dose")

RDSR_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.88.67"


@dataclass
class ParsedRDSRContent:
    modality: str = "CT"
    total_dlp_mgy_cm: float | None = None
    total_ctdivol_mgy: float | None = None
    total_dose_rp_mgy: float | None = None
    irradiation_events_count: int = 0
    manufacturer: str | None = None
    device_model: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


def parse_rdsr(ds: Dataset) -> ParsedRDSRContent:
    """Parse a pydicom RDSR dataset to extract radiation dose exposure metrics."""
    content = ParsedRDSRContent()
    content.manufacturer = str(getattr(ds, "Manufacturer", "") or "") or None
    content.device_model = str(getattr(ds, "ManufacturerModelName", "") or "") or None
    content.modality = str(getattr(ds, "Modality", "CT") or "CT").upper()

    # Traverse Content Sequence (0040,A730)
    seq = getattr(ds, "ContentSequence", None)
    if not seq:
        return content

    def _inspect_seq(items):
        for item in items:
            val_type = str(getattr(item, "ValueType", "")).upper()
            concept_seq = getattr(item, "ConceptNameCodeSequence", None)
            code_val = ""
            if concept_seq and len(concept_seq) > 0:
                code_val = str(getattr(concept_seq[0], "CodeValue", ""))

            # DLP (Dose Length Product)
            if code_val in ("113813", "DLP") or "DOSE LENGTH PRODUCT" in str(getattr(item, "ConceptNameCodeSequence", "")).upper():
                num_seq = getattr(item, "MeasuredValueSequence", None)
                if num_seq and len(num_seq) > 0:
                    val = getattr(num_seq[0], "NumericValue", None)
                    if val is not None:
                        content.total_dlp_mgy_cm = float(val)

            # Mean CTDIvol
            elif code_val in ("113830", "CTDIVOL") or "CTDIVOL" in str(getattr(item, "ConceptNameCodeSequence", "")).upper():
                num_seq = getattr(item, "MeasuredValueSequence", None)
                if num_seq and len(num_seq) > 0:
                    val = getattr(num_seq[0], "NumericValue", None)
                    if val is not None:
                        content.total_ctdivol_mgy = float(val)

            # Dose (RP)
            elif code_val in ("113725", "DOSE_RP") or "DOSE (RP)" in str(getattr(item, "ConceptNameCodeSequence", "")).upper():
                num_seq = getattr(item, "MeasuredValueSequence", None)
                if num_seq and len(num_seq) > 0:
                    val = getattr(num_seq[0], "NumericValue", None)
                    if val is not None:
                        content.total_dose_rp_mgy = float(val)

            # Irradiation Event
            elif code_val in ("113706", "EVENT") or "IRRADIATION EVENT" in str(getattr(item, "ConceptNameCodeSequence", "")).upper():
                content.irradiation_events_count += 1

            # Recurse child content sequence
            child_seq = getattr(item, "ContentSequence", None)
            if child_seq:
                _inspect_seq(child_seq)

    _inspect_seq(seq)
    return content


async def index_dose_report(
    session: AsyncSession,
    meta: dict,
    ds: Dataset,
    file_path: str,
) -> RadiationDoseReport | None:
    """Index an incoming RDSR dataset into the database."""
    sop_uid = meta.get("sop_instance_uid") or ""
    if not sop_uid:
        return None

    content = parse_rdsr(ds)
    patient_id = meta.get("patient_id") or str(getattr(ds, "PatientID", "UNKNOWN"))
    study_uid = meta.get("study_instance_uid") or ""

    existing = (
        await session.execute(
            select(RadiationDoseReport).where(RadiationDoseReport.sop_instance_uid == sop_uid)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.study_instance_uid != study_uid:
        raise ValueError("RDSR SOP UID belongs to another study")

    payload_json = json.dumps(
        {
            "dlp_mgy_cm": content.total_dlp_mgy_cm,
            "ctdivol_mgy": content.total_ctdivol_mgy,
            "dose_rp_mgy": content.total_dose_rp_mgy,
            "events_count": content.irradiation_events_count,
        }
    )

    if existing is not None:
        existing.file_path = file_path
        existing.total_dlp_mgy_cm = content.total_dlp_mgy_cm
        existing.total_ctdivol_mgy = content.total_ctdivol_mgy
        existing.total_dose_rp_mgy = content.total_dose_rp_mgy
        existing.irradiation_events_count = content.irradiation_events_count
        existing.raw_payload_json = payload_json
        await session.flush()
        return existing

    rdsr = RadiationDoseReport(
        sop_instance_uid=sop_uid,
        study_instance_uid=study_uid,
        series_instance_uid=meta.get("series_instance_uid") or "",
        patient_id=patient_id,
        modality=content.modality,
        total_dlp_mgy_cm=content.total_dlp_mgy_cm,
        total_ctdivol_mgy=content.total_ctdivol_mgy,
        total_dose_rp_mgy=content.total_dose_rp_mgy,
        irradiation_events_count=content.irradiation_events_count,
        manufacturer=content.manufacturer,
        device_model=content.device_model,
        raw_payload_json=payload_json,
        file_path=file_path,
    )
    session.add(rdsr)
    await session.flush()

    audit_service.add_audit(
        session,
        sop_instance_uid=sop_uid,
        study_instance_uid=rdsr.study_instance_uid,
        action="DOSE_REPORT_INDEXED",
        performed_by="dose-monitoring-service",
    )

    log.info("Radiation Dose Report indexed for study %s (DLP: %s)", rdsr.study_instance_uid, content.total_dlp_mgy_cm)
    return rdsr


async def get_dose_for_study(
    session: AsyncSession,
    study_instance_uid: str,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[RadiationDoseReport]:
    """Get all radiation dose reports for a study (B-9: tenant-scoped).

    ``radiation_dose_reports`` carries no tenant column, so the predicate is
    applied to the joined study; another institution's dose reports are not
    returned and the router turns that into 404.
    """
    stmt = query_service.dose_report_query(
        study_uid=study_instance_uid, tenant_id=tenant_id, include_all=include_all
    ).order_by(RadiationDoseReport.id.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_dose_report_by_sop(
    session: AsyncSession,
    sop_instance_uid: str,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> RadiationDoseReport | None:
    """One RDSR by its own SOPInstanceUID, or None (B-9 scoped).

    None covers both "no such report" and "another tenant's report" — the router
    must not distinguish them, since a 403 would confirm the object exists.
    """
    stmt = query_service.dose_report_query(
        sop_uid=sop_instance_uid, tenant_id=tenant_id, include_all=include_all
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_patient_cumulative_dose(
    session: AsyncSession,
    patient_id: str,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> dict[str, Any]:
    """Cumulative lifetime radiation dose metrics for a patient (B-9: scoped).

    Patient identifiers are unique per tenant, not globally (A-8), so the
    aggregate is only meaningful — and only safe — within the caller's tenant.
    A foreign patient simply has no reports in scope, which reports as zeros.
    """
    stmt = query_service.dose_aggregate_query(
        [
            func.count(RadiationDoseReport.id),
            func.sum(RadiationDoseReport.total_dlp_mgy_cm),
            func.sum(RadiationDoseReport.total_ctdivol_mgy),
            func.sum(RadiationDoseReport.total_dose_rp_mgy),
            func.sum(RadiationDoseReport.irradiation_events_count),
        ],
        tenant_id=tenant_id,
        include_all=include_all,
    ).where(RadiationDoseReport.patient_id == patient_id)

    res = (await session.execute(stmt)).one()

    # MySQL/MariaDB return DECIMAL for SUM/AVG — coerce to plain float/int so
    # the JSON response is serializable on every backend.
    return {
        "patient_id": patient_id,
        "total_reports_count": int(res[0] or 0),
        "cumulative_dlp_mgy_cm": round(float(res[1] or 0.0), 2),
        "cumulative_ctdivol_mgy": round(float(res[2] or 0.0), 2),
        "cumulative_dose_rp_mgy": round(float(res[3] or 0.0), 2),
        "total_irradiation_events": int(res[4] or 0),
    }


async def get_dose_analytics(
    session: AsyncSession,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> dict[str, Any]:
    """Radiation dose analytics across the caller's scope (B-9).

    Archive-wide for an ADMIN (``include_all``); otherwise the caller's tenant
    only, so one institution's dose profile is not disclosed to another.
    """
    stmt = query_service.dose_aggregate_query(
        [
            func.count(RadiationDoseReport.id),
            func.avg(RadiationDoseReport.total_dlp_mgy_cm),
            func.max(RadiationDoseReport.total_dlp_mgy_cm),
            func.avg(RadiationDoseReport.total_ctdivol_mgy),
        ],
        tenant_id=tenant_id,
        include_all=include_all,
    )
    res = (await session.execute(stmt)).one()

    return {
        "total_rdsr_reports": int(res[0] or 0),
        "average_dlp_mgy_cm": round(float(res[1] or 0.0), 2),
        "max_dlp_mgy_cm": round(float(res[2] or 0.0), 2),
        "average_ctdivol_mgy": round(float(res[3] or 0.0), 2),
    }
