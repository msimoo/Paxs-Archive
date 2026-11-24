"""Upserts incoming DICOM metadata into the Patient -> Study -> Series ->
Instance hierarchy and fans out side effects (audit log, compression job).

Idempotent: re-importing an existing instance updates file location/size and
does not duplicate rows (SOP/Study/Series UIDs are unique).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydicom.dataset import Dataset
from sqlalchemy import literal, select, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import dicom_uids
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
from app.services import content_version, query_service, storage_service, worklist_service
from app.services.dicomweb_service import dataset_to_json_text

log = logging.getLogger("paxs.indexing")

# GSPS SOP Class UID — Grayscale Softcopy Presentation State
_GSPS_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.11.1"

# DICOM keyword -> tag, for the mandatory Type 1 UIDs (A-17).
_UID_TAG = dict(dicom_uids.MANDATORY_UIDS)


def _require_uid(meta: dict, meta_key: str, keyword: str) -> str:
    """Return a mandatory Type 1 UID from ``meta``, or raise (A-17).

    Defence in depth behind the ingest boundary, which already refused such a
    dataset. Reaching here means a caller bypassed that check, so raising is the
    correct outcome: the previous behaviour filed every UID-less instance under
    the literal ``"UNKNOWN"``, silently merging unrelated examinations into one
    study bucket.
    """
    return dicom_uids.check_uid(meta.get(meta_key), keyword, _UID_TAG[keyword])


class CrossTenantIngestError(ValueError):
    """An instance tried to join or replace another tenant's study data (C04/A-9).

    Raised instead of silently appending or re-pointing: ingest turns it into a
    **409 Conflict** (STOW-RS) or a Processing Failure (C-STORE), matching the
    C05 hard-reject behaviour. 409 rather than 400 because the request is
    well-formed — it conflicts with existing state the caller does not own.
    """


def tenant_conflict(writer_tenant: int | None, owner_tenant: int | None) -> bool:
    """True when a tenant-scoped writer may not touch a row owned by ``owner_tenant``.

    The single expression of the C04/A-9 rule, shared by the study, series and
    instance checks so the three can never drift apart (the FA-1 lesson).

    Deliberately narrow: a *legacy* writer (``writer_tenant is None``) is the
    pre-VNA unscoped scope and keeps its existing behaviour, and a legacy row
    (``owner_tenant is None``) stays adoptable — that is the documented
    migration path for data ingested before tenants existed. What is refused is
    one tenant reaching into another tenant's row.
    """
    return (
        writer_tenant is not None
        and owner_tenant is not None
        and owner_tenant != writer_tenant
    )


def _ingest_owner_matches_scope(owner: Any, writer_tenant: int | None) -> bool:
    """Require an existing Study and its Patient to share the writer's scope.

    Ingest is stricter than reads: a tenantless source may write only legacy
    NULL-owned rows, and a tenant source may not adopt an unassigned row.
    """
    return bool(
        owner is not None
        and owner.patient_id is not None
        and owner.tenant_id == writer_tenant
        and owner.patient_tenant_id == writer_tenant
    )


async def assert_tenant_owns_ingest_target(
    session: AsyncSession, meta: dict, tenant_id: int | None = None,
    include_all: bool = False,
) -> None:
    """A-9: refuse to append to, or re-point, another tenant's study data.

    ``_reject_cross_tenant_ingest`` only compared the *study* tenant, so a
    replayed ``SeriesInstanceUID``/``SOPInstanceUID`` slipped through and
    ``upsert_instance`` re-pointed the existing row's ``file_path`` at the
    caller's object — one institution's images replaced by another's. All three
    levels are checked here, on the UIDs that identify them globally:

    * study   — C04: do not append into another tenant's study;
    * series  — do not attach instances to another tenant's series;
    * instance— do not adopt/re-point another tenant's SOP instance.

    Read-only: nothing is created, moved or mutated. Raises
    :class:`CrossTenantIngestError` so callers can map it to 409 (STOW-RS) or a
    DIMSE failure (C-STORE).
    """
    if include_all:
        return

    study_uid = str(meta.get("study_instance_uid") or "")
    series_uid = str(meta.get("series_instance_uid") or "")
    sop_uid = str(meta.get("sop_instance_uid") or "")

    # Combine Study / Series / Instance ownership checks into a single SQL
    # round-trip (eliminating 2 extra queries per incoming DICOM slice while
    # preserving exact deterministic study -> series -> instance priority).
    branches = []
    if study_uid:
        branches.append(
            select(
                literal("study").label("level"),
                Study.id.label("id"),
                Study.tenant_id.label("tenant_id"),
                Patient.id.label("patient_id"),
                Patient.tenant_id.label("patient_tenant_id"),
            )
            .outerjoin(Patient, Patient.id == Study.patient_id)
            .where(Study.study_instance_uid == study_uid)
        )
    if series_uid:
        branches.append(
            select(
                literal("series").label("level"),
                Study.id.label("id"),
                Study.tenant_id.label("tenant_id"),
                Patient.id.label("patient_id"),
                Patient.tenant_id.label("patient_tenant_id"),
            )
            .join(Series, Series.study_id == Study.id)
            .outerjoin(Patient, Patient.id == Study.patient_id)
            .where(Series.series_instance_uid == series_uid)
        )
    if sop_uid:
        branches.append(
            select(
                literal("instance").label("level"),
                Study.id.label("id"),
                Study.tenant_id.label("tenant_id"),
                Patient.id.label("patient_id"),
                Patient.tenant_id.label("patient_tenant_id"),
            )
            .join(Series, Series.study_id == Study.id)
            .join(Instance, Instance.series_id == Series.id)
            .outerjoin(Patient, Patient.id == Study.patient_id)
            .where(Instance.sop_instance_uid == sop_uid)
        )
    if not branches:
        return

    stmt = branches[0] if len(branches) == 1 else union_all(*branches)
    rows_by_level = {row.level: row for row in (await session.execute(stmt)).all()}

    if "study" in rows_by_level and not _ingest_owner_matches_scope(rows_by_level["study"], tenant_id):
        raise CrossTenantIngestError(
            f"study {study_uid} is outside the source tenant scope; refusing to append"
        )
    if "series" in rows_by_level and not _ingest_owner_matches_scope(rows_by_level["series"], tenant_id):
        raise CrossTenantIngestError(
            f"series {series_uid} is outside the source tenant scope; refusing to write"
        )
    if "instance" in rows_by_level and not _ingest_owner_matches_scope(rows_by_level["instance"], tenant_id):
        raise CrossTenantIngestError(
            f"instance {sop_uid} is outside the source tenant scope; refusing to replace"
        )


def _staged_size(read_path: str) -> int | None:
    """Bytes of the staged object, or ``None`` when it is absent/unreadable."""
    try:
        return os.path.getsize(read_path)
    except OSError:
        return None


async def _resolve_instance_size(
    file_path: str,
    file_size_bytes: int | None,
    read_path: str | None,
    storage_backend: str | None,
) -> int:
    """Bytes for the instance row: caller-measured, staged file, or canonical object.

    H09: while the object is still staged the canonical key has no bytes yet, so
    the staged file is the only accurate measure. N-4: both filesystem calls run
    on a worker thread — ingest is async and must not block the loop while a
    modality is streaming.

    Returns 0 (the documented "size unknown" sentinel) when every source fails;
    a genuinely unreachable backend must not look like a 0-byte object in the
    audit trail, so the failure is logged first.
    """
    if file_size_bytes is not None:
        return int(file_size_bytes)
    if read_path:
        staged = await asyncio.to_thread(_staged_size, read_path)
        if staged is not None:
            return staged
    try:
        return storage_service.size(file_path, backend_name=storage_backend)
    except OSError as exc:  # size is metadata; indexing must proceed
        log.debug("index_object_size_unavailable error_type=%s", type(exc).__name__)
        return 0


def _int(value: object) -> int | None:
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


async def resolve_patient_identity(
    session: AsyncSession,
    meta: dict,
    tenant_id: int | None = None,
    source: str | None = None,
) -> PatientResolution:
    """Resolve patient identity (races are settled by the savepoint retry below)."""
    return await _resolve_patient_identity_transaction(session, meta, tenant_id, source)


async def _resolve_patient_identity_transaction(
    session: AsyncSession,
    meta: dict,
    tenant_id: int | None,
    source: str | None,
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
    identity_patient = (
        await session.execute(
            select(PatientIdentity, Patient)
            .join(Patient, PatientIdentity.patient_id == Patient.id)
            .where(
                query_service.tenant_predicate(PatientIdentity.tenant_id, tenant_id),
                PatientIdentity.assigning_authority_id == authority.id,
                PatientIdentity.identifier == identifier,
            )
        )
    ).first()
    identity: PatientIdentity | None = identity_patient[0] if identity_patient else None
    patient: Patient | None = identity_patient[1] if identity_patient else None
    if patient is not None and patient.tenant_id != tenant_id:
        # The identity's direct owner is not authoritative when its linked
        # Patient disagrees. Reject the corrupted mapping instead of adopting
        # or mutating another tenant's patient row.
        raise ValueError("Patient identity ownership does not match tenant")

    if identity is not None:
        assert patient is not None
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
                    query_service.tenant_predicate(Patient.tenant_id, tenant_id),
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
                            query_service.tenant_predicate(Patient.tenant_id, tenant_id),
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
    study_uid = _require_uid(meta, "study_instance_uid", "StudyInstanceUID")
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
        from app.services.scripting_service import fire as script_fire
        from app.services.webhook_service import fire as webhook_fire

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
        if settings.ROUTING_QUEUE_ENABLED:
            # Evaluate on this transaction: a second session cannot see the new
            # uncommitted study and would silently omit its durable routing job.
            from app.services import routing_service

            await routing_service.evaluate_and_route_study(
                study_uid, meta, tenant_id=tenant_id, session=session
            )
    else:
        # C04: an instance must not *join* a study owned by another tenant. The
        # tenant is applied on first creation only, so without this check a
        # tenant-A writer would silently append data to a tenant-B study (and
        # the file would land in tenant B's study directory).
        if tenant_conflict(tenant_id, study.tenant_id):
            raise CrossTenantIngestError(
                f"study {study_uid} belongs to another tenant; refusing to append to it"
            )
        if meta.get("study_description") and study.study_description != meta["study_description"]:
            study.study_description = meta["study_description"]
            await session.flush()
    return study


async def get_or_create_series(session: AsyncSession, meta: dict, study: Study) -> Series:
    series_uid = _require_uid(meta, "series_instance_uid", "SeriesInstanceUID")
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
                          storage_backend: str | None = None,
                          tenant_id: int | None = None,
                          metadata_json: str | None = None) -> Instance:
    """Create the instance row, or refresh an existing one's stored location.

    A-9: the refresh path is a *mutation* of an existing row, so it re-checks
    ownership at the point of mutation — `assert_tenant_owns_ingest_target` has
    already run by here, and this second check is what makes "never re-point
    another tenant's file_path" true even if a caller reaches this function
    directly. `tenant_id=None` keeps the previous behaviour for the legacy scope.

    ``metadata_json`` is the WADO-RS view of the object, produced by the caller
    while it already had the dataset open. A re-sent instance *replaces* the
    object, so it replaces the cached metadata too — a stale copy here would
    describe a different file (and, after a transfer-syntax change, make the
    viewer pick the wrong decoder). ``None`` leaves an existing value alone only
    when nothing was re-read, so callers that did read pass the fresh text.
    """
    sop_uid = _require_uid(meta, "sop_instance_uid", "SOPInstanceUID")
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
            metadata_json=metadata_json,
        )
        session.add(instance)
        await session.flush()
        # H-07: remembered (not persisted) so a post-commit promote failure
        # knows it may delete this row rather than one that pre-existed.
        instance._paxs_ingest_created = True
    else:
        if tenant_id is not None:
            # The instance's owner is the tenant of the study it sits in.
            owner = (
                await session.execute(
                    select(Study.tenant_id)
                    .join(Series, Series.study_id == Study.id)
                    .where(Series.id == instance.series_id)
                )
            ).scalar_one_or_none()
            if tenant_conflict(tenant_id, owner):
                raise CrossTenantIngestError(
                    f"instance {sop_uid} belongs to another tenant; "
                    "refusing to replace its stored object"
                )
        instance.file_path = file_path
        instance.file_size_bytes = file_size
        instance.storage_backend = storage_backend
        # C-04: a re-sent/corrected object replaces the bytes behind this SOP
        # UID; advance its version so no cached representation outlives it.
        content_version.bump(instance)
        if meta.get("transfer_syntax_uid"):
            instance.transfer_syntax_uid = meta["transfer_syntax_uid"]
        if metadata_json is not None:
            instance.metadata_json = metadata_json
        await session.flush()
    return instance


async def index_new_instance(session: AsyncSession, meta: dict,
                             file_path: str, sha256: str | None = None,
                             performed_by: str = "dicom-receiver",
                             tenant_id: int | None = None,
                             storage_backend: str | None = None,
                             read_path: str | None = None,
                             ds: Dataset | None = None,
                             file_size_bytes: int | None = None,
                             include_all: bool = False) -> Instance:
    """Full pipeline for one incoming instance (called by C-STORE and STOW-RS).

    `tenant_id` (VNA) is the owning tenant; callers resolve it from the source
    AE title (C-STORE) or the authenticated user (STOW-RS).
    `storage_backend` (multi-storage) is the named backend the object was
    written to; None = the environment default backend.

    Raises:
        CrossTenantIngestError: when any of the incoming UIDs already belongs to
            another tenant (A-9) — checked before anything is written, so a
            refused ingest leaves no row and no object behind.
    """
    # The WADO-RS view of this object, produced now because the dataset is
    # already parsed here. Doing it on the request path instead costs 13 ms per
    # instance every time a viewer opens the study (82 s for a 4k-instance
    # study, measured) — ingest pays it once, off the event loop.
    metadata_json = (
        await asyncio.to_thread(dataset_to_json_text, ds) if ds is not None else None
    )

    # A-9: the authoritative ownership check for *both* ingest paths (STOW-RS and
    # C-STORE). The STOW router also calls this before staging so a refused
    # upload never touches disk; this call is what makes the guarantee hold for
    # every other caller too.
    await assert_tenant_owns_ingest_target(
        session, meta, tenant_id, include_all=include_all
    )
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
    size = await _resolve_instance_size(
        file_path, file_size_bytes, read_path, storage_backend
    )
    instance = await upsert_instance(
        session, series, meta, file_path, size, storage_backend=storage_backend,
        tenant_id=tenant_id, metadata_json=metadata_json,
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

    # Invalidate the current copy even if replication is already in progress.
    if settings.DR_REPLICATION_ENABLED:
        from app.services.replication_service import mark_study_dirty

        await mark_study_dirty(session, study.study_instance_uid)

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
        except Exception as exc:  # optional GSPS indexing must not fail ingest
            log.warning("gsps_indexing_failed error_type=%s", type(exc).__name__)

    # KIN (Key Object Selection): index key image note metadata.
    if meta.get("sop_class_uid") == "1.2.840.10008.5.1.4.1.1.88.22" or meta.get("modality") == "KO":
        try:
            from app.services.kin_service import index_key_image_note

            kin_ds = ds or storage_service.read_dataset(
                read_path or file_path, stop_before_pixels=True, backend_name=storage_backend
            )
            await index_key_image_note(session, meta, kin_ds, file_path)
        except Exception as exc:  # optional KIN indexing must not fail ingest
            log.warning("kin_indexing_failed error_type=%s", type(exc).__name__)

    # RDSR (Radiation Dose Structured Report): index radiation dose exposure metrics.
    if meta.get("sop_class_uid") == "1.2.840.10008.5.1.4.1.1.88.67":
        try:
            from app.services.dose_service import index_dose_report

            dose_ds = ds or storage_service.read_dataset(
                read_path or file_path, stop_before_pixels=True, backend_name=storage_backend
            )
            await index_dose_report(session, meta, dose_ds, file_path)
        except Exception as exc:  # optional dose indexing must not fail ingest
            log.warning("rdsr_indexing_failed error_type=%s", type(exc).__name__)

    await session.flush()
    return instance


def instance_was_created(instance: Instance | None) -> bool:
    """True when this ingest *inserted* the row (H-07), False when it replaced one."""
    return bool(getattr(instance, "_paxs_ingest_created", False))


async def compensate_unpublished_instance(session: AsyncSession, sop_instance_uid: str,
                                          *, created: bool,
                                          performed_by: str) -> bool:
    """Undo a committed index whose object never reached its canonical key (H-07).

    Ingest commits the row, then promotes the staged object. When the promote
    fails the row points at a key with no bytes: QIDO lists it, WADO 404s. A row
    this ingest *created* is deleted (with its queued compression jobs, deleted
    explicitly because SQLite does not enforce the cascade by default). A row
    that pre-existed is left alone — the previous object is still at the key
    and deleting it would destroy a good instance. Either way an audit row
    records the failure. Commits its own transaction; returns True when a row
    was removed. Rejected alternative: an ``availability`` column — it needs a
    migration and every read path would have to learn to filter on it.
    """
    from sqlalchemy import delete

    removed = False
    instance = (
        await session.execute(
            select(Instance).where(Instance.sop_instance_uid == sop_instance_uid)
        )
    ).scalar_one_or_none()
    if created and instance is not None:
        await session.execute(
            delete(CompressionJob).where(CompressionJob.instance_id == instance.id)
        )
        await session.delete(instance)
        removed = True
    session.add(
        AuditLog(
            sop_instance_uid=sop_instance_uid,
            action="STORE_PUBLISH_FAILED",
            performed_by=performed_by,
        )
    )
    await session.commit()
    log.error(
        "ingest_publish_failed_compensated sop_instance_uid=%s row_removed=%s",
        sop_instance_uid, removed,
    )
    return removed


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
