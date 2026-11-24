"""Research export: download a study as de-identified DICOM (ZIP).

Auth-gated (any logged-in user). Produces a zip where every instance has its
PHI stripped and its UIDs remapped consistently, per app/services/anonymizer.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.v1.deps import get_current_user, require_csrf, require_study_access
from app.core.concurrency import run_blocking
from app.core.response_admission import (
    AdmittedStreamingResponse,
    release_response_bytes,
    reserve_response_bytes,
)
from app.db.models import AuditLog, Instance, Series, Study, User
from app.db.session import get_db
from app.services import anonymizer_service as anonymizer
from app.services import client_media_service, dicomdir_service
from app.services.export_job_service import export_jobs

router = APIRouter(prefix="/api/v1", tags=["export"])
log = logging.getLogger("paxs.api.export")


def _export_workspace() -> Iterator[Path]:
    """Keep the temporary anonymized ZIP on disk until streaming completes."""
    with TemporaryDirectory(prefix="paxs-anon-export-") as temp:
        yield Path(temp)


def _build_anonymized_zip(
    workspace: Path,
    study_uid: str,
    paths: list[str],
    backends: dict[str, str | None],
    estimated_bytes: int,
    date_shift_salt: bytes | None,
) -> tuple[Path, int]:
    if estimated_bytes > 0 and shutil.disk_usage(workspace).free < estimated_bytes:
        raise OSError("Insufficient temporary disk space for anonymized export")
    destination = workspace / f"paxs_anonymized_{study_uid}.zip"
    anonymizer.anonymize_files_to_zip_file(
        paths,
        destination,
        zip_name=f"{study_uid}.zip",
        backends=backends,
        date_shift_salt=date_shift_salt,
    )
    return destination, destination.stat().st_size


def _admitted_zip_response(zip_path: Path, zip_size: int, filename: str) -> AdmittedStreamingResponse:
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


@router.get(
    "/export/{study_uid}/anonymized",
    responses={200: {"content": {"application/zip": {}}}},
    summary="Download a study as anonymized DICOM (ZIP)",
)
async def export_anonymized(
    study_uid: str,
    shift_dates: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    workspace: Path = Depends(_export_workspace),
) -> AdmittedStreamingResponse:
    # C04: an export is a bulk read — resolve it through the tenant guard.
    study = await require_study_access(db, study_uid, user)
    instances = (
        await db.execute(
            select(Instance)
            .options(joinedload(Instance.series))
            .join(Series, Instance.series_id == Series.id)
            .where(Series.study_id == study.id)
            .order_by(Instance.id)
        )
    ).scalars().all()
    if not instances:
        raise HTTPException(404, "Study has no instances")

    paths = [i.file_path for i in instances]
    backends = {i.file_path: i.storage_backend for i in instances}
    estimated_bytes = sum(max(0, int(i.file_size_bytes or 0)) for i in instances)
    salt = anonymizer.derive_tenant_date_salt(user.tenant_id) if shift_dates else None
    try:
        zip_path, zip_size = await run_blocking(
            _build_anonymized_zip,
            workspace, study_uid, paths, backends, estimated_bytes, salt,
        )
    except HTTPException:
        raise
    except FileNotFoundError:
        log.warning("anonymized_export_source_missing")
        raise HTTPException(422, "Stored DICOM object is unavailable") from None
    except Exception as exc:  # pydicom and storage errors can embed PHI or paths
        log.error("anonymized_export_failed error_type=%s", type(exc).__name__)
        raise HTTPException(422, "Anonymized export failed; stored data could not be processed") from None

    reserve_response_bytes(zip_size)
    try:
        db.add(AuditLog(study_instance_uid=study_uid, action="EXPORTED", performed_by=user.username))
        return _admitted_zip_response(zip_path, zip_size, f"paxs_anonymized_{study_uid}.zip")
    except BaseException:
        release_response_bytes(zip_size)
        raise


class AsyncExportJobRequest(BaseModel):
    study_uids: list[str] = Field(..., min_length=1, max_length=25)
    kind: Literal["anonymized", "dicomdir"] = "anonymized"
    shift_dates: bool = False


async def _prepare_anonymized_builder(
    db: AsyncSession, study_uid: str, shift_dates: bool, tenant_id: int | None
) -> Any:
    instances = (
        await db.execute(
            select(Instance)
            .join(Series, Instance.series_id == Series.id)
            .join(Study, Series.study_id == Study.id)
            .where(Study.study_instance_uid == study_uid)
            .order_by(Instance.id)
        )
    ).scalars().all()
    if not instances:
        raise HTTPException(404, "Study has no instances")
    paths = [i.file_path for i in instances]
    backends = {i.file_path: i.storage_backend for i in instances}
    estimated = sum(max(0, int(i.file_size_bytes or 0)) for i in instances)
    salt = anonymizer.derive_tenant_date_salt(tenant_id) if shift_dates else None
    filename = f"paxs_anonymized_{study_uid}.zip"

    def _build(workspace: Path) -> tuple[Path, str, int]:
        zip_path, zip_size = _build_anonymized_zip(
            workspace, study_uid, paths, backends, estimated, salt
        )
        return zip_path, filename, zip_size

    return _build


async def _prepare_dicomdir_builder(db: AsyncSession, study_uids: list[str]) -> Any:
    studies = await dicomdir_service._load_studies(db, study_uids)
    structure, candidates = dicomdir_service._build_studies_structure(studies)
    filename = dicomdir_service._export_filename(studies)

    def _build(workspace: Path) -> tuple[Path, str, int]:
        dest = workspace / filename
        dicomdir_service._write_dicomdir_zip(dest, structure, candidates)
        return dest, filename, dest.stat().st_size

    return _build


@router.post("/export/jobs", status_code=202, dependencies=[Depends(require_csrf)])
async def start_async_export_job(
    req: AsyncExportJobRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Start an asynchronous export job (202 Accepted) for large/multi-GB studies."""
    for uid in req.study_uids:
        await require_study_access(db, uid, user)
    if req.kind == "anonymized":
        if len(req.study_uids) != 1:
            raise HTTPException(400, "Anonymized export jobs accept one study_uid at a time")
        builder = await _prepare_anonymized_builder(
            db, req.study_uids[0], req.shift_dates, user.tenant_id
        )
    else:
        try:
            builder = await _prepare_dicomdir_builder(db, req.study_uids)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
    try:
        job = export_jobs.create_job(user.id, req.kind, req.study_uids, builder)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from None
    for uid in req.study_uids:
        db.add(AuditLog(study_instance_uid=uid, action=f"EXPORT_JOB_{req.kind.upper()}", performed_by=user.username))
    response.headers["X-PAXS-Node"] = job.owner_node
    return job.snapshot()


@router.get("/export/jobs/{job_id}")
async def get_async_export_job(
    job_id: str,
    response: Response,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Poll status of an asynchronous export job (local or Redis-mirrored)."""
    snap = export_jobs.get_status_for_owner(job_id, user.id)
    if snap is None:
        raise HTTPException(404, "Export job not found")
    if snap.get("owner_node"):
        response.headers["X-PAXS-Node"] = str(snap["owner_node"])
    return snap


@router.get("/export/jobs/{job_id}/download")
async def download_async_export_job(
    job_id: str,
    user: User = Depends(get_current_user),
) -> AdmittedStreamingResponse:
    """Stream the completed ZIP archive for an asynchronous export job."""
    job = export_jobs.get_for_owner(job_id, user.id)
    if job is None:
        remote = export_jobs.get_status_for_owner(job_id, user.id)
        if remote is not None and remote.get("owner_node"):
            raise HTTPException(
                409,
                f"Export file resides on node '{remote['owner_node']}'; route via sticky export backend.",
                headers={"X-PAXS-Node": str(remote["owner_node"])},
            )
        raise HTTPException(404, "Export job not found")
    if job.phase != "ready" or job.file_path is None or not job.file_path.is_file():
        raise HTTPException(409, f"Export job is not ready (phase={job.phase})")
    reserve_response_bytes(job.size_bytes)
    try:
        return _admitted_zip_response(job.file_path, job.size_bytes, job.filename or f"{job_id}.zip")
    except BaseException:
        release_response_bytes(job.size_bytes)
        raise


@router.delete("/export/jobs/{job_id}")
async def delete_async_export_job(
    job_id: str,
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    """Delete an export job and immediately reclaim its temporary disk workspace."""
    if not export_jobs.delete_for_owner(job_id, user.id):
        raise HTTPException(404, "Export job not found")
    return {"deleted": True}
