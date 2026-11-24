"""User management — Admin console → Users tab.

GET    /api/v1/users                    -> every account (admin only)
POST   /api/v1/users                    -> create an account (admin only)
PATCH  /api/v1/users/{user_id}          -> update role / full_name / is_active
POST   /api/v1/users/{user_id}/reset-password -> set a new password
DELETE /api/v1/users/{user_id}          -> remove an account

Safety guards: an admin can never delete or disable *themselves*, and the
last active ADMIN cannot be deleted, disabled or demoted (the archive would
become unmanageable).

Q-3 / FA-2: every statement lives in :mod:`app.services.user_service`; this
module keeps the HTTP shape, the guards and the audit calls. Accounts are not
tenant-owned rows, so the isolation boundary here is ``require_admin`` rather
than a tenant predicate.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import ROLES, write_audit
from app.api.v1.deps import require_admin
from app.core.security import hash_password
from app.db.models import User
from app.db.session import get_db
from app.services import user_service

router = APIRouter(prefix="/api/v1/users", tags=["users"])


def _user_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": bool(user.is_active),
        "tenant_id": user.tenant_id,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


async def _get_user(db: AsyncSession, user_id: int) -> User:
    user = await user_service.get_user(db, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


def _parse_details(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {"raw": raw}


class CreateUserBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    full_name: str | None = None
    role: str = "PHYSICIAN"
    # VNA: assign the account to a tenant (NULL = legacy unscoped scope).
    tenant_id: int | None = None


class PatchUserBody(BaseModel):
    role: str | None = None
    full_name: str | None = None
    is_active: bool | None = None
    tenant_id: int | None = None


class ResetPasswordBody(BaseModel):
    password: str = Field(min_length=6, max_length=128)


@router.get("")
async def list_users(_admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return [_user_dict(u) for u in await user_service.list_users(db)]


@router.post("", status_code=201)
async def create_user(body: CreateUserBody, _admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    if body.role not in ROLES:
        raise HTTPException(422, f"Invalid role, use one of {ROLES}")
    if await user_service.username_exists(db, body.username):
        raise HTTPException(409, "Username already taken")
    user = await user_service.create_user(
        db, username=body.username, password=body.password,
        full_name=body.full_name, role=body.role, tenant_id=body.tenant_id,
    )
    return _user_dict(user)


@router.get("/sessions")
async def list_sessions(_admin: User = Depends(require_admin),
                        db: AsyncSession = Depends(get_db)):
    """List active login sessions (admin console → Sessions tab).

    Every session that is still valid (not revoked, not expired), newest
    activity first, joined with the owning account so the admin sees who is
    signed in from which device / IP.
    """
    return [
        {
            "id": s.id,
            "user_id": s.user_id,
            "username": u.username,
            "full_name": u.full_name,
            "role": u.role,
            "ip_address": s.ip_address,
            "user_agent": s.user_agent,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "last_used_at": s.last_used_at.isoformat() if s.last_used_at else None,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
        }
        for s, u in await user_service.list_active_sessions(db)
    ]


@router.post("/sessions/{session_id}/revoke")
async def revoke_session(session_id: str, admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)):
    """Revoke one login session (per-session sign-out).

    Kills the targeted device immediately: the row is marked revoked, so its
    refresh cookie no longer rotates and the /viewer gate 303s it to /login.
    Other sessions (other devices / browsers) are untouched.
    """
    session = await user_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    if session.is_revoked:
        raise HTTPException(409, "Session already revoked")
    user = await user_service.get_user(db, session.user_id)
    session.is_revoked = True
    session.revoke_reason = "admin"
    await write_audit(db, session.user_id, "session_revoked",
                      details={"session_id": session_id, "by": admin.username,
                               "revoke_reason": "admin"})
    await db.flush()
    return {"ok": True, "username": user.username if user else None}


@router.post("/sessions/revoke-user/{user_id}")
async def revoke_user_sessions(user_id: int, admin: User = Depends(require_admin),
                               db: AsyncSession = Depends(get_db)):
    """Revoke every active session of one user (per-user sign-out).

    Kicks the user off all devices at once — each session row is marked
    revoked so every refresh cookie stops rotating and the /viewer gate
    303s each device to /login. The admin's own sessions are excluded if
    they target themselves, so you can't lock yourself out.
    """
    user = await _get_user(db, user_id)
    target = await user_service.sessions_of_user(
        db, user_id,
        # Never lock yourself out: revoking your own user id skips your own
        # sessions entirely (returns revoked=0) rather than killing the
        # device you are currently using.
        exclude_user_id=admin.id if user_id == admin.id else None,
    )
    for s in target:
        s.is_revoked = True
        s.revoke_reason = "admin_bulk"
    if target:
        await write_audit(db, user_id, "sessions_revoked_all",
                          details={"count": len(target), "by": admin.username})
        await db.flush()
    return {"ok": True, "username": user.username, "revoked": len(target)}


@router.get("/auth-audit")
async def list_auth_audit(action: str | None = None, user_id: int | None = None,
                          limit: int = 100, _admin: User = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)):
    """Auth audit trail (admin console → Auth Audit tab).

    Login / failed-login / lockout / refresh-rotation / theft-detection /
    role-change / session-revoke events, newest first, filterable by action
    and user. Exposes IPs and user agents, so it is admin-only (the DICOM
    tamper-evident trail stays on GET /api/v1/audit).
    """
    rows = await user_service.list_auth_audit(
        db, action=action, user_id=user_id, limit=limit
    )
    return [
        {
            "id": e.id,
            "action": e.action,
            "user_id": e.user_id,
            "username": u.username if u else None,
            "ip_address": e.ip_address,
            "user_agent": e.user_agent,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            "details": _parse_details(e.details),
        }
        for e, u in rows
    ]


@router.get("/locked")
async def list_locked(_admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    """Accounts currently locked out by the brute-force guard.

    Admin console shows each with remaining minutes and a one-click unlock;
    unknown usernames never appear here (failed logins don't create users).
    """
    return [
        {
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name,
            "role": u.role,
            "failed_attempts": u.failed_attempts or 0,
            "locked_until": u.locked_until.isoformat() if u.locked_until else None,
            "remaining_minutes": user_service.lockout_remaining_minutes(u),
        }
        for u in await user_service.list_locked(db)
    ]


@router.post("/{user_id}/reset-lockout")
async def reset_lockout(user_id: int, admin: User = Depends(require_admin),
                        db: AsyncSession = Depends(get_db)):
    """Unlock an account locked by the brute-force guard (per-account reset).

    Clears the failed-attempt counter and the lock window immediately, and
    records who did it in the auth audit trail.
    """
    user = await _get_user(db, user_id)
    user.failed_attempts = 0
    user.locked_until = None
    await write_audit(db, user.id, "lockout_reset",
                      details={"by": admin.username})
    await db.flush()
    return {"ok": True, "username": user.username}


@router.patch("/{user_id}")
async def patch_user(user_id: int, body: PatchUserBody,
                     admin: User = Depends(require_admin),
                     db: AsyncSession = Depends(get_db)):
    user = await _get_user(db, user_id)

    if body.role is not None and body.role not in ROLES:
        raise HTTPException(422, f"Invalid role, use one of {ROLES}")

    is_self = user.id == admin.id
    if body.is_active is False and is_self:
        raise HTTPException(422, "You cannot disable your own account")
    if body.role is not None and is_self and body.role != "ADMIN":
        raise HTTPException(422, "You cannot demote your own account")

    # Last-admin guards: never leave the archive without an active ADMIN.
    if user.role == "ADMIN" and not is_self:
        admins = await user_service.active_admin_count(db)
        if admins <= 1 and (body.is_active is False or (body.role is not None and body.role != "ADMIN")):
            raise HTTPException(422, "Cannot disable or demote the last ADMIN")

    if body.role is not None and body.role != user.role:
        await write_audit(db, user.id, "role_changed",
                          details={"old": user.role, "new": body.role})
        user.role = body.role
    if body.full_name is not None:
        user.full_name = body.full_name or None
    if body.is_active is not None:
        user.is_active = body.is_active
    # VNA: move the account between tenants (None = legacy unscoped scope).
    if "tenant_id" in body.model_fields_set:
        user.tenant_id = body.tenant_id
    await db.flush()
    return _user_dict(user)


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: int, body: ResetPasswordBody,
                         _admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)):
    user = await _get_user(db, user_id)
    user.password_hash = hash_password(body.password)
    # H04: a password reset must invalidate existing sessions — otherwise a
    # stolen cookie keeps working after the credential is changed.
    revoked = await user_service.revoke_all_sessions(db, user.id)
    await write_audit(db, user.id, "password_change")
    await db.flush()
    return {"ok": True, "username": user.username, "sessions_revoked": revoked}


@router.delete("/{user_id}")
async def delete_user(user_id: int, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    user = await _get_user(db, user_id)
    if user.id == admin.id:
        raise HTTPException(422, "You cannot delete your own account")
    if user.role == "ADMIN" and await user_service.active_admin_count(db) <= 1:
        raise HTTPException(422, "Cannot delete the last ADMIN")
    await user_service.delete_account(db, user)
    return {"ok": True, "username": user.username}
