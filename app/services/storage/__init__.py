"""Storage backends and the backend factory.

Selection (multi-storage):

* ``get_storage_backend()`` — the environment-configured backend
  (``STORAGE_BACKEND`` = ``local`` | ``s3``, with the ``S3_*`` settings). This
  is the implicit default and the only backend on a single-store install.
* ``get_storage_backend("name")`` — a named backend registered in the
  ``storage_backends`` table (Admin → Storage). Instances record which backend
  holds them in ``instances.storage_backend``, and every read/delete path passes
  that name through, so an object always resolves to the store that has it.

Instances are cached per name in a bounded LRU; ``reset_storage_backend()``
drops the cache so a configuration change takes effect without a restart.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from app.core.config import settings
from app.services.storage.backend import (
    StorageAccessViolation,
    StorageBackend,
    StorageError,
)
from app.services.storage.local import LocalStorageBackend

# Importing the S3 backend is safe offline: boto3 itself is imported lazily
# inside S3StorageBackend._s3(), so this module stays importable without it.
from app.services.storage.s3 import S3StorageBackend

log = logging.getLogger("paxs.storage")

__all__ = [
    "LocalStorageBackend",
    "S3StorageBackend",
    "StorageAccessViolation",
    "StorageBackend",
    "StorageError",
    "get_storage_backend",
    "reset_storage_backend",
]

_ENV_KEY = "__env__"
_CACHE: OrderedDict[str, StorageBackend] = OrderedDict()
_LOCK = threading.Lock()


def _build_env_backend() -> StorageBackend:
    """The backend named by ``STORAGE_BACKEND`` / ``S3_*``."""
    kind = (settings.STORAGE_BACKEND or "local").strip().lower()
    if kind == "s3":
        return S3StorageBackend(
            bucket=settings.S3_BUCKET,
            endpoint_url=settings.S3_ENDPOINT_URL,
            region=settings.S3_REGION,
            access_key=settings.S3_ACCESS_KEY,
            secret_key=settings.S3_SECRET_KEY,
            presign_expires=settings.S3_PRESIGN_EXPIRES,
        )
    if kind != "local":
        raise StorageError(f"unknown STORAGE_BACKEND {kind!r} (expected 'local' or 's3')")
    return LocalStorageBackend(settings.STORAGE_ROOT, name="local")


def _build_named_backend(name: str) -> StorageBackend:
    """A backend registered in the ``storage_backends`` table."""
    from sqlalchemy import select

    from app.db.models import StorageBackendConfig
    from app.db.session import SyncSessionLocal

    with SyncSessionLocal() as session:
        row = session.execute(
            select(StorageBackendConfig).where(StorageBackendConfig.name == name)
        ).scalar_one_or_none()
        if row is None:
            # KeyError (not StorageError): an unknown *name* is a lookup miss,
            # which callers such as the study-tiering path already handle.
            raise KeyError(f"unknown storage backend: {name!r}")
        if not row.enabled:
            raise StorageError(f"storage backend {name!r} is disabled")
        kind = (row.kind or "").strip().lower()
        if kind == "s3":
            return S3StorageBackend(
                bucket=row.bucket or settings.S3_BUCKET,
                endpoint_url=row.endpoint_url or settings.S3_ENDPOINT_URL,
                region=row.region or settings.S3_REGION,
                access_key=row.access_key or settings.S3_ACCESS_KEY,
                secret_key=row.secret_key or settings.S3_SECRET_KEY,
                presign_expires=row.presign_expires or settings.S3_PRESIGN_EXPIRES,
                name=name,
            )
        if kind != "local":
            raise StorageError(f"storage backend {name!r} has unknown kind {kind!r}")
        return LocalStorageBackend(row.root_path or settings.STORAGE_ROOT, name=name)


def get_storage_backend(name: str | None = None) -> StorageBackend:
    """Return a named or environment backend within the configured cache cap."""
    cache_key = name or _ENV_KEY
    with _LOCK:
        maximum = getattr(settings, "STORAGE_BACKEND_CACHE_MAX_ENTRIES", 128)
        _trim_cache_locked(maximum)
        if maximum == 0:
            return _build_named_backend(name) if name else _build_env_backend()
        cached = _CACHE.get(cache_key)
        if cached is not None:
            _CACHE.move_to_end(cache_key)
            return cached
        backend = _build_named_backend(name) if name else _build_env_backend()
        _CACHE[cache_key] = backend
        _trim_cache_locked(maximum)
        return backend


def _trim_cache_locked(maximum: int) -> None:
    """Enforce the backend cache cap while ``_LOCK`` is held."""
    while len(_CACHE) > maximum:
        _CACHE.popitem(last=False)


def reset_storage_backend() -> None:
    """Drop cached backends so the next lookup re-reads the configuration."""
    with _LOCK:
        _CACHE.clear()
