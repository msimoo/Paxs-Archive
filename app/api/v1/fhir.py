"""FHIR R4 read gateway (HL7/FHIR integration).

Exposes archive data as FHIR resources so external systems (EMR/HIS,
research portals) can consume PAXS without knowing DICOMweb. Read-only for
now: Patient and ImagingStudy, with minimal search (identifier/name/patient).

Response content type is `application/fhir+json`.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.v1.deps import get_current_user_any, require_study_access, tenant_scope
from app.core.config import resolve_public_base_url, settings
from app.db.models import Patient, Series, Study
from app.db.session import get_db
from app.services import query_service

router = APIRouter(prefix="/fhir", tags=["FHIR R4"])

FHIR_JSON = "application/fhir+json"
SERVER_BASE = resolve_public_base_url() + "/fhir"


def _fhir_response(data: dict, status: int = 200) -> Response:
    return Response(
        content=json.dumps(data),
        media_type=FHIR_JSON,
        status_code=status,
    )


def _patient_resource(p: Patient) -> dict:
    name: dict | None = None
    if p.patient_name:
        # DICOM name is typically "FAMILY^GIVEN^MIDDLE^SUFFIX".
        parts = p.patient_name.replace("^", " ").split()
        name = {"text": p.patient_name.replace("^", " ")}
        if parts:
            name["family"] = parts[0]
        if len(parts) > 1:
            name["given"] = parts[1:]
    return {
        "resourceType": "Patient",
        "id": str(p.id),
        "identifier": [{"system": "urn:oid:1.2.826.0.1.3680043.8.498",
                        "value": p.patient_id}],
        "name": [name] if name else [],
        "birthDate": p.patient_birth_date or None,
        "gender": _fhir_gender(p.patient_sex),
        "meta": {"lastUpdated": p.updated_at.isoformat() if p.updated_at else None},
    }


def _fhir_gender(sex: str | None) -> str | None:
    if not sex:
        return None
    s = sex.strip().upper()
    return {"M": "male", "F": "female", "O": "other", "U": "unknown"}.get(s, "unknown")


def _tenant_predicate(column, tenant_id: int | None, include_all: bool):
    """Tenant-isolation predicate matching ``query_service.apply_study_tenant_scope``.

    * ``include_all`` (ADMIN)      -> ``None`` (no restriction, every tenant);
    * ``tenant_id`` given          -> ``column == tenant_id``;
    * ``tenant_id`` is None        -> ``column IS NULL`` (the legacy/unscoped bucket).

    Using the same convention as the Study-scoped query builders (C04) is what
    keeps a tenant user from ever seeing another institution's rows — including a
    direct patient read by numeric id (C05).
    """
    if include_all:
        return None
    return column.is_(None) if tenant_id is None else column == tenant_id


def _imagingstudy_resource(study: Study, patient: Patient,
                           series_rows: list[Series], counts: dict[int, int]) -> dict:
    modalities = sorted({s.modality for s in series_rows if s.modality})
    return {
        "resourceType": "ImagingStudy",
        "id": study.study_instance_uid,
        "status": "available",
        "subject": {"reference": f"Patient/{patient.id}"},
        "identifier": ([{"system": "urn:dicom:uid",
                         "value": f"urn:oid:{study.study_instance_uid}"}] +
                       ([{"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                                               "code": "ACSN"}]},
                          "value": study.accession_number}]
                        if study.accession_number else [])),
        "started": f"{study.study_date}T{study.study_time or '000000'}"
        if study.study_date else None,
        "numberOfSeries": len(series_rows),
        "numberOfInstances": sum(counts.get(s.id, 0) for s in series_rows),
        "modality": [{"system": "http://dicom.nema.org/resources/ontology/DCM",
                      "code": m} for m in modalities],
        "series": [
            {
                "uid": s.series_instance_uid,
                "number": s.series_number,
                "modality": {"system": "http://dicom.nema.org/resources/ontology/DCM",
                             "code": s.modality} if s.modality else None,
                "description": s.series_description,
                "bodySite": s.body_part_examined,
                "numberOfInstances": counts.get(s.id, 0),
            }
            for s in series_rows
        ],
    }


# --------------------------------------------------------------------------- #
@router.get("/metadata", response_class=Response)
async def capability_statement():
    return _fhir_response({
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": "2026-01-01",
        "kind": "instance",
        "fhirVersion": "4.0.1",
        "format": ["json"],
        "rest": [{
            "mode": "server",
            "resource": [
                {"type": "Patient",
                 "interaction": [{"code": "read"}, {"code": "search-type"}]},
                {"type": "ImagingStudy",
                 "interaction": [{"code": "read"}, {"code": "search-type"}]},
            ],
        }],
    })


@router.get("/Patient", response_class=Response)
async def search_patient(
    db: AsyncSession = Depends(get_db),
    _id: str | None = Query(None, alias="_id"),
    identifier: str | None = Query(None),
    name: str | None = Query(None),
    _count: int = Query(50, alias="_count", ge=1, le=500),
    _user=Depends(get_current_user_any),
):
    stmt = select(Patient)
    conds = []
    # C05: patient search is tenant-scoped, exactly like ImagingStudy search.
    # Without this a tenant-A user could enumerate every institution's
    # demographics (name / MRN / birth date / sex).
    _tenant_id, _include_all = tenant_scope(_user)
    _tp = _tenant_predicate(Patient.tenant_id, _tenant_id, _include_all)
    if _tp is not None:
        conds.append(_tp)
    if _id:
        try:
            conds.append(Patient.id == int(_id))
        except ValueError:
            raise HTTPException(400, "Invalid _id")
    if identifier:
        conds.append(Patient.patient_id == identifier.split("|")[-1])
    if name:
        conds.append(Patient.patient_name.like(f"%{name}%"))
    for cond in conds:
        stmt = stmt.where(cond)
    patients = (await db.execute(stmt.limit(_count))).scalars().all()
    total = (
        await db.execute(select(func.count(Patient.id)).where(*conds))
    ).scalar_one()
    return _fhir_response({
        "resourceType": "Bundle",
        "type": "searchset",
        "total": total,
        "entry": [{"fullUrl": f"{SERVER_BASE}/Patient/{p.id}",
                   "resource": _patient_resource(p)} for p in patients],
    })


@router.get("/Patient/{patient_id}", response_class=Response)
async def read_patient(patient_id: str, db: AsyncSession = Depends(get_db),
                       _user=Depends(get_current_user_any)):
    try:
        pid = int(patient_id)
    except ValueError:
        raise HTTPException(400, "Patient id must be numeric")
    patient = (await db.execute(select(Patient).where(Patient.id == pid))).scalar_one_or_none()
    if patient is None:
        raise HTTPException(404, "Patient not found")
    # C05: a direct id read must respect the tenant boundary. A cross-tenant
    # patient — or a legacy NULL-tenant row seen by a tenant user — is a 404,
    # never a 403, so existence is not disclosed.
    _tenant_id, _include_all = tenant_scope(_user)
    if not _include_all and patient.tenant_id != _tenant_id:
        raise HTTPException(404, "Patient not found")
    return _fhir_response(_patient_resource(patient))


@router.get("/ImagingStudy", response_class=Response)
async def search_imagingstudy(
    db: AsyncSession = Depends(get_db),
    patient: str | None = Query(None),
    accession: str | None = Query(None),
    _count: int = Query(50, alias="_count", ge=1, le=500),
    _user=Depends(get_current_user_any),
):
    stmt = (
        select(Study)
        .options(joinedload(Study.patient))
        .join(Patient, Study.patient_id == Patient.id)
    )
    conds = []
    # C04: search results are tenant-scoped, not just direct reads. Added to
    # `conds` so the page query and the total-count query agree.
    _tenant_id, _include_all = tenant_scope(_user)
    _tp = _tenant_predicate(Study.tenant_id, _tenant_id, _include_all)
    if _tp is not None:
        conds.append(_tp)
    if patient:
        if patient.startswith("Patient/"):
            try:
                conds.append(Patient.id == int(patient.split("/")[1]))
            except ValueError:
                raise HTTPException(400, "Invalid patient reference")
        else:
            conds.append(Patient.patient_id == patient)
    if accession:
        conds.append(Study.accession_number == accession)
    for cond in conds:
        stmt = stmt.where(cond)
    studies = (await db.execute(stmt.limit(_count))).scalars().unique().all()
    total = (
        await db.execute(
            select(func.count(Study.id))
            .select_from(Study)
            .join(Patient, Study.patient_id == Patient.id)
            .where(*conds)
        )
    ).scalar_one()

    # Batch-load all series for the page (no N+1).
    series_by_study: dict[int, list[Series]] = {s.id: [] for s in studies}
    if studies:
        rows = (await db.execute(
            select(Series).where(Series.study_id.in_(list(series_by_study)))
            .order_by(Series.id)
        )).scalars().all()
        for r in rows:
            series_by_study.setdefault(r.study_id, []).append(r)
    all_series = [sr for lst in series_by_study.values() for sr in lst]
    counts = await query_service.series_instance_counts(
        db, [r.id for r in all_series]
    )

    entries = []
    for s in studies:
        series_rows = series_by_study[s.id]
        entries.append({
            "fullUrl": f"{SERVER_BASE}/ImagingStudy/{s.study_instance_uid}",
            "resource": _imagingstudy_resource(s, s.patient, series_rows, counts),
        })
    return _fhir_response({
        "resourceType": "Bundle",
        "type": "searchset",
        "total": total,
        "entry": entries,
    })


@router.get("/ImagingStudy/{study_uid}", response_class=Response)
async def read_imagingstudy(study_uid: str, db: AsyncSession = Depends(get_db),
                            _user=Depends(get_current_user_any)):
    # C04: a FHIR read is still a read of one institution's study.
    study = await require_study_access(db, study_uid, _user)
    study = (
        await db.execute(
            select(Study).options(joinedload(Study.patient)).where(Study.id == study.id)
        )
    ).scalar_one()
    series_rows = (await db.execute(
        select(Series).where(Series.study_id == study.id)
    )).scalars().all()
    counts = await query_service.series_instance_counts(db, [r.id for r in series_rows])
    return _fhir_response(_imagingstudy_resource(study, study.patient, series_rows, counts))
