"""Modality Performed Procedure Step (MPPS) service.

The modality reports the real exam lifecycle over DIMSE N-CREATE / N-SET for
the Modality Performed Procedure Step SOP Class (1.2.840.10008.3.1.2.3.3).
This module owns the performed_procedure_steps rows and drives the A1 worklist
status machine with the scanner as source of truth:

  N-CREATE (IN PROGRESS)  -> performed step created, scheduled step -> IN PROGRESS
  N-SET   (COMPLETED)     -> performed step COMPLETED + study/series linked,
                             scheduled step COMPLETED (reuses complete_step)
  N-SET   (DISCONTINUED)  -> performed step DISCONTINUED, scheduled step
                             released back to SCHEDULED (re-bookable)

Handlers run on pynetdicom's own threads, so everything here is *sync* on a
SyncSessionLocal session — the same pattern as handle_find.
"""

from __future__ import annotations

import logging

from pydicom.dataset import Dataset
from sqlalchemy import exists, not_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, aliased

from app.db.models import Patient, PerformedProcedureStep, ScheduledProcedureStep, Study
from app.services import worklist_service
from app.services.query_service import apply_tenant_scope, tenant_predicate

log = logging.getLogger("paxs.mpps")

VALID_STATUS = ("IN PROGRESS", "COMPLETED", "DISCONTINUED")


def _performed_step_out(
    pps: PerformedProcedureStep,
    patient: Patient | None,
    scheduled_step: ScheduledProcedureStep | None,
) -> dict[str, object]:
    """Serialize a performed step after its tenant-scoped query."""
    return {
        "id": pps.id,
        "sop_instance_uid": pps.sop_instance_uid,
        "status": pps.status,
        "pps_id": pps.pps_id,
        "description": pps.pps_description,
        "modality": pps.modality,
        "performed_station_aet": pps.performed_station_aet,
        "accession_number": pps.accession_number,
        "start_date": pps.start_date,
        "start_time": pps.start_time,
        "end_date": pps.end_date,
        "end_time": pps.end_time,
        "study_instance_uid": pps.study_instance_uid,
        "series_instance_uid": pps.series_instance_uid,
        "patient_id": patient.patient_id if patient else None,
        "patient_name": patient.patient_name if patient else None,
        "scheduled_step_id": scheduled_step.sps_id if scheduled_step else None,
        "created_at": pps.created_at.isoformat() if pps.created_at else None,
        "updated_at": pps.updated_at.isoformat() if pps.updated_at else None,
    }


async def list_performed_steps(
    session: AsyncSession,
    status: str | None,
    tenant_id: int | None,
    include_all: bool,
) -> list[dict[str, object]]:
    """List MPPS rows within the caller's tenant, preserving ADMIN scope."""
    scheduled_patient = aliased(Patient)
    study_patient = aliased(Patient)
    statement = (
        select(PerformedProcedureStep, Patient, ScheduledProcedureStep)
        .outerjoin(Patient, PerformedProcedureStep.patient_id == Patient.id)
        .outerjoin(
            ScheduledProcedureStep,
            PerformedProcedureStep.scheduled_step_id == ScheduledProcedureStep.id,
        )
        .outerjoin(
            scheduled_patient,
            ScheduledProcedureStep.patient_id == scheduled_patient.id,
        )
    )
    statement = apply_tenant_scope(
        statement, PerformedProcedureStep.tenant_id, tenant_id, include_all
    )
    if not include_all:
        linked_patient_scope = tenant_predicate(Patient.tenant_id, tenant_id, False)
        statement = statement.where(
            or_(PerformedProcedureStep.patient_id.is_(None), linked_patient_scope)
        )
        statement = statement.where(
            not_(
                exists(
                    select(Study.id).where(
                        Study.study_instance_uid
                        == PerformedProcedureStep.study_instance_uid,
                    ).join(
                        study_patient, Study.patient_id == study_patient.id
                    ).where(
                        or_(
                            Study.tenant_id.is_distinct_from(tenant_id),
                            study_patient.tenant_id.is_distinct_from(tenant_id),
                        )
                    )
                )
            )
        )
        scheduled_patient_scope = tenant_predicate(
            scheduled_patient.tenant_id, tenant_id, False
        )
        statement = statement.where(
            or_(
                PerformedProcedureStep.scheduled_step_id.is_(None),
                ScheduledProcedureStep.id.is_(None),
                scheduled_patient_scope,
            ),
            not_(
                exists(
                    select(Study.id).where(
                        Study.study_instance_uid
                        == ScheduledProcedureStep.study_instance_uid,
                    ).join(
                        study_patient, Study.patient_id == study_patient.id
                    ).where(
                        or_(
                            Study.tenant_id.is_distinct_from(tenant_id),
                            study_patient.tenant_id.is_distinct_from(tenant_id),
                        )
                    )
                )
            ),
        )
    if status:
        statement = statement.where(PerformedProcedureStep.status == status)
    statement = statement.order_by(PerformedProcedureStep.id.desc()).limit(100)
    rows = (await session.execute(statement)).all()
    return [
        _performed_step_out(pps, patient, scheduled_step)
        for pps, patient, scheduled_step in rows
    ]


def _str(ds: Dataset, kw: str) -> str | None:
    if not hasattr(ds, kw):
        return None
    value = getattr(ds, kw)
    text = str(value).strip()
    return text or None


def _open_step_by_sps_id(
    session: Session,
    sps_id: str,
    tenant_id: int | None,
) -> ScheduledProcedureStep | None:
    """Find an open exact-SPS match only through a patient in this tenant."""
    scope = tenant_predicate(Patient.tenant_id, tenant_id, False)
    step = session.execute(
        select(ScheduledProcedureStep)
        .join(Patient, ScheduledProcedureStep.patient_id == Patient.id)
        .where(
            ScheduledProcedureStep.sps_id == sps_id,
            ScheduledProcedureStep.status.in_(("SCHEDULED", "IN PROGRESS")),
            scope,
        )
    ).scalars().first()
    return step if step and _study_is_in_scope(session, step.study_instance_uid, tenant_id) else None


def _open_step_by_accession(
    session: Session,
    accession: str,
    tenant_id: int | None,
) -> ScheduledProcedureStep | None:
    """Find an open accession match only through a patient in this tenant."""
    scope = tenant_predicate(Patient.tenant_id, tenant_id, False)
    step = session.execute(
        select(ScheduledProcedureStep)
        .join(Patient, ScheduledProcedureStep.patient_id == Patient.id)
        .where(
            ScheduledProcedureStep.status.in_(("SCHEDULED", "IN PROGRESS")),
            ScheduledProcedureStep.accession_number == accession,
            scope,
        )
    ).scalars().first()
    return step if step and _study_is_in_scope(session, step.study_instance_uid, tenant_id) else None


def _open_step_by_patient(
    session: Session,
    patient: Patient,
    modality: str | None,
    tenant_id: int | None,
) -> ScheduledProcedureStep | None:
    """Find the earliest open step for an already scoped patient and modality."""
    scope = tenant_predicate(Patient.tenant_id, tenant_id, False)
    statement = (
        select(ScheduledProcedureStep)
        .join(Patient, ScheduledProcedureStep.patient_id == Patient.id)
        .where(ScheduledProcedureStep.patient_id == patient.id, scope)
        .where(ScheduledProcedureStep.status.in_(("SCHEDULED", "IN PROGRESS")))
        .order_by(ScheduledProcedureStep.created_at)
    )
    if modality:
        statement = statement.where(
            or_(ScheduledProcedureStep.modality.is_(None), ScheduledProcedureStep.modality == modality)
        )
    step = session.execute(statement.limit(1)).scalars().first()
    return step if step and _study_is_in_scope(session, step.study_instance_uid, tenant_id) else None


def _link_scheduled_step(
    session: Session | None,
    ds: Dataset,
    patient: Patient | None,
    modality: str | None,
    tenant_id: int | None,
) -> ScheduledProcedureStep | None:
    """Match by exact SPS ID, accession, then patient/modality within one tenant."""
    try:
        scheduled = getattr(ds, "ScheduledStepAttributesSequence")[0]
        sequence = getattr(scheduled, "ScheduledProcedureStepSequence", None)
        sps_id = _str(sequence[0], "ScheduledProcedureStepID") if sequence else None
    except Exception as exc:
        log.debug("mpps_sps_attributes_unusable error_type=%s", type(exc).__name__)
        sps_id = None
    if session is None:
        return None
    if sps_id:
        step = _open_step_by_sps_id(session, sps_id, tenant_id)
        if step:
            return step
    accession = _str(ds, "AccessionNumber")
    if accession:
        step = _open_step_by_accession(session, accession, tenant_id)
        if step:
            return step
    return _open_step_by_patient(session, patient, modality, tenant_id) if patient else None


def _get_or_create_patient(
    session: Session, ds: Dataset, tenant_id: int | None
) -> Patient | None:
    patient_id = _str(ds, "PatientID")
    if not patient_id:
        return None
    patient_scope = tenant_predicate(Patient.tenant_id, tenant_id, False)
    patient = session.execute(
        select(Patient).where(Patient.patient_id == patient_id, patient_scope)
    ).scalars().first()
    if patient is None:
        try:
            # Savepoint (same pattern as indexing_service): if another thread
            # (HL7 listener, another DIMSE handler) wins the unique-MRN insert,
            # the failed INSERT rolls back without disturbing this transaction.
            with session.begin_nested():
                patient = Patient(
                    patient_id=patient_id,
                    tenant_id=tenant_id,
                    patient_name=_str(ds, "PatientName"),
                    patient_birth_date=_str(ds, "PatientBirthDate"),
                    patient_sex=_str(ds, "PatientSex"),
                )
                session.add(patient)
                session.flush()
        except IntegrityError as exc:
            # Another writer (HL7 listener / a second DIMSE handler) created the
            # same MRN between our SELECT and INSERT; re-read the winner.
            log.debug("mpps_patient_insert_race error_type=%s", type(exc).__name__)
            patient = session.execute(
                select(Patient).where(
                    Patient.patient_id == patient_id, patient_scope
                )
            ).scalars().first()
    if patient is not None and patient.source != "his":
        # Modality data is fine to backfill for non-HIS patients; HIS wins.
        if patient.patient_name is None:
            patient.patient_name = _str(ds, "PatientName")
        if patient.patient_birth_date is None:
            patient.patient_birth_date = _str(ds, "PatientBirthDate")
        if patient.patient_sex is None:
            patient.patient_sex = _str(ds, "PatientSex")
    return patient


def create_step(
    session: Session,
    sop_instance_uid: str | None,
    ds: Dataset,
    tenant_id: int | None = None,
) -> None:
    """Handle an MPPS N-CREATE (exam start, status IN PROGRESS).

    Idempotent on the MPPS SOP Instance UID: a re-sent N-CREATE (retry after a
    dropped response) updates the existing row instead of duplicating it.
    """
    if not sop_instance_uid:
        log.warning("MPPS N-CREATE without an Affected SOP Instance UID")
        return

    patient = _get_or_create_patient(session, ds, tenant_id)
    modality = _str(ds, "Modality")
    step = _link_scheduled_step(session, ds, patient, modality, tenant_id)

    pps = session.execute(
        select(PerformedProcedureStep).where(
            PerformedProcedureStep.sop_instance_uid == sop_instance_uid
        )
    ).scalars().first()
    if pps is None:
        pps = PerformedProcedureStep(
            sop_instance_uid=sop_instance_uid, tenant_id=tenant_id
        )
        session.add(pps)
    elif pps.tenant_id != tenant_id:
        raise ValueError("MPPS instance belongs to a different tenant")

    pps.patient_id = patient.id if patient else None
    pps.scheduled_step_id = step.id if step else None
    pps.status = _str(ds, "PerformedProcedureStepStatus") or "IN PROGRESS"
    pps.pps_id = _str(ds, "PerformedProcedureStepID")
    pps.pps_description = _str(ds, "PerformedProcedureStepDescription")
    pps.modality = modality
    pps.performed_station_aet = _str(ds, "PerformedStationAETitle")
    pps.accession_number = _str(ds, "AccessionNumber")
    pps.start_date = _str(ds, "PerformedProcedureStepStartDate")
    pps.start_time = _str(ds, "PerformedProcedureStepStartTime")

    if step is not None and step.status == "SCHEDULED":
        step.status = "IN PROGRESS"
        log.info("MPPS: worklist step %s now IN PROGRESS", step.sps_id)
    # Handlers run in pynetdicom threads with a `with SyncSessionLocal() as
    # session:` block — closing the session only rolls back unless we commit.
    session.commit()


def update_step(
    session: Session,
    sop_instance_uid: str | None,
    ds: Dataset,
    tenant_id: int | None = None,
) -> bool:
    """Handle an MPPS N-SET (exam end). Returns False when the SOP Instance is
    unknown (the caller replies 0x0112 No such object instance)."""
    if not sop_instance_uid:
        return False
    patient_scope = tenant_predicate(Patient.tenant_id, tenant_id, False)
    pps = session.execute(
        select(PerformedProcedureStep)
        .outerjoin(Patient, PerformedProcedureStep.patient_id == Patient.id)
        .where(
            PerformedProcedureStep.sop_instance_uid == sop_instance_uid,
            tenant_predicate(PerformedProcedureStep.tenant_id, tenant_id, False),
            or_(PerformedProcedureStep.patient_id.is_(None), patient_scope),
        )
    ).scalars().first()
    if pps is None:
        log.warning("MPPS N-SET for unknown instance %s", sop_instance_uid)
        return False

    incoming_study_uid = _str(ds, "StudyInstanceUID")
    try:
        series_seq = getattr(ds, "PerformedSeriesSequence", None)
        if not incoming_study_uid and series_seq:
            incoming_study_uid = _str(series_seq[0], "StudyInstanceUID")
    except Exception as exc:  # malformed optional sequence must not expose dataset details
        log.debug(
            "MPPS: unusable PerformedSeriesSequence error_type=%s",
            type(exc).__name__,
        )
        series_seq = None
    if incoming_study_uid and not _study_is_in_scope(
        session, incoming_study_uid, tenant_id
    ):
        return False
    if pps.scheduled_step_id is not None and not _scheduled_step_is_in_scope(
        session, pps.scheduled_step_id, tenant_id
    ):
        return False

    status = _str(ds, "PerformedProcedureStepStatus")
    if status:
        pps.status = status
    pps.end_date = _str(ds, "PerformedProcedureStepEndDate")
    pps.end_time = _str(ds, "PerformedProcedureStepEndTime")

    # Performed Series Sequence (0040,0275) links the performed step to the
    # stored study/series the modality just produced.
    try:
        if series_seq:
            first = series_seq[0]
            pps.study_instance_uid = _str(first, "StudyInstanceUID") or pps.study_instance_uid
            pps.series_instance_uid = _str(first, "SeriesInstanceUID") or pps.series_instance_uid
            pps.modality = _str(first, "Modality") or pps.modality
    except Exception as exc:
        log.debug(
            "MPPS: unusable PerformedSeriesSequence error_type=%s",
            type(exc).__name__,
        )

    # Link a scheduled step now if we couldn't at N-CREATE (some modalities
    # only carry the SPS reference in the final update).
    if pps.scheduled_step_id is None:
        patient = session.get(Patient, pps.patient_id) if pps.patient_id else None
        step = _link_scheduled_step(session, ds, patient, pps.modality, tenant_id)
        if step is not None:
            pps.scheduled_step_id = step.id

    step = session.get(ScheduledProcedureStep, pps.scheduled_step_id) if pps.scheduled_step_id else None
    if status == "COMPLETED":
        # Close the worklist loop: reuse the A1 completion (links the study).
        if step is not None and step.status in ("SCHEDULED", "IN PROGRESS"):
            worklist_service.complete_step(step, pps.study_instance_uid)
            log.info("MPPS: worklist step %s COMPLETED", step.sps_id)
    elif status == "DISCONTINUED":
        # Release the slot so the technologist can re-book it.
        if step is not None and step.status == "IN PROGRESS":
            step.status = "SCHEDULED"
            log.info("MPPS: worklist step %s released to SCHEDULED", step.sps_id)

    session.commit()
    return True


def _study_is_in_scope(session: Session, study_uid: str, tenant_id: int | None) -> bool:
    """Allow a future study UID; reject an existing study owned by another tenant."""
    if not study_uid:
        return True
    row = session.execute(
        select(Study.tenant_id, Patient.tenant_id)
        .join(Patient, Study.patient_id == Patient.id)
        .where(Study.study_instance_uid == study_uid)
    ).first()
    return row is None or (row[0] == tenant_id and row[1] == tenant_id)


def _scheduled_step_is_in_scope(
    session: Session, step_id: int, tenant_id: int | None
) -> bool:
    """Require the scheduled row, its patient, and linked study to agree."""
    patient_scope = tenant_predicate(Patient.tenant_id, tenant_id, False)
    step = session.execute(
        select(ScheduledProcedureStep).join(
            Patient, ScheduledProcedureStep.patient_id == Patient.id
        ).where(ScheduledProcedureStep.id == step_id, patient_scope)
    ).scalar_one_or_none()
    return step is not None and _study_is_in_scope(
        session, step.study_instance_uid, tenant_id
    )
