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

All endpoints require authentication (get_current_user).
Admin endpoints for AA management require admin role.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_admin
from app.db.models import AssigningAuthority, Patient, PatientIdentity, User
from app.db.session import get_db
from app.services import pix_pdq_service

router = APIRouter(prefix="/api/v1/pix", tags=["PIX/PDQ"])


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


# Assigning Authority Management (Admin)
@router.post("/assigning-authorities", status_code=201)
async def create_assigning_authority(
    body: AssigningAuthorityCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Register a new Assigning Authority (Admin only)."""
    # Check for duplicate OID
    existing = await db.execute(
        select(AssigningAuthority).where(AssigningAuthority.oid == body.oid)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Assigning authority with OID {body.oid!r} already exists")
    
    aa = AssigningAuthority(
        oid=body.oid,
        name=body.name,
        is_authoritative=body.is_authoritative,
        description=body.description,
    )
    db.add(aa)
    await db.commit()
    await db.refresh(aa)
    return AssigningAuthorityResponse(
        id=aa.id,
        oid=aa.oid,
        name=aa.name,
        is_authoritative=aa.is_authoritative,
        description=aa.description,
        created_at=aa.created_at.isoformat() if aa.created_at else None,
    )


@router.get("/assigning-authorities")
async def list_assigning_authorities(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all registered Assigning Authorities."""
    aas = await db.execute(select(AssigningAuthority))
    return [
        AssigningAuthorityResponse(
            id=aa.id,
            oid=aa.oid,
            name=aa.name,
            is_authoritative=aa.is_authoritative,
            description=aa.description,
            created_at=aa.created_at.isoformat() if aa.created_at else None,
        )
        for aa in aas.scalars().all()
    ]


@router.get("/assigning-authorities/{oid}")
async def get_assigning_authority(
    oid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get Assigning Authority details by OID."""
    aa = await db.execute(
        select(AssigningAuthority).where(AssigningAuthority.oid == oid)
    )
    aa = aa.scalar_one_or_none()
    if not aa:
        raise HTTPException(404, f"Assigning authority {oid!r} not found")
    return AssigningAuthorityResponse(
        id=aa.id,
        oid=aa.oid,
        name=aa.name,
        is_authoritative=aa.is_authoritative,
        description=aa.description,
        created_at=aa.created_at.isoformat() if aa.created_at else None,
    )


@router.delete("/assigning-authorities/{oid}", status_code=204)
async def delete_assigning_authority(
    oid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Delete an Assigning Authority (Admin only)."""
    aa = await db.execute(
        select(AssigningAuthority).where(AssigningAuthority.oid == oid)
    )
    aa = aa.scalar_one_or_none()
    if not aa:
        raise HTTPException(404, f"Assigning authority {oid!r} not found")
    await db.delete(aa)
    await db.commit()
    return None


# Patient Identity Management
@router.post("/identities", status_code=201)
async def create_patient_identity(
    body: PatientIdentityCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create or update a patient identity mapping."""
    try:
        identity = await asyncio.to_thread(
            pix_pdq_service.upsert_patient_identity,
            body.patient_id,
            body.assigning_authority_oid,
            body.identifier,
            body.id_type,
            body.is_primary,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(400, f"Failed to create identity: {exc}")
    
    # Fetch AA name for response
    aa = await db.execute(
        select(AssigningAuthority).where(AssigningAuthority.id == identity.assigning_authority_id)
    )
    aa = aa.scalar_one()
    
    return PatientIdentityResponse(
        id=identity.id,
        patient_id=identity.patient_id,
        assigning_authority_oid=aa.oid,
        assigning_authority_name=aa.name,
        identifier=identity.identifier,
        id_type=identity.id_type,
        is_primary=identity.is_primary,
    )


@router.get("/patients/{patient_id}/identities")
async def list_patient_identities(
    patient_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get all identities for a patient across all assigning authorities."""
    patient = await db.get(Patient, patient_id)
    if not patient:
        raise HTTPException(404, f"Patient {patient_id} not found")
    
    identities = await db.execute(
        select(PatientIdentity, AssigningAuthority)
        .join(AssigningAuthority, PatientIdentity.assigning_authority_id == AssigningAuthority.id)
        .where(PatientIdentity.patient_id == patient_id)
    )
    
    return [
        PatientIdentityResponse(
            id=pi.id,
            patient_id=pi.patient_id,
            assigning_authority_oid=aa.oid,
            assigning_authority_name=aa.name,
            identifier=pi.identifier,
            id_type=pi.id_type,
            is_primary=pi.is_primary,
        )
        for pi, aa in identities.all()
    ]


@router.delete("/patients/{patient_id}/identities/{aa_oid}", status_code=204)
async def delete_patient_identity(
    patient_id: int,
    aa_oid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
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
@router.get("/query")
async def pix_query(
    assigning_authority_oid: str | None = Query(None),
    identifier: str | None = Query(None),
    patient_name: str | None = Query(None),
    birth_date: str | None = Query(None),
    sex: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    """PIX Query: Find patient identifiers across all assigning authorities.
    
    Either provide (assigning_authority_oid + identifier) for exact match,
    or demographics (patient_name, birth_date, sex) for fuzzy match.
    Returns all known identities for matching patient(s).
    """
    if assigning_authority_oid and identifier:
        results = await asyncio.to_thread(
            pix_pdq_service.pix_query_by_identifier,
            assigning_authority_oid,
            identifier,
        )
    elif patient_name or birth_date or sex:
        results = await asyncio.to_thread(
            pix_pdq_service.pix_query_by_demographics,
            patient_name,
            birth_date,
            sex,
        )
    else:
        raise HTTPException(422, "Provide either (assigning_authority_oid + identifier) or demographics")
    
    return JSONResponse([
        PatientIdentityResultResponse(
            assigning_authority_oid=r.assigning_authority_oid,
            assigning_authority_name=r.assigning_authority_name,
            identifier=r.identifier,
            id_type=r.id_type,
            is_primary=r.is_primary,
        ).model_dump()
        for r in results
    ])


# PDQ Query (Patient Demographics Query)
@router.get("/pdq/query")
async def pdq_query(
    assigning_authority_oid: str | None = Query(None),
    identifier: str | None = Query(None),
    patient_name: str | None = Query(None),
    birth_date: str | None = Query(None),
    sex: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    """PDQ Query: Find patient demographics across all assigning authorities.
    
    Either provide (assigning_authority_oid + identifier) for exact match,
    or demographics (patient_name, birth_date, sex) for fuzzy match.
    Optionally filter by assigning_authority_oid.
    Returns full demographics for matching patient(s).
    """
    if assigning_authority_oid and identifier:
        results = await asyncio.to_thread(
            pix_pdq_service.pdq_query_by_identifier,
            assigning_authority_oid,
            identifier,
        )
    elif patient_name or birth_date or sex:
        results = await asyncio.to_thread(
            pix_pdq_service.pdq_query_by_demographics,
            patient_name,
            birth_date,
            sex,
            assigning_authority_oid,
        )
    else:
        raise HTTPException(422, "Provide either (assigning_authority_oid + identifier) or demographics")
    
    return JSONResponse([
        PatientDemographicsResponse(
            patient_id=d.patient_id,
            identities=[
                PatientIdentityResultResponse(
                    assigning_authority_oid=i.assigning_authority_oid,
                    assigning_authority_name=i.assigning_authority_name,
                    identifier=i.identifier,
                    id_type=i.id_type,
                    is_primary=i.is_primary,
                )
                for i in d.identities
            ],
            patient_name=d.patient_name,
            birth_date=d.birth_date,
            sex=d.sex,
            phone=d.phone,
            source=d.source,
        ).model_dump()
        for d in results
    ])
