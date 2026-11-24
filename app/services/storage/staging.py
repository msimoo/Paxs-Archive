"""Stage-then-publish: the one write contract every ingest path shares.

``StagedObject`` is the state machine (N-3):

  1. **Stage** — new bytes go to an isolated temp location; the canonical key
     still holds the previous (good) object.
  2. **Verify** — :meth:`StagedObject.verify` reads the staged bytes back and
     compares the SHA-256, so a truncated or misdirected write is caught here.
  3. **Promote** — :meth:`StagedObject.promote` publishes atomically
     (``os.replace`` on the local backend). No reader sees a partial object.
  4. **Cleanup** — the source object is unlinked only after promotion *and* the
     database commit have both succeeded (:func:`defer_after_commit`).

Nothing here publishes before the caller says so. Quota is reserved *before*
any byte is written and released on every failure path, which is why
:func:`stage_bytes` reserves first and releases in its ``except``.

The higher-level operations built on this (save / persist / move / compress)
live in :mod:`app.services.storage.lifecycle`.
"""

from __future__ import annotations

import hashlib
import io
import logging
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, cast

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import UID, ExplicitVRLittleEndian
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core import dicom_uids
from app.core.config import settings
from app.services.quota_service import QuotaExceeded, QuotaInfo, release_quota, reserve_quota
from app.services.storage.backend import StorageError, spool_sha256, spool_size
from app.services.storage.keys import instance_key

# `_resolve` here is the facade-routed seam (see resolve._resolve_seam): the
# fault-injection suite patches ``storage_service._resolve``.
from app.services.storage.resolve import _resolve_seam as _resolve

log = logging.getLogger("paxs.storage")

_FACADE = "app.services.storage_service"

_DISK_USAGE_CACHE_TTL_SECONDS = 5.0
_disk_usage_lock = threading.Lock()
_disk_usage_cache: dict[str, tuple[float, int, int]] = {}  # root -> (ts, total, free)


def invalidate_disk_space_cache() -> None:
    """Clear cached filesystem disk-usage snapshots (used by tests & admin checks)."""
    with _disk_usage_lock:
        _disk_usage_cache.clear()


def _cached_disk_usage(root_str: str) -> tuple[int, int]:
    now = time.monotonic()
    with _disk_usage_lock:
        entry = _disk_usage_cache.get(root_str)
        if entry is not None and (now - entry[0]) < _DISK_USAGE_CACHE_TTL_SECONDS:
            return entry[1], entry[2]
    usage = shutil.disk_usage(root_str)
    with _disk_usage_lock:
        if len(_disk_usage_cache) >= 32:
            _disk_usage_cache.clear()
        _disk_usage_cache[root_str] = (now, int(usage.total), int(usage.free))
    return int(usage.total), int(usage.free)


def check_local_disk_space(backend: Any, incoming_bytes: int = 0) -> None:
    """Reject new local-disk ingests before the filesystem hits 100% full.

    Raises :class:`QuotaExceeded` (backend scope -> HTTP 507 on STOW-RS,
    DICOM 0xA700 Out of Resources on C-STORE) when projected free space falls
    below ``STORAGE_MIN_FREE_GB`` or ``STORAGE_MIN_FREE_PERCENT``.
    """
    if getattr(backend, "kind", None) != "local":
        return
    min_gb = float(getattr(settings, "STORAGE_MIN_FREE_GB", 0.0) or 0.0)
    min_pct = float(getattr(settings, "STORAGE_MIN_FREE_PERCENT", 0.0) or 0.0)
    if min_gb <= 0.0 and min_pct <= 0.0:
        return
    root = getattr(backend, "root", None)
    if root is None:
        return
    try:
        total, free = _cached_disk_usage(str(root))
    except OSError:
        return
    if total <= 0:
        return
    projected_free = max(0, free - max(0, int(incoming_bytes)))
    min_bytes_floor = int(min_gb * (1024 ** 3)) if min_gb > 0.0 else 0
    min_pct_floor = int(total * (min_pct / 100.0)) if min_pct > 0.0 else 0
    required_free = max(min_bytes_floor, min_pct_floor)
    if projected_free < required_free:
        scope_id = str(getattr(backend, "name", None) or "local-disk")
        log.warning(
            "storage_disk_low_water_mark backend=%s free_mb=%.1f required_mb=%.1f",
            scope_id,
            projected_free / (1024 * 1024),
            required_free / (1024 * 1024),
        )
        raise QuotaExceeded([
            QuotaInfo(
                scope_type="backend",
                scope_id=scope_id,
                limit_bytes=total,
                used_bytes=total,  # forces is_exceeded=True, tenant_limited=False -> HTTP 507 / 0xA700
                warning_threshold_pct=90,
                enabled=True,
                warning_triggered=True,
            )
        ])


def get_local_disk_status(backend_name: str | None = None) -> dict[str, Any]:
    """Return live free-space telemetry for the active local storage backend."""
    bk = _resolve(backend_name)
    if getattr(bk, "kind", None) != "local" or getattr(bk, "root", None) is None:
        return {"kind": getattr(bk, "kind", "unknown"), "local": False, "low_space": False}
    try:
        usage = shutil.disk_usage(str(bk.root))
        total = int(usage.total)
        free = int(usage.free)
    except OSError as exc:
        return {"kind": "local", "local": True, "error": type(exc).__name__, "low_space": False}
    min_gb = float(getattr(settings, "STORAGE_MIN_FREE_GB", 0.0) or 0.0)
    min_pct = float(getattr(settings, "STORAGE_MIN_FREE_PERCENT", 0.0) or 0.0)
    min_floor = max(
        int(min_gb * (1024 ** 3)) if min_gb > 0.0 else 0,
        int(total * (min_pct / 100.0)) if min_pct > 0.0 else 0,
    )
    free_pct = round((free / total) * 100.0, 2) if total > 0 else 0.0
    return {
        "kind": "local",
        "local": True,
        "root": str(bk.root),
        "total_gb": round(total / (1024 ** 3), 2),
        "free_gb": round(free / (1024 ** 3), 2),
        "free_pct": free_pct,
        "min_free_gb": min_gb,
        "min_free_pct": min_pct,
        "low_space": bool(min_floor > 0 and free < min_floor),
    }


def _facade() -> ModuleType:
    """The public ``app.services.storage_service`` namespace, resolved per call.

    ``stage_bytes`` is the seam the fault-injection tests patch (a stage that
    fails halfway must still release its reservation), so internal callers look
    it up on the facade at call time instead of binding it at import.
    """
    return sys.modules[_FACADE]


@dataclass
class StagedObject:
    """An object written to a temporary location, not yet published (H08 + H09).

    This is the stage-then-publish contract every write path shares
    (A-1 / A-14 / FD-1):

      1. **Stage** — the new bytes go to an isolated temp location; the canonical
         key still holds the previous (good) object.
      2. **Assert** — :meth:`verify` reads the staged bytes back and compares the
         SHA-256, so a truncated or misdirected write is caught here.
      3. **Promote** — :meth:`promote` publishes atomically (``os.replace`` on the
         local backend). Until this runs, no reader can observe a partial object.
      4. **Cleanup** — a caller only unlinks the *source* object once promotion
         and the database commit have both succeeded.

    Nothing is published at the canonical key until :meth:`promote` runs. If
    anything fails, :meth:`discard` removes the staged file and gives the
    reserved quota back, leaving any pre-existing object — and every source
    object — untouched.
    """

    key: str
    staged_path: str | None
    sha256: str
    size: int            # bytes *charged* to the quota (a delta on reimports)
    tenant_id: int | None
    backend_name: str | None
    bytes_written: int = 0  # the object's actual size, measured while staging

    def verify(self) -> None:
        """Read the staged bytes back and assert they are intact (Phase 2).

        The assertion the whole pattern rests on: a short write, a backend that
        silently wrote somewhere else, or a corrupted buffer is caught *while*
        the canonical key still holds the previous good object and the source
        objects are still in place — instead of being discovered by a viewer (or
        a modality's storage-commitment check) later.

        C-7: when the object was staged to a spool file, the check is a
        **streamed** re-hash of that file (constant memory), not a full
        read-back into a buffer — so verifying a multi-GB instance costs one
        bounded pass instead of one object in RAM.
        """
        backend = _resolve(self.backend_name)
        staged_verifier = getattr(backend, "verify_staged", None)
        if self.staged_path is not None and staged_verifier is not None:
            digest, size = staged_verifier(self.staged_path)
        elif self.staged_path is not None:
            digest = spool_sha256(self.staged_path)
            size = spool_size(self.staged_path)
        else:
            data = staged_bytes(self)
            digest = hashlib.sha256(data).hexdigest()
            size = len(data)
        if digest != self.sha256:
            raise StorageError(
                f"staged object for {self.key!r} failed read-back verification: "
                f"sha256 {digest} != {self.sha256}"
            )
        if self.bytes_written and size != self.bytes_written:
            raise StorageError(
                f"staged object for {self.key!r} read back {size} bytes, "
                f"expected {self.bytes_written}"
            )

    def promote(self) -> None:
        """Publish the staged bytes at the canonical key (no-op when unstaged)."""
        if self.staged_path is None:
            return
        _resolve(self.backend_name).promote_staged(self.staged_path, self.key)
        self.staged_path = None

    def discard(self) -> None:
        """Drop the staged file and release the reservation."""
        if self.staged_path is not None:
            try:
                _resolve(self.backend_name).discard_staged(self.staged_path)
            except Exception as exc:  # cleanup must not mask the cause
                log.warning("staged_object_discard_failed error_type=%s",
                            type(exc).__name__)
            self.staged_path = None
        if self.size > 0:
            try:
                release_quota(self.tenant_id, self.backend_name, self.size)
            except Exception as exc:
                log.warning("staged_object_quota_release_failed error_type=%s",
                            type(exc).__name__)


def staged_bytes(staged: StagedObject) -> bytes:
    """The bytes a staged object currently holds, read back from its backend.

    For backends that stage (local) this reads the temp file; for backends where
    a single write is already atomic (S3) it reads the canonical key — which is
    the same assertion, since the bytes are there either way.
    """
    bk = _resolve(staged.backend_name)
    if staged.staged_path is not None:
        reader = getattr(bk, "read_staged", None)
        if reader is not None:
            return reader(staged.staged_path)
    return bk.read_bytes(staged.key)


def publish_staged(staged: Iterable[StagedObject]) -> None:
    """Promote every staged object (Phase 3), raising on the first failure."""
    for obj in staged:
        obj.promote()


def discard_staged(staged: Iterable[StagedObject]) -> None:
    """Discard every staged object, releasing reservations (never raises)."""
    for obj in staged:
        obj.discard()


def defer_after_commit(session: Session | AsyncSession,
                       callback: Callable[[], None]) -> None:
    """Run ``callback`` **only if** the caller's transaction commits.

    The ordering primitive behind Phases 3-4 of the stage-then-publish contract:
    work that must not happen unless the database actually commits — publishing a
    replacement over an existing key, unlinking a superseded source object — is
    registered here rather than done inline. On rollback the callback is dropped,
    so the pre-existing objects stay exactly as the rolled-back database
    describes them.

    This is why it is an event and not an inline call: the source object is the
    last copy that still matches the *uncommitted* database, so deleting (or
    overwriting) it before the commit turns any later failure into data loss —
    the rollback restores rows that point at files which no longer hold the data
    the row describes (the "phantom reference" defect this change removes).

    Registered on the sync Session, so it fires for async and sync callers alike.
    A failure inside the callback is logged and never fails the commit: the row
    is already durable, and the worst case is an unreferenced duplicate, never a
    dangling reference.
    """
    from sqlalchemy import event

    sync_session = getattr(session, "sync_session", session)
    state = {"active": True}

    def _on_commit(_session: Session) -> None:
        if not state["active"]:
            return
        state["active"] = False
        try:
            callback()
        except Exception as exc:  # must never fail a committed transaction
            log.warning("post_commit_storage_callback_failed error_type=%s",
                        type(exc).__name__)

    def _on_rollback(_session: Session) -> None:
        # Rolled back: the pre-existing objects are the only copies that match
        # the database, so the deferred work must not run.
        state["active"] = False

    event.listen(sync_session, "after_commit", _on_commit, once=True)
    event.listen(sync_session, "after_rollback", _on_rollback, once=True)


def defer_after_rollback(session: Session | AsyncSession,
                         callback: Callable[[], None]) -> None:
    """Run ``callback`` if the caller's transaction rolls back (best effort).

    The counterpart of :func:`defer_after_commit`, for work that has to be
    *undone* rather than completed: objects this operation published at
    brand-new keys are unreferenced once the transaction rolls back, so they are
    removed to leave the file layer exactly as it was before the call. A failure
    is logged, never raised into the rollback path.
    """
    from sqlalchemy import event

    sync_session = getattr(session, "sync_session", session)
    state = {"armed": True}

    def _on_rollback(_session: Session) -> None:
        if not state["armed"]:
            return
        state["armed"] = False
        try:
            callback()
        except Exception as exc:  # must never disturb the rollback
            log.warning("post_rollback_storage_cleanup_failed error_type=%s",
                        type(exc).__name__)

    def _on_commit(_session: Session) -> None:
        # Committed: the published objects are referenced now, so keep them.
        state["armed"] = False

    event.listen(sync_session, "after_rollback", _on_rollback, once=True)
    event.listen(sync_session, "after_commit", _on_commit, once=True)


def defer_cleanup(session: Session | AsyncSession,
                  keys: Iterable[tuple[str, str | None]]) -> None:
    """Unlink superseded objects only if the caller's transaction commits.

    Phase 4 of the stage-then-publish contract, built on
    :func:`defer_after_commit`. The deletes are per-object unlinks; one that
    fails leaves an unreferenced duplicate (harmless, and collectable) rather
    than failing the commit.

    C-17: these are *superseded* copies replaced by a rewrite that was charged
    ``charge_bytes=0``, so they release no quota — the bytes are still counted
    under the object's new (or replacement) row, and decrementing here would
    double-count the same bytes out of the archive. Only an object leaving the
    archive entirely gives its bytes back.
    """
    pending = [(key, backend) for key, backend in keys if key]
    if not pending:
        return

    def _delete() -> None:
        for key, backend in pending:
            try:
                _resolve(backend).delete(key)
            except Exception as exc:  # cleanup must never fail the commit
                log.warning(
                    "superseded_object_cleanup_failed error_type=%s",
                    type(exc).__name__,
                )

    defer_after_commit(session, _delete)


def _ensure_file_meta(ds: Dataset, sop_uid: str) -> None:
    """Give a dataset the File Meta Information pydicom needs to encode it.

    Received datasets normally carry it; a synthesized one (split/merge, tests)
    may not, and ``save_as(enforce_file_format=True)`` would fail without it.
    """
    if getattr(ds, "file_meta", None) is not None:
        return
    meta = FileMetaDataset()
    # pydicom types these attributes as UID; the values arrive as str (one from
    # an arbitrary dataset attribute, one from the caller). The cast is
    # type-only — a runtime UID() construction would validate and warn on an
    # empty/legacy value, which this function has never done.
    meta.MediaStorageSOPClassUID = cast(UID, str(getattr(ds, "SOPClassUID", "")))
    meta.MediaStorageSOPInstanceUID = cast(UID, sop_uid)
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = meta


def stage_bytes(
    key: str,
    data: bytes,
    *,
    tenant_id: int | None = None,
    backend_name: str | None = None,
    charge_bytes: int | None = None,
    require_isolated: bool = False,
) -> StagedObject:
    """Reserve quota, then stage raw ``data`` at ``key`` without publishing it.

    Rewrites/transcodes pass ``charge_bytes=0`` so they are not double-counted
    or blocked by the local disk low-water mark.
    """
    bk = _resolve(backend_name)
    charge = len(data) if charge_bytes is None else max(0, charge_bytes)
    if charge:
        check_local_disk_space(bk, incoming_bytes=len(data))
        reserve_quota(tenant_id, backend_name, charge)

    try:
        if hasattr(bk, "stage_bytes"):
            staged_path, sha = bk.stage_bytes(key, data)
        elif hasattr(bk, "stage_isolated_bytes"):
            # Remote backends may implement an isolated server-side staging
            # object without exposing the local-backend stage_bytes contract.
            staged_path, sha = bk.stage_isolated_bytes(key, data)
        elif require_isolated:
            raise StorageError(
                f"backend {getattr(bk, 'name', backend_name or 'default')!r} "
                "does not support isolated staging"
            )
        else:
            # Keep historical behavior only for legacy callers that explicitly
            # permit atomic writes without a separate staging namespace.
            bk.save_bytes(key, data)
            staged_path, sha = None, bk.sha256(key)
    except Exception:
        # The write itself failed — give the reservation straight back.
        if charge:
            release_quota(tenant_id, backend_name, charge)
        raise

    return StagedObject(
        key=key,
        staged_path=staged_path,
        sha256=sha,
        size=charge,
        tenant_id=tenant_id,
        backend_name=backend_name,
        bytes_written=len(data),
    )


def stage_spooled(spool_path: str, key: str, *,
                  tenant_id: int | None = None,
                  backend_name: str | None = None,
                  size: int | None = None,
                  sha256: str | None = None,
                  charge_bytes: int | None = None) -> StagedObject:
    """Adopt a locally spooled file as the staged object for ``key`` (C-10).

    The streamed ingest spools each multipart part to disk **before** it can
    know the canonical key (the key is derived from the part's DICOM UIDs), so
    the reservation and the :class:`StagedObject` are created here, once both are
    known. Reserving before :meth:`StagedObject.promote` preserves the
    stage-then-publish ordering: a full archive refuses the object while nothing
    has been published and no row has been written.

    ``size``/``sha256`` are the figures measured while spooling; when omitted
    they are re-derived from the file (streamed, never buffered).
    """
    measured = spool_size(spool_path) if size is None else int(size)
    digest = sha256 or spool_sha256(spool_path)
    charge = measured if charge_bytes is None else max(0, charge_bytes)
    if charge:
        check_local_disk_space(_resolve(backend_name), incoming_bytes=measured)
        # Raises QuotaExceeded; nothing has been published at this point.
        reserve_quota(tenant_id, backend_name, charge)
    return StagedObject(
        key=key,
        staged_path=spool_path,
        sha256=digest,
        size=charge,
        tenant_id=tenant_id,
        backend_name=backend_name,
        bytes_written=measured,
    )


def stage_dataset_with_quota(ds: Dataset, tenant_id: int | None,
                             backend_name: str | None = None,
                             charge_bytes: int | None = None) -> StagedObject:
    """Reserve quota, then stage a Dataset without publishing it.

    Thin wrapper over :func:`stage_bytes` (which owns the quota ordering); the
    Dataset is serialized first, then staged exactly like raw bytes.

    A-17: the Type 1 UIDs are validated here too, not only at the ingest
    boundary, so no caller can reach the key builder with a missing UID and
    recreate the shared ``sha1("")`` bucket that merged unrelated studies.
    """
    uids = dicom_uids.validate_mandatory_uids(ds)
    sop_uid = uids["SOPInstanceUID"]
    _ensure_file_meta(ds, sop_uid)

    key = instance_key(
        uids["StudyInstanceUID"],
        uids["SeriesInstanceUID"],
        sop_uid,
    )
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return _facade().stage_bytes(
        key, buf.getvalue(),
        tenant_id=tenant_id, backend_name=backend_name, charge_bytes=charge_bytes,
    )


def purge_stale_temp_files(backend_name: str | None = None,
                           max_age_seconds: int = 3600) -> int:
    """Sweep abandoned `.tmp_*` staged files (H09 maintenance helper)."""
    bk = _resolve(backend_name)
    if not hasattr(bk, "purge_stale_temp_files"):
        return 0
    return bk.purge_stale_temp_files(max_age_seconds)
