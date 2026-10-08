"""Disaster Recovery replication (roadmap #49) + LB health probes (#47).

Covers replication_service (target resolution, idempotent enqueue, sweep
copying objects to a named backend target, failure handling), the admin API
(/api/v1/replication: status / records / sweep / replicate-one) with auth
gates, and the /health/live + /health/ready probes used by HAProxy.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import ReplicationRecord
from app.db.session import AsyncSessionLocal, SyncSessionLocal
from app.services import indexing_service, replication_service, storage_service
from app.services.storage import get_storage_backend, reset_storage_backend
from tests.helpers import make_instance


def _register(client, username, password="secret123"):
    return client.post("/api/v1/auth/register",
                       json={"username": username, "password": password})


def _auth(client, username, password="secret123"):
    reg = _register(client, username, password)
    if reg.status_code == 201:
        return {"Authorization": f"Bearer {reg.json()['access_token']}"}
    return {"Authorization": "Bearer " + client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]}


@pytest.fixture(autouse=True)
def admin(client):
    return _auth(client, "rep_admin")


@pytest.fixture(autouse=True)
def _dr_off(monkeypatch):
    """DR off by default; individual tests flip it on."""
    monkeypatch.setattr(settings, "DR_REPLICATION_ENABLED", False)
    monkeypatch.setattr(settings, "DR_REPLICATION_TARGET", "")
    monkeypatch.setattr(settings, "DR_REPLICATION_PREFIX", "replica/")
    reset_storage_backend()


def _ingest(study_uid: str, patient: str, sop_uid: str):
    from app.dicom.parser import parse_metadata

    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.91.1",
                       sop_uid=sop_uid, instance_num=1, patient_id=patient)
    key, sha = storage_service.save_dataset(ds)

    async def _index():
        async with AsyncSessionLocal() as s:
            async with s.begin():
                await indexing_service.index_new_instance(
                    s, parse_metadata(ds), key, sha, performed_by="test")

    asyncio.run(_index())


def _record(study_uid: str) -> ReplicationRecord:
    with SyncSessionLocal() as s:
        return s.execute(select(ReplicationRecord).where(
            ReplicationRecord.study_instance_uid == study_uid)).scalar_one()


# --------------------------------------------------------------------------- #
# Service — target resolution + enqueue
# --------------------------------------------------------------------------- #
def test_target_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    assert "unconfigured" in replication_service.target_display()
    with pytest.raises(ValueError):
        replication_service.target_backend()


def test_enqueue_idempotent_and_reset():
    with SyncSessionLocal() as s:
        r1 = replication_service.enqueue_study(s, "1.2.3.9.1.1")
        r2 = replication_service.enqueue_study(s, "1.2.3.9.1.1")
        assert r1.id == r2.id  # same row
        r1.status = "FAILED"
        s.flush()
        r3 = replication_service.enqueue_study(s, "1.2.3.9.1.1")
        assert r3.status == "PENDING"  # failed -> re-queued
        s.rollback()


# --------------------------------------------------------------------------- #
# Service — sweep replicates to a named backend target
# --------------------------------------------------------------------------- #
def test_sweep_replicates_to_named_target(client, admin, tmp_path, monkeypatch):
    target_dir = tmp_path / "dr-target"
    target_dir.mkdir()
    res = client.post("/api/v1/storage/backends", headers=admin, json={
        "name": "dr-replica", "kind": "local", "root_path": str(target_dir)})
    assert res.status_code == 201
    reset_storage_backend()

    _ingest("1.2.3.9.88.1", "P-DR", "1.2.3.5.88.1")
    monkeypatch.setattr(settings, "DR_REPLICATION_ENABLED", True)
    monkeypatch.setattr(settings, "DR_REPLICATION_TARGET", "dr-replica")

    result = replication_service.sweep()
    assert result["enabled"] is True
    assert result["replicated"] >= 1

    rec = _record("1.2.3.9.88.1")
    assert rec.status == "REPLICATED"
    assert rec.object_count >= 1
    assert rec.bytes_replicated > 0

    # the object physically exists on the target backend under the prefix
    bk = get_storage_backend("dr-replica")
    assert bk.exists("replica/1.2.3.9.88.1/1.2.3.5.88.1.dcm")
    reset_storage_backend()


def test_sweep_records_failure_for_unknown_target(client, admin, monkeypatch):
    _ingest("1.2.3.9.77.1", "P-DR2", "1.2.3.5.77.1")
    monkeypatch.setattr(settings, "DR_REPLICATION_ENABLED", True)
    monkeypatch.setattr(settings, "DR_REPLICATION_TARGET", "no-such-backend")
    result = replication_service.sweep()
    assert result["error"]  # validation failure is surfaced, not raised


def test_replicate_unknown_study_raises(client, admin, tmp_path, monkeypatch):
    target_dir = tmp_path / "dr-x"
    target_dir.mkdir()
    client.post("/api/v1/storage/backends", headers=admin, json={
        "name": "dr-x", "kind": "local", "root_path": str(target_dir)})
    reset_storage_backend()
    monkeypatch.setattr(settings, "DR_REPLICATION_ENABLED", True)
    monkeypatch.setattr(settings, "DR_REPLICATION_TARGET", "dr-x")
    with pytest.raises(KeyError):
        replication_service.replicate_study("1.2.3.9.0.0")


def test_new_instance_requeues_replicated_study(client, admin, tmp_path, monkeypatch):
    """A late-arriving instance on an already-REPLICATED study must re-trigger
    the sweep (late series are common in DICOM) — #49 regression guard."""
    target_dir = tmp_path / "dr-rq"
    target_dir.mkdir()
    client.post("/api/v1/storage/backends", headers=admin, json={
        "name": "dr-rq", "kind": "local", "root_path": str(target_dir)})
    reset_storage_backend()
    monkeypatch.setattr(settings, "DR_REPLICATION_ENABLED", True)
    monkeypatch.setattr(settings, "DR_REPLICATION_TARGET", "dr-rq")

    _ingest("1.2.3.9.44.1", "P-DR5", "1.2.3.5.44.1")
    assert replication_service.sweep()["replicated"] >= 1
    assert _record("1.2.3.9.44.1").status == "REPLICATED"

    # A second slice joins the same study afterwards.
    _ingest("1.2.3.9.44.1", "P-DR5", "1.2.3.5.44.2")
    assert _record("1.2.3.9.44.1").status == "PENDING"  # re-queued by the hook

    assert replication_service.sweep()["replicated"] >= 1
    assert _record("1.2.3.9.44.1").status == "REPLICATED"
    assert _record("1.2.3.9.44.1").object_count >= 2
    reset_storage_backend()


# --------------------------------------------------------------------------- #
# Admin API
# --------------------------------------------------------------------------- #
def test_api_requires_auth(client):
    # The autouse `admin` fixture logged in on this client, and the API also
    # accepts cookie sessions (C03), so clear the jar to be anonymous.
    client.cookies.clear()
    assert client.get("/api/v1/replication").status_code == 401
    assert client.post("/api/v1/replication/sweep").status_code == 401


def test_api_requires_admin(client, admin):
    h = _auth(client, "plain_doc", "secret123")
    assert client.get("/api/v1/replication", headers=h).status_code == 403
    assert client.post("/api/v1/replication/sweep", headers=h).status_code == 403


def test_api_status_and_records(client, admin, tmp_path, monkeypatch):
    target_dir = tmp_path / "dr2"
    target_dir.mkdir()
    client.post("/api/v1/storage/backends", headers=admin, json={
        "name": "dr2", "kind": "local", "root_path": str(target_dir)})
    _ingest("1.2.3.9.66.1", "P-DR3", "1.2.3.5.66.1")
    monkeypatch.setattr(settings, "DR_REPLICATION_ENABLED", True)
    monkeypatch.setattr(settings, "DR_REPLICATION_TARGET", "dr2")

    res = client.post("/api/v1/replication/sweep", headers=admin)
    assert res.status_code == 200
    body = res.json()
    assert body["replicated"] >= 1

    res = client.get("/api/v1/replication", headers=admin)
    st = res.json()
    assert st["enabled"] is True
    assert st["replicated"] >= 1

    res = client.get("/api/v1/replication/records", headers=admin)
    rows = res.json()["records"]
    assert any(r["study_instance_uid"] == "1.2.3.9.66.1"
               and r["status"] == "REPLICATED" for r in rows)


def test_api_replicate_one_study(client, admin, tmp_path, monkeypatch):
    target_dir = tmp_path / "dr3"
    target_dir.mkdir()
    client.post("/api/v1/storage/backends", headers=admin, json={
        "name": "dr3", "kind": "local", "root_path": str(target_dir)})
    _ingest("1.2.3.9.55.1", "P-DR4", "1.2.3.5.55.1")
    monkeypatch.setattr(settings, "DR_REPLICATION_ENABLED", True)
    monkeypatch.setattr(settings, "DR_REPLICATION_TARGET", "dr3")

    res = client.post("/api/v1/replication/studies/1.2.3.9.55.1", headers=admin)
    assert res.status_code == 200, res.text
    assert res.json()["objects"] >= 1
    assert _record("1.2.3.9.55.1").status == "REPLICATED"

    # unknown study -> 404
    res = client.post("/api/v1/replication/studies/1.2.3.9.0.0", headers=admin)
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# LB health probes (roadmap #47)
# --------------------------------------------------------------------------- #
def test_health_live_ready(client, admin):
    live = client.get("/api/v1/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert live.json()["instance"] == settings.PAXS_INSTANCE_ID

    ready = client.get("/api/v1/health/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"

    full = client.get("/api/v1/health", headers=admin).json()
    assert full["instance"] == settings.PAXS_INSTANCE_ID
