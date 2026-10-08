"""PAXS Licensing API Endpoints."""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.api.v1.deps import get_current_user, get_current_user_any, require_admin
from app.core.licensing import (
    LicenseManager,
    get_hardware_info,
    get_license_manager,
    get_machine_fingerprint,
)
from app.db.models import User

router = APIRouter(prefix="/api/v1/license", tags=["License"])


class LicenseActivateRequest(BaseModel):
    license_b64: str = Field(..., description="Base64-encoded .lic binary or plain JSON license string")


class LicenseStatusResponse(BaseModel):
    status: str
    status_message: str
    is_active: bool
    server_fingerprint: str
    edition: str
    customer_name: str | None = None
    customer_organization: str | None = None
    customer_email: str | None = None
    issued_at: str | None = None
    expires_at: str | None = None
    is_perpetual: bool = False
    days_remaining: int | None = None
    max_users: int = 0
    max_studies_per_year: int = 0
    features: list[str] = []
    machine_fingerprint: str | None = None
    hardware_matched: bool = False
    license_file_present: bool = False


@router.get("", response_model=LicenseStatusResponse)
@router.get("/status", response_model=LicenseStatusResponse)
async def get_license_status(
    lic: LicenseManager = Depends(get_license_manager),
    _user=Depends(get_current_user_any),
) -> Any:
    """Returns current server license status, edition, features, and expiration."""
    return lic.get_summary()


@router.get("/fingerprint")
async def get_server_fingerprint(_admin=Depends(require_admin)) -> dict[str, Any]:
    """Returns the host machine hardware fingerprint and system identifiers."""
    return {
        "fingerprint": get_machine_fingerprint(),
        "hardware_info": get_hardware_info(),
    }


@router.post("/reload")
async def reload_license(
    lic: LicenseManager = Depends(get_license_manager),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Forces the server to re-read and verify paxs.lic from disk."""
    lic.load_license()
    return {
        "success": True,
        "status": lic.status.value,
        "message": lic.status_message,
        "is_active": lic.is_active,
    }


@router.post("/activate")
async def activate_license_base64(
    payload: LicenseActivateRequest,
    lic: LicenseManager = Depends(get_license_manager),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Activates a license by providing base64-encoded license content."""
    try:
        content_bytes = base64.b64decode(payload.license_b64)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid base64 encoding: {exc}",
        )

    ok, message = lic.activate_from_bytes(content_bytes)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

    return {
        "success": True,
        "message": message,
        "summary": lic.get_summary(),
    }


@router.post("/upload")
async def upload_license_file(
    file: UploadFile = File(...),
    lic: LicenseManager = Depends(get_license_manager),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Uploads and activates a .lic license file directly."""
    content_bytes = await file.read()
    if not content_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    ok, message = lic.activate_from_bytes(content_bytes)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

    return {
        "success": True,
        "message": message,
        "summary": lic.get_summary(),
    }


class LicenseOnlineActivateRequest(BaseModel):
    activation_key: str
    customer_email: str | None = None
    portal_url: str | None = None


@router.post("/activate-online")
async def activate_license_online(
    payload: LicenseOnlineActivateRequest,
    lic: LicenseManager = Depends(get_license_manager),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Activates a license key online against the vendor activation portal."""
    ok, message = lic.activate_online(
        activation_key=payload.activation_key,
        portal_url=payload.portal_url,
        customer_email=payload.customer_email,
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

    return {
        "success": True,
        "message": message,
        "summary": lic.get_summary(),
    }


@router.post("/validate-online")
async def validate_license_online(
    lic: LicenseManager = Depends(get_license_manager),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Manually triggers an online license heartbeat validation."""
    ok, message = lic.validate_online()
    return {
        "success": ok,
        "message": message,
        "summary": lic.get_summary(),
    }

