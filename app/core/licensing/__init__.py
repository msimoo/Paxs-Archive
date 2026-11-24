"""PAXS Licensing and Hardware-Bound Protection Package."""

from app.core.licensing.crypto import (
    COMPROMISED_KEY_ID,
    MIN_SIGNING_KEY_BITS,
    REVOKED_KEYS,
    TRUSTED_KEY_ID,
    InvalidLicenseSignature,
    LicenseConfigurationError,
    LicenseData,
    LicenseError,
    RevokedKey,
    RevokedSigningKey,
    assert_license_signature,
    decrypt_license_container,
    encrypt_license_container,
    generate_rsa_key_pair,
    get_canonical_payload_bytes,
    public_key_id,
    revoked_key,
    sign_license,
    verify_license_signature,
)
from app.core.licensing.deps import require_license_feature, require_valid_license
from app.core.licensing.fingerprint import get_hardware_info, get_machine_fingerprint
from app.core.licensing.manager import (
    LicenseManager,
    LicenseStatus,
    get_license_manager,
)

__all__ = [
    "COMPROMISED_KEY_ID",
    "TRUSTED_KEY_ID",
    "MIN_SIGNING_KEY_BITS",
    "REVOKED_KEYS",
    "RevokedKey",
    "LicenseError",
    "LicenseConfigurationError",
    "InvalidLicenseSignature",
    "RevokedSigningKey",
    "LicenseData",
    "LicenseStatus",
    "LicenseManager",
    "get_license_manager",
    "get_machine_fingerprint",
    "get_hardware_info",
    "require_valid_license",
    "require_license_feature",
    "encrypt_license_container",
    "decrypt_license_container",
    "get_canonical_payload_bytes",
    "public_key_id",
    "revoked_key",
    "sign_license",
    "assert_license_signature",
    "verify_license_signature",
    "generate_rsa_key_pair",
]
