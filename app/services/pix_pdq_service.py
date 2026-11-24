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

A-10 / FD-3 — A40 merges are non-destructive and tenant-scoped
-------------------------------------------------------------
An ADT^A40 says "this patient was registered twice": PID-3 names the **survivor**
and MRG-1 names the record being **subsumed**. The first implementation of
:func:`handle_adt_feed_patient_merge` got three things wrong, and each one can
destroy medical records:

1. it looked both MRNs up **globally** (``Patient.patient_id == mrn`` with no
   tenant scope), so when two institutions happened to use the same MRN the merge
   matched whichever row the database returned first — a cross-tenant merge, and
   the cross-tenant PHI corruption the VNA work exists to prevent;
2. it re-pointed ``study.patient_id`` and then called ``session.delete(source)``.
   ``Patient.studies`` is ``cascade="all, delete-orphan"`` and ``studies.patient_id``
   is ``ON DELETE CASCADE``, so **any study the ORM still considered a child of the
   source was physically deleted** — the image rows and their objects gone, not
   merely unlinked;
3. it left no record of what had pointed where, so a bad merge was undetectable.

The rules the implementation now follows:

* **Strict scope.** Both MRNs must resolve inside the caller's tenant scope
  (``data["tenant_id"]``; ``None`` is the legacy/unscoped bucket the HL7 listener
  operates in — see ``app/dicom/hl7.py:_ensure_patient``). An MRN that resolves
  only *outside* that scope is a cross-tenant match: the merge is refused and an
  audit alert is written. Nothing in the other tenant is read, written or moved.
* **Nothing is ever deleted.** Not a study, not an image row, not a patient
  identity, not the subsumed patient. Studies are re-linked with a single bulk
  ``UPDATE``, so the ORM's ``delete-orphan`` cascade is never in play.
* **Audited.** Every re-linked study gets an ``AuditLog`` row recording the
  previous patient link as internal primary keys — never the MRN, which would be
  PHI in a log (AGENTS.md N-2).
* **Archival and deprecation are opt-in and still non-destructive.** Neither
  happens unless the message carries the explicit confirmation flag, and a merge
  never archives anything: moving objects is ``lifecycle_service``'s job, behind
  its own destructive-role gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import AssigningAuthority, Patient, PatientIdentity, Study
from app.db.session import SyncSessionLocal
from app.services import audit_service, query_service

log = logging.getLogger("paxs.pix_pdq")

# --------------------------------------------------------------------------- #
# A-10 / FD-3 — audit actions for the A40 merge.
#
# `audit_log.action` is String(32): every value here must fit, or MySQL truncates
# (SQLite would not complain, which is exactly how that class of bug ships).
# --------------------------------------------------------------------------- #
MERGE_ACTION_MERGED = "PATIENT_MERGE"
MERGE_ACTION_RELINKED = "PATIENT_MERGE_RELINKED"
MERGE_ACTION_REJECTED = "PATIENT_MERGE_REJECTED"
MERGE_ACTION_UNKNOWN = "PATIENT_MERGE_UNKNOWN"
MERGE_ACTION_REFUSED = "PATIENT_MERGE_REFUSED"
MERGE_ACTION_DEPRECATED = "PATIENT_MERGE_DEPRECATED"

#: Detail tokens written to ``audit_log.performed_by``. They describe *why* a
#: merge was refused and must stay PHI-free: tenant scopes, internal primary keys
#: and counts only, never an MRN or a name.
DETAIL_CROSS_TENANT = "cross_tenant_mrn"
DETAIL_UNKNOWN_PATIENT = "patient_not_in_scope"
DETAIL_ARCHIVE_UNCONFIRMED = "archive_requested_without_confirmation"
DETAIL_ARCHIVE_OUT_OF_SCOPE = "archive_confirmed_but_out_of_scope_use_lifecycle_service"
DETAIL_DEPRECATE_UNCONFIRMED = "deprecate_requested_without_confirmation"


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


def authority_by_oid(session: Session, oid: str) -> AssigningAuthority | None:
    """The Assigning Authority registered for ``oid`` (no row -> ``None``)."""
    return session.execute(
        select(AssigningAuthority).where(AssigningAuthority.oid == oid)
    ).scalar_one_or_none()


def upsert_identity_row(session: Session, patient_id: int, aa: AssigningAuthority,
                     identifier: str, id_type: str,
                     is_primary: bool) -> tuple[PatientIdentity, bool]:
    """Create or update one (patient, authority) identity. Returns (row, created).

    Split out of :func:`upsert_patient_identity` so the management API can
    return the row *and* its authority from a single transaction instead of
    re-reading the AA in a second session.
    """
    patient = session.get(Patient, patient_id)
    if patient is None:
        raise ValueError(f"Unknown patient ID: {patient_id}")
    existing = session.execute(
        select(PatientIdentity).where(
            PatientIdentity.patient_id == patient_id,
            PatientIdentity.assigning_authority_id == aa.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.tenant_id = patient.tenant_id
        existing.identifier = identifier
        existing.id_type = id_type
        existing.is_primary = is_primary
        return existing, False
    identity = PatientIdentity(
        patient_id=patient_id,
        tenant_id=patient.tenant_id,
        assigning_authority_id=aa.id,
        identifier=identifier,
        id_type=id_type,
        is_primary=is_primary,
    )
    session.add(identity)
    return identity, True


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
        aa = authority_by_oid(session, assigning_authority_oid)
        if aa is None:
            raise ValueError(f"Unknown assigning authority OID: {assigning_authority_oid}")
        identity, created = upsert_identity_row(
            session, patient_id, aa, identifier, id_type, is_primary
        )
        session.commit()
        session.refresh(identity)
        log.info(
            "patient_identity_%s",
            "created" if created else "updated",
        )
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


def _upsert_adt_feed_identities(session: Session, patient: Patient, data: dict) -> None:
    """Upsert all ADT identities in the patient registration transaction."""
    identity_specs = [(data["issuer"], data["patient_id"], "MR", True)]
    identity_specs.extend(
        (
            item["aa_oid"],
            item["identifier"],
            item.get("id_type", "MR"),
            item.get("is_primary", False),
        )
        for item in data.get("additional_identities", [])
    )
    for authority_oid, identifier, id_type, is_primary in identity_specs:
        authority = authority_by_oid(session, authority_oid)
        if authority is None:
            raise ValueError("Unknown assigning authority")
        upsert_identity_row(
            session, patient.id, authority, identifier, id_type, is_primary
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
        tenant_id = data.get("tenant_id")
        scope = query_service.tenant_predicate(Patient.tenant_id, tenant_id, False)
        patient = session.execute(
            select(Patient).where(Patient.patient_id == data["patient_id"], scope)
        ).scalar_one_or_none()
        
        if patient is None:
            patient = Patient(patient_id=data["patient_id"], tenant_id=tenant_id)
            session.add(patient)
            session.flush()
            log.info("PIX Feed: new patient registered")
        
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
        
        # One transaction prevents a patient row from committing without its IDs.
        _upsert_adt_feed_identities(session, patient, data)
        
        session.commit()
        session.refresh(patient)
        return patient


def _alert(session: Session, action: str, actor: str, detail: str) -> None:
    """Record a merge decision in the audit trail and log it PHI-free (A-10).

    ``detail`` is a reason token plus non-PHI context (tenant scope, internal
    primary keys, counts). An MRN or a patient name must never reach this call —
    ``audit_log`` is not access-controlled the way the database is (N-2).
    """
    audit_service.add_audit(
        session,
        sop_instance_uid=None,
        action=action,
        performed_by=f"{actor} {detail}"[:128],
    )
    log.error("pix_a40 %s %s", action.lower(), detail)


def _refuse_unsafe_merge_options(session: Session, data: dict, actor: str) -> bool:
    """True when the message asks for something a merge must not do (A-10/FD-3).

    Archival and deprecation are destructive-capable, so neither is ever implicit:
    without the explicit confirmation flag the whole merge is refused, and with
    the flag an *archival* is still not performed here — archiving moves objects
    and belongs to ``lifecycle_service`` behind its own destructive-role gate.
    A confirmed *deprecation* is recorded by the caller; it never deletes the
    subsumed row.
    """
    if data.get("archive_source"):
        confirmed = bool(data.get("confirm_archive_source"))
        _alert(session, MERGE_ACTION_REFUSED, actor,
               DETAIL_ARCHIVE_OUT_OF_SCOPE if confirmed else DETAIL_ARCHIVE_UNCONFIRMED)
        return True
    if data.get("deprecate_source") and not data.get("confirm_deprecate_source"):
        _alert(session, MERGE_ACTION_REFUSED, actor, DETAIL_DEPRECATE_UNCONFIRMED)
        return True
    return False


def _find_merge_patient(session: Session, mrn: str,
                        tenant_id: int | None) -> tuple[Patient | None, bool]:
    """``(patient inside the caller's scope, whether the MRN also exists outside it)``.

    The scope is applied with the canonical :func:`query_service.tenant_predicate`.
    "Also exists outside" is derived by comparing the total row count for the MRN
    against the in-scope count, rather than by writing a second, inverted tenant
    comparison — FA-1 exists precisely because two hand-written predicates drift
    apart, and a source-grep test fails the build if a second one appears.
    """
    scope = query_service.tenant_predicate(Patient.tenant_id, tenant_id, False)
    in_scope = session.execute(
        select(Patient).where(Patient.patient_id == mrn, scope)
    ).scalars().all()
    total = session.execute(
        select(func.count(Patient.id)).where(Patient.patient_id == mrn)
    ).scalar_one()
    return (in_scope[0] if in_scope else None), total > len(in_scope)


def _resolve_merge_pair(session: Session, source_mrn: str, target_mrn: str,
                        tenant_id: int | None, actor: str) -> tuple[Patient, Patient] | None:
    """Both patients inside the caller's scope, or None after writing the alert.

    Distinguishes "this MRN is not ours" (cross-tenant — the A-10 hazard) from
    "this MRN is unknown here", because they need different operator responses
    and only the first is a security event.
    """
    source, source_outside = _find_merge_patient(session, source_mrn, tenant_id)
    target, target_outside = _find_merge_patient(session, target_mrn, tenant_id)
    if source is not None and target is not None:
        return source, target
    cross_tenant = (source is None and source_outside) or (target is None and target_outside)
    detail = DETAIL_CROSS_TENANT if cross_tenant else DETAIL_UNKNOWN_PATIENT
    _alert(session, MERGE_ACTION_REJECTED if cross_tenant else MERGE_ACTION_UNKNOWN,
           actor, f"{detail} tenant_scope={tenant_id}")
    return None


def _relink_studies(session: Session, source: Patient, target: Patient,
                    tenant_id: int | None, actor: str) -> tuple[int, int]:
    """Re-point the subsumed patient's studies at the survivor. **Never deletes.**

    A single bulk ``UPDATE`` is used deliberately. Iterating ``source.studies``
    and re-pointing the ORM objects would leave them in the source's collection,
    where ``cascade="all, delete-orphan"`` treats them as orphans — which is how
    the previous implementation destroyed them once it deleted the source row.

    Studies are also filtered by ``Study.tenant_id``: a study owned by another
    tenant is left exactly where it is, so a merge can never move another
    institution's imaging. Returns ``(relinked, left_alone)``.
    """
    scope = query_service.tenant_predicate(Study.tenant_id, tenant_id, False)
    # Counted *before* the UPDATE: afterwards the re-linked studies no longer
    # point at the source, so the remainder would always look like zero.
    total = session.execute(
        select(func.count(Study.id)).where(Study.patient_id == source.id)
    ).scalar_one()
    uids = session.execute(
        select(Study.study_instance_uid).where(Study.patient_id == source.id, scope)
    ).scalars().all()

    if uids:
        session.execute(
            update(Study)
            .where(Study.patient_id == source.id, scope)
            .values(patient_id=target.id)
            .execution_options(synchronize_session=False)
        )
        for uid in uids:
            # The audit trail of the *previous* link: internal primary keys, so
            # the record is complete without putting an MRN in the log (N-2).
            audit_service.add_audit(
                session,
                sop_instance_uid=None,
                study_instance_uid=uid,
                action=MERGE_ACTION_RELINKED,
                performed_by=f"{actor} src_patient_pk={source.id} dst_patient_pk={target.id}",
            )

    return len(uids), total - len(uids)

def handle_adt_feed_patient_merge(data: dict) -> bool:
    """Process an ADT^A40 (patient merge) — tenant-scoped and non-destructive.

    ``source_patient_id`` is the MRN being subsumed (MRG-1), ``target_patient_id``
    the survivor (PID-3), and ``tenant_id`` the caller's scope (``None`` = the
    legacy/unscoped bucket the HL7 listener operates in). See the module docstring
    for the A-10 / FD-3 rules. Returns True only when the merge was applied; every
    refusal writes an audit row, so a rejected A40 is never silent.
    """
    actor = str(data.get("performed_by") or "hl7-pix-listener")
    tenant_id = data.get("tenant_id")
    source_mrn = str(data.get("source_patient_id") or "").strip()
    target_mrn = str(data.get("target_patient_id") or "").strip()

    if not source_mrn or not target_mrn:
        log.warning("pix_a40_missing_mrn tenant_scope=%s", tenant_id)
        return False
    if source_mrn == target_mrn:
        log.warning("pix_a40_source_equals_target tenant_scope=%s", tenant_id)
        return False

    relinked = left_alone = 0
    with SyncSessionLocal() as session:
        try:
            if _refuse_unsafe_merge_options(session, data, actor):
                session.commit()
                return False

            pair = _resolve_merge_pair(session, source_mrn, target_mrn, tenant_id, actor)
            if pair is None:
                session.commit()
                return False
            source, target = pair

            relinked, left_alone = _relink_studies(session, source, target, tenant_id, actor)
            audit_service.add_audit(
                session, sop_instance_uid=None, action=MERGE_ACTION_MERGED,
                performed_by=f"{actor} src_patient_pk={source.id} dst_patient_pk={target.id} "
                             f"relinked={relinked} left_alone={left_alone}",
            )
            if data.get("deprecate_source"):
                # A confirmed deprecation is *recorded*, never applied by deleting
                # the subsumed row: keeping that record is the point of a merge.
                audit_service.add_audit(
                    session, sop_instance_uid=None, action=MERGE_ACTION_DEPRECATED,
                    performed_by=f"{actor} src_patient_pk={source.id}",
                )
            session.commit()
        except Exception as exc:  # one bad A40 must not kill the listener thread
            session.rollback()
            log.error("pix_a40_merge_failed error_type=%s tenant_scope=%s",
                      type(exc).__name__, tenant_id)
            return False

    log.info("pix_a40_merged relinked=%d left_alone=%d tenant_scope=%s",
             relinked, left_alone, tenant_id)
    return True


__all__ = [
    "PatientDemographics",
    "PatientIdentityResult",
    "authority_by_oid",
    "create_assigning_authority",
    "delete_patient_identity",
    "get_assigning_authority",
    "handle_adt_feed_patient_merge",
    "handle_adt_feed_patient_registration",
    "list_assigning_authorities",
    "pdq_query_by_demographics",
    "pdq_query_by_identifier",
    "pix_query_by_demographics",
    "pix_query_by_identifier",
    "upsert_identity_row",
    "upsert_patient_identity",
]
