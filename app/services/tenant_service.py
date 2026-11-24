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
from collections import OrderedDict

from sqlalchemy import event
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession

from app.core.config import settings
from app.db.models import Study, Tenant, TenantAeTitle, User

# --------------------------------------------------------------------------- #
# In-process AE -> tenant cache.
#
# Cache entries avoid one DB round-trip per C-STORE instance during bulk ingest.
# The LRU ceiling protects against arbitrary calling AE titles; a generation
# prevents an in-flight pre-invalidation lookup from repopulating stale policy.
# --------------------------------------------------------------------------- #
_AE_CACHE: OrderedDict[str, int | None] = OrderedDict()
_AE_CACHE_LOCK = threading.Lock()
_AE_CACHE_GENERATION = 0


def _trim_ae_cache_locked(maximum: int) -> None:
    """Enforce the live LRU ceiling while the cache lock is held."""
    while len(_AE_CACHE) > maximum:
        _AE_CACHE.popitem(last=False)


def _ae_cache_get(key: str) -> tuple[bool, int | None, int]:
    """Return a cached value and the generation for a possible DB lookup."""
    with _AE_CACHE_LOCK:
        maximum = max(0, int(settings.DICOM_TENANT_AE_CACHE_MAX_ENTRIES))
        if maximum == 0:
            _AE_CACHE.clear()
            return False, None, _AE_CACHE_GENERATION
        _trim_ae_cache_locked(maximum)
        if key not in _AE_CACHE:
            return False, None, _AE_CACHE_GENERATION
        value = _AE_CACHE[key]
        _AE_CACHE.move_to_end(key)
        return True, value, _AE_CACHE_GENERATION


def _ae_cache_set(key: str, value: int | None, generation: int) -> None:
    """Cache a lookup only if no tenant-mapping invalidation raced it."""
    with _AE_CACHE_LOCK:
        maximum = max(0, int(settings.DICOM_TENANT_AE_CACHE_MAX_ENTRIES))
        if generation != _AE_CACHE_GENERATION or maximum == 0:
            return
        _AE_CACHE[key] = value
        _AE_CACHE.move_to_end(key)
        _trim_ae_cache_locked(maximum)


def _ae_key(ae_title: str) -> str:
    return ae_title.upper()


def invalidate_ae_cache() -> None:
    """Drop the cached AE->tenant map (after API mutations)."""
    global _AE_CACHE_GENERATION
    with _AE_CACHE_LOCK:
        _AE_CACHE.clear()
        _AE_CACHE_GENERATION += 1


def _invalidate_ae_cache_after_commit(_session: SyncSession) -> None:
    """Clear mappings again once their database transaction is visible."""
    invalidate_ae_cache()


def _schedule_ae_cache_invalidation(session: AsyncSession) -> None:
    """Prevent concurrent lookups from caching pre-commit mapping state."""
    event.listen(
        session.sync_session,
        "after_commit",
        _invalidate_ae_cache_after_commit,
        once=True,
    )


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
async def resolve_tenant_id_for_ae(session: AsyncSession, ae_title: str | None) -> int | None:
    """Tenant id for a calling DICOM AE title (None = unmapped -> unscoped)."""
    if not ae_title:
        return None
    key = _ae_key(ae_title)
    cached, value, generation = _ae_cache_get(key)
    if cached:
        return value
    result = (
        await session.execute(
            select(TenantAeTitle.tenant_id).where(TenantAeTitle.ae_title == key)
        )
    ).scalar_one_or_none()
    _ae_cache_set(key, result, generation)
    return result


def resolve_tenant_id_for_ae_sync(session: SyncSession, ae_title: str | None) -> int | None:
    """Sync twin for pynetdicom handlers running on the sync engine."""
    if not ae_title:
        return None
    key = _ae_key(ae_title)
    cached, value, generation = _ae_cache_get(key)
    if cached:
        return value
    result = session.execute(
        select(TenantAeTitle.tenant_id).where(TenantAeTitle.ae_title == key)
    ).scalar_one_or_none()
    _ae_cache_set(key, result, generation)
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
    return dict(rows)


# --------------------------------------------------------------------------- #
# Tenant administration (Q-3 / FA-2)
#
# The statements behind ``api/v1/tenants.py``. That router is admin-level
# tenant *administration* — it defines the isolation boundary rather than being
# scoped by it — so there is no tenant predicate to apply here; the boundary for
# this surface is ``require_admin`` on the routes.
#
# Detaching on delete is explicit rather than left to the FK: SQLite does not
# enforce cascades without ``PRAGMA foreign_keys=ON``, and the intent is
# *preserve the data* (release studies/users back to the legacy unscoped scope),
# not cascade-delete a hospital's studies because its tenant row went away.
# --------------------------------------------------------------------------- #
async def list_tenants(session: AsyncSession) -> list[Tenant]:
    """Every tenant, by name."""
    return list((await session.execute(
        select(Tenant).order_by(Tenant.name)
    )).scalars().all())


async def get_tenant(session: AsyncSession, tenant_id: int) -> Tenant | None:
    return (await session.execute(
        select(Tenant).where(Tenant.id == tenant_id)
    )).scalar_one_or_none()


async def tenant_name_or_code_taken(session: AsyncSession, *,
                                    name: str, code: str) -> bool:
    """True when another tenant already uses ``name`` or ``code``."""
    return (await session.execute(
        select(Tenant.id).where((Tenant.name == name) | (Tenant.code == code))
    )).scalar_one_or_none() is not None


async def tenant_name_taken_by_other(session: AsyncSession, *, name: str,
                                     tenant_id: int) -> bool:
    return (await session.execute(
        select(Tenant.id).where(Tenant.name == name, Tenant.id != tenant_id)
    )).scalar_one_or_none() is not None


async def create_tenant(session: AsyncSession, *, name: str, code: str,
                        description: str | None) -> Tenant:
    """Insert a tenant and reload its server-side defaults.

    ``created_at`` is a MySQL server default, only loaded on an explicit
    refresh — lazy-loading it on access would escape the greenlet.
    """
    tenant = Tenant(name=name, code=code, description=description)
    session.add(tenant)
    await session.flush()
    await session.refresh(tenant)
    return tenant


async def ae_titles_of(session: AsyncSession, tenant_id: int) -> list[str]:
    return list((await session.execute(
        select(TenantAeTitle.ae_title).where(TenantAeTitle.tenant_id == tenant_id)
    )).scalars().all())


async def study_count_of(session: AsyncSession, tenant_id: int) -> int:
    return (await session.execute(
        select(func.count(Study.id)).where(Study.tenant_id == tenant_id)
    )).scalar_one()


async def delete_tenant_cascade(session: AsyncSession, tenant: Tenant) -> None:
    """Delete a tenant, releasing its studies and users to the unscoped scope.

    AE mappings are removed (they are pure configuration); studies and users are
    *detached*, not deleted — their data is preserved and they fall back to the
    legacy NULL-tenant bucket, exactly as an unmapped AE does.
    """
    await session.execute(
        sa_delete(TenantAeTitle).where(TenantAeTitle.tenant_id == tenant.id)
    )
    await session.execute(
        update(Study).where(Study.tenant_id == tenant.id).values(tenant_id=None)
    )
    await session.execute(
        update(User).where(User.tenant_id == tenant.id).values(tenant_id=None)
    )
    await session.delete(tenant)
    invalidate_ae_cache()
    _schedule_ae_cache_invalidation(session)


async def ae_mapping_owner(session: AsyncSession, ae_title: str) -> TenantAeTitle | None:
    """The existing mapping for ``ae_title``, if any (globally unique)."""
    return (await session.execute(
        select(TenantAeTitle).where(TenantAeTitle.ae_title == ae_title)
    )).scalar_one_or_none()


async def map_ae_title(session: AsyncSession, tenant_id: int, ae_title: str) -> None:
    session.add(TenantAeTitle(tenant_id=tenant_id, ae_title=ae_title))
    await session.flush()
    invalidate_ae_cache()
    _schedule_ae_cache_invalidation(session)


async def get_ae_mapping(session: AsyncSession, tenant_id: int,
                         ae_title: str) -> TenantAeTitle | None:
    return (await session.execute(
        select(TenantAeTitle).where(
            TenantAeTitle.tenant_id == tenant_id,
            TenantAeTitle.ae_title == ae_title,
        )
    )).scalar_one_or_none()


async def unmap_ae_title(session: AsyncSession, row: TenantAeTitle) -> None:
    await session.delete(row)
    invalidate_ae_cache()
    _schedule_ae_cache_invalidation(session)


async def assign_user(session: AsyncSession, tenant: Tenant, username: str) -> User | None:
    """Point ``username`` at ``tenant``; ``None`` when there is no such account."""
    target = await get_user_by_username(session, username)
    if target is None:
        return None
    target.tenant_id = tenant.id
    await session.flush()
    return target


async def unassign_user(session: AsyncSession, tenant_id: int,
                        username: str) -> bool:
    """Clear the assignment; False when the user is not in that tenant."""
    target = await get_user_by_username(session, username)
    if target is None or target.tenant_id != tenant_id:
        return False
    target.tenant_id = None
    await session.flush()
    return True


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    return (await session.execute(
        select(User).where(User.username == username)
    )).scalar_one_or_none()
