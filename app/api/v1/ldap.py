"""LDAP / Active Directory authentication API (``plan_9_2026.md`` C02).

  POST /api/v1/ldap/login — authenticate a directory user

The endpoint is fail-closed:

* **503** while the directory is not enabled/configured — it never falls back to
  a built-in identity;
* **401** for every failure mode with one generic message (no user enumeration);
* rate limited exactly like the local login endpoint;
* disabled accounts are rejected;
* every attempt is written to the authentication audit trail.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ratelimit import rate_limit
from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_db
from app.services import ldap_service

log = logging.getLogger("paxs.api.ldap")

router = APIRouter(prefix="/api/v1/ldap", tags=["LDAP / Active Directory Integration"])

# One message for every failure mode: never disclose whether the username exists.
_INVALID_CREDENTIALS = "Invalid LDAP domain credentials or user not found"


class LDAPLoginRequest(BaseModel):
    username: str = Field(..., description="Enterprise domain username")
    password: str = Field(..., description="Enterprise domain password")


async def _audit(db: AsyncSession, user_id: int | None, action: str,
                 request: Request, details: dict | None = None) -> None:
    """Write an audit row and commit it immediately.

    The audit row is committed here because a failed login raises HTTPException,
    which makes the request-scoped session roll back — an uncommitted audit row
    would be lost exactly when it matters most.
    """
    from app.api.v1.auth import write_audit

    try:
        await write_audit(db, user_id, action, request, details)
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - auditing must not mask the response
        log.warning("LDAP audit write failed (%s): %s", action, exc)


@router.post(
    "/login",
    response_class=JSONResponse,
    dependencies=[Depends(rate_limit("auth", settings.RATE_LIMIT_LOGIN_PER_MINUTE, 60))],
)
async def ldap_login(
    req: LDAPLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate a directory user and issue a PAXS access token."""
    if not ldap_service.is_ldap_configured():
        # Disabled, or enabled without a server/base DN: refuse rather than
        # authenticate by any other means.
        log.info("LDAP login attempt while directory authentication is not configured")
        raise HTTPException(503, "LDAP authentication is not enabled on this server")

    identity = ldap_service.authenticate_ldap_user(req.username, req.password)
    if identity is None:
        await _audit(db, None, "login_failed", request, {"via": "ldap"})
        raise HTTPException(401, _INVALID_CREDENTIALS)

    try:
        user = await ldap_service.sync_ldap_user_to_db(db, identity)
    except ldap_service.LDAPConfigurationError as exc:
        log.error("LDAP provisioning refused: %s", exc)
        await _audit(db, None, "login_failed", request, {"via": "ldap", "reason": "unmapped_role"})
        raise HTTPException(401, _INVALID_CREDENTIALS) from exc

    if not user.is_active:
        await _audit(db, user.id, "login_failed", request, {"via": "ldap", "reason": "disabled"})
        raise HTTPException(401, _INVALID_CREDENTIALS)

    await db.commit()
    await _audit(db, user.id, "login_success", request, {"via": "ldap"})

    token = create_access_token(
        user.username, user.role, settings.SECRET_KEY, settings.TOKEN_EXPIRE_MINUTES
    )
    return JSONResponse(
        {
            "access_token": token,
            "token_type": "bearer",
            "username": user.username,
            "role": user.role,
            "full_name": user.full_name,
            "auth_provider": "LDAP/ActiveDirectory",
        }
    )
