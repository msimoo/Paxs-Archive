"""Upserts incoming DICOM metadata into the Patient -> Study -> Series ->
Instance hierarchy and fans out side effects (audit log, compression job).

Idempotent: re-importing an existing instance updates file location/size and
does not duplicate rows (SOP/Study/Series UIDs are unique).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import (
    AssigningAuthority,
    AuditLog,
    CompressionJob,
    Instance,
    Patient,
    PatientIdentity,
    PatientIdentityConflict,
    Series,
    Study,
)
from app.services import storage_service, worklist_service

log = logging.getLogger("paxs.indexing")

# GSPS SOP Class UID — Grayscale Softcopy Presentation State
_GSPS_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.11.1"


class CrossTenantIngestError(ValueError):
    """An instance tried to join a study owned by a different tenant (C04).

    Raised instead of silently appending: ingest turns it into a 400 (STOW-RS)
    or a Processing Failure (C-STORE), matching the C05 hard-reject behaviour.
    """


def _int(value) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# C10 — tenant-scoped patient identity
#
# Patient identity used to be global: `patients.patient_id` was unique across the
# whole installation, so two institutions using the same MRN shared one row and
# each ingest overwrote the other's demographics. Identity is now resolved by
# (tenant, assigning authority, patient id):
#
#   * the authority is IssuerOfPatientID (0010,0021) when the modality sends it,
#     otherwise a per-tenant default domain;
#   * a blank/absent PatientID becomes a per-study synthetic identity, so
#     unidentified patients never merge across examinations;
#   * a demographic mismatch is *quarantined* (recorded for reconciliation) and
#     never overwrites the validated row.
# --------------------------------------------------------------------------- #
DEFAULT_AUTHORITY_PREFIX = "TENANT"
LEGACY_AUTHORITY = "LEGACY_DEFAULT"
UNIDENTIFIED_PREFIX = "UNIDENTIFIED-"

_DEMOGRAPHIC_FIELDS = ("patient_name", "patient_birth_date", "patient_sex")


@dataclass
class PatientResolution:
    """Outcome of resolving an ingest's patient identity."""

    patient: Patient
    outcome: str                      # created | matched | conflict | unidentified
    identifier: str
    authority: str
    conflicts: dict[str, dict] = field(default_factory=dict)


def default_authority_oid(tenant_id: int | None) -> str:
    """Per-tenant default identity domain when the modality sends no issuer."""
    if tenant_id is None:
        return LEGACY_AUTHORITY
    return f"{DEFAULT_AUTHORITY_PREFIX}_{tenant_id}_DEFAULT"


async def _ensure_authority(session: AsyncSession, oid: str) -> AssigningAuthority:
    authority = (
        await session.execute(
            select(AssigningAuthority).where(AssigningAuthority.oid == oid)
        )
    ).scalar_one_or_none()
    if authority is None:
        authority = AssigningAuthority(oid=oid, name=oid, is_authoritative=True)
        session.add(authority)
        await session.flush()
    return authority


def _tenant_filter(column, tenant_id: int | None):
    """Match a tenant, treating NULL as the legacy/unscoped bucket."""
    return column.is_(None) if tenant_id is None else column == tenant_id


async def resolve_patient_identity(
    session: AsyncSession,
    meta: dict,
    tenant_id: int | None = None,
    source: str | None = None,
) -> PatientResolution:
    """Resolve (tenant, assigning authority, patient id) to a Patient row.

    Never merges institutions and never overwrites validated demographics.
    """
    raw_id = str(meta.get("patient_id") or "").strip()
    study_uid = str(meta.get("study_instance_uid") or "").strip() or None

    if raw_id:
        identifier = raw_id
        unidentified = False
    else:
        # C10: a missing PatientID must not collapse every anonymous patient
        # into one shared record — key it to the study instead.
        identifier = f"{UNIDENTIFIED_PREFIX}{study_uid or uuid.uuid4().hex}"
        unidentified = True

    authority_oid = (
        str(meta.get("issuer_of_patient_id") or "").strip()
        or default_authority_oid(tenant_id)
    )
    authority = await _ensure_authority(session, authority_oid)

    # 1) Known identity for this (tenant, authority, identifier)?
    identity = (
        await session.execute(
            select(PatientIdentity)
            .join(Patient, PatientIdentity.patient_id == Patient.id)
            .where(
                _tenant_filter(PatientIdentity.tenant_id, tenant_id),
                PatientIdentity.assigning_authority_id == authority.id,
                PatientIdentity.identifier == identifier,
            )
        )
    ).scalar_one_or_none()
    if identity is not None:
        patient = (
            await session.execute(select(Patient).where(Patient.id == identity.patient_id))
        ).scalar_one()
    else:
        # 2) Legacy patient row for this tenant that has *no* identity yet?
        #
        # The `~Patient.identities.any()` guard matters: a patient already
        # carrying an identity under a different assigning authority is a
        # different domain, and the same MRN string there may be a different
        # person. Matching it implicitly would re-create exactly the conflation
        # this fix removes; cross-domain links belong to an explicit PIX merge.
        patient = (
            await session.execute(
                select(Patient).where(
                    _tenant_filter(Patient.tenant_id, tenant_id),
                    Patient.patient_id == identifier,
                    ~Patient.identities.any(),
                )
            )
        ).scalar_one_or_none()
        created = False
        if patient is None:
            # 3) Genuinely new patient for this tenant.
            try:
                async with session.begin_nested():
                    patient = Patient(
                        tenant_id=tenant_id,
                        patient_id=identifier,
                        patient_name=meta.get("patient_name"),
                        patient_birth_date=meta.get("patient_birth_date"),
                        patient_sex=meta.get("patient_sex"),
                    )
                    session.add(patient)
                    await session.flush()
                created = True
            except IntegrityError:
                # `(tenant, patient_id)` is unique: another writer (or an earlier
                # identity under a different assigning authority) already owns
                # this MRN in this tenant, so adopt that row. Within one tenant an
                # MRN identifies one local patient; extra identities attach to it.
                patient = (
                    await session.execute(
                        select(Patient).where(
                            _tenant_filter(Patient.tenant_id, tenant_id),
                            Patient.patient_id == identifier,
                        )
                    )
                ).scalar_one_or_none()
                if patient is None:
                    raise
            session.add(
                PatientIdentity(
                    tenant_id=tenant_id,
                    patient_id=patient.id,
                    assigning_authority_id=authority.id,
                    identifier=identifier,
                )
            )
            await session.flush()
            return PatientResolution(
                patient=patient,
                outcome=(
                    "unidentified" if unidentified
                    else "created" if created
                    else "matched"
                ),
                identifier=identifier,
                authority=authority_oid,
            )

        # Backfill the identity for a legacy row so future lookups are direct.
        session.add(
            PatientIdentity(
                tenant_id=tenant_id,
                patient_id=patient.id,
                assigning_authority_id=authority.id,
                identifier=identifier,
            )
        )
        await session.flush()

    # Existing patient: quarantine conflicts, never overwrite.
    conflicts: dict[str, dict] = {}
    if patient.source != "his":  # HIS demographics remain authoritative
        for field_name in _DEMOGRAPHIC_FIELDS:
            incoming = meta.get(field_name)
            current = getattr(patient, field_name)
            if incoming and current and str(incoming).strip() != str(current).strip():
                conflicts[field_name] = {"existing": current, "incoming": incoming}

    if conflicts:
        session.add(
            PatientIdentityConflict(
                tenant_id=tenant_id,
                patient_id=patient.id,
                study_instance_uid=study_uid,
                identifier=identifier,
                assigning_authority=authority_oid,
                detail=json.dumps(conflicts),
                source=source,
            )
        )
        await session.flush()
        log.warning(
            "patient demographic conflict quarantined: tenant=%s mrn=%s fields=%s",
            tenant_id, identifier, sorted(conflicts),
        )

    # Backfill genuinely empty fields (never overwrite a populated one).
    if patient.source != "his":
        changed = False
        for field_name in _DEMOGRAPHIC_FIELDS:
            if getattr(patient, field_name) is None and meta.get(field_name):
                setattr(patient, field_name, meta[field_name])
                changed = True
        if changed:
            await session.flush()

    return PatientResolution(
        patient=patient,
        outcome="conflict" if conflicts else "matched",
        identifier=identifier,
        authority=authority_oid,
        conflicts=conflicts,
    )


async def get_or_create_patient(session: AsyncSession, meta: dict,
                                tenant_id: int | None = None,
                                source: str | None = None) -> Patient:
    """Backwards-compatible wrapper around :func:`resolve_patient_identity`."""
    resolution = await resolve_patient_identity(session, meta, tenant_id, source)
    return resolution.patient


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
        # C04: an instance must not *join* a study owned by another tenant. The
        # tenant is applied on first creation only, so without this check a
        # tenant-A writer would silently append data to a tenant-B study (and
        # the file would land in tenant B's study directory).
        if (
            tenant_id is not None
            and study.tenant_id is not None
            and study.tenant_id != tenant_id
        ):
            raise CrossTenantIngestError(
                f"study {study_uid} belongs to another tenant; refusing to append to it"
            )
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
                             storage_backend: str | None = None,
                             read_path: str | None = None,
                             ds: Dataset | None = None,
                             file_size_bytes: int | None = None) -> Instance:
    """Full pipeline for one incoming instance (called by C-STORE and STOW-RS).

    `tenant_id` (VNA) is the owning tenant; callers resolve it from the source
    AE title (C-STORE) or the authenticated user (STOW-RS).
    `storage_backend` (multi-storage) is the named backend the object was
    written to; None = the environment default backend."""
    # C10: identity is scoped to the ingest's tenant, not the whole install.
    patient = await get_or_create_patient(
        session, meta, tenant_id=tenant_id, source=performed_by
    )
    study = await get_or_create_study(session, meta, patient, tenant_id=tenant_id)
    # Late-arriving slices must stay on the study's backend; record it so the
    # study row and its instances agree on where the data lives.
    if storage_backend:
        study.storage_backend = storage_backend
    series = await get_or_create_series(session, meta, study)

    # H09: the object is still staged, so the canonical key has no bytes yet.
    # Prefer the size the caller already measured while staging; then the staged
    # file itself; only then the canonical object (non-staged callers).
    size = 0
    if file_size_bytes is not None:
        size = int(file_size_bytes)
    elif read_path and os.path.exists(str(read_path)):
        size = os.path.getsize(str(read_path))
    else:
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

    from app.services.compression_service import SOURCE_METHOD, THUMBNAIL_METHOD, normalize_method

    has_pixels = (
        "PixelData" in ds if ds is not None else bool(meta.get("rows") and meta.get("columns"))
    )
    method = normalize_method(settings.DEFAULT_COMPRESSION)
    compress = settings.COMPRESSION_ENABLED and method != SOURCE_METHOD
    if has_pixels and settings.THUMBNAIL_ENABLED and (settings.THUMBNAIL_ON_RECEIPT or not compress):
        session.add(CompressionJob(instance_id=instance.id, method=THUMBNAIL_METHOD, status="PENDING"))
    if has_pixels and compress:
        session.add(
            CompressionJob(
                instance_id=instance.id,
                method=method,
                status="PENDING",
            )
        )

    # GSPS: index presentation state metadata for quick lookup.
    if meta.get("sop_class_uid") == _GSPS_SOP_CLASS_UID:
        try:
            from app.services.gsps_service import index_presentation_state

            # H09: the caller already parsed this dataset — never re-read the
            # object, which is still staged and not yet at its canonical key.
            gsps_ds = ds or storage_service.read_dataset(
                read_path or file_path, stop_before_pixels=True, backend_name=storage_backend
            )
            # `file_path` is what gets *recorded*, so it must be the canonical
            # key — the staged path is gone once the object is published.
            await index_presentation_state(session, meta, gsps_ds, file_path)
        except Exception:
            import logging
            logging.getLogger("paxs.indexing").warning(
                "GSPS indexing failed for %s", meta.get("sop_instance_uid"), exc_info=True
            )

    # KIN (Key Object Selection): index key image note metadata.
    if meta.get("sop_class_uid") == "1.2.840.10008.5.1.4.1.1.88.22" or meta.get("modality") == "KO":
        try:
            from app.services.kin_service import index_key_image_note

            kin_ds = ds or storage_service.read_dataset(
                read_path or file_path, stop_before_pixels=True, backend_name=storage_backend
            )
            await index_key_image_note(session, meta, kin_ds, file_path)
        except Exception:
            import logging
            logging.getLogger("paxs.indexing").warning(
                "KIN indexing failed for %s", meta.get("sop_instance_uid"), exc_info=True
            )

    # RDSR (Radiation Dose Structured Report): index radiation dose exposure metrics.
    if meta.get("sop_class_uid") == "1.2.840.10008.5.1.4.1.1.88.67":
        try:
            from app.services.dose_service import index_dose_report

            dose_ds = ds or storage_service.read_dataset(
                read_path or file_path, stop_before_pixels=True, backend_name=storage_backend
            )
            await index_dose_report(session, meta, dose_ds, file_path)
        except Exception:
            import logging
            logging.getLogger("paxs.indexing").warning(
                "RDSR indexing failed for %s", meta.get("sop_instance_uid"), exc_info=True
            )

    await session.flush()
    return instance


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
