"""FastAPI dependency gates for license validation & feature access."""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, status

from app.core.licensing.manager import LicenseManager, get_license_manager


async def require_valid_license(
    lic: LicenseManager = Depends(get_license_manager),
) -> LicenseManager:
    """Dependency ensuring the archive has an active, valid license (or active grace period)."""
    if not lic.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "LICENSE_REQUIRED",
                "message": lic.status_message,
                "status": lic.status.value,
                "server_fingerprint": lic.get_summary().get("server_fingerprint"),
            },
        )
    return lic


def require_license_feature(feature_name: str) -> Callable[..., LicenseManager]:
    """Dependency factory ensuring a specific feature is licensed in the current edition."""

    async def _feature_gate(
        lic: LicenseManager = Depends(require_valid_license),
    ) -> LicenseManager:
        if not lic.has_feature(feature_name):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "FEATURE_NOT_LICENSED",
                    "feature": feature_name,
                    "message": f"Feature '{feature_name}' is not licensed under your current {lic.get_summary().get('edition', '')} edition.",
                },
            )
        return lic

    return _feature_gate
