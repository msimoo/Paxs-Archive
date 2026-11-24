"""Object lifecycle: save, persist, move between backends, compress.

Each function here is a *complete* stage-then-publish transaction built from
:mod:`app.services.storage.staging`: it reserves, stages, verifies, publishes,
and — for a move — unlinks the source only after the destination is proven
intact. That is why they live above the primitives: the ordering is the
contract, and it must not be re-derived at each call site.

``stage_bytes`` is resolved through the ``storage_service`` facade (see
:func:`_facade`) because the fault-injection suite patches it there to prove a
half-failed stage still releases its quota reservation.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
import tempfile
from types import ModuleType

from pydicom.dataset import Dataset

from app.services.quota_service import check_quota_before_write
from app.services.storage.backend import StorageError
from app.services.storage.keys import thumbnail_key

# `_resolve` here is the facade-routed seam (see resolve._resolve_seam): the
# fault-injection suite patches ``storage_service._resolve``.
from app.services.storage.resolve import _resolve_seam as _resolve
from app.services.storage.staging import (
    StagedObject,
    discard_staged,
    publish_staged,
    stage_dataset_with_quota,
)

log = logging.getLogger("paxs.storage")

_FACADE = "app.services.storage_service"


def _facade() -> ModuleType:
    """The public ``app.services.storage_service`` namespace, resolved per call."""
    return sys.modules[_FACADE]


def persist_bytes(key: str, data: bytes, backend_name: str | None = None) -> str:
    """Stage, verify and publish raw bytes; returns the object's SHA-256.

    The one-shot form of the stage-then-publish contract (A-1/A-14/FD-1) for
    callers that own the whole write: derived artifacts (SR, thumbnails, XDS-I
    documents) that must never appear half-written at their canonical key, and
    must not replace a previous good copy unless the new bytes verify. Use
    :func:`stage_bytes` directly when the publish has to wait for a DB commit.
    """
    staged = _facade().stage_bytes(key, data, backend_name=backend_name, charge_bytes=0)
    try:
        staged.verify()
    except Exception:
        staged.discard()
        raise
    staged.promote()
    return staged.sha256


def save_bytes_with_quota(key: str, data: bytes, tenant_id: int | None,
                          backend_name: str | None = None) -> None:
    """Persist raw bytes with quota enforcement, via stage -> verify -> promote.

    Args:
        key: Storage key (relative path)
        data: Bytes to write
        tenant_id: Tenant ID for quota scoping (None = unscoped/legacy)
        backend_name: Named backend (None = env default)
    """
    additional = len(data)
    allowed, infos = check_quota_before_write(tenant_id, backend_name, additional)
    if not allowed:
        exceeded = [f"{i.scope_type}:{i.scope_id}" for i in infos if i.is_exceeded]
        raise StorageError(f"Storage quota exceeded for {', '.join(exceeded)}")
    persist_bytes(key, data, backend_name)


def save_dataset(ds: Dataset, backend_name: str | None = None) -> tuple[str, str]:
    """Persist a Dataset via the given (or default) backend. Returns (key, sha256).

    Stage-then-publish (A-1/A-14/FD-1): the object is staged, read back and
    verified, then published atomically — a failed or short write can never
    leave a partial object at the canonical key, and a bad write is caught
    before it replaces a previous good copy. This replaces the previous
    write-then-hash order, which published first and verified nothing.

    Note: This version does NOT enforce quotas because tenant context is not
    available at this low level. Callers that have tenant context should use
    save_dataset_with_quota() instead.
    """
    staged = stage_dataset_with_quota(ds, None, backend_name, charge_bytes=0)
    try:
        staged.verify()
    except Exception:
        staged.discard()
        raise
    staged.promote()
    return staged.key, staged.sha256


def save_dataset_with_quota(ds: Dataset, tenant_id: int | None,
                            backend_name: str | None = None) -> tuple[str, str]:
    """Persist a Dataset with quota enforcement.

    Args:
        ds: pydicom Dataset to store
        tenant_id: Tenant ID for quota scoping (None = unscoped/legacy)
        backend_name: Named backend (None = env default)

    Returns:
        (key, sha256) of stored object

    Raises:
        StorageError: If quota would be exceeded

    Stage-then-publish (A-1/A-14/FD-1): the quota is reserved before any bytes
    are written, the staged object is verified by read-back, and only then is it
    published; a failure discards the stage and gives the reservation back.
    """
    staged = stage_dataset_with_quota(ds, tenant_id, backend_name)
    try:
        staged.verify()
    except Exception:
        staged.discard()
        raise
    staged.promote()
    return staged.key, staged.sha256


def move_to_backend(key: str, to_backend: str, from_backend: str | None = None) -> bool:
    """Copy one object to another backend and delete the source (cross-backend
    move). Used by Study → Move to backend tiering. ``from_backend=None``
    means the object currently lives on the env backend.

    Stage-then-publish (A-1/A-14/FD-1): the destination is staged, read back and
    verified, promoted, and verified *again* on the destination — and only then
    is the source unlinked. The previous implementation wrote the destination
    once and deleted the source immediately, so a truncated upload destroyed the
    only remaining copy of the instance.
    """
    if to_backend == from_backend:
        return True
    src = _resolve(from_backend)
    if not src.exists(key):
        return False

    dst = _resolve(to_backend)
    staged = _facade().stage_bytes(key, src.read_bytes(key),
                                   backend_name=to_backend, charge_bytes=0)
    try:
        staged.verify()          # Phase 2 — the staged bytes are intact
    except Exception:
        staged.discard()
        raise

    # The staged file is not the destination object yet: remember whether a
    # real promote is needed so the destination read-back below is meaningful.
    was_staged = staged.staged_path is not None
    staged.promote()             # Phase 3 — published on the destination

    if was_staged and dst.sha256(key) != staged.sha256:
        # The promote silently mis-wrote: remove the bad copy and keep the
        # source, so the archive never ends up with neither object intact.
        try:
            dst.delete(key)
        except Exception as exc:  # the raise below is the real signal
            log.warning("failed_move_destination_cleanup_failed error_type=%s",
                        type(exc).__name__)
        raise StorageError(
            f"cross-backend move verification failed for {key!r}: the "
            f"destination copy does not match the source"
        )

    src.delete(key)              # Phase 4 — only now
    return True


def move_study_to_backend(study_uid: str, to_backend: str) -> dict:
    """Relocate every object of a study (instances + thumbnails) to a named
    backend and update the DB rows. Returns ``{"moved", "from_backend",
    "already"}``. Raises KeyError for an unknown study or backend.

    Stage-then-publish (A-1/A-14/FD-1): every object is staged on the target and
    verified **before** the database is touched, and the source copies are only
    unlinked after the transaction that points at the target has committed. A
    failure at any point therefore leaves the study wholly on its original
    backend with the database still describing it — never split across two
    backends with the DB lying about where the objects are.
    """
    from sqlalchemy import select

    from app.db.models import Instance, Series, Study
    from app.db.session import SyncSessionLocal
    from app.services.storage import get_storage_backend

    get_storage_backend(to_backend)  # validate the target exists + is enabled
    with SyncSessionLocal() as session:
        study = session.execute(
            select(Study).where(Study.study_instance_uid == study_uid)
        ).scalar_one_or_none()
        if study is None:
            raise KeyError(f"study not found: {study_uid}")
        src = study.storage_backend
        if src == to_backend:
            return {"moved": 0, "from_backend": src, "already": True}
        instances = session.execute(
            select(Instance)
            .join(Series, Instance.series_id == Series.id)
            .where(Series.study_id == study.id)
        ).scalars().all()

        src_backend = _resolve(src)
        staged: list[StagedObject] = []
        staged_keys: list[str] = []
        moved = 0
        try:
            for inst in instances:
                instance_moved = False
                for key in (inst.file_path, thumbnail_key(inst.sop_instance_uid)):
                    if not key or not src_backend.exists(key):
                        continue
                    obj = _facade().stage_bytes(key, src_backend.read_bytes(key),
                                                backend_name=to_backend, charge_bytes=0)
                    try:
                        obj.verify()
                    except Exception:
                        obj.discard()
                        raise
                    staged.append(obj)
                    staged_keys.append(key)
                    if key == inst.file_path:
                        instance_moved = True
                if instance_moved:
                    moved += 1
                inst.storage_backend = to_backend
            study.storage_backend = to_backend

            # Publish before the commit: the committed rows point at the target,
            # so a promote failure must abort the transaction rather than leave
            # the database referencing an object that was never written there.
            publish_staged(staged)
            session.commit()
        except Exception:
            # The transaction rolled back, so the database still points at the
            # source backend: drop the staged temp files and any destination
            # copies this call already published, leaving the study whole.
            discard_staged(staged)
            for key in staged_keys:
                try:
                    _resolve(to_backend).delete(key)
                except Exception as exc:  # the raise below is the signal
                    log.warning("failed_move_destination_cleanup_failed error_type=%s",
                                type(exc).__name__)
            raise

        # Phase 4 — the database now points at the target backend, so the
        # source copies can go. A failure here leaves an unreferenced duplicate.
        for key in staged_keys:
            try:
                src_backend.delete(key)
            except Exception as exc:
                log.warning(
                    "moved_source_object_cleanup_failed error_type=%s",
                    type(exc).__name__,
                )
        return {"moved": moved, "from_backend": src, "already": False}


def compress(key: str, method: str = "RLE", backend_name: str | None = None) -> dict:
    """Losslessly compress a stored instance in place (backend-agnostic).

    Stage-then-publish (A-1/A-14/FD-1): the compressed payload is produced in a
    temp file, staged through the backend and read back **before** it replaces
    the canonical object. A crash, a bad encode or a failed upload therefore
    leaves the original object intact — the previous remote-backend path
    overwrote the key directly, so a failure mid-upload destroyed the only copy,
    and the local path replaced the file without ever verifying the result.
    """
    from app.services.compression_service import compress_file

    bk = _resolve(backend_name)

    # Stream the source into a scratch file (never the object's own directory,
    # so the archive tree is untouched until promotion) and compress there.
    fd, tmp = tempfile.mkstemp(suffix=".dcm")
    os.close(fd)
    try:
        with open(tmp, "wb") as out, bk.read_stream(key) as src_fh:
            shutil.copyfileobj(src_fh, out)
        result = compress_file(tmp, method)
        if result["already_compressed"]:
            return result
        with open(tmp, "rb") as fh:
            data = fh.read()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)

    staged = _facade().stage_bytes(key, data, backend_name=backend_name, charge_bytes=0)
    try:
        staged.verify()
    except Exception:
        staged.discard()
        raise
    staged.promote()

    result["compressed_bytes"] = bk.size(key)
    return result
