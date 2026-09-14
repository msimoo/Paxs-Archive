"""Upserts incoming DICOM metadata into the Patient -> Study -> Series ->
Instance hierarchy and fans out side effects (audit log, compression job).

Idempotent: re-importing an existing instance updates file location/size and
does not duplicate rows (SOP/Study/Series UIDs are unique).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import AuditLog, CompressionJob, Instance, Patient, Series, Study
from app.services import storage_service, worklist_service

# GSPS SOP Class UID — Grayscale Softcopy Presentation State
_GSPS_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.11.1"


def _int(value) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


async def get_or_create_patient(session: AsyncSession, meta: dict) -> Patient:
    patient_id = meta.get("patient_id") or "ANONYMOUS"
    patient = (
        await session.execute(select(Patient).where(Patient.patient_id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        try:
            # Savepoint: if the HL7 listener (or another receiver) wins the
            # unique-patient_id race, the failed INSERT rolls back without
            # disturbing the rest of this transaction (important for STOW,
            # which shares one session across all parts of a request).
            async with session.begin_nested():
                patient = Patient(
                    patient_id=patient_id,
                    patient_name=meta.get("patient_name"),
                    patient_birth_date=meta.get("patient_birth_date"),
                    patient_sex=meta.get("patient_sex"),
                )
                session.add(patient)
                await session.flush()
        except IntegrityError:
            patient = (
                await session.execute(select(Patient).where(Patient.patient_id == patient_id))
            ).scalar_one_or_none()
            if patient is None:
                # Snapshot-isolation edge (MySQL REPEATABLE READ): the winner's
                # commit is invisible to this transaction; surface the error and
                # let the caller (STOW per-part / C-STORE) handle it as a normal
                # DB failure rather than guessing.
                raise
    else:
        if patient.source == "his":
            # Registered by the HL7 ADT listener: HIS is authoritative, so never
            # overwrite its demographics — but backfill gaps the ADT didn't
            # carry (some messages omit DOB/sex) from the modality's data.
            changed = False
            if patient.patient_name is None and meta.get("patient_name"):
                patient.patient_name = meta["patient_name"]
                changed = True
            if patient.patient_birth_date is None and meta.get("patient_birth_date"):
                patient.patient_birth_date = meta["patient_birth_date"]
                changed = True
            if patient.patient_sex is None and meta.get("patient_sex"):
                patient.patient_sex = meta["patient_sex"]
                changed = True
            if changed:
                await session.flush()
            return patient
        changed = False
        if meta.get("patient_name") and patient.patient_name != meta["patient_name"]:
            patient.patient_name = meta["patient_name"]
            changed = True
        if meta.get("patient_birth_date") and patient.patient_birth_date != meta["patient_birth_date"]:
            patient.patient_birth_date = meta["patient_birth_date"]
            changed = True
        if meta.get("patient_sex") and patient.patient_sex != meta["patient_sex"]:
            patient.patient_sex = meta["patient_sex"]
            changed = True
        if changed:
            await session.flush()
    return patient


async def get_or_create_study(session: AsyncSession, meta: dict, patient: Patient,
                              tenant_id: int | None = None) -> Study:
    """Upsert a study. `tenant_id` (VNA) is applied on first creation only —
    re-imported slices join the tenant of the study's first writer."""
    study_uid = meta.get("study_instance_uid") or "UNKNOWN"
    study = (
        await session.execute(select(Study).where(Study.study_instance_uid == study_uid))
    ).scalar_one_or_none()
    if study is None:
        study = Study(
            patient_id=patient.id,
            study_instance_uid=study_uid,
            study_date=meta.get("study_date"),
            study_time=meta.get("study_time"),
            accession_number=meta.get("accession_number"),
            study_id=meta.get("study_id"),
            study_description=meta.get("study_description"),
            referring_physician=meta.get("referring_physician"),
            tenant_id=tenant_id,
        )
        session.add(study)
        await session.flush()
        # First instance of a brand-new study -> close the Modality Worklist
        # loop: if an open scheduled procedure step matches (by accession, or
        # by patient + modality), mark it COMPLETED and link this study.
        step = await worklist_service.find_matching_step(session, patient.id, meta)
        if step is not None:
            worklist_service.complete_step(step, study_uid)
            await session.flush()
        # First instance of a brand-new study -> notify external systems
        # (webhooks + custom server scripts).
        from app.services.webhook_service import fire as webhook_fire
        from app.services.scripting_service import fire as script_fire

        webhook_fire(
            "StudyReceived",
            study_uid=study_uid,
            patient_id=patient.patient_id,
            patient_name=patient.patient_name,
            accession_number=meta.get("accession_number"),
            study_description=meta.get("study_description"),
        )
        script_fire(
            "StudyReceived",
            study_uid=study_uid,
            patient_id=patient.patient_id,
            patient_name=patient.patient_name,
            accession_number=meta.get("accession_number"),
            study_description=meta.get("study_description"),
        )
        # First instance of a brand-new study -> evaluate auto-routing rules.
        try:
            from app.services import routing_service

            await routing_service.evaluate_and_route_study(study_uid, meta)
        except Exception:
            import logging

            logging.getLogger("paxs.indexing").warning(
                "Auto-routing evaluation failed for %s", study_uid, exc_info=True
            )
    else:
        if meta.get("study_description") and study.study_description != meta["study_description"]:
            study.study_description = meta["study_description"]
            await session.flush()
    return study


async def get_or_create_series(session: AsyncSession, meta: dict, study: Study) -> Series:
    series_uid = meta.get("series_instance_uid") or "UNKNOWN"
    series = (
        await session.execute(select(Series).where(Series.series_instance_uid == series_uid))
    ).scalar_one_or_none()
    if series is None:
        series = Series(
            study_id=study.id,
            series_instance_uid=series_uid,
            modality=meta.get("modality"),
            series_number=_int(meta.get("series_number")),
            series_description=meta.get("series_description"),
            body_part_examined=meta.get("body_part_examined"),
            laterality=meta.get("laterality"),
            frame_of_reference_uid=meta.get("frame_of_reference_uid"),
        )
        session.add(series)
        await session.flush()
    else:
        # Late-arriving slice: refresh the spatial contract (a modality may send
        # the Frame of Reference on later instances) and re-arm triage.
        if meta.get("frame_of_reference_uid"):
            series.frame_of_reference_uid = meta["frame_of_reference_uid"]
        if series.triage_status != "PENDING":
            series.triage_status = "PENDING"
    return series


async def upsert_instance(session: AsyncSession, series: Series, meta: dict,
                          file_path: str, file_size: int,
                          storage_backend: str | None = None) -> Instance:
    sop_uid = meta.get("sop_instance_uid") or "UNKNOWN"
    instance = (
        await session.execute(select(Instance).where(Instance.sop_instance_uid == sop_uid))
    ).scalar_one_or_none()
    if instance is None:
        instance = Instance(
            series_id=series.id,
            sop_instance_uid=sop_uid,
            sop_class_uid=meta.get("sop_class_uid"),
            instance_number=_int(meta.get("instance_number")),
            file_path=file_path,
            storage_backend=storage_backend,
            file_size_bytes=file_size,
            transfer_syntax_uid=meta.get("transfer_syntax_uid"),
            rows=_int(meta.get("rows")),
            columns=_int(meta.get("columns")),
            bits_allocated=_int(meta.get("bits_allocated")),
            photometric_interpretation=meta.get("photometric_interpretation"),
            slice_thickness=meta.get("slice_thickness"),
        )
        session.add(instance)
        await session.flush()
    else:
        instance.file_path = file_path
        instance.file_size_bytes = file_size
        instance.storage_backend = storage_backend
        if meta.get("transfer_syntax_uid"):
            instance.transfer_syntax_uid = meta["transfer_syntax_uid"]
        await session.flush()
    return instance


async def index_new_instance(session: AsyncSession, meta: dict,
                             file_path: str, sha256: str | None = None,
                             performed_by: str = "dicom-receiver",
                             tenant_id: int | None = None,
                             storage_backend: str | None = None) -> Instance:
    """Full pipeline for one incoming instance (called by C-STORE and STOW-RS).

    `tenant_id` (VNA) is the owning tenant; callers resolve it from the source
    AE title (C-STORE) or the authenticated user (STOW-RS).
    `storage_backend` (multi-storage) is the named backend the object was
    written to; None = the environment default backend."""
    patient = await get_or_create_patient(session, meta)
    study = await get_or_create_study(session, meta, patient, tenant_id=tenant_id)
    # Late-arriving slices must stay on the study's backend; record it so the
    # study row and its instances agree on where the data lives.
    if storage_backend:
        study.storage_backend = storage_backend
    series = await get_or_create_series(session, meta, study)

    try:
        size = storage_service.size(file_path, backend_name=storage_backend)
    except Exception:
        size = 0
    instance = await upsert_instance(
        session, series, meta, file_path, size, storage_backend=storage_backend
    )

    # Audit trail: STORED with SHA-256 fingerprint (tamper detection).
    session.add(
        AuditLog(
            sop_instance_uid=instance.sop_instance_uid,
            study_instance_uid=study.study_instance_uid,
            sha256_hash=sha256,
            action="STORED",
            performed_by=performed_by,
        )
    )

    # DR replication: if this study was already replicated off-site, a new
    # object arriving now (late series, post-processing, re-routed slices)
    # must re-trigger the sweep — otherwise the replica would silently drift.
    # One indexed lookup only when the feature is enabled.
    if settings.DR_REPLICATION_ENABLED:
        from app.db.models import ReplicationRecord

        rec = (
            await session.execute(
                select(ReplicationRecord).where(
                    ReplicationRecord.study_instance_uid == study.study_instance_uid,
                    ReplicationRecord.status == "REPLICATED",
                )
            )
        ).scalar_one_or_none()
        if rec is not None:
            rec.status = "PENDING"

    # Queue lossless compression for the background worker (skip PR — no pixels).
    if settings.COMPRESSION_ENABLED and meta.get("sop_class_uid") != _GSPS_SOP_CLASS_UID:
        session.add(
            CompressionJob(
                instance_id=instance.id,
                method=settings.DEFAULT_COMPRESSION,
                status="PENDING",
            )
        )

    # GSPS: index presentation state metadata for quick lookup.
    if meta.get("sop_class_uid") == _GSPS_SOP_CLASS_UID:
        try:
            from app.services.gsps_service import index_presentation_state

            ds = storage_service.read_dataset(
                file_path, stop_before_pixels=True, backend_name=storage_backend
            )
            await index_presentation_state(session, meta, ds, file_path)
        except Exception:
            import logging
            logging.getLogger("paxs.indexing").warning(
                "GSPS indexing failed for %s", meta.get("sop_instance_uid"), exc_info=True
            )

    # KIN (Key Object Selection): index key image note metadata.
    if meta.get("sop_class_uid") == "1.2.840.10008.5.1.4.1.1.88.22" or meta.get("modality") == "KO":
        try:
            from app.services.kin_service import index_key_image_note

            ds = storage_service.read_dataset(
                file_path, stop_before_pixels=True, backend_name=storage_backend
            )
            await index_key_image_note(session, meta, ds, file_path)
        except Exception:
            import logging
            logging.getLogger("paxs.indexing").warning(
                "KIN indexing failed for %s", meta.get("sop_instance_uid"), exc_info=True
            )

    # RDSR (Radiation Dose Structured Report): index radiation dose exposure metrics.
    if meta.get("sop_class_uid") == "1.2.840.10008.5.1.4.1.1.88.67":
        try:
            from app.services.dose_service import index_dose_report

            ds = storage_service.read_dataset(
                file_path, stop_before_pixels=True, backend_name=storage_backend
            )
            await index_dose_report(session, meta, ds, file_path)
        except Exception:
            import logging
            logging.getLogger("paxs.indexing").warning(
                "RDSR indexing failed for %s", meta.get("sop_instance_uid"), exc_info=True
            )

    await session.flush()
    return instance


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
