"""PaxsViewer Control Panel web integration (served at /admin).

The control-panel renderer (from ``D:\\Github\\control-panel-electron``) is a
pure Electron UI: it talks exclusively through ``window.electronApi``. PAXS
serves the built bundle at ``/admin`` with a web bridge
(``app/static/admin/admin-bridge.js``) that backs that API with the
``/api/v1/admin-panel`` REST endpoints. These tests verify the mount, the
bridge asset, and every endpoint the bridge calls.
"""

from __future__ import annotations

import pytest

from app.core.config import settings

ADMIN_TITLE = "PaxsViewer Control Panel"


@pytest.fixture(autouse=True)
def _admin_config(tmp_path, monkeypatch):
    """Point the panel's persisted config at a temp file (never the repo)."""
    monkeypatch.setattr(settings, "ADMIN_PANEL_CONFIG_FILE", str(tmp_path / "admin_panel.json"))
    monkeypatch.setenv("PAXS_DISABLE_BROWSER", "1")  # never open real browser tabs
    yield


def _auth(client, username, password="secret123", role=None):
    """Register the first user (bootstraps as ADMIN) and return the token."""
    body = {"username": username, "password": password}
    if role:
        body["role"] = role
    reg = client.post("/api/v1/auth/register", json=body)
    assert reg.status_code == 201, reg.text
    return reg.json().get("access_token")


# --------------------------------------------------------------------------- #
# Static mount (/admin)
# --------------------------------------------------------------------------- #
def test_admin_mount_serves_control_panel(client):
    res = client.get("/admin/")
    assert res.status_code == 200
    assert ADMIN_TITLE in res.text
    assert 'src="./admin-bridge.js"' in res.text


def test_admin_bridge_script_served(client):
    """The bridge must be served as real JavaScript — a missing file would
    fall through to the SPA fallback (index.html) and break the panel."""
    res = client.get("/admin/admin-bridge.js")
    assert res.status_code == 200
    assert "javascript" in res.headers.get("content-type", "")
    assert "window.electronApi" in res.text
    assert "/api/v1/admin-panel" in res.text
    assert not res.text.lstrip().startswith("<!doctype")


def test_admin_version_marker(client):
    res = client.get("/admin/admin-version.txt")
    assert res.status_code == 200
    assert res.text.strip()


def test_admin_spa_fallback_serves_index(client):
    """Unknown deep paths under /admin fall back to index.html (SPA)."""
    res = client.get("/admin/some/client/route")
    assert res.status_code == 200
    assert ADMIN_TITLE in res.text


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #
def test_admin_status_requires_auth(client):
    res = client.get("/api/v1/admin-panel/status")
    assert res.status_code in (401, 403)


def test_admin_config_requires_auth(client):
    res = client.get("/api/v1/admin-panel/config")
    assert res.status_code in (401, 403)


def test_admin_actions_require_auth(client):
    res = client.post("/api/v1/admin-panel/actions/start-all")
    assert res.status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Status / logs / config
# --------------------------------------------------------------------------- #
def test_admin_status_snapshot_shape(client):
    token = _auth(client, "admin_panel_admin")
    res = client.get("/api/v1/admin-panel/status",
                     headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    body = res.json()
    for key in ("orthanc", "proxy", "api", "mysql", "adminFront"):
        assert key in body
        assert body[key] in ("unknown", "running", "stopped", "error",
                             "starting", "stopping")
    assert "lastChecked" in body


def test_admin_logs_available(client):
    token = _auth(client, "admin_panel_logs")
    res = client.get("/api/v1/admin-panel/logs",
                     headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_admin_config_defaults(client):
    token = _auth(client, "admin_panel_cfg")
    res = client.get("/api/v1/admin-panel/config",
                     headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    cfg = res.json()
    assert cfg["orthancPort"] == 8042
    assert cfg["proxyPort"] == 3000
    assert cfg["adminFrontPort"] == 5173
    assert cfg["mysqlServiceName"]


def test_admin_config_save_round_trip(client):
    token = _auth(client, "admin_panel_cfg2")
    headers = {"Authorization": f"Bearer {token}"}
    res = client.post("/api/v1/admin-panel/config",
                      json={"adminFrontPort": 9999, "monitorIntervalMs": 3000},
                      headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["config"]["adminFrontPort"] == 9999
    assert body["config"]["monitorIntervalMs"] == 3000
    # other keys untouched
    assert body["config"]["orthancPort"] == 8042

    # persisted (GET returns the same values)
    res = client.get("/api/v1/admin-panel/config", headers=headers)
    assert res.json()["adminFrontPort"] == 9999


def test_admin_config_clamps_ports(client):
    token = _auth(client, "admin_panel_cfg3")
    res = client.post("/api/v1/admin-panel/config",
                      json={"orthancPort": 999999, "adminFrontPort": -5},
                      headers={"Authorization": f"Bearer {token}"})
    body = res.json()
    assert body["ok"] is True
    assert body["config"]["orthancPort"] == 65535
    assert body["config"]["adminFrontPort"] == 1


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def test_admin_action_open_viewer(client):
    token = _auth(client, "admin_panel_open")
    res = client.post("/api/v1/admin-panel/actions/open-viewer",
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "viewer" in body["url"]


def test_admin_action_unknown(client):
    token = _auth(client, "admin_panel_unk")
    res = client.post("/api/v1/admin-panel/actions/bogus",
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["ok"] is False


def test_admin_action_restart_unknown_service(client):
    token = _auth(client, "admin_panel_rst")
    res = client.post("/api/v1/admin-panel/actions/restart",
                      json={"service": "nonsense"},
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["ok"] is False


def test_admin_action_requires_admin_role(client):
    """A non-admin user can read status but not drive actions."""
    first = _auth(client, "admin_panel_boss", role="ADMIN")
    # second user with a non-admin role
    _auth(client, "admin_panel_doc", password="secret123", role="PHYSICIAN")
    login = client.post("/api/v1/auth/login",
                        json={"username": "admin_panel_doc", "password": "secret123"})
    doc_token = login.json()["access_token"]
    res = client.post("/api/v1/admin-panel/actions/start-all",
                      headers={"Authorization": f"Bearer {doc_token}"})
    assert res.status_code in (401, 403)
    # ... but the admin can
    res = client.post("/api/v1/admin-panel/actions/stop-all",
                      headers={"Authorization": f"Bearer {first}"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
