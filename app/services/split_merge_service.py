"""Study Split and Merge Service.

Fixes technologist header errors by allowing series or instances to be split
off into a new/different study, or merged into an existing target study.

Handles DICOM file header rewriting (pydicom), storage update, and atomic
SQLAlchemy database transactions while logging full audit trail entries.
"""

from __future__ import annotations

import logging

import pydicom
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import Instance, Patient, Series, Study
from app.services import audit_service, indexing_service, storage_service

log = logging.getLogger("paxs.split_merge")


async def split_series_to_new_study(
    session: AsyncSession,
    source_study_uid: str,
    series_uids: list[str],
    target_patient_id: str | None = None,
    target_patient_name: str | None = None,
    new_study_description: str | None = None,
    performed_by: str = "technologist",
) -> Study:
    """Split specific series out of a source study into a brand-new study.

    Rewrites StudyInstanceUID and patient demographics in the underlying DICOM
    files on storage and updates DB relationships.
    """
    # 1. Fetch source study with patient relationship joined
    source_study = (
        await session.execute(
            select(Study)
            .options(joinedload(Study.patient))
            .where(Study.study_instance_uid == source_study_uid)
        )
    ).scalar_one_or_none()
    if not source_study:
        raise ValueError(f"Source study '{source_study_uid}' not found")

    # 2. Get or create target patient
    if target_patient_id:
        patient_meta = {
            "patient_id": target_patient_id,
            "patient_name": target_patient_name or source_study.patient.patient_name,
            "patient_birth_date": source_study.patient.patient_birth_date,
            "patient_sex": source_study.patient.patient_sex,
        }
        target_patient = await indexing_service.get_or_create_patient(session, patient_meta)
    else:
        target_patient = source_study.patient

    # 3. Generate new StudyInstanceUID
    new_study_uid = generate_uid()

    # 4. Create new Study row
    new_study = Study(
        patient_id=target_patient.id,
        study_instance_uid=new_study_uid,
        study_date=source_study.study_date,
        study_time=source_study.study_time,
        accession_number=f"SPLIT-{new_study_uid[:8]}",
        study_id=source_study.study_id,
        study_description=new_study_description or f"Split from {source_study_uid[:12]}...",
        referring_physician=source_study.referring_physician,
    )
    session.add(new_study)
    await session.flush()

    # 5. Fetch series to move
    series_stmt = select(Series).where(
        Series.study_id == source_study.id,
        Series.series_instance_uid.in_(series_uids),
    )
    series_to_move = (await session.execute(series_stmt)).scalars().all()

    if not series_to_move:
        raise ValueError("No matching series found in source study to split")

    for s in series_to_move:
        # Re-parent series to new study
        s.study_id = new_study.id

        # Update all instances of this series
        inst_stmt = select(Instance).where(Instance.series_id == s.id)
        instances = (await session.execute(inst_stmt)).scalars().all()

        for inst in instances:
            _rewrite_instance_study_uid(
                inst.file_path,
                new_study_uid=new_study_uid,
                new_patient_id=target_patient.patient_id,
                new_patient_name=target_patient.patient_name,
                backend=inst.storage_backend,
            )
            new_key = storage_service.instance_key(
                new_study_uid, s.series_instance_uid, inst.sop_instance_uid
            )
            if inst.file_path != new_key:
                old_bytes = storage_service.read_bytes(
                    inst.file_path, backend_name=inst.storage_backend
                )
                storage_service.save_bytes(
                    new_key, old_bytes, backend_name=inst.storage_backend
                )
                storage_service.delete_file(inst.file_path, backend_name=inst.storage_backend)
                inst.file_path = new_key

            audit_service.add_audit(
                session,
                sop_instance_uid=inst.sop_instance_uid,
                study_instance_uid=new_study_uid,
                action="SPLIT",
                performed_by=performed_by,
            )

    await session.flush()

    # Clean up empty source study if all series were split
    remaining_series = (
        await session.execute(
            select(Series).where(Series.study_id == source_study.id)
        )
    ).scalars().all()

    if not remaining_series:
        await session.delete(source_study)

    await session.flush()
    log.info(
        "Split %d series from study %s into new study %s",
        len(series_to_move),
        source_study_uid,
        new_study_uid,
    )
    new_study.patient = target_patient
    return new_study


async def merge_studies(
    session: AsyncSession,
    source_study_uids: list[str],
    target_study_uid: str,
    performed_by: str = "technologist",
) -> Study:
    """Merge series from source studies into a target study.

    Re-parents series to target study and rewrites DICOM file headers.
    """
    target_study = (
        await session.execute(
            select(Study)
            .options(joinedload(Study.patient))
            .where(Study.study_instance_uid == target_study_uid)
        )
    ).scalar_one_or_none()
    if not target_study:
        raise ValueError(f"Target study '{target_study_uid}' not found")

    target_patient = target_study.patient

    source_studies = (
        await session.execute(
            select(Study).where(Study.study_instance_uid.in_(source_study_uids))
        )
    ).scalars().all()

    if not source_studies:
        raise ValueError("No matching source studies found for merge")

    total_merged_series = 0

    for src_study in source_studies:
        if src_study.id == target_study.id:
            continue

        series_stmt = select(Series).where(Series.study_id == src_study.id)
        series_list = (await session.execute(series_stmt)).scalars().all()

        for s in series_list:
            s.study_id = target_study.id
            total_merged_series += 1

            inst_stmt = select(Instance).where(Instance.series_id == s.id)
            instances = (await session.execute(inst_stmt)).scalars().all()

            for inst in instances:
                _rewrite_instance_study_uid(
                    inst.file_path,
                    new_study_uid=target_study_uid,
                    new_patient_id=target_patient.patient_id,
                    new_patient_name=target_patient.patient_name,
                    backend=inst.storage_backend,
                )
                new_key = storage_service.instance_key(
                    target_study_uid, s.series_instance_uid, inst.sop_instance_uid
                )
                if inst.file_path != new_key:
                    old_bytes = storage_service.read_bytes(
                        inst.file_path, backend_name=inst.storage_backend
                    )
                    storage_service.save_bytes(
                        new_key, old_bytes, backend_name=inst.storage_backend
                    )
                    storage_service.delete_file(inst.file_path, backend_name=inst.storage_backend)
                    inst.file_path = new_key

                audit_service.add_audit(
                    session,
                    sop_instance_uid=inst.sop_instance_uid,
                    study_instance_uid=target_study_uid,
                    action="MERGED",
                    performed_by=performed_by,
                )

        # Delete empty source study
        await session.delete(src_study)

    await session.flush()
    log.info(
        "Merged %d series from %d studies into target study %s",
        total_merged_series,
        len(source_studies),
        target_study_uid,
    )
    return target_study


def _rewrite_instance_study_uid(
    file_key: str,
    new_study_uid: str,
    new_patient_id: str,
    new_patient_name: str | None = None,
    backend: str | None = None,
) -> None:
    """Modify DICOM tags in file dataset stored in backend."""
    try:
        ds = storage_service.read_dataset(file_key, backend_name=backend)
        ds.StudyInstanceUID = new_study_uid
        ds.PatientID = new_patient_id
        if new_patient_name:
            ds.PatientName = new_patient_name

        # Save dataset back to storage facade
        storage_service.save_dataset(ds, backend_name=backend)
    except Exception as exc:
        log.warning("Failed to rewrite DICOM tags for %s: %s", file_key, exc)
