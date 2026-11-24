"""Tenant-scoped PIX/PDQ query operations used only by inbound HL7 QBP."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import AssigningAuthority, Patient, PatientIdentity, Tenant
from app.db.session import SyncSessionLocal
from app.services.pix_pdq_service import PatientDemographics, PatientIdentityResult


def _hl7_matching_patients(
    session: Session,
    tenant_id: int,
    identifier: str | None,
    assigning_authority_oid: str | None,
    patient_name: str | None,
    birth_date: str | None,
    sex: str | None,
    phone: str | None,
) -> list[Patient]:
    """Find at most 50 patients inside a tenant using nonempty criteria."""
    if tenant_id <= 0 or (identifier and not assigning_authority_oid):
        raise ValueError("HL7 QBP requires a tenant and paired identity values")
    if not any((identifier, patient_name, birth_date, sex, phone)):
        raise ValueError("HL7 QBP requires an identifier or demographic criterion")
    stmt = select(Patient).where(Patient.tenant_id == tenant_id)
    if identifier and assigning_authority_oid:
        stmt = stmt.join(PatientIdentity, PatientIdentity.patient_id == Patient.id).join(
            AssigningAuthority, AssigningAuthority.id == PatientIdentity.assigning_authority_id
        ).where(
            PatientIdentity.tenant_id == tenant_id,
            PatientIdentity.identifier == identifier,
            AssigningAuthority.oid == assigning_authority_oid,
        )
    elif assigning_authority_oid:
        stmt = stmt.where(_tenant_has_authority(tenant_id, assigning_authority_oid))
    for column, value in (
        (Patient.patient_birth_date, birth_date),
        (Patient.patient_sex, sex),
        (Patient.patient_phone, phone),
    ):
        if value:
            stmt = stmt.where(column == value)
    if patient_name:
        literal_name = patient_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        name_pattern = literal_name.replace("^", "%")
        stmt = stmt.where(Patient.patient_name.ilike(f"%{name_pattern}%", escape="\\"))
    return session.execute(stmt.distinct().limit(50)).scalars().all()


def _tenant_has_authority(tenant_id: int, authority_oid: str) -> ColumnElement[bool]:
    """Correlated EXISTS clause restricting demographic queries to an AA."""
    return (
        select(PatientIdentity.id)
        .join(AssigningAuthority, AssigningAuthority.id == PatientIdentity.assigning_authority_id)
        .where(
            PatientIdentity.patient_id == Patient.id,
            PatientIdentity.tenant_id == tenant_id,
            AssigningAuthority.oid == authority_oid,
        )
        .exists()
    )


def _tenant_identities(
    session: Session, patient_id: int, tenant_id: int
) -> list[PatientIdentityResult]:
    """Return identities only when both identity and parent belong to tenant."""
    rows = session.execute(
        select(PatientIdentity, AssigningAuthority)
        .join(AssigningAuthority, PatientIdentity.assigning_authority_id == AssigningAuthority.id)
        .join(Patient, Patient.id == PatientIdentity.patient_id)
        .where(
            Patient.id == patient_id,
            Patient.tenant_id == tenant_id,
            PatientIdentity.tenant_id == tenant_id,
        )
    ).all()
    return [
        PatientIdentityResult(aa.oid, aa.name, identity.identifier,
                              identity.id_type, identity.is_primary)
        for identity, aa in rows
    ]


def hl7_pix_query(
    tenant_id: int,
    identifier: str | None = None,
    assigning_authority_oid: str | None = None,
    patient_name: str | None = None,
    birth_date: str | None = None,
    sex: str | None = None,
    phone: str | None = None,
) -> list[list[PatientIdentityResult]]:
    """Run a tenant-bound PIX query for a mapped HL7 sender."""
    with SyncSessionLocal() as session:
        patients = _hl7_matching_patients(
            session, tenant_id, identifier, assigning_authority_oid,
            patient_name, birth_date, sex, phone,
        )
        identities = [_tenant_identities(session, patient.id, tenant_id) for patient in patients]
        return [patient_ids for patient_ids in identities if patient_ids]


def hl7_pdq_query(
    tenant_id: int,
    identifier: str | None = None,
    assigning_authority_oid: str | None = None,
    patient_name: str | None = None,
    birth_date: str | None = None,
    sex: str | None = None,
    phone: str | None = None,
) -> list[PatientDemographics]:
    """Run a tenant-bound PDQ query without legacy or foreign identities."""
    with SyncSessionLocal() as session:
        patients = _hl7_matching_patients(
            session, tenant_id, identifier, assigning_authority_oid,
            patient_name, birth_date, sex, phone,
        )
        results = []
        for patient in patients:
            identities = _tenant_identities(session, patient.id, tenant_id)
            if identities:
                results.append(PatientDemographics(
                    patient.id, identities, patient.patient_name, patient.patient_birth_date,
                    patient.patient_sex, patient.patient_phone, patient.source,
                ))
        return results


def hl7_sender_tenant(
    sender_key: str, sender_tenant_map: dict[str, int]
) -> int | None:
    """Resolve an exact configured peer/MSH key to an active tenant."""
    tenant_id = sender_tenant_map.get(sender_key)
    if tenant_id is None or tenant_id <= 0:
        return None
    with SyncSessionLocal() as session:
        tenant = session.get(Tenant, tenant_id)
        return tenant.id if tenant is not None and tenant.is_active else None
