"""Settings → Redis tab API (/api/v1/redis).

Covers:
  - GET /api/v1/redis/status (open for health checks, flags admin role when authenticated)
  - POST /api/v1/redis/test (probes Redis without saving)
  - POST /api/v1/redis/configure (persists configuration)
  - GET /api/v1/redis/keys & /key & DELETE /key
  - POST /api/v1/redis/ping
  - POST /api/v1/redis/server/start & /stop & /restart & /install
"""

import tempfile
from pathlib import Path

import pytest
from app.core.config import settings as runtime_settings
from app.core.security import create_access_token, hash_password
from app.db.models import User
from app.db.session import SyncSessionLocal


def _ensure_admin():
    with SyncSessionLocal() as session:
        user = session.query(User).filter_by(username="admin_redis_user").first()
        if not user:
            user = User(
                username="admin_redis_user",
                password_hash=hash_password("secret123"),
                full_name="Redis Admin",
                role="ADMIN",
                is_active=True,
            )
            session.add(user)
            session.commit()
    token = create_access_token("admin_redis_user", "ADMIN", runtime_settings.SECRET_KEY, 60)
    return {"Authorization": f"Bearer {token}"}


def _ensure_non_admin():
    with SyncSessionLocal() as session:
        user = session.query(User).filter_by(username="rad_redis_user").first()
        if not user:
            user = User(
                username="rad_redis_user",
                password_hash=hash_password("secret123"),
                full_name="Radiologist",
                role="RADIOLOGIST",
                is_active=True,
            )
            session.add(user)
            session.commit()
    token = create_access_token("rad_redis_user", "RADIOLOGIST", runtime_settings.SECRET_KEY, 60)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _isolate_redis_env():
    """Redirect .env writes to temp file."""
    tmp = Path(tempfile.mkdtemp(prefix="paxs_redis_test_"))
    snap_env = getattr(runtime_settings, "ENV_FILE_PATH", ".env")
    runtime_settings.ENV_FILE_PATH = str(tmp / "test.env")
    yield tmp
    runtime_settings.ENV_FILE_PATH = snap_env


def test_redis_status_requires_authentication(anonymous_client):
    """C03: the cache panel is no longer readable without a login.

    It exposes cache contents and server internals, so it now requires an
    authenticated caller (previously it answered anonymously with admin=False).
    """
    anonymous_client.cookies.clear()
    res = anonymous_client.get("/api/v1/redis/status")
    assert res.status_code == 401

    data = anonymous_client.get("/api/v1/redis/status", headers=_ensure_admin()).json()
    assert "ok" in data
    assert "connected" in data
    assert "enabled" in data
    assert "tier1_ram" in data
    assert "server_install" in data
    assert data["admin"] is True


def test_redis_status_authenticated_admin(client):
    """Redis status endpoint should report admin=True when admin token is provided."""
    headers = _ensure_admin()
    res = client.get("/api/v1/redis/status", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["admin"] is True


def test_redis_server_start_requires_admin(anonymous_client):
    """Server control endpoints should reject unauthenticated or non-admin callers."""
    anonymous_client.cookies.clear()
    # Unauthenticated
    res = anonymous_client.post("/api/v1/redis/server/start")
    assert res.status_code in (401, 403)

    # Non-admin user
    headers = _ensure_non_admin()
    res = anonymous_client.post("/api/v1/redis/server/start", headers=headers)
    assert res.status_code == 403


def test_redis_server_start_with_admin(client):
    """Admin caller can invoke start_server and receive status/probe result."""
    headers = _ensure_admin()
    res = client.post("/api/v1/redis/server/start", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "ok" in data


def test_redis_ping(client):
    """Ping endpoint returns latency and ping status."""
    res = client.post("/api/v1/redis/ping", headers=_ensure_admin())
    assert res.status_code == 200
    data = res.json()
    assert "latency_ms" in data


def test_redis_keys_and_detail(client):
    """Search and inspect keys."""
    res = client.get("/api/v1/redis/keys?pattern=*", headers=_ensure_admin())
    assert res.status_code == 200
    data = res.json()
    assert "keys" in data
    assert "count" in data


def test_redis_configure(client):
    """Admin can configure Redis connection."""
    headers = _ensure_admin()
    res = client.post("/api/v1/redis/configure", headers=headers, json={
        "host": "127.0.0.1",
        "port": 6379,
        "db": 0,
        "enabled": True,
        "ssl": False,
        "persist_env": True,
    })
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "url_masked" in data


def test_redis_volume_caching_and_inspection(auth_client, client):
    """Opening 3D volume stream or MPR populates Redis under paxs:vol:* which is discoverable in the Key Browser."""
    from tests.test_volume import _ingest_series

    study, series, _ = _ingest_series(auth_client, n_slices=4)

    # 1. Fetch 3D volume stream as the WebGL DICOM viewer does
    v_resp = client.get(f"/api/v1/volume/{series}/data", headers=_ensure_admin())
    assert v_resp.status_code == 200

    # 2. Inspect Redis keys matching paxs:vol:*
    keys_resp = client.get("/api/v1/redis/keys?pattern=paxs:vol:*", headers=_ensure_admin())
    assert keys_resp.status_code == 200
    k_data = keys_resp.json()
    assert k_data["ok"] is True
    vol_keys = [k["key"] for k in k_data["keys"]]
    assert any(k.startswith("paxs:vol:") for k in vol_keys)

    # 3. Inspect the specific volume key details
    target_key = next(k for k in vol_keys if k.startswith("paxs:vol:"))
    detail_resp = client.get(f"/api/v1/redis/key?key={target_key}", headers=_ensure_admin())
    assert detail_resp.status_code == 200
    d_data = detail_resp.json()
    assert d_data["ok"] is True
    assert d_data["size_bytes"] > 0
    assert d_data["tier"] in ("Redis (L2)", "RAM (L1)")


def test_redis_frame_caching_and_inspection(auth_client, client):
    """Fetching DICOM frames caches them into Redis under paxs:frame:*."""
    from tests.test_volume import _ingest_series

    study, series, sops = _ingest_series(auth_client, n_slices=2)

    # Fetch frame 1 of instance
    f_resp = auth_client.get(
        f"/dicom-web/studies/{study}/series/{series}/instances/{sops[0]}/frames/1",
        headers={"Accept": 'multipart/related; type="application/octet-stream"'},
    )
    assert f_resp.status_code == 200

    # Inspect Redis keys matching paxs:frame:*
    keys_resp = client.get("/api/v1/redis/keys?pattern=paxs:frame:*", headers=_ensure_admin())
    assert keys_resp.status_code == 200
    k_data = keys_resp.json()
    assert k_data["ok"] is True
    frame_keys = [k["key"] for k in k_data["keys"]]
    assert any(k.startswith("paxs:frame:") for k in frame_keys)

    # Inspect the specific frame key details
    target_key = next(k for k in frame_keys if k.startswith("paxs:frame:"))
    detail_resp = client.get(f"/api/v1/redis/key?key={target_key}", headers=_ensure_admin())
    assert detail_resp.status_code == 200
    d_data = detail_resp.json()
    assert d_data["ok"] is True
    assert d_data["size_bytes"] > 0


def test_redis_warm_caching_and_inspection(auth_client, client):
    """Opening/warming a study populates Redis under paxs:warm:* with summary metadata."""
    from app.services.warm_service import _warm_study
    from tests.test_volume import _ingest_series

    study, _, _ = _ingest_series(auth_client, n_slices=2)
    _warm_study(study)

    # Inspect Redis keys matching paxs:warm:*
    keys_resp = client.get("/api/v1/redis/keys?pattern=paxs:warm:*", headers=_ensure_admin())
    assert keys_resp.status_code == 200
    k_data = keys_resp.json()
    assert k_data["ok"] is True
    warm_keys = [k["key"] for k in k_data["keys"]]
    assert any(k.startswith("paxs:warm:") for k in warm_keys)

    # Inspect the specific warm record key details
    target_key = next(k for k in warm_keys if k.startswith("paxs:warm:"))
    detail_resp = client.get(f"/api/v1/redis/key?key={target_key}", headers=_ensure_admin())
    assert detail_resp.status_code == 200
    d_data = detail_resp.json()
    assert d_data["ok"] is True
    assert "WARMED" in d_data.get("preview", "") or d_data.get("size_bytes", 0) > 0
