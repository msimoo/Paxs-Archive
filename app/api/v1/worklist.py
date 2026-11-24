"""Modality Worklist management API — schedule and manage the scheduled
procedure steps served to modalities over DICOM C-FIND (MWL SOP Class).

Patients are normally registered by the HL7 ADT listener (which also opens a
default step on admit/register); this API is the manual booking fallback and
the dashboard's Schedule panel backend.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, tenant_scope
from app.core.config import settings
from app.core.roles import WRITE_ROLES
from app.db.models import Patient, ScheduledProcedureStep, User
from app.db.session import get_db
from app.services import worklist_service

log = logging.getLogger("paxs.api.worklist")

router = APIRouter(prefix="/api/v1/worklist", tags=["worklist"])

# Booking clerks (TECHNOLOGIST) and admins manage the schedule; deletes are admin-only.
# WRITE_ROLES is imported from app.core.roles so the role vocabulary has one definition.

VALID_STATUS = ("SCHEDULED", "IN PROGRESS", "COMPLETED", "CANCELED")


class WorklistIn(BaseModel):
    patient_id: str = Field(min_length=1, max_length=64, examples=["MRN123456"])
    patient_name: str | None = Field(default=None, max_length=192,
                                     description="Used only to create a patient row if the MRN is unknown")
    accession_number: str | None = Field(default=None, max_length=64)
    modality: str | None = Field(default=None, max_length=16, examples=["CT"])
    scheduled_station_aet: str | None = Field(default=None, max_length=64, examples=["CT1"])
    start_date: str | None = Field(default=None, max_length=8, examples=["20260805"])
    start_time: str | None = Field(default=None, max_length=16)
    status: str = Field(default="SCHEDULED", max_length=16)
    requested_procedure_id: str | None = Field(default=None, max_length=64)
    requested_procedure_description: str | None = Field(default=None, max_length=255)
    scheduled_performing_physician: str | None = Field(default=None, max_length=192)


class WorklistUpdate(BaseModel):
    status: str | None = Field(default=None, max_length=16)
    scheduled_station_aet: str | None = Field(default=None, max_length=64)
    start_date: str | None = Field(default=None, max_length=8)
    start_time: str | None = Field(default=None, max_length=16)
    modality: str | None = Field(default=None, max_length=16)
    accession_number: str | None = Field(default=None, max_length=64)
    requested_procedure_id: str | None = Field(default=None, max_length=64)
    requested_procedure_description: str | None = Field(default=None, max_length=255)
    scheduled_performing_physician: str | None = Field(default=None, max_length=192)


# Order fields the Workflow page may edit. Status is handled separately because
# it also stamps/clears ``performed_at``. An empty string clears a field.
_EDITABLE_FIELDS = (
    "scheduled_station_aet",
    "start_date",
    "start_time",
    "modality",
    "accession_number",
    "requested_procedure_id",
    "requested_procedure_description",
    "scheduled_performing_physician",
)


def _step_out(step: ScheduledProcedureStep, patient: Patient | None) -> dict:
    return {
        "id": step.id,
        "sps_id": step.sps_id,
        "patient_id": patient.patient_id if patient else None,
        "patient_name": patient.patient_name if patient else None,
        "patient_source": patient.source if patient else None,
        "accession_number": step.accession_number,
        "modality": step.modality,
        "scheduled_station_aet": step.scheduled_station_aet,
        "start_date": step.start_date,
        "start_time": step.start_time,
        "status": step.status,
        "requested_procedure_id": step.requested_procedure_id,
        "requested_procedure_description": step.requested_procedure_description,
        "scheduled_performing_physician": step.scheduled_performing_physician,
        "study_instance_uid": step.study_instance_uid,
        "performed_at": step.performed_at.isoformat() if step.performed_at else None,
        "created_at": step.created_at.isoformat() if step.created_at else None,
        "updated_at": step.updated_at.isoformat() if step.updated_at else None,
    }


def _require_write(user: User) -> None:
    if user.role not in WRITE_ROLES:
        raise HTTPException(403, f"Requires one of {WRITE_ROLES} roles")


async def _get_patient(db: AsyncSession, body, tenant_id: int | None,
                       include_all: bool) -> Patient:
    """Resolve the patient for a booking, within the caller's tenant scope.

    B-9: ``scheduled_procedure_steps`` has no tenant column — it inherits one
    through ``patient_id``. Looking a patient up globally would let a tenant
    book against another institution's MRN and then read their demographics
    back out of the worklist, so the lookup is scoped (in the service) and a
    manual patient is created *inside* the caller's tenant.
    """
    patient = await worklist_service.get_patient_for_booking(
        db, body.patient_id, body.patient_name, tenant_id, include_all
    )
    if patient is None:
        raise HTTPException(
            404,
            f"Patient '{body.patient_id}' not found — HL7 ADT registers patients automatically; "
            "provide patient_name to create a manual patient.",
        )
    return patient


@router.get("/hl7-status")
async def hl7_gateway_status(_user: User = Depends(get_current_user)) -> dict:
    """Return HL7 MLLP listener and outbound ORU EMR configuration status."""
    return {
        "hl7_enabled": bool(settings.HL7_ENABLED),
        "hl7_host": settings.HL7_HOST,
        "hl7_port": int(settings.HL7_PORT),
        "hl7_tls_enabled": bool(settings.HL7_TLS_ENABLED),
        "emr_enabled": bool(settings.HL7_EMR_ENABLED),
        "emr_host": settings.HL7_EMR_HOST,
        "emr_port": int(settings.HL7_EMR_PORT),
        "emr_application": settings.HL7_EMR_APPLICATION,
        "default_station_aet": settings.MWL_DEFAULT_STATION_AET,
    }


@router.get("")
async def list_steps(status: str | None = None,
                     station: str | None = None,
                     modality: str | None = None,
                     search: str | None = None,
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    # B-9: the worklist is patient-keyed, so the tenant predicate goes on the
    # joined Patient (the step table itself carries no tenant column).
    tenant_id, include_all = tenant_scope(user)
    rows = await worklist_service.list_scheduled_steps(
        db,
        status=status,
        station=station,
        modality=modality,
        search=search,
        tenant_id=tenant_id,
        include_all=include_all,
    )
    return [_step_out(step, patient) for step, patient in rows]


@router.post("", status_code=201)
async def create_step(body: WorklistIn, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    _require_write(user)
    if body.status not in VALID_STATUS:
        raise HTTPException(422, f"Invalid status, use one of {VALID_STATUS}")
    tenant_id, include_all = tenant_scope(user)
    patient = await _get_patient(db, body, tenant_id, include_all)
    step = ScheduledProcedureStep(
        patient_id=patient.id,
        # ScheduledProcedureStepID is VR SH (max 16 chars) — keep it short.
        sps_id=f"SPS-{secrets.token_hex(4).upper()}",
        accession_number=body.accession_number,
        modality=body.modality,
        scheduled_station_aet=body.scheduled_station_aet,
        start_date=body.start_date,
        start_time=body.start_time,
        status=body.status,
        requested_procedure_id=body.requested_procedure_id,
        requested_procedure_description=body.requested_procedure_description,
        scheduled_performing_physician=body.scheduled_performing_physician,
    )
    db.add(step)
    await db.flush()
    await db.refresh(step)
    log.info("Worklist step %s scheduled for %s by %s",
             step.sps_id, body.patient_id, user.username)
    return _step_out(step, patient)


@router.patch("/{step_id}")
async def update_step(step_id: int, body: WorklistUpdate,
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    _require_write(user)
    tenant_id, include_all = tenant_scope(user)
    step = await worklist_service.get_scheduled_step(db, step_id, tenant_id, include_all)
    if step is None:
        # B-9: a foreign step is reported exactly like a missing one.
        raise HTTPException(404, "Worklist step not found")
    if body.status is not None:
        if body.status not in VALID_STATUS:
            raise HTTPException(422, f"Invalid status, use one of {VALID_STATUS}")
        step.status = body.status
        if body.status == "COMPLETED":
            step.performed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            step.performed_at = None  # reverted — clear the stale timestamp
    for field in _EDITABLE_FIELDS:
        value = getattr(body, field)
        if value is None:
            continue
        value = value.strip()
        if field == "modality":
            value = value.upper()
        if field == "start_date" and value and not (len(value) == 8 and value.isdigit()):
            raise HTTPException(422, "start_date must be YYYYMMDD")
        setattr(step, field, value or None)
    await db.flush()
    await db.refresh(step)  # load server_default/onupdate timestamps (async)
    patient = await db.get(Patient, step.patient_id)
    log.info("Worklist step %s updated by %s", step.sps_id, user.username)
    return _step_out(step, patient)


@router.delete("/{step_id}", status_code=204)
async def delete_step(step_id: int, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    if user.role != "ADMIN":
        raise HTTPException(403, "Only ADMIN can delete worklist steps")
    tenant_id, include_all = tenant_scope(user)
    step = await worklist_service.get_scheduled_step(db, step_id, tenant_id, include_all)
    if step is None:
        raise HTTPException(404, "Worklist step not found")
    await db.delete(step)
    log.info("Worklist step %s removed by %s", step.sps_id, user.username)
