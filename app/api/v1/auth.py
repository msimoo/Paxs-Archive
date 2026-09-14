"""Authentication: register (first user becomes ADMIN), login, refresh, me.

Session layer (auth plan §2/§3): login creates a session row and stamps an
HttpOnly cookie holding an opaque refresh token ("<family_id>.<random>",
stored only as SHA-256). Access tokens are short (TOKEN_EXPIRE_MINUTES, 15 by
default) and silently renewed via POST /auth/refresh, which rotates the
refresh token. Reuse of a rotated token revokes the whole session family
(theft detection, plan §4.3). The cookie also gates /viewer + /studies-ui
(the viewer auth gate in app/main.py).

Brute-force lockout (plan §4.1): MAX_FAILED_ATTEMPTS bad logins lock the
account for LOCKOUT_DURATION_MINUTES; attempts 1-4 return a generic error,
the 5th locks it, and attempts while locked report the remaining minutes.
Every auth event (login_success / login_failed / account_locked / logout /
token_refresh / token_theft_detected / password_change / role_changed) is
appended to auth_audit_log (plan §3.3).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    clear_session_cookie,
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    refresh_token_family,
    set_session_cookie,
    verify_password,
)
from app.db.models import AuthAuditLog, Session, User
from app.db.session import get_db
from app.api.v1.deps import get_current_user, get_current_user_any
from app.api.ratelimit import rate_limit

log = logging.getLogger("paxs.auth")
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

ROLES = ("ADMIN", "RADIOLOGIST", "PHYSICIAN", "TECHNOLOGIST")

# Post-login landing per role (auth plan §1). These are the real pages the
# archive serves — the dashboard at /, the study list at /viewer. The
# plan's /archive/dashboard + /viewer/study-list targets do not exist as
# separate SPA routes, so ADMIN lands on the dashboard and clinical roles
# on the study list. A same-origin ?next= overrides the landing.
ROLE_LANDING = {
    "ADMIN": "/",
    "RADIOLOGIST": "/viewer",
    "PHYSICIAN": "/viewer",
    "TECHNOLOGIST": "/viewer",
}


def role_landing(role: str, next_url: str | None = None) -> str:
    """Landing page after login: ?next= (same-origin only) or role default."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        # same-origin absolute path only — blocks open-redirect via ?next=
        if "\n" not in next_url and "\r" not in next_url:
            return next_url
    return ROLE_LANDING.get(role, "/viewer")


class RegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    full_name: str | None = None
    role: str | None = None


class LoginBody(BaseModel):
    username: str
    password: str


async def write_audit(db: AsyncSession, user_id: int | None, action: str,
                      request: Request | None = None,
                      details: dict | None = None) -> None:
    """Append one row to auth_audit_log (plan §3.3). Details is small JSON,
    never PHI. Committed by the caller (get_db commits on success)."""
    db.add(AuthAuditLog(
        user_id=user_id,
        action=action,
        ip_address=_client_ip(request) if request else None,
        user_agent=_client_ua(request) if request else None,
        details=json.dumps(details) if details else None,
    ))


def _user_dict(user: User) -> dict:
    return {
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _session_expiry() -> datetime:
    return _now() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)


def _client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _client_ua(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    return ua[:500] if ua else None


async def _revoke_family(db: AsyncSession, family_id: str) -> None:
    """Revoke every session sharing a family id (theft response)."""
    if not family_id:
        return
    sessions = (
        await db.execute(select(Session).where(Session.family_id == family_id))
    ).scalars().all()
    for s in sessions:
        s.is_revoked = True
        s.revoke_reason = "token_reuse"
    if sessions:
        log.warning("revoked session family %s (%d session(s))",
                    family_id[:8], len(sessions))


async def _create_session(db: AsyncSession, user: User,
                          request: Request) -> tuple[str, str]:
    """Insert a session row; return (family_id, refresh_token)."""
    family_id = uuid.uuid4().hex
    refresh_token = generate_refresh_token(family_id)
    db.add(Session(
        id=uuid.uuid4().hex,
        user_id=user.id,
        refresh_token_hash=hash_refresh_token(refresh_token),
        family_id=family_id,
        ip_address=_client_ip(request),
        user_agent=_client_ua(request),
        expires_at=_session_expiry(),
    ))
    return family_id, refresh_token


def _auth_response(user: User, access_token: str, next_url: str | None) -> JSONResponse:
    return JSONResponse({
        "access_token": access_token,
        "token_type": "bearer",
        "user": _user_dict(user),
        "redirect_to": role_landing(user.role, next_url),
    })


@router.post("/register", status_code=201,
            dependencies=[Depends(rate_limit("auth", settings.RATE_LIMIT_LOGIN_PER_MINUTE, 60))])
async def register(body: RegisterBody, request: Request, db: AsyncSession = Depends(get_db)):
    if not settings.ALLOW_REGISTER:
        raise HTTPException(403, "Registration is disabled on this server")

    existing = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, "Username already taken")

    total = (await db.execute(select(func.count(User.id)))).scalar_one()
    role = body.role or "PHYSICIAN"
    if total == 0:
        role = "ADMIN"  # first user bootstraps as admin
    elif role not in ROLES:
        raise HTTPException(422, f"Invalid role, use one of {ROLES}")
    elif role == "ADMIN":
        # Self-registration can never mint admins: an existing admin must
        # promote the account via Admin → Users (privilege-escalation guard).
        raise HTTPException(403, "New accounts cannot be created as ADMIN")

    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        role=role,
    )
    db.add(user)
    await db.flush()
    # MySQL has no INSERT..RETURNING: server-generated columns (created_at)
    # are unloaded after the flush and accessing them triggers a sync lazy
    # refresh -> MissingGreenlet in the async handler. Reload inside the
    # async context (same pattern as users.py admin create).
    await db.refresh(user)
    await write_audit(db, user.id, "login_success", request, {"via": "register"})

    access_token = create_access_token(user.username, user.role, settings.SECRET_KEY,
                                       settings.TOKEN_EXPIRE_MINUTES)
    _, refresh_token = await _create_session(db, user, request)
    response = _auth_response(user, access_token, request.query_params.get("next"))
    response.status_code = 201
    set_session_cookie(response, refresh_token)
    return response


@router.post("/login",
            dependencies=[Depends(rate_limit("auth", settings.RATE_LIMIT_LOGIN_PER_MINUTE, 60))])
async def login(body: LoginBody, request: Request, db: AsyncSession = Depends(get_db)):
    user = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()

    if user is None:
        # Unknown username: generic message, no lockout state to touch.
        raise HTTPException(401, "Invalid username or password")

    if user.locked_until is not None and user.locked_until > _now():
        # Still inside the lockout window — refuse even a correct password.
        remaining = int((user.locked_until - _now()).total_seconds() // 60) + 1
        await write_audit(db, user.id, "login_failed", request,
                          {"reason": "locked", "attempt": user.failed_attempts})
        await db.commit()  # audit must survive the exception (get_db rolls back)
        raise HTTPException(
            423, f"Account locked — try again in {remaining} minute(s)"
        )

    ok = verify_password(body.password, user.password_hash)
    if not ok:
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= settings.MAX_FAILED_ATTEMPTS:
            user.locked_until = _now() + timedelta(minutes=settings.LOCKOUT_DURATION_MINUTES)
            await write_audit(db, user.id, "account_locked", request,
                              {"attempt": user.failed_attempts,
                               "lockout_minutes": settings.LOCKOUT_DURATION_MINUTES})
            log.warning("account %s locked after %d failed attempts",
                        user.username, user.failed_attempts)
            await db.commit()
            raise HTTPException(
                423, f"Too many failed attempts — account locked for {settings.LOCKOUT_DURATION_MINUTES} minutes"
            )
        await write_audit(db, user.id, "login_failed", request,
                          {"attempt": user.failed_attempts})
        await db.commit()
        raise HTTPException(401, "Invalid username or password")

    if not user.is_active:
        await write_audit(db, user.id, "login_failed", request, {"reason": "disabled"})
        await db.commit()
        raise HTTPException(401, "Account disabled")

    # Success — reset the lockout counters and stamp the login.
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login = _now()
    user.last_login_ip = _client_ip(request)
    await write_audit(db, user.id, "login_success", request)

    access_token = create_access_token(user.username, user.role, settings.SECRET_KEY,
                                       settings.TOKEN_EXPIRE_MINUTES)
    _, refresh_token = await _create_session(db, user, request)
    response = _auth_response(user, access_token, request.query_params.get("next"))
    set_session_cookie(response, refresh_token)
    return response


@router.post("/refresh",
            dependencies=[Depends(rate_limit("auth", settings.RATE_LIMIT_LOGIN_PER_MINUTE, 60))])
async def refresh(request: Request, db: AsyncSession = Depends(get_db)):
    """Rotate the refresh token and issue a fresh short-lived access token.

    Theft detection (plan §4.3): a token whose session row is missing or
    revoked means it was already rotated — revoke the whole family.
    """
    token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if not token:
        raise HTTPException(401, "No session")

    token_hash = hash_refresh_token(token)
    sess = (
        await db.execute(select(Session).where(Session.refresh_token_hash == token_hash))
    ).scalar_one_or_none()

    if sess is None or sess.is_revoked:
        # Rotated token replayed (or a deleted session) — possible theft.
        family = refresh_token_family(token) or (sess.family_id if sess else None)
        await _revoke_family(db, family)
        await write_audit(db, sess.user_id if sess else None,
                          "token_theft_detected", request,
                          {"family_prefix": (family or "")[:8]})
        log.warning("refresh-token reuse detected — revoked family %s",
                    (family or "?")[:8])
        # Commit before raising: get_db rolls back on exception, and the
        # family revocation must survive the 401 response.
        await db.commit()
        raise HTTPException(401, "Session invalid or reused")

    if sess.expires_at < _now():
        sess.is_revoked = True
        sess.revoke_reason = "expired"
        await db.commit()
        raise HTTPException(401, "Session expired")

    user = (
        await db.execute(select(User).where(User.id == sess.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(401, "Account disabled")

    # Rotate: new refresh token, same family, then hand out a new access token.
    new_token = generate_refresh_token(sess.family_id)
    sess.refresh_token_hash = hash_refresh_token(new_token)
    sess.last_used_at = _now()
    sess.expires_at = _session_expiry()

    await write_audit(db, user.id, "token_refresh", request)
    access_token = create_access_token(user.username, user.role, settings.SECRET_KEY,
                                       settings.TOKEN_EXPIRE_MINUTES)
    response = _auth_response(user, access_token, request.query_params.get("next"))
    set_session_cookie(response, new_token)
    return response


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    """Revoke the session row behind the cookie and clear it."""
    token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if token:
        sess = (
            await db.execute(select(Session).where(
                Session.refresh_token_hash == hash_refresh_token(token)))
        ).scalar_one_or_none()
        if sess is not None:
            sess.is_revoked = True
            sess.revoke_reason = "logout"
            await write_audit(db, sess.user_id, "logout", request)
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


@router.get("/me")
async def me(user: User = Depends(get_current_user_any)):
    return _user_dict(user)
