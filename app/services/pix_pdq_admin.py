"""PIX/PDQ *management* API services (Q-3).

The PIX Manager primitives — identity feed, cross-reference queries and the A40
merge — live in :mod:`app.services.pix_pdq_service`. This module is the REST
*management* face: assigning-authority CRUD and patient-identity listings. It was
split out (2026-10-22) so the protocol module stays under the module ceiling and
so the API-shaped return types (``AssigningAuthorityEntry`` /
``PatientIdentityEntry``) sit next to the functions that build them.

Same session model as the protocol module: sync ``SyncSessionLocal``, driven
from the router through ``asyncio.to_thread`` — the HL7 listener shares this
code, and a second async dialect of it would drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, or_, select

from app.db.models import AssigningAuthority, Patient, PatientIdentity
from app.db.session import SyncSessionLocal
from app.services.pix_pdq_service import authority_by_oid, upsert_identity_row
from app.services import query_service


@dataclass
class AssigningAuthorityEntry:
    """An Assigning Authority as the management API returns it.

    ``created_at`` is pre-formatted here so the routing layer needs neither the
    ORM model nor a timestamp-formatting rule of its own.
    """
    id: int
    oid: str
    name: str
    is_authoritative: bool
    description: str | None
    created_at: str | None


@dataclass
class PatientIdentityEntry:
    """An identity row as the management API returns it.

    ``PatientIdentityResult`` is the PIX answer (identity data only); the admin
    list also has to name the row and its patient, so the two are kept separate
    rather than widening the query response with internal primary keys.
    """
    id: int
    patient_id: int
    assigning_authority_oid: str
    assigning_authority_name: str
    identifier: str
    id_type: str
    is_primary: bool


def register_patient_identity(
    patient_id: int,
    assigning_authority_oid: str,
    identifier: str,
    id_type: str = "MR",
    is_primary: bool = True,
) -> PatientIdentityEntry:
    """Upsert an identity and return it with its authority (Q-3).

    Same operation as :func:`upsert_patient_identity`, shaped for the REST
    handler: one session, one commit, and the authority's OID/name read in the
    same transaction so the response needs no second lookup.
    """
    with SyncSessionLocal() as session:
        aa = authority_by_oid(session, assigning_authority_oid)
        if aa is None:
            raise ValueError(f"Unknown assigning authority OID: {assigning_authority_oid}")
        identity, _created = upsert_identity_row(
            session, patient_id, aa, identifier, id_type, is_primary
        )
        session.commit()
        session.refresh(identity)
        return PatientIdentityEntry(
            id=identity.id,
            patient_id=identity.patient_id,
            assigning_authority_oid=aa.oid,
            assigning_authority_name=aa.name,
            identifier=identity.identifier,
            id_type=identity.id_type,
            is_primary=identity.is_primary,
        )


def _authority_entry(aa: AssigningAuthority) -> AssigningAuthorityEntry:
    """Project an authority row onto the API shape (created_at pre-formatted)."""
    return AssigningAuthorityEntry(
        id=aa.id,
        oid=aa.oid,
        name=aa.name,
        is_authoritative=aa.is_authoritative,
        description=aa.description,
        created_at=aa.created_at.isoformat() if aa.created_at else None,
    )


def register_assigning_authority(oid: str, name: str, is_authoritative: bool = False,
                                 description: str | None = None,
                                 ) -> AssigningAuthorityEntry | None:
    """Register an authority; **None** when the OID is already taken (Q-3).

    The existence check and the insert share one transaction, so two concurrent
    admins registering the same OID cannot both pass the check. The row is
    refreshed after the commit because ``expire_on_commit`` would otherwise
    leave it detached with no loaded ``id``/``created_at`` for the response.
    """
    with SyncSessionLocal() as session:
        if authority_by_oid(session, oid) is not None:
            return None
        aa = AssigningAuthority(
            oid=oid, name=name, is_authoritative=is_authoritative,
            description=description,
        )
        session.add(aa)
        session.commit()
        session.refresh(aa)
        return _authority_entry(aa)


def get_assigning_authority_entry(oid: str) -> AssigningAuthorityEntry | None:
    """One authority in API shape, or None (Q-3)."""
    with SyncSessionLocal() as session:
        aa = authority_by_oid(session, oid)
        return None if aa is None else _authority_entry(aa)


def list_assigning_authority_entries() -> list[AssigningAuthorityEntry]:
    """Every registered authority in API shape, unordered as stored (Q-3)."""
    with SyncSessionLocal() as session:
        rows = session.execute(select(AssigningAuthority)).scalars().all()
        return [_authority_entry(aa) for aa in rows]


def delete_assigning_authority(oid: str) -> bool:
    """Delete an authority by OID; False when there is no such row (Q-3)."""
    with SyncSessionLocal() as session:
        aa = authority_by_oid(session, oid)
        if aa is None:
            return False
        session.delete(aa)
        session.commit()
        return True


def list_patient_identity_entries(
    patient_id: int,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[PatientIdentityEntry] | None:
    """Every identity of a patient, or **None** when the patient does not exist.

    ``None`` and ``[]`` are different answers: the handler turns the first into
    a 404 and the second into an empty 200.
    """
    with SyncSessionLocal() as session:
        patient_scope = query_service.tenant_predicate(Patient.tenant_id, tenant_id, include_all)
        identity_scope = query_service.tenant_predicate(
            PatientIdentity.tenant_id, tenant_id, include_all
        )
        patient_stmt = select(Patient).where(Patient.id == patient_id)
        if patient_scope is not None:
            patient_stmt = patient_stmt.where(patient_scope)
        if session.execute(patient_stmt).scalar_one_or_none() is None:
            return None
        stmt = (
            select(PatientIdentity, AssigningAuthority)
            .join(AssigningAuthority,
                  PatientIdentity.assigning_authority_id == AssigningAuthority.id)
            .join(Patient, Patient.id == PatientIdentity.patient_id)
            .where(PatientIdentity.patient_id == patient_id,
                   or_(
                       PatientIdentity.tenant_id == Patient.tenant_id,
                       and_(
                           query_service.tenant_predicate(
                               PatientIdentity.tenant_id, None, False
                           ),
                           query_service.tenant_predicate(Patient.tenant_id, None, False),
                       ),
                   ))
        )
        if identity_scope is not None:
            stmt = stmt.where(identity_scope)
        rows = session.execute(stmt).all()
        return [
            PatientIdentityEntry(
                id=pi.id,
                patient_id=pi.patient_id,
                assigning_authority_oid=aa.oid,
                assigning_authority_name=aa.name,
                identifier=pi.identifier,
                id_type=pi.id_type,
                is_primary=pi.is_primary,
            )
            for pi, aa in rows
        ]


__all__ = [
    "AssigningAuthorityEntry",
    "PatientIdentityEntry",
    "delete_assigning_authority",
    "get_assigning_authority_entry",
    "list_assigning_authority_entries",
    "list_patient_identity_entries",
    "register_assigning_authority",
    "register_patient_identity",
]
