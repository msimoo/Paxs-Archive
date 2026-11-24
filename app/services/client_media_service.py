"""Build complete DICOMDIR downloads for a workstation's removable media."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
from typing import Literal
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pycdlib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Instance, Series
from app.services import storage_service
from app.services.disc_burn_service import DiscBurnError, DiscInstance, stage_study
from app.services.storage.backend import StorageError

MediaFormat = Literal["iso", "zip", "dicom-zip"]
MAX_ISO_BYTES = 8_500_000_000
MAX_ZIP_BYTES = 16 * 1024**3
MAX_INSTANCES = 100_000
_ACTIVE_EXPORTS = BoundedSemaphore(2)


class MediaExportError(Exception):
    """A safe export error suitable for showing to the workstation operator."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@contextmanager
def media_workspace() -> Iterator[Path]:
    """Reserve a bounded slot and remove its temporary files after download."""
    if not _ACTIVE_EXPORTS.acquire(blocking=False):
        raise MediaExportError("Two media downloads are already in progress. Try again shortly.", 503)
    try:
        with TemporaryDirectory(prefix="paxs-client-media-") as temp:
            yield Path(temp)
    finally:
        _ACTIVE_EXPORTS.release()


async def load_instances(db: AsyncSession, authorized_study_id: int) -> list[DiscInstance]:
    """Snapshot every object belonging to the already-authorized study."""
    rows = (await db.execute(
        select(
            Instance.sop_instance_uid, Series.series_instance_uid,
            Instance.file_path, Instance.storage_backend, Instance.file_size_bytes,
        )
        .join(Series, Instance.series_id == Series.id)
        .where(Series.study_id == authorized_study_id)
        .order_by(Instance.id)
        .limit(MAX_INSTANCES + 1)
    )).all()
    if not rows:
        raise MediaExportError("This study has no stored DICOM instances.", 404)
    if len(rows) > MAX_INSTANCES:
        raise MediaExportError("This study exceeds the portable export instance limit.", 413)
    return [DiscInstance(*row) for row in rows]


def _iso_entry_path(rel_parts: tuple[str, ...], *, is_dir: bool) -> str:
    """Build a strict ISO-9660 Level 3 path (uppercase, safe chars, version `;1`)."""
    cleaned = ["".join(ch if ch.isalnum() or ch in "_." else "_" for ch in part.upper()) for part in rel_parts]
    joined = "/" + "/".join(cleaned)
    if is_dir:
        return joined
    last = cleaned[-1]
    return joined + (";1" if "." in last else ".;1")


def _write_iso(fileset: Path, target: Path) -> None:
    image = pycdlib.PyCdlib()
    image.new(interchange_level=3, joliet=3, vol_ident="PAXS_DICOM")
    try:
        for path in sorted(fileset.rglob("*")):
            rel = path.relative_to(fileset)
            joliet = "/" + rel.as_posix()
            if path.is_dir():
                image.add_directory(iso_path=_iso_entry_path(rel.parts, is_dir=True), joliet_path=joliet)
            else:
                image.add_file(str(path), iso_path=_iso_entry_path(rel.parts, is_dir=False), joliet_path=joliet)
        image.write(str(target))
    finally:
        image.close()


def _write_zip(fileset: Path, target: Path) -> None:
    with ZipFile(target, "w", compression=ZIP_STORED) as archive:
        for path in sorted(fileset.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(fileset).as_posix())


def _write_dicom_zip(target: Path, instances: list[DiscInstance], byte_budget: int) -> None:
    """Compress stored objects into a ZIP without changing their DICOM bytes."""
    with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=1) as archive:
        for index, instance in enumerate(instances, start=1):
            copied = 0
            try:
                with (
                    storage_service.read_stream(instance.path, backend_name=instance.backend) as source,
                    archive.open(f"DICOM/{index:06d}.dcm", "w", force_zip64=True) as destination,
                ):
                    while chunk := source.read(1024 * 1024):
                        byte_budget -= len(chunk)
                        if byte_budget < 0:
                            raise MediaExportError("The study exceeds the available ZIP export capacity.", 413)
                        destination.write(chunk)
                        copied += len(chunk)
            except (FileNotFoundError, StorageError):
                raise MediaExportError("A stored DICOM object could not be read.") from None
            if not copied or (instance.size > 0 and copied != instance.size):
                raise MediaExportError("A stored DICOM object has an unexpected size.")


def build_client_media(
    workspace: Path, study_uid: str, instances: list[DiscInstance], media_format: MediaFormat,
) -> Path:
    """Package every stored DICOM object for workstation download."""
    if not instances:
        raise MediaExportError("This study has no stored DICOM instances.", 404)
    package_limit = MAX_ISO_BYTES if media_format == "iso" else MAX_ZIP_BYTES
    source_bytes = sum(instance.size for instance in instances)
    if source_bytes > package_limit:
        raise MediaExportError("The study exceeds the selected media format limit.", 413)
    workspace.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(workspace).free
    if media_format == "dicom-zip":
        if source_bytes > free_bytes:
            raise MediaExportError("The archive lacks temporary space to prepare this study.", 507)
        target = workspace / "PAXS_STUDY.zip"
        _write_dicom_zip(target, instances, min(package_limit, free_bytes))
        if target.stat().st_size > package_limit:
            raise MediaExportError("The completed package exceeds the selected media format limit.", 413)
        return target
    # Staged source files and the finished package must fit simultaneously.
    staging_limit = min(package_limit, free_bytes // 2)
    if source_bytes > staging_limit:
        raise MediaExportError("The archive lacks temporary space to prepare this study.", 507)
    try:
        fileset, _ = stage_study(workspace, study_uid, instances, staging_limit)
    except DiscBurnError as exc:
        raise MediaExportError(str(exc).replace("No disc was written.", "No package was created.")) from None
    target = workspace / f"PAXS_DICOM.{media_format}"
    writer = _write_iso if media_format == "iso" else _write_zip
    writer(fileset, target)
    if target.stat().st_size > package_limit:
        raise MediaExportError("The completed package exceeds the selected media format limit.", 413)
    return target


def stream_package(path: Path) -> Iterator[bytes]:
    """Stream downloads without retaining the package in server or browser JS memory."""
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            yield chunk
