"""FHIR R4 read gateway (HL7/FHIR integration).

Exposes archive data as FHIR resources so external systems (EMR/HIS,
research portals) can consume PAXS without knowing DICOMweb. Read-only for
now: Patient and ImagingStudy, with minimal search (identifier/name/patient).

Response content type is `application/fhir+json`.

Q-3 / FA-2: this module holds **no SQL**. Every statement — and the tenant
predicate it must apply — lives in :mod:`app.services.fhir_service`, so the
endpoints read as parse → resolve scope → delegate → respond and a search can
never drift from the scope QIDO/WADO use.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user_any, require_study_access, tenant_scope
from app.db.session import get_db
from app.services import fhir_service

router = APIRouter(prefix="/fhir", tags=["FHIR R4"])

FHIR_JSON = "application/fhir+json"


def _fhir_response(data: dict, status: int = 200) -> Response:
    body, code = fhir_service.fhir_json(data, status)
    return Response(content=body, media_type=FHIR_JSON, status_code=code)


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
    patient_id: int | None = None
    if _id:
        try:
            patient_id = int(_id)
        except ValueError:
            raise HTTPException(400, "Invalid _id") from None
    tenant_id, include_all = tenant_scope(_user)
    bundle = await fhir_service.search_patients(
        db, tenant_id, include_all,
        patient_id=patient_id, identifier=identifier, name=name, limit=_count,
    )
    return _fhir_response(bundle)


@router.get("/Patient/{patient_id}", response_class=Response)
async def read_patient(patient_id: str, db: AsyncSession = Depends(get_db),
                       _user=Depends(get_current_user_any)):
    try:
        pid = int(patient_id)
    except ValueError:
        raise HTTPException(400, "Patient id must be numeric") from None
    # C05: a direct id read respects the tenant boundary — the service returns
    # None for a cross-tenant or out-of-scope row, so this is a 404, never a 403.
    tenant_id, include_all = tenant_scope(_user)
    patient = await fhir_service.get_patient(db, pid, tenant_id, include_all)
    if patient is None:
        raise HTTPException(404, "Patient not found")
    return _fhir_response(fhir_service.patient_resource(patient))


@router.get("/ImagingStudy", response_class=Response)
async def search_imagingstudy(
    db: AsyncSession = Depends(get_db),
    patient: str | None = Query(None),
    accession: str | None = Query(None),
    _count: int = Query(50, alias="_count", ge=1, le=500),
    _user=Depends(get_current_user_any),
):
    if patient and patient.startswith("Patient/"):
        try:
            int(patient.split("/")[1])
        except (ValueError, IndexError):
            raise HTTPException(400, "Invalid patient reference") from None
    tenant_id, include_all = tenant_scope(_user)
    bundle = await fhir_service.search_imaging_studies(
        db, tenant_id, include_all,
        patient=patient, accession=accession, limit=_count,
    )
    return _fhir_response(bundle)


@router.get("/ImagingStudy/{study_uid}", response_class=Response)
async def read_imagingstudy(study_uid: str, db: AsyncSession = Depends(get_db),
                            _user=Depends(get_current_user_any)) -> Response:
    # C04: a FHIR read is still a read of one institution's study.
    study = await require_study_access(db, study_uid, _user)
    tenant_id, include_all = tenant_scope(_user)
    bundle = await fhir_service.imaging_study_bundle_for(
        db, study, tenant_id, include_all
    )
    if bundle is None:
        raise HTTPException(404, "Study not found")
    return _fhir_response(bundle)
