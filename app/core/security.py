"""Security primitives — password hashing (PBKDF2, stdlib) and JWT (HS256, stdlib).

No third-party auth dependencies: keeps the hospital-install surface small and
auditable. Swap for python-jose/passlib if you prefer the ecosystem standard.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Any

from app.core.config import settings

# HttpOnly session cookie that carries the opaque REFRESH token (auth plan
# §2.2): the browser rides it on every same-origin request (the viewer auth
# gate), and POST /auth/refresh rotates it on use. SameSite=Lax keeps the
# cookie off cross-site POSTs (CSRF) while still sending it on top-level GET
# navigations like /login -> /viewer. The cookie lives for the refresh
# lifetime; the short access token is refreshed silently via /auth/refresh.
COOKIE_MAX_AGE = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600


def set_session_cookie(response, token: str) -> None:
    """Stamp the HttpOnly refresh-token cookie onto a login/refresh response."""
    response.set_cookie(
        settings.AUTH_COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        # H06: the SameSite policy is operator-configured (and validated at
        # startup in production) instead of being hardcoded here.
        samesite=settings.AUTH_COOKIE_SAMESITE,
        secure=settings.AUTH_COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response) -> None:
    """Expire the session cookie (logout)."""
    response.delete_cookie(settings.AUTH_COOKIE_NAME, path="/")


# --------------------------------------------------------------------------- #
# Refresh tokens (opaque, stored SHA-256 hashed, rotated per use)
# --------------------------------------------------------------------------- #
def generate_refresh_token(family_id: str) -> str:
    """A fresh opaque refresh token for a session family.

    The token embeds the family id ("<family_id>.<random>") so that when a
    rotated-out token is replayed the server can still identify and revoke
    the whole family even if its session row is gone (plan §4.3). Only the
    SHA-256 hash is ever persisted.
    """
    return f"{family_id}.{secrets.token_urlsafe(32)}"


def hash_refresh_token(token: str) -> str:
    """SHA-256 hex of a refresh token — the only form ever persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_token_family(token: str) -> str | None:
    """Extract the family id embedded in a refresh token (for theft recovery)."""
    try:
        fam, _ = token.split(".", 1)
        return fam if fam else None
    except Exception:
        return None


def session_token_from_cookie(value: str | None) -> str | None:
    """Return the raw cookie value when it is structurally plausible."""
    if not value or len(value) < 16 or len(value) > 256:
        return None
    return value

ALGORITHM = "HS256"
_PBKDF2_ITERATIONS = 200_000


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 (per-user random salt)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return (
        f"pbkdf2_sha256${_PBKDF2_ITERATIONS}$"
        f"{base64.urlsafe_b64encode(salt).decode('ascii')}$"
        f"{base64.urlsafe_b64encode(dk).decode('ascii')}"
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iterations, salt_b64, dk_b64 = stored.split("$")
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(dk_b64.encode("ascii"))
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# JWT (HS256)
# --------------------------------------------------------------------------- #
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_access_token(
    subject: str, role: str, secret: str, expires_minutes: int
) -> str:
    now = int(time.time())
    header = {"alg": ALGORITHM, "typ": "JWT"}
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + expires_minutes * 60,
    }
    header_b = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b}.{payload_b}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b}.{payload_b}.{_b64url(signature)}"


def decode_token(token: str, secret: str) -> dict[str, Any] | None:
    try:
        header_b, payload_b, sig_b = token.split(".")
        signing_input = f"{header_b}.{payload_b}".encode("utf-8")
        expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expected), sig_b):
            return None
        payload = json.loads(_b64url_decode(payload_b))
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None
