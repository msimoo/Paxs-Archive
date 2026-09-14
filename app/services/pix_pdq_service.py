"""IHE PIX/PDQ Service - Patient Identity Cross-referencing & Demographics Query.

Implements:
  * PIX Manager (Patient Identity Feed + Patient Identity Query)
  * PDQ Supplier (Patient Demographics Query)

Architecture:
  - AssigningAuthority: registered identity domains (OID/URI)
  - PatientIdentity: maps local Patient -> (AA, identifier, type)
  - PIX Query (QBP^Q23): find patient IDs across all known domains
  - PDQ Query (QBP^Q22): find patient demographics across all known domains
  - Patient Identity Feed (ADT^A04/A08/A40): register/update identities

All DB access is sync (SyncSessionLocal) for use in HL7 listener thread.
REST API wrappers use asyncio.to_thread like other HL7 endpoints.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from app.db.models import AssigningAuthority, Patient, PatientIdentity
from app.db.session import SyncSessionLocal

log = logging.getLogger("paxs.pix_pdq")


@dataclass
class PatientIdentityResult:
    """A single patient identifier from a specific assigning authority."""
    assigning_authority_oid: str
    assigning_authority_name: str
    identifier: str
    id_type: str
    is_primary: bool


@dataclass
class PatientDemographics:
    """Patient demographics for PDQ response."""
    patient_id: int  # local PK
    identities: list[PatientIdentityResult]
    patient_name: str | None
    birth_date: str | None  # YYYYMMDD
    sex: str | None
    phone: str | None
    source: str  # "dicom" | "his"


def create_assigning_authority(
    oid: str,
    name: str,
    is_authoritative: bool = False,
    description: str | None = None,
) -> AssigningAuthority:
    """Register a new Assigning Authority."""
    with SyncSessionLocal() as session:
        aa = AssigningAuthority(
            oid=oid,
            name=name,
            is_authoritative=is_authoritative,
            description=description,
        )
        session.add(aa)
        session.flush()
        session.refresh(aa)
        return aa


def get_assigning_authority(oid: str) -> AssigningAuthority | None:
    """Find an Assigning Authority by OID."""
    with SyncSessionLocal() as session:
        return session.execute(
            select(AssigningAuthority).where(AssigningAuthority.oid == oid)
        ).scalar_one_or_none()


def list_assigning_authorities() -> list[AssigningAuthority]:
    """List all registered Assigning Authorities."""
    with SyncSessionLocal() as session:
        return list(session.execute(select(AssigningAuthority)).scalars().all())


def upsert_patient_identity(
    patient_id: int,
    assigning_authority_oid: str,
    identifier: str,
    id_type: str = "MR",
    is_primary: bool = True,
) -> PatientIdentity:
    """Register or update a patient identifier in a specific assigning authority.
    
    This is the core PIX Patient Identity Feed operation.
    """
    with SyncSessionLocal() as session:
        aa = session.execute(
            select(AssigningAuthority).where(AssigningAuthority.oid == assigning_authority_oid)
        ).scalar_one_or_none()
        if aa is None:
            raise ValueError(f"Unknown assigning authority OID: {assigning_authority_oid}")

        # Check if this identity already exists
        existing = session.execute(
            select(PatientIdentity).where(
                PatientIdentity.patient_id == patient_id,
                PatientIdentity.assigning_authority_id == aa.id,
            )
        ).scalar_one_or_none()

        if existing:
            existing.identifier = identifier
            existing.id_type = id_type
            existing.is_primary = is_primary
            session.commit()
            session.refresh(existing)
            log.info("Updated patient identity: patient=%d aa=%s id=%s",
                     patient_id, assigning_authority_oid, identifier)
            return existing

        identity = PatientIdentity(
            patient_id=patient_id,
            assigning_authority_id=aa.id,
            identifier=identifier,
            id_type=id_type,
            is_primary=is_primary,
        )
        session.add(identity)
        session.commit()
        session.refresh(identity)
        log.info("Created patient identity: patient=%d aa=%s id=%s",
                 patient_id, assigning_authority_oid, identifier)
        return identity


def delete_patient_identity(patient_id: int, assigning_authority_oid: str) -> bool:
    """Remove a patient identity mapping."""
    with SyncSessionLocal() as session:
        aa = session.execute(
            select(AssigningAuthority).where(AssigningAuthority.oid == assigning_authority_oid)
        ).scalar_one_or_none()
        if aa is None:
            return False
        identity = session.execute(
            select(PatientIdentity).where(
                PatientIdentity.patient_id == patient_id,
                PatientIdentity.assigning_authority_id == aa.id,
            )
        ).scalar_one_or_none()
        if identity:
            session.delete(identity)
            session.commit()
            return True
        return False


def pix_query_by_identifier(
    assigning_authority_oid: str,
    identifier: str,
) -> list[PatientIdentityResult]:
    """PIX Query: find all patient identifiers for a given (AA, identifier) pair.
    
    Returns all identities for the matched patient across ALL assigning authorities.
    This is the core IHE PIX cross-reference operation.
    """
    with SyncSessionLocal() as session:
        # Find the identity matching the query
        aa = session.execute(
            select(AssigningAuthority).where(AssigningAuthority.oid == assigning_authority_oid)
        ).scalar_one_or_none()
        if aa is None:
            return []

        identity = session.execute(
            select(PatientIdentity).where(
                PatientIdentity.assigning_authority_id == aa.id,
                PatientIdentity.identifier == identifier,
            )
        ).scalar_one_or_none()
        if identity is None:
            return []

        # Found the patient - now get ALL their identities
        return _get_all_identities_for_patient(session, identity.patient_id)


def pix_query_by_demographics(
    patient_name: str | None = None,
    birth_date: str | None = None,
    sex: str | None = None,
) -> list[PatientIdentityResult]:
    """PIX Query by demographics (fuzzy match).
    
    Returns all identities for all matching patients across ALL assigning authorities.
    """
    with SyncSessionLocal() as session:
        stmt = select(Patient)
        if patient_name:
            # Simple contains match on patient_name
            stmt = stmt.where(Patient.patient_name.ilike(f"%{patient_name}%"))
        if birth_date:
            stmt = stmt.where(Patient.patient_birth_date == birth_date)
        if sex:
            stmt = stmt.where(Patient.patient_sex == sex)
        
        patients = session.execute(stmt.limit(50)).scalars().all()
        results = []
        for patient in patients:
            results.extend(_get_all_identities_for_patient(session, patient.id))
        return results


def _get_all_identities_for_patient(session, patient_id: int) -> list[PatientIdentityResult]:
    """Get all identities for a patient across all assigning authorities."""
    identities = session.execute(
        select(PatientIdentity, AssigningAuthority)
        .join(AssigningAuthority, PatientIdentity.assigning_authority_id == AssigningAuthority.id)
        .where(PatientIdentity.patient_id == patient_id)
    ).all()
    
    return [
        PatientIdentityResult(
            assigning_authority_oid=aa.oid,
            assigning_authority_name=aa.name,
            identifier=pi.identifier,
            id_type=pi.id_type,
            is_primary=pi.is_primary,
        )
        for pi, aa in identities
    ]


def pdq_query_by_identifier(
    assigning_authority_oid: str,
    identifier: str,
) -> list[PatientDemographics]:
    """PDQ Query: find patient demographics for a given (AA, identifier) pair.
    
    Returns full demographics for the matched patient including all known IDs.
    """
    with SyncSessionLocal() as session:
        aa = session.execute(
            select(AssigningAuthority).where(AssigningAuthority.oid == assigning_authority_oid)
        ).scalar_one_or_none()
        if aa is None:
            return []

        identity = session.execute(
            select(PatientIdentity).where(
                PatientIdentity.assigning_authority_id == aa.id,
                PatientIdentity.identifier == identifier,
            )
        ).scalar_one_or_none()
        if identity is None:
            return []

        return [_build_demographics(session, identity.patient_id)]


def pdq_query_by_demographics(
    patient_name: str | None = None,
    birth_date: str | None = None,
    sex: str | None = None,
    assigning_authority_oid: str | None = None,
) -> list[PatientDemographics]:
    """PDQ Query by demographics (fuzzy match).
    
    Returns full demographics for all matching patients.
    Optionally filter by assigning authority.
    """
    with SyncSessionLocal() as session:
        stmt = select(Patient)
        if patient_name:
            stmt = stmt.where(Patient.patient_name.ilike(f"%{patient_name}%"))
        if birth_date:
            stmt = stmt.where(Patient.patient_birth_date == birth_date)
        if sex:
            stmt = stmt.where(Patient.patient_sex == sex)
        
        patients = session.execute(stmt.limit(50)).scalars().all()
        results = []
        for patient in patients:
            # If AA filter specified, only include patients with an identity in that AA
            if assigning_authority_oid:
                aa = session.execute(
                    select(AssigningAuthority).where(AssigningAuthority.oid == assigning_authority_oid)
                ).scalar_one_or_none()
                if aa:
                    has_identity = session.execute(
                        select(PatientIdentity.id).where(
                            PatientIdentity.patient_id == patient.id,
                            PatientIdentity.assigning_authority_id == aa.id,
                        )
                    ).scalar_one_or_none()
                    if not has_identity:
                        continue
            results.append(_build_demographics(session, patient.id))
        return results


def _build_demographics(session, patient_id: int) -> PatientDemographics:
    """Build full demographics object for a patient."""
    patient = session.get(Patient, patient_id)
    if not patient:
        raise ValueError(f"Patient {patient_id} not found")
    
    identities = _get_all_identities_for_patient(session, patient_id)
    
    return PatientDemographics(
        patient_id=patient.id,
        identities=identities,
        patient_name=patient.patient_name,
        birth_date=patient.patient_birth_date,
        sex=patient.patient_sex,
        phone=patient.patient_phone,
        source=patient.source,
    )


def handle_adt_feed_patient_registration(data: dict) -> Patient | None:
    """Process an ADT^A04/A08/A40 message as a Patient Identity Feed.
    
    This is the PIX Manager's feed interface. It creates/updates the patient
    and their identities in the specified assigning authorities.
    
    Expected data keys (from HL7 parser):
      - patient_id, issuer (AA OID), name, birth_date, sex, phone
      - Optional: additional_identities = list of (aa_oid, identifier, id_type)
    """
    if not data.get("patient_id") or not data.get("issuer"):
        log.warning("PIX Feed: missing patient_id or issuer (AA OID)")
        return None
    
    with SyncSessionLocal() as session:
        # Find or create patient (same logic as HL7 listener)
        patient = session.execute(
            select(Patient).where(Patient.patient_id == data["patient_id"])
        ).scalar_one_or_none()
        
        if patient is None:
            patient = Patient(patient_id=data["patient_id"])
            session.add(patient)
            session.flush()
            log.info("PIX Feed: new patient %s", data["patient_id"])
        
        # Apply demographics (HIS is authoritative)
        patient.source = "his"
        if data.get("name"):
            patient.patient_name = data["name"]
        if data.get("birth_date"):
            patient.patient_birth_date = data["birth_date"]
        if data.get("sex"):
            patient.patient_sex = data["sex"]
        if data.get("phone"):
            patient.patient_phone = data["phone"]
        
        # Upsert primary identity
        upsert_patient_identity(
            patient.id,
            data["issuer"],
            data["patient_id"],
            id_type="MR",
            is_primary=True,
        )
        
        # Handle additional identities if provided
        for add_id in data.get("additional_identities", []):
            upsert_patient_identity(
                patient.id,
                add_id["aa_oid"],
                add_id["identifier"],
                id_type=add_id.get("id_type", "MR"),
                is_primary=add_id.get("is_primary", False),
            )
        
        session.commit()
        session.refresh(patient)
        return patient


def handle_adt_feed_patient_merge(data: dict) -> bool:
    """Process an ADT^A40 (patient merge) message.
    
    Merges two patient records: source_patient_id -> target_patient_id.
    All identities, studies, etc. are moved to target.
    """
    source_id = data.get("source_patient_id")
    target_id = data.get("target_patient_id")
    if not source_id or not target_id:
        return False
    
    with SyncSessionLocal() as session:
        source = session.execute(
            select(Patient).where(Patient.patient_id == source_id)
        ).scalar_one_or_none()
        target = session.execute(
            select(Patient).where(Patient.patient_id == target_id)
        ).scalar_one_or_none()
        
        if not source or not target:
            log.warning("PIX Feed A40: source or target patient not found")
            return False
        
        # Move all identities to target
        for identity in source.identities:
            # Check for conflict
            existing = session.execute(
                select(PatientIdentity).where(
                    PatientIdentity.patient_id == target.id,
                    PatientIdentity.assigning_authority_id == identity.assigning_authority_id,
                )
            ).scalar_one_or_none()
            if existing:
                # Keep existing, delete duplicate
                session.delete(identity)
            else:
                identity.patient_id = target.id
        
        # Move studies (will cascade due to FK)
        for study in source.studies:
            study.patient_id = target.id
        
        # Delete source patient
        session.delete(source)
        session.commit()
        log.info("PIX Feed A40: merged %s into %s", source_id, target_id)
        return True


__all__ = [
    "PatientIdentityResult",
    "PatientDemographics",
    "create_assigning_authority",
    "get_assigning_authority",
    "list_assigning_authorities",
    "upsert_patient_identity",
    "delete_patient_identity",
    "pix_query_by_identifier",
    "pix_query_by_demographics",
    "pdq_query_by_identifier",
    "pdq_query_by_demographics",
    "handle_adt_feed_patient_registration",
    "handle_adt_feed_patient_merge",
]
