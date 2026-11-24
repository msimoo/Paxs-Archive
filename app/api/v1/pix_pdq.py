"""IHE PIX/PDQ REST API - Patient Identity Cross-referencing & Demographics Query.

Endpoints:
  GET  /api/v1/pix/assigning-authorities           List all registered AAs
  POST /api/v1/pix/assigning-authorities           Create new AA
  GET  /api/v1/pix/assigning-authorities/{oid}     Get AA details
  DELETE /api/v1/pix/assigning-authorities/{oid}   Delete AA

  POST /api/v1/pix/identities                      Create/update patient identity
  GET  /api/v1/pix/patients/{patient_id}/identities  Get all identities for patient
  DELETE /api/v1/pix/patients/{patient_id}/identities/{aa_oid}  Delete identity

  GET  /api/v1/pix/query                           PIX Query by identifier or demographics
  GET  /api/v1/pdq/query                           PDQ Query by identifier or demographics

All endpoints accept bearer or session-cookie authentication. Patient reads use
the caller's tenant scope; patient identity writes are ADMIN-only and CSRF
protected when authenticated by a browser cookie.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.v1.deps import get_current_user_any, require_admin, require_csrf, tenant_scope
from app.db.models import User
from app.services import pix_pdq_admin, pix_pdq_rest_service, pix_pdq_service

# Statements behind this router live in PIX/PDQ services. The handlers only
# shape requests and responses; sync database work runs in a worker thread so
# the event loop remains available (N-4).

router = APIRouter(
    prefix="/api/v1/pix",
    tags=["PIX/PDQ"],
    dependencies=[Depends(require_csrf)],
)


# Request/Response Models
class AssigningAuthorityCreate(BaseModel):
    oid: str = Field(..., min_length=1, max_length=128, description="OID/URI of the assigning authority")
    name: str = Field(..., min_length=1, max_length=128, description="Human-readable name")
    is_authoritative: bool = Field(False, description="Whether PAXS is authoritative for this domain")
    description: str | None = Field(None, max_length=255)


class AssigningAuthorityResponse(BaseModel):
    id: int
    oid: str
    name: str
    is_authoritative: bool
    description: str | None
    created_at: str | None


class PatientIdentityCreate(BaseModel):
    patient_id: int = Field(..., description="Local patient ID")
    assigning_authority_oid: str = Field(..., description="OID of the assigning authority")
    identifier: str = Field(..., min_length=1, max_length=64, description="Identifier value in that AA")
    id_type: str = Field("MR", max_length=16, description="Identifier type code (MR, NI, etc.)")
    is_primary: bool = Field(True)


class PatientIdentityResponse(BaseModel):
    id: int
    patient_id: int
    assigning_authority_oid: str
    assigning_authority_name: str
    identifier: str
    id_type: str
    is_primary: bool


class PIXQueryRequest(BaseModel):
    assigning_authority_oid: str | None = Field(None, description="AA OID for identifier-based query")
    identifier: str | None = Field(None, description="Patient identifier in that AA")
    patient_name: str | None = Field(None, description="Patient name for demographic query")
    birth_date: str | None = Field(None, description="Birth date (YYYYMMDD)")
    sex: str | None = Field(None, description="Sex (M/F/O)")


class PDQQueryRequest(BaseModel):
    assigning_authority_oid: str | None = Field(None, description="AA OID for identifier-based query or filter")
    identifier: str | None = Field(None, description="Patient identifier in that AA")
    patient_name: str | None = Field(None, description="Patient name for demographic query")
    birth_date: str | None = Field(None, description="Birth date (YYYYMMDD)")
    sex: str | None = Field(None, description="Sex (M/F/O)")


class PatientIdentityResultResponse(BaseModel):
    assigning_authority_oid: str
    assigning_authority_name: str
    identifier: str
    id_type: str
    is_primary: bool


class PatientDemographicsResponse(BaseModel):
    patient_id: int
    identities: list[PatientIdentityResultResponse]
    patient_name: str | None
    birth_date: str | None
    sex: str | None
    phone: str | None
    source: str


def _authority_response(aa: pix_pdq_admin.AssigningAuthorityEntry) -> AssigningAuthorityResponse:
    """Service entry -> response model (kept here: response shape is HTTP's job)."""
    return AssigningAuthorityResponse(
        id=aa.id,
        oid=aa.oid,
        name=aa.name,
        is_authoritative=aa.is_authoritative,
        description=aa.description,
        created_at=aa.created_at,
    )


def _identity_response(entry: pix_pdq_admin.PatientIdentityEntry) -> PatientIdentityResponse:
    return PatientIdentityResponse(
        id=entry.id,
        patient_id=entry.patient_id,
        assigning_authority_oid=entry.assigning_authority_oid,
        assigning_authority_name=entry.assigning_authority_name,
        identifier=entry.identifier,
        id_type=entry.id_type,
        is_primary=entry.is_primary,
    )


# Assigning Authority Management (Admin)
@router.post("/assigning-authorities", status_code=201)
async def create_assigning_authority(
    body: AssigningAuthorityCreate,
    user: User = Depends(require_admin),
) -> AssigningAuthorityResponse:
    """Register a new Assigning Authority (Admin only)."""
    aa = await asyncio.to_thread(
        pix_pdq_admin.register_assigning_authority,
        body.oid,
        body.name,
        body.is_authoritative,
        body.description,
    )
    if aa is None:
        raise HTTPException(409, f"Assigning authority with OID {body.oid!r} already exists")
    return _authority_response(aa)


@router.get("/assigning-authorities")
async def list_assigning_authorities(
    user: User = Depends(get_current_user_any),
) -> list[AssigningAuthorityResponse]:
    """List all registered Assigning Authorities."""
    aas = await asyncio.to_thread(pix_pdq_admin.list_assigning_authority_entries)
    return [_authority_response(aa) for aa in aas]


@router.get("/assigning-authorities/{oid}")
async def get_assigning_authority(
    oid: str,
    user: User = Depends(get_current_user_any),
) -> AssigningAuthorityResponse:
    """Get Assigning Authority details by OID."""
    aa = await asyncio.to_thread(pix_pdq_admin.get_assigning_authority_entry, oid)
    if aa is None:
        raise HTTPException(404, f"Assigning authority {oid!r} not found")
    return _authority_response(aa)


@router.delete("/assigning-authorities/{oid}", status_code=204)
async def delete_assigning_authority(
    oid: str,
    user: User = Depends(require_admin),
) -> None:
    """Delete an Assigning Authority (Admin only)."""
    if not await asyncio.to_thread(pix_pdq_admin.delete_assigning_authority, oid):
        raise HTTPException(404, f"Assigning authority {oid!r} not found")
    return None


# Patient Identity Management
@router.post("/identities", status_code=201)
async def create_patient_identity(
    body: PatientIdentityCreate,
    user: User = Depends(require_admin),
) -> PatientIdentityResponse:
    """Create or update a patient identity mapping (Admin only)."""
    try:
        entry = await asyncio.to_thread(
            pix_pdq_admin.register_patient_identity,
            body.patient_id,
            body.assigning_authority_oid,
            body.identifier,
            body.id_type,
            body.is_primary,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # database failures may include patient identifiers
        raise HTTPException(400, "Failed to create identity") from None
    return _identity_response(entry)


@router.get("/patients/{patient_id}/identities")
async def list_patient_identities(
    patient_id: int,
    user: User = Depends(get_current_user_any),
) -> list[PatientIdentityResponse]:
    """Get all identities for a patient across all assigning authorities."""
    tenant_id, include_all = tenant_scope(user)
    entries = await asyncio.to_thread(
        pix_pdq_admin.list_patient_identity_entries,
        patient_id,
        tenant_id,
        include_all,
    )
    if entries is None:
        raise HTTPException(404, f"Patient {patient_id} not found")
    return [_identity_response(entry) for entry in entries]


@router.delete("/patients/{patient_id}/identities/{aa_oid}", status_code=204)
async def delete_patient_identity(
    patient_id: int,
    aa_oid: str,
    user: User = Depends(require_admin),
) -> None:
    """Delete a patient identity mapping (Admin only)."""
    deleted = await asyncio.to_thread(
        pix_pdq_service.delete_patient_identity,
        patient_id,
        aa_oid,
    )
    if not deleted:
        raise HTTPException(404, "Identity not found")
    return None


# PIX Query (Patient Identity Cross-referencing)
def _pix_query_has_criteria(
    assigning_authority_oid: str | None,
    identifier: str | None,
    patient_name: str | None,
    birth_date: str | None,
    sex: str | None,
) -> bool:
    """Require either a complete identifier pair or at least one demographic filter."""
    return bool(
        (assigning_authority_oid and identifier)
        or patient_name
        or birth_date
        or sex
    )


def _pix_identity_response(results: Iterable[Any]) -> JSONResponse:
    """Serialize only the identity fields supported by the PIX response."""
    payload = [
        PatientIdentityResultResponse(
            assigning_authority_oid=result.assigning_authority_oid,
            assigning_authority_name=result.assigning_authority_name,
            identifier=result.identifier,
            id_type=result.id_type,
            is_primary=result.is_primary,
        ).model_dump()
        for result in results
    ]
    return JSONResponse(payload)


def _pdq_demographics_response(results: Iterable[Any]) -> JSONResponse:
    """Serialize demographic matches without leaking ORM objects."""
    payload = [
        PatientDemographicsResponse(
            patient_id=demographic.patient_id,
            identities=[
                PatientIdentityResultResponse(
                    assigning_authority_oid=identity.assigning_authority_oid,
                    assigning_authority_name=identity.assigning_authority_name,
                    identifier=identity.identifier,
                    id_type=identity.id_type,
                    is_primary=identity.is_primary,
                )
                for identity in demographic.identities
            ],
            patient_name=demographic.patient_name,
            birth_date=demographic.birth_date,
            sex=demographic.sex,
            phone=demographic.phone,
            source=demographic.source,
        ).model_dump()
        for demographic in results
    ]
    return JSONResponse(payload)


@router.get("/query")
async def pix_query(
    assigning_authority_oid: str | None = Query(None),
    identifier: str | None = Query(None),
    patient_name: str | None = Query(None),
    birth_date: str | None = Query(None),
    sex: str | None = Query(None),
    user: User = Depends(get_current_user_any),
) -> JSONResponse:
    """PIX Query: Find patient identifiers within the caller's tenant.

    Either provide (assigning_authority_oid + identifier) for exact match,
    or demographics (patient_name, birth_date, sex) for fuzzy match.
    Returns all known identities for matching patient(s).
    """
    if not _pix_query_has_criteria(
        assigning_authority_oid, identifier, patient_name, birth_date, sex
    ):
        raise HTTPException(422, "Provide either (assigning_authority_oid + identifier) or demographics")
    if assigning_authority_oid and identifier:
        patient_name = birth_date = sex = None
    else:
        assigning_authority_oid = identifier = None
    tenant_id, include_all = tenant_scope(user)
    results = await asyncio.to_thread(
        pix_pdq_rest_service.pix_query,
        assigning_authority_oid,
        identifier,
        patient_name,
        birth_date,
        sex,
        tenant_id,
        include_all,
    )
    return _pix_identity_response(results)


# PDQ Query (Patient Demographics Query)
@router.get("/pdq/query")
async def pdq_query(
    assigning_authority_oid: str | None = Query(None),
    identifier: str | None = Query(None),
    patient_name: str | None = Query(None),
    birth_date: str | None = Query(None),
    sex: str | None = Query(None),
    user: User = Depends(get_current_user_any),
) -> JSONResponse:
    """PDQ Query: Find patient demographics within the caller's tenant.

    Either provide (assigning_authority_oid + identifier) for exact match,
    or demographics (patient_name, birth_date, sex) for fuzzy match.
    Optionally filter by assigning_authority_oid.
    Returns full demographics for matching patient(s).
    """
    if not ((assigning_authority_oid and identifier) or patient_name or birth_date or sex):
        raise HTTPException(422, "Provide either (assigning_authority_oid + identifier) or demographics")
    if assigning_authority_oid and identifier:
        patient_name = birth_date = sex = None
    else:
        identifier = None
    tenant_id, include_all = tenant_scope(user)
    results = await asyncio.to_thread(
        pix_pdq_rest_service.pdq_query,
        assigning_authority_oid,
        identifier,
        patient_name,
        birth_date,
        sex,
        tenant_id,
        include_all,
    )
    return _pdq_demographics_response(results)
