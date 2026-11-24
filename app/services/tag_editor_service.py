"""DICOM Tag Editor service — safe demographic/study correction across DB and stored files.

Updates patient and study metadata in PostgreSQL and rewrites every stored
DICOM instance in the study atomically (bumping ``content_version`` and updating
``metadata_json`` so WADO-RS/viewer caches stay coherent).
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.concurrency import run_blocking
from app.db.models import AuditLog, Instance, Series, Study
from app.services import content_version, storage_service
from app.services.dicomweb_service import dataset_to_json_text

log = logging.getLogger("paxs.tag_editor")

_DATE_RE = re.compile(r"^\d{8}$")
_VALID_SEX = frozenset({"", "M", "F", "O"})

_TAG_FIELDS = (
    ("patient_id", "PatientID"),
    ("patient_name", "PatientName"),
    ("patient_birth_date", "PatientBirthDate"),
    ("patient_sex", "PatientSex"),
    ("accession_number", "AccessionNumber"),
    ("study_date", "StudyDate"),
    ("study_description", "StudyDescription"),
    ("referring_physician", "ReferringPhysicianName"),
)


@dataclass(frozen=True)
class TagPatch:
    patient_id: str | None = None
    patient_name: str | None = None
    patient_birth_date: str | None = None
    patient_sex: str | None = None
    accession_number: str | None = None
    study_date: str | None = None
    study_description: str | None = None
    referring_physician: str | None = None


def validate_tag_patch(patch: TagPatch) -> dict[str, str]:
    """Validate and normalize non-None editable fields; returns field -> value map."""
    updates: dict[str, str] = {}
    for field_name, _kw in _TAG_FIELDS:
        raw = getattr(patch, field_name)
        if raw is None:
            continue
        val = str(raw).strip()
        if field_name in {"patient_birth_date", "study_date"} and val and not _DATE_RE.match(val):
            raise ValueError(f"{field_name} must be YYYYMMDD or empty")
        if field_name == "patient_sex":
            val = val.upper()
            if val not in _VALID_SEX:
                raise ValueError("patient_sex must be M, F, O, or empty")
        if field_name == "patient_id" and not val:
            raise ValueError("patient_id cannot be empty")
        updates[field_name] = val
    return updates


def rewrite_dicom_object_tags(
    file_path: str, backend_name: str | None, updates: dict[str, str]
) -> str:
    """Apply validated tag updates to one stored DICOM object and return updated WADO JSON."""
    ds = storage_service.read_dataset(file_path, backend_name=backend_name)
    for field_name, keyword in _TAG_FIELDS:
        if field_name in updates:
            setattr(ds, keyword, updates[field_name])
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    storage_service.save_bytes(file_path, buf.getvalue(), backend_name=backend_name)
    return dataset_to_json_text(ds)


def serialize_study_tags(study: Study) -> dict[str, Any]:
    """Return editable study/patient tags plus series summary for tag-editor & split UI."""
    patient = study.patient
    return {
        "study_instance_uid": study.study_instance_uid,
        "patient_id": patient.patient_id if patient else "",
        "patient_name": patient.patient_name if patient else "",
        "patient_birth_date": patient.patient_birth_date if patient else "",
        "patient_sex": patient.patient_sex if patient else "",
        "accession_number": study.accession_number or "",
        "study_date": study.study_date or "",
        "study_description": study.study_description or "",
        "referring_physician": study.referring_physician or "",
        "series": [
            {
                "series_instance_uid": ser.series_instance_uid,
                "modality": ser.modality or "",
                "series_number": ser.series_number,
                "series_description": ser.series_description or "",
                "instance_count": len(ser.instances),
            }
            for ser in study.series_list
        ],
    }


async def load_study_with_series(session: AsyncSession, study_uid: str) -> Study | None:
    stmt = (
        select(Study)
        .options(
            joinedload(Study.patient),
            joinedload(Study.series_list).joinedload(Series.instances),
        )
        .where(Study.study_instance_uid == study_uid)
    )
    return (await session.execute(stmt)).scalars().unique().one_or_none()


async def apply_study_tag_edits(
    session: AsyncSession,
    study: Study,
    patch: TagPatch,
    *,
    performed_by: str,
) -> dict[str, Any]:
    """Persist validated tag edits to PostgreSQL and rewrite each stored DICOM instance."""
    updates = validate_tag_patch(patch)
    if not updates:
        return {"updated_fields": [], "instances_updated": 0, "study": serialize_study_tags(study)}

    patient = study.patient
    if patient is not None:
        for attr in ("patient_id", "patient_name", "patient_birth_date", "patient_sex"):
            if attr in updates:
                setattr(patient, attr, updates[attr] or None)
    for attr in ("accession_number", "study_date", "study_description", "referring_physician"):
        if attr in updates:
            setattr(study, attr, updates[attr] or None)

    instances: list[Instance] = [inst for ser in study.series_list for inst in ser.instances]
    updated_count = 0
    for inst in instances:
        if not storage_service.exists(inst.file_path, backend_name=inst.storage_backend):
            continue
        new_meta_json = await run_blocking(
            rewrite_dicom_object_tags, inst.file_path, inst.storage_backend, updates
        )
        inst.metadata_json = new_meta_json
        content_version.bump(inst)
        updated_count += 1

    session.add(AuditLog(
        study_instance_uid=study.study_instance_uid,
        action="TAGS_EDITED",
        performed_by=performed_by,
    ))
    await session.flush()
    log.info(
        "Study tags updated by %s: fields=%s instances=%d",
        performed_by, sorted(updates), updated_count,
    )
    return {
        "updated_fields": sorted(updates),
        "instances_updated": updated_count,
        "study": serialize_study_tags(study),
    }
