"""Batched C-STORE from the archive: resolve keys, materialize, send once.

The batch layer exists for one reason: association cost. Opening a fresh
association (TCP + TLS + ACSE + DIMSE negotiation) per instance meant five
handshakes for a five-image study and hundreds for a CT, and that churn was
most of a forward's wall-clock time — multiplied by TLS handshakes on a TLS
link. Everything here resolves a study's instances to a manifest, then hands
the whole list to one association (B-15).

Seam note: the tests drive this module by patching names on the public
``app.dicom.sender`` module (``AE``, ``_tls_args``, ``_materialize_for_send``),
so those are looked up through :func:`_facade` at call time.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pydicom
from pydicom.uid import ExplicitVRLittleEndian

log = logging.getLogger("paxs.dicom.sender")

_FACADE = "app.dicom.sender"


def _facade() -> ModuleType:
    """The public ``app.dicom.sender`` namespace, resolved on every call."""
    return sys.modules[_FACADE]


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
    all matching instances, including missing objects so transfers count their
    failure rather than silently omit them; ``backend`` is the named storage
    backend that holds each object (multi-storage), or None
    for the env-configured backend.

    VNA: when ``include_all`` is False the match is restricted to the caller's
    tenant scope (``tenant_id=None`` keeps the legacy unscoped data), exactly
    like the query service.
    """
    from sqlalchemy import or_, select

    from app.db.models import Instance, Series, Study
    from app.db.session import SyncSessionLocal

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
            # FA-1: the canonical predicate — never a hand-rolled comparison.
            from app.services import query_service

            stmt = query_service.apply_study_tenant_scope(stmt, tenant_id, include_all)
        rows = session.execute(stmt).all()

    # Keep missing objects in the manifest. send_keys will fail and count them;
    # silently dropping them would report a successful but incomplete study.
    return [
        {"sop_uid": sop_uid, "key": key, "study_uid": study_uid, "backend": backend}
        for sop_uid, key, study_uid, backend in rows
    ]


#: pynetdicom allows at most 128 presentation contexts per association, and the
#: peer may accept fewer. A batch that needs more distinct (SOP class, transfer
#: syntax) pairs than this is refused rather than opening per-instance
#: associations that violate the one-association-per-study constraint.
_MAX_BATCH_CONTEXTS = 128


def _batch_contexts(files: list[str]) -> list[tuple[str, str]]:
    """Distinct (SOP class, transfer syntax) pairs across a batch.

    Read with ``stop_before_pixels`` — only the header is needed to negotiate,
    so a 500 MB instance is never loaded just to decide the contexts.
    """
    seen: set[tuple[str, str]] = set()
    contexts: list[tuple[str, str]] = []
    for path in files:
        try:
            head = pydicom.dcmread(path, stop_before_pixels=True)
        except Exception as exc:  # unreadable file: the send step will report it
            log.debug("could not read SOP header for context negotiation "
                      "(error_type=%s)", type(exc).__name__)
            continue
        sop = str(getattr(head, "SOPClassUID", "") or "")
        if not sop:
            continue
        ts = str(
            getattr(getattr(head, "file_meta", None), "TransferSyntaxUID", "")
            or ExplicitVRLittleEndian
        )
        key = (sop, ts)
        if key not in seen:
            seen.add(key)
            contexts.append(key)
    return contexts


def send_files_batch(
    host: str, port: int, ae_title: str,
    files: list[str],
    our_ae_title: str = "PAXS_SCU",
    trust_cert: str | Path | None = None,
    on_progress: Any = None,
) -> dict:
    """C-STORE many files over **one** association (B-15).

    The old path opened a fresh association (TCP + TLS + ACSE + DIMSE
    negotiation) per instance — five handshakes for a five-image study, and
    hundreds for a CT. That churn is most of the wall-clock cost of a forward,
    and it multiplies the TLS handshakes on a TLS link.

    Here the association is established once, with one presentation context per
    distinct ``(SOP class, transfer syntax)`` in the batch, and every instance is
    sent over it. Per-instance failures are still isolated: a rejected context
    or a bad dataset marks only that instance, and the rest continue.

    Returns ``{"ok": bool, "reason"?: str, "results": [{"ok", "status"?, "reason"?}]}``
    with one result per input file, in order.
    """
    if not files:
        return {"ok": True, "results": []}

    contexts = _batch_contexts(files)
    if len(contexts) > _MAX_BATCH_CONTEXTS:
        # DICOM limits an association request to 128 presentation contexts.
        # Falling back to one association per instance breaks the one-study,
        # one-association guarantee and can create hundreds of handshakes.
        log.warning(
            "C-STORE batch needs %d presentation contexts (protocol cap %d); refusing batch",
            len(contexts), _MAX_BATCH_CONTEXTS,
        )
        if on_progress is not None:
            on_progress(len(files), 0, len(files))
        reason = f"Study requires more than {_MAX_BATCH_CONTEXTS} presentation contexts"
        return {
            "ok": False,
            "reason": reason,
            "results": [{"ok": False, "reason": reason} for _ in files],
        }

    ae = _facade().AE(ae_title=our_ae_title)
    from app.dicom.sender.scu import _configure_scu_ae
    _configure_scu_ae(ae)
    for sop, ts in contexts:
        try:
            ae.add_requested_context(sop, ts)
        except ValueError:
            # An unknown/unencodable transfer syntax still negotiates the SOP
            # class; the instance then fails with 0x0122 rather than the whole
            # batch failing to associate.
            log.debug("C-STORE context rejected for transfer syntax %s; "
                      "requesting Explicit VR Little Endian", ts)
            ae.add_requested_context(sop, ExplicitVRLittleEndian)

    assoc = ae.associate(host, port, ae_title=ae_title,
                         tls_args=_facade()._tls_args(host, trust_cert))
    if not assoc.is_established:
        reason = "association rejected"
        try:
            reason = getattr(assoc.acceptor, "reason", reason) or reason
        except Exception as exc:
            log.debug("Could not read C-STORE association rejection detail "
                      "(error_type=%s)", type(exc).__name__)
        assoc.release()
        return {
            "ok": False,
            "reason": reason,
            "results": [{"ok": False, "reason": reason} for _ in files],
        }

    results: list[dict[str, Any]] = []
    try:
        for index, path in enumerate(files):
            try:
                ds = pydicom.dcmread(path)
                status = assoc.send_c_store(ds, msg_id=index + 1)
                code = int(status.Status) if status is not None else None
                if code == 0x0000:
                    results.append({"ok": True, "status": code})
                else:
                    results.append({
                        "ok": False,
                        "status": code,
                        "reason": f"status 0x{code:04x}" if code is not None
                        else "association aborted",
                    })
            except Exception as exc:
                # One bad instance must not abort the rest of the batch.
                log.error("C-STORE instance transfer failed (error_type=%s)",
                          type(exc).__name__)
                results.append({
                    "ok": False,
                    "reason": f"Instance transfer failed ({type(exc).__name__})",
                })
            if on_progress is not None:
                sent = sum(bool(result.get("ok")) for result in results)
                on_progress(len(files), sent, len(results) - sent)
    finally:
        assoc.release()

    return {"ok": all(r["ok"] for r in results), "results": results}


def send_keys(
    host: str, port: int, ae_title: str,
    targets: list[dict[str, Any]],
    our_ae_title: str = "PAXS_SCU",
    trust_cert: str | Path | None = None,
    on_progress: Any = None,
) -> dict:
    """C-STORE a list of storage targets (from :func:`resolve_keys_sync`).

    Works on every backend (temp-materializes S3 objects). Returns a summary
    with per-instance results for auditing. Every instance failure is logged and
    counted; temporary-file cleanup errors are logged independently so they do
    not turn a completed transfer into an ambiguous retry.

    B-15: the whole batch rides **one** association (``send_files_batch``), so a
    study forward costs one handshake instead of one per instance.
    """
    results: dict[str, Any] = {"ok": False, "sent": 0, "failed": 0, "results": []}
    # One slot per target, in the caller's order, so a materialization failure
    # and a send failure are reported the same way.
    items: list[dict[str, Any]] = []
    prepared: list[tuple[int, str, bool]] = []   # (target index, local path, is_temp)
    try:
        for index, target in enumerate(targets):
            item = {
                "sop_uid": target["sop_uid"],
                "study_uid": target.get("study_uid"),
                "ok": False,
            }
            items.append(item)
            try:
                local, is_temp = _facade()._materialize_for_send(
                    target["key"], backend=target.get("backend")
                )
                prepared.append((index, local, is_temp))
            except Exception as exc:
                # One bad instance must not abort the whole study forward.
                log.error("C-STORE instance transfer failed (sop_uid=%s, "
                          "error_type=%s)", target["sop_uid"], type(exc).__name__)
                item["reason"] = f"Instance transfer failed ({type(exc).__name__})"

        if prepared:
            from app.dicom import dcmtk

            def progress(total, sent, failed):
                if on_progress is not None:
                    on_progress(len(items), sent, failed + len(items) - len(prepared))

            if dcmtk.enabled_for(trust_cert):
                batch = dcmtk.send_store(
                    host, port, ae_title,
                    [(local, items[index]["sop_uid"]) for index, local, _temp in prepared],
                    our_ae_title=our_ae_title, on_progress=progress,
                )
                results["engine"] = "dcmtk"
            else:
                batch = _facade().send_files_batch(
                    host, port, ae_title,
                    [local for _index, local, _temp in prepared],
                    our_ae_title=our_ae_title, trust_cert=trust_cert,
                    on_progress=progress if on_progress is not None else None,
                )
            for (index, _local, _temp), outcome in zip(
                prepared, batch["results"], strict=True
            ):
                item = items[index]
                item["ok"] = bool(outcome.get("ok"))
                if not item["ok"]:
                    item["reason"] = (
                        outcome.get("reason") or batch.get("reason")
                        or f"status 0x{(outcome.get('status') or 0):04x}"
                    )
    finally:
        for _index, local, is_temp in prepared:
            if not is_temp:
                continue
            try:
                os.unlink(local)
            except OSError as exc:
                # Sending succeeded or failed independently; a cleanup error
                # must not hide that result or leak a temp path.
                log.warning("C-STORE temporary file cleanup failed (error_type=%s)",
                            type(exc).__name__)

    for item in items:
        if item["ok"]:
            results["sent"] += 1
        else:
            results["failed"] += 1
        results["results"].append(item)
        if on_progress is not None and not prepared:
            on_progress(len(items), results["sent"], results["failed"])
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
    on_progress: Any = None,
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

    targets = _facade().resolve_keys_sync(
        study_uids, series_uids, instance_uids, tenant_id=tenant_id, include_all=include_all
    )
    if not targets:
        return {"ok": False, "reason": "No matching instances", "sent": 0, "failed": 0, "results": []}

    return _facade().send_keys(
        node.host, node.port, node.ae_title, targets,
        our_ae_title=our_ae_title,
        # B-16: the node's pinned peer certificate is the trust anchor when no
        # global DICOM_TLS_CA is configured.
        trust_cert=getattr(node, "trust_cert", None),
        on_progress=on_progress,
    )
