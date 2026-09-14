"""DICOM metadata extraction — turns a pydicom Dataset into a flat dict that
the indexing service persists into the Patient/Study/Series/Instance tables.

Everything is defensive: real-world modality files routinely omit tags, so we
never raise on a missing element.
"""

from __future__ import annotations

from pathlib import Path

import pydicom
from pydicom.dataset import Dataset

# DICOM keywords we care about, mapped to database column names.
_META_FIELDS = (
    # patient
    "patient_id",
    "patient_name",
    "patient_birth_date",
    "patient_sex",
    # study
    "study_instance_uid",
    "study_date",
    "study_time",
    "accession_number",
    "study_id",
    "study_description",
    "referring_physician",
    # series
    "series_instance_uid",
    "modality",
    "series_number",
    "series_description",
    "body_part_examined",
    "laterality",
    "frame_of_reference_uid",
    # instance
    "sop_instance_uid",
    "sop_class_uid",
    "instance_number",
    "rows",
    "columns",
    "bits_allocated",
    "photometric_interpretation",
    "slice_thickness",
)

# keyword -> the DICOM attribute to read from the dataset
_KEYWORD_MAP = {
    "patient_id": "PatientID",
    "patient_name": "PatientName",
    "patient_birth_date": "PatientBirthDate",
    "patient_sex": "PatientSex",
    "study_instance_uid": "StudyInstanceUID",
    "study_date": "StudyDate",
    "study_time": "StudyTime",
    "accession_number": "AccessionNumber",
    "study_id": "StudyID",
    "study_description": "StudyDescription",
    "referring_physician": "ReferringPhysicianName",
    "series_instance_uid": "SeriesInstanceUID",
    "modality": "Modality",
    "series_number": "SeriesNumber",
    "series_description": "SeriesDescription",
    "body_part_examined": "BodyPartExamined",
    "laterality": "Laterality",
    "frame_of_reference_uid": "FrameOfReferenceUID",
    "sop_instance_uid": "SOPInstanceUID",
    "sop_class_uid": "SOPClassUID",
    "instance_number": "InstanceNumber",
    "rows": "Rows",
    "columns": "Columns",
    "bits_allocated": "BitsAllocated",
    "photometric_interpretation": "PhotometricInterpretation",
    "slice_thickness": "SliceThickness",
}


def _clean(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, pydicom.valuerep.PersonName):
        return str(value)
    text = str(value).strip()
    return text or None


def parse_metadata(ds: Dataset) -> dict:
    """Extract flat metadata from a pydicom Dataset (file_meta set by caller)."""
    meta: dict = {}
    for key in _META_FIELDS:
        attr = _KEYWORD_MAP[key]
        value = getattr(ds, attr, None) if hasattr(ds, attr) else None
        meta[key] = _clean(value)

    # Transfer syntax lives in the file meta header, not the dataset body.
    ts = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
    meta["transfer_syntax_uid"] = str(ts) if ts else None
    return meta


def parse_file(path: str | Path) -> dict:
    """Read a DICOM file from disk and extract metadata (fast, no pixel data)."""
    ds = pydicom.dcmread(str(path), stop_before_pixels=True)
    return parse_metadata(ds)


def dataset_from_bytes(data: bytes, force: bool = False) -> Dataset:
    """Parse DICOM bytes into a Dataset (used by STOW-RS and tests)."""
    import io

    return pydicom.dcmread(io.BytesIO(data), force=force)
