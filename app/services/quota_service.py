"""Storage Quota Management Service.

Enforces tenant-scoped and backend-scoped storage limits.

Architecture:
  - Quotas are defined in the `storage_quotas` table (StorageQuota model).
  - Each quota applies to either a tenant (tenant_id) OR a backend (storage_backend_name).
  - When both apply, the stricter (lower) limit wins.
  - A quota of 0 means unlimited.
  - warning_threshold_pct (default 80%) triggers advisory alerts but does not block writes.

Integration points:
  - storage_service.save_bytes() / save_dataset() call `check_quota_before_write()`
  - API endpoints expose current usage vs limits and allow CRUD of quota rules.
  - Worker can periodically evaluate quotas and emit warnings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Instance, StorageBackendConfig, StorageQuota, Tenant
from app.db.session import SyncSessionLocal

log = logging.getLogger("paxs.quota")


@dataclass
class QuotaInfo:
    """Aggregated quota information for a scope (tenant or backend)."""
    scope_type: str  # "tenant" | "backend"
    scope_id: str    # tenant.code or backend name
    limit_bytes: int  # 0 = unlimited
    used_bytes: int
    warning_threshold_pct: int
    enabled: bool
    warning_triggered: bool

    @property
    def available_bytes(self) -> int:
        if self.limit_bytes <= 0:
            return 2**63 - 1  # effectively unlimited
        return max(0, self.limit_bytes - self.used_bytes)

    @property
    def usage_pct(self) -> float:
        if self.limit_bytes <= 0:
            return 0.0
        return (self.used_bytes / self.limit_bytes) * 100

    @property
    def is_exceeded(self) -> bool:
        return self.limit_bytes > 0 and self.used_bytes >= self.limit_bytes

    @property
    def is_warning(self) -> bool:
        if self.limit_bytes <= 0:
            return False
        return self.usage_pct >= self.warning_threshold_pct


def get_effective_quotas(tenant_id: int | None = None,
                         backend_name: str | None = None) -> list[StorageQuota]:
    """Fetch all enabled quotas matching the given tenant and/or backend.

    Returns quotas ordered by limit_bytes ASC (strictest first) so the caller
    can simply take the first non-zero limit as the effective limit.
    """
    with SyncSessionLocal() as session:
        stmt = select(StorageQuota).where(StorageQuota.enabled.is_(True))
        if tenant_id is not None:
            stmt = stmt.where(StorageQuota.tenant_id == tenant_id)
        if backend_name is not None:
            stmt = stmt.where(StorageQuota.storage_backend_name == backend_name)
        # Also include "global" quotas (neither tenant nor backend set) if any
        # but those would be unusual; we skip for now to avoid ambiguity.
        quotas = session.execute(
            stmt.order_by(StorageQuota.max_bytes.asc())
        ).scalars().all()
    return list(quotas)


def get_tenant_usage_bytes(tenant_id: int, backend_name: str | None = None) -> int:
    """Calculate current storage usage for a tenant (optionally scoped to a backend)."""
    with SyncSessionLocal() as session:
        stmt = (
            select(func.coalesce(func.sum(Instance.file_size_bytes), 0))
            .join(Instance.series)
            .join(Series.study)
            .where(Study.tenant_id == tenant_id)
        )
        if backend_name:
            stmt = stmt.where(Instance.storage_backend == backend_name)
        return int(session.execute(stmt).scalar_one() or 0)


def get_backend_usage_bytes(backend_name: str | None) -> int:
    """Calculate current storage usage for a backend (all tenants)."""
    # backend_name=None means the env default backend
    with SyncSessionLocal() as session:
        stmt = select(func.coalesce(func.sum(Instance.file_size_bytes), 0)).where(
            Instance.storage_backend == backend_name
        )
        return int(session.execute(stmt).scalar_one() or 0)


def check_quota_before_write(tenant_id: int | None,
                             backend_name: str | None,
                             additional_bytes: int = 0) -> tuple[bool, list[QuotaInfo]]:
    """Check if a write of `additional_bytes` would exceed any applicable quota.

    Returns (allowed, quota_infos). If allowed is False, the write should be rejected.
    quota_infos contains all applicable quotas with current usage for reporting.
    """
    quotas = get_effective_quotas(tenant_id, backend_name)
    if not quotas:
        return True, []

    # Calculate usage for each quota scope
    infos: list[QuotaInfo] = []
    for q in quotas:
        if q.tenant_id is not None:
            used = get_tenant_usage_bytes(q.tenant_id, q.storage_backend_name)
            scope_type = "tenant"
            # Resolve tenant code for reporting
            with SyncSessionLocal() as session:
                tenant = session.get(Tenant, q.tenant_id)
                scope_id = tenant.code if tenant else str(q.tenant_id)
        else:
            used = get_backend_usage_bytes(q.storage_backend_name)
            scope_type = "backend"
            scope_id = q.storage_backend_name or "default"

        info = QuotaInfo(
            scope_type=scope_type,
            scope_id=scope_id,
            limit_bytes=q.max_bytes,
            used_bytes=used + additional_bytes,
            warning_threshold_pct=q.warning_threshold_pct,
            enabled=q.enabled,
            warning_triggered=False,
        )
        info.warning_triggered = info.is_warning
        infos.append(info)

    # Determine if any quota would be exceeded
    allowed = all(not info.is_exceeded for info in infos)

    if not allowed:
        exceeded = [f"{i.scope_type}:{i.scope_id}" for i in infos if i.is_exceeded]
        log.warning("Quota exceeded for write: %s", ", ".join(exceeded))

    # Log warnings
    for info in infos:
        if info.warning_triggered:
            log.warning(
                "Storage quota warning: %s:%s at %.1f%% (threshold %d%%)",
                info.scope_type, info.scope_id, info.usage_pct, info.warning_threshold_pct
            )

    return allowed, infos


def get_quota_status(tenant_id: int | None = None,
                     backend_name: str | None = None) -> list[QuotaInfo]:
    """Get current quota status for reporting/monitoring (does not block)."""
    quotas = get_effective_quotas(tenant_id, backend_name)
    infos: list[QuotaInfo] = []
    for q in quotas:
        if q.tenant_id is not None:
            used = get_tenant_usage_bytes(q.tenant_id, q.storage_backend_name)
            scope_type = "tenant"
            with SyncSessionLocal() as session:
                tenant = session.get(Tenant, q.tenant_id)
                scope_id = tenant.code if tenant else str(q.tenant_id)
        else:
            used = get_backend_usage_bytes(q.storage_backend_name)
            scope_type = "backend"
            scope_id = q.storage_backend_name or "default"

        info = QuotaInfo(
            scope_type=scope_type,
            scope_id=scope_id,
            limit_bytes=q.max_bytes,
            used_bytes=used,
            warning_threshold_pct=q.warning_threshold_pct,
            enabled=q.enabled,
            warning_triggered=used > 0 and q.max_bytes > 0 and (used / q.max_bytes * 100) >= q.warning_threshold_pct,
        )
        infos.append(info)
    return infos


async def get_quota_status_async(db: AsyncSession,
                                 tenant_id: int | None = None,
                                 backend_name: str | None = None) -> list[dict]:
    """Async version for API endpoints."""
    stmt = select(StorageQuota).where(StorageQuota.enabled.is_(True))
    if tenant_id is not None:
        stmt = stmt.where(StorageQuota.tenant_id == tenant_id)
    if backend_name is not None:
        stmt = stmt.where(StorageQuota.storage_backend_name == backend_name)
    quotas = (await db.execute(stmt.order_by(StorageQuota.max_bytes.asc()))).scalars().all()

    results = []
    for q in quotas:
        if q.tenant_id is not None:
            # Tenant-scoped quota
            used_stmt = (
                select(func.coalesce(func.sum(Instance.file_size_bytes), 0))
                .join(Instance.series)
                .join(Series.study)
                .where(Study.tenant_id == q.tenant_id)
            )
            if q.storage_backend_name:
                used_stmt = used_stmt.where(Instance.storage_backend == q.storage_backend_name)
            used = int((await db.execute(used_stmt)).scalar_one() or 0)
            scope_type = "tenant"
            tenant = await db.get(Tenant, q.tenant_id)
            scope_id = tenant.code if tenant else str(q.tenant_id)
        else:
            # Backend-scoped quota
            used = int((
                await db.execute(
                    select(func.coalesce(func.sum(Instance.file_size_bytes), 0))
                    .where(Instance.storage_backend == q.storage_backend_name)
                )
            ).scalar_one() or 0)
            scope_type = "backend"
            scope_id = q.storage_backend_name or "default"

        results.append({
            "quota_id": q.id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "limit_bytes": q.max_bytes,
            "used_bytes": used,
            "available_bytes": max(0, q.max_bytes - used) if q.max_bytes > 0 else None,
            "usage_pct": round((used / q.max_bytes * 100), 2) if q.max_bytes > 0 else 0.0,
            "warning_threshold_pct": q.warning_threshold_pct,
            "is_exceeded": q.max_bytes > 0 and used >= q.max_bytes,
            "is_warning": q.max_bytes > 0 and used > 0 and (used / q.max_bytes * 100) >= q.warning_threshold_pct,
            "enabled": q.enabled,
        })
    return results


# Import at bottom to avoid circular dependency
from app.db.models import Series, Study


__all__ = [
    "QuotaInfo",
    "get_effective_quotas",
    "get_tenant_usage_bytes",
    "get_backend_usage_bytes",
    "check_quota_before_write",
    "get_quota_status",
    "get_quota_status_async",
]
