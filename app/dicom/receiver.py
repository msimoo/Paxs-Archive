"""DICOM DIMSE receiver (SCP) built on pynetdicom.

Services:
  * C-STORE SCP  — accept images from modalities (CT/MR/CR/US/...)
  * C-FIND SCP   — answer query/retrieve worklist queries against the DB
  * C-GET SCP    — send stored instances back to requesting SCUs
  * C-ECHO       — verification

The store handler saves the file, indexes metadata and queues compression.
Because pynetdicom runs handlers in its own threads, database work happens
either on the sync engine (C-FIND / C-GET) or in a fresh event loop (C-STORE).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Iterator
import ssl
import threading
import time
from functools import wraps
from typing import Any

import pydicom
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
from pynetdicom import AE, StoragePresentationContexts, evt
from pynetdicom.sop_class import (
    ModalityPerformedProcedureStep,
    ModalityWorklistInformationFind,
    StorageCommitmentPushModel,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelGet,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)
from sqlalchemy import Select, select

from app.core import dicom_uids
from app.core.observability import record_dimse_operation
from app.core.config import settings
from app.db.models import DicomNode, Patient
from app.db.session import DimseAsyncSessionLocal, SyncSessionLocal, run_dimse_coro
from app.dicom import receiver_queue
from app.dicom.parser import parse_metadata
from app.dicom.receiver_queue import receiver_queue_snapshot as receiver_queue_snapshot
from app.services import (
    mpps_service,
    query_service,
    storage_commit_service,
    storage_service,
    tenant_service,
    worklist_service,
)
from app.services.indexing_service import (
    CrossTenantIngestError,
    compensate_unpublished_instance,
    index_new_instance,
    instance_was_created,
)
from app.services.node_service import get_node_sync
from app.services.quota_service import QuotaExceeded

log = logging.getLogger("paxs.dicom")

_ae: AE | None = None

#: Status returned to an unauthorized caller from a *handler* (defence in depth
#: behind the association gate). 0x0113 is the DICOM "calling AE title not
#: recognized" code; the association-level equivalent is an A-ASSOCIATE-RJ with
#: diagnostic 0x03 (see :func:`handle_association_requested`).
STATUS_CALLING_AE_NOT_RECOGNIZED = 0x0113

#: "Identifier does not match SOP Class" — returned when a SERIES/INSTANCE
#: C-FIND omits the parent UID that scopes it (B-8).
STATUS_IDENTIFIER_MISMATCH = 0x0122

#: H-04: transfer syntaxes for every non-storage context. Implicit VR LE is the
#: mandatory default (PS3.5 §10.1); Explicit VR LE is kept for modern peers.
NON_STORAGE_TRANSFER_SYNTAXES = [ImplicitVRLittleEndian, ExplicitVRLittleEndian]

# --------------------------------------------------------------------------- #
# Calling-AE allowlist (B-8)
# --------------------------------------------------------------------------- #
# `_calling_ae(event)` was already extracted but only used for tenant mapping:
# any AE that could reach the port could associate, store and enumerate. The
# allowlist closes that. It is the union of the configured titles and the peers
# registered in `dicom_nodes` (a node we dial is a node we trust to call us).
#
# The DB read is cached for a few seconds: this runs once per association and
# once per handler, and a C-STORE burst must not become a query per instance.
_ALLOWLIST_TTL_SECONDS = 5.0
_allowlist_cache: tuple[float, frozenset[str]] | None = None
_allowlist_lock = threading.Lock()


def _configured_calling_aes() -> set[str]:
    """Titles from ``DICOM_ALLOWED_CALLING_AES`` (comma-separated)."""
    raw = getattr(settings, "DICOM_ALLOWED_CALLING_AES", "") or ""
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def _registered_calling_aes() -> set[str]:
    """AE titles of every registered ``dicom_nodes`` peer."""
    try:
        with SyncSessionLocal() as session:
            rows = session.execute(select(DicomNode.ae_title)).all()
        return {str(title).strip().upper() for (title,) in rows if title}
    except Exception as exc:
        # A DB hiccup must not widen the allowlist. The configured titles still
        # apply; an archive with neither configured nor registered titles keeps
        # the legacy open behaviour, which is why this is not fatal.
        log.warning("could not read registered AE titles for the calling-AE "
                    "allowlist (error_type=%s)", type(exc).__name__)
        return set()


def allowed_calling_aes(*, refresh: bool = False) -> frozenset[str]:
    """Calling AE titles permitted to associate (empty = no allowlist).

    Enforcement is **opt-in**: it switches on when ``DICOM_ALLOWED_CALLING_AES``
    names at least one title. Once it is on, the registered ``dicom_nodes``
    titles are accepted too — a peer this archive already dials is a peer it
    trusts to call it back — so an operator does not have to list its own
    destinations twice.

    Registered titles are deliberately **not** enough on their own to enable
    enforcement: an archive that merely has one forwarding destination would
    otherwise start rejecting every modality the moment it upgraded, because a
    modality is never registered as a `dicom_nodes` peer.
    """
    global _allowlist_cache
    configured = _configured_calling_aes()
    if not configured:
        return frozenset()

    now = time.monotonic()
    with _allowlist_lock:
        if (
            not refresh
            and _allowlist_cache is not None
            and now - _allowlist_cache[0] < _ALLOWLIST_TTL_SECONDS
        ):
            return _allowlist_cache[1]
    titles = frozenset(configured | _registered_calling_aes())
    with _allowlist_lock:
        _allowlist_cache = (now, titles)
    return titles


def invalidate_calling_ae_cache() -> None:
    """Drop the cached allowlist (node added/removed, config overridden)."""
    global _allowlist_cache
    with _allowlist_lock:
        _allowlist_cache = None


def is_calling_ae_allowed(calling_ae: str | None) -> bool:
    """True when ``calling_ae`` may use this SCP.

    No allowlist configured → allow (backwards compatible). An allowlist
    configured but no calling AE presented → deny: an unidentified caller is
    exactly what the allowlist exists to exclude.
    """
    allowed = allowed_calling_aes()
    if not allowed:
        return True
    if not calling_ae:
        return False
    return str(calling_ae).strip().upper() in allowed


def _requested_calling_ae(event) -> str | None:
    """The calling AE of an *association request* (B-8).

    `EVT_REQUESTED` fires before ACSE negotiation, so ``requestor.ae_title`` is
    still the empty placeholder the acceptor was constructed with — reading it
    here would see ``''`` for every peer. The title exists only on the raw
    A-ASSOCIATE primitive at this point. (Inside a handler, after negotiation,
    :func:`_calling_ae` is correct.)
    """
    requestor = getattr(getattr(event, "assoc", None), "requestor", None)
    primitive = getattr(requestor, "primitive", None)
    title = getattr(primitive, "calling_ae_title", None)
    if title:
        return str(title)
    return getattr(requestor, "ae_title", None) or None


def handle_association_requested(event) -> None:
    """Reject an association whose calling AE is not allowlisted (B-8).

    pynetdicom dispatches ``EVT_REQUESTED`` before ACSE negotiation and skips
    negotiation if the association is already rejected, so this is the one place
    an unauthorized peer can be turned away before it sends a single DIMSE
    message. The reject is ``result=0x01`` (permanent), ``source=0x01`` (DUL
    service user), ``diagnostic=0x03`` (calling AE title not recognised) — the
    ACSE form of status ``0x0113``.
    """
    calling_ae = _requested_calling_ae(event)
    if is_calling_ae_allowed(calling_ae):
        return
    # N-2: the AE title is an operational identifier, not PHI, and naming it is
    # what makes a misconfigured peer diagnosable.
    log.warning("DICOM association rejected: calling AE %r is not allowlisted",
                calling_ae or "<absent>")
    try:
        event.assoc.acse.send_reject(0x01, 0x01, 0x03)
    except Exception as exc:  # Reject transport is best-effort before the abort fallback.
        # Fall back to an abort so a rejected peer still cannot proceed.
        log.debug(
            "dicom_association_reject_failed aborting error_type=%s",
            type(exc).__name__,
        )
        try:
            event.assoc.abort()
        except Exception as abort_exc:  # Both transport failure details are untrusted.
            log.debug(
                "dicom_association_abort_failed error_type=%s",
                type(abort_exc).__name__,
            )


# --------------------------------------------------------------------------- #
# C-STORE
# --------------------------------------------------------------------------- #
def _resolve_tenant(source_ae: str | None) -> int | None:
    """Tenant the calling AE maps to, resolved before any write (H08).

    C-03: synchronous, on the thread-safe sync engine — the same path C-FIND,
    MPPS and Storage Commitment already use. It used to spin up an event loop
    per C-STORE just for this lookup, on the API's async pool.
    """
    with SyncSessionLocal() as session:
        return tenant_service.resolve_tenant_id_for_ae_sync(session, source_ae)


def _calling_ae(event) -> str | None:
    return getattr(getattr(event.assoc, "requestor", None), "ae_title", None)


_ASSOC_CACHE_MAX = 256
_assoc_cache: dict[int, dict[str, Any]] = {}
_assoc_cache_lock = threading.Lock()


def _assoc_entry(assoc: Any) -> dict[str, Any]:
    key = id(assoc)
    with _assoc_cache_lock:
        entry = _assoc_cache.get(key)
        if entry is None:
            if len(_assoc_cache) >= _ASSOC_CACHE_MAX:
                _assoc_cache.pop(next(iter(_assoc_cache)), None)
            entry = {"backends": {}}
            _assoc_cache[key] = entry
        return entry


def _resolve_tenant_for_assoc(assoc: Any, source_ae: str | None) -> int | None:
    """Cache tenant lookup per live association so 2,000-slice CT bursts pay it once."""
    if assoc is None:
        return _resolve_tenant(source_ae)
    entry = _assoc_entry(assoc)
    if "tenant_id" in entry and entry.get("source_ae") == source_ae:
        return entry["tenant_id"]
    tenant_id = _resolve_tenant(source_ae)
    entry["source_ae"] = source_ae
    entry["tenant_id"] = tenant_id
    return tenant_id


def _resolve_backend_for_assoc(assoc: Any, study_uid: str) -> str | None:
    """Cache study storage-backend lookup per live association."""
    if assoc is None:
        return storage_service.resolve_study_backend(study_uid)
    backends = _assoc_entry(assoc)["backends"]
    if study_uid in backends:
        return backends[study_uid]
    backend = storage_service.resolve_study_backend(study_uid)
    if len(backends) < 64:
        backends[study_uid] = backend
    return backend


def _track_association_end(event) -> None:
    with _assoc_cache_lock:
        _assoc_cache.pop(id(event.assoc), None)
    if event.event == evt.EVT_CONN_CLOSE and event.assoc.is_released:
        return
    receiver_queue.finish_association(event.assoc, failed=event.event != evt.EVT_RELEASED)


async def _index_async(meta: dict, path: str, sha256: str, tenant_id: int | None,
                       backend: str | None = None, read_path: str | None = None,
                       ds=None, file_size_bytes: int | None = None) -> bool:
    """Index and commit one C-STORE instance; True when the row was newly created."""
    # C-03: DIMSE-only engine (NullPool). This coroutine runs under a fresh
    # ``asyncio.run`` loop on a pynetdicom thread and must not borrow — or
    # return — a connection owned by the API's event loop.
    async with DimseAsyncSessionLocal() as session:
        async with session.begin():
            # Use the mapping snapshot resolved before quota reservation/staging.
            # Re-resolving here could write the same object under a different
            # tenant if an administrator remapped the AE during a large store.
            instance = await index_new_instance(
                session, meta, path, sha256, performed_by="dicom-receiver",
                tenant_id=tenant_id, storage_backend=backend,
                read_path=read_path,
                ds=ds,
                file_size_bytes=file_size_bytes,
            )
            return instance_was_created(instance)


async def _compensate_async(sop_instance_uid: str, created: bool) -> None:
    """H-07: undo the committed row after its object failed to publish."""
    async with DimseAsyncSessionLocal() as session:
        await compensate_unpublished_instance(
            session, sop_instance_uid, created=created, performed_by="dicom-receiver",
        )


def _promote_or_compensate(staged, sop_instance_uid: str, created: bool) -> None:
    """Publish the staged object; on failure, compensate the committed row and re-raise.

    The row is already durable at this point, so a raise here used to leave an
    instance that QIDO lists and WADO cannot serve (H-07). Compensation failure
    is logged but never masks the original promote error.
    """
    try:
        staged.promote()
    except Exception:
        try:
            run_dimse_coro(_compensate_async(sop_instance_uid, created))
        except Exception as comp_exc:  # report the promote failure, not this one
            log.error("c_store_compensation_failed sop_instance_uid=%s error_type=%s",
                      sop_instance_uid, type(comp_exc).__name__)
        raise


def handle_store(event) -> int:
    ds = event.dataset
    if ds is None:
        return 0xC212  # Processing failure
    # B-8 defence in depth: the association gate already refused an unlisted
    # caller, but the allowlist can change (or be reloaded) between the request
    # and this message. Refusing here costs one set lookup.
    source_ae = _calling_ae(event)
    if not is_calling_ae_allowed(source_ae):
        log.warning("C-STORE refused: calling AE is not allowlisted")
        return STATUS_CALLING_AE_NOT_RECOGNIZED
    path: str | None = None
    staged = None
    backend: str | None = None
    tracked_study_uid: str | None = None
    stored_bytes: int | None = None
    try:
        ds.file_meta = event.file_meta
        # A-17: the three Type 1 UIDs must be present and usable *before*
        # anything is staged. This check used to skip absent values, which then
        # fell back to a constant bucket ("UNKNOWN" / sha1("")) and merged every
        # UID-less study from every patient into one.
        uids = dicom_uids.validate_mandatory_uids(ds)
        receiver_queue.begin_store(uids["StudyInstanceUID"], event.assoc, source_ae, ds)
        tracked_study_uid = uids["StudyInstanceUID"]
        # C05: and each must be a safe path segment, so a UID can never create
        # an object outside the storage root. Still load-bearing when
        # DICOM_UID_STRICT_FORMAT is disabled and the format rule is relaxed.
        for _keyword, _value in uids.items():
            storage_service.validate_component(_value, field=_keyword)
        # Multi-storage: write new studies to the registered default backend
        # (None = env backend); late slices rejoin the study's recorded one.
        backend = _resolve_backend_for_assoc(event.assoc, uids["StudyInstanceUID"])
        # H08: the quota is scoped to the tenant the AE maps to, so it must be
        # resolved *before* the object is staged (nothing is written first).
        tenant_id = _resolve_tenant_for_assoc(event.assoc, source_ae)
        staged = storage_service.stage_dataset_with_quota(
            ds, tenant_id, backend_name=backend
        )
        meta = parse_metadata(ds)
        try:
            created = run_dimse_coro(_index_async(
                meta, staged.key, staged.sha256, tenant_id=tenant_id, backend=backend,
                read_path=staged.staged_path or staged.key,
                ds=ds,
                file_size_bytes=staged.bytes_written,
            ))
        except Exception:
            # H09: the row never became durable — drop the staged file and give
            # the reservation back. A pre-existing object is never touched.
            staged.discard()
            raise
        # Indexing committed: publish the object (H-07: compensate on failure;
        # the generic handler below then discards the spool and answers 0xC212).
        _promote_or_compensate(staged, uids["SOPInstanceUID"], created)
        path = staged.key
        sha256 = staged.sha256
        # Storage Commitment: if a modality asked (N-ACTION) before its images
        # were fully stored, each arriving instance re-verifies the pending
        # requests — the N-EVENT-REPORT fires automatically once the last
        # referenced instance lands (no manual re-request).
        try:
            storage_commit_service.on_instance_stored(uids["SOPInstanceUID"])
        except Exception as exc:  # store success must survive optional hook failure
            log.error("storage_commit_store_hook_failed error_type=%s",
                      type(exc).__name__)
        stored_bytes = staged.bytes_written
        return 0x0000  # Success
    except QuotaExceeded as exc:
        # H08: Refused — Out of Resources (PS3.4). Nothing was written.
        log.warning("C-STORE refused by quota: %s", exc)
        return 0xA700
    except dicom_uids.Type1UIDError as exc:
        # A-17: the dataset cannot be filed. Validation runs *before* staging, so
        # nothing was written and there is nothing to compensate.
        #
        # 0xC000 ("Error: Cannot understand") is the defined C-STORE failure for
        # an unusable payload. 0x0110 is an N-service/MPPS code, not a valid
        # C-STORE status, so it is not returned on the DIMSE path — the precise
        # tag and reason go to the log instead.
        log.warning("C-STORE rejected, unusable Type 1 UID: %s", exc)
        return exc.dimse_status
    except storage_service.InvalidStorageKey as exc:
        # C05: malformed identifier -> 0xC000 "Cannot understand" (PS3.4).
        # Nothing was written (validation precedes the save), so no cleanup.
        log.warning("c_store_invalid_storage_key error_type=%s", type(exc).__name__)
        return 0xC000
    except CrossTenantIngestError as exc:
        # C04: refused a cross-tenant append; the object written above is removed
        # by the generic handler's cleanup, so re-raise it as-is.
        log.warning("C-STORE rejected, cross-tenant ingest: %s", exc)
        raise
    except Exception as exc:
        log.error("c_store_handler_failed error_type=%s", type(exc).__name__)
        # H09: compensation must never delete a pre-existing object. Only the
        # staged file this call created is removed (a failed reimport used to
        # delete the *existing* instance via this path).
        if staged is not None:
            try:
                staged.discard()
            except Exception as cleanup_exc:
                log.warning("c_store_staged_cleanup_failed error_type=%s",
                            type(cleanup_exc).__name__)
        return 0xC212  # Processing failure
    finally:
        if tracked_study_uid is not None:
            receiver_queue.finish_store(tracked_study_uid, stored_bytes)


# --------------------------------------------------------------------------- #
# C-FIND
# --------------------------------------------------------------------------- #
_PATIENT_KEYS = {
    "PatientID": "patient_id",
    "PatientName": "patient_name",
    "PatientBirthDate": "patient_birth_date",
    "PatientSex": "patient_sex",
}
_STUDY_KEYS = {
    **_PATIENT_KEYS,
    "StudyDate": "study_date",
    "StudyTime": "study_time",
    "AccessionNumber": "accession_number",
    "StudyDescription": "study_description",
    "StudyInstanceUID": "study_instance_uid",
    "ReferringPhysicianName": "referring_physician",
    "StudyID": "study_id",
}
# Study-row keys excluding the patient keys (which are read from
# `study.patient` — the study row's `patient_id` is the integer FK, not the
# MRN). Kept separate so the STUDY-level C-FIND response never fills patient
# fields from the wrong source.
_STUDY_ROW_KEYS = {k: v for k, v in _STUDY_KEYS.items() if k not in _PATIENT_KEYS}
_SERIES_KEYS = {
    "Modality": "modality",
    "SeriesInstanceUID": "series_instance_uid",
    "SeriesNumber": "series_number",
    "SeriesDescription": "series_description",
    "BodyPartExamined": "body_part_examined",
}
_INSTANCE_KEYS = {
    "SOPInstanceUID": "sop_instance_uid",
    "SOPClassUID": "sop_class_uid",
    "InstanceNumber": "instance_number",
    "Rows": "rows",
    "Columns": "columns",
}


def _fill_from(ds, src, keys: dict, requested: set[str]) -> None:
    for keyword, attr in keys.items():
        if keyword not in requested:
            continue
        value = getattr(src, attr, None)
        if value is not None:
            try:
                setattr(ds, keyword, value)
            except (ValueError, TypeError, KeyError) as exc:  # one unconvertible key must not abort C-FIND
                # A VR/validation refusal for one attribute: the reply simply
                # omits it. Debug, type only — the value is patient data (N-2).
                log.debug("cfind_key_not_settable error_type=%s", type(exc).__name__)


def _query_filters(query, keys: dict[str, str]) -> dict:
    """C-FIND semantics: a key with an empty value requests the field be
    returned; only non-empty values act as match filters."""
    filters: dict = {}
    for kw in keys:
        value = getattr(query, kw, None) if kw in query else None
        # pydicom coerces "" on numeric VRs to None — never match on a None
        # (a None value is a malformed request, not a real filter).
        if value is None:
            continue
        if str(value).strip():
            filters[kw] = value
    return filters


def handle_worklist_find(event):
    """C-FIND SCP for the Modality Worklist Information Model.

    Same protocol as Q/R C-FIND (0xFF00 per match, then 0x0000), but the query
    is single-level and the matches are scheduled procedure steps joined to
    their patient.
    """
    query = event.identifier
    if query is None:
        yield 0xC212, None
        return

    requested = worklist_service.requested_keys(query)
    filters = worklist_service.query_filters(query)
    log.info("MWL C-FIND filter_keys=%s", sorted(filters))

    with SyncSessionLocal() as session:
        # B-9: a worklist row inherits its tenant through the patient. A mapped
        # calling AE only ever sees its own institution's scheduled steps;
        # an unmapped AE keeps the legacy (NULL) scope.
        tenant_id = tenant_service.resolve_tenant_id_for_ae_sync(
            session, _calling_ae(event)
        )
        rows = session.execute(
            worklist_service.build_statement(filters, tenant_id=tenant_id)
        )
        for step, patient in rows:
            if _dimse_was_cancelled(event):
                yield 0xFE00, None
                return
            ds = worklist_service.serialize_sps(step, patient, requested)
            yield 0xFF00, ds

    if _dimse_was_cancelled(event):
        yield 0xFE00, None
        return
    yield 0x0000, None


def _new_find_response(level: str) -> pydicom.Dataset:
    """Empty C-FIND response at ``level``, declaring UTF-8 (H-03).

    Patient names here are routinely Arabic. Without (0008,0005) pydicom
    encodes PN/LO in the default repertoire, and a value it cannot encode is
    dropped by ``_fill_from`` — the modality then shows no name (wrong-patient
    risk). ISO_IR 192 can carry every value the database holds.
    """
    ds = pydicom.Dataset()
    ds.SpecificCharacterSet = worklist_service.RESPONSE_CHARACTER_SET
    ds.QueryRetrieveLevel = level
    return ds


def _find_patients(session, query, requested: set[str], tenant_id) -> Iterator:
    """PATIENT level: patients that own at least one study in scope."""
    stmt = select(Patient)
    for kw, attr in _PATIENT_KEYS.items():
        if kw in query and str(getattr(query, kw)).strip():
            value = str(getattr(query, kw))
            col = getattr(Patient, attr)
            stmt = stmt.where(
                col.ilike(value.replace("*", "%")) if "*" in value else col == value
            )
    # Only patients that own at least one study inside the tenant scope.
    from app.db.models import Study

    sub = select(Study.patient_id)
    sub = query_service.apply_study_tenant_scope(sub, tenant_id, include_all=False)
    stmt = stmt.where(Patient.id.in_(sub))
    for patient in session.execute(stmt).scalars():
        ds = _new_find_response("PATIENT")
        _fill_from(ds, patient, _PATIENT_KEYS, requested)
        yield 0xFF00, ds


def _find_studies(session, query, requested: set[str], tenant_id) -> Iterator:
    """STUDY level: full study rows within the caller's scope."""
    filters = _query_filters(query, _STUDY_KEYS)
    stmt = query_service.study_query(filters, tenant_id=tenant_id, include_all=False)
    for study in session.execute(stmt).scalars().unique():
        ds = _new_find_response("STUDY")
        _fill_from(ds, study.patient, _PATIENT_KEYS, requested)
        _fill_from(ds, study, _STUDY_ROW_KEYS, requested)
        yield 0xFF00, ds


def _find_series(session, query, requested: set[str], tenant_id) -> Iterator:
    """SERIES level, scoped by the required parent StudyInstanceUID (B-8).

    A SERIES query without its parent study enumerated every series in the
    archive (tenant-scoped) — a browsing oracle. The parent UID is required,
    and applied as a join condition — not as a LIKE filter on a UID.
    """
    study_uid = str(getattr(query, "StudyInstanceUID", "") or "").strip()
    if not study_uid:
        log.info("C-FIND SERIES refused: StudyInstanceUID is required")
        yield STATUS_IDENTIFIER_MISMATCH, None
        return
    filters = _query_filters(query, _SERIES_KEYS)
    stmt = query_service.series_query(
        study_uid, filters, tenant_id=tenant_id, include_all=False
    )
    for series in session.execute(stmt).scalars():
        ds = _new_find_response("SERIES")
        # Full context: patient + study rows above the series.
        _fill_from(ds, series.study.patient, _PATIENT_KEYS, requested)
        _fill_from(ds, series.study, _STUDY_ROW_KEYS, requested)
        _fill_from(ds, series, _SERIES_KEYS, requested)
        yield 0xFF00, ds


def _find_instances(session, query, requested: set[str], tenant_id) -> Iterator:
    """INSTANCE level, scoped by the required parent SeriesInstanceUID (B-8).

    An INSTANCE query without its parent series walked the whole archive; the
    StudyInstanceUID is applied as well when the caller supplies one, and a
    mismatched study/series pair returns nothing.
    """
    series_uid = str(getattr(query, "SeriesInstanceUID", "") or "").strip()
    if not series_uid:
        log.info("C-FIND INSTANCE refused: SeriesInstanceUID is required")
        yield STATUS_IDENTIFIER_MISMATCH, None
        return
    study_uid = str(getattr(query, "StudyInstanceUID", "") or "").strip()
    filters = _query_filters(query, _INSTANCE_KEYS)
    stmt = query_service.instance_query(
        study_uid or None, series_uid, filters,
        tenant_id=tenant_id, include_all=False,
    )
    for instance in session.execute(stmt).scalars():
        ds = _new_find_response("INSTANCE")
        # Full context: patient + study + series rows above the instance.
        _fill_from(ds, instance.series.study.patient, _PATIENT_KEYS, requested)
        _fill_from(ds, instance.series.study, _STUDY_ROW_KEYS, requested)
        _fill_from(ds, instance.series, _SERIES_KEYS, requested)
        _fill_from(ds, instance, _INSTANCE_KEYS, requested)
        yield 0xFF00, ds


_FIND_LEVELS = {
    "PATIENT": _find_patients,
    "STUDY": _find_studies,
    "SERIES": _find_series,
    "INSTANCE": _find_instances,
}


def _dimse_was_cancelled(event: Any) -> bool:
    """Read pynetdicom's cancellation state without assuming every event has it."""
    return bool(getattr(event, "is_cancelled", False))


def _yield_cancellable_find(
    event: Any, results: Iterator[tuple[int, pydicom.Dataset | None]]
) -> Iterator[tuple[int, pydicom.Dataset | None]]:
    """Stream C-FIND responses and stop before asking for another row on cancel."""
    while True:
        if _dimse_was_cancelled(event):
            yield 0xFE00, None
            return
        try:
            result = next(results)
        except StopIteration:
            yield 0x0000, None
            return
        yield result
        if result[0] != 0xFF00:
            return


def handle_find(event: Any) -> Iterator[tuple[int, pydicom.Dataset | None]]:
    # B-8 defence in depth: an unlisted caller never reaches here through the
    # association gate, but a mid-flight allowlist change must still refuse.
    if not is_calling_ae_allowed(_calling_ae(event)):
        log.warning("C-FIND refused: calling AE is not allowlisted")
        yield STATUS_CALLING_AE_NOT_RECOGNIZED, None
        return

    # A single association port serves both Q/R and Worklist C-FIND — route on
    # the negotiated presentation context's abstract syntax.
    ctx = getattr(event, "context", None)
    if ctx is not None and str(ctx.abstract_syntax) == str(ModalityWorklistInformationFind):
        yield from _yield_cancellable_find(event, handle_worklist_find(event))
        return

    query = event.identifier
    if query is None:
        yield 0xC212, None
        return
    level = str(getattr(query, "QueryRetrieveLevel", "STUDY")).upper()
    requested = {kw for kw in query.dir() if kw != "QueryRetrieveLevel"}

    log.info("C-FIND level=%s match_keys=%s", level, sorted(requested))

    with SyncSessionLocal() as session:
        # VNA: a mapped calling AE only ever sees its own tenant's studies;
        # unmapped AEs keep the legacy unscoped scope.
        tenant_id = tenant_service.resolve_tenant_id_for_ae_sync(
            session, _calling_ae(event)
        )
        handler = _FIND_LEVELS.get(level)
        if handler is None:
            yield STATUS_IDENTIFIER_MISMATCH, None
            return
        yield from _yield_cancellable_find(
            event, handler(session, query, requested, tenant_id)
        )


# --------------------------------------------------------------------------- #
# MPPS (Modality Performed Procedure Step) — N-CREATE / N-SET
# --------------------------------------------------------------------------- #
def _decode_attribute_list(event) -> pydicom.Dataset | None:
    """Decode an N-CREATE/N-SET Attribute List into a pydicom Dataset.

    pynetdicom hands the raw encoded dataset (BytesIO) to the handler; the
    transfer syntax from the accepted presentation context tells us how to
    decode it. N-CREATE/N-SET carry it as `AttributeList`, N-ACTION as
    `ActionInformation` — both resolve to the same raw buffer.
    """
    raw = getattr(event.request, "AttributeList", None)
    if raw is None:
        raw = getattr(event.request, "ActionInformation", None)
    if raw is None:
        return None
    ts = event.context.transfer_syntax
    from pydicom.uid import (
        DeflatedExplicitVRLittleEndian,
        ExplicitVRBigEndian,
        ImplicitVRLittleEndian,
    )
    from pynetdicom.dsutils import decode

    is_implicit = str(ts) == str(ImplicitVRLittleEndian)
    is_little = str(ts) != str(ExplicitVRBigEndian)
    deflated = str(ts) == str(DeflatedExplicitVRLittleEndian)
    try:
        return decode(raw, is_implicit, is_little, deflated)
    except Exception as exc:
        log.error("mpps_attribute_list_decode_failed error_type=%s", type(exc).__name__)
        return None


def handle_n_create(event):
    """N-CREATE SCP for the Modality Performed Procedure Step SOP Class.

    The modality reports that an exam started (status IN PROGRESS). We record
    the performed step and flip the linked worklist step to IN PROGRESS — the
    scanner is the source of truth, no C-STORE inference needed.

    Protocol: return a single (status, dataset) tuple; 0x0000 on success.
    """
    try:
        req = event.request
        ds = _decode_attribute_list(event)
        sop_uid = req.AffectedSOPInstanceUID
        if ds is None:
            # Don't report success for a request we couldn't even parse.
            log.error("MPPS N-CREATE %s: unreadable Attribute List", sop_uid)
            return 0x0110, None  # Processing failure
        with SyncSessionLocal() as session:
            tenant_id = tenant_service.resolve_tenant_id_for_ae_sync(
                session, _calling_ae(event)
            )
            mpps_service.create_step(session, sop_uid, ds, tenant_id)
        log.info("MPPS N-CREATE %s (%s)", sop_uid, str(getattr(ds, "PerformedProcedureStepStatus", "")))
        return 0x0000, None
    except Exception as exc:
        log.error("mpps_n_create_failed error_type=%s", type(exc).__name__)
        return 0x0110, None  # Processing failure


def handle_n_set(event):
    """N-SET SCP for MPPS — the modality reports exam end.

    COMPLETED closes the worklist loop (scheduled step COMPLETED + study
    linked); DISCONTINUED releases the step back to SCHEDULED. Unknown SOP
    Instance UID replies 0x0112 (No such object instance).
    """
    try:
        req = event.request
        ds = _decode_attribute_list(event)
        sop_uid = req.RequestedSOPInstanceUID
        if ds is None:
            log.error("MPPS N-SET %s: unreadable Attribute List", sop_uid)
            return 0x0110, None
        with SyncSessionLocal() as session:
            tenant_id = tenant_service.resolve_tenant_id_for_ae_sync(
                session, _calling_ae(event)
            )
            ok = mpps_service.update_step(session, sop_uid, ds, tenant_id)
        if not ok:
            log.warning("MPPS N-SET for unknown instance %s", sop_uid)
            return 0x0112, None
        log.info("MPPS N-SET %s -> %s", sop_uid, str(getattr(ds, "PerformedProcedureStepStatus", "")))
        return 0x0000, None
    except Exception as exc:
        log.error("mpps_n_set_failed error_type=%s", type(exc).__name__)
        return 0x0110, None


# --------------------------------------------------------------------------- #
# Storage Commitment (Push Model) — N-ACTION
# --------------------------------------------------------------------------- #
def handle_n_action(event):
    """N-ACTION SCP for the Storage Commitment Push Model SOP Class.

    The modality asks "will you guarantee these instances are archived?".
    We acknowledge the request immediately (0x0000) and deliver the verdict
    asynchronously as an N-EVENT-REPORT (EventTypeID=1) on this association —
    immediately when every reference is already verifiable, or automatically
    once the last referenced instance lands via C-STORE (see
    storage_commit_service.on_instance_stored).
    """
    try:
        ds = _decode_attribute_list(event)
        if ds is None:
            log.error("Storage Commitment N-ACTION: unreadable Attribute List")
            return 0x0110, None  # Processing failure
        tx_uid = str(getattr(ds, "TransactionUID", "")).strip()
        if not tx_uid:
            log.error("Storage Commitment N-ACTION without a Transaction UID")
            return 0x0110, None
        ae_title = getattr(getattr(event.assoc, "requestor", None), "ae_title", None)
        # Register the association *before* handle_action can queue the report,
        # so the sender always knows which association to ride.
        from app.dicom.commit_sender import register_association

        if not register_association(tx_uid, event.assoc):
            return 0xA700, None  # Out of Resources; no DB request was created.
        try:
            with SyncSessionLocal() as session:
                tenant_id = tenant_service.resolve_tenant_id_for_ae_sync(
                    session, ae_title
                )
                results = storage_commit_service.handle_action(
                    session, tx_uid, ae_title, ds, tenant_id
                )
        except Exception:
            # The request never reached the queue: release the association record
            # now, or it would outlive the transaction as an orphan (C-2).
            from app.dicom.commit_sender import _cleanup_bookkeeping

            _cleanup_bookkeeping(tx_uid)
            raise
        log.info(
            "Storage Commitment N-ACTION %s from AE '%s' (%d instances, %d verified)",
            tx_uid, ae_title, len(results), sum(1 for r in results if r.get("ok")),
        )
        return 0x0000, None
    except Exception as exc:
        log.error("storage_commit_n_action_failed error_type=%s", type(exc).__name__)
        return 0x0110, None  # Processing failure


# --------------------------------------------------------------------------- #
# C-MOVE
# --------------------------------------------------------------------------- #
_MAX_QR_SUBOPERATIONS = 65535
_QR_QUERY_BATCH_SIZE = 256


def _matching_instance_statement(
    query: pydicom.dataset.Dataset, level: str, tenant_id: int | None
) -> Select:
    """Build a bounded-scope retrieval query after validating its unique keys."""
    normalized_level = level.upper()
    required_keys = {
        "STUDY": ("StudyInstanceUID",),
        "SERIES": ("StudyInstanceUID", "SeriesInstanceUID"),
        "INSTANCE": ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"),
        "IMAGE": ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"),
    }.get(normalized_level)
    if required_keys is None:
        raise ValueError("Unsupported QueryRetrieveLevel for retrieval")

    values: list[str] = []
    for key in required_keys:
        value = str(query.get(key, "")).strip()
        if not value:
            raise ValueError(f"C-MOVE/C-GET identifier requires {key}")
        values.append(value)

    study_uid = values[0]
    if normalized_level == "STUDY":
        return query_service.instance_query(
            study_uid, None, {}, tenant_id=tenant_id, include_all=False
        )
    series_uid = values[1]
    if normalized_level == "SERIES":
        return query_service.instance_query(
            study_uid, series_uid, {}, tenant_id=tenant_id, include_all=False
        )
    if normalized_level in {"INSTANCE", "IMAGE"}:
        return query_service.instance_query(
            study_uid, series_uid, {"SOPInstanceUID": values[2]},
            tenant_id=tenant_id, include_all=False,
        )
    raise ValueError("Unsupported QueryRetrieveLevel for retrieval")


def _iter_matching_instance_keys(query, level: str, tenant_id: int | None,
                                 check_exists: bool = True) -> Iterator[tuple[str, str | None]]:
    """Yield matching object references in bounded DB batches, preserving VNA scope."""
    with SyncSessionLocal() as session:
        stmt = _matching_instance_statement(query, level, tenant_id)
        rows = session.execute(stmt.execution_options(yield_per=_QR_QUERY_BATCH_SIZE)).scalars()
        for instance in rows:
            if check_exists and not storage_service.exists(
                instance.file_path, backend_name=instance.storage_backend
            ):
                continue
            yield instance.file_path, instance.storage_backend


def _matching_instance_keys(query, level: str,
                            tenant_id: int | None) -> list[tuple[str, str | None]]:
    """Snapshot at most the DICOM US sub-operation limit of matching keys."""
    keys: list[tuple[str, str | None]] = []
    for _key in _iter_matching_instance_keys(query, level, tenant_id):
        keys.append(_key)
        if len(keys) > _MAX_QR_SUBOPERATIONS:
            break
    return keys


def handle_move(event):
    """C-MOVE SCP: resolve the Move Destination AE title from the node
    registry, then hand matching instances to pynetdicom, which performs the
    C-STORE sub-operations to the destination automatically.

    Yield protocol (pynetdicom service_class._move_scp):
      1. (host, port) of the destination AE
      2. int — number of C-STORE sub-operations
      3. (0xFF00, dataset) for each instance to send

    pynetdicom auto-sends the final success/warning response once the
    generator is exhausted, so no trailing yield is required.
    """
    query = event.identifier
    if query is None:
        # First yield must be the destination tuple — (None, None) makes
        # pynetdicom reply 0xA801 (Refused: Move destination unknown).
        yield (None, None)
        return

    dest_aet = event.move_destination
    if not dest_aet:
        log.warning("C-MOVE: request carried no Move Destination AE title")
        yield (None, None)  # -> 0xA801 Refused: Move destination unknown
        return

    node = get_node_sync(dest_aet)
    if node is None:
        log.warning("C-MOVE: unknown destination AE '%s'", dest_aet)
        yield (None, None)  # -> 0xA801 Refused: Move destination unknown
        return

    level = str(getattr(query, "QueryRetrieveLevel", "STUDY")).upper()
    log.info("C-MOVE %s -> %s (%s:%s)", level, dest_aet, node.host, node.port)

    # VNA: the querying AE only moves instances inside its own tenant scope.
    with SyncSessionLocal() as scope_session:
        tenant_id = tenant_service.resolve_tenant_id_for_ae_sync(
            scope_session, _calling_ae(event)
        )

    # Snapshot a protocol-bounded list of references, then decode one instance
    # at a time. This preserves the exact sub-operation count without retaining
    # the full DICOM datasets for the whole study.
    keys = _matching_instance_keys(query, level, tenant_id)
    count = len(keys)
    if count > _MAX_QR_SUBOPERATIONS:
        yield (node.host, node.port)
        yield count  # pynetdicom returns C516 for counts beyond the US range.
        return
    if count == 0:
        log.info("C-MOVE %s: no readable instances to move", dest_aet)
        yield (node.host, node.port)
        yield 0
        return

    yield (node.host, node.port)
    yield count
    for key, backend in keys:
        if _dimse_was_cancelled(event):
            yield 0xFE00, None
            return
        try:
            ds = storage_service.read_dataset(key, backend_name=backend)
        except Exception as exc:
            # Pynetdicom converts a handler exception to a failed C-MOVE; this
            # conservative abort avoids reporting a successful incomplete move.
            log.error("C-MOVE instance read failed (error_type=%s)", type(exc).__name__)
            raise RuntimeError("C-MOVE could not read a matched instance") from None
        yield 0xFF00, ds


# --------------------------------------------------------------------------- #
# C-GET
# --------------------------------------------------------------------------- #
def handle_get(event):
    """C-GET SCP: send matching instances back to the requesting SCU over this
    association.

    Yield protocol (pynetdicom service_class._get_scp):
      1. int — number of C-STORE sub-operations (0 -> pynetdicom replies
         success with zero counts)
      2. (0xFF00, dataset) for each instance to send

    pynetdicom auto-sends the final success/warning response once the
    generator is exhausted, so no trailing yield is required.
    """
    query = event.identifier
    if query is None:
        yield 0  # no matches -> success with zero sub-operations
        return
    level = str(getattr(query, "QueryRetrieveLevel", "STUDY")).upper()
    log.info("C-GET %s", level)

    # VNA: scope retrieval to the calling AE's tenant (unmapped = legacy).
    with SyncSessionLocal() as scope_session:
        tenant_id = tenant_service.resolve_tenant_id_for_ae_sync(
            scope_session, _calling_ae(event)
        )

    keys = _matching_instance_keys(query, level, tenant_id)
    count = len(keys)
    if count > _MAX_QR_SUBOPERATIONS:
        yield count
        return
    yield count
    for key, backend in keys:
        if _dimse_was_cancelled(event):
            yield 0xFE00, None
            return
        try:
            ds = storage_service.read_dataset(key, backend_name=backend)
            yield 0xFF00, ds
        except Exception as exc:
            # Storage keys and decoder errors can contain PHI; expose type only.
            log.error("C-GET instance read failed (error_type=%s)", type(exc).__name__)
            yield 0xA701, None


# --------------------------------------------------------------------------- #
# Server lifecycle
# --------------------------------------------------------------------------- #
def _dimse_status_code(value: Any) -> int | None:
    """Extract a DIMSE status from a handler result without inspecting payloads."""
    if isinstance(value, tuple) and value:
        return _dimse_status_code(value[0])
    if isinstance(value, int):
        return value
    status = getattr(value, "Status", None)
    return int(status) if isinstance(status, int) else None


def _dimse_status_class(status: int | None) -> str:
    """Map DICOM status codes to bounded metric outcomes."""
    if status == 0x0000:
        return "success"
    if status is not None and status & 0xF000 == 0xB000:
        return "warning"
    return "failure"


def _instrument_dimse_handler(
    operation: str, handler: Callable[..., Any]
) -> Callable[..., Any]:
    """Measure one receiver handler, including generator-backed sub-operations."""
    @wraps(handler)
    def observed(event: Any) -> Any:
        started = time.perf_counter()
        try:
            result = handler(event)
        except BaseException:
            record_dimse_operation(operation, "failure", time.perf_counter() - started)
            raise
        if not inspect.isgenerator(result):
            record_dimse_operation(
                operation, _dimse_status_class(_dimse_status_code(result)),
                time.perf_counter() - started,
            )
            return result

        def observed_results() -> Iterator[Any]:
            final_status: int | None = None
            try:
                for item in result:
                    item_status = _dimse_status_code(item)
                    if item_status is not None:
                        final_status = item_status
                    yield item
            except BaseException:
                record_dimse_operation(
                    operation, "failure", time.perf_counter() - started
                )
                raise
            else:
                record_dimse_operation(
                    operation, _dimse_status_class(final_status),
                    time.perf_counter() - started,
                )
        return observed_results()
    return observed


def start_receiver(host: str | None = None, port: int | None = None,
                   ae_title: str | None = None,
                   tls_context: ssl.SSLContext | None = None) -> AE:
    """Start the SCP server (non-blocking). Returns the AE handle.

    Pass `tls_context` to serve DICOM over TLS (AES over TLS 1.2/1.3). The
    production bootstrap (`start_receiver_in_thread`) builds it from the
    DICOM_TLS_* settings; tests inject their own generated context.
    """
    global _ae
    if _ae is not None:
        return _ae

    ae = AE(ae_title=ae_title or settings.DICOM_AE_TITLE)
    ae.maximum_associations = settings.DICOM_MAX_ASSOCIATIONS
    ae.maximum_pdu_size = int(settings.DICOM_MAX_PDU_SIZE)
    ae.network_timeout = int(settings.DICOM_NETWORK_TIMEOUT_SECONDS)
    ae.acse_timeout = int(settings.DICOM_ACSE_TIMEOUT_SECONDS)
    ae.dimse_timeout = int(settings.DICOM_DIMSE_TIMEOUT_SECONDS)
    # Storage SOP classes, declared explicitly with the SCP role so the C-GET
    # sub-operations (which push instances back over the requesting
    # association) have a role-carrying context to send through — an
    # auto/role-less supported context cannot be used for the SCU side of a
    # C-STORE sub-operation.
    for cx in StoragePresentationContexts:
        ae.add_supported_context(
            cx.abstract_syntax, list(cx.transfer_syntax),
            scp_role=True, scu_role=False,
        )
    # Query/Retrieve service classes (C-FIND / C-GET / C-MOVE) + C-ECHO.
    # H-04: Implicit VR LE is the PS3.5 §10.1 default every AE must accept;
    # older CR/US devices and RIS gateways propose nothing else.
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelFind, NON_STORAGE_TRANSFER_SYNTAXES)
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelGet, NON_STORAGE_TRANSFER_SYNTAXES)
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelMove, NON_STORAGE_TRANSFER_SYNTAXES)
    ae.add_supported_context(ModalityWorklistInformationFind, NON_STORAGE_TRANSFER_SYNTAXES)
    ae.add_supported_context(ModalityPerformedProcedureStep, NON_STORAGE_TRANSFER_SYNTAXES)
    # Storage Commitment Push Model: the SCP also acts as SCU here to send the
    # asynchronous N-EVENT-REPORT on the accepted association (role selection
    # negotiation requests both roles for this context).
    ae.add_supported_context(
        StorageCommitmentPushModel, NON_STORAGE_TRANSFER_SYNTAXES,
        scu_role=True, scp_role=True,
    )
    ae.add_supported_context(Verification, NON_STORAGE_TRANSFER_SYNTAXES)
    # The destination association for C-MOVE sub-operations is created from
    # this same AE, so it must also *request* storage contexts as an SCU.
    ae.requested_contexts = StoragePresentationContexts

    handlers = [
        # B-8: dispatched before ACSE negotiation, so an unlisted calling AE is
        # turned away with an A-ASSOCIATE-RJ (diagnostic 0x03) before it can
        # send a single DIMSE message.
        (evt.EVT_REQUESTED, handle_association_requested),
        (evt.EVT_RELEASED, _track_association_end),
        (evt.EVT_ABORTED, _track_association_end),
        (evt.EVT_CONN_CLOSE, _track_association_end),
        (evt.EVT_C_STORE, _instrument_dimse_handler("c_store", handle_store)),
        (evt.EVT_C_FIND, _instrument_dimse_handler("c_find", handle_find)),
        (evt.EVT_C_GET, _instrument_dimse_handler("c_get", handle_get)),
        (evt.EVT_C_MOVE, _instrument_dimse_handler("c_move", handle_move)),
        (evt.EVT_N_CREATE, _instrument_dimse_handler("n_create", handle_n_create)),
        (evt.EVT_N_SET, _instrument_dimse_handler("n_set", handle_n_set)),
        (evt.EVT_N_ACTION, _instrument_dimse_handler("n_action", handle_n_action)),
    ]
    ae.start_server(
        (host or settings.DICOM_HOST, port or settings.DICOM_PORT),
        block=False,
        evt_handlers=handlers,
        ssl_context=tls_context,
    )
    _ae = ae
    log.info(
        "DICOM SCP '%s' listening on %s:%s (%s — C-STORE/C-FIND incl. MWL/C-GET/C-MOVE/MPPS/Storage-Commit/C-ECHO)",
        ae.ae_title, host or settings.DICOM_HOST, port or settings.DICOM_PORT,
        "TLS enabled" if tls_context else "plaintext",
    )
    return ae


def shutdown_receiver() -> None:
    global _ae
    if _ae is not None:
        try:
            _ae.shutdown()
        except Exception as exc:  # Shutdown is best-effort and may expose peer details.
            log.debug("dicom_receiver_shutdown_failed error_type=%s", type(exc).__name__)
        _ae = None


def start_receiver_in_thread() -> None:
    """Start the receiver without blocking app startup (catches port errors)."""
    import threading

    def _run() -> None:
        try:
            tls_context = None
            if settings.DICOM_TLS_ENABLED:
                from app.dicom.tls import ensure_cert_paths_exist, server_context

                ensure_cert_paths_exist()  # surface cert errors at boot, not on first modality
                tls_context = server_context()
            start_receiver(tls_context=tls_context)
        except Exception as exc:  # Startup boundary: report failure without traceback details.
            log.error(
                "DICOM receiver failed to start on %s:%s error_type=%s",
                settings.DICOM_HOST,
                settings.DICOM_PORT,
                type(exc).__name__,
            )

    threading.Thread(target=_run, name="paxs-dicom-scp", daemon=True).start()
