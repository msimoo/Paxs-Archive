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
from sqlalchemy import or_, select

from sqlalchemy.exc import IntegrityError

from app.db.models import Patient, PerformedProcedureStep, ScheduledProcedureStep
from app.services import worklist_service

log = logging.getLogger("paxs.mpps")

VALID_STATUS = ("IN PROGRESS", "COMPLETED", "DISCONTINUED")


def _str(ds: Dataset, kw: str) -> str | None:
    if not hasattr(ds, kw):
        return None
    value = getattr(ds, kw)
    text = str(value).strip()
    return text or None


def _link_scheduled_step(session, ds: Dataset, patient: Patient | None,
                         modality: str | None) -> ScheduledProcedureStep | None:
    """Find the open (SCHEDULED / IN PROGRESS) worklist step this exam fulfils.

    Priority: the ScheduledProcedureStepID echoed by the modality (from MWL),
    then accession number, then patient + modality (same fallback chain as the
    C-STORE matcher in worklist_service.find_matching_step).
    """
    # 1) Exact SPS ID from ScheduledStepAttributesSequence (0040,0007).
    try:
        sched_attrs = getattr(ds, "ScheduledStepAttributesSequence")
        sched = sched_attrs[0]
        sps_seq = getattr(sched, "ScheduledProcedureStepSequence", None)
        if sps_seq:
            sps_id = _str(sps_seq[0], "ScheduledProcedureStepID")
            if sps_id:
                step = session.execute(
                    select(ScheduledProcedureStep).where(
                        ScheduledProcedureStep.sps_id == sps_id,
                        ScheduledProcedureStep.status.in_(("SCHEDULED", "IN PROGRESS")),
                    )
                ).scalars().first()
                if step is not None:
                    return step
    except Exception:
        log.debug("MPPS: no usable ScheduledStepAttributesSequence", exc_info=True)

    # 2) Accession number on an open step.
    accession = _str(ds, "AccessionNumber")
    if accession:
        step = session.execute(
            select(ScheduledProcedureStep).where(
                ScheduledProcedureStep.status.in_(("SCHEDULED", "IN PROGRESS")),
                ScheduledProcedureStep.accession_number == accession,
            )
        ).scalars().first()
        if step is not None:
            return step

    # 3) Patient + modality (covers ADT-created default steps without accession).
    if patient is not None:
        stmt = (
            select(ScheduledProcedureStep)
            .where(
                ScheduledProcedureStep.status.in_(("SCHEDULED", "IN PROGRESS")),
                ScheduledProcedureStep.patient_id == patient.id,
            )
            .order_by(ScheduledProcedureStep.created_at)
        )
        if modality:
            stmt = stmt.where(
                or_(
                    ScheduledProcedureStep.modality.is_(None),
                    ScheduledProcedureStep.modality == modality,
                )
            )
        return session.execute(stmt.limit(1)).scalars().first()
    return None


def _get_or_create_patient(session, ds: Dataset) -> Patient | None:
    patient_id = _str(ds, "PatientID")
    if not patient_id:
        return None
    patient = session.execute(
        select(Patient).where(Patient.patient_id == patient_id)
    ).scalars().first()
    if patient is None:
        try:
            # Savepoint (same pattern as indexing_service): if another thread
            # (HL7 listener, another DIMSE handler) wins the unique-MRN insert,
            # the failed INSERT rolls back without disturbing this transaction.
            with session.begin_nested():
                patient = Patient(
                    patient_id=patient_id,
                    patient_name=_str(ds, "PatientName"),
                    patient_birth_date=_str(ds, "PatientBirthDate"),
                    patient_sex=_str(ds, "PatientSex"),
                )
                session.add(patient)
                session.flush()
        except IntegrityError:
            patient = session.execute(
                select(Patient).where(Patient.patient_id == patient_id)
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


def create_step(session, sop_instance_uid: str | None, ds: Dataset) -> None:
    """Handle an MPPS N-CREATE (exam start, status IN PROGRESS).

    Idempotent on the MPPS SOP Instance UID: a re-sent N-CREATE (retry after a
    dropped response) updates the existing row instead of duplicating it.
    """
    if not sop_instance_uid:
        log.warning("MPPS N-CREATE without an Affected SOP Instance UID")
        return

    patient = _get_or_create_patient(session, ds)
    modality = _str(ds, "Modality")
    step = _link_scheduled_step(session, ds, patient, modality)

    pps = session.execute(
        select(PerformedProcedureStep).where(
            PerformedProcedureStep.sop_instance_uid == sop_instance_uid
        )
    ).scalars().first()
    if pps is None:
        pps = PerformedProcedureStep(sop_instance_uid=sop_instance_uid)
        session.add(pps)

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


def update_step(session, sop_instance_uid: str | None, ds: Dataset) -> bool:
    """Handle an MPPS N-SET (exam end). Returns False when the SOP Instance is
    unknown (the caller replies 0x0112 No such object instance)."""
    if not sop_instance_uid:
        return False
    pps = session.execute(
        select(PerformedProcedureStep).where(
            PerformedProcedureStep.sop_instance_uid == sop_instance_uid
        )
    ).scalars().first()
    if pps is None:
        log.warning("MPPS N-SET for unknown instance %s", sop_instance_uid)
        return False

    status = _str(ds, "PerformedProcedureStepStatus")
    if status:
        pps.status = status
    pps.end_date = _str(ds, "PerformedProcedureStepEndDate")
    pps.end_time = _str(ds, "PerformedProcedureStepEndTime")

    # Performed Series Sequence (0040,0275) links the performed step to the
    # stored study/series the modality just produced.
    try:
        series_seq = getattr(ds, "PerformedSeriesSequence")
        if series_seq:
            first = series_seq[0]
            pps.study_instance_uid = _str(first, "StudyInstanceUID") or pps.study_instance_uid
            pps.series_instance_uid = _str(first, "SeriesInstanceUID") or pps.series_instance_uid
            pps.modality = _str(first, "Modality") or pps.modality
    except Exception:
        log.debug("MPPS: no usable PerformedSeriesSequence", exc_info=True)

    # Link a scheduled step now if we couldn't at N-CREATE (some modalities
    # only carry the SPS reference in the final update).
    if pps.scheduled_step_id is None:
        patient = session.get(Patient, pps.patient_id) if pps.patient_id else None
        step = _link_scheduled_step(session, ds, patient, pps.modality)
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
