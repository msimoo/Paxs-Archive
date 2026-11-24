"""Single-object access, streaming reads/writes, and destructive deletes.

Two invariants live here:

* **C-7 streaming** — large objects are consumed or produced chunk-wise
  (:func:`iter_bytes`, :func:`write_stream`); ``read_bytes`` is for objects that
  are bounded by contract.
* **C-17 quota accounting** — a delete releases exactly the bytes that left the
  archive, measured from the object immediately before the unlink, and a delete
  that removes nothing decrements nothing (so an orphan retry is safe).

``delete_file`` is resolved through the ``storage_service`` facade at call time
(see :func:`_facade`): the regression suite patches that module's attribute and
expects a tree delete to observe the substitution.
"""

from __future__ import annotations

import io
import logging
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterable
from types import ModuleType
from typing import BinaryIO

import pydicom
from pydicom.dataset import Dataset

from app.core.config import settings
from app.services.quota_service import decrement_usage
from app.services.storage.backend import DEFAULT_CHUNK_SIZE
from app.services.storage.keys import instance_key, thumbnail_key, validate_component

# `_resolve` here is the facade-routed seam (see resolve._resolve_seam): the
# fault-injection suite patches ``storage_service._resolve``.
from app.services.storage.resolve import _resolve_seam as _resolve

log = logging.getLogger("paxs.storage")

_FACADE = "app.services.storage_service"


def _facade() -> ModuleType:
    """The public ``app.services.storage_service`` namespace, resolved per call.

    ``delete_file`` is patched on that module by the atomicity tests, and a
    study/tree delete calls it once per object — so the lookup has to happen at
    call time, not at import time.
    """
    return sys.modules[_FACADE]


# --------------------------------------------------------------------------- #
# Backend accessors
# --------------------------------------------------------------------------- #
def local_path(key: str, backend_name: str | None = None) -> str | None:
    """Real filesystem path for `key`, or None on remote backends."""
    return _resolve(backend_name).local_path(key)


def exists(key: str, backend_name: str | None = None) -> bool:
    return _resolve(backend_name).exists(key)


def read_stream(key: str, backend_name: str | None = None) -> BinaryIO:
    """Open a backend-native readable stream without materializing the object."""
    return _resolve(backend_name).read_stream(key)


def size(key: str, backend_name: str | None = None) -> int:
    return _resolve(backend_name).size(key)


def save_bytes(key: str, data: bytes, backend_name: str | None = None) -> None:
    _resolve(backend_name).save_bytes(key, data)


def read_bytes(key: str, backend_name: str | None = None) -> bytes:
    return _resolve(backend_name).read_bytes(key)


def read_dataset(key: str, stop_before_pixels: bool = False,
                 backend_name: str | None = None) -> Dataset:
    """Read a stored instance into a pydicom Dataset (works on any backend).

    The bytes are materialized in memory; pydicom keeps the buffer alive for
    lazy pixel access, so downstream `pixel_array`/`save_as` calls work.
    """
    data = _resolve(backend_name).read_bytes(key)
    return pydicom.dcmread(io.BytesIO(data), stop_before_pixels=stop_before_pixels)


def read_dataset_with_timings(
    key: str,
    stop_before_pixels: bool = False,
    backend_name: str | None = None,
) -> tuple[Dataset, float, float]:
    """Read one DICOM object and separately time object I/O and dataset parsing.

    This is used only by opt-in diagnostics so the ordinary read path keeps its
    existing interface and does not pay for phase timing.
    """
    started = time.perf_counter()
    data = _resolve(backend_name).read_bytes(key)
    storage_read_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    dataset = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=stop_before_pixels)
    dataset_read_ms = (time.perf_counter() - started) * 1000
    return dataset, storage_read_ms, dataset_read_ms


def sha256_of(key: str, backend_name: str | None = None) -> str:
    # Note: on remote (S3) backends this re-reads the whole object; used by the
    # audit trail and cheap enough for archive-scale writes.
    return _resolve(backend_name).sha256(key)


def move_object(key: str, dest_key: str, backend_name: str | None = None) -> bool:
    """Move one object to a new key on the same backend (tiering)."""
    return _resolve(backend_name).move(key, dest_key)


def presigned_url(key: str, expires_seconds: int | None = None,
                  backend_name: str | None = None) -> str | None:
    """Short-lived direct-download URL (S3), or None for the local backend
    (the app serves those itself via WADO-URI / WADO-RS)."""
    return _resolve(backend_name).presigned_url(
        key, expires_seconds or settings.S3_PRESIGN_EXPIRES
    )


# --------------------------------------------------------------------------- #
# Streaming accessors (C-7 / C-8 / C-10)
# --------------------------------------------------------------------------- #
async def iter_bytes(key: str, backend_name: str | None = None,
                     chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
    """Yield an object's bytes in chunks, never buffering it (C-7).

    The streaming read every large-object path must use instead of
    :func:`read_bytes`: an 80 MB (or 8 GB) instance is consumed one
    ``chunk_size`` piece at a time.
    """
    async for chunk in _resolve(backend_name).iter_bytes(key, chunk_size):
        yield chunk


async def write_stream(key: str, stream: AsyncIterator[bytes],
                       backend_name: str | None = None) -> int:
    """Stream an async chunk iterator into ``key``; return bytes written (C-7).

    Wraps the backend's atomic streaming writer (temp file + rename on a
    filesystem, managed multipart upload on S3), so the object never exists in
    RAM and a reader never observes a partial write.
    """
    return await _resolve(backend_name).write_stream(key, stream)


def upload_fileobj(key: str, file_obj: BinaryIO,
                   backend_name: str | None = None) -> None:
    """Upload an open binary file object to ``key`` via the native uploader (C-7)."""
    _resolve(backend_name).upload_fileobj(key, file_obj)


def staging_dir(backend_name: str | None = None) -> str:
    """Directory a backend wants spooled parts written to (C-10).

    The local backend returns ``STORAGE_ROOT/.tmp`` so a later promote is a
    same-filesystem rename; a backend without a staging area (S3) returns the
    system temp directory, where the spool is uploaded from instead.
    """
    bk = _resolve(backend_name)
    getter = getattr(bk, "staging_dir", None)
    if getter is not None:
        return str(getter())
    return tempfile.gettempdir()


# --------------------------------------------------------------------------- #
# Destructive operations (C-17 accounting)
# --------------------------------------------------------------------------- #
def delete_file(key: str, backend_name: str | None = None, *,
                tenant_id: int | None = None,
                charge_release_bytes: int | None = None) -> bool:
    """Unlink one object and give its bytes back to the quota counters (C-17).

    Returns whether an object was actually removed. The decrement is keyed to the
    **verified** size of the object that was deleted — measured here from the
    backend immediately before the unlink, not taken from a caller's guess — so
    ``used_bytes`` falls by exactly what left the archive. A delete of a key that
    does not exist removes nothing and decrements nothing, which is what makes
    the orphan-retry path safe to run repeatedly.

    ``charge_release_bytes`` lets a caller that already measured the object (and
    whose key is a *destination* rather than the canonical one, as in a tier
    rollback) pin the figure; otherwise it is measured from the object.
    """
    bk = _resolve(backend_name)
    size = charge_release_bytes
    if size is None:
        try:
            size = int(bk.size(key))
        except (OSError, ValueError):  # an unmeasurable key still gets deleted
            # An object that cannot be measured is one we cannot account for; a
            # failed delete would then leave PHI behind, so still attempt the
            # unlink and simply charge back nothing (reconciliation repairs the
            # counter, a surviving object cannot be repaired).
            log.warning("storage_size_unavailable_before_delete; releasing no quota")
            size = 0
    removed = bk.delete(key)
    if removed and size:
        try:
            decrement_usage(tenant_id, backend_name, size)
        except Exception as exc:  # accounting must never fail the delete
            log.warning("deleted_object_quota_release_failed error_type=%s",
                        type(exc).__name__)
    return removed


def delete_instance_files(study_uid: str, series_uid: str, sop_uid: str,
                          backend_name: str | None = None, *,
                          tenant_id: int | None = None,
                          instance_size: int | None = None) -> None:
    """Delete one instance's object (and its thumbnail).

    ``instance_size`` is the size the database recorded for the instance
    (``instances.file_size_bytes``) and is the figure charged back to the quota,
    so the counter is decremented by exactly the bytes the row accounted for.
    """
    key = instance_key(study_uid, series_uid, sop_uid)
    _facade().delete_file(key, backend_name, tenant_id=tenant_id,
                          charge_release_bytes=instance_size)
    # The thumbnail is derived, not an instance object, so it is not charged.
    _resolve(backend_name).delete(thumbnail_key(sop_uid))


def delete_tree(prefix: str, backend_name: str | None = None, *,
                objects: Iterable[tuple[str, int]] | None = None,
                tenant_id: int | None = None) -> int:
    """Recursively delete a prefix, releasing each known object's bytes (C-17).

    ``objects`` is an iterable of ``(key, size)`` for the objects the caller
    knows about (from the database) — the tree walk cannot know a size per
    object without stating each one, so the caller supplies what it indexed. Each
    is charged back only if it was actually present. Returns the bytes released.

    When ``objects`` is omitted the recursive delete still runs, but nothing is
    credited back: the quota counters would then be left high until
    :func:`quota_service.recompute_tenant_storage` next runs. Every production
    caller passes ``objects``; the fallback exists so a maintenance script that
    only wants the bytes gone cannot be blocked by accounting.
    """
    released = 0
    for key, size in (objects or ()):
        if _facade().delete_file(
            key, backend_name, tenant_id=tenant_id, charge_release_bytes=size
        ):
            released += size
    _resolve(backend_name).delete_tree(prefix)
    return released


def delete_study_files(study_uid: str, backend_name: str | None = None, *,
                       objects: Iterable[tuple[str, int]] | None = None,
                       tenant_id: int | None = None) -> int:
    """Delete every object under a study, releasing their bytes (C-17).

    Validated, not sanitised: a destructive recursive delete must never run
    against a rewritten or dot-segment prefix (C05). Returns the bytes released.
    """
    prefix = validate_component(study_uid, field="StudyInstanceUID")
    return delete_tree(prefix, backend_name, objects=objects, tenant_id=tenant_id)
