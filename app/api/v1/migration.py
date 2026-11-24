"""Migration REST API endpoints.

  POST /api/v1/migration/folder  — Trigger directory DICOM ingest/watcher job
  POST /api/v1/migration/cmove   — Trigger remote C-MOVE batch migration job
  GET  /api/v1/migration/jobs    — List migration jobs & progress
  GET  /api/v1/migration/jobs/{id} — Get specific migration job details
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_admin
from app.db.models import MigrationJob, User
from app.db.session import get_db
from app.services import migration_service

log = logging.getLogger("paxs.api.migration")

router = APIRouter(prefix="/api/v1/migration", tags=["Migration Tools"])


class FolderMigrationRequest(BaseModel):
    folder_path: str = Field(..., description="Local directory path containing DICOM files")
    delete_after_import: bool = Field(False, description="Delete original files after successful import")


class CMoveMigrationRequest(BaseModel):
    ae_title: str = Field(..., description="Remote DICOM Node AE Title registered in PAXS")
    study_instance_uids: list[str] = Field(..., description="List of StudyInstanceUIDs to request via C-MOVE")


def _job_to_dict(job: MigrationJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "source_info": job.source_info,
        "total_items": job.total_items,
        "processed_items": job.processed_items,
        "failed_items": job.failed_items,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@router.post("/folder", response_class=JSONResponse)
async def start_folder_migration(
    req: FolderMigrationRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Trigger a background migration job scanning and importing a local folder."""
    job = await migration_service.create_migration_job(
        db, job_type="FOLDER_WATCH", source_info=req.folder_path
    )

    background_tasks.add_task(
        migration_service.run_folder_migration,
        job.id,
        req.folder_path,
        req.delete_after_import,
    )

    return JSONResponse(
        {"message": "Folder migration job initiated", "job": _job_to_dict(job)},
        status_code=202,
    )


@router.post("/cmove", response_class=JSONResponse)
async def start_cmove_migration(
    req: CMoveMigrationRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Trigger a batch C-MOVE migration job pulling studies from a remote DICOM Node."""
    if not req.study_instance_uids:
        raise HTTPException(400, "study_instance_uids list cannot be empty")

    source_info = f"AE:{req.ae_title} ({len(req.study_instance_uids)} studies)"
    job = await migration_service.create_migration_job(
        db, job_type="CMOVE_BATCH", source_info=source_info
    )

    background_tasks.add_task(
        migration_service.run_cmove_migration,
        job.id,
        req.ae_title,
        req.study_instance_uids,
    )

    return JSONResponse(
        {"message": "C-MOVE migration job initiated", "job": _job_to_dict(job)},
        status_code=202,
    )


@router.get("/jobs", response_class=JSONResponse)
async def list_migration_jobs(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """List all migration jobs."""
    stmt = select(MigrationJob).order_by(MigrationJob.id.desc())
    jobs = (await db.execute(stmt)).scalars().all()
    return JSONResponse([_job_to_dict(j) for j in jobs])


@router.get("/jobs/{job_id}", response_class=JSONResponse)
async def get_migration_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Get status & metrics for a specific migration job."""
    job = (
        await db.execute(select(MigrationJob).where(MigrationJob.id == job_id))
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Migration job not found")
    return JSONResponse(_job_to_dict(job))
