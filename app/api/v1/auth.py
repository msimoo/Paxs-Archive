"""Authentication: register (first user becomes ADMIN), login, refresh, me.

Session layer (auth plan §2/§3): login creates a session row and stamps an
HttpOnly cookie holding an opaque refresh token ("<family_id>.<random>",
stored only as SHA-256). Access tokens are short (TOKEN_EXPIRE_MINUTES, 15 by
default) and silently renewed via POST /auth/refresh, which rotates the
refresh token. Reuse of a rotated token revokes the whole session family
(theft detection, plan §4.3). The cookie also gates /viewer + /studies-ui
(the viewer auth gate in app/main/middleware.py).

Brute-force lockout (plan §4.1): MAX_FAILED_ATTEMPTS bad logins lock the
account for LOCKOUT_DURATION_MINUTES; attempts 1-4 return a generic error,
the 5th locks it, and attempts while locked report the remaining minutes.
Every auth event (login_success / login_failed / account_locked / logout /
token_refresh / token_theft_detected / password_change / role_changed) is
appended to auth_audit_log (plan §3.3).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ratelimit import rate_limit
from app.api.v1.deps import get_current_user_any
from app.core.config import (
    MIN_ADMIN_PASSWORD_LENGTH,
    bootstrap_enabled,
    is_weak_admin_password,
    settings,
    setup_token_matches,
)
from app.core.csrf import clear_csrf_cookie, csrf_token_for, set_csrf_cookie
from app.core.roles import DESTRUCTIVE_ROLES, ROLES
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
from app.db.models import AuthSource, User
from app.db.session import get_db
from app.services import auth_service, user_service

log = logging.getLogger("paxs.auth")
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# ROLES is re-exported from app.core.roles so that role names have exactly one
# definition (shared with the LDAP group mapping and the worklist guard).

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
    "REPORT_WRITER": "/viewer",
}


def role_landing(role: str, next_url: str | None = None, root_path: str = "") -> str:
    """Landing page after login: ?next= (same-origin only) or role default.

    ``root_path`` is the reverse-proxy mount prefix (V2-4): the result is a
    browser-facing path, so it must carry the prefix or the redirect lands
    outside the app.
    """
    from app.core.proxy_prefix import prefix_path

    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        # same-origin absolute path only — blocks open-redirect via ?next=
        if "\n" not in next_url and "\r" not in next_url:
            return prefix_path(root_path, next_url)
    return prefix_path(root_path, ROLE_LANDING.get(role, "/viewer"))


class RegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    full_name: str | None = None
    role: str | None = None
    # B-4: required only for the FIRST account (the bootstrap administrator),
    # and only when SETUP_TOKEN is configured. Ignored afterwards.
    setup_token: str | None = None


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class LoginBody(BaseModel):
    username: str
    password: str


async def write_audit(db: AsyncSession, user_id: int | None, action: str,
                      request: Request | None = None,
                      details: dict | None = None) -> None:
    """Append one row to auth_audit_log (plan §3.3). Details is small JSON,
    never PHI. Committed by the caller (get_db commits on success).

    The row and its JSON encoding live in ``auth_service.record_event`` (Q-3);
    this thin wrapper keeps the call sites reading as *policy* ("record that
    this happened") instead of persistence details.
    """
    await auth_service.record_event(
        db,
        user_id=user_id,
        action=action,
        ip_address=_client_ip(request) if request else None,
        user_agent=_client_ua(request) if request else None,
        details=details,
    )


def _user_dict(user: User) -> dict:
    return {
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        # B-4: the client forces a password change when this is true.
        "must_change_password": bool(getattr(user, "must_change_password", False)),
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
    revoked = await auth_service.revoke_family(db, family_id, reason="token_reuse")
    if revoked:
        log.warning("revoked session family %s (%d session(s))",
                    family_id[:8], revoked)


async def _create_session(db: AsyncSession, user: User,
                          request: Request) -> tuple[str, str]:
    """Insert a session row; return (family_id, refresh_token)."""
    family_id = uuid.uuid4().hex
    refresh_token = generate_refresh_token(family_id)
    await auth_service.open_session(
        db,
        session_id=uuid.uuid4().hex,
        user_id=user.id,
        refresh_token_hash=hash_refresh_token(refresh_token),
        family_id=family_id,
        ip_address=_client_ip(request),
        user_agent=_client_ua(request),
        expires_at=_session_expiry(),
    )
    return family_id, refresh_token


def _auth_response(user: User, access_token: str, next_url: str | None,
                   root_path: str = "", csrf_token: str = "") -> JSONResponse:
    """The login/refresh body: access token, CSRF token, user, landing page.

    ``csrf_token`` (B-19/FS-10) is the signed double-submit value the SPA echoes
    in the ``X-CSRF-Token`` header; it is also delivered as a JS-readable cookie
    by the callers, so a client may use either source.
    """
    return JSONResponse({
        "access_token": access_token,
        "token_type": "bearer",
        "csrf_token": csrf_token,
        "user": _user_dict(user),
        "redirect_to": role_landing(user.role, next_url, root_path),
    })


@router.post("/register", status_code=201,
            dependencies=[Depends(rate_limit("auth", settings.RATE_LIMIT_LOGIN_PER_MINUTE, 60))])
async def register(body: RegisterBody, request: Request, db: AsyncSession = Depends(get_db)):
    if await auth_service.username_taken(db, body.username):
        raise HTTPException(409, "Username already taken")

    total = await auth_service.account_count(db)

    # B-4: the very first account is the bootstrap ADMIN. It is no longer free:
    # the caller must present the configured SETUP_TOKEN, and with no token
    # configured the API cannot create it at all (use the CLI / db/init instead).
    # This closes "the first unauthenticated caller on an empty database becomes
    # ADMIN".
    bootstrap = total == 0
    if bootstrap:
        if not bootstrap_enabled(settings):
            raise HTTPException(
                403,
                "Bootstrap is disabled: set SETUP_TOKEN to create the first "
                "administrator, or initialize the database with the CLI.",
            )
        if not setup_token_matches(body.setup_token, settings):
            # Deliberately generic: the token is a credential.
            raise HTTPException(403, "Invalid or missing setup token")
        if is_weak_admin_password(body.password):
            raise HTTPException(
                422,
                "The bootstrap administrator password is too weak - use at least "
                f"{MIN_ADMIN_PASSWORD_LENGTH} characters and avoid common values.",
            )
        role = "ADMIN"
    else:
        if not settings.ALLOW_REGISTER:
            raise HTTPException(403, "Registration is disabled on this server")

        # H02: self-registration may only mint least-privilege roles. ADMIN and
        # RADIOLOGIST are destructive-capable, so they are refused outright —
        # an existing admin must create such accounts via Admin → Users.
        allowed = {
            r.strip().upper()
            for r in settings.REGISTER_ALLOWED_ROLES.split(",")
            if r.strip()
        } - {*DESTRUCTIVE_ROLES, "REPORT_WRITER"}
        requested = (body.role or "").strip().upper()
        if requested and requested not in ROLES:
            raise HTTPException(422, f"Invalid role, use one of {ROLES}")
        if requested and requested not in allowed:
            raise HTTPException(
                403,
                "Self-registration cannot create this role; "
                f"allowed: {sorted(allowed) or 'none'}. "
                "An administrator can create privileged accounts in Admin → Users.",
            )
        role = requested or (sorted(allowed)[0] if allowed else "PHYSICIAN")

    # The bootstrap admin chose its own (validated) password, so it is not
    # forced to rotate; accounts created by automation are (see db_admin).
    user = await auth_service.create_local_account(
        db,
        username=body.username,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        role=role,
    )
    await write_audit(db, user.id, "login_success", request, {"via": "register"})

    access_token = create_access_token(user.username, user.role, settings.SECRET_KEY,
                                       settings.TOKEN_EXPIRE_MINUTES)
    _, refresh_token = await _create_session(db, user, request)
    # Commit before answering: get_db's own commit runs after the response is
    # sent, so a client using the new cookie at once could reach a server that
    # has not stored the session yet and get 401 (observed on the first
    # request after login).
    await db.commit()
    response = _auth_response(user, access_token, request.query_params.get("next"),
                              request.scope.get("root_path") or "",
                              csrf_token_for(refresh_token))
    response.status_code = 201
    set_session_cookie(response, refresh_token)
    set_csrf_cookie(response, refresh_token)
    return response


@router.post("/login",
            dependencies=[Depends(rate_limit("auth", settings.RATE_LIMIT_LOGIN_PER_MINUTE, 60))])
async def login(body: LoginBody, request: Request, db: AsyncSession = Depends(get_db)):
    user = await user_service.get_user_by_username(db, body.username)

    if user is None:
        # Unknown username: generic message, no lockout state to touch.
        raise HTTPException(401, "Invalid username or password")

    if user.auth_source != AuthSource.LOCAL:
        await write_audit(db, user.id, "login_failed", request,
                          {"reason": "auth_source_mismatch"})
        await db.commit()
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
    # Commit before answering: get_db's own commit runs after the response is
    # sent, so a client using the new cookie at once could reach a server that
    # has not stored the session yet and get 401 (observed on the first
    # request after login).
    await db.commit()
    response = _auth_response(user, access_token, request.query_params.get("next"),
                              request.scope.get("root_path") or "",
                              csrf_token_for(refresh_token))
    set_session_cookie(response, refresh_token)
    set_csrf_cookie(response, refresh_token)
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
    sess = await auth_service.find_session(db, token_hash)

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

    user = await auth_service.load_account(db, sess.user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "Account disabled")

    # Rotate: new refresh token, same family, then hand out a new access token.
    new_token = generate_refresh_token(sess.family_id)
    sess.refresh_token_hash = hash_refresh_token(new_token)
    sess.last_used_at = _now()
    sess.expires_at = _session_expiry()

    await write_audit(db, user.id, "token_refresh", request)
    # Commit before answering: get_db's own commit runs after the response is
    # sent, so a client using the new cookie at once could reach a server that
    # has not stored the session yet and get 401 (observed on the first
    # request after login or refresh).
    await db.commit()
    access_token = create_access_token(user.username, user.role, settings.SECRET_KEY,
                                       settings.TOKEN_EXPIRE_MINUTES)
    response = _auth_response(user, access_token, request.query_params.get("next"),
                              request.scope.get("root_path") or "",
                              csrf_token_for(new_token))
    set_session_cookie(response, new_token)
    set_csrf_cookie(response, new_token)
    return response


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    """Revoke the session row behind the cookie and clear it."""
    token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if token:
        sess = await auth_service.find_session(db, hash_refresh_token(token))
        if sess is not None:
            auth_service.mark_revoked(sess, "logout")
            await write_audit(db, sess.user_id, "logout", request)
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    clear_csrf_cookie(response)
    return response


@router.post("/change-password")
async def change_password(body: ChangePasswordBody, request: Request,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user_any)):
    """Change the caller's own password (B-4).

    Verifies the current password, stores the new one and clears the
    ``must_change_password`` flag. Every session is revoked (mirroring the admin
    reset) so a credential that was issued by automation — or a cookie stolen
    before the change — stops working immediately; the caller signs in again
    with the new password.
    """
    if not verify_password(body.current_password, user.password_hash):
        await write_audit(db, user.id, "login_failed", request,
                          {"reason": "password_change_wrong_current"})
        await db.commit()  # the audit entry must survive the raised error
        raise HTTPException(401, "Current password is incorrect")

    # A privileged account must not be able to rotate onto a weak value.
    if user.role in DESTRUCTIVE_ROLES and is_weak_admin_password(body.new_password):
        raise HTTPException(
            422,
            "New password is too weak for a privileged account - use at least "
            f"{MIN_ADMIN_PASSWORD_LENGTH} characters and avoid common values.",
        )

    # `get_current_user_any` resolves the user in its OWN session and returns a
    # detached instance, so writing to it here would be silently discarded.
    # Re-load it inside this request's session (the same pattern users.py uses
    # for the admin reset).
    account = await auth_service.load_account(db, user.id)
    if account is None:
        raise HTTPException(401, "User not found")

    account.password_hash = hash_password(body.new_password)
    account.must_change_password = False
    revoked = await user_service.revoke_all_sessions(db, account.id)
    await write_audit(db, account.id, "password_change", request)
    await db.flush()
    return {"ok": True, "username": account.username,
            "must_change_password": False, "sessions_revoked": revoked or 0}


@router.get("/me")
async def me(user: User = Depends(get_current_user_any)):
    return {**_user_dict(user), "patient_name_display": settings.PATIENT_NAME_DISPLAY}
