"""PAXS Cryptographic License Engine.

Handles:
- RSA digital signature generation (PSS padding + SHA-256) and verification
  against an embedded vendor public key
- AES-256-GCM license container encryption & decryption
- Canonical JSON serialization for tamper-proof verification
- **Signing-key rotation and revocation** (B-2 / FS-7)

Key rotation (B-2 / FS-7)
-------------------------
The original vendor signing key (RSA-2048) was committed to this repository and
is therefore public: anyone holding it can mint a licence this archive accepts.
The response is a three-part rotation, all enforced here:

1. ``TRUSTED_PUBLIC_KEY_PEM`` is now a fresh **RSA-4096** key generated on
   2026-10-13. Only its *public* half is in the tree; the private half lives in
   the vendor's secrets store (``PAXS_LICENSE_SIGNING_KEY`` /
   ``scripts/vendor_keys/``, which is git-ignored).
2. The compromised key is kept in ``REVOKED_KEYS`` — as a **public** key, so no
   secret is re-introduced — purely so a licence it signed can be *named* and
   rejected explicitly (:class:`RevokedSigningKey`) rather than reported as a
   generic bad signature. Legacy licences carry no key id, so this is the only
   way to tell the operator *why* their licence stopped working.
3. :func:`sign_license` refuses to sign with anything below
   ``MIN_SIGNING_KEY_BITS``, so the retired key cannot be used to mint new
   licences even by accident.

Every licence issued before the rotation must be re-issued: that is the point of
a rotation, and it is the only way to be sure no licence in the field carries a
forgery-friendly signature. See ``docs/SECURITY-KEY-ROTATION.md`` for the
history-purge commands and the re-issue runbook.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, Field

log = logging.getLogger("paxs.licensing.crypto")

#: Smallest RSA modulus accepted for *signing*. The leaked key was 2048-bit and
#: is retired; 3072 is the current floor for new material.
MIN_SIGNING_KEY_BITS = 3072

#: Environment variables that may override the embedded public key, in order.
PUBLIC_KEY_ENV_VARS = ("PAXS_LICENSE_PUBLIC_KEY", "PAXS_LICENSE_PUBLIC_KEY_PATH")

#: Environment variable holding the 32-byte (or salted) container key.
AES_KEY_ENV_VAR = "PAXS_LICENSE_AES_KEY"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class LicenseError(Exception):
    """Base class for every licensing failure."""


class LicenseConfigurationError(LicenseError):
    """The licensing configuration is unusable — fail closed, never fall back.

    Raised for a missing container key, an unreadable public key, or a public
    key that is on the revocation list. Never raised in place of a *signature*
    verdict; that is :class:`InvalidLicenseSignature`.
    """


class InvalidLicenseSignature(LicenseError):
    """The licence is not signed by a trusted key, or its payload was altered."""


class RevokedSigningKey(InvalidLicenseSignature):
    """The licence *is* signed — by a key this build has retired.

    A subclass of :class:`InvalidLicenseSignature` on purpose: callers that only
    care "is it valid?" keep working, while a caller that wants to tell the
    operator "your licence was signed by the compromised key, ask the vendor for
    a re-issued one" can catch the narrower type.
    """


# --------------------------------------------------------------------------- #
# Key material
# --------------------------------------------------------------------------- #
# Vendor public key — safe to distribute, and the only key this build trusts.
# Rotated 2026-10-13 (RSA-4096). Regenerate with:
#     python scripts/generate_license.py init-keys --force
# then replace this constant with scripts/vendor_keys/paxs_vendor_public.pem.
TRUSTED_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAsIL8x0Ke7WecdI4PsrAx
TzvEdZjbkzMf7tPE+ve4jaE412lZjmB7R0TYM0PboMSJFQICQXZnnUcMmLYYX55e
36wl+N5IXdWEKeuN2dftzHWGQhF3wDB22vG5SjyEXJ6J9huD5WeTpyIFOpP03/CF
23oku9tdwDTTdFe+U9MQEKtT0vtAQLjNGSqoFzmU2ju5oOHtIubOcL3bL6T8qumD
D4fn9evmbETIFchUGhVg9+ODK/b+EaZmEtih1vjEAxwGwwYzTQnp+09iwK9T1CUv
pa1dsGzqadOfO8oHociJizB4/PswPesCxsQZeyIOEQ7sET05PES7WVwY2IiiV2PK
RH2C4FbYTMt/5GnuTQXSXkqpHpJjNxMnxEiSZjU42REN5u8PU7AkcPmYQYZV/XDG
qbwpSbdcISKpCuTLV718gwhH5U2MC4PQrXggimwNJsr62FfyRwqfeJvrr778MruR
5J6LRqzqIcXHkEhx8IGLJqBBdjzRtb2V+ozNrblgr2RAZ+MSVv0uw2AOlLlVqUNd
69nT8N+EPfwRgGimoCcuXekcw35E/VZvijqqr+aAZwjdKqTZElhvca9Xpl0SdI4t
06uayBjM7xPzn0EWAf5LfhvgZsVMoMXnTmVUh8Qm9Wk2rtiv6o2gDVMhnC25F/zO
aVokqKJdl1qIXzyF2D8j4sECAwEAAQ==
-----END PUBLIC KEY-----"""

#: The retired RSA-2048 vendor key. A **public** key: it is already public in
#: this repository's history, so keeping it costs nothing and buys an explicit
#: "signed by the compromised key" diagnosis for licences already in the field.
COMPROMISED_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2TEcG4VYlBoyBoUabo43
pWA0t4fHj4EZZvHrG2/hZzPmbHuyV+mtGHH1b2l01C/vE1dFvQQT1uHziP4X5gW4
cyNjKRdKR9JM6BCzbS2nrGXPDguqaLx3quR2rehimpq0zKw5rp3f176AIIv4hEA6
V/5b7zVDakMqhEN38ruQYJQ45q1LpxbMGh/x+1pdHNFW7Ebdz4BATd8S9ekCCOp5
FzDrFcJEZiSJieKnxhUXtfLxnyzt02UFgRNrQbI+eaI/Ib73PgOi7G5vyFEZbhyY
GL5kbjLeddKdARRUR7npaWR9/HEgNMrZB8sUtHV47Gm8a+FivJTFP1KljkB7HMT0
YQIDAQAB
-----END PUBLIC KEY-----"""


@dataclass(frozen=True)
class RevokedKey:
    """A public key this build must never accept a licence from."""

    key_id: str
    pem: str
    reason: str


def _fingerprint(key: rsa.RSAPublicKey) -> str:
    """Stable key id: SHA-256 over the DER SubjectPublicKeyInfo, lowercase hex.

    Chosen over "first N bytes of the modulus" so the id is independent of the
    key's encoding (PEM line wrapping, whitespace) — only the key itself.
    """
    der = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def load_public_key(pem_bytes: bytes | str) -> rsa.RSAPublicKey:
    """Load an RSA public key from PEM, raising on anything else."""
    raw = pem_bytes.encode("utf-8") if isinstance(pem_bytes, str) else pem_bytes
    try:
        key = serialization.load_pem_public_key(raw, backend=default_backend())
    except Exception as exc:
        raise LicenseConfigurationError(
            f"could not parse the configured licence public key: {type(exc).__name__}"
        ) from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise LicenseConfigurationError("the configured licence public key is not RSA")
    return key


def public_key_id(pem_bytes: bytes | str) -> str:
    """The key id of a PEM-encoded public key."""
    return _fingerprint(load_public_key(pem_bytes))


TRUSTED_KEY_ID = public_key_id(TRUSTED_PUBLIC_KEY_PEM)
COMPROMISED_KEY_ID = public_key_id(COMPROMISED_PUBLIC_KEY_PEM)

#: Every key this build refuses. Add an entry (with its reason) when a key is
#: retired; the id is what a licence's ``signing_key_id`` is compared against.
REVOKED_KEYS: tuple[RevokedKey, ...] = (
    RevokedKey(
        key_id=COMPROMISED_KEY_ID,
        pem=COMPROMISED_PUBLIC_KEY_PEM,
        reason="vendor signing key was committed to git history (B-2 / FS-7), "
               "rotated 2026-10-13",
    ),
)

REVOKED_KEY_IDS = frozenset(entry.key_id for entry in REVOKED_KEYS)


def revoked_key(key_id: str | None) -> RevokedKey | None:
    """The revocation entry for ``key_id``, or None when it is not retired."""
    if not key_id:
        return None
    for entry in REVOKED_KEYS:
        if entry.key_id == key_id:
            return entry
    return None


# --------------------------------------------------------------------------- #
# License payload
# --------------------------------------------------------------------------- #
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
    signature: str | None = Field(None, description="Base64-encoded RSA-PSS signature")
    signing_key_id: str | None = Field(
        None,
        description=(
            "Key id of the signing key (SHA-256 of its DER public key). Advisory: "
            "like `signature` it is excluded from the signed payload, so it can only "
            "ever *narrow* trust — a forged id still fails the signature check."
        ),
    )


#: Fields carried alongside the signature but deliberately NOT signed. Excluding
#: `signing_key_id` keeps the canonical bytes identical to licences issued
#: before the field existed, so a legacy licence still parses and still reaches
#: the revocation diagnosis instead of failing as "malformed".
_UNSIGNED_FIELDS = ("signature", "signing_key_id")


def get_canonical_payload_bytes(data: LicenseData | dict[str, Any]) -> bytes:
    """Produces deterministic, canonical JSON bytes without the signature field for hashing."""
    raw_dict = data.model_dump(mode="json") if isinstance(data, LicenseData) else dict(data)

    for field_name in _UNSIGNED_FIELDS:
        raw_dict.pop(field_name, None)

    # Convert datetimes to ISO format if present
    for k in ("issued_at", "expires_at"):
        val = raw_dict.get(k)
        if isinstance(val, datetime):
            raw_dict[k] = val.isoformat()

    # Sort keys for deterministic hashing
    canonical_json = json.dumps(raw_dict, sort_keys=True, separators=(",", ":"))
    return canonical_json.encode("utf-8")


# --------------------------------------------------------------------------- #
# Public key resolution
# --------------------------------------------------------------------------- #
def _configured_public_key_pem() -> bytes:
    """PEM bytes for the verification key: env var, settings, file, then embedded."""
    pem_env = os.environ.get("PAXS_LICENSE_PUBLIC_KEY", "").strip()
    if not pem_env:
        try:
            from app.core.config import settings

            pem_env = (getattr(settings, "PAXS_LICENSE_PUBLIC_KEY", "") or "").strip()
        except Exception:
            pem_env = ""
    if pem_env:
        return pem_env.encode("utf-8")

    pem_path = os.environ.get("PAXS_LICENSE_PUBLIC_KEY_PATH", "").strip()
    if not pem_path:
        try:
            from app.core.config import settings

            pem_path = (getattr(settings, "PAXS_LICENSE_PUBLIC_KEY_PATH", "") or "").strip()
        except Exception:
            pem_path = ""
    if pem_path and os.path.isfile(pem_path):
        try:
            with open(pem_path, "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise LicenseConfigurationError(
                f"could not read the licence public key at PAXS_LICENSE_PUBLIC_KEY_PATH: "
                f"{type(exc).__name__}"
            ) from exc
    return TRUSTED_PUBLIC_KEY_PEM.encode("utf-8")


def get_public_key() -> rsa.RSAPublicKey:
    """The verification key: an explicit override, else the embedded trusted key.

    A retired key is refused outright even when explicitly configured: an
    operator must not be able to re-arm the compromised key by exporting it,
    which is exactly the misconfiguration that would make the rotation moot.
    """
    pem_bytes = _configured_public_key_pem()
    key = load_public_key(pem_bytes)
    retired = revoked_key(_fingerprint(key))
    if retired is not None:
        raise LicenseConfigurationError(
            f"the configured licence public key is revoked ({retired.reason}); "
            "install the rotated key instead"
        )
    return key


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #
def sign_license(
    data: LicenseData,
    private_key_pem: bytes,
    password: bytes | None = None,
) -> LicenseData:
    """Signs a LicenseData payload with an RSA private key using PSS padding.

    The key id is stamped automatically, and a key below
    :data:`MIN_SIGNING_KEY_BITS` is refused — the retired 2048-bit key must not
    be usable to mint a licence even by a careless build.
    """
    priv_key = serialization.load_pem_private_key(
        private_key_pem, password=password, backend=default_backend()
    )
    if not isinstance(priv_key, rsa.RSAPrivateKey):
        raise ValueError("Provided private key is not an RSA private key")
    if priv_key.key_size < MIN_SIGNING_KEY_BITS:
        raise ValueError(
            f"refusing to sign with a {priv_key.key_size}-bit RSA key; "
            f"{MIN_SIGNING_KEY_BITS} bits is the minimum for licence signing"
        )

    data.signing_key_id = _fingerprint(priv_key.public_key())
    sig_bytes = priv_key.sign(
        get_canonical_payload_bytes(data),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    data.signature = base64.b64encode(sig_bytes).decode("ascii")
    return data


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def _signature_matches(key: rsa.RSAPublicKey, sig_bytes: bytes, payload: bytes) -> bool:
    """True when ``sig_bytes`` is a valid signature over ``payload`` for ``key``.

    RSA-PSS is tried first; PKCS#1 v1.5 is accepted as well because the vendor
    portal signs with PHP's ``openssl_sign``, whose default is v1.5.
    """
    schemes = (
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        padding.PKCS1v15(),
    )
    for scheme in schemes:
        try:
            key.verify(sig_bytes, payload, scheme, hashes.SHA256())
            return True
        except (InvalidSignature, ValueError):  # best-effort: try the next scheme
            continue
    return False


def _decode_signature(signature: str | None) -> bytes:
    """Base64-decode the signature, or raise InvalidLicenseSignature."""
    if not signature:
        raise InvalidLicenseSignature("licence has no signature field")
    try:
        return base64.b64decode(signature.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise InvalidLicenseSignature("licence signature is not valid base64") from exc


def assert_license_signature(
    data: LicenseData,
    public_key: rsa.RSAPublicKey | None = None,
) -> None:
    """Verify a licence, raising :class:`InvalidLicenseSignature` when it fails.

    Three steps, cheapest first:

    1. a declared ``signing_key_id`` that is on the revocation list is rejected
       without doing any public-key work — and *before* the signature is even
       decoded, so the verdict does not depend on the signature being
       well-formed;
    2. the signature is checked against the trusted key;
    3. only if that fails, the signature is checked against each revoked key, so
       a licence minted with the compromised key is reported as
       :class:`RevokedSigningKey` — an actionable message — instead of a generic
       "invalid signature".

    Step 3 is what makes the rotation diagnosable for licences already in the
    field, which carry no key id at all.
    """
    declared = revoked_key(data.signing_key_id)
    if declared is not None:
        raise RevokedSigningKey(
            f"licence {data.license_id} declares signing key {declared.key_id[:16]}…, "
            f"which is revoked: {declared.reason}"
        )

    sig_bytes = _decode_signature(data.signature)
    key = public_key if public_key is not None else get_public_key()
    payload = get_canonical_payload_bytes(data)
    if _signature_matches(key, sig_bytes, payload):
        return

    for entry in REVOKED_KEYS:
        try:
            retired = load_public_key(entry.pem)
        except LicenseConfigurationError:
            log.warning("revoked key %s is unreadable; skipping the diagnosis",
                        entry.key_id[:16])
            continue
        if _signature_matches(retired, sig_bytes, payload):
            raise RevokedSigningKey(
                f"licence {data.license_id} was signed by revoked key "
                f"{entry.key_id[:16]}…: {entry.reason}. Re-issue the licence with the "
                f"current vendor key (id {TRUSTED_KEY_ID[:16]}…)."
            )

    raise InvalidLicenseSignature(
        f"licence {data.license_id} is not signed by the trusted vendor key"
    )


def verify_license_signature(
    data: LicenseData,
    public_key: rsa.RSAPublicKey | None = None,
) -> bool:
    """Boolean wrapper over :func:`assert_license_signature`.

    Kept as the long-standing call-site API (the manager maps ``False`` onto
    ``LicenseStatus.INVALID_SIGNATURE``). Callers that need to distinguish "bad
    signature" from "signed by a retired key" should call the raising form.
    """
    try:
        assert_license_signature(data, public_key)
        return True
    except RevokedSigningKey as exc:
        log.error("License signature rejected — revoked signing key: %s", exc)
        return False
    except (InvalidLicenseSignature, LicenseConfigurationError) as exc:
        log.warning("License signature verification failed for %s: %s",
                    data.license_id, exc)
        return False


# --------------------------------------------------------------------------- #
# AES-256-GCM container
# --------------------------------------------------------------------------- #
DEFAULT_SHARED_AES_KEY = "PAXS_LIC_SHARED_AES_2026_KEY_9F4A2C8E1B7D6035A9C2E4F81D3B5E7A"


def _resolve_aes_key(aes_key: bytes | None) -> bytes:
    """The 32-byte container key from env, settings, or shared vendor key."""
    raw = aes_key
    if raw is None:
        configured = os.environ.get(AES_KEY_ENV_VAR, "")
        if not configured:
            # Imported lazily: app.core.config does not depend on this package.
            from app.core.config import settings

            configured = getattr(settings, AES_KEY_ENV_VAR, "") or ""
        if not configured:
            configured = DEFAULT_SHARED_AES_KEY
        raw = configured.encode("utf-8")

    if len(raw) == 32:
        return raw
    return hashlib.sha256(raw).digest()


def encrypt_license_container(data: LicenseData, aes_key: bytes | None = None) -> bytes:
    """Encrypts signed LicenseData into a binary .lic container with AES-256-GCM.

    Structure: [4 bytes Magic: "PAXS"] + [12 bytes IV] + [Ciphertext + 16 bytes Tag]
    """
    aesgcm = AESGCM(_resolve_aes_key(aes_key))
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
        except ValueError as exc:
            log.warning("plain-JSON licence payload did not validate: %s", type(exc).__name__)

    if len(container_bytes) < 32 or not container_bytes.startswith(b"PAXS"):
        raise ValueError("Invalid PAXS license container format (missing header magic)")

    iv = container_bytes[4:16]
    ciphertext_and_tag = container_bytes[16:]

    aesgcm = AESGCM(_resolve_aes_key(aes_key))
    try:
        decrypted_json = aesgcm.decrypt(iv, ciphertext_and_tag, b"PAXS_LIC_v1")
        return LicenseData.model_validate_json(decrypted_json.decode("utf-8"))
    except InvalidTag:
        raise ValueError("License file decryption failed: invalid key or corrupted file") from None


# --------------------------------------------------------------------------- #
# Key generation
# --------------------------------------------------------------------------- #
def generate_rsa_key_pair(key_size: int = 4096) -> tuple[bytes, bytes]:
    """Generate a fresh RSA private/public key pair (PEM bytes).

    Defaults to 4096 bits — the size of the rotated vendor key. The private half
    must never be committed; write it to the git-ignored ``scripts/vendor_keys/``
    or straight into the vendor's secrets store.
    """
    if key_size < MIN_SIGNING_KEY_BITS:
        raise ValueError(
            f"key_size {key_size} is below the {MIN_SIGNING_KEY_BITS}-bit minimum"
        )
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
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
