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
from app.services import query_service

log = logging.getLogger("paxs.worklist")

#: H-03: (0008,0005) on every C-FIND / MWL response — UTF-8, so Arabic names
#: survive. Also used by the Q/R C-FIND responses in ``app/dicom/receiver.py``.
RESPONSE_CHARACTER_SET = "ISO_IR 192"

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


def build_statement(filters: dict, tenant_id: int | None = None,
                    include_all: bool = False):
    """SELECT (step, patient) matching the worklist filters.

    CANCELED steps are hidden unless the modality explicitly asked for them,
    matching the "active worklist" semantics of dcm4chee/Orthanc.

    B-9: ``scheduled_procedure_steps`` carries no tenant column — it inherits
    one through ``patient_id`` — so the predicate goes on the **joined Patient**.
    A modality association that is mapped to a tenant therefore only ever sees
    its own institution's worklist; an unmapped AE keeps the legacy (NULL) scope,
    exactly like the Q/R C-FIND path.
    """
    stmt = select(ScheduledProcedureStep, Patient).join(
        Patient, ScheduledProcedureStep.patient_id == Patient.id
    )
    stmt = query_service.apply_tenant_scope(
        stmt, Patient.tenant_id, tenant_id, include_all
    )
    for kw, value in filters.items():
        col = _column_for(kw)
        if col is None:
            continue
        if "*" in value:
            stmt = stmt.where(col.ilike(value.replace("*", "%")))
        elif kw in ("ScheduledStationAETitle", "Modality"):
            # A step without a station/modality (ADT-created default) is open
            # to every station — NULL acts as a wildcard.
            stmt = stmt.where(or_(col.is_(None), col == value))
        else:
            stmt = stmt.where(col == value)
    if "ScheduledProcedureStepStatus" not in filters:
        stmt = stmt.where(ScheduledProcedureStep.status != "CANCELED")
    return stmt.order_by(ScheduledProcedureStep.start_date, ScheduledProcedureStep.start_time)


# --------------------------------------------------------------------------- #
# REST worklist queries (B-9)
#
# The router must stay free of statement construction, and every one of these
# reads is tenant-scoped through the owning Patient — so both the SQL and the
# predicate live in one place instead of being repeated per endpoint.
# --------------------------------------------------------------------------- #
def scheduled_steps_query(
    *,
    status: str | None = None,
    station: str | None = None,
    modality: str | None = None,
    search: str | None = None,
    tenant_id: int | None = None,
    include_all: bool = False,
):
    """The dashboard worklist listing, tenant-scoped."""
    stmt = (
        select(ScheduledProcedureStep, Patient)
        .join(Patient, ScheduledProcedureStep.patient_id == Patient.id)
    )
    stmt = query_service.apply_tenant_scope(
        stmt, Patient.tenant_id, tenant_id, include_all
    )
    if status:
        stmt = stmt.where(ScheduledProcedureStep.status == status)
    if station:
        stmt = stmt.where(ScheduledProcedureStep.scheduled_station_aet == station)
    if modality:
        stmt = stmt.where(ScheduledProcedureStep.modality == modality)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            Patient.patient_name.ilike(like)
            | Patient.patient_id.ilike(like)
            | ScheduledProcedureStep.accession_number.ilike(like)
        )
    return stmt.order_by(
        ScheduledProcedureStep.start_date.desc().nulls_last(), ScheduledProcedureStep.id.desc()
    )


async def list_scheduled_steps(session, **kwargs):
    """(step, patient) rows for the dashboard worklist, tenant-scoped."""
    return list((await session.execute(scheduled_steps_query(**kwargs))).all())


async def get_scheduled_step(session, step_id: int, tenant_id: int | None = None,
                             include_all: bool = False):
    """One scheduled step, or None when it is outside the caller's tenant (B-9).

    A foreign step is deliberately indistinguishable from a missing one.
    """
    stmt = (
        select(ScheduledProcedureStep)
        .join(Patient, ScheduledProcedureStep.patient_id == Patient.id)
        .where(ScheduledProcedureStep.id == step_id)
    )
    stmt = query_service.apply_tenant_scope(
        stmt, Patient.tenant_id, tenant_id, include_all
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_patient_for_booking(session, patient_id: str, patient_name: str | None,
                                  tenant_id: int | None = None,
                                  include_all: bool = False):
    """Resolve the patient a booking is for, inside the caller's tenant (B-9).

    Looking the MRN up globally would let one institution book against another's
    patient and then read their demographics back out of the worklist. Returns
    ``None`` when the MRN is unknown and no name was supplied (the caller turns
    that into 404).
    """
    stmt = query_service.apply_tenant_scope(
        select(Patient).where(Patient.patient_id == patient_id),
        Patient.tenant_id,
        tenant_id,
        include_all,
    )
    patient = (await session.execute(stmt)).scalar_one_or_none()
    if patient is None and patient_name:
        patient = Patient(
            patient_id=patient_id, patient_name=patient_name, tenant_id=tenant_id
        )
        session.add(patient)
        await session.flush()
    return patient


def requested_keys(query: Dataset) -> set[str]:
    """Return keys the modality asked to have populated (non-empty in query
    means a match filter AND a return key; empty means return-only)."""
    requested = {kw for kw in query.dir()}
    seq = getattr(query, "ScheduledProcedureStepSequence", None)
    if seq:
        try:
            requested |= {kw for kw in seq[0].dir()}
        except (ValueError, TypeError, AttributeError, IndexError) as exc:  # a malformed SPS item narrows the reply
            # An empty or non-Dataset SPS item means the modality asked for the
            # top-level keys only; the union simply does not grow.
            log.debug("worklist_sps_unreadable error_type=%s", type(exc).__name__)
    return {k for k in requested if k in _ALL_KEYS}


def serialize_sps(step: ScheduledProcedureStep, patient: Patient,
                  requested: set[str]) -> Dataset:
    """Build the C-FIND-RSP dataset for one matching scheduled procedure step.

    H-03: always declares UTF-8 (``ISO_IR 192``). Without it Arabic patient
    names cannot be encoded and the scanner's worklist shows ``?????`` or no
    name at all — a wrong-patient risk at the modality.
    """
    ds = Dataset()
    ds.SpecificCharacterSet = RESPONSE_CHARACTER_SET
    step_ds = Dataset()
    _copy_requested_fields(step_ds, step, _SEQ_KEYS, requested)
    # The sequence is the heart of a worklist response — always include it.
    ds.ScheduledProcedureStepSequence = [step_ds]
    _copy_requested_fields(ds, step, _TOP_KEYS, requested)
    _copy_requested_fields(ds, patient, _PATIENT_KEYS, requested)
    return ds


def _copy_requested_fields(
    target: Dataset,
    source: object,
    field_map: dict[str, str],
    requested: set[str],
) -> None:
    """Copy available requested attributes, isolating malformed optional fields."""
    for keyword, attribute in field_map.items():
        if keyword not in requested:
            continue
        value = getattr(source, attribute, None)
        if value is None:
            continue
        try:
            setattr(target, keyword, value)
        except Exception as exc:
            log.debug(
                "mwl_response_field_rejected tag=%s error_type=%s",
                keyword,
                type(exc).__name__,
            )


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
