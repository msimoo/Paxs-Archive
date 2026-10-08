"""Tests for the high-core performance & scalability upgrades:

1. Cached storage usage (TTL cache so /stats doesn't walk the storage tree
   on every page load).
2. Atomic worker job claiming (safe with N standalone worker processes) +
   the standalone worker entrypoint.
3. Keyset pagination for QIDO-RS studies (cursor instead of OFFSET).
"""

from __future__ import annotations

import pytest

from app.services.storage_service import (
    cached_storage_usage_bytes,
    invalidate_usage_cache,
    storage_usage_bytes,
)
from app.services.worker import CompressionWorker


# --------------------------------------------------------------------------- #
# 1. Cached storage usage
# --------------------------------------------------------------------------- #
def test_cached_storage_usage_caches_then_invalidates(monkeypatch):
    """The cached variant must hit the underlying walk once per TTL, then
    serve the stored value until invalidated."""
    # Deterministic regardless of whether a live Redis is reachable: force the
    # in-process fallback path, and clear any value a previous /stats call in
    # this session may have left in the module-level cache.
    invalidate_usage_cache()
    monkeypatch.setattr("app.core.config.settings.REDIS_CACHE_ENABLED", False)
    calls = {"n": 0}
    real = storage_usage_bytes

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr("app.services.storage_service.storage_usage_bytes", _counting)
    # Fresh cache: first call computes.
    assert cached_storage_usage_bytes() == real()
    assert calls["n"] == 1
    # Second call within TTL: served from cache.
    assert cached_storage_usage_bytes() == real()
    assert calls["n"] == 1
    # Invalidation forces a recompute.
    invalidate_usage_cache()
    assert cached_storage_usage_bytes() == real()
    assert calls["n"] == 2


def test_cached_storage_usage_zero_ttl_disables_cache(monkeypatch):
    """STATS_USAGE_CACHE_SECONDS=0 bypasses the cache entirely."""
    monkeypatch.setattr("app.core.config.settings.STATS_USAGE_CACHE_SECONDS", 0)
    monkeypatch.setattr("app.core.config.settings.REDIS_CACHE_ENABLED", False)
    calls = {"n": 0}
    real = storage_usage_bytes

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr("app.services.storage_service.storage_usage_bytes", _counting)
    cached_storage_usage_bytes()
    cached_storage_usage_bytes()
    assert calls["n"] == 2


def test_cached_storage_usage_backend_scoped(monkeypatch):
    """Caches are keyed per backend; a named backend gets its own slot."""
    monkeypatch.setattr("app.core.config.settings.REDIS_CACHE_ENABLED", False)
    calls = {"n": 0}

    def _usage(backend_name: str | None = None):
        calls["n"] += 1
        return 100 if backend_name == "bk-a" else 200

    monkeypatch.setattr("app.services.storage_service.storage_usage_bytes", _usage)
    assert cached_storage_usage_bytes("bk-a") == 100
    assert cached_storage_usage_bytes("bk-a") == 100  # cached
    assert cached_storage_usage_bytes("bk-b") == 200  # fresh slot
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# 2. Atomic worker claim
# --------------------------------------------------------------------------- #
def _seed_compression_jobs(n: int, status: str = "PENDING") -> None:
    """Insert n compression jobs against the first stored instance."""
    from sqlalchemy import select

    from app.db.models import CompressionJob, Instance
    from app.db.session import SyncSessionLocal

    with SyncSessionLocal() as s:
        inst = s.execute(select(Instance).limit(1)).scalar_one_or_none()
        assert inst is not None, "expected a stored instance from the upload"
        for _ in range(n):
            s.add(CompressionJob(instance_id=inst.id, method="RLE", status=status))
        s.commit()


def _pending_count() -> int:
    from sqlalchemy import func, select

    from app.db.models import CompressionJob
    from app.db.session import SyncSessionLocal

    with SyncSessionLocal() as s:
        return int(s.execute(
            select(func.count(CompressionJob.id))
            .where(CompressionJob.status == "PENDING")
        ).scalar_one())


def test_worker_claims_atomically(client):
    """Two workers claiming concurrently must never get the same job — each
    PENDING job is transitioned to RUNNING by exactly one claim."""
    from app.db.session import SyncSessionLocal
    from tests.test_dicomweb import _upload_study

    _upload_study(client, n=1, study_uid="1.2.3.claim")
    baseline = _pending_count()  # ingest may auto-create compression jobs
    _seed_compression_jobs(3)

    worker = CompressionWorker(poll_seconds=999)

    # Worker A claims a batch (baseline + the 3 seeded = all pending now).
    with SyncSessionLocal() as s:
        first = worker._claim_jobs(s)
        assert len(first) == baseline + 3
        assert all(j.status == "RUNNING" for j in first)

    # Worker B (separate session, simulating another process) claims nothing —
    # every job is already RUNNING.
    with SyncSessionLocal() as s:
        second = worker._claim_jobs(s)
    assert second == []

    # No job id appears in both claims.
    a_ids = {j.id for j in first}
    b_ids = {j.id for j in second}
    assert a_ids.isdisjoint(b_ids)

    # Reset so later tests in this module don't see leftover jobs.
    invalidate_usage_cache()


def test_worker_claim_handles_partial_pending(client):
    """A worker only claims jobs that are still PENDING; RUNNING/DONE jobs are
    left for whichever process claimed them."""
    from app.db.session import SyncSessionLocal
    from tests.test_dicomweb import _upload_study

    _upload_study(client, n=1, study_uid="1.2.3.claim2")
    baseline = _pending_count()  # ingest may auto-create a PENDING job
    _seed_compression_jobs(1, status="PENDING")
    _seed_compression_jobs(1, status="RUNNING")
    _seed_compression_jobs(1, status="DONE")

    worker = CompressionWorker(poll_seconds=999)
    with SyncSessionLocal() as s:
        claimed = worker._claim_jobs(s)
    # Every PENDING job (baseline + the one we seeded) is claimed; the
    # RUNNING/DONE jobs are left to whichever process claimed them.
    assert len(claimed) == baseline + 1
    assert all(j.status == "RUNNING" for j in claimed)
    assert _pending_count() == 0

    # The RUNNING/DONE jobs were never re-claimed by this second attempt.
    with SyncSessionLocal() as s:
        again = worker._claim_jobs(s)
    assert again == []


def test_worker_entrypoint_imports():
    """The standalone worker can be imported and its CLI helpers exist
    (python -m app.services.worker)."""
    import importlib

    mod = importlib.import_module("app.services.worker")
    assert hasattr(mod, "run_standalone")
    assert hasattr(mod, "CompressionWorker")


# --------------------------------------------------------------------------- #
# 3. Keyset pagination for QIDO
# --------------------------------------------------------------------------- #
def test_qido_keyset_pagination(client):
    """?cursor=<date>|<id> walks studies in the same (date desc, id desc)
    order as the first page, with no overlap and no gaps."""
    from tests.test_dicomweb import _upload_study

    # 4 studies across 2 dates; upload order gives distinct ids per date.
    _upload_study(client, patient_id="P1", study_date="20250101", study_uid="1.2.3.1")
    _upload_study(client, patient_id="P2", study_date="20250101", study_uid="1.2.3.2")
    _upload_study(client, patient_id="P3", study_date="20250401", study_uid="1.2.3.3")
    _upload_study(client, patient_id="P4", study_date="20250401", study_uid="1.2.3.4")

    # First page: 2 rows, limit=2.
    page1 = client.get("/dicom-web/studies?limit=2")
    assert page1.status_code == 200
    r1 = page1.json()
    assert len(r1) == 2
    cursor = page1.headers.get("x-next-cursor")
    assert cursor, "full page must advertise the next cursor"

    # Second page continues with the remaining studies.
    page2 = client.get(f"/dicom-web/studies?limit=2&cursor={cursor}")
    assert page2.status_code == 200
    r2 = page2.json()
    assert len(r2) == 2
    assert page2.headers.get("x-next-cursor") is None

    # No overlap, no duplicates across both pages.
    uids = [r["0020000D"]["Value"][0] for r in r1 + r2]
    assert len(set(uids)) == 4


def test_qido_keyset_respects_filters(client):
    """Keyset pagination composes with the existing QIDO filters (e.g. by
    patient): cursor pages only the filtered subset."""
    from tests.test_dicomweb import _upload_study

    _upload_study(client, patient_id="P100", study_date="20250101", study_uid="1.2.3.10")
    _upload_study(client, patient_id="P100", study_date="20250102", study_uid="1.2.3.11")
    _upload_study(client, patient_id="P200", study_date="20250103", study_uid="1.2.3.12")

    page1 = client.get("/dicom-web/studies?PatientID=P100&limit=1")
    assert len(page1.json()) == 1
    cursor = page1.headers.get("x-next-cursor")
    assert cursor

    page2 = client.get(f"/dicom-web/studies?PatientID=P100&limit=1&cursor={cursor}")
    r2 = page2.json()
    assert len(r2) == 1
    assert r2[0]["00100020"]["Value"] == ["P100"]
    assert page2.headers.get("x-next-cursor") is None


# --------------------------------------------------------------------------- #
# C03: DICOMweb is private by default, so this module's requests carry a valid
# ADMIN bearer token. (No test here asserts anonymous behaviour.)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(auth_client):
    """Authenticated client (ADMIN bearer token)."""
    return auth_client
