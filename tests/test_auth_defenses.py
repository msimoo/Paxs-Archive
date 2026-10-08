"""Auth defenses (plan §3.3/§4.1/§4.2): lockout, audit trail, headers."""

from __future__ import annotations

from app.core.config import settings
from app.db.models import AuthAuditLog
from app.db.session import sync_engine


def _register(client, username="dr.ali", password="secret123"):
    return client.post("/api/v1/auth/register", json={
        "username": username, "password": password, "full_name": "Ali Hassan",
    })


def _login(client, username="dr.ali", password="secret123"):
    return client.post("/api/v1/auth/login",
                       json={"username": username, "password": password})


def _audit_rows() -> list[dict]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            AuthAuditLog.__table__.select().order_by(AuthAuditLog.__table__.c.id)
        ).fetchall()
        return [dict(r._mapping) for r in rows]


# --------------------------------------------------------------------------- #
# Brute-force lockout (plan §4.1)
# --------------------------------------------------------------------------- #
def test_four_failed_attempts_are_generic_and_do_not_lock(client):
    _register(client)
    for _ in range(settings.MAX_FAILED_ATTEMPTS - 1):
        res = _login(client, password="wrongpass")
        assert res.status_code == 401
        assert res.json()["detail"] == "Invalid username or password"
    # Correct password still works on the 4th failed attempt's aftermath.
    assert _login(client).status_code == 200


def test_fifth_attempt_locks_account(client):
    _register(client)
    for _ in range(settings.MAX_FAILED_ATTEMPTS - 1):
        assert _login(client, password="wrongpass").status_code == 401
    res = _login(client, password="wrongpass")
    assert res.status_code == 423
    assert "locked" in res.json()["detail"].lower()


def test_locked_account_rejects_correct_password_with_remaining_time(client):
    _register(client)
    for _ in range(settings.MAX_FAILED_ATTEMPTS):
        _login(client, password="wrongpass")
    # Even the correct password is refused while inside the lockout window.
    res = _login(client)
    assert res.status_code == 423
    assert "minute" in res.json()["detail"]


def test_unknown_username_never_locks(client):
    # No account to lock — always the generic message, never 423.
    for _ in range(settings.MAX_FAILED_ATTEMPTS + 2):
        res = _login(client, username="ghost", password="wrongpass")
        assert res.status_code == 401
    assert _login(client, username="ghost", password="x").status_code == 401


def test_successful_login_resets_failed_attempts(client):
    _register(client)
    for _ in range(settings.MAX_FAILED_ATTEMPTS - 1):
        _login(client, password="wrongpass")
    assert _login(client).status_code == 200
    # Counter is zero again — bad passwords restart from scratch.
    for _ in range(settings.MAX_FAILED_ATTEMPTS - 1):
        assert _login(client, password="wrongpass").status_code == 401
    assert _login(client).status_code == 200


# --------------------------------------------------------------------------- #
# Auth audit trail (plan §3.3)
# --------------------------------------------------------------------------- #
def test_audit_records_login_events(client):
    _register(client)  # writes login_success (via register)
    _login(client, password="bad1")
    _login(client)  # success
    actions = [r["action"] for r in _audit_rows()]
    assert actions.count("login_success") == 2
    assert actions.count("login_failed") == 1
    # Details JSON carried the attempt count.
    failed = [r for r in _audit_rows() if r["action"] == "login_failed"][0]
    assert '"attempt"' in failed["details"]


def test_audit_records_lockout(client):
    _register(client)
    for _ in range(settings.MAX_FAILED_ATTEMPTS):
        _login(client, password="wrongpass")
    actions = [r["action"] for r in _audit_rows()]
    assert "account_locked" in actions


def test_audit_records_logout_and_refresh(client):
    _register(client)
    client.cookies.clear()
    _login(client)
    client.post("/api/v1/auth/refresh")
    client.post("/api/v1/auth/logout")
    actions = [r["action"] for r in _audit_rows()]
    assert "token_refresh" in actions
    assert "logout" in actions


def test_audit_records_theft_detection(client):
    _register(client)
    client.cookies.clear()
    _login(client)
    rotated = client.cookies.get(settings.AUTH_COOKIE_NAME)
    client.post("/api/v1/auth/refresh")  # rotate
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, rotated)  # replay
    client.post("/api/v1/auth/refresh")  # → theft
    actions = [r["action"] for r in _audit_rows()]
    assert "token_theft_detected" in actions


# --------------------------------------------------------------------------- #
# Global security headers (plan §4.2)
# --------------------------------------------------------------------------- #
def test_security_headers_on_api_response(client):
    res = client.get("/api/v1/auth/me")
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in res.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
    assert "camera=()" in res.headers["permissions-policy"]
    # H06 / RFC 6797 §7.2: HSTS is only advertised over HTTPS. The test client
    # speaks plain HTTP, so sending it here would be wrong (it can break LAN
    # access after a protocol switch).
    assert ("strict-transport-security" in res.headers) is bool(settings.HTTPS_ENABLED)


def test_hsts_is_registered_exactly_when_https_is_enabled():
    """H06: the HSTS header follows the HTTPS_ENABLED switch, not a constant."""
    import app.main as main_app

    assert ("Strict-Transport-Security" in main_app._SECURITY_HEADERS) is bool(
        settings.HTTPS_ENABLED
    )
    if settings.HTTPS_ENABLED:
        assert main_app._SECURITY_HEADERS["Strict-Transport-Security"] == (
            "max-age=31536000; includeSubDomains"
        )


def test_security_headers_on_static_page(client):
    res = client.get("/login")
    assert res.headers.get("x-content-type-options") == "nosniff"
    assert "content-security-policy" in res.headers


def test_security_headers_disabled_flag(client, monkeypatch):
    monkeypatch.setattr(settings, "SECURITY_HEADERS_ENABLED", False)
    res = client.get("/api/v1/auth/me")
    assert "strict-transport-security" not in res.headers


# --------------------------------------------------------------------------- #
# Auth audit console (admin) + lockout reset
# --------------------------------------------------------------------------- #
def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_admin_lists_auth_audit_with_filters(client):
    admin = _register(client, "root")  # first user bootstraps as ADMIN
    admin_token = admin.json()["access_token"]
    _register(client, "dr.badr")
    for _ in range(settings.MAX_FAILED_ATTEMPTS):
        _login(client, "dr.badr", password="wrongpass")

    res = client.get("/api/v1/users/auth-audit", headers=_bearer(admin_token))
    assert res.status_code == 200
    actions = {e["action"] for e in res.json()}
    assert {"login_success", "login_failed", "account_locked"} <= actions
    assert any(e["username"] == "dr.badr" for e in res.json())

    # Filter by action.
    res = client.get("/api/v1/users/auth-audit?action=account_locked",
                     headers=_bearer(admin_token))
    rows = res.json()
    assert rows and all(e["action"] == "account_locked" for e in rows)
    assert rows[0]["username"] == "dr.badr"

    # Filter by user.
    badr_id = rows[0]["user_id"]
    res = client.get(f"/api/v1/users/auth-audit?user_id={badr_id}",
                     headers=_bearer(admin_token))
    assert res.json() and all(e["user_id"] == badr_id for e in res.json())

    # Events carry IP + details so the admin can investigate.
    first = client.get("/api/v1/users/auth-audit", headers=_bearer(admin_token)).json()[0]
    assert "ip_address" in first and "timestamp" in first


def test_auth_audit_and_locked_require_admin(client):
    _register(client, "root")
    non_admin = _register(client, "dr.cat")
    token = non_admin.json()["access_token"]
    assert client.get("/api/v1/users/auth-audit", headers=_bearer(token)).status_code == 403
    assert client.get("/api/v1/users/locked", headers=_bearer(token)).status_code == 403


def test_locked_list_and_per_account_reset(client):
    admin = _register(client, "root")
    admin_token = admin.json()["access_token"]
    _register(client, "dr.badr")
    for _ in range(settings.MAX_FAILED_ATTEMPTS):
        _login(client, "dr.badr", password="wrongpass")
    assert _login(client, "dr.badr").status_code == 423  # confirmed locked

    rows = client.get("/api/v1/users/locked", headers=_bearer(admin_token)).json()
    assert [r["username"] for r in rows] == ["dr.badr"]
    assert rows[0]["failed_attempts"] == settings.MAX_FAILED_ATTEMPTS
    assert rows[0]["remaining_minutes"] >= 1

    badr_id = rows[0]["id"]
    res = client.post(f"/api/v1/users/{badr_id}/reset-lockout",
                      headers=_bearer(admin_token))
    assert res.status_code == 200
    assert client.get("/api/v1/users/locked", headers=_bearer(admin_token)).json() == []

    # The account can log in again — and the unlock is itself audited.
    assert _login(client, "dr.badr").status_code == 200
    assert any(e["action"] == "lockout_reset" for e in _audit_rows())


def test_reset_lockout_unknown_user_404(client):
    admin = _register(client, "root")
    admin_token = admin.json()["access_token"]
    res = client.post("/api/v1/users/999999/reset-lockout", headers=_bearer(admin_token))
    assert res.status_code == 404
