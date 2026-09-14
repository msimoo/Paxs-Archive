"""PAXS License Manager.

Manages runtime license lifecycle:
- Startup license loading and cryptographic verification
- Hardware fingerprint matching
- Expiry and quota enforcement
- Online license validation and grace period tracking
- Feature flag gating
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.licensing.crypto import (
    LicenseData,
    decrypt_license_container,
    verify_license_signature,
)
from app.core.licensing.fingerprint import get_hardware_info, get_machine_fingerprint

log = logging.getLogger("paxs.licensing.manager")


class LicenseStatus(str, Enum):
    VALID = "VALID"
    GRACE_PERIOD = "GRACE_PERIOD"
    EXPIRED = "EXPIRED"
    HARDWARE_MISMATCH = "HARDWARE_MISMATCH"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    REVOKED = "REVOKED"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    NOT_FOUND = "NOT_FOUND"


class LicenseManager:
    """Singleton-friendly License Manager."""

    def __init__(self, license_path: Path | str | None = None):
        self._custom_path = Path(license_path) if license_path else None
        self._data: LicenseData | None = None
        self._status: LicenseStatus = LicenseStatus.NOT_FOUND
        self._status_message: str = "No license loaded"
        self._last_checked_at: datetime | None = None
        self._grace_period_days: int = 7
        self.load_license()

    @property
    def license_file_path(self) -> Path:
        if self._custom_path:
            return self._custom_path
        env_path = os.environ.get("PAXS_LICENSE_PATH")
        if env_path:
            return Path(env_path)
        # Default locations to search: ./paxs.lic, ./config/paxs.lic, or adjacent to script
        cwd_lic = Path.cwd() / "paxs.lic"
        if cwd_lic.exists():
            return cwd_lic
        config_lic = Path.cwd() / "config" / "paxs.lic"
        if config_lic.exists():
            return config_lic
        return cwd_lic

    @property
    def status(self) -> LicenseStatus:
        return self._status

    @property
    def status_message(self) -> str:
        return self._status_message

    @property
    def is_active(self) -> bool:
        """Returns True if PAXS is fully operational (VALID or in GRACE_PERIOD)."""
        return self._status in (LicenseStatus.VALID, LicenseStatus.GRACE_PERIOD)

    @property
    def license_data(self) -> LicenseData | None:
        return self._data

    def load_license(self, file_path: Path | str | None = None) -> LicenseStatus:
        """Loads, decrypts, and verifies the license file."""
        if file_path:
            self._custom_path = Path(file_path)

        path = self.license_file_path
        self._last_checked_at = datetime.now(timezone.utc)

        if not path.exists():
            # Check if developer mode allows temporary dev license
            if os.environ.get("PAXS_DEV_LICENSE", "0") in ("1", "true", "yes"):
                log.info("PAXS_DEV_LICENSE enabled — operating under Developer Unlimited License")
                self._data = LicenseData(
                    license_id="PAXS-DEV-LOCAL-UNLIMITED",
                    version="1.0",
                    customer_name="Local Developer",
                    customer_organization="PAXS Development Environment",
                    edition="enterprise",
                    machine_fingerprint="*",
                    issued_at=datetime.now(timezone.utc),
                    expires_at=None,
                    max_users=999,
                    max_studies_per_year=9999999,
                    features=[
                        "mpr_3d",
                        "recist",
                        "brain_seg",
                        "ich_triage",
                        "tmtv_pet",
                        "li_rads",
                        "lung_rads",
                        "agatston",
                        "pe_clot",
                        "aortic_aneurysm",
                        "cardiac_ef",
                        "breast_density",
                        "cad_mammography",
                        "bone_age_fracture",
                        "cobb_angle",
                        "hl7_router",
                        "fhir_gateway",
                        "ai_report_draft",
                        "telerad_edge",
                        "storage_tiering",
                        "ai_pipeline",
                    ],
                )
                self._status = LicenseStatus.VALID
                self._status_message = "Active Developer License (Unlimited)"
                return self._status

            self._data = None
            self._status = LicenseStatus.NOT_FOUND
            self._status_message = f"License file not found at {path.name}"
            log.warning("No PAXS license file found at %s", path)
            return self._status

        try:
            raw_bytes = path.read_bytes()
            data = decrypt_license_container(raw_bytes)
        except Exception as exc:
            self._data = None
            self._status = LicenseStatus.INVALID_SIGNATURE
            self._status_message = f"Failed to decrypt license container: {exc}"
            log.error("License container decryption error: %s", exc)
            return self._status

        # Verify RSA digital signature
        if not verify_license_signature(data):
            self._data = data
            self._status = LicenseStatus.INVALID_SIGNATURE
            self._status_message = "License digital signature is invalid or has been modified"
            log.error("License signature verification failed for %s", data.license_id)
            return self._status

        # Verify hardware fingerprint
        host_fp = get_machine_fingerprint()
        if data.machine_fingerprint != "*" and data.machine_fingerprint.upper() != host_fp.upper():
            self._data = data
            self._status = LicenseStatus.HARDWARE_MISMATCH
            self._status_message = (
                f"License hardware mismatch. Target: {data.machine_fingerprint}, Host: {host_fp}"
            )
            log.error("License hardware mismatch: expected %s, got %s", data.machine_fingerprint, host_fp)
            return self._status

        # Check expiration date
        now = datetime.now(timezone.utc)
        if data.expires_at:
            if now > data.expires_at:
                # Check if within grace period
                grace_end = data.expires_at + timedelta(days=self._grace_period_days)
                if now <= grace_end:
                    days_left = (grace_end - now).days + 1
                    self._data = data
                    self._status = LicenseStatus.GRACE_PERIOD
                    self._status_message = f"License expired on {data.expires_at.strftime('%Y-%m-%d')}. In grace period ({days_left} day(s) left)."
                    log.warning("License %s is operating in GRACE_PERIOD", data.license_id)
                    return self._status
                else:
                    self._data = data
                    self._status = LicenseStatus.EXPIRED
                    self._status_message = f"License expired on {data.expires_at.strftime('%Y-%m-%d')}"
                    log.warning("License %s is EXPIRED", data.license_id)
                    return self._status

        self._data = data
        self._status = LicenseStatus.VALID
        self._status_message = f"License active: {data.edition.upper()} ({data.customer_name})"
        log.info("License verified successfully: ID=%s, Edition=%s, Customer=%s",
                 data.license_id, data.edition, data.customer_name)
        return self._status

    def activate_from_bytes(self, content_bytes: bytes, destination_path: Path | str | None = None) -> tuple[bool, str]:
        """Tests and activates a new license from raw uploaded bytes."""
        try:
            data = decrypt_license_container(content_bytes)
        except Exception as exc:
            return False, f"Invalid license file format or decryption failed: {exc}"

        if not verify_license_signature(data):
            return False, "License cryptographic signature is invalid or tampered"

        host_fp = get_machine_fingerprint()
        if data.machine_fingerprint != "*" and data.machine_fingerprint.upper() != host_fp.upper():
            return False, f"Hardware fingerprint mismatch: license is bound to {data.machine_fingerprint}, but this server is {host_fp}"

        if data.expires_at and datetime.now(timezone.utc) > data.expires_at:
            return False, f"Cannot activate an already expired license (expired {data.expires_at.strftime('%Y-%m-%d')})"

        target_path = Path(destination_path) if destination_path else self.license_file_path
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(content_bytes)
            self.load_license(target_path)
            return True, f"Successfully activated {data.edition.upper()} license for {data.customer_name}"
        except Exception as exc:
            return False, f"Failed to save license file to disk: {exc}"

    def activate_online(
        self,
        activation_key: str,
        portal_url: str | None = None,
        customer_email: str | None = None,
    ) -> tuple[bool, str]:
        """Contacts the vendor activation portal to claim a license for this machine."""
        import json
        import urllib.request
        import urllib.error
        from app.core.config import settings

        base_url = (portal_url or settings.LICENSE_PORTAL_URL).rstrip("/")
        if base_url.endswith("/api/activate.php"):
            endpoint = base_url
        elif base_url.endswith("/api/validate.php"):
            endpoint = base_url.replace("/api/validate.php", "/api/activate.php")
        else:
            endpoint = f"{base_url}/api/activate.php"
        host_fp = get_machine_fingerprint()
        hw_info = get_hardware_info()

        payload = {
            "action": "activate",
            "key": activation_key.strip(),
            "email": customer_email.strip() if customer_email else "",
            "fingerprint": host_fp,
            "hardware_info": hw_info,
            "app_version": settings.APP_VERSION,
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": f"PAXS-Archive/{settings.APP_VERSION}"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            try:
                err_body = json.loads(err.read().decode("utf-8"))
                return False, err_body.get("message") or err_body.get("detail") or f"Portal HTTP error ({err.code})"
            except Exception:
                return False, f"Portal HTTP error {err.code}: {err.reason}"
        except Exception as exc:
            return False, f"Failed to connect to license portal ({endpoint}): {exc}"

        if not resp_data.get("success"):
            return False, resp_data.get("message") or "License activation rejected by portal"

        b64_lic = resp_data.get("license_b64")
        if not b64_lic:
            return False, "Portal response missing license container payload"

        try:
            container_bytes = base64.b64decode(b64_lic)
        except Exception as exc:
            return False, f"Invalid base64 payload from portal: {exc}"

        return self.activate_from_bytes(container_bytes)

    def validate_online(self, portal_url: str | None = None) -> tuple[bool, str]:
        """Heartbeat check against vendor portal to sync renewals or revocations."""
        if not self._data or not self._data.license_id:
            return False, "No active license to validate"

        import json
        import urllib.request
        import urllib.error
        from app.core.config import settings

        base_url = (portal_url or settings.LICENSE_PORTAL_URL).rstrip("/")
        if base_url.endswith("/api/validate.php"):
            endpoint = base_url
        elif base_url.endswith("/api/activate.php"):
            endpoint = base_url.replace("/api/activate.php", "/api/validate.php")
        else:
            endpoint = f"{base_url}/api/validate.php"
        host_fp = get_machine_fingerprint()

        payload = {
            "action": "validate",
            "license_id": self._data.license_id,
            "fingerprint": host_fp,
            "app_version": settings.APP_VERSION,
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": f"PAXS-Archive/{settings.APP_VERSION}"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.warning("License portal heartbeat check failed (offline): %s", exc)
            return False, f"Heartbeat check unreachable: {exc}"

        if resp_data.get("revoked"):
            self._status = LicenseStatus.INVALID_SIGNATURE
            self._status_message = f"License was revoked remotely: {resp_data.get('reason', 'Revocation by vendor')}"
            return False, self._status_message

        # Check if portal delivered a renewed/upgraded license container
        if resp_data.get("renewed_license_b64"):
            try:
                container_bytes = base64.b64decode(resp_data["renewed_license_b64"])
                ok, msg = self.activate_from_bytes(container_bytes)
                if ok:
                    log.info("License automatically renewed online from portal: %s", self._data.license_id)
            except Exception as exc:
                log.warning("Failed to apply online renewed license: %s", exc)

        return True, "License validated successfully with online portal"

    def has_feature(self, feature_name: str) -> bool:
        """Checks if a specific feature is licensed and enabled."""
        if not self.is_active or not self._data:
            return False
        return feature_name.lower() in [f.lower() for f in self._data.features]

    def check_user_seat(self, current_user_count: int) -> bool:
        """Checks if current active users exceed maximum allowed seats."""
        if not self.is_active or not self._data:
            return False
        return current_user_count <= self._data.max_users

    def check_study_quota(self, current_yearly_studies: int) -> bool:
        """Checks if current annual studies exceed quota."""
        if not self.is_active or not self._data:
            return False
        return current_yearly_studies <= self._data.max_studies_per_year

    def get_summary(self) -> dict[str, Any]:
        """Returns structured license status summary for APIs and dashboards."""
        host_fp = get_machine_fingerprint()
        hw_info = get_hardware_info()

        summary: dict[str, Any] = {
            "status": self._status.value,
            "status_message": self._status_message,
            "is_active": self.is_active,
            "server_fingerprint": host_fp,
            "hardware_info": hw_info,
            "last_checked_at": self._last_checked_at.isoformat() if self._last_checked_at else None,
            "license_file_present": self.license_file_path.exists(),
            "license_file_path": str(self.license_file_path),
        }

        if self._data:
            now = datetime.now(timezone.utc)
            days_remaining = None
            if self._data.expires_at:
                delta = self._data.expires_at - now
                days_remaining = max(0, delta.days)

            summary.update({
                "license_id": self._data.license_id,
                "edition": self._data.edition.upper(),
                "customer_name": self._data.customer_name,
                "customer_organization": self._data.customer_organization,
                "customer_email": self._data.customer_email,
                "issued_at": self._data.issued_at.isoformat(),
                "expires_at": self._data.expires_at.isoformat() if self._data.expires_at else None,
                "is_perpetual": self._data.expires_at is None,
                "days_remaining": days_remaining,
                "max_users": self._data.max_users,
                "max_studies_per_year": self._data.max_studies_per_year,
                "features": self._data.features,
                "machine_fingerprint": self._data.machine_fingerprint,
                "hardware_matched": (
                    self._data.machine_fingerprint == "*" or self._data.machine_fingerprint.upper() == host_fp.upper()
                ),
            })
        else:
            summary.update({
                "license_id": None,
                "edition": "UNLICENSED",
                "customer_name": None,
                "customer_organization": None,
                "issued_at": None,
                "expires_at": None,
                "is_perpetual": False,
                "days_remaining": 0,
                "max_users": 0,
                "max_studies_per_year": 0,
                "features": [],
                "machine_fingerprint": None,
                "hardware_matched": False,
            })

        return summary


_LICENSE_MANAGER_INSTANCE: LicenseManager | None = None


def get_license_manager() -> LicenseManager:
    """Returns the global LicenseManager singleton."""
    global _LICENSE_MANAGER_INSTANCE
    if _LICENSE_MANAGER_INSTANCE is None:
        _LICENSE_MANAGER_INSTANCE = LicenseManager()
    return _LICENSE_MANAGER_INSTANCE
