"""Unified login page + cookie-gated /viewer (auth plan §2).

The gate middleware (ViewerAuthGateMiddleware) redirects anonymous /viewer
and /studies-ui requests to /login?next=…, lets patient share links
(/viewer?share=…) through, and accepts the HttpOnly paxs_session cookie set
by POST /api/v1/auth/login. These tests enable the gate explicitly (it is
off for the rest of the suite via AUTH_GATE_ENABLED=false in conftest).
"""

from __future__ import annotations

import pytest

from app.core.config import settings


def _register(client, username="dr.ali", password="secret123", role=None, full_name="Ali Hassan"):
    body = {"username": username, "password": password, "full_name": full_name}
    if role:
        body["role"] = role
    return client.post("/api/v1/auth/register", json=body)
def _login(client, username="dr.ali", password="secret123", next_url=None):
    url = "/api/v1/auth/login"
    if next_url:
        url += f"?next={next_url}"
    return client.post(url, json={"username": username, "password": password})


@pytest.fixture()
def gate(client, monkeypatch):
    """Flip the viewer auth gate on for one test."""
    monkeypatch.setattr(settings, "AUTH_GATE_ENABLED", True)
    return client


# --------------------------------------------------------------------------- #
# The gate middleware
# --------------------------------------------------------------------------- #
def test_gate_redirects_anonymous_viewer_to_login(gate):
    res = gate.get("/viewer", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/login?next=")
    assert "%2Fviewer" in res.headers["location"]


def test_gate_preserves_deep_link_in_next(gate):
    res = gate.get("/viewer?StudyInstanceUIDs=1.2.3.4", follow_redirects=False)
    assert res.status_code == 303
    loc = res.headers["location"]
    assert loc.startswith("/login?next=")
    assert "StudyInstanceUIDs" in loc


def test_gate_covers_studies_ui(gate):
    res = gate.get("/studies-ui", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/login?next=")


def test_gate_covers_dashboard_root(gate):
    res = gate.get("/", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/login?next=")
    assert "%2F" in res.headers["location"]  # next=/ preserved


def test_gate_covers_settings_dashboard(gate):
    res = gate.get("/settings", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/login?next=")
    assert "%2Fsettings" in res.headers["location"]


def test_gate_accepts_valid_cookie_on_dashboard(gate):
    _register(gate, username="admin_dash", password="secret123")
    _login(gate, username="admin_dash", password="secret123")
    assert gate.cookies.get(settings.AUTH_COOKIE_NAME)
    res = gate.get("/", follow_redirects=False)
    assert res.status_code == 200


def test_gate_accepts_valid_cookie_on_settings(gate):
    _register(gate, username="admin_set", password="secret123")
    _login(gate, username="admin_set", password="secret123")
    res = gate.get("/settings", follow_redirects=False)
    assert res.status_code == 200


def test_gate_lets_share_links_through(gate):
    # Patient share links are delivered externally — no login required.
    res = gate.get("/viewer?share=abc123")
    assert res.status_code == 200


def test_gate_accepts_valid_cookie(gate):
    _register(gate, username="dr.b", password="secret123")
    login = _login(gate, username="dr.b", password="secret123")
    assert login.status_code == 200
    cookie = gate.cookies.get(settings.AUTH_COOKIE_NAME)
    assert cookie  # the test client's jar stored the HttpOnly cookie

    res = gate.get("/viewer")
    assert res.status_code == 200


def test_gate_rejects_tampered_cookie(gate):
    gate.cookies.set(settings.AUTH_COOKIE_NAME, "not-a-jwt")
    res = gate.get("/viewer", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/login?next=")


def test_gate_off_serves_viewer_anonymously(client):
    # Gate disabled (the suite default) — anonymous access works as before.
    assert client.get("/viewer").status_code in (200, 303)


# --------------------------------------------------------------------------- #
# Login/register set the cookie + role landing
# --------------------------------------------------------------------------- #
def test_login_sets_http_only_cookie(client):
    _register(client, username="dr.c", password="secret123")
    res = _login(client, username="dr.c", password="secret123")
    assert res.status_code == 200
    set_cookie = res.headers.get("set-cookie", "")
    assert settings.AUTH_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert res.json()["access_token"]


def test_register_sets_http_only_cookie(client):
    res = _register(client, username="dr.d", password="secret123")
    assert res.status_code == 201
    assert settings.AUTH_COOKIE_NAME in res.headers.get("set-cookie", "")


def test_role_landing_admin_is_dashboard(client):
    _register(client, username="admin1", password="secret123")  # first user → ADMIN
    res = _login(client, username="admin1", password="secret123")
    assert res.json()["redirect_to"] == "/"


def test_role_landing_radiologist_is_study_list(client):
    from tests.helpers import create_user

    admin = _register(client, username="admin2", password="secret123")
    create_user(
        client,
        {"Authorization": f"Bearer {admin.json()['access_token']}"},
        "dr.e",
        "RADIOLOGIST",
    )
    res = _login(client, username="dr.e", password="secret123")
    assert res.json()["redirect_to"] == "/viewer"


def test_next_param_overrides_role_landing(client):
    _register(client, username="dr.f", password="secret123", role="RADIOLOGIST")
    res = _login(client, username="dr.f", password="secret123", next_url="/viewer?StudyInstanceUIDs=1.2.3")
    assert res.json()["redirect_to"] == "/viewer?StudyInstanceUIDs=1.2.3"


def test_next_param_blocks_open_redirect(client):
    from tests.helpers import create_user

    admin = _register(client, username="admin_x", password="secret123")  # first user → ADMIN
    create_user(
        client,
        {"Authorization": f"Bearer {admin.json()['access_token']}"},
        "dr.g",
        "RADIOLOGIST",
    )
    res = _login(client, username="dr.g", password="secret123", next_url="https://evil.example/phish")
    assert res.json()["redirect_to"] == "/viewer"  # falls back to role landing


def test_logout_clears_cookie(client):
    _register(client, username="dr.h", password="secret123")
    _login(client, username="dr.h", password="secret123")
    assert client.cookies.get(settings.AUTH_COOKIE_NAME)
    res = client.post("/api/v1/auth/logout")
    assert res.status_code == 200
    assert client.cookies.get(settings.AUTH_COOKIE_NAME) is None


# --------------------------------------------------------------------------- #
# The /login page itself
# --------------------------------------------------------------------------- #
def test_login_page_served(client):
    res = client.get("/login")
    assert res.status_code == 200
    assert "PAXS Archive" in res.text
    assert "auth/login" in res.text or "Sign in" in res.text


def test_login_page_redirects_authenticated_user(client):
    _register(client, username="admin3", password="secret123")
    _login(client, username="admin3", password="secret123")
    assert client.cookies.get(settings.AUTH_COOKIE_NAME)
    res = client.get("/login", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/"  # ADMIN role landing


def test_login_page_redirects_with_next(client):
    _register(client, username="admin4", password="secret123")
    _register(client, username="dr.i", password="secret123", role="RADIOLOGIST")
    _login(client, username="dr.i", password="secret123")
    res = client.get("/login", params={"next": "/viewer"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/viewer"


def test_logout_page_clears_cookie_and_redirects(client):
    _register(client, username="dr.j", password="secret123")
    _login(client, username="dr.j", password="secret123")
    res = client.get("/logout", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login"
    assert client.cookies.get(settings.AUTH_COOKIE_NAME) is None
