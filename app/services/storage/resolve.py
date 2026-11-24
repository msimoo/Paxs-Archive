"""Which backend holds an object — and the default when none is recorded.

Multi-storage means a study's objects can live on a named backend registered in
``storage_backends``, not only the environment-configured one. Every read,
write and delete path therefore resolves a backend *name* first; ``None`` means
"the env default" and never "guess".
"""

from __future__ import annotations

import sys

from app.services.storage import get_storage_backend
from app.services.storage.backend import StorageBackend

_FACADE = "app.services.storage_service"


def _resolve(backend_name: str | None = None) -> StorageBackend:
    """The backend to use: the named one when given, else the env default."""
    return get_storage_backend(backend_name) if backend_name else get_storage_backend()


def _resolve_seam(backend_name: str | None = None) -> StorageBackend:
    """Resolve a backend through the public facade — the fault-injection seam.

    The stage-then-publish suite patches ``storage_service._resolve`` to install
    a backend that fails on command, and every write path must observe that.
    Sibling submodules therefore call this wrapper (imported as ``_resolve``)
    instead of binding the real function at import time.
    """
    return sys.modules[_FACADE]._resolve(backend_name)


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


def backend() -> StorageBackend:
    """The active storage backend (lazily created, cached)."""
    return get_storage_backend()


def resolve_backend(backend_name: str | None = None) -> StorageBackend:
    """The backend to use: the named one when given, else the env default.

    Public accessor for callers that need to hand a backend's *native* streaming
    primitives (``read_stream`` / ``upload_fileobj``) a raw file object — e.g.
    the replication sweep, which streams backend-to-backend (C-8).
    """
    return _resolve(backend_name)
