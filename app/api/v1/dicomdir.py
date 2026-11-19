"""DICOMDIR Media Export REST API.

  POST /api/v1/export/dicomdir         — Download standard DICOMDIR ZIP archive for CD/DVD media
  POST /api/v1/export/dicomdir/preview — Preview DICOMDIR catalog structure without binary download
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.v1.deps import get_current_user, get_current_user_any, require_admin, require_study_access
from app.core.config import get_settings
from app.db.models import Instance, Series, Study, User
from app.db.session import AsyncSessionLocal, get_db
from app.services import audit_service, dicomdir_service, disc_burn_service

log = logging.getLogger("paxs.api.dicomdir")

router = APIRouter(prefix="/api/v1/export/dicomdir", tags=["DICOMDIR Media Export"])


class ExportDICOMDIRRequest(BaseModel):
    study_instance_uids: list[str] = Field(..., description="List of StudyInstanceUIDs to package into DICOMDIR export")


class DiscBurnRequest(BaseModel):
    study_uid: str = Field(min_length=1, max_length=128)


@dataclass
class DiscBurnJob:
    owner: int
    study_uid: str
    count: int
    drive: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    phase: str = "queued"
    error: str | None = None

    def progress(self) -> dict:
        return {"phase": self.phase, "count": self.count, "error": self.error}


# shortcut: status is kept per standalone server process. Use a persistent
# job store if the Windows recorder is ever shared by multiple API replicas.
_disc_jobs: dict[UUID, DiscBurnJob] = {}


async def _run_disc_job(
    job: DiscBurnJob, username: str, drive: str, instances: list[disc_burn_service.DiscInstance],
    volume_label: str,
) -> None:
    try:
        await asyncio.to_thread(
            disc_burn_service.burn_study, drive, job.study_uid, instances,
            lambda phase: setattr(job, "phase", phase), volume_label=volume_label,
        )
    except disc_burn_service.DiscBurnError as exc:
        job.error = str(exc)
    except Exception as exc:
        log.error("disc_burn_failed phase=%s error_type=%s", job.phase, type(exc).__name__)
        job.error = "The burn could not be completed. Check the disc and server before retrying."

    try:
        async with AsyncSessionLocal() as session:
            audit_service.add_audit(
                session, None, "DISC_BURN_FAILED" if job.error else "DISC_BURN_VERIFIED",
                performed_by=username, study_instance_uid=job.study_uid,
            )
            await session.commit()
    except Exception as exc:
        log.error("Could not record disc burn result error_type=%s", type(exc).__name__)
    finally:
        job.phase = "failed" if job.error else "verified"


def _disc_burn_active() -> bool:
    return any(job.phase not in {"verified", "failed"} for job in _disc_jobs.values())


@router.get("/disc/recorders", dependencies=[Depends(require_admin)])
async def disc_recorders():
    try:
        return {"recorders": await asyncio.to_thread(disc_burn_service.list_recorders)}
    except (disc_burn_service.DiscBurnError, OSError) as exc:
        detail = str(exc) if isinstance(exc, disc_burn_service.DiscBurnError) else "Recorder discovery is unavailable."
        raise HTTPException(503, detail) from None


@router.get("/disc/media", dependencies=[Depends(require_admin)])
async def disc_media(drive: str | None = Query(None, pattern=r"^([A-Za-z]:)?$")):
    if _disc_burn_active():
        raise HTTPException(409, "A burn is in progress. Check the media after it finishes.")
    selected = get_settings().DISC_BURN_DRIVE if drive is None else drive
    try:
        return await asyncio.to_thread(disc_burn_service.check_media, selected)
    except disc_burn_service.DiscBurnError as exc:
        raise HTTPException(409, str(exc)) from None
    except OSError:
        raise HTTPException(503, "The server could not check the recorder or temporary storage.") from None


@router.get("/disc/activity", dependencies=[Depends(require_admin)])
async def disc_activity():
    # Keep patient identity out of the settings view. Audit logs retain the full record.
    jobs = [
        {**job.progress(), "drive": job.drive, "started_at": job.started_at}
        for job in list(reversed(_disc_jobs.values()))[:10]
    ]
    return {"jobs": jobs, "active": _disc_burn_active()}


@router.post("/disc/{job_id}", status_code=202)
async def start_disc_burn(
    job_id: UUID,
    req: DiscBurnRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    study = await require_study_access(db, req.study_uid, user)
    settings = get_settings()
    if not settings.DISC_BURN_ENABLED:
        raise HTTPException(403, "CD/DVD burning is disabled in Settings")
    drive = settings.DISC_BURN_DRIVE
    volume_label = settings.DISC_BURN_VOLUME_LABEL
    rows = (await db.execute(
        select(
            Instance.sop_instance_uid, Series.series_instance_uid,
            Instance.file_path, Instance.storage_backend, Instance.file_size_bytes,
        )
        .join(Series, Instance.series_id == Series.id)
        .where(Series.study_id == study.id)
        .order_by(Instance.id)
    )).all()
    if not rows:
        raise HTTPException(404, "This study has no stored DICOM instances")

    previous = _disc_jobs.get(job_id)
    if previous:
        if previous.owner != user.id or previous.study_uid != req.study_uid:
            raise HTTPException(409, "This burn job ID is already in use")
        return previous.progress()

    if _disc_burn_active():
        raise HTTPException(409, "A CD/DVD burn is already in progress")
    if len(_disc_jobs) >= 128:
        del _disc_jobs[next(iter(_disc_jobs))]
    job = DiscBurnJob(user.id, req.study_uid, len(rows), drive.upper())
    _disc_jobs[job_id] = job
    username = user.username
    try:
        audit_service.add_audit(
            db, None, "DISC_BURN_REQUESTED", performed_by=username, study_instance_uid=req.study_uid,
        )
        await db.commit()
    except BaseException:
        del _disc_jobs[job_id]
        raise

    instances = [disc_burn_service.DiscInstance(*row) for row in rows]
    background.add_task(_run_disc_job, job, username, drive, instances, volume_label)
    return job.progress()


@router.get("/disc/{job_id}")
async def get_disc_burn(job_id: UUID, user: User = Depends(get_current_user_any)):
    job = _disc_jobs.get(job_id)
    if job is None or job.owner != user.id:
        raise HTTPException(404, "Disc burn job not found")
    return job.progress()


@router.post("", response_class=Response)
async def export_dicomdir_package(
    req: ExportDICOMDIRRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate and download a standard DICOMDIR export ZIP archive (contains DICOMDIR file + IMAGES/)."""
    if not req.study_instance_uids:
        raise HTTPException(400, "study_instance_uids list cannot be empty")
    # C04: a bulk package must not mix in another institution's studies.
    for _uid in req.study_instance_uids:
        await require_study_access(db, _uid, user)

    try:
        zip_bytes, filename = await dicomdir_service.export_dicomdir_package(
            db, req.study_instance_uids, performed_by=user.username
        )
        await db.commit()

        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as val_err:
        raise HTTPException(404, str(val_err)) from val_err
    except Exception as exc:  # storage/export failures may contain PHI or paths
        log.error("dicomdir_export_failed error_type=%s", type(exc).__name__)
        raise HTTPException(500, "DICOMDIR export failed") from None


@router.post("/preview", response_class=JSONResponse)
async def preview_dicomdir_structure(
    req: ExportDICOMDIRRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Preview the DICOMDIR record hierarchy structure (PATIENT -> STUDY -> SERIES -> IMAGE)."""
    if not req.study_instance_uids:
        raise HTTPException(400, "study_instance_uids list cannot be empty")
    # C04
    for _uid in req.study_instance_uids:
        await require_study_access(db, _uid, user)

    stmt = (
        select(Study)
        .options(
            joinedload(Study.patient),
            joinedload(Study.series_list).joinedload(Series.instances),
        )
        .where(Study.study_instance_uid.in_(req.study_instance_uids))
    )
    studies = list((await db.execute(stmt)).scalars().unique().all())
    if not studies:
        raise HTTPException(404, "No matching studies found")

    studies_structure = []
    img_counter = 1
    for st in studies:
        st_data = {
            "patient_id": st.patient.patient_id if st.patient else "UNKNOWN",
            "patient_name": st.patient.patient_name if st.patient else "UNKNOWN",
            "study_instance_uid": st.study_instance_uid,
            "series_count": len(st.series_list),
            "series_list": [],
        }
        for ser in st.series_list:
            ser_data = {
                "series_instance_uid": ser.series_instance_uid,
                "modality": ser.modality,
                "image_count": len(ser.instances),
                "instances": [
                    {
                        "sop_instance_uid": inst.sop_instance_uid,
                        "relative_file_id": ["IMAGES", f"IMG{img_counter + idx:05d}"],
                    }
                    for idx, inst in enumerate(ser.instances)
                ],
            }
            img_counter += len(ser.instances)
            st_data["series_list"].append(ser_data)
        studies_structure.append(st_data)

    ds = dicomdir_service.create_dicomdir_dataset(studies_structure)
    records_count = len(ds.DirectoryRecordSequence) if hasattr(ds, "DirectoryRecordSequence") else 0

    return JSONResponse(
        {
            "sop_class_uid": ds.file_meta.MediaStorageSOPClassUID,
            "total_directory_records": records_count,
            "studies": studies_structure,
        }
    )
