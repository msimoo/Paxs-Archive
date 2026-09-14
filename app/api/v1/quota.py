"""Storage Quota Management API.

Admin-only endpoints for managing storage quotas:

    GET    /api/v1/quotas              List all quotas with current usage
    POST   /api/v1/quotas              Create a new quota rule
    GET    /api/v1/quotas/{id}         Get quota details
    PUT    /api/v1/quotas/{id}         Update quota rule
    DELETE /api/v1/quotas/{id}         Delete quota rule
    GET    /api/v1/quotas/status       Current usage vs limits (monitoring)
    GET    /api/v1/quotas/status/tenant/{tenant_id}  Tenant-specific status
    GET    /api/v1/quotas/status/backend/{backend_name}  Backend-specific status

Quotas apply to either a tenant (tenant_id) OR a storage backend (storage_backend_name).
A quota of 0 bytes means unlimited. The warning_threshold_pct (default 80%) triggers
advisory alerts but does not block writes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_admin
from app.db.models import StorageBackendConfig, StorageQuota, Tenant, User
from app.db.session import get_db
from app.services import quota_service

router = APIRouter(prefix="/api/v1/quotas", tags=["Storage Quotas"])


class QuotaCreate(BaseModel):
    tenant_id: int | None = Field(None, description="Tenant ID (omit for backend-scoped)")
    storage_backend_name: str | None = Field(None, description="Backend name (omit for tenant-scoped)")
    max_bytes: int = Field(0, ge=0, description="Maximum bytes (0 = unlimited)")
    warning_threshold_pct: int = Field(80, ge=1, le=100, description="Warning threshold percentage")
    enabled: bool = True


class QuotaUpdate(BaseModel):
    max_bytes: int | None = Field(None, ge=0)
    warning_threshold_pct: int | None = Field(None, ge=1, le=100)
    enabled: bool | None = None


def _serialize_quota(q: StorageQuota, usage_bytes: int) -> dict:
    scope_type = "tenant" if q.tenant_id else "backend"
    scope_id = q.tenant_id if q.tenant_id else (q.storage_backend_name or "default")
    limit = q.max_bytes
    used = usage_bytes
    return {
        "id": q.id,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "tenant_id": q.tenant_id,
        "storage_backend_name": q.storage_backend_name,
        "max_bytes": limit,
        "used_bytes": used,
        "available_bytes": max(0, limit - used) if limit > 0 else None,
        "usage_pct": round((used / limit * 100), 2) if limit > 0 else 0.0,
        "warning_threshold_pct": q.warning_threshold_pct,
        "is_exceeded": limit > 0 and used >= limit,
        "is_warning": limit > 0 and used > 0 and (used / limit * 100) >= q.warning_threshold_pct,
        "enabled": q.enabled,
        "created_at": q.created_at.isoformat() if q.created_at else None,
        "updated_at": q.updated_at.isoformat() if q.updated_at else None,
    }


async def _get_usage_for_quota(db: AsyncSession, q: StorageQuota) -> int:
    from sqlalchemy import func
    from app.db.models import Instance, Series, Study

    if q.tenant_id is not None:
        stmt = (
            select(func.coalesce(func.sum(Instance.file_size_bytes), 0))
            .join(Instance.series)
            .join(Series.study)
            .where(Study.tenant_id == q.tenant_id)
        )
        if q.storage_backend_name:
            stmt = stmt.where(Instance.storage_backend == q.storage_backend_name)
    else:
        stmt = select(func.coalesce(func.sum(Instance.file_size_bytes), 0)).where(
            Instance.storage_backend == q.storage_backend_name
        )
    return int((await db.execute(stmt)).scalar_one() or 0)


@router.get("")
async def list_quotas(db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_admin)):
    """List all quota rules with current usage."""
    quotas = (await db.execute(
        select(StorageQuota).order_by(StorageQuota.id)
    )).scalars().all()

    results = []
    for q in quotas:
        usage = await _get_usage_for_quota(db, q)
        results.append(_serialize_quota(q, usage))
    return JSONResponse(results)


@router.post("", status_code=201)
async def create_quota(body: QuotaCreate,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_admin)):
    """Create a new storage quota rule.

    Exactly one of tenant_id or storage_backend_name must be provided.
    """
    if (body.tenant_id is None) == (body.storage_backend_name is None):
        raise HTTPException(422, "Exactly one of tenant_id or storage_backend_name must be set")

    if body.tenant_id is not None:
        tenant = await db.get(Tenant, body.tenant_id)
        if not tenant:
            raise HTTPException(404, f"Tenant {body.tenant_id} not found")
    if body.storage_backend_name is not None:
        backend = (await db.execute(
            select(StorageBackendConfig).where(StorageBackendConfig.name == body.storage_backend_name)
        )).scalar_one_or_none()
        if not backend:
            raise HTTPException(404, f"Storage backend {body.storage_backend_name!r} not found")

    q = StorageQuota(
        tenant_id=body.tenant_id,
        storage_backend_name=body.storage_backend_name,
        max_bytes=body.max_bytes,
        warning_threshold_pct=body.warning_threshold_pct,
        enabled=body.enabled,
    )
    db.add(q)
    await db.flush()
    await db.commit()
    await db.refresh(q)
    usage = await _get_usage_for_quota(db, q)
    return JSONResponse(_serialize_quota(q, usage), status_code=201)


@router.get("/{quota_id}")
async def get_quota(quota_id: int, db: AsyncSession = Depends(get_db),
                    user: User = Depends(require_admin)):
    q = await db.get(StorageQuota, quota_id)
    if not q:
        raise HTTPException(404, f"Quota {quota_id} not found")
    usage = await _get_usage_for_quota(db, q)
    return JSONResponse(_serialize_quota(q, usage))


@router.put("/{quota_id}")
async def update_quota(quota_id: int, body: QuotaUpdate,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_admin)):
    q = await db.get(StorageQuota, quota_id)
    if not q:
        raise HTTPException(404, f"Quota {quota_id} not found")

    data = body.model_dump(exclude_unset=True)
    for field, val in data.items():
        setattr(q, field, val)
    await db.commit()
    await db.refresh(q)
    usage = await _get_usage_for_quota(db, q)
    return JSONResponse(_serialize_quota(q, usage))


@router.delete("/{quota_id}", status_code=204)
async def delete_quota(quota_id: int, db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_admin)):
    q = await db.get(StorageQuota, quota_id)
    if not q:
        raise HTTPException(404, f"Quota {quota_id} not found")
    await db.delete(q)
    await db.commit()
    return None


@router.get("/status")
async def quota_status(tenant_id: int | None = Query(None),
                       backend_name: str | None = Query(None),
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_admin)):
    """Current quota status (usage vs limits) for monitoring dashboards."""
    status = await quota_service.get_quota_status_async(db, tenant_id, backend_name)
    return JSONResponse(status)


@router.get("/status/tenant/{tenant_id}")
async def tenant_quota_status(tenant_id: int, db: AsyncSession = Depends(get_db),
                              user: User = Depends(require_admin)):
    """Quota status for a specific tenant."""
    status = await quota_service.get_quota_status_async(db, tenant_id=tenant_id)
    return JSONResponse(status)


@router.get("/status/backend/{backend_name}")
async def backend_quota_status(backend_name: str, db: AsyncSession = Depends(get_db),
                               user: User = Depends(require_admin)):
    """Quota status for a specific storage backend."""
    status = await quota_service.get_quota_status_async(db, backend_name=backend_name)
    return JSONResponse(status)
