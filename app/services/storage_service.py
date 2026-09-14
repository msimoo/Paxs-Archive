"""Storage service — facade over the pluggable :class:`StorageBackend`.

Object layout (keys are POSIX relative paths, portable across backends):

    {StudyInstanceUID}/{SeriesInstanceUID}/{SOPInstanceUID}.dcm
    .thumbnails/{SOPInstanceUID}.png

Keys are what the DB stores in ``instances.file_path``. Storing under the SOP
UID makes re-import idempotent: the same instance arriving twice simply
overwrites its own object.

Backend selection (multi-storage):

* ``backend_name=None``  -> the environment-configured backend
  (``STORAGE_BACKEND`` / ``S3_*``) — the implicit default.
* ``backend_name="name"`` -> a named backend registered in the
  ``storage_backends`` table (Admin → Storage). Instances record their
  backend in ``instances.storage_backend``; every read/delete path passes it
  through so objects resolve to the backend that actually holds them.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from app.core.config import settings
from app.services.storage import get_storage_backend

from app.services.quota_service import check_quota_before_write
from app.services.storage.backend import StorageError

log = logging.getLogger("paxs.storage")

# DICOM UIDs are [0-9.] only; anything else is untrusted input that could
# escape the storage root via path components (.., /, drive letters).
_UID_OK = re.compile(r"^[0-9.]{1,64}$")
_UID_STRIP = re.compile(r"[^0-9A-Za-z.-]")


def _safe_component(value: str, fallback: str = "unknown") -> str:
    value = str(value or "").strip()
    if _UID_OK.match(value):
        return value
    cleaned = _UID_STRIP.sub("_", value)[:64]
    if not cleaned or cleaned in (".", ".."):
        # Keep determinism + uniqueness for malformed values.
        return f"{fallback}_{hashlib.sha1(value.encode('utf-8', 'replace')).hexdigest()[:20]}"
    return cleaned


# --------------------------------------------------------------------------- #
# Key construction
# --------------------------------------------------------------------------- #
def instance_key(study_uid: str, series_uid: str, sop_uid: str) -> str:
    return (
        f"{_safe_component(study_uid, 'study')}/"
        f"{_safe_component(series_uid, 'series')}/"
        f"{_safe_component(sop_uid, 'sop')}.dcm"
    )


def thumbnail_key(sop_uid: str) -> str:
    return f".thumbnails/{_safe_component(sop_uid, 'sop')}.png"


# --------------------------------------------------------------------------- #
# Backend resolution
# --------------------------------------------------------------------------- #
def _resolve(backend_name: str | None = None):
    """The backend to use: the named one when given, else the env default."""
    return get_storage_backend(backend_name) if backend_name else get_storage_backend()


def default_backend_name() -> str | None:
    """Name of the registered default backend, or None (use the env backend)."""
    from sqlalchemy import select

    from app.db.models import StorageBackendConfig
    from app.db.session import SyncSessionLocal

    with SyncSessionLocal() as session:
        row = session.execute(
            select(StorageBackendConfig).where(StorageBackendConfig.is_default.is_(True))
        ).scalar_one_or_none()
    return row.name if row is not None else None


def resolve_study_backend(study_uid: str) -> str | None:
    """Backend a study's objects live on: its recorded backend if it already
    exists, otherwise the registered default (env backend when none)."""
    from sqlalchemy import select

    from app.db.models import Study
    from app.db.session import SyncSessionLocal

    with SyncSessionLocal() as session:
        name = session.execute(
            select(Study.storage_backend).where(Study.study_instance_uid == study_uid)
        ).scalar_one_or_none()
    if name:
        return name
    return default_backend_name()


def backend():
    """The active storage backend (lazily created, cached)."""
    return get_storage_backend()


# --------------------------------------------------------------------------- #
# Backend accessors
# --------------------------------------------------------------------------- #
def local_path(key: str, backend_name: str | None = None) -> str | None:
    """Real filesystem path for `key`, or None on remote backends."""
    return _resolve(backend_name).local_path(key)


def exists(key: str, backend_name: str | None = None) -> bool:
    return _resolve(backend_name).exists(key)


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


def sha256_of(key: str, backend_name: str | None = None) -> str:
    # Note: on remote (S3) backends this re-reads the whole object; used by the
    # audit trail and cheap enough for archive-scale writes.
    return _resolve(backend_name).sha256(key)


def move_object(key: str, dest_key: str, backend_name: str | None = None) -> bool:
    """Move one object to a new key on the same backend (tiering)."""
    return _resolve(backend_name).move(key, dest_key)


def delete_file(key: str, backend_name: str | None = None) -> bool:
    return _resolve(backend_name).delete(key)


def delete_instance_files(study_uid: str, series_uid: str, sop_uid: str,
                          backend_name: str | None = None) -> None:
    bk = _resolve(backend_name)
    bk.delete(instance_key(study_uid, series_uid, sop_uid))
    bk.delete(thumbnail_key(sop_uid))


def delete_tree(prefix: str, backend_name: str | None = None) -> None:
    _resolve(backend_name).delete_tree(prefix)


def delete_study_files(study_uid: str, backend_name: str | None = None) -> None:
    _resolve(backend_name).delete_tree(_safe_component(study_uid, "study"))


def storage_usage_bytes(backend_name: str | None = None) -> int:
    return _resolve(backend_name).usage_bytes()


# --------------------------------------------------------------------------- #
# Cached usage counters (scalability)
# --------------------------------------------------------------------------- #
# storage_usage_bytes() walks the storage tree (O(objects) on the local
# backend; a bucket listing on S3). The dashboard /stats endpoint calls it on
# every page load, which becomes a real cost past ~10^5 objects. The cached
# variant keeps a TTL cache: Redis when available (shared across API nodes
# behind HAProxy), else an in-process dict. TTL is set by
# STATS_USAGE_CACHE_SECONDS; 0 disables the cache entirely.

_usage_cache: dict[tuple[str | None, str], tuple[float, int]] = {}


def cached_storage_usage_bytes(backend_name: str | None = None) -> int:
    """storage_usage_bytes() with a TTL cache (Redis-first, in-process
    fallback). Safe to call from async endpoints: the underlying walk is I/O
    bound, so callers should still run it via asyncio.to_thread() when the
    cache is cold.
    """
    ttl = getattr(settings, "STATS_USAGE_CACHE_SECONDS", 60)
    if not ttl:
        return storage_usage_bytes(backend_name)

    cache_key = f"paxs:usage:{(backend_name or 'default')}"

    # 1) Redis (shared across API nodes).
    from app.services.redis_service import cache_get, cache_set

    redis_val = cache_get(cache_key)
    if redis_val is not None:
        try:
            return int(redis_val)
        except (TypeError, ValueError):
            pass

    # 2) In-process cache.
    now = time.monotonic()
    hit = _usage_cache.get((backend_name, "local"))
    if hit is not None and now - hit[0] < ttl:
        return hit[1]

    # 3) Cold: compute and store in both caches.
    total = storage_usage_bytes(backend_name)
    _usage_cache[(backend_name, "local")] = (now, total)
    cache_set(cache_key, str(total), expire_seconds=ttl)
    return total


def invalidate_usage_cache(backend_name: str | None = None) -> None:
    """Drop cached usage counters (called after deletes/tiering so /stats
    reflects the change immediately instead of waiting out the TTL)."""
    _usage_cache.pop((backend_name, "local"), None)
    from app.services.redis_service import cache_delete_pattern

    cache_delete_pattern(f"paxs:usage:{(backend_name or 'default')}")


def presigned_url(key: str, expires_seconds: int | None = None,
                  backend_name: str | None = None) -> str | None:
    """Short-lived direct-download URL (S3), or None for the local backend
    (the app serves those itself via WADO-URI / WADO-RS)."""
    return _resolve(backend_name).presigned_url(
        key, expires_seconds or settings.S3_PRESIGN_EXPIRES
    )


# --------------------------------------------------------------------------- #
# Higher-level operations
# --------------------------------------------------------------------------- #
def save_bytes_with_quota(key: str, data: bytes, tenant_id: int | None,
                          backend_name: str | None = None) -> None:
    """Persist raw bytes with quota enforcement.

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
    _resolve(backend_name).save_bytes(key, data)


def save_dataset(ds: Dataset, backend_name: str | None = None) -> tuple[str, str]:
    """Persist a Dataset via the given (or default) backend. Returns (key, sha256).

    Note: This version does NOT enforce quotas because tenant context is not
    available at this low level. Callers that have tenant context should use
    save_dataset_with_quota() instead.
    """
    study_uid = str(getattr(ds, "StudyInstanceUID", "UNKNOWN"))
    series_uid = str(getattr(ds, "SeriesInstanceUID", "UNKNOWN"))
    sop_uid = str(getattr(ds, "SOPInstanceUID", "UNKNOWN"))

    # A received dataset normally carries file_meta; be defensive anyway.
    if getattr(ds, "file_meta", None) is None:
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = str(getattr(ds, "SOPClassUID", ""))
        meta.MediaStorageSOPInstanceUID = sop_uid
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.file_meta = meta

    key = instance_key(study_uid, series_uid, sop_uid)
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    bk = _resolve(backend_name)
    bk.save_bytes(key, buf.getvalue())
    return key, bk.sha256(key)


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
    """
    study_uid = str(getattr(ds, "StudyInstanceUID", "UNKNOWN"))
    series_uid = str(getattr(ds, "SeriesInstanceUID", "UNKNOWN"))
    sop_uid = str(getattr(ds, "SOPInstanceUID", "UNKNOWN"))

    if getattr(ds, "file_meta", None) is None:
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = str(getattr(ds, "SOPClassUID", ""))
        meta.MediaStorageSOPInstanceUID = sop_uid
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.file_meta = meta

    key = instance_key(study_uid, series_uid, sop_uid)
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    data = buf.getvalue()

    # Quota check before write
    allowed, infos = check_quota_before_write(tenant_id, backend_name, len(data))
    if not allowed:
        exceeded = [f"{i.scope_type}:{i.scope_id}" for i in infos if i.is_exceeded]
        raise StorageError(f"Storage quota exceeded for {', '.join(exceeded)}")

    bk = _resolve(backend_name)
    bk.save_bytes(key, data)
    return key, bk.sha256(key)


def move_to_backend(key: str, to_backend: str, from_backend: str | None = None) -> bool:
    """Copy one object to another backend and delete the source (cross-backend
    move). Used by Study → Move to backend tiering. ``from_backend=None``
    means the object currently lives on the env backend."""
    if to_backend == from_backend:
        return True
    src = _resolve(from_backend)
    dst = get_storage_backend(to_backend)
    if not src.exists(key):
        return False
    dst.save_bytes(key, src.read_bytes(key))
    src.delete(key)
    return True


def move_study_to_backend(study_uid: str, to_backend: str) -> dict:
    """Relocate every object of a study (instances + thumbnails) to a named
    backend and update the DB rows. Returns ``{"moved", "from_backend",
    "already"}``. Raises KeyError for an unknown study or backend."""
    from sqlalchemy import select

    from app.db.models import Instance, Series, Study
    from app.db.session import SyncSessionLocal

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
        moved = 0
        moved_keys: list[str] = []
        try:
            for inst in instances:
                if not inst.file_path:
                    continue
                if move_to_backend(inst.file_path, to_backend, src):
                    moved += 1
                    moved_keys.append(inst.file_path)
                if move_to_backend(thumbnail_key(inst.sop_instance_uid), to_backend, src):
                    moved_keys.append(thumbnail_key(inst.sop_instance_uid))
                inst.storage_backend = to_backend
            study.storage_backend = to_backend
            session.commit()
        except Exception:
            # The DB still points at `src`; roll the already-moved objects back
            # so the study is never split across backends with the DB lying.
            for key in reversed(moved_keys):
                try:
                    move_to_backend(key, src, to_backend)
                except Exception:
                    pass
            raise
        return {"moved": moved, "from_backend": src, "already": False}


def compress(key: str, method: str = "RLE", backend_name: str | None = None) -> dict:
    """Losslessly compress a stored instance in place (backend-agnostic).

    Local backend: compress the real file. Remote backend: materialize to a
    temp file, compress, upload the result back.
    """
    from app.services.compression_service import compress_file

    bk = _resolve(backend_name)
    local = bk.local_path(key)
    if local is not None:
        result = compress_file(local, method)
        result["compressed_bytes"] = bk.size(key)
        return result

    import os
    import tempfile

    data = bk.read_bytes(key)
    fd, tmp = tempfile.mkstemp(suffix=".dcm")
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        result = compress_file(tmp, method)
        with open(tmp, "rb") as fh:
            bk.save_bytes(key, fh.read())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    result["compressed_bytes"] = bk.size(key)
    return result
