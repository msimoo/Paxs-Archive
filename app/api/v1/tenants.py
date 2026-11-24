"""VNA (Vendor Neutral Archive) multi-tenancy REST API.

Tenants are institutions/departments whose studies are isolated. Admins create
tenants, map DICOM AE titles to them (so ingest + queries from that AE stay in
the tenant) and assign users to them (REST/QIDO browsing scope). Everything
unmapped/unassigned keeps the legacy unscoped scope.

Q-3 / FA-2: this router defines the isolation boundary rather than being scoped
by it, so every statement lives in :mod:`app.services.tenant_service` and the
routes keep validation + HTTP status mapping. The surface is gated by
``require_admin`` / ``require_destructive_role``, which is the boundary the AST
guard pins for it.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_admin, require_destructive_role
from app.db.models import Tenant, User
from app.db.session import get_db
from app.services import tenant_service

router = APIRouter(prefix="/api/v1/tenants", tags=["vna"])

# DICOM PS3.5 AE titles: 1-16 chars, uppercase-alphanumerics plus -_.
_AE_RE = re.compile(r"^[A-Z0-9._\-]{1,16}$")


def normalize_ae_title(raw: str) -> str:
    """Validate + normalize a DICOM AE title (uppercase, 1-16 chars)."""
    title = raw.strip().upper()
    if not _AE_RE.match(title):
        raise HTTPException(
            422,
            "ae_title must be 1-16 chars of A-Z 0-9 . _ - (DICOM PS3.5)",
        )
    return title


class TenantCreate(BaseModel):
    name: str
    code: str
    description: str | None = None


class TenantUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None


class AeMapping(BaseModel):
    ae_title: str


class UserAssignment(BaseModel):
    username: str


async def _tenant_or_404(db: AsyncSession, tenant_id: int) -> Tenant:
    tenant = await tenant_service.get_tenant(db, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    return tenant


async def _serialize(db: AsyncSession, tenant: Tenant) -> dict:
    return {
        "id": tenant.id,
        "name": tenant.name,
        "code": tenant.code,
        "description": tenant.description,
        "is_active": tenant.is_active,
        "ae_titles": await tenant_service.ae_titles_of(db, tenant.id),
        "study_count": await tenant_service.study_count_of(db, tenant.id),
        "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
    }


@router.get("")
async def list_tenants(db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_destructive_role)):
    return [await _serialize(db, t) for t in await tenant_service.list_tenants(db)]


@router.post("", status_code=201)
async def create_tenant(body: TenantCreate, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_admin)):
    name = body.name.strip()
    code = body.code.strip().lower().replace(" ", "-")
    if not name or not code:
        raise HTTPException(422, "name and code are required")
    if await tenant_service.tenant_name_or_code_taken(db, name=name, code=code):
        raise HTTPException(409, "A tenant with that name or code already exists")
    tenant = await tenant_service.create_tenant(
        db, name=name, code=code,
        description=body.description.strip() if body.description else None,
    )
    return await _serialize(db, tenant)


@router.patch("/{tenant_id}")
async def update_tenant(tenant_id: int, body: TenantUpdate,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_admin)):
    tenant = await _tenant_or_404(db, tenant_id)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "name cannot be empty")
        if await tenant_service.tenant_name_taken_by_other(
            db, name=name, tenant_id=tenant.id
        ):
            raise HTTPException(409, "Another tenant already uses that name")
        tenant.name = name
    if body.description is not None:
        tenant.description = body.description.strip() or None
    if body.is_active is not None:
        tenant.is_active = body.is_active
    await db.flush()
    await db.refresh(tenant)
    return await _serialize(db, tenant)


@router.delete("/{tenant_id}", status_code=204)
async def delete_tenant(tenant_id: int, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_admin)):
    tenant = await _tenant_or_404(db, tenant_id)
    await tenant_service.delete_tenant_cascade(db, tenant)


@router.get("/{tenant_id}/aes")
async def list_ae_titles(tenant_id: int, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_destructive_role)):
    tenant = await _tenant_or_404(db, tenant_id)
    return {
        "tenant_id": tenant.id,
        "ae_titles": await tenant_service.ae_titles_of(db, tenant.id),
    }


@router.post("/{tenant_id}/aes", status_code=201)
async def map_ae_title(tenant_id: int, body: AeMapping,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_admin)):
    tenant = await _tenant_or_404(db, tenant_id)
    ae_title = normalize_ae_title(body.ae_title)
    existing = await tenant_service.ae_mapping_owner(db, ae_title)
    if existing is not None:
        if existing.tenant_id == tenant.id:
            raise HTTPException(409, f"{ae_title} is already mapped to this tenant")
        raise HTTPException(
            409, f"{ae_title} is already mapped to tenant {existing.tenant_id}"
        )
    await tenant_service.map_ae_title(db, tenant.id, ae_title)
    return {"tenant_id": tenant.id, "ae_title": ae_title}


@router.delete("/{tenant_id}/aes/{ae_title}", status_code=204)
async def unmap_ae_title(tenant_id: int, ae_title: str,
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_admin)):
    ae_title = normalize_ae_title(ae_title)
    row = await tenant_service.get_ae_mapping(db, tenant_id, ae_title)
    if row is None:
        raise HTTPException(404, "AE title not mapped to this tenant")
    await tenant_service.unmap_ae_title(db, row)


@router.post("/{tenant_id}/users", status_code=200)
async def assign_user(tenant_id: int, body: UserAssignment,
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_admin)):
    """Assign a user to this tenant — their REST/QIDO browsing becomes scoped."""
    tenant = await _tenant_or_404(db, tenant_id)
    target = await tenant_service.assign_user(db, tenant, body.username.strip())
    if target is None:
        raise HTTPException(404, "User not found")
    return {"username": target.username, "tenant_id": tenant.id, "tenant_name": tenant.name}


@router.delete("/{tenant_id}/users/{username}", status_code=204)
async def unassign_user(tenant_id: int, username: str,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_admin)):
    """Remove a user's tenant assignment (back to the legacy unscoped scope)."""
    await _tenant_or_404(db, tenant_id)
    if not await tenant_service.unassign_user(db, tenant_id, username):
        raise HTTPException(404, "User not assigned to this tenant")


@router.get("/context")
async def tenant_context(db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """The caller's tenant scope — surfaced by the UI to show isolation state."""
    if user.role == "ADMIN":
        return {"is_admin": True, "tenant_id": None, "tenant_name": None}
    tenant = (
        await tenant_service.get_tenant(db, user.tenant_id)
        if user.tenant_id else None
    )
    return {
        "is_admin": False,
        "tenant_id": user.tenant_id,
        "tenant_name": tenant.name if tenant else None,
        "scoped": user.tenant_id is not None,
    }
