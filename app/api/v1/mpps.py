"""MPPS (Modality Performed Procedure Step) API — live exam state.

Read-only surface for the dashboard: performed steps are written by the DICOM
N-CREATE / N-SET SCP (`app/dicom/receiver.py`), never by HTTP. This endpoint
lets technologists/radiologists watch exams in real time and see the
scheduled -> performed -> stored chain close.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, tenant_scope
from app.db.session import get_db
from app.services import mpps_service

log = logging.getLogger("paxs.api.mpps")

router = APIRouter(prefix="/api/v1/mpps", tags=["mpps"])


@router.get("")
async def list_performed(status: str | None = None,
                         db: AsyncSession = Depends(get_db),
                         user: Any = Depends(get_current_user)) -> list[dict[str, object]]:
    """List performed procedure steps, newest first (optional status filter)."""
    tenant_id, include_all = tenant_scope(user)
    return await mpps_service.list_performed_steps(
        db, status, tenant_id, include_all
    )
