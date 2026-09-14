"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.core.config import settings
from app.core.security import decode_token, hash_refresh_token
from app.db.models import Session, User
from app.db.session import AsyncSessionLocal

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user_from_cookie(request: Request) -> User | None:
    """Resolve the user from the HttpOnly refresh-token cookie, or None.

    Used by the /viewer auth gate and the /login page (already-signed-in
    redirect). The cookie carries the opaque refresh token; we look up the
    session row by its SHA-256 hash and reject revoked/expired sessions.
    Returns None ("redirect to /login") instead of raising.
    """
    token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if not token:
        return None
    async with AsyncSessionLocal() as session:
        sess = (
            await session.execute(
                select(Session).where(
                    Session.refresh_token_hash == hash_refresh_token(token)
                )
            )
        ).scalar_one_or_none()
        if sess is None or sess.is_revoked:
            return None
        user = (
            await session.execute(select(User).where(User.id == sess.user_id))
        ).scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(credentials.credentials, settings.SECRET_KEY)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.username == payload["sub"]))
        ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        # Disabled accounts are rejected even with a valid token.
        raise HTTPException(status_code=401, detail="Account disabled")
    return user


async def get_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User | None:
    """Current user when a valid bearer token or session cookie is present, else None.

    Used by browse endpoints (studies list, QIDO-RS, status endpoints) that stay readable
    for guests / unauthenticated users while providing user context when authenticated.
    """
    if credentials is not None:
        payload = decode_token(credentials.credentials, settings.SECRET_KEY)
        if payload is not None:
            async with AsyncSessionLocal() as session:
                user = (
                    await session.execute(
                        select(User).where(User.username == payload["sub"])
                    )
                ).scalar_one_or_none()
            if user is not None and user.is_active:
                return user
    return await get_current_user_from_cookie(request)


async def get_current_user_any(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User:
    """Current user via bearer token (preferred) or the HttpOnly session cookie.

    Used by /auth/me and admin actions so browser sessions with HttpOnly refresh cookies
    can authenticate without re-logging in. Raises 401 when neither authenticates.
    """
    user = await get_optional_user(request, credentials)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# Roles allowed to destroy archive data (delete studies / shares).
DESTRUCTIVE_ROLES = ("ADMIN", "RADIOLOGIST")


async def require_destructive_role(user: User = Depends(get_current_user_any)) -> User:
    if user.role not in DESTRUCTIVE_ROLES:
        raise HTTPException(
            status_code=403,
            detail=f"Requires one of {DESTRUCTIVE_ROLES} roles",
        )
    return user


async def require_admin(user: User = Depends(get_current_user_any)) -> User:
    if user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Requires ADMIN role")
    return user
