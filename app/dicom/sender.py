"""DICOM SCU helpers — C-STORE (send/forward), C-FIND (query), C-MOVE and
C-ECHO against peer AEs. Also used by the integration tests to push images
into the receiver.

High-level entry points:
  * :func:`send_to_node_sync` — resolve a registered ``dicom_nodes`` entry and
    C-STORE every matching instance (study/series/instance scope, VNA-aware)
  * :func:`query_remote_node_sync` — resolve a registered node and run a
    C-FIND query against it (any Q/R level, wildcard-friendly)
  * :func:`send_keys` — send an explicit list of storage keys (works on both
    the local filesystem and S3/MinIO backends)
  * :func:`send_file` / :func:`send_directory` — raw single-file/dir sends
  * :func:`send_find` / :func:`send_move` / :func:`send_echo` — raw DIMSE ops
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import pydicom
from pynetdicom import AE
from pydicom.dataset import Dataset
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelGet,
    StudyRootQueryRetrieveInformationModelMove,
)
from pydicom.uid import ExplicitVRLittleEndian

log = logging.getLogger("paxs.dicom.sender")


def _tls_args(peer_host: str):
    """(context, server_hostname) when DICOM TLS is enabled, else None.

    The same global switch that turns TLS on for the receiver also upgrades
    every outbound association (node ping, forwarding, C-MOVE) — so a TLS
    archive talks to TLS peers without per-call plumbing. `peer_host` is the
    address we actually connect to: it feeds TLS hostname verification.
    """
    from app.dicom.tls import scu_tls_args

    return scu_tls_args(peer_host)


def send_file(host: str, port: int, ae_title: str, file_path: str | Path,
              our_ae_title: str = "PAXS_SCU") -> dict:
    """Send one DICOM file via C-STORE. Returns {"ok": bool, ...}."""
    ds = pydicom.dcmread(str(file_path))
    sop_class = str(getattr(ds, "SOPClassUID", ""))
    transfer_syntax = str(getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", ExplicitVRLittleEndian))

    ae = AE(ae_title=our_ae_title)
    try:
        ae.add_requested_context(sop_class, transfer_syntax)
    except ValueError:
        ae.add_requested_context(sop_class, ExplicitVRLittleEndian)

    assoc = ae.associate(host, port, ae_title=ae_title, tls_args=_tls_args(host))
    if not assoc.is_established:
        reason = "association rejected"
        try:
            reason = getattr(assoc.acceptor, "reason", reason) or reason
        except Exception:
            pass
        assoc.release()
        return {"ok": False, "reason": reason}

    status = assoc.send_c_store(ds, msg_id=1)
    assoc.release()

    code = int(status.Status) if status is not None else None
    return {"ok": code == 0x0000, "status": code, "sop_instance_uid": str(ds.SOPInstanceUID)}


def send_find(
    host: str, port: int, ae_title: str,
    query: Dataset | None = None,
    query_model=StudyRootQueryRetrieveInformationModelFind,
    our_ae_title: str = "PAXS_SCU",
) -> dict:
    """C-FIND SCU — query a remote archive and collect the matches.

    `query` carries QueryRetrieveLevel + match keys; empty values request that
    the SCP return the field (DICOM semantics). `query_model` selects the
    information model (Study Root by default; prefetching uses Patient Root).

    Returns ``{"ok", "status", "matches": [Dataset, ...], "reason"?}`` where
    ``status`` is the final C-FIND status (0x0000 = success). Association
    failure yields ``{"ok": False, "reason": "association rejected"}``.
    """
    ae = AE(ae_title=our_ae_title)
    ae.add_requested_context(query_model, ExplicitVRLittleEndian)

    assoc = ae.associate(host, port, ae_title=ae_title, tls_args=_tls_args(host))
    if not assoc.is_established:
        assoc.release()
        return {"ok": False, "reason": "association rejected"}

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
                result["matches"].append(identifier)
            elif code == 0x0000:
                result["ok"] = True
            elif code not in (0xFF00, 0xFF01):
                result["reason"] = f"status 0x{code:04x}"
    except Exception as exc:
        result["reason"] = str(exc)
    finally:
        assoc.release()
    return result


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
        except Exception:
            pass
    for keyword, value in (filters or {}).items():
        if value is None:
            continue
        try:
            setattr(query, keyword, str(value))
        except Exception:
            log.warning("C-FIND: cannot set query keyword %s", keyword)

    result = send_find(node.host, node.port, node.ae_title, query,
                       our_ae_title=our_ae_title)
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "status": result["status"],
        "ae_title": ae_title,
        "matches": [_normalize_match(m) for m in result["matches"]],
    }


def send_move(host: str, port: int, ae_title: str, dest_ae_title: str,
              query: Dataset | None = None, our_ae_title: str = "PAXS_SCU") -> dict:
    """Send a C-MOVE request to a remote archive: ask it to move matching
    instances to `dest_ae_title` (another AE, typically ourselves or a
    forwarding target). Returns a summary of the move response.

    `query` may carry QueryRetrieveLevel + match keys (e.g. StudyInstanceUID);
    defaults to a STUDY-level query matching nothing (so use a real query).
    """
    ae = AE(ae_title=our_ae_title)
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove, ExplicitVRLittleEndian)

    assoc = ae.associate(host, port, ae_title=ae_title, tls_args=_tls_args(host))
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
            if hasattr(status, "NumberOfCompletedSuboperations"):
                results["completed"] = status.NumberOfCompletedSuboperations or 0
                results["failed"] = status.NumberOfFailedSuboperations or 0
                results["remaining"] = status.NumberOfRemainingSuboperations or 0
    except Exception as exc:
        results["reason"] = str(exc)
    finally:
        assoc.release()
    return results


def send_get(host: str, port: int, ae_title: str,
             query: Dataset | None = None, our_ae_title: str = "PAXS_SCU",
             on_received=None) -> dict:
    """C-GET SCU — pull matching instances from a remote archive.

    Unlike C-MOVE, C-GET has the *remote* SCP push the instances back over
    this same association; no second AE is involved. `query` carries
    QueryRetrieveLevel + match keys (e.g. StudyInstanceUID).

    The payloads arrive as C-STORE sub-operations (the C-GET-RSP identifiers
    are empty), so they are collected in an EVT_C_STORE handler. Pass
    `on_received(dataset)` to stream each instance as it lands — useful for
    large pulls that must not buffer the whole study in RAM; otherwise the
    datasets are buffered and returned in ``received``.

    Returns ``{"ok", "status", "received": [Dataset, ...], "reason"?}``
    (``received`` is empty when `on_received` is used). Association failure
    yields ``{"ok": False, "reason": ...}``.
    """
    from pynetdicom import StoragePresentationContexts, build_role, evt

    ae = AE(ae_title=our_ae_title)
    # The C-GET SCU *receives* the instances as C-STORE sub-operations over
    # this association — offer the storage SOP classes with the SCP role and
    # answer each incoming store with success (the default is 0xC211 Failure).
    ext_neg = []
    for cx in StoragePresentationContexts:
        ae.add_requested_context(cx.abstract_syntax)
        ext_neg.append(build_role(cx.abstract_syntax, scp_role=True))
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelGet, ExplicitVRLittleEndian)

    received: list[Dataset] = []

    def _on_store(event) -> int:
        ds = event.dataset
        if ds is not None:
            if on_received is not None:
                # The sub-operation is acknowledged regardless; ingest failures
                # are surfaced per-instance by the caller's collector.
                try:
                    on_received(ds)
                except Exception:
                    log.exception("C-GET on_received handler failed")
            else:
                received.append(ds)
        return 0x0000

    assoc = ae.associate(
        host, port, ae_title=ae_title,
        ext_neg=ext_neg, evt_handlers=[(evt.EVT_C_STORE, _on_store)],
        tls_args=_tls_args(host),
    )
    if not assoc.is_established:
        assoc.release()
        return {"ok": False, "reason": "association rejected"}

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
                result["completed"] = getattr(status, "NumberOfCompletedSuboperations", 0)
                result["failed"] = getattr(status, "NumberOfFailedSuboperations", 0)
            elif code not in (0xFF00, 0xFF01):
                result["reason"] = f"status 0x{code:04x}"
        result["received"] = received
    except Exception as exc:
        result["reason"] = str(exc)
    finally:
        assoc.release()
    return result


def _ingest_pulled_dataset(ds: Dataset, tenant_id: int | None = None) -> None:
    """Save + index one C-GET-received instance (mirrors the C-STORE pipeline).

    Uses the same storage key layout (idempotent re-import by SOP UID) and
    the same indexing side effects (audit STORED, webhook on new study,
    compression job). `tenant_id` (VNA) tags newly created studies.
    """
    import asyncio

    from app.dicom.parser import parse_metadata
    from app.db.session import AsyncSessionLocal
    from app.services import storage_service
    from app.services.indexing_service import index_new_instance

    # Multi-storage: pulled slices join the study's recorded backend so a
    # study on a named backend never splits across backends.
    backend = storage_service.resolve_study_backend(
        str(getattr(ds, "StudyInstanceUID", "UNKNOWN"))
    )
    path, sha256 = storage_service.save_dataset(ds, backend_name=backend)
    meta = parse_metadata(ds)

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await index_new_instance(
                    session, meta, path, sha256,
                    performed_by="dicom-pull", tenant_id=tenant_id,
                    storage_backend=backend,
                )

    asyncio.run(_run())


def pull_from_node_sync(
    ae_title: str,
    level: str = "STUDY",
    filters: dict | None = None,
    our_ae_title: str = "PAXS_SCU",
    tenant_id: int | None = None,
) -> dict:
    """C-GET a registered remote node and ingest the received instances locally.

    Resolves the ``dicom_nodes`` entry, issues a C-GET for the given Q/R level
    + match keys, and saves every received instance through the normal ingest
    pipeline (idempotent re-import). Newly created studies are tagged with
    `tenant_id` (VNA). Returns a summary with per-instance results for
    auditing; node-level problems carry a ``reason``.
    """
    from sqlalchemy import select

    from app.db.models import DicomNode
    from app.db.session import SyncSessionLocal

    with SyncSessionLocal() as session:
        node = session.execute(
            select(DicomNode).where(DicomNode.ae_title == ae_title)
        ).scalar_one_or_none()
    if node is None:
        return {"ok": False, "reason": "Node not found", "pulled": 0, "failed": 0, "results": []}
    if not node.enabled:
        return {"ok": False, "reason": "Node is disabled", "pulled": 0, "failed": 0, "results": []}

    # If PatientID is provided without StudyInstanceUID, query studies for this patient first
    # to guarantee compatibility across all PACS systems (many PACS only accept STUDY-level C-GET queries with StudyInstanceUID).
    patient_id = (filters or {}).get("PatientID")
    study_uid = (filters or {}).get("StudyInstanceUID")
    if patient_id and not study_uid:
        try:
            find_res = query_remote_node_sync(ae_title, level="STUDY", filters={"PatientID": str(patient_id)}, our_ae_title=our_ae_title)
            found_studies = [
                m.get("StudyInstanceUID") for m in (find_res.get("matches") or [])
                if m.get("StudyInstanceUID")
            ]
            if found_studies:
                total_pulled = 0
                total_failed = 0
                all_results = []
                for s_uid in found_studies:
                    sub_filters = dict(filters or {})
                    sub_filters["StudyInstanceUID"] = s_uid
                    sub_query = Dataset()
                    sub_query.QueryRetrieveLevel = "STUDY"
                    for k, v in sub_filters.items():
                        if v is not None and str(v).strip():
                            try:
                                setattr(sub_query, k, str(v))
                            except Exception:
                                pass
                    def _collect_sub(ds: Dataset) -> None:
                        nonlocal total_pulled, total_failed
                        item = {
                            "sop_uid": str(getattr(ds, "SOPInstanceUID", "")),
                            "study_uid": str(getattr(ds, "StudyInstanceUID", "")),
                            "ok": False,
                        }
                        try:
                            _ingest_pulled_dataset(ds, tenant_id)
                            item["ok"] = True
                            total_pulled += 1
                        except Exception as exc:
                            item["reason"] = str(exc)
                            total_failed += 1
                        all_results.append(item)

                    send_get(node.host, node.port, node.ae_title, sub_query,
                             our_ae_title=our_ae_title, on_received=_collect_sub)
                
                return {
                    "ok": True,
                    "status": 0x0000,
                    "ae_title": ae_title,
                    "level": "STUDY",
                    "pulled": total_pulled,
                    "failed": total_failed,
                    "results": all_results,
                }
        except Exception as exc:
            log.warning("C-FIND pre-query for PatientID %s failed: %s; falling back to direct C-GET", patient_id, exc)

    # Stream the ingest: each received instance is saved + indexed as it
    # lands (never buffers a whole study in RAM), with per-instance failure
    # isolation — one bad instance never aborts the rest of the pull.
    pulled = failed = 0
    results: list[dict] = []

    def _collect(ds: Dataset) -> None:
        nonlocal pulled, failed
        item = {
            "sop_uid": str(getattr(ds, "SOPInstanceUID", "")),
            "study_uid": str(getattr(ds, "StudyInstanceUID", "")),
            "ok": False,
        }
        try:
            _ingest_pulled_dataset(ds, tenant_id)
            item["ok"] = True
            pulled += 1
        except Exception as exc:
            item["reason"] = str(exc)
            failed += 1
        results.append(item)

    query = Dataset()
    qr_level = (level or "STUDY").upper()
    query.QueryRetrieveLevel = qr_level
    # Always request the standard return fields (empty value = return field),
    # mirroring query_remote_node_sync so STUDY/PATIENT/SERIES/INSTANCE pulls
    # carry the same match-key set the dashboard expects.
    for keyword in _RETURN_FIELDS.get(qr_level, []):
        try:
            setattr(query, keyword, "")
            if getattr(query, keyword, None) is None:
                del query[keyword]
        except Exception:
            pass
    for keyword, value in (filters or {}).items():
        if value is None:
            continue
        try:
            setattr(query, keyword, str(value))
        except Exception:
            log.warning("C-GET: cannot set query keyword %s", keyword)

    result = send_get(node.host, node.port, node.ae_title, query,
                      our_ae_title=our_ae_title, on_received=_collect)
    if not result.get("ok"):
        return {
            "ok": False,
            "reason": result.get("reason") or "remote C-GET failed",
            "pulled": pulled, "failed": failed, "results": results,
        }

    return {
        "ok": True,
        "status": result.get("status"),
        "ae_title": ae_title,
        "level": qr_level,
        "pulled": pulled,
        "failed": failed,
        "results": results,
    }


def send_echo(host: str, port: int, ae_title: str, our_ae_title: str = "PAXS_SCU") -> dict:
    """C-ECHO (DICOM verification) against a remote AE. Returns {"ok": ...}."""
    from pynetdicom.sop_class import Verification

    ae = AE(ae_title=our_ae_title)
    ae.add_requested_context(Verification, ExplicitVRLittleEndian)
    assoc = ae.associate(host, port, ae_title=ae_title, tls_args=_tls_args(host))
    if not assoc.is_established:
        assoc.release()
        return {"ok": False, "reason": "association rejected"}
    status = assoc.send_c_echo()
    assoc.release()
    code = int(status.Status) if status is not None else None
    return {"ok": code == 0x0000, "status": code}


def _send_mpps_n_op(host: str, port: int, ae_title: str, ds: Dataset,
                    sop_uid: str, operation: str,
                    our_ae_title: str = "PAXS_SCU") -> dict:
    """Shared N-CREATE/N-SET SCU for the Modality Performed Procedure Step.

    `operation` is "create" or "set". Returns ``{"ok", "status", "reason"?}``
    — status 0x0000 = success, 0x0112 = unknown SOP instance (N-SET before
    N-CREATE), 0xA700/0xA701 = attribute list error (missing / inconsistent
    value). Association failure or a mid-transfer abort yields
    ``{"ok": False, "reason": ...}``.
    """
    from pynetdicom.sop_class import ModalityPerformedProcedureStep

    ae = AE(ae_title=our_ae_title)
    ae.add_requested_context(ModalityPerformedProcedureStep, ExplicitVRLittleEndian)
    assoc = ae.associate(host, port, ae_title=ae_title, tls_args=_tls_args(host))
    if not assoc.is_established:
        assoc.release()
        return {"ok": False, "reason": "association rejected"}
    result: dict = {"ok": False, "status": None}
    try:
        if operation == "create":
            status, _ = assoc.send_n_create(
                ds, ModalityPerformedProcedureStep, sop_uid)
        else:
            status, _ = assoc.send_n_set(
                ds, ModalityPerformedProcedureStep, sop_uid)
        code = int(status.Status) if status is not None and status.Status is not None else None
        result["status"] = code
        result["ok"] = code == 0x0000
        if not result["ok"] and code is not None:
            result["reason"] = f"status 0x{code:04x}"
    except Exception as exc:
        # Mid-transfer abort (connection reset, protocol error) must not leak
        # an exception — match the sibling SCU helpers' graceful contract.
        result["reason"] = str(exc)
    finally:
        assoc.release()
    return result


def send_mpps_create(host: str, port: int, ae_title: str, ds: Dataset,
                     sop_uid: str, our_ae_title: str = "PAXS_SCU") -> dict:
    """MPPS N-CREATE SCU — report exam start (status IN PROGRESS) to a remote
    archive, RIS or EMR. `ds` is the performed-step attribute list."""
    return _send_mpps_n_op(host, port, ae_title, ds, sop_uid, "create",
                           our_ae_title=our_ae_title)


def send_mpps_set(host: str, port: int, ae_title: str, ds: Dataset,
                  sop_uid: str, our_ae_title: str = "PAXS_SCU") -> dict:
    """MPPS N-SET SCU — report exam end (COMPLETED / DISCONTINUED) to a remote
    archive, closing the performed step it previously created."""
    return _send_mpps_n_op(host, port, ae_title, ds, sop_uid, "set",
                           our_ae_title=our_ae_title)


def send_directory(host: str, port: int, ae_title: str, directory: str | Path) -> dict:
    """Send every DICOM file under a directory. Returns summary."""
    directory = Path(directory)
    results = {"ok": 0, "failed": 0, "errors": []}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        try:
            result = send_file(host, port, ae_title, path)
            if result.get("ok"):
                results["ok"] += 1
            else:
                results["failed"] += 1
                results["errors"].append(f"{path.name}: {result.get('reason')}")
        except Exception as exc:
            results["failed"] += 1
            results["errors"].append(f"{path.name}: {exc}")
    return results


# --------------------------------------------------------------------------- #
# High-level SCU: send stored instances from the archive to a peer node
# --------------------------------------------------------------------------- #
def _materialize_for_send(key: str, backend: str | None = None) -> tuple[str, bool]:
    """Filesystem path for a storage key plus whether it's a temp file.

    Local backend: the real file. S3/MinIO: materialize to a temp file (the
    caller must clean it up). `backend` is the named backend that holds the
    object (multi-storage).
    """
    from app.services import storage_service

    local = storage_service.local_path(key, backend_name=backend)
    if local is not None:
        return local, False
    data = storage_service.read_bytes(key, backend_name=backend)
    fd, tmp = tempfile.mkstemp(suffix=".dcm")
    os.close(fd)
    with open(tmp, "wb") as fh:
        fh.write(data)
    return tmp, True


def resolve_keys_sync(
    study_uids: list[str] | None = None,
    series_uids: list[str] | None = None,
    instance_uids: list[str] | None = None,
    tenant_id: int | None = None,
    include_all: bool = True,
) -> list[dict[str, Any]]:
    """Storage keys matching any of the given scopes (study / series / instance).

    Returns ``[{"sop_uid", "key", "study_uid", "backend"}, ...]`` for
    instances whose files actually exist on the active backend; ``backend`` is
    the named storage backend that holds each object (multi-storage), or None
    for the env-configured backend.

    VNA: when ``include_all`` is False the match is restricted to the caller's
    tenant scope (``tenant_id=None`` keeps the legacy unscoped data), exactly
    like the query service.
    """
    from sqlalchemy import or_, select

    from app.db.models import Instance, Series, Study
    from app.db.session import SyncSessionLocal
    from app.services import storage_service

    conds = []
    if study_uids:
        conds.append(Study.study_instance_uid.in_(study_uids))
    if series_uids:
        conds.append(Series.series_instance_uid.in_(series_uids))
    if instance_uids:
        conds.append(Instance.sop_instance_uid.in_(instance_uids))
    if not conds:
        return []

    with SyncSessionLocal() as session:
        stmt = (
            select(
                Instance.sop_instance_uid,
                Instance.file_path,
                Study.study_instance_uid,
                Instance.storage_backend,
            )
            .join(Series, Series.id == Instance.series_id)
            .join(Study, Study.id == Series.study_id)
            .where(or_(*conds))
        )
        if not include_all:
            cond = (
                Study.tenant_id.is_(None)
                if tenant_id is None
                else Study.tenant_id == tenant_id
            )
            stmt = stmt.where(cond)
        rows = session.execute(stmt).all()

    out = []
    for sop_uid, key, study_uid, backend in rows:
        if storage_service.exists(key, backend_name=backend):
            out.append({
                "sop_uid": sop_uid, "key": key, "study_uid": study_uid,
                "backend": backend,
            })
    return out


def send_keys(
    host: str, port: int, ae_title: str,
    targets: list[dict[str, Any]],
    our_ae_title: str = "PAXS_SCU",
) -> dict:
    """C-STORE a list of storage targets (from :func:`resolve_keys_sync`).

    Works on every backend (temp-materializes S3 objects). Returns a summary
    with per-instance results for auditing.
    """
    results: dict[str, Any] = {"ok": False, "sent": 0, "failed": 0, "results": []}
    for target in targets:
        item = {
            "sop_uid": target["sop_uid"],
            "study_uid": target.get("study_uid"),
            "ok": False,
        }
        try:
            local, is_temp = _materialize_for_send(
                target["key"], backend=target.get("backend")
            )
            try:
                res = send_file(host, port, ae_title, local, our_ae_title=our_ae_title)
            finally:
                if is_temp:
                    try:
                        os.unlink(local)
                    except OSError:
                        pass
            item["ok"] = bool(res.get("ok"))
            if not res.get("ok"):
                item["reason"] = res.get("reason") or f"status 0x{res.get('status', 0):04x}"
        except Exception as exc:
            # One bad instance must not abort the whole study forward.
            item["reason"] = str(exc)
        if item["ok"]:
            results["sent"] += 1
        else:
            results["failed"] += 1
        results["results"].append(item)
    results["ok"] = results["failed"] == 0 and results["sent"] > 0
    return results


def send_to_node_sync(
    ae_title: str,
    study_uids: list[str] | None = None,
    series_uids: list[str] | None = None,
    instance_uids: list[str] | None = None,
    our_ae_title: str = "PAXS_SCU",
    tenant_id: int | None = None,
    include_all: bool = True,
) -> dict:
    """Resolve a registered ``dicom_nodes`` destination and C-STORE the
    matching instances to it. Returns a summary dict; on node-level problems a
    ``reason`` field explains the failure (caller maps it to HTTP).
    """
    from sqlalchemy import select

    from app.db.models import DicomNode
    from app.db.session import SyncSessionLocal

    with SyncSessionLocal() as session:
        node = session.execute(
            select(DicomNode).where(DicomNode.ae_title == ae_title)
        ).scalar_one_or_none()
    if node is None:
        return {"ok": False, "reason": "Node not found", "sent": 0, "failed": 0, "results": []}
    if not node.enabled:
        return {"ok": False, "reason": "Node is disabled", "sent": 0, "failed": 0, "results": []}

    targets = resolve_keys_sync(
        study_uids, series_uids, instance_uids, tenant_id=tenant_id, include_all=include_all
    )
    if not targets:
        return {"ok": False, "reason": "No matching instances", "sent": 0, "failed": 0, "results": []}

    result = send_keys(node.host, node.port, node.ae_title, targets, our_ae_title=our_ae_title)
    return result
