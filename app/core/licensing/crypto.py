"""PAXS Cryptographic License Engine.

Handles:
- RSA-2048 digital signature generation (PSS padding + SHA-256)
- RSA-2048 digital signature verification with embedded public key
- AES-256-GCM license container encryption & decryption
- Canonical JSON serialization for tamper-proof verification
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, Field

log = logging.getLogger("paxs.licensing.crypto")

# Default embedded PAXS Public Key (RSA-2048) for verifying signed licenses
DEFAULT_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2TEcG4VYlBoyBoUabo43
pWA0t4fHj4EZZvHrG2/hZzPmbHuyV+mtGHH1b2l01C/vE1dFvQQT1uHziP4X5gW4
cyNjKRdKR9JM6BCzbS2nrGXPDguqaLx3quR2rehimpq0zKw5rp3f176AIIv4hEA6
V/5b7zVDakMqhEN38ruQYJQ45q1LpxbMGh/x+1pdHNFW7Ebdz4BATd8S9ekCCOp5
FzDrFcJEZiSJieKnxhUXtfLxnyzt02UFgRNrQbI+eaI/Ib73PgOi7G5vyFEZbhyY
GL5kbjLeddKdARRUR7npaWR9/HEgNMrZB8sUtHV47Gm8a+FivJTFP1KljkB7HMT0
YQIDAQAB
-----END PUBLIC KEY-----"""

# Default AES-256 secret for container wrapping (32-byte SHA-256 digest)
_DEFAULT_AES_KEY = hashes.Hash(hashes.SHA256(), backend=default_backend())
_DEFAULT_AES_KEY.update(b"PAXS_ARCHIVE_LIC_SEC_2026_AES25")
_DEFAULT_AES_KEY = _DEFAULT_AES_KEY.finalize()


class LicenseData(BaseModel):
    """Structured license certificate payload."""

    license_id: str = Field(..., description="Unique license identifier, e.g. PAXS-HOSP-2026-A1B2-C3D4")
    version: str = Field("1.0", description="License schema version")
    customer_name: str = Field(..., description="Licensee organization or person name")
    customer_email: str | None = Field(None, description="Contact email")
    customer_organization: str | None = Field(None, description="Hospital or clinic name")
    customer_country: str | None = Field(None, description="Country code / name")
    product: str = Field("PAXS Archive & Viewer", description="Licensed product name")
    edition: str = Field("hospital", description="clinic | hospital | enterprise | trial")
    machine_fingerprint: str = Field(
        "*",
        description="Target hardware fingerprint (32-char hex) or '*' for wildcard/cloud",
    )
    issued_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="License issue timestamp (UTC)",
    )
    expires_at: datetime | None = Field(
        None,
        description="License expiration timestamp (UTC) or None for perpetual",
    )
    max_users: int = Field(100, description="Max concurrent/active user seats")
    max_studies_per_year: int = Field(100000, description="Max DICOM studies ingestible per year")
    features: list[str] = Field(
        default_factory=lambda: [
            "mpr_3d",
            "recist",
            "lung_rads",
            "agatston",
            "pe_clot",
            "breast_density",
            "hl7_router",
            "fhir_gateway",
            "storage_tiering",
        ],
        description="Enabled clinical & system feature flags",
    )
    signature: str | None = Field(None, description="Base64-encoded RSA-2048-PSS signature")


def get_canonical_payload_bytes(data: LicenseData | dict[str, Any]) -> bytes:
    """Produces deterministic, canonical JSON bytes without the signature field for hashing."""
    if isinstance(data, LicenseData):
        raw_dict = data.model_dump(mode="json")
    else:
        raw_dict = dict(data)

    raw_dict.pop("signature", None)

    # Convert datetimes to ISO format if present
    for k in ("issued_at", "expires_at"):
        val = raw_dict.get(k)
        if isinstance(val, datetime):
            raw_dict[k] = val.isoformat()

    # Sort keys for deterministic hashing
    canonical_json = json.dumps(raw_dict, sort_keys=True, separators=(",", ":"))
    return canonical_json.encode("utf-8")


def get_public_key() -> rsa.RSAPublicKey:
    """Loads RSA public key from environment or embedded default."""
    pem_env = os.environ.get("PAXS_LICENSE_PUBLIC_KEY")
    pem_path = os.environ.get("PAXS_LICENSE_PUBLIC_KEY_PATH")

    pem_bytes: bytes
    if pem_env:
        pem_bytes = pem_env.encode("utf-8")
    elif pem_path and os.path.isfile(pem_path):
        with open(pem_path, "rb") as f:
            pem_bytes = f.read()
    else:
        pem_bytes = DEFAULT_PUBLIC_KEY_PEM.encode("utf-8")

    key = serialization.load_pem_public_key(pem_bytes, backend=default_backend())
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("Provided public key is not an RSA public key")
    return key


def sign_license(data: LicenseData, private_key_pem: bytes, password: bytes | None = None) -> LicenseData:
    """Signs a LicenseData payload with an RSA-2048 private key using PSS padding."""
    priv_key = serialization.load_pem_private_key(
        private_key_pem, password=password, backend=default_backend()
    )
    if not isinstance(priv_key, rsa.RSAPrivateKey):
        raise ValueError("Provided private key is not an RSA private key")

    payload_bytes = get_canonical_payload_bytes(data)
    sig_bytes = priv_key.sign(
        payload_bytes,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    data.signature = base64.b64encode(sig_bytes).decode("ascii")
    return data


def verify_license_signature(data: LicenseData, public_key: rsa.RSAPublicKey | None = None) -> bool:
    """Verifies the RSA-2048 signature (PSS with PKCS1v15 fallback) on the LicenseData payload."""
    if not data.signature:
        log.warning("License %s missing signature field", data.license_id)
        return False

    if public_key is None:
        try:
            public_key = get_public_key()
        except Exception as exc:
            log.error("Failed to load license public key: %s", exc)
            return False

    try:
        sig_bytes = base64.b64decode(data.signature.encode("ascii"))
        payload_bytes = get_canonical_payload_bytes(data)

        # 1. Primary: Try RSA-PSS
        try:
            public_key.verify(
                sig_bytes,
                payload_bytes,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
            return True
        except (InvalidSignature, ValueError):
            pass

        # 2. Compatibility: Try PKCS1v15 (standard PHP openssl_sign)
        try:
            public_key.verify(
                sig_bytes,
                payload_bytes,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return True
        except (InvalidSignature, ValueError):
            pass

        log.warning("License signature verification failed for %s", data.license_id)
        return False
    except Exception as exc:
        log.warning("License signature verification error for %s: %s", data.license_id, exc)
        return False


def encrypt_license_container(data: LicenseData, aes_key: bytes | None = None) -> bytes:
    """Encrypts signed LicenseData into a binary .lic container with AES-256-GCM.
    
    Structure: [4 bytes Magic: "PAXS"] + [12 bytes IV] + [Ciphertext + 16 bytes Tag]
    """
    key = aes_key or (os.environ.get("PAXS_LICENSE_AES_KEY", "").encode("utf-8") if os.environ.get("PAXS_LICENSE_AES_KEY") else _DEFAULT_AES_KEY)
    if len(key) != 32:
        h = hashes.Hash(hashes.SHA256(), backend=default_backend())
        h.update(key)
        key = h.finalize()

    aesgcm = AESGCM(key)
    iv = os.urandom(12)
    payload_json = data.model_dump_json()
    encrypted = aesgcm.encrypt(iv, payload_json.encode("utf-8"), b"PAXS_LIC_v1")
    return b"PAXS" + iv + encrypted


def decrypt_license_container(container_bytes: bytes, aes_key: bytes | None = None) -> LicenseData:
    """Decrypts a binary .lic container or plain JSON into LicenseData."""
    # Support plain JSON format directly (for human-readable trial licenses or debugging)
    trimmed = container_bytes.strip()
    if trimmed.startswith(b"{") and trimmed.endswith(b"}"):
        try:
            return LicenseData.model_validate_json(trimmed.decode("utf-8"))
        except Exception:
            pass

    if len(container_bytes) < 32 or not container_bytes.startswith(b"PAXS"):
        raise ValueError("Invalid PAXS license container format (missing header magic)")

    iv = container_bytes[4:16]
    ciphertext_and_tag = container_bytes[16:]

    key = aes_key or (os.environ.get("PAXS_LICENSE_AES_KEY", "").encode("utf-8") if os.environ.get("PAXS_LICENSE_AES_KEY") else _DEFAULT_AES_KEY)
    if len(key) != 32:
        h = hashes.Hash(hashes.SHA256(), backend=default_backend())
        h.update(key)
        key = h.finalize()

    aesgcm = AESGCM(key)
    try:
        decrypted_json = aesgcm.decrypt(iv, ciphertext_and_tag, b"PAXS_LIC_v1")
        return LicenseData.model_validate_json(decrypted_json.decode("utf-8"))
    except InvalidTag:
        raise ValueError("License file decryption failed: invalid key or corrupted file")


def generate_rsa_key_pair() -> tuple[bytes, bytes]:
    """Utility to generate a fresh RSA-2048 private/public key pair (PEM bytes)."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem
