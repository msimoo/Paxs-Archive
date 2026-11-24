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
import hashlib
import hmac
import io
import logging
import shutil
import tempfile
import zipfile
from collections.abc import Collection
from datetime import datetime, timedelta

from pathlib import Path
from pydicom.dataset import Dataset
from pydicom.tag import BaseTag, Tag
from pydicom.uid import generate_uid

from app.core.config import settings

log = logging.getLogger("paxs.anonymizer")

# Tag keywords considered PHI under PS3.15 Basic Profile.
PS315_DEIDENTIFICATION_METHOD = (
    "DICOM PS 3.15 Basic Application Level Confidentiality Profile"
)

# An empty allowlist is the safe institutional profile. Callers may pass an
# explicit set of exact private tags when a validated local profile requires it.
SAFE_PRIVATE_TAGS: frozenset[int] = frozenset()

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


def derive_tenant_date_salt(tenant_id: int | str | None) -> bytes:
    """Derive a stable, secret tenant salt without exposing tenant identity.

    The deployment secret acts as the encryption/key-encryption boundary; the
    returned HMAC output is never written into the DICOM export. Different
    tenants receive unrelated deterministic date shifts.
    """
    scope = "legacy" if tenant_id is None else str(tenant_id)
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        f"paxs-anonymizer-date-shift-v1:{scope}".encode(),
        hashlib.sha256,
    ).digest()


def _date_shift_days(salt: bytes) -> int:
    """Return a deterministic offset in the PS3.15-compatible bounded range."""
    digest = hmac.new(salt, b"date-offset-v1", hashlib.sha256).digest()
    return int.from_bytes(digest[:2], "big") % 7301 - 3650


def _shift_date_value(value: object, days: int) -> object:
    """Shift a DICOM DA value while preserving its precision when possible."""
    text = str(value or "")
    formats = (("%Y%m%d", 8), ("%Y%m", 6), ("%Y", 4))
    for fmt, width in formats:
        if len(text) != width:
            continue
        try:
            parsed = datetime.strptime(text, fmt).date() + timedelta(days=days)
        except ValueError:
            return ""
        return parsed.strftime(fmt)
    return ""


def _normalise_private_allowlist(tags: Collection[int | BaseTag] | None) -> set[int]:
    """Convert an explicit institutional private-tag profile to integers."""
    return {int(Tag(tag)) for tag in (tags or SAFE_PRIVATE_TAGS)}


def _walk_dataset(
    ds: Dataset,
    *,
    private_allowlist: set[int],
    date_shift_days: int | None,
    remove_dates: bool,
) -> None:
    """Recursively sanitize a Dataset and every item in its SQ elements."""
    remove_keywords = set(PHI_KEYWORDS) - set(DATE_KEYWORDS)
    for elem in list(ds):
        if elem.tag.is_private and int(elem.tag) not in private_allowlist:
            del ds[elem.tag]
            continue
        if elem.keyword in remove_keywords or elem.keyword in PHI_SEQUENCES:
            del ds[elem.tag]
            continue
        if elem.VR == "SQ":
            for item in elem.value or []:
                if isinstance(item, Dataset):
                    _walk_dataset(
                        item,
                        private_allowlist=private_allowlist,
                        date_shift_days=date_shift_days,
                        remove_dates=remove_dates,
                    )
            continue
        if elem.keyword in DATE_KEYWORDS:
            if date_shift_days is not None:
                elem.value = _shift_date_value(elem.value, date_shift_days)
            elif remove_dates:
                del ds[elem.tag]
        elif remove_dates and elem.keyword in TIME_KEYWORDS:
            del ds[elem.tag]


def _remap_root_uids(ds: Dataset, uid_map: dict[str, str]) -> None:
    """Remap identifying root UIDs while leaving SOP class/type UIDs intact."""
    for attr in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "FrameOfReferenceUID"):
        old = getattr(ds, attr, None)
        if old is not None:
            setattr(ds, attr, uid_map.setdefault(str(old), generate_uid()))


def anonymize_dataset(
    ds: Dataset,
    uid_map: dict[str, str],
    *,
    date_shift_salt: bytes | None = None,
    private_tag_whitelist: Collection[int | BaseTag] | None = None,
) -> Dataset:
    """Return a recursively de-identified deep copy of ``ds``.

    ``date_shift_salt`` enables a deterministic tenant-specific date offset;
    omitting it preserves the existing safe date-erasure behavior. Private
    tags are always removed unless explicitly listed in the institutional
    allowlist. The source dataset and file meta are never mutated.
    """
    out = copy.deepcopy(ds)
    private_allowlist = _normalise_private_allowlist(private_tag_whitelist)
    if date_shift_salt is not None and not date_shift_salt:
        raise ValueError("date_shift_salt must not be empty")
    shift_days = _date_shift_days(date_shift_salt) if date_shift_salt is not None else None
    remove_dates = date_shift_salt is None and settings.ANONYMIZER_STRIP_DATES
    _walk_dataset(
        out,
        private_allowlist=private_allowlist,
        date_shift_days=shift_days,
        remove_dates=remove_dates,
    )
    if out.file_meta:
        _walk_dataset(
            out.file_meta,
            private_allowlist=private_allowlist,
            date_shift_days=shift_days,
            remove_dates=remove_dates,
        )

    _remap_root_uids(out, uid_map)
    if out.file_meta and getattr(out.file_meta, "MediaStorageSOPInstanceUID", None):
        out.file_meta.MediaStorageSOPInstanceUID = uid_map.setdefault(
            str(out.file_meta.MediaStorageSOPInstanceUID), generate_uid()
        )

    out.PatientIdentityRemoved = "YES"
    out.DeidentificationMethod = PS315_DEIDENTIFICATION_METHOD

    for elem in list(out):
        if elem.tag.element == 0x0000 and not elem.tag.is_private:
            del out[elem.tag]
    return out


def _write_anonymized_zip(
    target: io.BytesIO | Path,
    file_paths: list[str],
    zip_name: str | None = None,
    backends: dict[str, str | None] | None = None,
    *,
    date_shift_salt: bytes | None = None,
    private_tag_whitelist: Collection[int | BaseTag] | None = None,
) -> int:
    """De-identify ``file_paths`` into ``target`` (file path or BytesIO) and return UID count."""
    uid_map: dict[str, str] = {}
    archive_name = zip_name or f"anonymized_{datetime.now():%Y%m%d_%H%M%S}.zip"
    from app.services import storage_service

    backend_map = backends or {}
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for key in file_paths:
            ds = storage_service.read_dataset(key, backend_name=backend_map.get(key))
            clean = anonymize_dataset(
                ds,
                uid_map,
                date_shift_salt=date_shift_salt,
                private_tag_whitelist=private_tag_whitelist,
            )
            member = (
                f"{clean.StudyInstanceUID}/{clean.SeriesInstanceUID}/"
                f"{clean.SOPInstanceUID}.dcm"
            )
            with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spooled:
                clean.save_as(spooled, enforce_file_format=True)
                spooled.seek(0)
                with zf.open(member, "w") as out_member:
                    shutil.copyfileobj(spooled, out_member, length=1024 * 1024)

    log.info("Anonymized %d files -> %s (%d UIDs remapped)",
             len(file_paths), archive_name, len(uid_map))
    return len(uid_map)


def anonymize_files_to_zip_file(
    file_paths: list[str],
    destination: Path,
    zip_name: str | None = None,
    backends: dict[str, str | None] | None = None,
    *,
    date_shift_salt: bytes | None = None,
    private_tag_whitelist: Collection[int | BaseTag] | None = None,
) -> Path:
    """Write an anonymized study ZIP directly to ``destination`` on disk without RAM buffering."""
    _write_anonymized_zip(
        destination,
        file_paths,
        zip_name=zip_name or destination.name,
        backends=backends,
        date_shift_salt=date_shift_salt,
        private_tag_whitelist=private_tag_whitelist,
    )
    return destination


def anonymize_files_to_zip(
    file_paths: list[str], zip_name: str | None = None,
    backends: dict[str, str | None] | None = None,
    *,
    date_shift_salt: bytes | None = None,
    private_tag_whitelist: Collection[int | BaseTag] | None = None,
) -> bytes:
    """Read DICOM files, de-identify each, and return a ZIP archive as bytes.

    UID remapping is consistent across all files in one call, so a multi-file
    study stays linked after export. `backends` maps each storage key to its
    multi-storage backend name (None = env backend).
    """
    buf = io.BytesIO()
    _write_anonymized_zip(
        buf,
        file_paths,
        zip_name=zip_name,
        backends=backends,
        date_shift_salt=date_shift_salt,
        private_tag_whitelist=private_tag_whitelist,
    )
    return buf.getvalue()
