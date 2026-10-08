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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Patient, ScheduledProcedureStep, User
from app.db.session import get_db
from app.api.v1.deps import get_current_user
from app.core.roles import WRITE_ROLES

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
        "created_at": step.created_at.isoformat() if step.created_at else None,
        "updated_at": step.updated_at.isoformat() if step.updated_at else None,
    }


def _require_write(user: User) -> None:
    if user.role not in WRITE_ROLES:
        raise HTTPException(403, f"Requires one of {WRITE_ROLES} roles")


async def _get_patient(db: AsyncSession, body) -> Patient:
    patient = (
        await db.execute(select(Patient).where(Patient.patient_id == body.patient_id))
    ).scalar_one_or_none()
    if patient is None:
        if not body.patient_name:
            raise HTTPException(
                404,
                f"Patient '{body.patient_id}' not found — HL7 ADT registers patients automatically; "
                "provide patient_name to create a manual patient.",
            )
        patient = Patient(patient_id=body.patient_id, patient_name=body.patient_name)
        db.add(patient)
        await db.flush()
    return patient


@router.get("")
async def list_steps(status: str | None = None,
                     station: str | None = None,
                     modality: str | None = None,
                     search: str | None = None,
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    stmt = (
        select(ScheduledProcedureStep, Patient)
        .join(Patient, ScheduledProcedureStep.patient_id == Patient.id)
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
            Patient.patient_name.like(like)
            | Patient.patient_id.like(like)
            | ScheduledProcedureStep.accession_number.like(like)
        )
    stmt = stmt.order_by(ScheduledProcedureStep.start_date.desc(),
                         ScheduledProcedureStep.id.desc())
    rows = (await db.execute(stmt)).all()
    return [_step_out(step, patient) for step, patient in rows]


@router.post("", status_code=201)
async def create_step(body: WorklistIn, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    _require_write(user)
    if body.status not in VALID_STATUS:
        raise HTTPException(422, f"Invalid status, use one of {VALID_STATUS}")
    patient = await _get_patient(db, body)
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
    step = (
        await db.execute(select(ScheduledProcedureStep).where(ScheduledProcedureStep.id == step_id))
    ).scalar_one_or_none()
    if step is None:
        raise HTTPException(404, "Worklist step not found")
    if body.status is not None:
        if body.status not in VALID_STATUS:
            raise HTTPException(422, f"Invalid status, use one of {VALID_STATUS}")
        step.status = body.status
        if body.status == "COMPLETED":
            step.performed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            step.performed_at = None  # reverted — clear the stale timestamp
    if body.scheduled_station_aet is not None:
        step.scheduled_station_aet = body.scheduled_station_aet
    if body.start_date is not None:
        step.start_date = body.start_date
    if body.start_time is not None:
        step.start_time = body.start_time
    if body.modality is not None:
        step.modality = body.modality
    await db.flush()
    await db.refresh(step)  # load server_default/onupdate timestamps (async)
    patient = (await db.execute(select(Patient).where(Patient.id == step.patient_id))).scalar_one()
    log.info("Worklist step %s updated by %s", step.sps_id, user.username)
    return _step_out(step, patient)


@router.delete("/{step_id}", status_code=204)
async def delete_step(step_id: int, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    if user.role != "ADMIN":
        raise HTTPException(403, "Only ADMIN can delete worklist steps")
    step = (
        await db.execute(select(ScheduledProcedureStep).where(ScheduledProcedureStep.id == step_id))
    ).scalar_one_or_none()
    if step is None:
        raise HTTPException(404, "Worklist step not found")
    await db.delete(step)
    log.info("Worklist step %s removed by %s", step.sps_id, user.username)
