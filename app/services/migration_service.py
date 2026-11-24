"""PACS Migration Service.

Provides bulk import capability for migrating legacy imaging data into PAXS:
1. Folder Watch / Bulk Directory Ingest: Reads DICOM files recursively from a disk path,
   saves them to storage, and indexes them into the DB.
2. Remote C-MOVE Batch Import: Requests C-MOVE sub-operations from a remote DICOM Node
   to pull historical studies into PAXS.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import pydicom
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import MigrationJob
from app.db.session import AsyncSessionLocal
from app.dicom.sender import send_move
from app.services import indexing_service, storage_service
from app.services.node_service import get_node_sync

log = logging.getLogger("paxs.migration")


def scan_and_import_folder_sync(
    folder_path: str | Path, delete_after_import: bool = False
) -> dict[str, Any]:
    """Synchronous file-by-file scanner and importer for a local directory.

    Parses all files under `folder_path`, stores and indexes DICOM datasets,
    and returns progress metrics.
    """
    path = Path(folder_path)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Folder path does not exist or is not a directory: {folder_path}")

    processed = 0
    failed = 0
    errors: list[str] = []

    # Find candidate DICOM files (.dcm or any file)
    candidate_files = [f for f in path.rglob("*") if f.is_file()]

    for file_p in candidate_files:
        try:
            # Try parsing as DICOM
            ds = pydicom.dcmread(str(file_p), force=False)
            if ds is None or getattr(ds, "SOPInstanceUID", None) is None:
                continue

            # Save to PAXS storage backend and parse metadata
            stored_key, sha256 = storage_service.save_dataset(ds)
            meta = indexing_service.parse_metadata(ds) if hasattr(indexing_service, "parse_metadata") else None
            if meta is None:
                from app.dicom.parser import parse_metadata
                meta = parse_metadata(ds)

            # Index into DB using a dedicated session
            async def _index():
                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        await indexing_service.index_new_instance(
                            session, meta, stored_key, sha256, performed_by="migration-service"
                        )

            asyncio.run(_index())
            processed += 1

            if delete_after_import:
                try:
                    file_p.unlink()
                except OSError as exc:
                    log.warning("migration_source_cleanup_failed error_type=%s",
                                type(exc).__name__)

        except Exception as exc:
            # Not a valid DICOM file or store error
            failed += 1
            errors.append(f"File import failed (error_type={type(exc).__name__}).")
            log.debug("migration_file_import_failed error_type=%s",
                      type(exc).__name__)

    return {
        "total": len(candidate_files),
        "processed": processed,
        "failed": failed,
        "errors": errors[:50],  # cap error samples
    }


async def create_migration_job(
    session: AsyncSession,
    job_type: str,
    source_info: str,
) -> MigrationJob:
    """Create a new migration job entry in PENDING status."""
    job = MigrationJob(
        job_type=job_type,
        status="PENDING",
        source_info=source_info,
        total_items=0,
        processed_items=0,
        failed_items=0,
    )
    session.add(job)
    await session.flush()
    # Materialize server_default columns (created_at) eagerly — MySQL/MariaDB
    # lack RETURNING, so accessing them after the session closes would trigger
    # a lazy refresh outside the async context (MissingGreenlet).
    await session.refresh(job)
    return job


async def run_folder_migration(job_id: int, folder_path: str, delete_after: bool = False) -> None:
    """Background task runner for directory migration."""
    async with AsyncSessionLocal() as session:
        job = (
            await session.execute(select(MigrationJob).where(MigrationJob.id == job_id))
        ).scalar_one_or_none()
        if not job:
            return

        job.status = "RUNNING"
        await session.commit()

    try:
        # Run blocking folder import in thread pool
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None, scan_and_import_folder_sync, folder_path, delete_after
        )

        async with AsyncSessionLocal() as session:
            job = (
                await session.execute(select(MigrationJob).where(MigrationJob.id == job_id))
            ).scalar_one_or_none()
            if job:
                job.status = "COMPLETED"
                job.total_items = res["total"]
                job.processed_items = res["processed"]
                job.failed_items = res["failed"]
                if res["errors"]:
                    job.error_message = json.dumps(res["errors"])
                job.finished_at = datetime.utcnow()
                await session.commit()

    except Exception as exc:  # job failure is recorded without retaining exception data
        log.error("folder_migration_job_failed job_id=%s error_type=%s",
                  job_id, type(exc).__name__)
        async with AsyncSessionLocal() as session:
            job = (
                await session.execute(select(MigrationJob).where(MigrationJob.id == job_id))
            ).scalar_one_or_none()
            if job:
                job.status = "FAILED"
                job.error_message = (
                    "Folder migration failed. Review server diagnostics for the "
                    "job; patient and filesystem details are omitted."
                )
                job.finished_at = datetime.utcnow()
                await session.commit()


async def run_cmove_migration(
    job_id: int, ae_title: str, study_uids: list[str]
) -> None:
    """Background task runner for remote C-MOVE batch migration."""
    node = get_node_sync(ae_title)
    if not node or not node.enabled:
        async with AsyncSessionLocal() as session:
            job = (
                await session.execute(select(MigrationJob).where(MigrationJob.id == job_id))
            ).scalar_one_or_none()
            if job:
                job.status = "FAILED"
                job.error_message = f"Node AE '{ae_title}' not found or disabled"
                job.finished_at = datetime.utcnow()
                await session.commit()
        return

    async with AsyncSessionLocal() as session:
        job = (
            await session.execute(select(MigrationJob).where(MigrationJob.id == job_id))
        ).scalar_one_or_none()
        if not job:
            return
        job.status = "RUNNING"
        job.total_items = len(study_uids)
        await session.commit()

    processed = 0
    failed = 0
    errors = []

    loop = asyncio.get_running_loop()

    for st_uid in study_uids:
        try:
            # Construct study C-MOVE query dataset
            query_ds = pydicom.Dataset()
            query_ds.QueryRetrieveLevel = "STUDY"
            query_ds.StudyInstanceUID = st_uid

            # Execute C-MOVE via threadpool executor
            res = await loop.run_in_executor(
                None,
                partial(
                    send_move,
                    node.host,
                    node.port,
                    node.ae_title,
                    settings.DICOM_AE_TITLE,
                    query_ds,
                    # B-16: the migration source is a TLS peer like any other;
                    # its pinned certificate is the trust anchor for the SCU.
                    trust_cert=getattr(node, "trust_cert", None),
                ),
            )

            if res.get("ok"):
                processed += 1
            else:
                failed += 1
                errors.append(f"Study {st_uid}: remote retrieval failed.")
        except Exception as exc:  # noqa: BLE001 - boundary guard: one study must not abort the C-MOVE job
            # The job keeps going and the per-study reason is reported to the
            # operator; the log stays type-only (the message can carry a path).
            log.warning("migration_study_failed error_type=%s", type(exc).__name__)
            failed += 1
            errors.append(
                f"Study {st_uid}: retrieval failed (error_type={type(exc).__name__})."
            )

        # Update progress
        async with AsyncSessionLocal() as session:
            job = (
                await session.execute(select(MigrationJob).where(MigrationJob.id == job_id))
            ).scalar_one_or_none()
            if job:
                job.processed_items = processed
                job.failed_items = failed
                await session.commit()

    # Finish job
    async with AsyncSessionLocal() as session:
        job = (
            await session.execute(select(MigrationJob).where(MigrationJob.id == job_id))
        ).scalar_one_or_none()
        if job:
            job.status = "COMPLETED" if failed == 0 else "COMPLETED_WITH_ERRORS" if processed > 0 else "FAILED"
            if errors:
                job.error_message = json.dumps(errors[:50])
            job.finished_at = datetime.utcnow()
            await session.commit()
