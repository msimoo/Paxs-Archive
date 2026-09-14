"""Modality Worklist service — query building and response serialization for
the DICOM Modality Worklist Information Model (1.2.840.10008.5.1.4.31).

The worklist is single-level (no QueryRetrieveLevel): a modality sends a
C-FIND-RQ whose identifier carries scheduled-procedure match keys and requests
returned fields with empty values (the same convention as Q/R C-FIND). The SCP
replies with one dataset per matching Scheduled Procedure Step, joined to the
patient row so demographics are always the authoritative ones (HIS wins).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydicom.dataset import Dataset
from sqlalchemy import or_, select

from app.db.models import Patient, ScheduledProcedureStep

log = logging.getLogger("paxs.worklist")

# Keys that live inside ScheduledProcedureStepSequence (0040,0100) — both in the
# modality's query and in the response.
_SEQ_KEYS = {
    "ScheduledStationAETitle": "scheduled_station_aet",          # (0040,0001)
    "Modality": "modality",                                       # (0008,0060)
    "ScheduledProcedureStepStartDate": "start_date",              # (0040,0002)
    "ScheduledProcedureStepStartTime": "start_time",              # (0040,0003)
    "ScheduledProcedureStepID": "sps_id",                         # (0040,0009)
    "ScheduledProcedureStepStatus": "status",                     # (0040,0020)
    "ScheduledPerformingPhysicianName": "scheduled_performing_physician",  # (0040,0006)
}

# Top-level keys of the worklist information model (on the step row).
_TOP_KEYS = {
    "AccessionNumber": "accession_number",                        # (0008,0050)
    "RequestedProcedureID": "requested_procedure_id",             # (0040,1001)
    "RequestedProcedureDescription": "requested_procedure_description",  # (0032,1060)
}

# Patient demographics — always read live from the patients table.
_PATIENT_KEYS = {
    "PatientName": "patient_name",
    "PatientID": "patient_id",
    "PatientBirthDate": "patient_birth_date",
    "PatientSex": "patient_sex",
}

_ALL_KEYS = {**_SEQ_KEYS, **_TOP_KEYS, **_PATIENT_KEYS}


def _raw(query: Dataset, kw: str) -> str:
    return str(getattr(query, kw)).strip() if hasattr(query, kw) else ""


def query_filters(query: Dataset) -> dict:
    """Extract match filters from a worklist C-FIND identifier.

    Empty-valued keys request *returned* fields and are not filters; only
    non-empty values act as match criteria (with '*' wildcard support).
    """
    filters: dict = {}
    seq = getattr(query, "ScheduledProcedureStepSequence", None)
    step = seq[0] if seq else None
    for kw in _SEQ_KEYS:
        src = step if step is not None else query
        value = _raw(src, kw)
        if value:
            filters[kw] = value
    for kw in (*_TOP_KEYS, *_PATIENT_KEYS):
        value = _raw(query, kw)
        if value:
            filters[kw] = value
    return filters


def _column_for(kw: str):
    attr = _SEQ_KEYS.get(kw) or _TOP_KEYS.get(kw)
    if attr:
        return getattr(ScheduledProcedureStep, attr)
    attr = _PATIENT_KEYS.get(kw)
    if attr:
        return getattr(Patient, attr)
    return None


def build_statement(filters: dict):
    """SELECT (step, patient) matching the worklist filters.

    CANCELED steps are hidden unless the modality explicitly asked for them,
    matching the "active worklist" semantics of dcm4chee/Orthanc.
    """
    stmt = select(ScheduledProcedureStep, Patient).join(
        Patient, ScheduledProcedureStep.patient_id == Patient.id
    )
    for kw, value in filters.items():
        col = _column_for(kw)
        if col is None:
            continue
        if "*" in value:
            stmt = stmt.where(col.like(value.replace("*", "%")))
        elif kw in ("ScheduledStationAETitle", "Modality"):
            # A step without a station/modality (ADT-created default) is open
            # to every station — NULL acts as a wildcard.
            stmt = stmt.where(or_(col.is_(None), col == value))
        else:
            stmt = stmt.where(col == value)
    if "ScheduledProcedureStepStatus" not in filters:
        stmt = stmt.where(ScheduledProcedureStep.status != "CANCELED")
    return stmt.order_by(ScheduledProcedureStep.start_date, ScheduledProcedureStep.start_time)


def requested_keys(query: Dataset) -> set[str]:
    """Return keys the modality asked to have populated (non-empty in query
    means a match filter AND a return key; empty means return-only)."""
    requested = {kw for kw in query.dir()}
    seq = getattr(query, "ScheduledProcedureStepSequence", None)
    if seq:
        try:
            requested |= {kw for kw in seq[0].dir()}
        except Exception:
            pass
    return {k for k in requested if k in _ALL_KEYS}


def serialize_sps(step: ScheduledProcedureStep, patient: Patient,
                  requested: set[str]) -> Dataset:
    """Build the C-FIND-RSP dataset for one matching scheduled procedure step."""
    ds = Dataset()

    step_ds = Dataset()
    for kw, attr in _SEQ_KEYS.items():
        if kw in requested:
            value = getattr(step, attr, None)
            if value is not None:
                try:
                    setattr(step_ds, kw, value)
                except Exception:
                    log.debug("MWL: cannot set %s", kw, exc_info=True)
    # The sequence is the heart of a worklist response — always include it.
    ds.ScheduledProcedureStepSequence = [step_ds]

    for kw, attr in _TOP_KEYS.items():
        if kw in requested:
            value = getattr(step, attr, None)
            if value is not None:
                try:
                    setattr(ds, kw, value)
                except Exception:
                    log.debug("MWL: cannot set %s", kw, exc_info=True)

    for kw, attr in _PATIENT_KEYS.items():
        if kw in requested:
            value = getattr(patient, attr, None)
            if value is not None:
                try:
                    setattr(ds, kw, value)
                except Exception:
                    log.debug("MWL: cannot set %s", kw, exc_info=True)
    return ds


async def find_matching_step(session, patient_id: int,
                             meta: dict) -> ScheduledProcedureStep | None:
    """Locate an open (SCHEDULED / IN PROGRESS) worklist step that an incoming
    study closes.

    Match by accession number first (explicit link), else by patient + modality
    (the modality echoes PatientID; the ADT-created default step has no
    accession). Returns None when nothing open matches. Caller holds the async
    session (this runs inside the STOW/C-STORE indexing transaction).

    IN PROGRESS steps are matched too: a modality that reported the exam start
    via MPPS (N-CREATE) but never delivered N-SET COMPLETED (crash, network
    loss) must still have its step closed by the C-STORE fallback — otherwise
    the worklist row would be stuck IN PROGRESS forever with no study link.
    """
    accession = (meta.get("accession_number") or "").strip()
    if accession:
        # Explicit link first: a step carrying the same accession wins.
        stmt = select(ScheduledProcedureStep).where(
            ScheduledProcedureStep.status.in_(("SCHEDULED", "IN PROGRESS")),
            ScheduledProcedureStep.accession_number == accession,
        )
        step = (await session.execute(stmt)).scalars().first()
        if step is not None:
            return step
    # Fall back to patient + modality (covers ADT-created default steps, which
    # carry no accession; modality NULL matches any modality). Oldest open step
    # wins so completion is deterministic when several are open.
    stmt = (
        select(ScheduledProcedureStep)
        .where(
            ScheduledProcedureStep.status.in_(("SCHEDULED", "IN PROGRESS")),
            ScheduledProcedureStep.patient_id == patient_id,
        )
        .order_by(ScheduledProcedureStep.created_at)
    )
    modality = (meta.get("modality") or "").strip()
    if modality:
        stmt = stmt.where(
            or_(
                ScheduledProcedureStep.modality.is_(None),
                ScheduledProcedureStep.modality == modality,
            )
        )
    return (await session.execute(stmt.limit(1))).scalars().first()


def complete_step(step: ScheduledProcedureStep, study_uid: str | None) -> None:
    """Mark a step COMPLETED and link the study that fulfilled it."""
    step.status = "COMPLETED"
    step.study_instance_uid = study_uid
    step.performed_at = datetime.now(timezone.utc).replace(tzinfo=None)
