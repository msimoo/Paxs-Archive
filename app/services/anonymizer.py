"""DICOM de-identification (anonymizer) for research export.

Implements a practical subset of the DICOM PS3.15 Basic Application Level
Confidentiality Profile:

  * Removes the standard PHI tags (patient identifiers, names, addresses,
    physician names, device IDs, institution, accession, comments...).
  * Remaps Study/Series/SOP/Frame-of-Reference UIDs to fresh values so the
    exported study remains internally consistent but untraceable.
  * Optionally strips private (vendor) tags and date/time elements.
  * Optionally zeroes the pixel data? No — we keep pixels (that's the point
    of an imaging export). Burned-in annotation in pixels is out of scope.

Output is a ZIP archive of anonymized DICOM files named
`{StudyUID}/{SeriesUID}/{SOPUID}.dcm`.
"""

from __future__ import annotations

import copy
import io
import logging
import zipfile
from datetime import datetime

from pydicom.dataset import Dataset
from pydicom.uid import generate_uid

from app.core.config import settings

log = logging.getLogger("paxs.anonymizer")

# Tag keywords considered PHI under PS3.15 Basic Profile.
PHI_KEYWORDS = [
    # Patient identity
    "PatientName", "PatientID", "IssuerOfPatientID", "OtherPatientIDs",
    "OtherPatientNames", "PatientBirthDate", "PatientBirthTime",
    "PatientSex", "PatientAge", "PatientWeight", "PatientSize",
    "PatientAddress", "PatientTelephoneNumbers", "PatientMotherBirthName",
    "MedicalRecordLocator", "MilitaryRank", "BranchOfService",
    "CountryOfResidence", "RegionOfResidence", "PatientComments",
    # Institution / operator / physician
    "InstitutionName", "InstitutionAddress", "InstitutionCodeSequence",
    "InstitutionalDepartmentName", "PerformingPhysicianName",
    "PerformingPhysicianIdentificationSequence", "ReferringPhysicianName",
    "ReferringPhysicianIdentificationSequence", "RequestingPhysician",
    "NameOfPhysiciansReadingStudy", "PhysiciansOfRecord",
    "OperatorsName", "OperatorIdentificationSequence",
    "StationName", "DeviceSerialNumber", "DeviceUID",
    "PrimaryDeviceType", "SoftwareVersions",
    # Study / scheduling identifiers
    "AccessionNumber", "StudyID", "RequestedProcedureID",
    "ScheduledProcedureStepID", "AdmissionID", "VisitID",
    # Free text that commonly leaks identifiers
    "StudyDescription", "SeriesDescription", "ProtocolName",
    "RequestedProcedureDescription", "ScheduledProcedureStepDescription",
    "PerformedProcedureStepDescription", "ReasonForStudy",
    "RequestAttributesSequence", "ContentDescription", "ImageComments",
]

# Elements whose presence alone can identify the source dataset.
PHI_SEQUENCES = [
    "ReferringPhysicianIdentificationSequence",
    "PerformingPhysicianIdentificationSequence",
    "OperatorIdentificationSequence",
    "InstitutionCodeSequence",
    "OtherPatientIDsSequence",
    "PatientClinicalTrialParticipationSequence",
]

DATE_KEYWORDS = [
    "StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate",
    "InstanceCreationDate", "PerformedProcedureStepStartDate",
    "ScheduledProcedureStepStartDate",
    "PatientBirthDate",
]

TIME_KEYWORDS = [
    "StudyTime", "SeriesTime", "AcquisitionTime", "ContentTime",
    "InstanceCreationTime", "PerformedProcedureStepStartTime",
    "ScheduledProcedureStepStartTime",
]


# Keep only keywords this pydicom build actually knows (keyword names drift
# between versions; unknown ones would only produce spurious warnings).
def _known(keywords: list[str]) -> list[str]:
    from pydicom.datadict import keyword_dict

    return [k for k in keywords if k in keyword_dict]


PHI_KEYWORDS = _known(PHI_KEYWORDS)
PHI_SEQUENCES = _known(PHI_SEQUENCES)
DATE_KEYWORDS = _known(DATE_KEYWORDS)
TIME_KEYWORDS = _known(TIME_KEYWORDS)


def anonymize_dataset(ds: Dataset, uid_map: dict[str, str]) -> Dataset:
    """Return a de-identified deep copy of `ds` using `uid_map` for UID remapping.

    ``copy.deepcopy`` (not ``Dataset.copy``) so the caller's dataset and its
    ``file_meta`` are never mutated."""
    out = copy.deepcopy(ds)

    # 1. Strip PHI tags.
    for kw in PHI_KEYWORDS:
        if kw in out:
            del out[kw]
    for kw in PHI_SEQUENCES:
        if kw in out:
            del out[kw]

    # 2. Optional private tag stripping (group odd numbers).
    if settings.ANONYMIZER_STRIP_PRIVATE_TAGS:
        for elem in list(out):
            if elem.tag.is_private and elem.tag.group % 2 == 1:
                del out[elem.tag]

    # 3. Optional date/time stripping (shifted dates are a better option for
    #    longitudinal research, but removal is the safe default).
    if settings.ANONYMIZER_STRIP_DATES:
        for kw in DATE_KEYWORDS:
            if kw in out:
                del out[kw]
        for kw in TIME_KEYWORDS:
            if kw in out:
                del out[kw]

    # 4. Remap identifying UIDs consistently.
    def _remap(attr: str) -> None:
        old = getattr(out, attr, None)
        if old is not None:
            setattr(out, attr, uid_map.setdefault(str(old), generate_uid()))

    _remap("StudyInstanceUID")
    _remap("SeriesInstanceUID")
    _remap("SOPInstanceUID")
    _remap("FrameOfReferenceUID")
    if hasattr(out, "file_meta") and out.file_meta:
        meta = out.file_meta
        # MediaStorageSOPInstanceUID must follow the (remapped) SOPInstanceUID.
        if getattr(meta, "MediaStorageSOPInstanceUID", None):
            meta.MediaStorageSOPInstanceUID = uid_map.setdefault(
                meta.MediaStorageSOPInstanceUID, generate_uid()
            )
        # MediaStorageSOPClassUID identifies the IOD *type* (CT Image Storage…),
        # not the patient — remapping it would corrupt the file. Leave it.
        out.file_meta = meta

    # 5. Drop Group Length elements left dangling after deletions
    #    (they cause pydicom save warnings). These are elements with an
    #    even group number and element number 0x0000 (deprecated in the
    #    standard; harmless to remove).
    for elem in list(out):
        if elem.tag.element == 0x0000 and not elem.tag.is_private:
            del out[elem.tag]

    return out


def anonymize_files_to_zip(
    file_paths: list[str], zip_name: str | None = None,
    backends: dict[str, str | None] | None = None,
) -> bytes:
    """Read DICOM files, de-identify each, and return a ZIP archive as bytes.

    UID remapping is consistent across all files in one call, so a multi-file
    study stays linked after export. `backends` maps each storage key to its
    multi-storage backend name (None = env backend).
    """
    uid_map: dict[str, str] = {}
    buf = io.BytesIO()
    archive_name = zip_name or f"anonymized_{datetime.now():%Y%m%d_%H%M%S}.zip"

    from app.services import storage_service

    backends = backends or {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key in file_paths:
            ds = storage_service.read_dataset(key, backend_name=backends.get(key))
            clean = anonymize_dataset(ds, uid_map)
            member = (
                f"{clean.StudyInstanceUID}/{clean.SeriesInstanceUID}/"
                f"{clean.SOPInstanceUID}.dcm"
            )
            b = io.BytesIO()
            clean.save_as(b, enforce_file_format=True)
            zf.writestr(member, b.getvalue())

    log.info("Anonymized %d files -> %s (%d UIDs remapped)",
             len(file_paths), archive_name, len(uid_map))
    return buf.getvalue()
