"""Radiologist reading worklist REST API (roadmap #30).

Task lists built on the archive:
  UNREAD    — studies with images, no report, not read
  DICTATED  — report drafted, awaiting finalization
  READ      — finalized (or explicitly marked read)
  STAT      — urgent overlay (manual flag / pending critical / HIGH triage)

Endpoints:
  GET    /api/v1/worklist-tasks?state=UNREAD|DICTATED|READ|STAT
  GET    /api/v1/worklist-tasks/counts
  POST   /api/v1/worklist-tasks/{study_uid}/stat        {on: bool}
  POST   /api/v1/worklist-tasks/{study_uid}/assign      {radiologist}
  POST   /api/v1/worklist-tasks/{study_uid}/read
  POST   /api/v1/worklist-tasks/{study_uid}/unread
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.services import reading_worklist_service as rwl

log = logging.getLogger("paxs.api.worklist_tasks")

router = APIRouter(prefix="/api/v1/worklist-tasks", tags=["worklist tasks"])

VALID_STATES = ("UNREAD", "DICTATED", "READ", "STAT")


class StatBody(BaseModel):
    on: bool = True


class AssignBody(BaseModel):
    radiologist: str | None = Field(default=None, max_length=128)


@router.get("/counts")
async def task_counts(db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Per-state reader task counts (VNA-scoped)."""
    return await rwl.reader_counts(db, user)


@router.get("")
async def list_tasks(state: str = Query("UNREAD"),
                     radiologist: str | None = Query(None),
                     page: int = Query(1, ge=1),
                     limit: int = Query(25, ge=1, le=100),
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """List studies in a given reader state."""
    try:
        return await rwl.list_reader_tasks(
            db, state, user, page=page, limit=limit, radiologist=radiologist
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{study_uid}/stat", status_code=200)
async def set_stat(study_uid: str, body: StatBody,
                   db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Manually flag / unflag a study as STAT (urgent)."""
    try:
        study = await rwl.set_stat(db, study_uid, body.on)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "study_uid": study.study_instance_uid, "stat": study.stat_flag}


@router.post("/{study_uid}/assign", status_code=200)
async def assign(study_uid: str, body: AssignBody,
                 db: AsyncSession = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Assign (or clear) the radiologist reading this study."""
    try:
        study = await rwl.assign_radiologist(db, study_uid, body.radiologist)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "ok": True,
        "study_uid": study.study_instance_uid,
        "assigned_radiologist": study.assigned_radiologist,
    }


@router.post("/{study_uid}/read", status_code=200)
async def mark_read(study_uid: str, db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Mark a study as read by the current user."""
    try:
        study = await rwl.mark_read(db, study_uid, performed_by=user.username)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "study_uid": study.study_instance_uid}


@router.post("/{study_uid}/unread", status_code=200)
async def mark_unread(study_uid: str, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Move a study back into the UNREAD pile."""
    try:
        study = await rwl.mark_unread(db, study_uid)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "study_uid": study.study_instance_uid}
