"""Multi-storage configuration (/api/v1/storage) + backend-aware ingest.

Covers:
  * auth gates — 401 anonymous, 403 non-admin
  * backend CRUD — validation (name, kind, required fields, duplicates),
    first-backend-becomes-default, PATCH enable/default, DELETE guard when
    instances reference the backend
  * factory — get_storage_backend(name) resolves a named backend and caches
  * ingest — with a registered default backend, a STOW-RS ingest records
    `storage_backend` on Study + Instance and writes to the named root
  * move — study relocation copies objects to the target backend and updates
    the DB; objects are readable through the new backend afterwards
  * local test-connection probe
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest
from sqlalchemy import select

from app.db.models import Instance, Series, Study
from app.db.session import AsyncSessionLocal
from app.services import indexing_service, storage_service
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
    """A seeded ADMIN usable from every test (also requested by name)."""
    return _auth(client, "store_admin")


@pytest.fixture()
def root_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _read_study_and_instance(study_uid):
    """Sync helper: (study, first_instance) rows from the DB."""

    async def _go():
        async with AsyncSessionLocal() as s:
            st = (await s.execute(select(Study).where(
                Study.study_instance_uid == study_uid))).scalar_one()
            inst = (await s.execute(select(Instance).join(Series).where(
                Series.study_id == st.id))).scalars().first()
            return st, inst

    return asyncio.run(_go())


def _new_local(name, root, client, admin, is_default=False):
    res = client.post("/api/v1/storage/backends", headers=admin, json={
        "name": name, "kind": "local", "root_path": root,
        "is_default": is_default})
    assert res.status_code == 201, res.text
    return res.json()


# --------------------------------------------------------------------------- #
# Auth gates
# --------------------------------------------------------------------------- #
def test_storage_requires_auth(client):
    assert client.get("/api/v1/storage/backends").status_code == 401
    assert client.post("/api/v1/storage/backends", json={}).status_code == 401


def test_storage_requires_admin(client):
    _register(client, "store_admin")
    h = _auth(client, "plain_doc", "secret123")
    assert client.get("/api/v1/storage/backends", headers=h).status_code == 403
    assert client.post("/api/v1/storage/backends", json={
        "name": "x", "kind": "local", "root_path": "/tmp"}, headers=h).status_code == 403


# --------------------------------------------------------------------------- #
# CRUD + validation
# --------------------------------------------------------------------------- #
def test_create_validation(client, admin, root_dir):
    assert client.post("/api/v1/storage/backends", headers=admin, json={
        "name": "bad name!", "kind": "local", "root_path": root_dir}).status_code == 422
    assert client.post("/api/v1/storage/backends", headers=admin, json={
        "name": "x", "kind": "local"}).status_code == 422  # root_path required
    assert client.post("/api/v1/storage/backends", headers=admin, json={
        "name": "x", "kind": "s3"}).status_code == 422  # bucket required
    assert client.post("/api/v1/storage/backends", headers=admin, json={
        "name": "x", "kind": "ftp", "root_path": root_dir}).status_code == 422


def test_first_backend_is_default(client, admin, root_dir):
    row = _new_local("primary", root_dir, client, admin)
    assert row["is_default"] is True
    data = client.get("/api/v1/storage/backends", headers=admin).json()
    assert data["backends"][0]["name"] == "primary"


def test_duplicate_rejected(client, admin, root_dir):
    _new_local("dup", root_dir, client, admin)
    res = client.post("/api/v1/storage/backends", headers=admin, json={
        "name": "dup", "kind": "local", "root_path": root_dir})
    assert res.status_code == 409


def test_patch_default_and_enable(client, admin, root_dir):
    _new_local("a", os.path.join(root_dir, "a"), client, admin)
    _new_local("b", os.path.join(root_dir, "b"), client, admin)
    res = client.patch("/api/v1/storage/backends/b", headers=admin, json={
        "is_default": True, "enabled": False})
    assert res.status_code == 200
    data = client.get("/api/v1/storage/backends", headers=admin).json()
    by_name = {b["name"]: b for b in data["backends"]}
    assert by_name["b"]["is_default"] is True
    assert by_name["b"]["enabled"] is False
    assert by_name["a"]["is_default"] is False
    # only one default at a time
    assert sum(1 for b in data["backends"] if b["is_default"]) == 1


def test_delete_guard_with_instances(client, admin, root_dir):
    _new_local("guarded", root_dir, client, admin)
    # ingest one instance on the named backend (it's the default)
    _ingest_with_backend(client, study_uid="1.2.3.9.99.1", patient="P-GUARD")
    res = client.delete("/api/v1/storage/backends/guarded", headers=admin)
    assert res.status_code == 409
    assert "instance" in res.json()["detail"].lower()
    # delete works once un-referenced
    res = client.delete("/api/v1/storage/backends/nope", headers=admin)
    assert res.status_code == 404


def test_test_connection_local(client, admin, root_dir):
    _new_local("probe", root_dir, client, admin)
    res = client.post("/api/v1/storage/backends/probe/test", headers=admin)
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["kind"] == "local"


# --------------------------------------------------------------------------- #
# Factory + backend-aware ingest
# --------------------------------------------------------------------------- #
def _ingest_with_backend(client, *, study_uid, patient, series_uid="1.2.3.4.77.1",
                         sop_uid="1.2.3.5.77.1"):
    """Mimic the receiver ingest path: resolve the study backend, save with it,
    and index recording it."""
    from app.dicom.parser import parse_metadata

    ds = make_instance(study_uid=study_uid, series_uid=series_uid,
                       sop_uid=sop_uid, instance_num=1, patient_id=patient)
    backend = storage_service.resolve_study_backend(study_uid)
    key, sha = storage_service.save_dataset(ds, backend_name=backend)

    async def _index():
        async with AsyncSessionLocal() as s:
            async with s.begin():
                await indexing_service.index_new_instance(
                    s, parse_metadata(ds), key, sha, performed_by="test",
                    storage_backend=backend,
                )

    asyncio.run(_index())


def test_factory_resolves_named_backend(client, admin, root_dir):
    _new_local("factory-bk", root_dir, client, admin)
    bk = get_storage_backend("factory-bk")
    assert bk.local_path("x/y.dcm") == os.path.join(root_dir, "x", "y.dcm")
    with pytest.raises(KeyError):
        get_storage_backend("missing-bk")
    reset_storage_backend()


def test_ingest_records_named_backend(client, admin, root_dir):
    _new_local("ingest-bk", root_dir, client, admin)
    reset_storage_backend()
    _ingest_with_backend(client, study_uid="1.2.3.9.88.1", patient="P-INGEST")
    st, inst = _read_study_and_instance("1.2.3.9.88.1")
    assert st.storage_backend == "ingest-bk"
    assert inst.storage_backend == "ingest-bk"
    # object physically lives in the named root
    assert storage_service.exists(inst.file_path, backend_name="ingest-bk")
    assert not storage_service.exists(inst.file_path)  # not on env root


def test_move_study_between_backends(client, admin, root_dir):
    d1 = os.path.join(root_dir, "one")
    d2 = os.path.join(root_dir, "two")
    os.makedirs(d1); os.makedirs(d2)
    _new_local("src-bk", d1, client, admin)
    _new_local("dst-bk", d2, client, admin, is_default=False)
    reset_storage_backend()
    _ingest_with_backend(client, study_uid="1.2.3.9.66.1", patient="P-MOVE")

    _, inst = _read_study_and_instance("1.2.3.9.66.1")
    old_key = inst.file_path
    assert inst.storage_backend == "src-bk"
    assert storage_service.exists(old_key, backend_name="src-bk")

    res = client.post("/api/v1/storage/studies/1.2.3.9.66.1/move",
                      headers=admin, json={"target_backend": "dst-bk"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["moved"] >= 1
    assert body["from_backend"] == "src-bk"

    st, inst = _read_study_and_instance("1.2.3.9.66.1")
    assert st.storage_backend == "dst-bk"
    assert inst.storage_backend == "dst-bk"
    # readable on the target, gone from the source
    assert storage_service.exists(inst.file_path, backend_name="dst-bk")
    assert not storage_service.exists(old_key, backend_name="src-bk")
