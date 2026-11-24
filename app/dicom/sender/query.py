"""DICOM query/retrieve SCU: C-FIND, C-MOVE and C-GET.

These are the association-level Q/R operations. ``query_remote_node_sync`` adds
the archive-side half (resolve a registered ``dicom_nodes`` row) but stops
there: ingesting the instances a C-GET returns is
:mod:`app.dicom.sender.cget`'s job, because that path touches storage, indexing
and the audit trail.

Why the seam indirection (:func:`_facade`): the SCU helpers used to live in one
``app.dicom.sender`` module and the test suite patches its attributes
(``sender.AE``, ``sender._tls_args``, ``sender.send_find``). Resolving those
through the facade at call time keeps that seam intact across the split.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
from app.dicom.sender.association import association_failure_reason
from pynetdicom.sop_class import (
    ModalityWorklistInformationFind,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelGet,
    StudyRootQueryRetrieveInformationModelMove,
)

log = logging.getLogger("paxs.dicom.sender")

NON_STORAGE_TRANSFER_SYNTAXES = [ImplicitVRLittleEndian, ExplicitVRLittleEndian]

_FACADE = "app.dicom.sender"


def _facade() -> ModuleType:
    """The public ``app.dicom.sender`` namespace, resolved on every call."""
    return sys.modules[_FACADE]


def send_find(
    host: str, port: int, ae_title: str,
    query: Dataset | None = None,
    query_model=StudyRootQueryRetrieveInformationModelFind,
    our_ae_title: str = "PAXS_SCU",
    trust_cert: str | Path | None = None,
    max_results: int | None = None,
) -> dict:
    """C-FIND SCU — query a remote archive and collect the matches.

    `query` carries QueryRetrieveLevel + match keys; empty values request that
    the SCP return the field (DICOM semantics). `query_model` selects the
    information model (Study Root by default; prefetching uses Patient Root).

    Returns ``{"ok", "status", "matches": [Dataset, ...], "reason"?}`` where
    ``status`` is the final C-FIND status (0x0000 = success). Association
    failure includes the peer's bounded rejection diagnostic.
    """
    ae = _facade().AE(ae_title=our_ae_title)
    from app.dicom.sender.scu import _configure_scu_ae
    _configure_scu_ae(ae)
    ae.add_requested_context(query_model, NON_STORAGE_TRANSFER_SYNTAXES)

    assoc = ae.associate(host, port, ae_title=ae_title,
                         tls_args=_facade()._tls_args(host, trust_cert))
    if not assoc.is_established:
        reason = association_failure_reason(assoc, ae_title, our_ae_title)
        assoc.release()
        return {"ok": False, "reason": reason}

    ds = query if query is not None else Dataset()
    if "QueryRetrieveLevel" not in ds:
        ds.QueryRetrieveLevel = "STUDY"

    result: dict = {"ok": False, "status": None, "matches": []}
    try:
        for status, identifier in assoc.send_c_find(ds, query_model=query_model):
            if status is None:
                continue
            code = int(status.Status)
            result["status"] = code
            if code in (0xFF00, 0xFF01) and identifier is not None:
                if max_results in (None, 0) or len(result["matches"]) < max_results:
                    result["matches"].append(identifier)
                else:
                    result["truncated"] = True
                    result["ok"] = True
                    # The extra pending response proves the peer has more
                    # matches than requested. Abort this association so a broad
                    # query cannot force us to drain an unbounded result stream.
                    assoc.abort()
                    break
            elif code == 0x0000:
                result["ok"] = True
            elif code not in (0xFF00, 0xFF01):
                result["reason"] = f"status 0x{code:04x}"
    except Exception as exc:
        # Protocol implementations can raise with dataset values in their text.
        log.error("C-FIND failed (error_type=%s)", type(exc).__name__)
        result["ok"] = False
        result["reason"] = f"C-FIND failed ({type(exc).__name__})"
    finally:
        assoc.release()
    return result


def query_remote_worklist(
    host: str,
    port: int,
    ae_title: str,
    filters: Mapping[str, object] | None = None,
    our_ae_title: str = "PAXS_SCU",
    trust_cert: str | Path | None = None,
    max_results: int | None = None,
) -> dict:
    """Query a remote Modality Worklist SCP and return raw matching datasets.

    ``DateRange`` accepts ``YYYYMMDD-YYYYMMDD`` or a two-item sequence. The
    result keeps pydicom ``Dataset`` objects intact so callers can choose their
    own serialization without a second parse or a large JSON copy.
    """
    query = Dataset()
    for keyword, value in (filters or {}).items():
        if keyword == "DateRange":
            query.ScheduledProcedureStepStartDate = _worklist_date_range(value)
            continue
        if keyword not in {
            "PatientName", "PatientID", "Modality", "ScheduledStationAETitle",
            "ScheduledProcedureStepStartDate", "ScheduledProcedureStepStartTime",
            "ScheduledPerformingPhysicianName", "AccessionNumber",
        }:
            raise ValueError(f"Unsupported MWL filter: {keyword}")
        setattr(query, keyword, str(value))

    ae = _facade().AE(ae_title=our_ae_title)
    from app.dicom.sender.scu import _configure_scu_ae

    _configure_scu_ae(ae)
    ae.add_requested_context(ModalityWorklistInformationFind, NON_STORAGE_TRANSFER_SYNTAXES)
    assoc = ae.associate(
        host, port, ae_title=ae_title,
        tls_args=_facade()._tls_args(host, trust_cert),
    )
    if not assoc.is_established:
        reason = association_failure_reason(assoc, ae_title, our_ae_title)
        assoc.release()
        return {"ok": False, "status": None, "matches": [], "reason": reason}

    result: dict = {"ok": False, "status": None, "matches": []}
    try:
        for status, identifier in assoc.send_c_find(
            query, query_model=ModalityWorklistInformationFind
        ):
            if status is None:
                continue
            code = int(status.Status)
            result["status"] = code
            if code in (0xFF00, 0xFF01) and identifier is not None:
                if max_results in (None, 0) or len(result["matches"]) < max_results:
                    result["matches"].append(identifier)
                else:
                    result["truncated"] = True
                    result["ok"] = True
                    assoc.abort()
                    break
            elif code == 0x0000:
                result["ok"] = True
            elif code not in (0xFF00, 0xFF01):
                result["reason"] = f"status 0x{code:04x}"
    except Exception as exc:
        log.error("MWL C-FIND failed (error_type=%s)", type(exc).__name__)
        result["ok"] = False
        result["reason"] = f"MWL C-FIND failed ({type(exc).__name__})"
    finally:
        assoc.release()
    return result


def _worklist_date_range(value: object) -> str:
    """Normalize a bounded MWL date range without accepting arbitrary tags."""
    if isinstance(value, str):
        date_range = value.strip()
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        date_range = f"{value[0]}-{value[1]}"
    elif isinstance(value, Mapping):
        date_range = f"{value.get('start', '')}-{value.get('end', '')}"
    else:
        raise ValueError("DateRange must be a string, two-item sequence, or mapping")
    if len(date_range) > 17 or date_range.count("-") > 1:
        raise ValueError("DateRange is invalid")
    return date_range


def _normalize_match(identifier: Dataset) -> dict:
    """Turn a C-FIND match dataset into a JSON-friendly {keyword: value} dict.

    PersonName and numeric values are stringified; multi-valued elements are
    joined with a backslash (DICOM multi-value convention)."""
    out: dict = {}
    for keyword in identifier.dir():
        if keyword in ("QueryRetrieveLevel", "SpecificCharacterSet"):
            continue
        value = getattr(identifier, keyword, None)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            out[keyword] = "\\".join(str(v) for v in value)
        else:
            out[keyword] = str(value)
    return out


# Standard return fields requested on every C-FIND so matches are useful in
# the dashboard without the caller having to ask for each field (empty value
# = "request this field be returned", per DICOM C-FIND semantics).
_RETURN_FIELDS = {
    "PATIENT": ["PatientName", "PatientID", "PatientBirthDate", "PatientSex"],
    "STUDY": [
        "PatientName", "PatientID", "PatientBirthDate", "PatientSex",
        "StudyInstanceUID", "StudyDate", "StudyTime", "AccessionNumber",
        "ModalitiesInStudy", "StudyDescription", "ReferringPhysicianName",
        "NumberOfStudyRelatedSeries", "NumberOfStudyRelatedInstances",
    ],
    "SERIES": [
        "PatientName", "PatientID", "PatientBirthDate", "PatientSex",
        "StudyInstanceUID", "StudyDate", "StudyTime", "AccessionNumber",
        "ModalitiesInStudy", "StudyDescription", "ReferringPhysicianName",
        "SeriesInstanceUID", "SeriesNumber", "Modality", "SeriesDescription",
        "SeriesDate", "SeriesTime", "NumberOfSeriesRelatedInstances",
    ],
    "INSTANCE": [
        "PatientName", "PatientID", "PatientBirthDate", "PatientSex",
        "StudyInstanceUID", "StudyDate", "StudyTime", "AccessionNumber",
        "ModalitiesInStudy", "StudyDescription", "ReferringPhysicianName",
        "SeriesInstanceUID", "SeriesNumber", "Modality", "SeriesDescription",
        "SeriesDate", "SeriesTime", "NumberOfSeriesRelatedInstances",
        "SOPInstanceUID", "SOPClassUID", "InstanceNumber",
    ],
}


def query_remote_node_sync(
    ae_title: str,
    level: str = "STUDY",
    filters: dict | None = None,
    our_ae_title: str = "PAXS_SCU",
    max_results: int | None = None,
) -> dict:
    """Resolve a registered ``dicom_nodes`` node and run a C-FIND against it.

    `filters` is a ``{DICOM keyword: value}`` dict — empty values request the
    field be returned, ``*`` wildcards work (the SCP translates them). The
    standard per-level return-field set (``_RETURN_FIELDS``) is always
    requested so results are useful in the dashboard. Returns
    ``{"ok", "status", "matches": [{keyword: value}, ...], "reason"?}``
    suitable for a REST response; node-level problems carry a ``reason``.
    """
    from sqlalchemy import select

    from app.db.models import DicomNode
    from app.db.session import SyncSessionLocal

    with SyncSessionLocal() as session:
        node = session.execute(
            select(DicomNode).where(DicomNode.ae_title == ae_title)
        ).scalar_one_or_none()
    if node is None:
        return {"ok": False, "reason": "Node not found", "matches": []}
    if not node.enabled:
        return {"ok": False, "reason": "Node is disabled", "matches": []}

    query = Dataset()
    qr_level = (level or "STUDY").upper()
    query.QueryRetrieveLevel = qr_level
    # Always request the standard return fields (empty value = return field).
    # pydicom coerces "" on numeric VRs (IS/DS/TM) to None — drop those so they
    # never become a stray `== None` filter on the SCP side.
    for keyword in _RETURN_FIELDS.get(qr_level, []):
        try:
            setattr(query, keyword, "")
            if getattr(query, keyword, None) is None:
                del query[keyword]
        except Exception as exc:
            # Optional return attributes may be unsupported by a peer/version;
            # omitting one must be visible, without logging its value.
            log.warning("Cannot set optional DICOM query return attribute (error_type=%s)",
                        type(exc).__name__)
    for keyword, value in (filters or {}).items():
        if value is None:
            continue
        try:
            setattr(query, keyword, str(value))
        except Exception as exc:
            log.error("Cannot set C-FIND match attribute (error_type=%s)", type(exc).__name__)
            raise ValueError("Invalid C-FIND match attribute") from None

    find_options = {"our_ae_title": our_ae_title,
                    "trust_cert": getattr(node, "trust_cert", None)}
    if max_results is not None:
        find_options["max_results"] = max_results
    from app.dicom import dcmtk

    if max_results and dcmtk.enabled_for(find_options["trust_cert"]):
        result = dcmtk.send_find(
            node.host, node.port, node.ae_title, query,
            our_ae_title=our_ae_title, max_results=max_results,
        )
    else:
        result = _facade().send_find(node.host, node.port, node.ae_title, query, **find_options)
    # D-1: normalize the matches on **every** branch, not just the success one.
    # `send_find` collects raw pydicom `Dataset` objects, and the early return
    # below used to hand them straight back to the caller — which is a REST
    # handler. FastAPI then failed to JSON-encode a `Dataset` and answered
    # HTTP 500 for exactly the case the operator needs to read: a C-FIND that
    # returned some matches and then failed (0xA700, or a protocol error).
    matches = [_normalize_match(m) for m in (result.get("matches") or [])]
    if not result.get("ok"):
        response = {
            "ok": False,
            "status": result.get("status"),
            "reason": result.get("reason") or "C-FIND failed",
            "ae_title": ae_title,
            "matches": matches,
        }
        if result.get("truncated"):
            response["truncated"] = True
        if result.get("engine"):
            response["engine"] = result["engine"]
        return response
    response = {
        "ok": True,
        "status": result["status"],
        "ae_title": ae_title,
        "matches": matches,
    }
    if result.get("truncated"):
        response["truncated"] = True
    if result.get("engine"):
        response["engine"] = result["engine"]
    return response


def send_move(host: str, port: int, ae_title: str, dest_ae_title: str,
              query: Dataset | None = None, our_ae_title: str = "PAXS_SCU",
              trust_cert: str | Path | None = None) -> dict:
    """Send a C-MOVE request to a remote archive: ask it to move matching
    instances to `dest_ae_title` (another AE, typically ourselves or a
    forwarding target). Returns a summary of the move response.

    `query` may carry QueryRetrieveLevel + match keys (e.g. StudyInstanceUID);
    defaults to a STUDY-level query matching nothing (so use a real query).
    """
    ae = _facade().AE(ae_title=our_ae_title)
    from app.dicom.sender.scu import _configure_scu_ae
    _configure_scu_ae(ae)
    ae.add_requested_context(
        StudyRootQueryRetrieveInformationModelMove, NON_STORAGE_TRANSFER_SYNTAXES
    )

    assoc = ae.associate(host, port, ae_title=ae_title,
                         tls_args=_facade()._tls_args(host, trust_cert))
    if not assoc.is_established:
        assoc.release()
        return {"ok": False, "reason": "association rejected"}

    ds = query if query is not None else Dataset()
    if "QueryRetrieveLevel" not in ds:
        ds.QueryRetrieveLevel = "STUDY"

    results = {"ok": False, "status": None, "completed": 0, "failed": 0, "remaining": 0}
    try:
        for status, _identifier in assoc.send_c_move(
            ds, move_aet=dest_ae_title, query_model=StudyRootQueryRetrieveInformationModelMove
        ):
            if status is None:
                continue
            code = int(status.Status)
            results["status"] = code
            if code == 0x0000:
                results["ok"] = True
            # D-1: every sub-operation counter is read with a default. Guarding
            # on one attribute (`hasattr(status, "NumberOfCompletedSuboperations")`)
            # and then touching the other two directly raised AttributeError as
            # soon as a peer answered with a status that carries only some of
            # them — which aborted the response loop and reported the whole
            # C-MOVE as failed even though instances had transferred.
            results["completed"] = getattr(status, "NumberOfCompletedSuboperations", 0) or 0
            results["failed"] = getattr(status, "NumberOfFailedSuboperations", 0) or 0
            results["remaining"] = getattr(status, "NumberOfRemainingSuboperations", 0) or 0
    except Exception as exc:
        # A protocol failure must invalidate any earlier success response.
        log.error("C-MOVE failed (error_type=%s)", type(exc).__name__)
        results["ok"] = False
        results["reason"] = f"C-MOVE failed ({type(exc).__name__})"
    finally:
        assoc.release()
    return results


def send_get(host: str, port: int, ae_title: str,
             query: Dataset | None = None, our_ae_title: str = "PAXS_SCU",
             on_received=None, trust_cert: str | Path | None = None) -> dict:
    """C-GET SCU — pull matching instances from a remote archive.

    Unlike C-MOVE, C-GET has the *remote* SCP push the instances back over
    this same association; no second AE is involved. `query` carries
    QueryRetrieveLevel + match keys (e.g. StudyInstanceUID).

    The payloads arrive as C-STORE sub-operations (the C-GET-RSP identifiers
    are empty), so they are collected in an EVT_C_STORE handler. Pass
    `on_received(dataset)` to stream each instance as it lands — useful for
    large pulls that must not buffer the whole study in RAM; otherwise the
    datasets are buffered and returned in ``received``.

    **A-13: a sub-operation is acknowledged only when it was actually stored.**
    The remote SCP treats ``0x0000`` as permission to purge its own copy, so
    answering Success for an instance we failed to ingest destroys the last
    remaining copy of that image. Any exception from ``on_received`` (or from
    buffering the dataset) therefore returns a DIMSE failure status — never
    ``0x0000`` — and is counted in ``failed``. The status is chosen by cause,
    mirroring this archive's C-STORE handler (`app/dicom/receiver.py`) because a
    C-GET sub-operation *is* a C-STORE:

    * out of resources (``OSError``/``IOError`` — disk full, unreadable target)
      → ``0xA700`` "Refused: Out of Resources" (PS3.4);
    * anything else (bad dataset, constraint violation, decoder error)
      → ``0xC000`` "Unable to process" (the ``0xC000``–``0xCFFF`` family).

    Returns ``{"ok", "status", "received": [Dataset, ...], "reason"?}``
    (``received`` is empty when `on_received` is used). ``ok`` is False when any
    sub-operation failed, and ``failed`` carries the local count. Association
    failure yields ``{"ok": False, "reason": ...}``.
    """
    from pynetdicom import StoragePresentationContexts, build_role, evt

    ae = _facade().AE(ae_title=our_ae_title)
    from app.dicom.sender.scu import _configure_scu_ae
    _configure_scu_ae(ae)
    # The C-GET SCU *receives* the instances as C-STORE sub-operations over this
    # association, so it must offer the storage SOP classes with the SCP role
    # and answer each incoming store itself — with Success only on success.
    ext_neg = []
    for cx in StoragePresentationContexts:
        # Request every transfer syntax advertised by pynetdicom's storage
        # context table. Requesting only the default uncompressed syntax makes
        # peers reject JPEG/JPEG-LS/JPEG2000 studies instead of transcoding or
        # selecting a syntax they already support.
        ae.add_requested_context(cx.abstract_syntax, list(cx.transfer_syntax))
        ext_neg.append(build_role(cx.abstract_syntax, scp_role=True))
    ae.add_requested_context(
        StudyRootQueryRetrieveInformationModelGet, NON_STORAGE_TRANSFER_SYNTAXES
    )

    received: list[Dataset] = []
    receive_failures = 0

    def _sub_operation_failure_status(exc: BaseException) -> int:
        """DIMSE status for a failed C-GET sub-operation, by cause (A-13).

        Never Success: the remote SCP purges its copy on ``0x0000``. Mirrors the
        codes `app/dicom/receiver.py` returns for the same causes, so a peer sees
        consistent semantics whether the instance arrived by C-STORE or C-GET.
        """
        # IOError is an alias of OSError on Python 3, so this covers both.
        if isinstance(exc, OSError):
            return 0xA700  # Refused: Out of Resources (PS3.4)
        return 0xC000  # Unable to process

    def _on_store(event) -> int:
        """EVT_C_STORE: store one C-GET sub-operation, or report the failure.

        The return value *is* the C-STORE response status the remote SCP counts
        into ``NumberOfFailedSuboperations`` — so a failure here is what stops it
        from purging an instance we do not hold.
        """
        nonlocal receive_failures
        ds = None
        try:
            ds = event.dataset
            if ds is None:
                raise ValueError("C-GET received no dataset")
            # Preserve the negotiated wire encoding before storage validation.
            # A bare event.dataset has no Part 10 transfer syntax metadata.
            file_meta = getattr(event, "file_meta", None)
            if file_meta is not None:
                ds.file_meta = file_meta
            if on_received is not None:
                on_received(ds)
            else:
                received.append(ds)
        except Exception as exc:
            # This wire boundary must translate arbitrary ingestion failures into
            # a DIMSE failure, never acknowledge an instance that was not stored.
            from app.core.dicom_diagnostics import decode_context

            receive_failures += 1
            status = _sub_operation_failure_status(exc)
            # PHI-free: `decode_context` emits only UIDs, modality and pixel
            # layout — never demographics, paths, or the exception's own text
            # (a codec/DB error can echo stored values).
            log.error(
                "C-GET sub-operation failed (status=0x%04X, error_type=%s) | %s",
                status,
                type(exc).__name__,
                decode_context(ds) if ds is not None else "dataset_unreadable",
            )
            return status
        return 0x0000

    assoc = ae.associate(
        host, port, ae_title=ae_title,
        ext_neg=ext_neg, evt_handlers=[(evt.EVT_C_STORE, _on_store)],
        tls_args=(_facade()._tls_args(host, trust_cert) if trust_cert is not None
                  else _facade()._tls_args(host)),
    )
    if not assoc.is_established:
        reason = association_failure_reason(assoc, ae_title, our_ae_title)
        error_code = "ASSOCIATION_REJECTED" if getattr(assoc, "is_rejected", False) else "ASSOCIATION_FAILED"
        assoc.release()
        return {"ok": False, "reason": reason, "error_code": error_code}

    contexts = getattr(assoc, "accepted_contexts", None)
    if contexts is not None and not any(
        context.abstract_syntax == StudyRootQueryRetrieveInformationModelGet
        for context in contexts
    ):
        assoc.release()
        return {"ok": False, "error_code": "RETRIEVE_UNSUPPORTED",
                "reason": "Remote peer did not accept Study Root C-GET"}

    ds = query if query is not None else Dataset()
    if "QueryRetrieveLevel" not in ds:
        ds.QueryRetrieveLevel = "STUDY"

    result: dict = {"ok": False, "status": None, "received": []}
    try:
        for status, _identifier in assoc.send_c_get(
            ds, query_model=StudyRootQueryRetrieveInformationModelGet
        ):
            if status is None:
                continue
            code = int(status.Status)
            result["status"] = code
            if code == 0x0000:
                result["ok"] = True
            for key, attribute in (("completed", "NumberOfCompletedSuboperations"),
                                   ("failed", "NumberOfFailedSuboperations"),
                                   ("remaining", "NumberOfRemainingSuboperations")):
                value = getattr(status, attribute, None)
                if value is not None:
                    result[key] = int(value)
            if code == 0xB000:
                result["ok"] = False
            elif code not in (0xFF00, 0xFF01):
                result["reason"] = f"status 0x{code:04x}"
        result["received"] = received
    except Exception as exc:
        # Catch at the protocol boundary so the association is always released.
        log.error("C-GET failed (error_type=%s)", type(exc).__name__)
        result["ok"] = False
        result["reason"] = f"C-GET failed ({type(exc).__name__})"
    finally:
        assoc.release()
    if receive_failures:
        result["ok"] = False
        result["failed"] = max(result.get("failed", 0), receive_failures)
        result["reason"] = "Local C-GET instance ingestion failed"
        result["error_code"] = "LOCAL_INGEST_FAILED"
    result["received"] = received
    return result
