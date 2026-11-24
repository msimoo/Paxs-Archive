"""DICOMDIR Service for Portable Media Exports (CD/DVD/USB).

Generates standard DICOM Part 10 DICOMDIR index catalog structures
(SOP Class 1.2.840.10008.1.3.10) with Directory Record Sequences (PATIENT -> STUDY -> SERIES -> IMAGE)
and packages portable zip/folder exports according to DICOM Media Exchange standards.
"""

from __future__ import annotations

import contextlib
import io
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.concurrency import run_blocking
from app.db.models import Series, Study
from app.services import audit_service, storage_service

log = logging.getLogger("paxs.dicomdir")

DICOMDIR_SOP_CLASS_UID = "1.2.840.10008.1.3.10"


def create_dicomdir_dataset(studies_structure: list[dict[str, Any]]) -> FileDataset:
    """Construct a standard DICOMDIR FileDataset with PATIENT -> STUDY -> SERIES -> IMAGE records."""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = DICOMDIR_SOP_CLASS_UID
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset("DICOMDIR", {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    # Directory Record Sequence (0004,1220)
    dir_records = []

    # Group by patient
    patients_map: dict[str, dict[str, Any]] = {}
    for st_info in studies_structure:
        pid = st_info.get("patient_id") or "UNKNOWN_PATIENT"
        if pid not in patients_map:
            patients_map[pid] = {
                "patient_name": st_info.get("patient_name") or pid,
                "studies": [],
            }
        patients_map[pid]["studies"].append(st_info)

    for pid, pat_data in patients_map.items():
        # PATIENT record
        pat_rec = Dataset()
        pat_rec.DirectoryRecordType = "PATIENT"
        pat_rec.PatientID = pid
        pat_rec.PatientName = pat_data["patient_name"]
        dir_records.append(pat_rec)

        for st_info in pat_data["studies"]:
            # STUDY record
            st_rec = Dataset()
            st_rec.DirectoryRecordType = "STUDY"
            st_rec.StudyInstanceUID = st_info["study_instance_uid"]
            st_rec.StudyDate = st_info.get("study_date") or ""
            st_rec.StudyTime = st_info.get("study_time") or ""
            st_rec.AccessionNumber = st_info.get("accession_number") or ""
            st_rec.StudyDescription = st_info.get("study_description") or ""
            dir_records.append(st_rec)

            for ser_info in st_info.get("series_list", []):
                # SERIES record
                ser_rec = Dataset()
                ser_rec.DirectoryRecordType = "SERIES"
                ser_rec.SeriesInstanceUID = ser_info["series_instance_uid"]
                ser_rec.Modality = ser_info.get("modality") or "OT"
                ser_rec.SeriesNumber = str(ser_info.get("series_number") or 1)
                ser_rec.SeriesDescription = ser_info.get("series_description") or ""
                dir_records.append(ser_rec)

                for inst_info in ser_info.get("instances", []):
                    # IMAGE record
                    img_rec = Dataset()
                    img_rec.DirectoryRecordType = "IMAGE"
                    img_rec.ReferencedSOPClassUIDInFile = inst_info.get("sop_class_uid") or "1.2.840.10008.5.1.4.1.1.2"
                    img_rec.ReferencedSOPInstanceUIDInFile = inst_info["sop_instance_uid"]
                    img_rec.ReferencedFileID = inst_info.get("relative_file_id", ["IMAGES", "IMG00001"])
                    img_rec.DirectoryRecordType = "IMAGE"
                    dir_records.append(img_rec)

    ds.DirectoryRecordSequence = Sequence(dir_records)
    return ds


def _build_studies_structure(
    studies: list[Study],
) -> tuple[list[dict[str, Any]], list[tuple[str, list[str], str | None]]]:
    studies_structure: list[dict[str, Any]] = []
    candidates: list[tuple[str, list[str], str | None]] = []
    img_counter = 1
    for st in studies:
        st_data: dict[str, Any] = {
            "patient_id": st.patient.patient_id if st.patient else "UNKNOWN",
            "patient_name": st.patient.patient_name if st.patient else "UNKNOWN",
            "study_instance_uid": st.study_instance_uid,
            "study_date": st.study_date,
            "study_time": st.study_time,
            "accession_number": st.accession_number,
            "study_description": st.study_description,
            "series_list": [],
        }
        for ser in st.series_list:
            ser_data: dict[str, Any] = {
                "series_instance_uid": ser.series_instance_uid,
                "modality": ser.modality,
                "series_number": ser.series_number,
                "series_description": ser.series_description,
                "instances": [],
            }
            for inst in ser.instances:
                rel_parts = ["IMAGES", f"IMG{img_counter:05d}"]
                img_counter += 1
                ser_data["instances"].append({
                    "sop_instance_uid": inst.sop_instance_uid,
                    "sop_class_uid": inst.sop_class_uid,
                    "relative_file_id": rel_parts,
                })
                candidates.append((inst.file_path, rel_parts, inst.storage_backend))
            st_data["series_list"].append(ser_data)
        studies_structure.append(st_data)
    return studies_structure, candidates


def _write_dicomdir_zip(
    target: io.BytesIO | Path,
    studies_structure: list[dict[str, Any]],
    candidates: list[tuple[str, list[str], str | None]],
) -> int:
    """Build DICOMDIR and stream stored DICOM objects into ``target`` (off-loop safe)."""
    dicomdir_ds = create_dicomdir_dataset(studies_structure)
    written = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        dcmdir_buf = io.BytesIO()
        dicomdir_ds.save_as(dcmdir_buf, enforce_file_format=True)
        zf.writestr("DICOMDIR", dcmdir_buf.getvalue())
        for abs_path, rel_parts, backend in candidates:
            if not storage_service.exists(abs_path, backend_name=backend):
                continue
            rel_path_str = "/".join(rel_parts)
            with contextlib.closing(
                storage_service.read_stream(abs_path, backend_name=backend)
            ) as src, zf.open(rel_path_str, "w") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            written += 1
    return written


async def _load_studies(session: AsyncSession, study_instance_uids: list[str]) -> list[Study]:
    stmt = (
        select(Study)
        .options(
            joinedload(Study.patient),
            joinedload(Study.series_list).joinedload(Series.instances),
        )
        .where(Study.study_instance_uid.in_(study_instance_uids))
    )
    studies = list((await session.execute(stmt)).scalars().unique().all())
    if not studies:
        raise ValueError("No matching studies found for DICOMDIR export")
    return studies


def _export_filename(studies: list[Study]) -> str:
    patient_id = studies[0].patient.patient_id if studies[0].patient else "MEDIA"
    safe_pid = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(patient_id))[:48] or "MEDIA"
    return f"DICOM_EXPORT_{safe_pid}.zip"


async def export_dicomdir_to_file(
    session: AsyncSession,
    study_instance_uids: list[str],
    workspace: Path,
    performed_by: str = "system",
) -> tuple[Path, str, int]:
    """Generate a DICOMDIR export ZIP on disk off the event loop and return ``(path, filename, size)``."""
    studies = await _load_studies(session, study_instance_uids)
    studies_structure, candidates = _build_studies_structure(studies)
    export_filename = _export_filename(studies)
    destination = workspace / export_filename
    written = await run_blocking(_write_dicomdir_zip, destination, studies_structure, candidates)
    for st in studies:
        audit_service.add_audit(
            session,
            sop_instance_uid=None,
            study_instance_uid=st.study_instance_uid,
            action="EXPORT_DICOMDIR_CREATED",
            performed_by=performed_by,
        )
    zip_size = destination.stat().st_size
    log.info("Generated DICOMDIR ZIP export on disk (%d bytes, %d files)", zip_size, written)
    return destination, export_filename, zip_size


async def export_dicomdir_package(
    session: AsyncSession,
    study_instance_uids: list[str],
    performed_by: str = "system",
) -> tuple[bytes, str]:
    """Generate a DICOMDIR export ZIP archive as bytes (off the event loop)."""
    studies = await _load_studies(session, study_instance_uids)
    studies_structure, candidates = _build_studies_structure(studies)
    zip_buf = io.BytesIO()
    written = await run_blocking(_write_dicomdir_zip, zip_buf, studies_structure, candidates)
    for st in studies:
        audit_service.add_audit(
            session,
            sop_instance_uid=None,
            study_instance_uid=st.study_instance_uid,
            action="EXPORT_DICOMDIR_CREATED",
            performed_by=performed_by,
        )
    zip_bytes = zip_buf.getvalue()
    export_filename = _export_filename(studies)
    log.info("Generated DICOMDIR ZIP export (%d bytes, %d files)", len(zip_bytes), written)
    return zip_bytes, export_filename
