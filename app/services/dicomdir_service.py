"""DICOMDIR Service for Portable Media Exports (CD/DVD/USB).

Generates standard DICOM Part 10 DICOMDIR index catalog structures
(SOP Class 1.2.840.10008.1.3.10) with Directory Record Sequences (PATIENT -> STUDY -> SERIES -> IMAGE)
and packages portable zip/folder exports according to DICOM Media Exchange standards.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from typing import Any

import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import Instance, Series, Study
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


async def export_dicomdir_package(
    session: AsyncSession,
    study_instance_uids: list[str],
    performed_by: str = "system",
) -> tuple[bytes, str]:
    """Generate a DICOMDIR export ZIP archive containing DICOMDIR index file + IMAGES/ directory."""
    # 1. Fetch studies
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

    # 2. Build structure dictionary
    studies_structure = []
    file_map: list[tuple[str, list[str]]] = []  # (actual_file_path, relative_file_parts)
    img_counter = 1

    for st in studies:
        st_data = {
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
            ser_data = {
                "series_instance_uid": ser.series_instance_uid,
                "modality": ser.modality,
                "series_number": ser.series_number,
                "series_description": ser.series_description,
                "instances": [],
            }

            for inst in ser.instances:
                filename = f"IMG{img_counter:05d}"
                rel_parts = ["IMAGES", filename]
                img_counter += 1

                ser_data["instances"].append(
                    {
                        "sop_instance_uid": inst.sop_instance_uid,
                        "sop_class_uid": inst.sop_class_uid,
                        "relative_file_id": rel_parts,
                    }
                )

                if storage_service.exists(inst.file_path, backend_name=inst.storage_backend):
                    file_map.append((inst.file_path, rel_parts, inst.storage_backend))

            st_data["series_list"].append(ser_data)
        studies_structure.append(st_data)

    # 3. Create DICOMDIR Dataset
    dicomdir_ds = create_dicomdir_dataset(studies_structure)

    # 4. Build ZIP file in memory
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Write DICOMDIR at root
        dcmdir_bytes = io.BytesIO()
        dicomdir_ds.save_as(dcmdir_bytes, enforce_file_format=True)
        zf.writestr("DICOMDIR", dcmdir_bytes.getvalue())

        # Write image files under IMAGES/
        for abs_path, rel_parts, backend in file_map:
            rel_path_str = "/".join(rel_parts)
            raw_dcm = storage_service.read_bytes(abs_path, backend_name=backend)
            zf.writestr(rel_path_str, raw_dcm)

    # 5. Audit Log
    for st in studies:
        audit_service.add_audit(
            session,
            sop_instance_uid=None,
            study_instance_uid=st.study_instance_uid,
            action="EXPORT_DICOMDIR_CREATED",
            performed_by=performed_by,
        )

    zip_bytes = zip_buf.getvalue()
    export_filename = f"DICOM_EXPORT_{studies[0].patient.patient_id if studies[0].patient else 'MEDIA'}.zip"
    log.info("Generated DICOMDIR ZIP export (%d bytes, %d files)", len(zip_bytes), len(file_map))
    return zip_bytes, export_filename
