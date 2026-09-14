"""LDAP / Active Directory Authentication REST API.

  POST /api/v1/ldap/login — Authenticate domain user against LDAP / Active Directory
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_db
from app.services import ldap_service

log = logging.getLogger("paxs.api.ldap")

router = APIRouter(prefix="/api/v1/ldap", tags=["LDAP / Active Directory Integration"])


class LDAPLoginRequest(BaseModel):
    username: str = Field(..., description="Enterprise domain username")
    password: str = Field(..., description="Enterprise domain password")


@router.post("/login", response_class=JSONResponse)
async def ldap_login(
    req: LDAPLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate enterprise user via LDAP / Active Directory and issue PAXS JWT token."""
    identity = ldap_service.authenticate_ldap_user(req.username, req.password)
    if not identity:
        raise HTTPException(401, "Invalid LDAP domain credentials or user not found")

    user = await ldap_service.sync_ldap_user_to_db(db, identity)
    await db.commit()

    from app.core.config import settings

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
