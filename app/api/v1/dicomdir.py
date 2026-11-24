"""DICOMDIR Media Export REST API.

  POST /api/v1/export/dicomdir         — Download standard DICOMDIR ZIP archive for CD/DVD media
  POST /api/v1/export/dicomdir/preview — Preview DICOMDIR catalog structure without binary download
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path as ApiPath, Query, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.v1.deps import get_current_user, get_current_user_any, require_admin, require_csrf, require_study_access
from app.core.config import get_settings
from app.core.response_admission import (
    AdmittedStreamingResponse,
    release_response_bytes,
    reserve_response_bytes,
)
from app.db.models import Instance, Series, Study, User
from app.db.session import AsyncSessionLocal, get_db
from app.services import audit_service, client_media_service, dicomdir_service, disc_burn_service

log = logging.getLogger("paxs.api.dicomdir")

router = APIRouter(prefix="/api/v1/export/dicomdir", tags=["DICOMDIR Media Export"])


class ExportDICOMDIRRequest(BaseModel):
    study_instance_uids: list[str] = Field(..., description="List of StudyInstanceUIDs to package into DICOMDIR export")


def client_media_workspace() -> Iterator[Path]:
    """Keep downloaded media alive for the lifetime of the streaming response."""
    try:
        with client_media_service.media_workspace() as workspace:
            yield workspace
    except client_media_service.MediaExportError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.get("/client/{study_uid}/{media_format}", response_class=StreamingResponse)
async def download_client_media(
    media_format: client_media_service.MediaFormat,
    study_uid: str = ApiPath(min_length=1, max_length=128),
    user: User = Depends(get_current_user_any),
    db: AsyncSession = Depends(get_db),
    workspace: Path = Depends(client_media_workspace),
) -> StreamingResponse:
    """Download original DICOM files or a DICOMDIR media package."""
    study = await require_study_access(db, study_uid, user)
    try:
        instances = await client_media_service.load_instances(db, study.id)
        package = await run_in_threadpool(
            client_media_service.build_client_media, workspace, study_uid, instances, media_format,
        )
        package_info = await run_in_threadpool(package.stat)
    except client_media_service.MediaExportError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except Exception as exc:  # Storage and DICOM exceptions may contain paths or PHI.
        log.error("client_media_export_failed error_type=%s", type(exc).__name__)
        raise HTTPException(500, "The archive could not create the media package.") from None
    audit_service.add_audit(
        db, None, f"CLIENT_MEDIA_{media_format.upper().replace('-', '_')}",
        performed_by=user.username, study_instance_uid=study_uid,
    )
    await db.commit()
    return StreamingResponse(
        client_media_service.stream_package(package),
        media_type="application/x-iso9660-image" if media_format == "iso" else "application/zip",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{package.name}"',
            "Content-Length": str(package_info.st_size),
        },
    )


class DiscBurnRequest(BaseModel):
    study_uid: str = Field(min_length=1, max_length=128)
    drive: str | None = Field(default=None, pattern=r"^([A-Za-z]:)?$")


class UsbExportRequest(BaseModel):
    study_uid: str = Field(min_length=1, max_length=128)
    drive: str = Field(pattern=r"^[A-Za-z]:$")
    folder_name: str | None = Field(default=None, max_length=64)


@dataclass
class DiscBurnJob:
    owner: int
    study_uid: str
    count: int
    drive: str
    target_kind: str = "disc"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    phase: str = "queued"
    output_path: str | None = None
    error: str | None = None

    def is_terminal(self) -> bool:
        return self.phase in {"verified", "failed"}

    def progress(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "count": self.count,
            "target_kind": self.target_kind,
            "drive": self.drive,
            "output_path": self.output_path,
            "error": self.error,
        }


# shortcut: status is kept per standalone server process. Use a persistent
# job store if the Windows recorder is ever shared by multiple API replicas.
_DISC_JOBS_MAX = 128
_DISC_JOB_TTL = timedelta(minutes=60)
_disc_jobs_lock = threading.Lock()
_disc_jobs: dict[UUID, DiscBurnJob] = {}


def _prune_disc_jobs_locked() -> None:
    """Drop expired terminal jobs and enforce max size without evicting active burns."""
    now = datetime.now(timezone.utc)
    expired = [
        jid for jid, job in _disc_jobs.items()
        if job.is_terminal() and (job.finished_at or job.started_at) + _DISC_JOB_TTL <= now
    ]
    for jid in expired:
        _disc_jobs.pop(jid, None)
    while len(_disc_jobs) >= _DISC_JOBS_MAX:
        terminal = next((jid for jid, job in _disc_jobs.items() if job.is_terminal()), None)
        if terminal is None:
            break
        _disc_jobs.pop(terminal, None)


async def _record_media_audit(username: str, study_uid: str, action: str) -> None:
    try:
        async with AsyncSessionLocal() as session:
            audit_service.add_audit(
                session, None, action, performed_by=username, study_instance_uid=study_uid,
            )
            await session.commit()
    except Exception as exc:
        log.error("Could not record media export audit error_type=%s", type(exc).__name__)


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
    await _record_media_audit(username, job.study_uid, "DISC_BURN_FAILED" if job.error else "DISC_BURN_VERIFIED")
    job.phase = "failed" if job.error else "verified"
    job.finished_at = datetime.now(timezone.utc)


async def _run_usb_job(
    job: DiscBurnJob, username: str, drive: str, instances: list[disc_burn_service.DiscInstance],
    folder_name: str | None,
) -> None:
    try:
        job.output_path = await asyncio.to_thread(
            disc_burn_service.export_study_to_usb, drive, job.study_uid, instances,
            lambda phase: setattr(job, "phase", phase), folder_name=folder_name,
        )
    except disc_burn_service.DiscBurnError as exc:
        job.error = str(exc)
    except Exception as exc:
        log.error("usb_export_failed phase=%s error_type=%s", job.phase, type(exc).__name__)
        job.error = "USB export could not be completed. Check the USB drive and free space."
    await _record_media_audit(username, job.study_uid, "USB_EXPORT_FAILED" if job.error else "USB_EXPORT_VERIFIED")
    job.phase = "failed" if job.error else "verified"
    job.finished_at = datetime.now(timezone.utc)


def _disc_burn_active() -> bool:
    with _disc_jobs_lock:
        return any(
            job.target_kind == "disc" and not job.is_terminal()
            for job in _disc_jobs.values()
        )


async def _load_study_disc_instances(db: AsyncSession, study_id: int) -> list[disc_burn_service.DiscInstance]:
    rows = (await db.execute(
        select(
            Instance.sop_instance_uid, Series.series_instance_uid,
            Instance.file_path, Instance.storage_backend, Instance.file_size_bytes,
        )
        .join(Series, Instance.series_id == Series.id)
        .where(Series.study_id == study_id)
        .order_by(Instance.id)
    )).all()
    if not rows:
        raise HTTPException(404, "This study has no stored DICOM instances")
    return [disc_burn_service.DiscInstance(*row) for row in rows]


@router.get("/server/devices")
async def list_server_devices(user: User = Depends(get_current_user_any)) -> dict[str, Any]:
    """Detect optical CD/DVD recorders and removable USB drives attached to the archive server."""
    devices = await asyncio.to_thread(disc_burn_service.list_removable_devices)
    cfg = get_settings()
    return {
        **devices,
        "disc_burn_enabled": bool(cfg.DISC_BURN_ENABLED),
        "default_disc_drive": (cfg.DISC_BURN_DRIVE or "").upper(),
    }


@router.get("/server/disc/media")
async def check_server_disc_media(
    drive: str | None = Query(None, pattern=r"^([A-Za-z]:)?$"),
    user: User = Depends(get_current_user_any),
) -> dict[str, str | int]:
    """Check blank media readiness on a server optical drive for the export modal."""
    if _disc_burn_active():
        raise HTTPException(409, "A burn is in progress. Check the media after it finishes.")
    selected = get_settings().DISC_BURN_DRIVE if drive is None else drive
    try:
        return await asyncio.to_thread(disc_burn_service.check_media, selected)
    except disc_burn_service.DiscBurnError as exc:
        raise HTTPException(409, str(exc)) from None
    except OSError:
        raise HTTPException(503, "The server could not check the recorder or temporary storage.") from None


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
    with _disc_jobs_lock:
        _prune_disc_jobs_locked()
        jobs = [
            {**job.progress(), "drive": job.drive, "started_at": job.started_at}
            for job in list(reversed(_disc_jobs.values()))[:10]
        ]
    return {"jobs": jobs, "active": _disc_burn_active()}


@router.post("/disc/{job_id}", status_code=202, dependencies=[Depends(require_csrf)])
async def start_disc_burn(
    job_id: UUID,
    req: DiscBurnRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_any),
):
    study = await require_study_access(db, req.study_uid, user)
    cfg = get_settings()
    if not cfg.DISC_BURN_ENABLED:
        raise HTTPException(403, "Server CD/DVD burning is disabled in Settings (enable DISC_BURN_ENABLED)")
    drive = (req.drive or cfg.DISC_BURN_DRIVE or "").upper()
    volume_label = cfg.DISC_BURN_VOLUME_LABEL
    instances = await _load_study_disc_instances(db, study.id)

    with _disc_jobs_lock:
        _prune_disc_jobs_locked()
        previous = _disc_jobs.get(job_id)
        if previous:
            if previous.owner != user.id or previous.study_uid != req.study_uid:
                raise HTTPException(409, "This burn job ID is already in use")
            return previous.progress()

        if any(j.target_kind == "disc" and not j.is_terminal() for j in _disc_jobs.values()):
            raise HTTPException(409, "A CD/DVD burn is already in progress")
        job = DiscBurnJob(user.id, req.study_uid, len(instances), drive, target_kind="disc")
        _disc_jobs[job_id] = job
    username = user.username
    try:
        audit_service.add_audit(
            db, None, "DISC_BURN_REQUESTED", performed_by=username, study_instance_uid=req.study_uid,
        )
        await db.commit()
    except BaseException:
        with _disc_jobs_lock:
            _disc_jobs.pop(job_id, None)
        raise

    background.add_task(_run_disc_job, job, username, drive, instances, volume_label)
    return job.progress()


@router.post("/usb/{job_id}", status_code=202, dependencies=[Depends(require_csrf)])
async def start_server_usb_export(
    job_id: UUID,
    req: UsbExportRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_any),
) -> dict[str, Any]:
    """Export a complete verified DICOMDIR study directly onto a USB drive attached to the server."""
    study = await require_study_access(db, req.study_uid, user)
    instances = await _load_study_disc_instances(db, study.id)
    drive = req.drive.upper()

    with _disc_jobs_lock:
        _prune_disc_jobs_locked()
        previous = _disc_jobs.get(job_id)
        if previous:
            if previous.owner != user.id or previous.study_uid != req.study_uid:
                raise HTTPException(409, "This export job ID is already in use")
            return previous.progress()

        job = DiscBurnJob(user.id, req.study_uid, len(instances), drive, target_kind="usb")
        _disc_jobs[job_id] = job
    username = user.username
    try:
        audit_service.add_audit(
            db, None, "USB_EXPORT_REQUESTED", performed_by=username, study_instance_uid=req.study_uid,
        )
        await db.commit()
    except BaseException:
        with _disc_jobs_lock:
            _disc_jobs.pop(job_id, None)
        raise

    background.add_task(_run_usb_job, job, username, drive, instances, req.folder_name)
    return job.progress()


@router.get("/disc/{job_id}")
@router.get("/usb/{job_id}")
async def get_disc_burn(job_id: UUID, user: User = Depends(get_current_user_any)):
    with _disc_jobs_lock:
        _prune_disc_jobs_locked()
        job = _disc_jobs.get(job_id)
    if job is None or job.owner != user.id:
        raise HTTPException(404, "Media export job not found")
    return job.progress()


@router.post("", response_class=StreamingResponse)
async def export_dicomdir_package(
    req: ExportDICOMDIRRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    workspace: Path = Depends(client_media_workspace),
) -> AdmittedStreamingResponse:
    """Generate and stream a standard DICOMDIR export ZIP archive (DICOMDIR + IMAGES/)."""
    if not req.study_instance_uids:
        raise HTTPException(400, "study_instance_uids list cannot be empty")
    # C04: a bulk package must not mix in another institution's studies.
    for _uid in req.study_instance_uids:
        await require_study_access(db, _uid, user)

    try:
        zip_path, filename, zip_size = await dicomdir_service.export_dicomdir_to_file(
            db, req.study_instance_uids, workspace, performed_by=user.username
        )
        await db.commit()
    except HTTPException:
        raise
    except ValueError as val_err:
        raise HTTPException(404, str(val_err)) from val_err
    except Exception as exc:  # storage/export failures may contain PHI or paths
        log.error("dicomdir_export_failed error_type=%s", type(exc).__name__)
        raise HTTPException(500, "DICOMDIR export failed") from None

    reserve_response_bytes(zip_size)
    try:
        return AdmittedStreamingResponse(
            client_media_service.stream_package(zip_path),
            reserved_bytes=zip_size,
            media_type="application/zip",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(zip_size),
            },
        )
    except BaseException:
        release_response_bytes(zip_size)
        raise


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
