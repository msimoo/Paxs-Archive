"""Session layer (auth plan §2.2/§3.2/§4.3).

Covers: session rows created at login, short access tokens, refresh-token
rotation, whole-family revocation when a rotated token is replayed, expired
sessions, and logout revoking the session row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.security import decode_token, hash_refresh_token
from app.db.models import Session
from app.db.session import sync_engine


def _register(client, username="dr.ali", password="secret123", role=None):
    body = {"username": username, "password": password, "full_name": "Ali Hassan"}
    if role:
        body["role"] = role
    return client.post("/api/v1/auth/register", json=body)
def _login(client, username="dr.ali", password="secret123"):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def _sessions() -> list[dict]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            Session.__table__.select().order_by(Session.__table__.c.created_at)
        ).fetchall()
        return [dict(r._mapping) for r in rows]


def _session_for_cookie(client) -> dict | None:
    cookie = client.cookies.get(settings.AUTH_COOKIE_NAME)
    if not cookie:
        return None
    h = hash_refresh_token(cookie)
    for s in _sessions():
        if s["refresh_token_hash"] == h:
            return s
    return None


def test_login_creates_session_row(client):
    _register(client)
    client.cookies.clear()  # register issued its own token; test login's row
    res = _login(client)
    assert res.status_code == 200
    s = _session_for_cookie(client)
    assert s is not None
    assert s["user_id"] and s["family_id"]
    assert s["is_revoked"] == 0
    assert s["expires_at"] > datetime.now(timezone.utc).replace(tzinfo=None)
    # Only the SHA-256 hash is stored — never the token itself.
    cookie = client.cookies.get(settings.AUTH_COOKIE_NAME)
    assert cookie
    assert cookie not in s["refresh_token_hash"]
    assert hash_refresh_token(cookie) == s["refresh_token_hash"]


def test_access_token_is_short_lived(client):
    _register(client)
    token = _login(client).json()["access_token"]
    payload = decode_token(token, settings.SECRET_KEY)
    assert payload is not None
    lifetime = payload["exp"] - payload["iat"]
    assert lifetime == settings.TOKEN_EXPIRE_MINUTES * 60
    assert lifetime <= 60 * 60  # ~15 min, not hours


def test_refresh_rotates_token_and_issues_new_access(client):
    _register(client)
    client.cookies.clear()
    login = _login(client)
    old_cookie = client.cookies.get(settings.AUTH_COOKIE_NAME)
    family_before = _session_for_cookie(client)["family_id"]
    assert login.json()["access_token"]

    res = client.post("/api/v1/auth/refresh")
    assert res.status_code == 200
    assert res.json()["access_token"]
    new_cookie = client.cookies.get(settings.AUTH_COOKIE_NAME)
    assert new_cookie and new_cookie != old_cookie
    # Session row updated with the new hash, same family.
    s = _session_for_cookie(client)
    assert s is not None
    assert s["family_id"] == family_before
    assert hash_refresh_token(new_cookie) == s["refresh_token_hash"]


def test_replay_of_rotated_token_revokes_family(client):
    _register(client)
    client.cookies.clear()
    _login(client)
    rotated_out = client.cookies.get(settings.AUTH_COOKIE_NAME)

    client.post("/api/v1/auth/refresh")  # rotates; cookie now holds new token
    current = client.cookies.get(settings.AUTH_COOKIE_NAME)
    assert current != rotated_out

    # Replay the rotated-out token — theft signal: whole family revoked.
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, rotated_out)
    res = client.post("/api/v1/auth/refresh")
    assert res.status_code == 401
    # The single session row (same family) is now revoked with token_reuse.
    rows = [s for s in _sessions() if s["revoke_reason"] == "token_reuse"]
    assert len(rows) == 1 and rows[0]["is_revoked"] == 1

    # Even the current token is dead now (family revoked).
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, current)
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_refresh_with_unknown_token_401(client):
    _register(client)
    _login(client)
    client.cookies.set(settings.AUTH_COOKIE_NAME, "deadbeef." + "x" * 40)
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_refresh_without_cookie_401(client):
    _register(client)
    client.cookies.clear()
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_expired_session_rejected(client):
    _register(client)
    client.cookies.clear()
    _login(client)
    with sync_engine.begin() as conn:
        conn.execute(
            Session.__table__.update().values(
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(seconds=1)
            )
        )
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_logout_revokes_session_row(client):
    _register(client)
    client.cookies.clear()
    _login(client)
    assert _session_for_cookie(client)["is_revoked"] == 0

    res = client.post("/api/v1/auth/logout")
    assert res.status_code == 200
    assert client.cookies.get(settings.AUTH_COOKIE_NAME) is None
    assert _session_for_cookie(client) is None  # cookie gone → no row match
    # The row behind the cookie is revoked.
    rows = [s for s in _sessions() if s["revoke_reason"] == "logout"]
    assert len(rows) == 1 and rows[0]["is_revoked"] == 1
    # Revoked session can no longer refresh.
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_login_page_and_gate_accept_valid_refresh_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_GATE_ENABLED", True)
    _register(client)
    _login(client)
    assert client.cookies.get(settings.AUTH_COOKIE_NAME)

    # Gate: entry page with a valid session → served.
    assert client.get("/viewer").status_code == 200
    # Login page: already-authenticated visitor → bounced to role landing.
    res = client.get("/login", follow_redirects=False)
    assert res.status_code == 303


def test_gate_rejects_revoked_session(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_GATE_ENABLED", True)
    _register(client)
    _login(client)
    with sync_engine.begin() as conn:
        conn.execute(
            Session.__table__.update().values(is_revoked=1, revoke_reason="test")
        )
    res = client.get("/viewer", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/login?next=")


def test_multi_device_logins_have_separate_families(client):
    _register(client)
    client.cookies.clear()
    _login(client)
    fam1 = _session_for_cookie(client)["family_id"]
    client.cookies.clear()
    _login(client)  # second device / browser
    fam2 = _session_for_cookie(client)["family_id"]
    assert fam1 != fam2


def test_me_resolves_from_session_cookie(client):
    """The study-list SPA holds only the HttpOnly cookie — /me must work."""
    from tests.helpers import create_user

    admin = _register(client, username="admin.rad")  # first user bootstraps as ADMIN
    create_user(
        client, {"Authorization": f"Bearer {admin.json()['access_token']}"},
        "dr.rad", "RADIOLOGIST",
    )
    client.cookies.clear()
    _login(client, username="dr.rad")
    assert client.cookies.get(settings.AUTH_COOKIE_NAME)

    res = client.get("/api/v1/auth/me")
    assert res.status_code == 200
    body = res.json()
    assert body["username"] == "dr.rad"
    assert body["role"] == "RADIOLOGIST"


def test_me_cookie_rejects_revoked_session(client):
    """After sign-out the cookie is dead — /me must 401, not re-authenticate."""
    _register(client)
    client.cookies.clear()
    _login(client)
    client.post("/api/v1/auth/logout")
    assert client.cookies.get(settings.AUTH_COOKIE_NAME) is None
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_requires_authentication(client):
    client.cookies.clear()
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_prefers_valid_bearer_over_stale_cookie(client):
    """Bearer (dashboard style) still wins even when a cookie is present."""
    _register(client, username="dr.bearer", role="ADMIN")
    client.cookies.clear()
    login = _login(client, username="dr.bearer")
    token = login.json()["access_token"]
    assert token

    res = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json()["username"] == "dr.bearer"
