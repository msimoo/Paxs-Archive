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
import logging
import ssl

import pydicom
from pynetdicom import AE, StoragePresentationContexts, evt
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom.sop_class import (
    ModalityPerformedProcedureStep,
    ModalityWorklistInformationFind,
    StorageCommitmentPushModel,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelGet,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)
from sqlalchemy import select

from app.core.config import settings
from app.db.models import Patient
from app.db.session import AsyncSessionLocal, SyncSessionLocal
from app.dicom.parser import parse_metadata
from app.services import (
    mpps_service,
    query_service,
    storage_commit_service,
    storage_service,
    tenant_service,
    worklist_service,
)
from app.services.indexing_service import CrossTenantIngestError, index_new_instance
from app.services.quota_service import QuotaExceeded
from app.services.node_service import get_node_sync

log = logging.getLogger("paxs.dicom")

_ae: AE | None = None

# --------------------------------------------------------------------------- #
# C-STORE
# --------------------------------------------------------------------------- #
async def _resolve_tenant(source_ae: str | None) -> int | None:
    """Tenant the calling AE maps to, resolved before any write (H08)."""
    async with AsyncSessionLocal() as session:
        return await tenant_service.resolve_tenant_id_for_ae(session, source_ae)


def _calling_ae(event) -> str | None:
    return getattr(getattr(event.assoc, "requestor", None), "ae_title", None)


async def _index_async(meta: dict, path: str, sha256: str, source_ae: str | None = None,
                       backend: str | None = None, read_path: str | None = None,
                       ds=None, file_size_bytes: int | None = None) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # VNA: instances stored from a tenant-mapped AE title belong to it.
            tenant_id = await tenant_service.resolve_tenant_id_for_ae(session, source_ae)
            # Multi-storage: keep late-arriving slices on the study's backend
            # (already resolved by the caller for the save).
            await index_new_instance(
                session, meta, path, sha256, performed_by="dicom-receiver",
                tenant_id=tenant_id, storage_backend=backend,
                read_path=read_path,
                ds=ds,
                file_size_bytes=file_size_bytes,
            )


def handle_store(event) -> int:
    ds = event.dataset
    if ds is None:
        return 0xC212  # Processing failure
    path: str | None = None
    staged = None
    backend: str | None = None
    try:
        ds.file_meta = event.file_meta
        # C05: validate identifiers before they reach the storage key builder —
        # a malformed UID must not create an object outside the storage root.
        for _attr in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"):
            _value = getattr(ds, _attr, None)
            if _value not in (None, ""):
                storage_service.validate_component(str(_value), field=_attr)
        # Multi-storage: write new studies to the registered default backend
        # (None = env backend); late slices rejoin the study's recorded one.
        backend = storage_service.resolve_study_backend(
            str(getattr(ds, "StudyInstanceUID", "UNKNOWN"))
        )
        source_ae = _calling_ae(event)
        # H08: the quota is scoped to the tenant the AE maps to, so it must be
        # resolved *before* the object is staged (nothing is written first).
        tenant_id = asyncio.run(_resolve_tenant(source_ae))
        staged = storage_service.stage_dataset_with_quota(
            ds, tenant_id, backend_name=backend
        )
        meta = parse_metadata(ds)
        try:
            asyncio.run(_index_async(
                meta, staged.key, staged.sha256, source_ae=source_ae, backend=backend,
                read_path=staged.staged_path or staged.key,
                ds=ds,
                file_size_bytes=staged.bytes_written,
            ))
        except Exception:
            # H09: the row never became durable — drop the staged file and give
            # the reservation back. A pre-existing object is never touched.
            staged.discard()
            raise
        # Indexing committed: publish the object.
        staged.promote()
        path = staged.key
        sha256 = staged.sha256
        # Storage Commitment: if a modality asked (N-ACTION) before its images
        # were fully stored, each arriving instance re-verifies the pending
        # requests — the N-EVENT-REPORT fires automatically once the last
        # referenced instance lands (no manual re-request).
        try:
            storage_commit_service.on_instance_stored(
                str(getattr(ds, "SOPInstanceUID", ""))
            )
        except Exception:
            log.exception("storage-commit hook failed (store already succeeded)")
        return 0x0000  # Success
    except QuotaExceeded as exc:
        # H08: Refused — Out of Resources (PS3.4). Nothing was written.
        log.warning("C-STORE refused by quota: %s", exc)
        return 0xA700
    except storage_service.InvalidStorageKey as exc:
        # C05: malformed identifier -> 0xC000 "Cannot understand" (PS3.4).
        # Nothing was written (validation precedes the save), so no cleanup.
        log.warning("C-STORE rejected, invalid identifier: %s", exc)
        return 0xC000
    except CrossTenantIngestError as exc:
        # C04: refused a cross-tenant append; the object written above is removed
        # by the generic handler's cleanup, so re-raise it as-is.
        log.warning("C-STORE rejected, cross-tenant ingest: %s", exc)
        raise
    except Exception:
        log.exception("C-STORE handler failed")
        # H09: compensation must never delete a pre-existing object. Only the
        # staged file this call created is removed (a failed reimport used to
        # delete the *existing* instance via this path).
        if staged is not None:
            try:
                staged.discard()
            except Exception:
                log.warning("Could not discard staged object", exc_info=True)
        return 0xC212  # Processing failure


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
            except Exception:
                pass


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
    log.info("MWL C-FIND %s", filters or {})

    with SyncSessionLocal() as session:
        for step, patient in session.execute(
            worklist_service.build_statement(filters)
        ).all():
            ds = worklist_service.serialize_sps(step, patient, requested)
            yield 0xFF00, ds

    yield 0x0000, None


def handle_find(event):
    # A single association port serves both Q/R and Worklist C-FIND — route on
    # the negotiated presentation context's abstract syntax.
    ctx = getattr(event, "context", None)
    if ctx is not None and str(ctx.abstract_syntax) == str(ModalityWorklistInformationFind):
        yield from handle_worklist_find(event)
        return

    query = event.identifier
    if query is None:
        yield 0xC212, None
        return
    level = str(getattr(query, "QueryRetrieveLevel", "STUDY")).upper()
    requested = {kw for kw in query.dir() if kw != "QueryRetrieveLevel"}

    log.info("C-FIND %s %s", level, {k: str(getattr(query, k)) for k in requested})

    with SyncSessionLocal() as session:
        # VNA: a mapped calling AE only ever sees its own tenant's studies;
        # unmapped AEs keep the legacy unscoped scope.
        tenant_id = tenant_service.resolve_tenant_id_for_ae_sync(
            session, _calling_ae(event)
        )
        if level == "PATIENT":
            stmt = select(Patient)
            for kw, attr in _PATIENT_KEYS.items():
                if kw in query and str(getattr(query, kw)).strip():
                    value = str(getattr(query, kw))
                    col = getattr(Patient, attr)
                    stmt = stmt.where(
                        col.like(value.replace("*", "%")) if "*" in value else col == value
                    )
            # Only patients that own at least one study inside the tenant scope.
            from app.db.models import Study

            sub = select(Study.patient_id)
            sub = query_service.apply_study_tenant_scope(
                sub, tenant_id, include_all=False
            )
            stmt = stmt.where(Patient.id.in_(sub))
            for patient in session.execute(stmt).scalars().all():
                ds = pydicom.Dataset()
                ds.QueryRetrieveLevel = "PATIENT"
                _fill_from(ds, patient, _PATIENT_KEYS, requested)
                yield 0xFF00, ds

        elif level == "STUDY":
            filters = _query_filters(query, _STUDY_KEYS)
            stmt = query_service.study_query(
                filters, tenant_id=tenant_id, include_all=False
            )
            for study in session.execute(stmt).scalars().unique().all():
                ds = pydicom.Dataset()
                ds.QueryRetrieveLevel = "STUDY"
                _fill_from(ds, study.patient, _PATIENT_KEYS, requested)
                _fill_from(ds, study, _STUDY_ROW_KEYS, requested)
                yield 0xFF00, ds

        elif level == "SERIES":
            filters = _query_filters(query, _SERIES_KEYS)
            stmt = query_service.series_query(
                None, filters, tenant_id=tenant_id, include_all=False
            )
            for series in session.execute(stmt).scalars().all():
                ds = pydicom.Dataset()
                ds.QueryRetrieveLevel = "SERIES"
                # Full context: patient + study rows above the series.
                _fill_from(ds, series.study.patient, _PATIENT_KEYS, requested)
                _fill_from(ds, series.study, _STUDY_ROW_KEYS, requested)
                _fill_from(ds, series, _SERIES_KEYS, requested)
                yield 0xFF00, ds

        elif level == "INSTANCE":
            filters = _query_filters(query, _INSTANCE_KEYS)
            stmt = query_service.instance_query(
                None, None, filters, tenant_id=tenant_id, include_all=False
            )
            for instance in session.execute(stmt).scalars().all():
                ds = pydicom.Dataset()
                ds.QueryRetrieveLevel = "INSTANCE"
                # Full context: patient + study + series rows above the instance.
                _fill_from(ds, instance.series.study.patient, _PATIENT_KEYS, requested)
                _fill_from(ds, instance.series.study, _STUDY_ROW_KEYS, requested)
                _fill_from(ds, instance.series, _SERIES_KEYS, requested)
                _fill_from(ds, instance, _INSTANCE_KEYS, requested)
                yield 0xFF00, ds

    yield 0x0000, None


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
    from pynetdicom.dsutils import decode
    from pydicom.uid import (
        DeflatedExplicitVRLittleEndian,
        ExplicitVRBigEndian,
        ImplicitVRLittleEndian,
    )

    is_implicit = str(ts) == str(ImplicitVRLittleEndian)
    is_little = str(ts) != str(ExplicitVRBigEndian)
    deflated = str(ts) == str(DeflatedExplicitVRLittleEndian)
    try:
        return decode(raw, is_implicit, is_little, deflated)
    except Exception:
        log.exception("MPPS: failed to decode Attribute List")
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
            mpps_service.create_step(session, sop_uid, ds)
        log.info("MPPS N-CREATE %s (%s)", sop_uid, str(getattr(ds, "PerformedProcedureStepStatus", "")))
        return 0x0000, None
    except Exception:
        log.exception("MPPS N-CREATE failed")
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
            ok = mpps_service.update_step(session, sop_uid, ds)
        if not ok:
            log.warning("MPPS N-SET for unknown instance %s", sop_uid)
            return 0x0112, None
        log.info("MPPS N-SET %s -> %s", sop_uid, str(getattr(ds, "PerformedProcedureStepStatus", "")))
        return 0x0000, None
    except Exception:
        log.exception("MPPS N-SET failed")
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

        register_association(tx_uid, event.assoc)
        with SyncSessionLocal() as session:
            results = storage_commit_service.handle_action(session, tx_uid, ae_title, ds)
        log.info(
            "Storage Commitment N-ACTION %s from AE '%s' (%d instances, %d verified)",
            tx_uid, ae_title, len(results), sum(1 for r in results if r.get("ok")),
        )
        return 0x0000, None
    except Exception:
        log.exception("Storage Commitment N-ACTION failed")
        return 0x0110, None  # Processing failure


# --------------------------------------------------------------------------- #
# C-MOVE
# --------------------------------------------------------------------------- #
def _matching_instance_keys(query, level: str,
                            tenant_id: int | None = None) -> list[tuple[str, str | None]]:
    """Resolve ``(key, backend)`` pairs for the instance storage keys matching a
    Q/R query (shared by C-GET and C-MOVE; same semantics as
    query_service.instance_query). Keys work for every storage backend;
    ``backend`` is the named multi-storage backend that holds each object.

    `tenant_id` (VNA) restricts the match to one tenant's studies
    (None + include_all=False = legacy unscoped data)."""
    keys: list[tuple[str, str | None]] = []
    with SyncSessionLocal() as session:
        stmt = query_service.instance_query(
            None, None, {}, tenant_id=tenant_id, include_all=False
        )
        if level == "STUDY" and "StudyInstanceUID" in query:
            stmt = query_service.instance_query(
                str(query.StudyInstanceUID), None, {},
                tenant_id=tenant_id, include_all=False,
            )
        elif level == "SERIES" and "SeriesInstanceUID" in query:
            stmt = query_service.instance_query(
                None, str(query.SeriesInstanceUID), {},
                tenant_id=tenant_id, include_all=False,
            )
        elif level == "INSTANCE" and "SOPInstanceUID" in query:
            stmt = query_service.instance_query(
                None, None, {"SOPInstanceUID": str(query.SOPInstanceUID)},
                tenant_id=tenant_id, include_all=False,
            )
        for instance in session.execute(stmt).scalars().all():
            if storage_service.exists(instance.file_path, backend_name=instance.storage_backend):
                keys.append((instance.file_path, instance.storage_backend))
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

    # Read every matching object *before* declaring the sub-operation count, so
    # the number of (0xFF00, ds) yields exactly matches what pynetdicom tracks.
    datasets: list[pydicom.Dataset] = []
    for key, backend in _matching_instance_keys(query, level, tenant_id=tenant_id):
        try:
            datasets.append(storage_service.read_dataset(key, backend_name=backend))
        except Exception:
            log.exception("C-MOVE: read failed for %s", key)

    if not datasets:
        log.info("C-MOVE %s: no readable instances to move", dest_aet)
        yield (node.host, node.port)
        yield 0
        return

    yield (node.host, node.port)
    yield len(datasets)
    for ds in datasets:
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

    keys = _matching_instance_keys(query, level, tenant_id=tenant_id)
    yield len(keys)
    for key, backend in keys:
        try:
            ds = storage_service.read_dataset(key, backend_name=backend)
            yield 0xFF00, ds
        except Exception:
            log.exception("C-GET read failed for %s", key)
            yield 0xA701, None


# --------------------------------------------------------------------------- #
# Server lifecycle
# --------------------------------------------------------------------------- #
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
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelFind, ExplicitVRLittleEndian)
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelGet, ExplicitVRLittleEndian)
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelMove, ExplicitVRLittleEndian)
    ae.add_supported_context(ModalityWorklistInformationFind, ExplicitVRLittleEndian)
    ae.add_supported_context(ModalityPerformedProcedureStep, ExplicitVRLittleEndian)
    # Storage Commitment Push Model: the SCP also acts as SCU here to send the
    # asynchronous N-EVENT-REPORT on the accepted association (role selection
    # negotiation requests both roles for this context).
    ae.add_supported_context(
        StorageCommitmentPushModel, ExplicitVRLittleEndian,
        scu_role=True, scp_role=True,
    )
    ae.add_supported_context(Verification, ExplicitVRLittleEndian)
    # The destination association for C-MOVE sub-operations is created from
    # this same AE, so it must also *request* storage contexts as an SCU.
    ae.requested_contexts = StoragePresentationContexts

    handlers = [
        (evt.EVT_C_STORE, handle_store),
        (evt.EVT_C_FIND, handle_find),
        (evt.EVT_C_GET, handle_get),
        (evt.EVT_C_MOVE, handle_move),
        (evt.EVT_N_CREATE, handle_n_create),
        (evt.EVT_N_SET, handle_n_set),
        (evt.EVT_N_ACTION, handle_n_action),
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
        except Exception:
            log.debug("receiver shutdown", exc_info=True)
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
        except Exception:
            log.exception("DICOM receiver failed to start on %s:%s",
                          settings.DICOM_HOST, settings.DICOM_PORT)

    threading.Thread(target=_run, name="paxs-dicom-scp", daemon=True).start()
