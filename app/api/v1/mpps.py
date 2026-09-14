"""MPPS (Modality Performed Procedure Step) API — live exam state.

Read-only surface for the dashboard: performed steps are written by the DICOM
N-CREATE / N-SET SCP (`app/dicom/receiver.py`), never by HTTP. This endpoint
lets technologists/radiologists watch exams in real time and see the
scheduled -> performed -> stored chain close.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Patient,
    PerformedProcedureStep,
    ScheduledProcedureStep,
    User,
)
from app.db.session import get_db
from app.api.v1.deps import get_current_user

log = logging.getLogger("paxs.api.mpps")

router = APIRouter(prefix="/api/v1/mpps", tags=["mpps"])


def _pps_out(pps: PerformedProcedureStep, patient: Patient | None,
             sps_id: str | None) -> dict:
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
        "scheduled_step_id": sps_id,
        "created_at": pps.created_at.isoformat() if pps.created_at else None,
        "updated_at": pps.updated_at.isoformat() if pps.updated_at else None,
    }


@router.get("")
async def list_performed(status: str | None = None,
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """List performed procedure steps, newest first (optional status filter)."""
    stmt = (
        select(PerformedProcedureStep, Patient, ScheduledProcedureStep)
        .outerjoin(Patient, PerformedProcedureStep.patient_id == Patient.id)
        .outerjoin(ScheduledProcedureStep, PerformedProcedureStep.scheduled_step_id == ScheduledProcedureStep.id)
    )
    if status:
        stmt = stmt.where(PerformedProcedureStep.status == status)
    stmt = stmt.order_by(PerformedProcedureStep.id.desc()).limit(100)
    rows = (await db.execute(stmt)).all()
    return [
        _pps_out(pps, patient, sps.sps_id if sps else None)
        for pps, patient, sps in rows
    ]
