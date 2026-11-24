"""C-GET pull: retrieve from a registered node and ingest what arrives.

The split from :mod:`app.dicom.sender.query` is deliberate. ``send_get`` only
speaks DIMSE; this module owns the archive side of a pull — saving each
received instance, indexing it, and turning an ingestion failure into a failed
C-STORE sub-operation so the peer keeps its copy (A-13).
"""

from __future__ import annotations

import logging
import sys
from types import ModuleType

from pydicom.dataset import Dataset

from app.services.storage.staging import StagedObject

log = logging.getLogger("paxs.dicom.sender")

_FACADE = "app.dicom.sender"


def _facade() -> ModuleType:
    """The public ``app.dicom.sender`` namespace, resolved on every call."""
    return sys.modules[_FACADE]


def _ingest_pulled_dataset(
    ds: Dataset, tenant_id: int | None = None, include_all: bool = False
) -> None:
    """Save + index one C-GET-received instance (mirrors the C-STORE pipeline).

    Uses the same storage key layout (idempotent re-import by SOP UID) and
    the same indexing side effects (audit STORED, webhook on new study,
    compression job). `tenant_id` (VNA) tags newly created studies.
    """
    import asyncio

    from app.db.session import AsyncSessionLocal
    from app.dicom.parser import parse_metadata
    from app.services import storage_service
    # Multi-storage: pulled slices join the study's recorded backend so a
    # study on a named backend never splits across backends.
    backend = storage_service.resolve_study_backend(
        str(getattr(ds, "StudyInstanceUID", "UNKNOWN"))
    )
    meta = parse_metadata(ds)
    staged = _stage_pulled_dataset(ds, tenant_id, backend)

    asyncio.run(_persist_pulled_stage(
        ds, meta, staged, tenant_id, backend, include_all
    ))


def _stage_pulled_dataset(
    ds: Dataset, tenant_id: int | None, backend: str | None
) -> StagedObject:
    """Validate, reserve quota, and verify a C-GET dataset before indexing."""
    from app.services import storage_service

    staged = storage_service.stage_dataset_with_quota(ds, tenant_id, backend)
    try:
        staged.verify()
    except Exception:
        staged.discard()
        raise
    return staged


async def _persist_pulled_stage(
    ds: Dataset,
    meta: dict,
    staged: StagedObject,
    tenant_id: int | None,
    backend: str | None,
    include_all: bool = False,
) -> None:
    """Commit the index row before making the staged object canonical."""
    from app.db.session import AsyncSessionLocal
    from app.services.indexing_service import index_new_instance

    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await index_new_instance(
                    session, meta, staged.key, staged.sha256,
                    performed_by="dicom-pull", tenant_id=tenant_id,
                    storage_backend=backend,
                    read_path=staged.staged_path or staged.key,
                    ds=ds,
                    file_size_bytes=staged.bytes_written,
                    include_all=include_all,
                )
        staged.promote()
    except Exception:
        # Failed indexing/publishing must discard the uncommitted stage and
        # release its quota reservation; the C-GET caller reports failure.
        staged.discard()
        raise


def _build_match_query(level: str, filters: dict | None, *, return_fields: bool,
                       require_nonempty: bool) -> Dataset:
    """Build the C-GET match dataset for `level`.

    `return_fields` requests the standard per-level return attributes (empty
    value = "return this field", per DICOM); `require_nonempty` skips blank
    filter values instead of sending them as an empty match key.
    """
    query = Dataset()
    query.QueryRetrieveLevel = level
    if return_fields:
        from app.dicom.sender.query import _RETURN_FIELDS

        for keyword in _RETURN_FIELDS.get(level, []):
            try:
                setattr(query, keyword, "")
                if getattr(query, keyword, None) is None:
                    del query[keyword]
            except Exception as exc:
                # Optional return attributes may be unsupported by a peer/version;
                # omitting one must be visible, without logging its value.
                log.warning("Cannot set optional DICOM query return attribute "
                            "(error_type=%s)", type(exc).__name__)
    for keyword, value in (filters or {}).items():
        if value is None or (require_nonempty and not str(value).strip()):
            continue
        try:
            setattr(query, keyword, str(value))
        except Exception as exc:
            # Reject malformed match keys rather than silently broadening the
            # scope of a remote pull.
            log.error("Cannot set C-GET match attribute (error_type=%s)", type(exc).__name__)
            raise ValueError("Invalid C-GET match attribute") from None
    return query


class _PullCollector:
    """C-GET EVT_C_STORE sink: ingest one instance and tally the outcome.

    Raises on a failed ingest so ``send_get`` answers that sub-operation with a
    DIMSE failure — a Success response would let the peer purge the only copy
    we do not hold (A-13).
    """

    def __init__(self, tenant_id: int | None, on_progress=None, total_items: int = 0,
                 include_all: bool = False) -> None:
        self.tenant_id = tenant_id
        self.include_all = include_all
        self.pulled = 0
        self.failed = 0
        self.results: list[dict] = []
        self.on_progress = on_progress
        self.total_items = max(0, total_items)
        self._report_progress()

    def _report_progress(self) -> None:
        if self.on_progress is not None:
            self.on_progress(self.total_items, self.pulled, self.failed)

    def __call__(self, ds: Dataset) -> None:
        item = {
            "sop_uid": str(getattr(ds, "SOPInstanceUID", "")),
            "study_uid": str(getattr(ds, "StudyInstanceUID", "")),
            "ok": False,
        }
        try:
            _facade()._ingest_pulled_dataset(
                ds, self.tenant_id, include_all=self.include_all
            )
            item["ok"] = True
            self.pulled += 1
        except Exception as exc:
            from app.core.dicom_diagnostics import decode_context

            log.error("Pulled instance ingestion failed (error_type=%s) | %s",
                      type(exc).__name__, decode_context(ds))
            item["reason"] = f"Instance ingestion failed ({type(exc).__name__})"
            self.failed += 1
            self.results.append(item)
            self._report_progress()
            # Propagate to send_get's C-STORE callback so it sends a failure
            # status, rather than acknowledging data loss.
            raise
        self.results.append(item)
        self._report_progress()


def _pull_patient_studies(node, filters: dict | None, our_ae_title: str,
                          tenant_id: int | None, on_progress=None,
                          include_all: bool = False) -> dict | None:
    """Pull every study of a PatientID-only request, one C-GET per study.

    Many PACS accept a STUDY-level C-GET only with a StudyInstanceUID, so a
    patient-scoped pull resolves the patient's studies with C-FIND first.
    Returns ``None`` when the pre-query found no study (the caller then falls
    back to a direct C-GET with the original filters).
    """
    find_res = _facade().query_remote_node_sync(
        node.ae_title, level="STUDY", filters={"PatientID": str(filters["PatientID"])},
        our_ae_title=our_ae_title,
    )
    found_studies = [
        m.get("StudyInstanceUID") for m in (find_res.get("matches") or [])
        if m.get("StudyInstanceUID")
    ]
    if not found_studies:
        return None

    expected_total = sum(
        int(match.get("NumberOfStudyRelatedInstances") or 0)
        for match in (find_res.get("matches") or [])
        if str(match.get("NumberOfStudyRelatedInstances") or "0").isdigit()
    )
    collector = _PullCollector(
        tenant_id, on_progress, expected_total, include_all=include_all
    )
    remote_ok, remote_reason = True, None
    for study_uid in found_studies:
        sub_filters = dict(filters or {})
        sub_filters["StudyInstanceUID"] = study_uid
        sub_query = _build_match_query(
            "STUDY", sub_filters, return_fields=False, require_nonempty=True
        )
        failed_before = collector.failed
        sub_result = _facade().send_get(
            node.host, node.port, node.ae_title, sub_query,
            our_ae_title=our_ae_title, on_received=collector,
        )
        # Include wire decode failures that never reached the collector,
        # without double-counting failures the collector recorded.
        collector.failed += max(
            0, int(sub_result.get("failed") or 0) - (collector.failed - failed_before)
        )
        if not sub_result.get("ok"):
            remote_ok = False
            remote_reason = sub_result.get("reason") or "Remote C-GET failed"

    ok = remote_ok and collector.failed == 0
    return {
        "ok": ok,
        "status": 0x0000 if ok else None,
        "reason": remote_reason,
        "ae_title": node.ae_title,
        "level": "STUDY",
        "pulled": collector.pulled,
        "failed": collector.failed,
        "results": collector.results,
    }


def pull_from_node_sync(
    ae_title: str,
    level: str = "STUDY",
    filters: dict | None = None,
    our_ae_title: str = "PAXS_SCU",
    tenant_id: int | None = None,
    on_progress=None,
    include_all: bool = False,
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

    # If PatientID is provided without StudyInstanceUID, query studies for this
    # patient first to guarantee compatibility across all PACS systems (many
    # PACS only accept STUDY-level C-GET queries with StudyInstanceUID).
    patient_id = (filters or {}).get("PatientID")
    study_uid = (filters or {}).get("StudyInstanceUID")
    if patient_id and not study_uid:
        try:
            summary = _pull_patient_studies(
                node, filters, our_ae_title, tenant_id, on_progress,
                include_all=include_all,
            )
            if summary is not None:
                return summary
        except Exception as exc:
            # Never emit the PatientID or remote exception text. The fallback
            # keeps the original match filters, not an unfiltered study query.
            log.warning("C-FIND pre-query failed (error_type=%s); trying direct C-GET",
                        type(exc).__name__)

    # Stream the ingest: each received instance is saved + indexed as it
    # lands (never buffers a whole study in RAM), with per-instance failure
    # isolation — one bad instance never aborts the rest of the pull.
    qr_level = (level or "STUDY").upper()
    query = _build_match_query(
        qr_level, filters, return_fields=True, require_nonempty=False
    )
    expected_total = 0
    if study_uid:
        try:
            count_result = _facade().query_remote_node_sync(
                node.ae_title, level="STUDY", filters={"StudyInstanceUID": str(study_uid)},
                our_ae_title=our_ae_title, max_results=1,
            )
            count_match = next(iter(count_result.get("matches") or []), {})
            count_value = count_match.get("NumberOfStudyRelatedInstances") or 0
            expected_total = int(count_value) if str(count_value).isdigit() else 0
        except Exception as exc:
            # C-FIND count is an optional display hint; do not prevent a valid C-GET.
            log.info("C-FIND progress estimate unavailable (error_type=%s)", type(exc).__name__)
    collector = _PullCollector(
        tenant_id, on_progress, expected_total, include_all=include_all
    )
    result = _facade().send_get(node.host, node.port, node.ae_title, query,
                                our_ae_title=our_ae_title, on_received=collector,
                                trust_cert=getattr(node, "trust_cert", None))
    failed = max(collector.failed, int(result.get("failed") or 0))
    if not result.get("ok"):
        return {
            "ok": False,
            # A-13: keep the DIMSE status in the failure summary — it is the one
            # field that distinguishes "the peer refused for resources" from
            # "we could not process the data", i.e. whether retrying after
            # freeing space can help. Dropping it left the caller with a bare
            # reason string.
            "status": result.get("status"),
            "reason": result.get("reason") or "remote C-GET failed",
            "error_code": result.get("error_code"),
            "pulled": collector.pulled, "failed": failed, "results": collector.results,
        }

    return {
        "ok": failed == 0,
        "status": result.get("status"),
        "ae_title": ae_title,
        "level": qr_level,
        "pulled": collector.pulled,
        "failed": failed,
        "results": collector.results,
    }
