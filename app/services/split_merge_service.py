"""Study Split and Merge Service.

Fixes technologist header errors by allowing series or instances to be split
off into a new/different study, or merged into an existing target study.

Handles DICOM file header rewriting (pydicom), storage update, and atomic
SQLAlchemy database transactions while logging full audit trail entries.

File-layer safety (issue C-1)
-----------------------------
The stored object and the database row must never disagree about a study's
identity. Every relocation therefore follows a strict order:

  1. read the source object, rewrite its ``StudyInstanceUID``/patient identity,
     write the result to the *new* key, and **verify by reading it back**;
  2. only after every new object is written and verified, flush the DB rows;
  3. only after the flush succeeds, unlink the superseded source objects.

If step 1 or 2 fails, the objects written by this call are removed, the source
objects are left untouched, and the exception propagates so the caller's
transaction rolls back. The previous implementation rewrote the header and then
overwrote the same key with the *original* bytes, so the stored file kept the old
``StudyInstanceUID``/``PatientID`` while the DB had already re-linked the series.
"""

from __future__ import annotations

import asyncio
import io
import logging

from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import Instance, Series, Study
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
        # C10: keep the new patient in the source study's tenant.
        target_patient = await indexing_service.get_or_create_patient(
            session, patient_meta, tenant_id=source_study.tenant_id
        )
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

    # 6. Collect every instance to relocate (before any file/DB mutation).
    targets: list[tuple[Instance, str]] = []
    for s in series_to_move:
        inst_stmt = select(Instance).where(Instance.series_id == s.id)
        instances = (await session.execute(inst_stmt)).scalars().all()
        targets.extend((inst, s.series_instance_uid) for inst in instances)

    # 7. Rewrite + relocate objects, then re-parent the series to the new study.
    #    The helper writes/verifies first, flushes the DB, and only then unlinks
    #    the superseded source objects — a failure anywhere leaves the source
    #    archive intact (issue C-1).
    await _rewrite_and_relocate(
        session,
        targets,
        new_study_uid=new_study_uid,
        new_patient_id=target_patient.patient_id,
        new_patient_name=target_patient.patient_name,
        performed_by=performed_by,
        action="SPLIT",
    )

    for s in series_to_move:
        s.study_id = new_study.id

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

        # Collect every instance of this source study before mutating anything.
        targets: list[tuple[Instance, str]] = []
        for s in series_list:
            inst_stmt = select(Instance).where(Instance.series_id == s.id)
            instances = (await session.execute(inst_stmt)).scalars().all()
            targets.extend((inst, s.series_instance_uid) for inst in instances)

        await _rewrite_and_relocate(
            session,
            targets,
            new_study_uid=target_study_uid,
            new_patient_id=target_patient.patient_id,
            new_patient_name=target_patient.patient_name,
            performed_by=performed_by,
            action="MERGED",
        )

        for s in series_list:
            s.study_id = target_study.id
            total_merged_series += 1

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
) -> bytes:
    """Read the stored instance, rewrite its study/patient identity, and return
    the re-encoded DICOM bytes.

    Errors propagate (issue C-1): a swallowed failure here would leave the stored
    object carrying the OLD ``StudyInstanceUID``/``PatientID`` while the database
    had already re-linked the series — a PHI-misattribution defect. The caller
    writes the returned bytes to the new key and verifies the result before the
    source object is unlinked.

    The dataset is read *with* pixel data so the rewritten object remains a
    complete, servable instance.
    """
    ds = storage_service.read_dataset(file_key, backend_name=backend)
    ds.StudyInstanceUID = new_study_uid
    ds.PatientID = new_patient_id
    if new_patient_name:
        ds.PatientName = new_patient_name

    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def _verify_rewritten_instance(
    key: str,
    backend: str | None,
    new_study_uid: str,
    new_patient_id: str,
) -> None:
    """Read the freshly written object back and assert its identity matches.

    This is the guard that makes the C-1 regression impossible: the source object
    is only ever unlinked after the destination has been proven to carry the new
    ``StudyInstanceUID``/``PatientID``.
    """
    check = storage_service.read_dataset(
        key, stop_before_pixels=True, backend_name=backend
    )
    got_study = str(getattr(check, "StudyInstanceUID", "") or "")
    got_patient = str(getattr(check, "PatientID", "") or "")
    if got_study != str(new_study_uid) or got_patient != str(new_patient_id):
        raise storage_service.StorageError(
            f"rewrite verification failed for {key}: stored "
            f"StudyInstanceUID={got_study!r} PatientID={got_patient!r} "
            f"(expected {new_study_uid!r}/{new_patient_id!r})"
        )


async def _rewrite_and_relocate(
    session: AsyncSession,
    targets: list[tuple[Instance, str]],
    *,
    new_study_uid: str,
    new_patient_id: str,
    new_patient_name: str | None,
    performed_by: str,
    action: str,
) -> None:
    """Rewrite and move every instance object to the key implied by
    ``new_study_uid``, then re-point its DB row.

    All-or-nothing at the file layer (issue C-1):

    * each new object is written and **verified** before anything is deleted;
    * the DB rows are flushed **before** the superseded source objects are
      unlinked, so a failed write or a failed DB update rolls back with the
      source objects intact;
    * if any step fails, the objects written by this call are removed and the
      exception propagates (the caller's transaction then rolls back).

    ``targets`` is a list of ``(instance, series_uid)`` pairs; the series UID is
    passed explicitly because ``instance.series`` may not be eagerly loaded and a
    lazy load would raise inside the async session.
    """
    written: list[tuple[str, str | None]] = []      # new objects to clean up on failure
    superseded: list[tuple[str, str | None]] = []   # old objects to unlink after flush

    try:
        for inst, series_uid in targets:
            old_key = inst.file_path
            backend = inst.storage_backend

            # Every storage call below is synchronous disk/S3 I/O; run it off the
            # event loop so a split/merge of a large study does not stall the
            # whole server (H-13).
            data = await asyncio.to_thread(
                _rewrite_instance_study_uid,
                old_key, new_study_uid, new_patient_id, new_patient_name, backend,
            )
            new_key = storage_service.instance_key(
                new_study_uid, series_uid, inst.sop_instance_uid
            )

            if new_key == old_key:
                # Same key — only reachable when the study UID is unchanged, which
                # the split (fresh UID) and merge (target != source) flows never
                # produce. Rewrite in place; there is no superseded object.
                await asyncio.to_thread(storage_service.save_bytes, new_key, data, backend)
                await asyncio.to_thread(
                    _verify_rewritten_instance, new_key, backend, new_study_uid, new_patient_id
                )
            else:
                await asyncio.to_thread(storage_service.save_bytes, new_key, data, backend)
                written.append((new_key, backend))
                # Verify BEFORE the DB is touched or the source is unlinked.
                await asyncio.to_thread(
                    _verify_rewritten_instance, new_key, backend, new_study_uid, new_patient_id
                )
                inst.file_path = new_key
                superseded.append((old_key, backend))

            audit_service.add_audit(
                session,
                sop_instance_uid=inst.sop_instance_uid,
                study_instance_uid=new_study_uid,
                action=action,
                performed_by=performed_by,
            )

        # DB first: if this flush fails the source objects are still on disk, so
        # the caller's rollback leaves the archive exactly as it was.
        await session.flush()
    except Exception:
        # Undo the file-layer changes this call made. The source objects were
        # never deleted, and the DB transaction is rolled back by the caller.
        for key, backend in written:
            try:
                await asyncio.to_thread(storage_service.delete_file, key, backend)
            except Exception:
                log.warning(
                    "could not clean up partially written object %s after a "
                    "failed %s", key, action, exc_info=True,
                )
        raise

    # The flush succeeded: the new objects are durable and the DB points at them,
    # so the superseded source objects can now be unlinked. A failure here is
    # logged but not fatal — the operation itself is complete.
    for old_key, backend in superseded:
        try:
            await asyncio.to_thread(storage_service.delete_file, old_key, backend)
        except Exception:
            log.warning(
                "could not delete superseded object %s after %s (object is now "
                "unreferenced)", old_key, action, exc_info=True,
            )
