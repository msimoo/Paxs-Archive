"""VNA (Vendor Neutral Archive) multi-tenancy helpers.

A tenant is an institution/department whose studies are isolated from every
other tenant.  Isolation is keyed off two inputs:

  * a DICOM **AE title** — mapped via `tenant_ae_titles`; associations from a
    mapped title ingest into and query only that tenant's data;
  * a **user** — `users.tenant_id` scopes REST/QIDO browsing (admins see all).

Anything unmapped / unassigned keeps the legacy *unscoped* scope
(``Study.tenant_id IS NULL``), so pre-VNA archives behave exactly as before.
"""

from __future__ import annotations

import threading

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession

from app.db.models import Tenant, TenantAeTitle, User

# --------------------------------------------------------------------------- #
# In-process AE -> tenant cache.
#
# The mapping is tiny (a handful of AE titles) and only changes through the
# tenants API, so we cache it to avoid one DB round-trip per C-STORE instance
# during bulk ingests.  The API calls invalidate_ae_cache() after any mutation.
# --------------------------------------------------------------------------- #
_AE_CACHE: dict[str, int | None] = {}
_AE_CACHE_LOCK = threading.Lock()


def _ae_key(ae_title: str) -> str:
    return ae_title.upper()


def invalidate_ae_cache() -> None:
    """Drop the cached AE->tenant map (after API mutations)."""
    global _AE_CACHE
    with _AE_CACHE_LOCK:
        _AE_CACHE = {}


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
async def resolve_tenant_id_for_ae(session: AsyncSession, ae_title: str | None) -> int | None:
    """Tenant id for a calling DICOM AE title (None = unmapped -> unscoped)."""
    if not ae_title:
        return None
    key = _ae_key(ae_title)
    with _AE_CACHE_LOCK:
        if key in _AE_CACHE:
            return _AE_CACHE[key]
    result = (
        await session.execute(
            select(TenantAeTitle.tenant_id).where(TenantAeTitle.ae_title == key)
        )
    ).scalar_one_or_none()
    with _AE_CACHE_LOCK:
        _AE_CACHE[key] = result
    return result


def resolve_tenant_id_for_ae_sync(session: SyncSession, ae_title: str | None) -> int | None:
    """Sync twin for pynetdicom handlers running on the sync engine."""
    if not ae_title:
        return None
    key = _ae_key(ae_title)
    with _AE_CACHE_LOCK:
        if key in _AE_CACHE:
            return _AE_CACHE[key]
    result = session.execute(
        select(TenantAeTitle.tenant_id).where(TenantAeTitle.ae_title == key)
    ).scalar_one_or_none()
    with _AE_CACHE_LOCK:
        _AE_CACHE[key] = result
    return result


def scope_from_user(user: User | None) -> tuple[int | None, bool]:
    """(tenant_id, include_all) for a REST caller.

    * ADMIN               -> (None, True)        — every tenant
    * user with tenant    -> (user.tenant_id, False)
    * user w/o tenant     -> (None, False)       — legacy unscoped data only
    * unauthenticated     -> (None, False)       — legacy unscoped data only
    """
    if user is None:
        return None, False
    if user.role == "ADMIN":
        return None, True
    return user.tenant_id, False


async def get_tenant_names(session: AsyncSession) -> dict[int, str]:
    """id -> name map (used to label studies in listings)."""
    rows = (await session.execute(select(Tenant.id, Tenant.name))).all()
    return {tid: name for tid, name in rows}
