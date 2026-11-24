"""CSRF protection for cookie-authenticated requests (B-19 / FS-10).

Threat model
------------
The session cookie (``AUTH_COOKIE_NAME``) is HttpOnly and the browser attaches
it to every same-origin request — including requests forged by a page on another
origin (the classic CSRF shape). ``SameSite=Lax`` blocks the cross-site POST
case, but it is not sufficient on its own: older browsers, ``SameSite=None``
deployments, and same-site-but-cross-origin (sub-domain) attackers all defeat it.
A cookie-authenticated ``PATCH /api/v1/config`` that changes a security setting
is exactly the kind of request a CSRF attacker wants to forge.

Defence: signed double-submit bound to the session
--------------------------------------------------
    csrf_token = HMAC-SHA256(SECRET_KEY, "paxs-csrf-v1:" + <session cookie>)

* the server stamps it into a **JS-readable** cookie (``CSRF_COOKIE_NAME``) on
  every login / refresh;
* the SPA echoes it back in the ``X-CSRF-Token`` header on unsafe requests;
* the server recomputes the HMAC from the HttpOnly session cookie it already
  holds and compares in constant time.

An attacker on another origin can read neither cookie (same-origin policy +
HttpOnly), so it can neither learn nor forge the token. Binding the token to the
*session* value — rather than a random value that merely sits in a cookie — also
defeats the sub-cookie-injection weakness of naive double-submit.

Scope
-----
Only requests actually authenticated **by the cookie** are checked. A request
carrying ``Authorization: Bearer …`` is an API client that does not rely on
ambient credentials and is exempt by design. The session lifecycle
(``/api/v1/auth/*``: login / refresh / logout) is exempt too — it acts on the
credential itself rather than on application state, and login has no session
cookie yet. A rejected request is answered **403**.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from http.cookies import CookieError, SimpleCookie
from typing import Any

from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.security import COOKIE_MAX_AGE

# The SPA reads the token from this JS-readable cookie and echoes it in the
# header below. The session cookie itself stays HttpOnly.
CSRF_COOKIE_NAME = "paxs_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

# Domain separation: a value minted for CSRF must never be replayable as a
# session/access token even if the two ever share a secret input.
_TOKEN_DOMAIN = b"paxs-csrf-v1:"

# Safe methods never change state and never need a token.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# The session lifecycle is exempt (see the module docstring).
CSRF_EXEMPT_PREFIXES: tuple[str, ...] = ("/api/v1/auth/",)


# --------------------------------------------------------------------------- #
# Token minting / verification (stateless — the server stores nothing)
# --------------------------------------------------------------------------- #
def csrf_token_for(session_token: str) -> str:
    """The double-submit token bound to ``session_token`` (the refresh cookie).

    Deterministic and stateless: recomputing it from the session cookie the
    server already has is what makes verification possible without any storage.
    An attacker cannot reproduce it without the SECRET_KEY *and* the victim's
    session token.
    """
    if not session_token:
        return ""
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        _TOKEN_DOMAIN + session_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def csrf_token_matches(session_token: str, provided: str | None) -> bool:
    """Constant-time check of a caller-supplied token against ``session_token``."""
    expected = csrf_token_for(session_token)
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)


def set_csrf_cookie(response: Any, session_token: str) -> None:
    """Stamp the JS-readable double-submit token onto a login/refresh response.

    Deliberately *not* HttpOnly: the SPA must read it to echo it back. That is
    safe because the token alone is useless without the HttpOnly session cookie
    it is bound to.
    """
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token_for(session_token),
        max_age=COOKIE_MAX_AGE,
        httponly=False,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        secure=settings.AUTH_COOKIE_SECURE,
        path="/",
    )


def clear_csrf_cookie(response: Any) -> None:
    """Expire the CSRF cookie on logout, mirroring ``clear_session_cookie``."""
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


# --------------------------------------------------------------------------- #
# Request inspection (works on a raw ASGI scope, so the middleware and the
# FastAPI dependency share exactly one decision path)
# --------------------------------------------------------------------------- #
def _header(scope: Mapping[str, Any], name: str) -> str:
    """First value of a request header (case-insensitive), or ""."""
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", ()):
        if key.lower() == wanted:
            return value.decode("latin-1")
    return ""


def _cookie(scope: Mapping[str, Any], name: str) -> str:
    """One cookie value out of the raw Cookie header, or "" (never raises)."""
    raw = _header(scope, "cookie")
    if not raw:
        return ""
    try:
        jar = SimpleCookie()
        jar.load(raw)
        morsel = jar.get(name)
        return morsel.value if morsel else ""
    except (CookieError, ValueError, KeyError):  # a malformed Cookie header must not 500
        return ""


def _relative_path(scope: Mapping[str, Any]) -> str:
    """Request path with any reverse-proxy mount prefix removed (V2-4)."""
    path = scope.get("path", "") or ""
    root = scope.get("root_path") or ""
    if root and path.startswith(root):
        return path[len(root):] or "/"
    return path


def _bearer_present(scope: Mapping[str, Any]) -> bool:
    """True when the caller presented an Authorization: Bearer header."""
    return _header(scope, "authorization").lower().startswith("bearer ")


def _is_exempt(scope: Mapping[str, Any]) -> bool:
    path = _relative_path(scope)
    return any(path.startswith(prefix) for prefix in CSRF_EXEMPT_PREFIXES)


def csrf_violation(scope: Mapping[str, Any]) -> bool:
    """True when the request must be rejected for a missing/invalid CSRF token.

    A request is only subject to CSRF when it (a) mutates state, (b) is
    authenticated by the ambient session cookie rather than a bearer token, and
    (c) is not part of the session lifecycle. Everything else — safe methods,
    API clients, and unauthenticated calls (which the auth layer rejects with
    401 anyway) — passes through untouched.
    """
    if not settings.CSRF_PROTECTION_ENABLED:
        return False
    if (scope.get("method") or "").upper() in _SAFE_METHODS:
        return False
    if _bearer_present(scope):
        # An explicit Authorization header is not ambient credentials: a
        # cross-site attacker cannot set it, so CSRF does not apply.
        return False
    session_token = _cookie(scope, settings.AUTH_COOKIE_NAME)
    if not session_token:
        # No cookie session — the auth dependency answers 401; a CSRF 403 here
        # would only mask that.
        return False
    if _is_exempt(scope):
        return False
    return not csrf_token_matches(session_token, _header(scope, CSRF_HEADER_NAME))


def csrf_failure_response() -> JSONResponse:
    """The 403 returned when a cookie-authenticated state change lacks a token."""
    return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)


class CsrfProtectionMiddleware:
    """Reject cookie-authenticated unsafe requests without a valid CSRF token.

    A plain ASGI middleware (like ``SecurityHeadersMiddleware``) so streaming
    and file responses pass through untouched. Inert while
    ``CSRF_PROTECTION_ENABLED`` is false.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and csrf_violation(scope):
            await csrf_failure_response()(scope, receive, send)
            return
        await self.app(scope, receive, send)
