"""Study Split and Merge Service.

Fixes technologist header errors by allowing series or instances to be split
off into a new/different study, or merged into an existing target study.

Handles DICOM file header rewriting (pydicom), storage update, and atomic
SQLAlchemy database transactions while logging full audit trail entries.

File-layer safety (issue C-1, A-1 / A-14 / FD-1)
-----------------------------------------------
The stored object and the database row must never disagree about a study's
identity, and a crash must never destroy the only copy of an instance. Every
relocation therefore routes through ``storage_service.StagedObject``
(stage -> verify -> promote -> cleanup):

  1. read the source object, rewrite its ``StudyInstanceUID``/patient identity
     and **stage** the result under a temporary key — the canonical keys a
     reader can reach are untouched;
  2. **verify** every staged object by reading it back and comparing its
     SHA-256, then flush the DB rows;
  3. **promote** each staged object atomically at its new key and read it back
     to prove the archive now serves the rewritten identity;
  4. unlink the superseded source objects only once the caller's transaction has
     actually committed (``storage_service.defer_cleanup``).

If any step fails, the staged/published objects this call created are removed,
the source objects are left untouched, and the exception propagates so the
caller's transaction rolls back. The previous implementation rewrote the header
and then overwrote the same key with the *original* bytes, so the stored file
kept the old ``StudyInstanceUID``/``PatientID`` while the DB had already
re-linked the series.
"""

from __future__ import annotations

import asyncio
import io
import logging

from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import Instance, Patient, Series, Study, Tenant
from app.services import audit_service, content_version, indexing_service, storage_service

log = logging.getLogger("paxs.split_merge")


class StudyTenantScopeError(PermissionError):
    """A split/merge operation crossed or lacked a verified tenant boundary."""


async def resolve_operation_tenant(
    session: AsyncSession,
    study_uids: list[str],
    *,
    caller_tenant_id: int | None,
    is_admin: bool,
    requested_tenant_id: int | None = None,
) -> tuple[int, int | None]:
    """Resolve a verified single-tenant scope for an API split/merge request.

    A tenant user can only name studies owned by their assigned tenant. An
    administrator may operate on one explicitly selected tenant; if any study
    is unassigned, the selected tenant is also the explicit assignment request.
    Cross-tenant operations and unscoped-user operations fail closed.
    """
    unique_uids = list(dict.fromkeys(study_uids))
    if not unique_uids:
        raise StudyTenantScopeError("At least one study is required")

    if not is_admin:
        if caller_tenant_id is None:
            raise StudyTenantScopeError(
                "A verified tenant assignment is required for split/merge"
            )
        if requested_tenant_id is not None and requested_tenant_id != caller_tenant_id:
            raise StudyTenantScopeError("Requested tenant does not match caller tenant")
        rows = list(
            (await session.execute(
                select(Study.tenant_id).where(
                    Study.study_instance_uid.in_(unique_uids),
                    Study.tenant_id == caller_tenant_id,
                )
            )).scalars().all()
        )
        if len(rows) != len(unique_uids):
            raise StudyTenantScopeError("Study not found in caller tenant")
        return caller_tenant_id, None

    studies = list(
        (await session.execute(
            select(Study).where(Study.study_instance_uid.in_(unique_uids))
        )).scalars().all()
    )
    if len(studies) != len(unique_uids):
        raise StudyTenantScopeError("One or more studies were not found")

    tenant_ids = {study.tenant_id for study in studies}
    if requested_tenant_id is None:
        if None in tenant_ids:
            raise StudyTenantScopeError(
                "An administrator must explicitly provide tenant_id to assign unscoped studies"
            )
        if len(tenant_ids) != 1:
            raise StudyTenantScopeError("Split/merge studies must belong to one tenant")
        selected_tenant_id = next(iter(tenant_ids))
        assignment_id = None
    else:
        selected_tenant_id = requested_tenant_id
        if any(tid not in (None, selected_tenant_id) for tid in tenant_ids):
            raise StudyTenantScopeError("Split/merge studies must belong to one tenant")
        assignment_id = selected_tenant_id if None in tenant_ids else None

    tenant = await session.get(Tenant, selected_tenant_id)
    if tenant is None or not tenant.is_active:
        raise StudyTenantScopeError("Active tenant not found")
    return selected_tenant_id, assignment_id


async def _tenant_patient(
    session: AsyncSession,
    patient: Patient,
    *,
    tenant_id: int,
    patient_id: str | None = None,
    patient_name: str | None = None,
) -> Patient:
    """Return an existing patient in the target tenant or resolve a scoped copy."""
    if patient.tenant_id == tenant_id and patient_id is None:
        return patient
    return await indexing_service.get_or_create_patient(
        session,
        {
            "patient_id": patient_id or patient.patient_id,
            "patient_name": patient_name or patient.patient_name,
            "patient_birth_date": patient.patient_birth_date,
            "patient_sex": patient.patient_sex,
        },
        tenant_id=tenant_id,
    )


async def _validate_study_tenant(
    session: AsyncSession,
    studies: list[Study],
    *,
    tenant_id: int,
    assign_unassigned_tenant_id: int | None,
    performed_by: str,
    is_admin: bool,
) -> None:
    """Validate and, only on explicit admin instruction, assign NULL studies."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_active:
        raise StudyTenantScopeError("Active tenant not found")
    if assign_unassigned_tenant_id not in (None, tenant_id):
        raise StudyTenantScopeError("Unassigned studies can only be assigned to the operation tenant")
    if assign_unassigned_tenant_id is not None and not is_admin:
        raise StudyTenantScopeError("Only an administrator may assign an unscoped study")

    for study in studies:
        if study.tenant_id == tenant_id:
            continue
        if study.tenant_id is not None:
            raise StudyTenantScopeError("Split/merge studies must belong to one tenant")
        if assign_unassigned_tenant_id != tenant_id:
            raise StudyTenantScopeError(
                "Unassigned studies require explicit administrative tenant assignment"
            )
        study.tenant_id = tenant_id
        audit_service.add_audit(
            session,
            sop_instance_uid=None,
            action="TENANT_ASSIGNED",
            performed_by=performed_by,
            study_instance_uid=study.study_instance_uid,
        )


async def split_series_to_new_study(
    session: AsyncSession,
    source_study_uid: str,
    series_uids: list[str],
    target_patient_id: str | None = None,
    target_patient_name: str | None = None,
    new_study_description: str | None = None,
    performed_by: str = "technologist",
    *,
    tenant_id: int,
    assign_unassigned_tenant_id: int | None = None,
    is_admin: bool = False,
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
    await _validate_study_tenant(
        session,
        [source_study],
        tenant_id=tenant_id,
        assign_unassigned_tenant_id=assign_unassigned_tenant_id,
        performed_by=performed_by,
        is_admin=is_admin,
    )

    # 2. Resolve the new study's patient inside the same verified tenant. An
    #    explicitly assigned legacy study's Patient row may remain shared with
    #    other unscoped studies, so never claim or retag that row in place.
    target_patient = await _tenant_patient(
        session,
        source_study.patient,
        tenant_id=tenant_id,
        patient_id=target_patient_id,
        patient_name=target_patient_name,
    )

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
        tenant_id=tenant_id,
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

    # 7. Re-parent series and point instance rows at their new keys within the
    #    request transaction; the helper stages/verifies and publishes rewrites
    #    before the caller commits, while retaining source objects until commit.
    for series in series_to_move:
        series.study_id = new_study.id

    await _rewrite_and_relocate(
        session,
        targets,
        new_study_uid=new_study_uid,
        new_patient_id=target_patient.patient_id,
        new_patient_name=target_patient.patient_name,
        performed_by=performed_by,
        action="SPLIT",
        tenant_id=tenant_id,
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
    *,
    tenant_id: int,
    assign_unassigned_tenant_id: int | None = None,
    is_admin: bool = False,
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

    requested_sources = list(dict.fromkeys(source_study_uids))
    found_sources = {study.study_instance_uid for study in source_studies}
    if not source_studies or found_sources != set(requested_sources):
        raise ValueError("One or more source studies were not found")
    await _validate_study_tenant(
        session,
        [target_study, *source_studies],
        tenant_id=tenant_id,
        assign_unassigned_tenant_id=assign_unassigned_tenant_id,
        performed_by=performed_by,
        is_admin=is_admin,
    )

    target_patient = await _tenant_patient(
        session, target_study.patient, tenant_id=tenant_id
    )
    target_study.patient = target_patient
    target_study.patient_id = target_patient.id

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

        for series in series_list:
            series.study_id = target_study.id
            total_merged_series += 1

        await _rewrite_and_relocate(
            session,
            targets,
            new_study_uid=target_study_uid,
            new_patient_id=target_patient.patient_id,
            new_patient_name=target_patient.patient_name,
            performed_by=performed_by,
            action="MERGED",
            tenant_id=tenant_id,
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
    tenant_id: int,
) -> None:
    """Rewrite and move every instance object to the key implied by
    ``new_study_uid``, then re-point its DB row.

    Stage-then-publish (A-1/A-14/FD-1), all-or-nothing at the file layer:

    * **Stage** — every rewritten object is written to an isolated staging area,
      so the canonical keys a reader can reach are untouched while the work
      happens and nothing partial is ever observable;
    * **Assert** — each staged object is read back and its SHA-256 compared;
    * **Promote** — the staged object is published atomically at its new key and
      read back *again* to prove the archive now serves the rewritten identity;
    * **Cleanup** — the superseded source objects are unlinked only once the
      caller's transaction commits (``storage_service.defer_cleanup``), so a
      rollback can never leave rows pointing at deleted files.

    An in-place rewrite (``new_key == old_key``) is the one case where promotion
    would overwrite the source itself, so its promote is deferred past the commit
    too — a rollback then leaves the original object byte-for-byte intact.

    ``targets`` is a list of ``(instance, series_uid)`` pairs; the series UID is
    passed explicitly because ``instance.series`` may not be eagerly loaded and a
    lazy load would raise inside the async session.
    """
    staged: list[storage_service.StagedObject] = []
    published: list[tuple[str, str | None]] = []    # new objects to drop on failure
    superseded: list[tuple[str, str | None]] = []   # sources to unlink after commit
    deferred_promotes: list[storage_service.StagedObject] = []

    try:
        # Phase 1 + 2 — stage and verify every object before touching any
        # canonical key or the database.
        plans: list[
            tuple[Instance, str, str, str | None, storage_service.StagedObject]
        ] = []
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
            obj = await asyncio.to_thread(
                storage_service.stage_bytes,
                new_key,
                data,
                backend_name=backend,
                tenant_id=tenant_id,
                charge_bytes=0,
                require_isolated=True,
            )
            # Track the object before verification so any verification exception
            # can discard its staging file and quota reservation.
            staged.append(obj)
            await asyncio.to_thread(obj.verify)
            plans.append((inst, old_key, new_key, backend, obj))

        # Phase 3 — update the rows in the caller's transaction before exposing
        # any destination key. The source keys remain the rollback copy.
        for inst, old_key, new_key, backend, obj in plans:
            inst.file_path = new_key
            # C-04: the object was rewritten with new study/patient identity;
            # WADO-URI and Redis address it by SOP UID alone, so bump.
            content_version.bump(inst)
            audit_service.add_audit(
                session,
                sop_instance_uid=inst.sop_instance_uid,
                study_instance_uid=new_study_uid,
                action=action,
                performed_by=performed_by,
            )
        await session.flush()

        # Phase 4 — publish only after all staged hashes and DB updates have been
        # verified. Source objects are still retained until the transaction
        # commits; rollback removes every newly published destination.
        for inst, old_key, new_key, backend, obj in plans:
            if new_key == old_key:
                deferred_promotes.append(obj)
                continue
            await asyncio.to_thread(obj.promote)
            published.append((new_key, backend))
            await asyncio.to_thread(
                _verify_rewritten_instance,
                new_key,
                backend,
                new_study_uid,
                new_patient_id,
            )
            superseded.append((old_key, backend))
    except Exception:
        # Undo this call's file-layer work. No source object was unlinked, and
        # the DB transaction is rolled back by the caller.
        for obj in staged:
            obj.discard()
        for key, backend in published:
            try:
                removed = await asyncio.to_thread(
                    storage_service.delete_file,
                    key,
                    backend,
                    tenant_id=tenant_id,
                    charge_release_bytes=0,
                )
                if not removed:
                    log.warning(
                        "split_merge_published_object_cleanup_failed action=%s result=false",
                        action,
                    )
            except Exception as exc:  # cleanup is best effort; source remains intact
                log.warning(
                    "split_merge_published_object_cleanup_failed action=%s error_type=%s",
                    action,
                    type(exc).__name__,
                )
        raise

    # Phase 3 (deferred) + Phase 4 — these run only if the transaction commits.
    if deferred_promotes:
        def _promote_in_place() -> None:
            for obj in deferred_promotes:
                obj.promote()

        storage_service.defer_after_commit(session, _promote_in_place)
    storage_service.defer_cleanup(session, superseded)

    # Symmetry: if the caller's transaction rolls back instead, the objects this
    # call published at brand-new keys are unreferenced — drop them so a failed
    # split/merge leaves the file layer exactly as it was.
    if published:
        def _drop_unpublished() -> None:
            for key, backend in published:
                try:
                    removed = storage_service.delete_file(
                        key,
                        backend,
                        tenant_id=tenant_id,
                        charge_release_bytes=0,
                    )
                    if not removed:
                        log.warning(
                            "split_merge_rollback_cleanup_failed action=%s result=false",
                            action,
                        )
                except Exception as exc:  # rollback cleanup must not mask DB failure
                    log.warning(
                        "split_merge_rollback_cleanup_failed action=%s error_type=%s",
                        action,
                        type(exc).__name__,
                    )

        storage_service.defer_after_rollback(session, _drop_unpublished)
