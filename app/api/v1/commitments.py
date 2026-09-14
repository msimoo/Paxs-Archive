"""Storage Commitment API — archive guarantee status.

Read-only surface for the dashboard: commitment transactions are written by
the DICOM N-ACTION SCP (`app/dicom/receiver.py`), never by HTTP. This
endpoint lets admins see which modalities asked for guarantees, whether every
instance was verified against the storage backend, and the per-request
outcome (delivered / failed / pending).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StorageCommitRequest, User
from app.db.session import get_db
from app.api.v1.deps import get_current_user

log = logging.getLogger("paxs.api.commitments")

router = APIRouter(prefix="/api/v1/commitments", tags=["commitments"])


def _req_out(req: StorageCommitRequest) -> dict:
    try:
        payload = json.loads(req.payload_json or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    results = payload.get("results", [])
    failures = [
        {"sop_instance_uid": r.get("sop_instance_uid"),
         "failure_reason": r.get("failure_reason")}
        for r in results if not r.get("ok")
    ]
    return {
        "id": req.id,
        "transaction_uid": req.transaction_uid,
        "ae_title": req.ae_title,
        "status": req.status,
        "instance_count": len(payload.get("requested", [])),
        "verified_count": sum(1 for r in results if r.get("ok")),
        "failures": failures,
        "sent_at": req.sent_at.isoformat() if req.sent_at else None,
        "created_at": req.created_at.isoformat() if req.created_at else None,
    }


@router.get("")
async def list_commitments(status: str | None = None,
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """List storage commitment transactions, newest first (optional filter)."""
    stmt = select(StorageCommitRequest).order_by(StorageCommitRequest.id.desc()).limit(100)
    if status:
        stmt = stmt.where(StorageCommitRequest.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return [_req_out(r) for r in rows]
