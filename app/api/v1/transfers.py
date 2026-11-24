"""Thin API for durable one-shot study send and retrieve jobs (FT-1)."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_destructive_role
from app.core.dicom_uids import PLACEHOLDER_UIDS
from app.db.session import get_db
from app.services import node_service, transfer_service

router = APIRouter(prefix="/api/v1/transfers", tags=["transfers"])
_UID = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")


class TransferCreate(BaseModel):
    """One study-level send or retrieve request; patient filters are excluded."""

    direction: Literal["SEND", "RETRIEVE"]
    transport: Literal["DIMSE", "DICOMWEB", "CMOVE"] = "DIMSE"
    node_ae_title: str = Field(min_length=1, max_length=64)
    study_uids: list[str] = Field(min_length=1, max_length=100)
    our_ae_title: str = Field(default="PAXS_SCU", min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_request(self) -> TransferCreate:
        if self.direction == "RETRIEVE" and len(self.study_uids) != 1:
            raise ValueError("RETRIEVE accepts exactly one StudyInstanceUID")
        if self.direction == "SEND" and self.transport != "DIMSE":
            raise ValueError("SEND currently supports DIMSE only")
        if any(
            not _UID.fullmatch(uid) or len(uid) > 64 or uid.casefold() in PLACEHOLDER_UIDS
            for uid in self.study_uids
        ):
            raise ValueError("study_uids must contain valid DICOM UIDs")
        if len(set(self.study_uids)) != len(self.study_uids):
            raise ValueError("study_uids must be unique")
        return self


def _job_out(job: Any) -> dict:
    """Serialize operational fields only; no demographics or credentials."""
    return {
        "id": job.id,
        "node_ae_title": job.node_ae_title,
        "direction": job.direction,
        "transport": job.transport,
        "study_uids": json.loads(job.study_uids_json),
        "status": job.status,
        "total_items": job.total_items,
        "completed_items": job.completed_items,
        "failed_items": job.failed_items,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@router.post("", status_code=202)
async def create_transfer(
    body: TransferCreate,
    db: AsyncSession = Depends(get_db),
    user: Any = Depends(require_destructive_role),
) -> dict:
    """Validate a registered peer and enqueue a tenant-scoped one-shot job."""
    if body.direction == "RETRIEVE" and user.role != "ADMIN":
        raise HTTPException(
            403, "Remote retrieval is ADMIN-only until the node is assigned to a tenant"
        )
    node = await node_service.get_node(db, body.node_ae_title)
    if node is None:
        raise HTTPException(404, "Node not found")
    if not node.enabled:
        raise HTTPException(409, "Node is disabled")
    if body.transport == "DICOMWEB" and not node.web_url:
        raise HTTPException(409, "Node has no DICOMweb base URL")
    if body.transport == "CMOVE":
        from app.dicom.dcmtk_move import availability

        problem = await asyncio.to_thread(availability, getattr(node, "trust_cert", None))
        if problem:
            raise HTTPException(409, problem)
    if body.direction == "SEND":
        try:
            await transfer_service.require_send_scope(db, user, body.study_uids)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
    try:
        job = await transfer_service.enqueue(
            db, user,
            {
                "node_ae_title": body.node_ae_title,
                "direction": body.direction,
                "transport": body.transport,
                "our_ae_title": body.our_ae_title,
                "study_uids_json": json.dumps(body.study_uids, separators=(",", ":")),
                "include_all": user.role == "ADMIN",
            },
        )
    except OverflowError as exc:
        raise HTTPException(503, str(exc)) from exc
    await db.commit()
    await db.refresh(job)
    return _job_out(job)


@router.get("")
async def list_transfers(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: Any = Depends(require_destructive_role),
) -> list[dict]:
    """List a bounded page of jobs visible to the authenticated user."""
    jobs = await transfer_service.list_jobs(db, user, limit, offset)
    return [_job_out(job) for job in jobs]


@router.get("/{job_id}")
async def get_transfer(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    user: Any = Depends(require_destructive_role),
) -> dict:
    """Read one transfer job without revealing cross-tenant existence."""
    job = await transfer_service.get_job(db, user, job_id)
    if job is None:
        raise HTTPException(404, "Transfer job not found")
    return _job_out(job)


@router.delete("/history")
async def clear_transfer_history(
    limit: int = Query(default=100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: Any = Depends(require_destructive_role),
) -> dict:
    """Clear finished transfer records only; queued/running jobs and images stay."""
    deleted = await transfer_service.clear_history(db, user, limit)
    await db.commit()
    return {"deleted": deleted}


@router.post("/{job_id}/cancel")
async def cancel_transfer(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    user: Any = Depends(require_destructive_role),
) -> dict:
    """Cancel a queued transfer; running DIMSE associations finish normally."""
    job = await transfer_service.get_job(db, user, job_id)
    if job is None:
        raise HTTPException(404, "Transfer job not found")
    if not await transfer_service.cancel_pending(db, user, job_id):
        raise HTTPException(409, "Only pending transfer jobs can be cancelled")
    await db.commit()
    return {"id": job_id, "status": "CANCELLED"}
