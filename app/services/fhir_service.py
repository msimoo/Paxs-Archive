"""FHIR R4 read gateway — queries and resource mapping (Q-3 / FA-2).

The router used to build every statement itself, which put ``select()`` and
``from app.db.models import …`` in the routing layer and made the tenant
predicate something each endpoint had to remember to apply. Both searches here
funnel through :func:`query_service.tenant_predicate` — the one predicate
(FA-1) — so a FHIR search cannot drift from QIDO/WADO scope.

Resource *mapping* lives here too, not in the router: it is presentation logic
over domain rows, and keeping it beside the queries is what lets the endpoints
read as parse → scope → delegate → respond (AGENTS.md §3.1).
"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.core.config import resolve_public_base_url
from app.db.models import Patient, Series, Study
from app.services import query_service

FHIR_JSON = "application/fhir+json"

#: Base URL for the ``fullUrl`` of every bundled resource.
SERVER_BASE = resolve_public_base_url() + "/fhir"

#: Coding system for the DICOM modality codes FHIR emits.
_DCM_SYSTEM = "http://dicom.nema.org/resources/ontology/DCM"
#: Coding system for the accession-number identifier.
_ACSN_SYSTEM = "http://terminology.hl7.org/CodeSystem/v2-0203"
#: Assigning authority for the MRN identifier.
_MRN_SYSTEM = "urn:oid:1.2.826.0.1.3680043.8.498"

#: LOINC/HL7 administrative gender mapping from DICOM ``PatientSex``.
_GENDER = {"M": "male", "F": "female", "O": "other", "U": "unknown"}


def fhir_json(data: dict, status: int = 200) -> tuple[str, int]:
    """``(body, status)`` for a FHIR response — serialisation, not transport."""
    return json.dumps(data), status


def _gender(sex: str | None) -> str | None:
    if not sex:
        return None
    return _GENDER.get(sex.strip().upper(), "unknown")


def patient_resource(p: Patient) -> dict:
    """A FHIR ``Patient`` for one row."""
    name: dict | None = None
    if p.patient_name:
        # DICOM PN is "FAMILY^GIVEN^MIDDLE^SUFFIX".
        parts = p.patient_name.replace("^", " ").split()
        name = {"text": p.patient_name.replace("^", " ")}
        if parts:
            name["family"] = parts[0]
        if len(parts) > 1:
            name["given"] = parts[1:]
    return {
        "resourceType": "Patient",
        "id": str(p.id),
        "identifier": [{"system": _MRN_SYSTEM, "value": p.patient_id}],
        "name": [name] if name else [],
        "birthDate": p.patient_birth_date or None,
        "gender": _gender(p.patient_sex),
        "meta": {"lastUpdated": p.updated_at.isoformat() if p.updated_at else None},
    }


def imaging_study_resource(study: Study, patient: Patient,
                           series_rows: list[Series],
                           counts: dict[int, int]) -> dict:
    """A FHIR ``ImagingStudy`` for one study, its series and per-series counts."""
    modalities = sorted({s.modality for s in series_rows if s.modality})
    identifiers = [{"system": "urn:dicom:uid",
                    "value": f"urn:oid:{study.study_instance_uid}"}]
    if study.accession_number:
        identifiers.append({
            "type": {"coding": [{"system": _ACSN_SYSTEM, "code": "ACSN"}]},
            "value": study.accession_number,
        })
    return {
        "resourceType": "ImagingStudy",
        "id": study.study_instance_uid,
        "status": "available",
        "subject": {"reference": f"Patient/{patient.id}"},
        "identifier": identifiers,
        "started": f"{study.study_date}T{study.study_time or '000000'}"
        if study.study_date else None,
        "numberOfSeries": len(series_rows),
        "numberOfInstances": sum(counts.get(s.id, 0) for s in series_rows),
        "modality": [{"system": _DCM_SYSTEM, "code": m} for m in modalities],
        "series": [
            {
                "uid": s.series_instance_uid,
                "number": s.series_number,
                "modality": {"system": _DCM_SYSTEM, "code": s.modality}
                if s.modality else None,
                "description": s.series_description,
                "bodySite": s.body_part_examined,
                "numberOfInstances": counts.get(s.id, 0),
            }
            for s in series_rows
        ],
    }


def _patient_bundle(patients: list[Patient], total: int) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": total,
        "entry": [{"fullUrl": f"{SERVER_BASE}/Patient/{p.id}",
                   "resource": patient_resource(p)} for p in patients],
    }


def _literal_like_pattern(value: str) -> str:
    """Build a substring pattern where SQL LIKE metacharacters are literal."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# --------------------------------------------------------------------------- #
# Patient
# --------------------------------------------------------------------------- #
async def search_patients(db, tenant_id: int | None, include_all: bool, *,
                          patient_id: int | None = None,
                          identifier: str | None = None,
                          name: str | None = None,
                          limit: int = 50) -> dict:
    """A FHIR ``Bundle`` of matching patients, tenant-scoped.

    The page query and the ``total`` count share one ``conds`` list, so a
    filtered search cannot report a total that disagrees with what it returned.
    """
    conds = []
    # C05: patient search is tenant-scoped. Without this a tenant-A user could
    # enumerate every institution's demographics.
    tp = query_service.tenant_predicate(Patient.tenant_id, tenant_id, include_all)
    if tp is not None:
        conds.append(tp)
    if patient_id is not None:
        conds.append(Patient.id == patient_id)
    if identifier:
        # An identifier may be "system|value"; the MRN is the value part.
        conds.append(Patient.patient_id == identifier.split("|")[-1])
    if name:
        conds.append(Patient.patient_name.ilike(_literal_like_pattern(name), escape="\\"))

    rows = (await db.execute(
        select(Patient).where(*conds).limit(limit)
    )).scalars().all()
    total = (await db.execute(
        select(func.count(Patient.id)).where(*conds)
    )).scalar_one()
    return _patient_bundle(list(rows), total)


async def get_patient(db, patient_id: int, tenant_id: int | None,
                      include_all: bool) -> Patient | None:
    """One patient, or ``None`` when it is absent *or* out of scope.

    A cross-tenant patient — or a legacy NULL-tenant row seen by a tenant user —
    is indistinguishable from a missing one, so the router can answer 404
    without disclosing existence (N-1).
    """
    stmt = select(Patient).where(Patient.id == patient_id)
    scope = query_service.tenant_predicate(Patient.tenant_id, tenant_id, include_all)
    if scope is not None:
        stmt = stmt.where(scope)
    return (await db.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# ImagingStudy
# --------------------------------------------------------------------------- #
async def _series_for_studies(db, study_ids: list[int]) -> dict[int, list[Series]]:
    """Batch-load every series of ``study_ids`` (no N+1)."""
    by_study: dict[int, list[Series]] = {sid: [] for sid in study_ids}
    if not study_ids:
        return by_study
    rows = (await db.execute(
        select(Series).where(Series.study_id.in_(study_ids)).order_by(Series.id)
    )).scalars().all()
    for row in rows:
        by_study.setdefault(row.study_id, []).append(row)
    return by_study


def _imaging_study_bundle(studies: list[Study], total: int,
                          series_by_study: dict[int, list[Series]],
                          counts: dict[int, int]) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": total,
        "entry": [
            {
                "fullUrl": f"{SERVER_BASE}/ImagingStudy/{s.study_instance_uid}",
                "resource": imaging_study_resource(
                    s, s.patient, series_by_study.get(s.id, []), counts
                ),
            }
            for s in studies
        ],
    }


async def search_imaging_studies(db, tenant_id: int | None, include_all: bool, *,
                                 patient: str | None = None,
                                 accession: str | None = None,
                                 limit: int = 50) -> dict:
    """A FHIR ``Bundle`` of matching studies, tenant-scoped (C04)."""
    conds = []
    tp = query_service.tenant_predicate(Study.tenant_id, tenant_id, include_all)
    if tp is not None:
        conds.append(tp)
    patient_tp = query_service.tenant_predicate(Patient.tenant_id, tenant_id, include_all)
    if patient_tp is not None:
        conds.append(patient_tp)
    if patient:
        if patient.startswith("Patient/"):
            conds.append(Patient.id == int(patient.split("/")[1]))
        else:
            conds.append(Patient.patient_id == patient)
    if accession:
        conds.append(Study.accession_number == accession)

    studies = (await db.execute(
        select(Study)
        .options(joinedload(Study.patient))
        .join(Patient, Study.patient_id == Patient.id)
        .where(*conds)
        .limit(limit)
    )).scalars().unique().all()
    total = (await db.execute(
        select(func.count(Study.id))
        .select_from(Study)
        .join(Patient, Study.patient_id == Patient.id)
        .where(*conds)
    )).scalar_one()

    series_by_study = await _series_for_studies(db, [s.id for s in studies])
    counts = await query_service.series_instance_counts(
        db, [r.id for rows in series_by_study.values() for r in rows]
    )
    return _imaging_study_bundle(list(studies), total, series_by_study, counts)


async def imaging_study_bundle_for(
    db, study: Study, tenant_id: int | None, include_all: bool
) -> dict | None:
    """Return an authorized study bundle, or ``None`` if its patient is out of scope."""
    stmt = (
        select(Study)
        .options(joinedload(Study.patient))
        .join(Patient, Study.patient_id == Patient.id)
        .where(Study.id == study.id)
    )
    patient_tp = query_service.tenant_predicate(Patient.tenant_id, tenant_id, include_all)
    if patient_tp is not None:
        stmt = stmt.where(patient_tp)
    study = (await db.execute(stmt)).scalar_one_or_none()
    if study is None:
        return None
    series_rows = (await db.execute(
        select(Series).where(Series.study_id == study.id)
    )).scalars().all()
    counts = await query_service.series_instance_counts(db, [r.id for r in series_rows])
    return imaging_study_resource(study, study.patient, list(series_rows), counts)
