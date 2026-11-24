"""DR replication admin API (roadmap #49).

    GET  /api/v1/replication            status + summary counts
    POST /api/v1/replication/sweep      replicate due studies now
    POST /api/v1/replication/studies/{study_uid}   replicate one study now
    GET  /api/v1/replication/records    recent per-study records

Admin-only. The target is a registered storage backend (Admin → Storage,
kind=s3) named by DR_REPLICATION_TARGET, or the env S3 config.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_admin
from app.db.models import User
from app.db.session import get_db
from app.services import replication_service

router = APIRouter(prefix="/api/v1/replication", tags=["replication"])


@router.get("")
async def replication_status(user: User = Depends(require_admin)):
    return replication_service.status()


@router.get("/records")
async def replication_records(limit: int = 30, user: User = Depends(require_admin)):
    return {"records": replication_service.records(limit)}


@router.post("/sweep")
async def replication_sweep(db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_admin)):
    # Release this request's transaction so the sync writer thread cannot
    # deadlock against it on SQLite.
    await db.commit()
    result = await asyncio.to_thread(replication_service.sweep)
    return result


@router.post("/studies/{study_uid}")
async def replicate_study(study_uid: str,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_admin)):
    await db.commit()
    try:
        result = await asyncio.to_thread(
            replication_service.replicate_study, study_uid
        )
    except replication_service.ReplicationBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, "Replication failed") from None
    if not result.get("ok", True):
        raise HTTPException(422, "Replication failed")
    return result
