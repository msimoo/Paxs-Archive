"""Tests for the pluggable StorageBackend (Phase B1).

Covers: backend selection via settings, local round-trip through the real
STOW -> DB -> WADO path, and the S3 backend against an in-memory fake boto3
client (no network, no MinIO required). The suite itself runs on SQLite and
MySQL via the standard conftest setup.
"""

import io

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.services import storage_service
from app.services.storage import (
    LocalStorageBackend,
    S3StorageBackend,
    get_storage_backend,
    reset_storage_backend,
)
from app.db.models import Instance
from app.db.session import SyncSessionLocal
from tests.helpers import ds_bytes, make_instance


class _Fake404(Exception):
    def __init__(self):
        super().__init__("Not Found")
        self.response = {"ResponseMetadata": {"HTTPStatusCode": 404}}


class _FakePaginator:
    """Single-page list_objects_v2 paginator over the fake bucket."""

    def __init__(self, client, prefix=None):
        self._client = client
        self._prefix = prefix

    def paginate(self, Bucket=None, Prefix=None, **kwargs):
        prefix = self._prefix if self._prefix is not None else (Prefix or "")
        keys = [k for k in self._client.objects if k.startswith(prefix)]
        if not keys:
            yield {}
            return
        yield {"Contents": [{"Key": k, "Size": len(self._client.objects[k])} for k in keys]}


class FakeS3Client:
    """Minimal in-memory stand-in for the boto3 s3 client surface we use."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket=None, Key=None, Body=b""):
        self.objects[Key] = Body

    def get_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            raise _Fake404()
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            raise _Fake404()
        return {"ContentLength": len(self.objects[Key])}

    def delete_object(self, Bucket=None, Key=None):
        self.objects.pop(Key, None)

    def delete_objects(self, Bucket=None, Delete=None):
        for obj in Delete.get("Objects", []):
            self.objects.pop(obj["Key"], None)

    def get_paginator(self, name):
        return _FakePaginator(self)

    def generate_presigned_url(self, ClientMethod=None, Params=None, ExpiresIn=None):
        return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?X-Amz-Expires={ExpiresIn}"


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #
def test_factory_default_is_local():
    assert isinstance(get_storage_backend(), LocalStorageBackend)


def test_factory_selects_s3(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "S3_BUCKET", "paxs-test")
    reset_storage_backend()
    try:
        backend = get_storage_backend()
        assert isinstance(backend, S3StorageBackend)
        assert backend.bucket == "paxs-test"
    finally:
        reset_storage_backend()
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")


# --------------------------------------------------------------------------- #
# Local backend end-to-end (real STOW -> DB -> WADO)
# --------------------------------------------------------------------------- #
def test_stow_stores_relative_key_and_wado_reads_it(client):
    ds = make_instance(study_uid="1.2.840.113619.2.408.123", series_uid="1.2.3.4.123",
                       sop_uid="1.2.3.5.123.1")
    boundary = "backend-test-1"
    body = (
        f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
        + ds_bytes(ds) + b"\r\n" + f"--{boundary}--\r\n".encode()
    )
    r = client.post(
        "/dicom-web/studies",
        headers={"Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}'},
        content=body,
    )
    assert r.status_code == 200

    with SyncSessionLocal() as s:
        inst = s.execute(select(Instance).where(Instance.sop_instance_uid == "1.2.3.5.123.1")).scalar_one()
        # The DB stores a portable relative key, not an absolute path.
        assert inst.file_path == "1.2.840.113619.2.408.123/1.2.3.4.123/1.2.3.5.123.1.dcm"
        assert storage_service.exists(inst.file_path)

    # WADO-RS returns the bytes.
    r = client.get("/dicom-web/studies/1.2.840.113619.2.408.123/series/1.2.3.4.123/instances/1.2.3.5.123.1")
    assert r.status_code == 200
    assert r.content == ds_bytes(ds)

    # Presign endpoint: local backend returns the app's own WADO-URI.
    client.post("/api/v1/auth/register", json={"username": "sbadmin", "password": "secret123"})
    res = client.post("/api/v1/auth/login", json={"username": "sbadmin", "password": "secret123"})
    h = {"Authorization": f"Bearer {res.json()['access_token']}"}
    r = client.get("/api/v1/storage/presign/1.2.840.113619.2.408.123/1.2.3.4.123/1.2.3.5.123.1", headers=h)
    assert r.status_code == 200
    assert r.json()["backend"] == "local"
    # The fallback WADO-URI must be a *usable* link (requestType=WADO required).
    assert r.json()["url"] == (
        "http://paxs.test/dicom-web/wado"
        "?requestType=WADO&objectUID=1.2.3.5.123.1"
    )


# --------------------------------------------------------------------------- #
# S3 backend (fake client)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def s3_backend():
    return S3StorageBackend(bucket="paxs-test", client=FakeS3Client())


def test_s3_save_read_exists_size_sha(s3_backend):
    assert s3_backend.local_path("a/b.dcm") is None  # remote
    s3_backend.save_bytes("a/b.dcm", b"hello-dicom")
    assert s3_backend.exists("a/b.dcm")
    assert not s3_backend.exists("nope.dcm")
    assert s3_backend.size("a/b.dcm") == len(b"hello-dicom")
    assert s3_backend.read_bytes("a/b.dcm") == b"hello-dicom"
    assert s3_backend.sha256("a/b.dcm") == __import__("hashlib").sha256(b"hello-dicom").hexdigest()
    # read_stream is file-like
    stream = s3_backend.read_stream("a/b.dcm")
    assert stream.read() == b"hello-dicom"


def test_s3_delete_and_delete_tree(s3_backend):
    s3_backend.save_bytes("s1/i1.dcm", b"1")
    s3_backend.save_bytes("s1/i2.dcm", b"2")
    s3_backend.save_bytes("s2/i3.dcm", b"3")
    assert s3_backend.delete("s1/i1.dcm") is True
    assert not s3_backend.exists("s1/i1.dcm")
    s3_backend.delete_tree("s1")
    assert not s3_backend.exists("s1/i2.dcm")
    assert s3_backend.exists("s2/i3.dcm")


def test_s3_presigned_url(s3_backend):
    url = s3_backend.presigned_url("s1/i1.dcm", expires_seconds=120)
    assert "s3.example" in url and "s1/i1.dcm" in url
    assert "X-Amz-Expires=120" in url


def test_s3_usage_bytes(s3_backend):
    s3_backend.save_bytes("u/1.dcm", b"12345")
    s3_backend.save_bytes("u/2.dcm", b"1234567")
    assert s3_backend.usage_bytes() == 12


def test_s3_read_missing_raises(s3_backend):
    from app.services.storage import StorageError

    with pytest.raises(StorageError):
        s3_backend.read_bytes("missing.dcm")


def test_s3_backend_through_facade(monkeypatch):
    """The storage_service facade drives an S3 backend via settings."""
    fake = FakeS3Client()
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    reset_storage_backend()

    # Inject the fake into the singleton created by the factory.
    backend = get_storage_backend()
    backend._client = fake  # replace the lazy (unused) real client
    try:
        storage_service.save_bytes("facade/x.dcm", b"payload")
        assert storage_service.exists("facade/x.dcm")
        assert storage_service.read_bytes("facade/x.dcm") == b"payload"
        url = storage_service.presigned_url("facade/x.dcm")
        assert "s3.example" in url
        assert storage_service.local_path("facade/x.dcm") is None
    finally:
        reset_storage_backend()
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
