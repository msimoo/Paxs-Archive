"""Multi-storage admin API — configure named storage backends (local + S3/MinIO).

Admin-only (except nothing: everything here touches configuration):

    GET    /api/v1/storage/backends            list backends + usage
    POST   /api/v1/storage/backends            create a named backend
    PATCH  /api/v1/storage/backends/{name}     update enable / default / connection
    POST   /api/v1/storage/backends/{name}/test  probe the connection
    DELETE /api/v1/storage/backends/{name}     delete (guarded: no instances may
                                               reference the backend)
    POST   /api/v1/storage/studies/{study_uid}/move  relocate a study to another
                                               backend (cross-backend copy+delete)

The first backend created becomes the default; at most one row may be the
default. Instances record their backend in ``instances.storage_backend``; every
read/write path passes it through, so objects always resolve to the backend
that actually holds them.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_admin
from app.core.config import settings
from app.db.models import Instance, StorageBackendConfig, User
from app.db.session import get_db
from app.services import storage_service
from app.services.storage import get_storage_backend, reset_storage_backend

router = APIRouter(prefix="/api/v1/storage", tags=["storage"])

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class BackendCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: str = "local"  # local | s3
    root_path: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    bucket: str | None = None
    access_key: str | None = None
    secret_key: str | None = None
    presign_expires: int = 900
    is_default: bool = False


class BackendPatch(BaseModel):
    enabled: bool | None = None
    is_default: bool | None = None
    root_path: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    bucket: str | None = None
    access_key: str | None = None
    secret_key: str | None = None
    presign_expires: int | None = None


class StudyMove(BaseModel):
    target_backend: str = Field(min_length=1, max_length=64)


def _serialize(row, instances: int, usage: int) -> dict:
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


def _counts_by_backend(db) -> tuple[dict[str, int], dict[str, int]]:
    """Per-backend instance counts and usage bytes, keyed by backend name
    (None = env backend)."""

    async def _load():
        inst_rows = (
            await db.execute(
                select(Instance.storage_backend, func.count(Instance.id)).group_by(
                    Instance.storage_backend
                )
            )
        ).all()
        usage_rows = (
            await db.execute(
                select(
                    Instance.storage_backend,
                    func.coalesce(func.sum(Instance.file_size_bytes), 0),
                ).group_by(Instance.storage_backend)
            )
        ).all()
        return (
            {k: int(n) for k, n in inst_rows},
            {k: int(u) for k, u in usage_rows},
        )

    return _load()


@router.get("/backends")
async def list_backends(db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_admin)):
    counts, usage = await _counts_by_backend(db)
    rows = (await db.execute(select(StorageBackendConfig).order_by(
        StorageBackendConfig.is_default.desc(), StorageBackendConfig.name
    ))).scalars().all()
    backends = [
        _serialize(r, counts.get(r.name, 0), usage.get(r.name, 0)) for r in rows
    ]
    return {
        "backends": backends,
        "env_backend": {
            "name": None,
            "kind": (settings.STORAGE_BACKEND or "local").lower(),
            "instances": counts.get(None, 0),
            "usage_bytes": usage.get(None, 0),
        },
    }


@router.post("/backends", status_code=201)
async def create_backend(body: BackendCreate,
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_admin)):
    name = body.name.strip()
    if not _NAME_RE.match(name):
        raise HTTPException(422, "Name must be 1-64 chars of A-Za-z0-9 _ -")
    kind = (body.kind or "local").strip().lower()
    if kind not in ("local", "s3"):
        raise HTTPException(422, "kind must be 'local' or 's3'")
    existing = (await db.execute(
        select(StorageBackendConfig).where(StorageBackendConfig.name == name)
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"Backend {name!r} already exists")

    if kind == "local" and not body.root_path:
        raise HTTPException(422, "root_path is required for local backends")
    if kind == "s3" and not body.bucket:
        raise HTTPException(422, "bucket is required for s3 backends")

    # First backend becomes the default automatically.
    count = (await db.execute(select(func.count(StorageBackendConfig.id)))).scalar_one()
    make_default = body.is_default or count == 0
    if make_default:
        await db.execute(StorageBackendConfig.__table__.update().values(is_default=False))

    row = StorageBackendConfig(
        name=name, kind=kind, is_default=make_default,
        root_path=body.root_path, endpoint_url=body.endpoint_url,
        region=body.region or "us-east-1", bucket=body.bucket,
        access_key=body.access_key, secret_key=body.secret_key,
        presign_expires=body.presign_expires or 900,
    )
    db.add(row)
    await db.flush()
    reset_storage_backend()
    return _serialize(row, 0, 0)


@router.patch("/backends/{name}")
async def patch_backend(name: str, body: BackendPatch,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_admin)):
    row = (await db.execute(
        select(StorageBackendConfig).where(StorageBackendConfig.name == name)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"Backend {name!r} not found")

    if body.is_default is True:
        await db.execute(StorageBackendConfig.__table__.update().values(is_default=False))
        row.is_default = True
    elif body.is_default is False:
        row.is_default = False
    for field, value in {
        "enabled": body.enabled,
        "root_path": body.root_path,
        "endpoint_url": body.endpoint_url,
        "region": body.region,
        "bucket": body.bucket,
        "access_key": body.access_key,
        "secret_key": body.secret_key,
        "presign_expires": body.presign_expires,
    }.items():
        if value is not None:
            setattr(row, field, value)
    await db.flush()
    reset_storage_backend()
    return _serialize(row, 0, 0)


@router.post("/backends/{name}/test")
async def test_backend(name: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_admin)):
    row = (await db.execute(
        select(StorageBackendConfig).where(StorageBackendConfig.name == name)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"Backend {name!r} not found")
    try:
        if row.kind == "s3":
            import time

            backend = get_storage_backend(name)
            client = backend._s3()
            t0 = time.time()
            client.list_objects_v2(Bucket=row.bucket, MaxKeys=1)
            latency_ms = int((time.time() - t0) * 1000)
            return {"ok": True, "kind": "s3", "latency_ms": latency_ms,
                    "detail": f"bucket {row.bucket!r} reachable"}
        # local
        import os
        path = row.root_path or ""
        if not os.path.isdir(path):
            return {"ok": False, "kind": "local", "detail": f"directory not found: {path}"}
        probe = os.path.join(path, ".paxs-probe")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.unlink(probe)
        return {"ok": True, "kind": "local", "latency_ms": 0,
                "detail": f"directory writable: {path}"}
    except Exception as exc:
        return {"ok": False, "kind": row.kind, "detail": str(exc)[:300]}


@router.delete("/backends/{name}", status_code=204)
async def delete_backend(name: str, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_admin)):
    row = (await db.execute(
        select(StorageBackendConfig).where(StorageBackendConfig.name == name)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"Backend {name!r} not found")
    in_use = (
        await db.execute(
            select(func.count(Instance.id)).where(Instance.storage_backend == name)
        )
    ).scalar_one()
    if in_use:
        raise HTTPException(
            409,
            f"{in_use} instance(s) still reference backend {name!r}; "
            "move those studies to another backend first",
        )
    await db.delete(row)
    await db.flush()
    reset_storage_backend()
    return None


@router.post("/studies/{study_uid}/move")
async def move_study(study_uid: str, body: StudyMove,
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_admin)):
    """Relocate a study's objects (instances + thumbnails) to another backend."""
    import asyncio

    # Release this request's transaction so the sync writer thread cannot
    # deadlock against it on SQLite (WAL readers still hold the shared lock).
    await db.commit()
    try:
        result = await asyncio.to_thread(
            storage_service.move_study_to_backend, study_uid, body.target_backend
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, f"Move failed: {exc}") from exc
    reset_storage_backend()
    return {"study_instance_uid": study_uid,
            "target_backend": body.target_backend, **result}
