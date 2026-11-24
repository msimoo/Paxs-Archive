"""Storage Commitment API — archive guarantee status.

Read-only surface for the dashboard: commitment transactions are written by
the DICOM N-ACTION SCP (`app/dicom/receiver.py`), never by HTTP. This
endpoint lets authorized users see which modalities asked for guarantees, whether every
instance was verified against the storage backend, and the per-request
outcome (delivered / failed / pending).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, tenant_scope
from app.db.models import User
from app.db.session import get_db
from app.services import storage_commit_service

router = APIRouter(prefix="/api/v1/commitments", tags=["commitments"])


@router.get("")
async def list_commitments(status: str | None = None,
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)) -> list[dict[str, object]]:
    """List storage commitment transactions, newest first (optional filter)."""
    tenant_id, include_all = tenant_scope(user)
    return await storage_commit_service.list_commitments(
        db, status, tenant_id, include_all
    )
