"""Named-storage-backend administration (Q-3 / FA-2).

The statements behind ``api/v1/storage.py``. That router configures the archive's
object stores, so it is admin-level and archive-wide — no tenant predicate
applies; the boundary is ``require_admin`` on the routes.

Deliberately a separate module from ``storage_service``: that one owns object
I/O (stage → verify → promote, streaming, quota crediting) and is already the
largest service in the tree. Backend *configuration* rows share nothing with
that machinery except the table name, and AGENTS.md §3.1 asks for one reason to
change per module.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import Instance, StorageBackendConfig


def serialize(row: StorageBackendConfig, instances: int, usage: int) -> dict:
    """API shape for one backend row (never includes the secret itself)."""
    return {
        "name": row.name,
        "kind": row.kind,
        "is_default": bool(row.is_default),
        "enabled": bool(row.enabled),
        "root_path": row.root_path,
        "endpoint_url": row.endpoint_url,
        "region": row.region,
        "bucket": row.bucket,
        "presign_expires": row.presign_expires,
        "instances": instances,
        "usage_bytes": usage,
        "has_secret": bool(row.secret_key),
    }


async def counts_by_backend(db) -> tuple[dict[str | None, int], dict[str | None, int]]:
    """Per-backend instance counts and usage bytes (``None`` = the env backend)."""
    inst_rows = (await db.execute(
        select(Instance.storage_backend, func.count(Instance.id))
        .group_by(Instance.storage_backend)
    )).all()
    usage_rows = (await db.execute(
        select(
            Instance.storage_backend,
            func.coalesce(func.sum(Instance.file_size_bytes), 0),
        ).group_by(Instance.storage_backend)
    )).all()
    return (
        {k: int(n) for k, n in inst_rows},
        {k: int(u) for k, u in usage_rows},
    )


async def list_backends(db) -> list[StorageBackendConfig]:
    """Configured backends, default first then by name."""
    return list((await db.execute(
        select(StorageBackendConfig).order_by(
            StorageBackendConfig.is_default.desc(), StorageBackendConfig.name
        )
    )).scalars().all())


async def get_backend(db, name: str) -> StorageBackendConfig | None:
    return (await db.execute(
        select(StorageBackendConfig).where(StorageBackendConfig.name == name)
    )).scalar_one_or_none()


async def backend_count(db) -> int:
    return (await db.execute(
        select(func.count(StorageBackendConfig.id))
    )).scalar_one()


async def clear_default_flag(db) -> None:
    """Demote every backend — the caller then promotes exactly one."""
    await db.execute(StorageBackendConfig.__table__.update().values(is_default=False))


async def create_backend(db, **fields) -> StorageBackendConfig:
    """Insert a backend row; the caller has already resolved ``is_default``."""
    row = StorageBackendConfig(**fields)
    db.add(row)
    await db.flush()
    return row


async def instances_on(db, name: str) -> int:
    """Instances still referencing ``name`` — the delete guard."""
    return (await db.execute(
        select(func.count(Instance.id)).where(Instance.storage_backend == name)
    )).scalar_one()


async def delete_backend(db, row: StorageBackendConfig) -> None:
    await db.delete(row)
    await db.flush()


def named_backend_count_sync() -> int:
    """Configured-backend count from a sync session (dashboard status payload).

    The health/status renderer is not async, so this cannot reuse
    :func:`backend_count`. Same statement, own session.
    """
    from app.db.models import StorageBackendConfig
    from app.db.session import SyncSessionLocal

    with SyncSessionLocal() as session:
        return int(session.execute(
            select(func.count(StorageBackendConfig.id))
        ).scalar_one())
