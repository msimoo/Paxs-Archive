"""Tenant-scoped PIX/PDQ REST queries.

HL7 QBP and legacy protocol helpers have separate contracts. These functions
scope browser/API reads from the authenticated user's tenant and require the
patient and identity rows to agree on that scope before returning PHI.
"""

from __future__ import annotations

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import AssigningAuthority, Patient, PatientIdentity
from app.db.session import SyncSessionLocal
from app.services import query_service
from app.services.pix_pdq_service import (
    PatientDemographics,
    PatientIdentityResult,
)

MAX_PATIENT_RESULTS = 50


def _patient_scope(stmt: Select, tenant_id: int | None, include_all: bool) -> Select:
    """Restrict a patient query to the caller's tenant or legacy NULL bucket."""
    if include_all:
        return stmt
    predicate = query_service.tenant_predicate(Patient.tenant_id, tenant_id, False)
    return stmt.where(predicate) if predicate is not None else stmt


def _identity_scope(
    tenant_id: int | None, include_all: bool
) -> tuple[ColumnElement[bool], ...]:
    """Require identity ownership to match the patient's authorized scope."""
    if include_all:
        return (or_(
            PatientIdentity.tenant_id == Patient.tenant_id,
            and_(
                query_service.tenant_predicate(PatientIdentity.tenant_id, None, False),
                query_service.tenant_predicate(Patient.tenant_id, None, False),
            ),
        ),)
    return (
        query_service.tenant_predicate(Patient.tenant_id, tenant_id, False),
        query_service.tenant_predicate(PatientIdentity.tenant_id, tenant_id, False),
    )


def _identities_for_patient(
    patient_id: int, tenant_id: int | None, include_all: bool
) -> list[PatientIdentityResult]:
    """Return only identity rows consistent with the caller's patient scope."""
    with SyncSessionLocal() as session:
        stmt = (
            select(PatientIdentity, AssigningAuthority)
            .join(AssigningAuthority, PatientIdentity.assigning_authority_id == AssigningAuthority.id)
            .join(Patient, Patient.id == PatientIdentity.patient_id)
            .where(Patient.id == patient_id, *_identity_scope(tenant_id, include_all))
        )
        return [
            PatientIdentityResult(aa.oid, aa.name, identity.identifier,
                                  identity.id_type, identity.is_primary)
            for identity, aa in session.execute(stmt).all()
        ]


def _matching_patients(
    session: Session,
    tenant_id: int | None,
    include_all: bool,
    patient_name: str | None,
    birth_date: str | None,
    sex: str | None,
) -> list[Patient]:
    """Find a bounded set of tenant-scoped patients from demographic criteria."""
    stmt = _patient_scope(select(Patient), tenant_id, include_all)
    if patient_name:
        literal_name = patient_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Patient.patient_name.ilike(f"%{literal_name}%", escape="\\"))
    if birth_date:
        stmt = stmt.where(Patient.patient_birth_date == birth_date)
    if sex:
        stmt = stmt.where(Patient.patient_sex == sex)
    return list(
        session.execute(stmt.order_by(Patient.id).limit(MAX_PATIENT_RESULTS)).scalars().all()
    )


def _patient_for_identifier(
    session: Session,
    assigning_authority_oid: str,
    identifier: str,
    tenant_id: int | None,
    include_all: bool,
) -> Patient | None:
    """Resolve one matching patient while checking both ownership columns."""
    stmt = (
        select(Patient)
        .join(PatientIdentity, PatientIdentity.patient_id == Patient.id)
        .join(AssigningAuthority, AssigningAuthority.id == PatientIdentity.assigning_authority_id)
        .where(
            AssigningAuthority.oid == assigning_authority_oid,
            PatientIdentity.identifier == identifier,
            *_identity_scope(tenant_id, include_all),
        )
    )
    return session.execute(stmt.distinct().limit(1)).scalars().first()


def pix_query(
    assigning_authority_oid: str | None,
    identifier: str | None,
    patient_name: str | None,
    birth_date: str | None,
    sex: str | None,
    tenant_id: int | None,
    include_all: bool,
) -> list[PatientIdentityResult]:
    """Run a tenant-scoped PIX query and return identities for matched patients."""
    with SyncSessionLocal() as session:
        if assigning_authority_oid and identifier:
            patients = [_patient_for_identifier(
                session, assigning_authority_oid, identifier, tenant_id, include_all
            )]
        else:
            patients = _matching_patients(
                session, tenant_id, include_all, patient_name, birth_date, sex
            )
        patient_ids = [patient.id for patient in patients if patient is not None]
    return [
        identity
        for patient_id in patient_ids
        for identity in _identities_for_patient(patient_id, tenant_id, include_all)
    ]


def _demographics(
    patient_id: int, tenant_id: int | None, include_all: bool
) -> PatientDemographics | None:
    """Build demographics only for a patient still visible within the scope."""
    with SyncSessionLocal() as session:
        stmt = _patient_scope(
            select(Patient).where(Patient.id == patient_id), tenant_id, include_all
        )
        patient = session.execute(stmt).scalar_one_or_none()
        if patient is None:
            return None
        identities_stmt = (
            select(PatientIdentity, AssigningAuthority)
            .join(AssigningAuthority, PatientIdentity.assigning_authority_id == AssigningAuthority.id)
            .join(Patient, Patient.id == PatientIdentity.patient_id)
            .where(Patient.id == patient_id, *_identity_scope(tenant_id, include_all))
        )
        identities = [
            PatientIdentityResult(aa.oid, aa.name, identity.identifier,
                                  identity.id_type, identity.is_primary)
            for identity, aa in session.execute(identities_stmt).all()
        ]
        return PatientDemographics(
            patient.id, identities, patient.patient_name, patient.patient_birth_date,
            patient.patient_sex, patient.patient_phone, patient.source,
        )


def pdq_query(
    assigning_authority_oid: str | None,
    identifier: str | None,
    patient_name: str | None,
    birth_date: str | None,
    sex: str | None,
    tenant_id: int | None,
    include_all: bool,
) -> list[PatientDemographics]:
    """Run a tenant-scoped PDQ query with an explicit maximum patient count."""
    with SyncSessionLocal() as session:
        if assigning_authority_oid and identifier:
            patient = _patient_for_identifier(
                session, assigning_authority_oid, identifier, tenant_id, include_all
            )
            patient_ids = [patient.id] if patient is not None else []
        else:
            patients = _matching_patients(
                session, tenant_id, include_all, patient_name, birth_date, sex
            )
            patient_ids = [patient.id for patient in patients]
            if assigning_authority_oid:
                allowed = session.execute(
                    select(PatientIdentity.patient_id)
                    .join(AssigningAuthority,
                          AssigningAuthority.id == PatientIdentity.assigning_authority_id)
                    .join(Patient, Patient.id == PatientIdentity.patient_id)
                    .where(
                        Patient.id.in_(patient_ids),
                        AssigningAuthority.oid == assigning_authority_oid,
                        *_identity_scope(tenant_id, include_all),
                    )
                ).scalars().all()
                patient_ids = list(dict.fromkeys(allowed))
    return [
        demographics
        for patient_id in patient_ids
        if (demographics := _demographics(patient_id, tenant_id, include_all)) is not None
    ]


def patient_identities(
    patient_id: int, tenant_id: int | None, include_all: bool
) -> list[PatientIdentityResult] | None:
    """List patient identities or return ``None`` when scope hides the patient."""
    with SyncSessionLocal() as session:
        patient = session.execute(_patient_scope(
            select(Patient.id).where(Patient.id == patient_id), tenant_id, include_all
        )).scalar_one_or_none()
    if patient is None:
        return None
    return _identities_for_patient(patient_id, tenant_id, include_all)


__all__ = ["patient_identities", "pdq_query", "pix_query"]
