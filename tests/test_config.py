"""Admin Settings API (/api/v1/config) — editable server configuration.

Covers the auth gate (admin-only writes), the GET registry shape, value
validation/coercion, persistence across requests, and the live application of
changes to the in-process settings object.
"""

import pytest

from app.core.config import settings as runtime_settings
from app.services.settings_service import EDITABLE_KEYS

GROUP_KEYS = {"sys", "dicom", "interop", "storage", "security", "workflow", "viewer"}


def _auth(client, username="cfg_admin", password="secret123", role="ADMIN"):
    # H02: self-registration cannot mint privileged roles, so accounts are
    # created through the admin API.
    from tests.helpers import auth_for_role

    return auth_for_role(client, username, role=role or "ADMIN", password=password)


@pytest.fixture(autouse=True)
def _restore_runtime_settings():
    """Restore the global settings object + server_settings rows after each test.

    PATCH mutates the process-wide pydantic settings object, and the lifespan
    replays stored overrides at the next TestClient start — so both the runtime
    object AND the rows must be reset or overrides leak into later test files.
    """
    from app.db.models import ServerSetting
    from app.db.session import sync_engine

    snap = {k: getattr(runtime_settings, k) for k in EDITABLE_KEYS if hasattr(runtime_settings, k)}
    yield
    for k, v in snap.items():
        try:
            setattr(runtime_settings, k, v)
        except Exception:
            pass
    with sync_engine.begin() as conn:
        conn.execute(ServerSetting.__table__.delete())


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #
def test_config_requires_auth(client):
    assert client.get("/api/v1/config").status_code == 401
    assert client.patch("/api/v1/config", json={"updates": {"APP_NAME": "X"}}).status_code == 401


def test_config_patch_requires_admin(client):
    h = _auth(client, username="dr_view", role="RADIOLOGIST")
    assert client.patch("/api/v1/config",
                        json={"updates": {"APP_NAME": "X"}}, headers=h).status_code == 403


def test_config_get_allows_any_authenticated_user(client):
    h = _auth(client, username="dr_view2", role="RADIOLOGIST")
    res = client.get("/api/v1/config", headers=h)
    assert res.status_code == 200
    assert res.json()["admin"] is False


# --------------------------------------------------------------------------- #
# GET shape
# --------------------------------------------------------------------------- #
def test_config_get_shape(client):
    h = _auth(client)
    res = client.get("/api/v1/config", headers=h)
    assert res.status_code == 200
    body = res.json()
    assert body["admin"] is True

    groups = {g["key"]: g for g in body["groups"]}
    assert set(groups) == GROUP_KEYS

    sys_fields = {f["key"]: f for f in groups["sys"]["fields"]}
    assert sys_fields["APP_NAME"]["type"] == "str"
    assert sys_fields["PUBLIC_BASE_URL"]["value"] == "http://paxs.test"  # env default
    assert sys_fields["PUBLIC_BASE_URL"]["changed"] is False
    assert "value" in sys_fields["SHARE_EXPIRY_DAYS"]

    dicom_fields = {f["key"]: f for f in groups["dicom"]["fields"]}
    assert dicom_fields["DICOM_PORT"]["restart"] is True

    # readonly live payload rides along (same shape as /health)
    assert body["readonly"]["app"]
    assert "dicom" in body["readonly"]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_config_patch_unknown_key(client):
    h = _auth(client)
    res = client.patch("/api/v1/config", json={"updates": {"NOT_A_SETTING": 1}}, headers=h)
    assert res.status_code == 422


def test_config_patch_wrong_type(client):
    h = _auth(client)
    res = client.patch("/api/v1/config", json={"updates": {"DICOM_PORT": "not-a-port"}}, headers=h)
    assert res.status_code == 422


def test_config_patch_select_invalid_option(client):
    h = _auth(client)
    res = client.patch("/api/v1/config", json={"updates": {"STORAGE_BACKEND": "sftp"}}, headers=h)
    assert res.status_code == 422


def test_config_patch_empty_batch(client):
    h = _auth(client)
    assert client.patch("/api/v1/config", json={"updates": {}}, headers=h).status_code == 422


# --------------------------------------------------------------------------- #
# Apply + persist
# --------------------------------------------------------------------------- #
def test_config_patch_applies_and_persists(client):
    h = _auth(client)
    res = client.patch("/api/v1/config",
                       json={"updates": {"APP_NAME": "PAXS Radiology Hub",
                                         "DICOM_PORT": 11112}}, headers=h)
    assert res.status_code == 200
    assert res.json()["ok"] is True
    applied = {a["key"]: a for a in res.json()["applied"]}
    assert applied["APP_NAME"]["value"] == "PAXS Radiology Hub"
    assert applied["APP_NAME"]["changed"] is True
    assert applied["DICOM_PORT"]["value"] == 11112

    # Runtime object mutated immediately
    assert runtime_settings.APP_NAME == "PAXS Radiology Hub"
    assert runtime_settings.DICOM_PORT == 11112

    # Health reflects the live change
    health = client.get("/api/v1/health", headers=h).json()
    assert health["app"] == "PAXS Radiology Hub"

    # Persisted: a fresh GET (new request) still reports the overrides
    again = client.get("/api/v1/config", headers=h).json()
    sys_fields = {f["key"]: f for f in [g for g in again["groups"] if g["key"] == "sys"][0]["fields"]}
    assert sys_fields["APP_NAME"]["value"] == "PAXS Radiology Hub"
    assert sys_fields["APP_NAME"]["changed"] is True


def test_config_patch_bool_roundtrip(client):
    h = _auth(client)
    res = client.patch("/api/v1/config",
                       json={"updates": {"DICOM_RECEIVER_ENABLED": False}}, headers=h)
    assert res.status_code == 200
    assert res.json()["applied"][0]["value"] is False
    assert runtime_settings.DICOM_RECEIVER_ENABLED is False

    again = client.get("/api/v1/config", headers=h).json()
    dicom = {f["key"]: f for f in [g for g in again["groups"] if g["key"] == "dicom"][0]["fields"]}
    assert dicom["DICOM_RECEIVER_ENABLED"]["value"] is False


def test_config_patch_partial_errors(client):
    h = _auth(client)
    res = client.patch("/api/v1/config",
                       json={"updates": {"APP_NAME": "OK", "BOGUS_KEY": 1}}, headers=h)
    assert res.status_code == 200
    body = res.json()
    assert body["partial"] is True
    assert [a["key"] for a in body["applied"]] == ["APP_NAME"]
    assert body["errors"][0]["key"] == "BOGUS_KEY"


def test_config_patch_restart_flag_survives(client):
    h = _auth(client)
    client.patch("/api/v1/config", json={"updates": {"DICOM_AE_TITLE": "CT_SCANNER"}}, headers=h)
    assert runtime_settings.DICOM_AE_TITLE == "CT_SCANNER"


def test_config_get_tolerates_stray_stored_keys(client):
    """Regression: the DB panel stores its own knobs in server_settings
    (e.g. dbp_slow_threshold_ms) that aren't in the editable registry.
    overrides_map() used to KeyError on the first one, 500ing the whole
    Settings page — it must skip non-registry keys like apply_db_overrides.
    """
    from app.db.models import ServerSetting
    from app.db.session import sync_engine

    with sync_engine.begin() as conn:
        conn.execute(ServerSetting.__table__.delete())
        conn.execute(ServerSetting.__table__.insert().values(
            key="dbp_slow_threshold_ms", value="1"))
        conn.execute(ServerSetting.__table__.insert().values(
            key="dbp_slow_query", value="0"))

    h = _auth(client)
    res = client.get("/api/v1/config", headers=h)
    assert res.status_code == 200
    body = res.json()
    # Every registry key is still present and untouched by the stray rows.
    keys = {f["key"] for g in body["groups"] for f in g["fields"]}
    assert keys == set(EDITABLE_KEYS)
    assert all(f["changed"] is False
               for g in body["groups"] for f in g["fields"])
