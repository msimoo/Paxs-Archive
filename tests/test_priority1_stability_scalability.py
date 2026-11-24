"""Tests for Priority 1 stability & scalability improvements.

Covers:
  1. CompressionWorker FOR UPDATE SKIP LOCKED claiming, shared heartbeat batching,
     and WORKER_COMPRESSION_CONCURRENCY parallel batch execution.
  2. DIMSE SCP PDU size & explicit network/ACSE/DIMSE timeout configuration.
  3. Single-roundtrip UNION ALL tenant ownership validation in indexing_service.
  4. Local storage disk low-water mark guard (DICOM 0xA700 / HTTP 507) + telemetry.
  5. DIMSE DB pool capacity accounting, Services Control probe concurrency cap,
     and bounded TTL pruning for `_disc_jobs`.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.db.session import dimse_pool_capacity
from app.services import storage_service
from app.services.indexing_service import CrossTenantIngestError, assert_tenant_owns_ingest_target
from app.services.quota_service import QuotaExceeded
from app.services.storage.local import LocalStorageBackend
from app.services.worker import CompressionWorker


_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


def test_dimse_pool_capacity_matches_options():
    cap = dimse_pool_capacity()
    expected_size = max(2, min(max(int(settings.DB_POOL_SIZE), int(settings.DICOM_MAX_ASSOCIATIONS)), 16))
    assert cap == expected_size + 8


def test_start_receiver_applies_pdu_and_timeouts(monkeypatch):
    from app.dicom import receiver

    captured = {}

    class _FakeAE:
        def __init__(self, ae_title):
            self.ae_title = ae_title
            self.maximum_associations = None
            self.maximum_pdu_size = None
            self.network_timeout = None
            self.acse_timeout = None
            self.dimse_timeout = None
            self.requested_contexts = None

        def add_supported_context(self, *args, **kwargs):
            return None

        def start_server(self, addr, **kwargs):
            captured["ae"] = self

        def shutdown(self):
            return None

    monkeypatch.setattr(receiver, "_ae", None)
    monkeypatch.setattr(receiver, "AE", _FakeAE)
    monkeypatch.setattr(settings, "DICOM_MAX_PDU_SIZE", 131072)
    monkeypatch.setattr(settings, "DICOM_NETWORK_TIMEOUT_SECONDS", 45)
    monkeypatch.setattr(settings, "DICOM_ACSE_TIMEOUT_SECONDS", 20)
    monkeypatch.setattr(settings, "DICOM_DIMSE_TIMEOUT_SECONDS", 50)

    try:
        receiver.start_receiver(host="127.0.0.1", port=10499, ae_title="TEST_SCP")
    finally:
        receiver.shutdown_receiver()

    ae = captured["ae"]
    assert ae.maximum_pdu_size == 131072
    assert ae.network_timeout == 45
    assert ae.acse_timeout == 20
    assert ae.dimse_timeout == 50


def test_assert_tenant_owns_ingest_target_uses_single_sql_roundtrip():
    """All three Study/Series/Instance checks run in a single DB execute call."""
    calls: list[object] = []

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _FakeSession:
        def __init__(self, rows):
            self._rows = rows

        async def execute(self, stmt):
            calls.append(stmt)
            return _FakeResult(self._rows)

    meta = {
        "study_instance_uid": "1.2.3.4",
        "series_instance_uid": "1.2.3.4.5",
        "sop_instance_uid": "1.2.3.4.5.6",
    }

    async def _run():
        # Matching tenant -> allowed in 1 query.
        ok_rows = [
            SimpleNamespace(level="study", id=1, tenant_id=7, patient_id=10, patient_tenant_id=7),
            SimpleNamespace(level="series", id=1, tenant_id=7, patient_id=10, patient_tenant_id=7),
        ]
        await assert_tenant_owns_ingest_target(_FakeSession(ok_rows), meta, tenant_id=7)
        assert len(calls) == 1

        # Conflicting series ownership -> rejected in 1 query with exact message.
        calls.clear()
        bad_rows = [
            SimpleNamespace(level="series", id=2, tenant_id=99, patient_id=20, patient_tenant_id=99),
            SimpleNamespace(level="instance", id=2, tenant_id=99, patient_id=20, patient_tenant_id=99),
        ]
        with pytest.raises(CrossTenantIngestError, match="series 1.2.3.4.5"):
            await assert_tenant_owns_ingest_target(_FakeSession(bad_rows), meta, tenant_id=7)
        assert len(calls) == 1

    asyncio.run(_run())


def test_local_disk_low_water_mark_guard_and_status(tmp_path: Path, monkeypatch):
    bk = LocalStorageBackend(tmp_path, name="primary-local")
    storage_service.invalidate_disk_space_cache()

    monkeypatch.setattr(settings, "STORAGE_MIN_FREE_GB", 5.0)
    monkeypatch.setattr(settings, "STORAGE_MIN_FREE_PERCENT", 5.0)

    # Plenty of free space (50 GB of 100 GB) -> allowed.
    with patch.object(
        shutil, "disk_usage", return_value=_DiskUsage(100 * 1024**3, 50 * 1024**3, 50 * 1024**3)
    ):
        storage_service.check_local_disk_space(bk, incoming_bytes=10 * 1024**2)

    storage_service.invalidate_disk_space_cache()

    # Low free space (2 GB of 100 GB < 5 GB floor) -> backend QuotaExceeded (HTTP 507 / 0xA700).
    with patch.object(
        shutil, "disk_usage", return_value=_DiskUsage(100 * 1024**3, 98 * 1024**3, 2 * 1024**3)
    ):
        with pytest.raises(QuotaExceeded) as exc_info:
            storage_service.check_local_disk_space(bk, incoming_bytes=1024)
        assert exc_info.value.tenant_limited is False
        assert exc_info.value.http_status == 507

        with patch.object(storage_service, "_resolve", return_value=bk):
            status = storage_service.get_local_disk_status()
            assert status["local"] is True
            assert status["low_space"] is True
            assert status["free_gb"] == 2.0

    # Zero-charge rewrite (in-place compression) must NOT be blocked by low disk floor.
    with patch.object(storage_service, "_resolve", return_value=bk), patch.object(
        shutil, "disk_usage", return_value=_DiskUsage(100 * 1024**3, 99 * 1024**3, 1 * 1024**3)
    ):
        staged = storage_service.stage_bytes("1.2/3.4/5.6.dcm", b"abc", charge_bytes=0)
        staged.discard()


def test_worker_claim_uses_skip_locked_and_no_per_job_heartbeat_thread(monkeypatch):
    worker = CompressionWorker(poll_seconds=1.0)
    executed_sql: list[str] = []

    class _FakeScalarResult:
        def __init__(self, vals):
            self._vals = vals
            self.rowcount = len(vals)

        def scalars(self):
            return self

        def all(self):
            return list(self._vals)

    fake_job = SimpleNamespace(id=42, owner_id=worker.owner_id, status="RUNNING")
    call_idx = {"n": 0}

    class _FakeSession:
        def execute(self, stmt):
            sql = str(stmt.compile(dialect=postgresql.dialect()))
            executed_sql.append(sql)
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                return _FakeScalarResult([42])
            if call_idx["n"] == 2:
                return _FakeScalarResult([42])
            return _FakeScalarResult([fake_job])

        def commit(self):
            return None

        def rollback(self):
            return None

    claimed = worker._claim_jobs(_FakeSession(), limit=2)
    assert [j.id for j in claimed] == [42]
    assert "FOR UPDATE SKIP LOCKED" in executed_sql[0].upper()

    # Verify _process_batch tracks active job IDs in shared set without spawning heartbeat threads.
    monkeypatch.setattr(settings, "WORKER_BATCH_SIZE", 2)
    monkeypatch.setattr(settings, "WORKER_COMPRESSION_CONCURRENCY", 2)
    monkeypatch.setattr(settings, "TRIAGE_RUN_IN_WORKER", False)
    monkeypatch.setattr(settings, "LIFECYCLE_RUN_IN_WORKER", False)
    monkeypatch.setattr(settings, "QUOTA_RECONCILE_RUN_IN_WORKER", False)
    monkeypatch.setattr(settings, "STORAGE_RECONCILE_SWEEP_EVERY_TICKS", 0)

    processed_ids: list[int] = []
    lock = threading.Lock()
    waves = [[SimpleNamespace(id=101), SimpleNamespace(id=102)], []]

    class _CtxSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, _model, job_id):
            return SimpleNamespace(id=job_id, owner_id=worker.owner_id)

        def commit(self):
            return None

    def _fake_claim(_session, limit=None):
        return waves.pop(0) if waves else []

    def _fake_process(_session, job):
        assert job.id in worker._active_job_ids
        time.sleep(0.02)
        with lock:
            processed_ids.append(job.id)

    monkeypatch.setattr("app.services.worker.SyncSessionLocal", _CtxSession)
    monkeypatch.setattr(worker, "_claim_jobs", _fake_claim)
    monkeypatch.setattr(worker, "_process_job", _fake_process)
    monkeypatch.setattr(worker, "_sweep_sessions", lambda: None)
    monkeypatch.setattr(worker, "_sweep_scheduled_backup", lambda: None)

    worker._process_batch()
    assert sorted(processed_ids) == [101, 102]
    assert worker._active_job_ids == set()


def test_services_control_probe_semaphore_enforces_limit(monkeypatch):
    from app.api.v1 import services_control

    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(services_control, "_dcmtk_probe_sem", sem)
    assert sem.acquire(blocking=False) is True
    try:
        with pytest.raises(HTTPException) as exc_info:
            services_control._run_bounded_probe(lambda x: x, {"ok": True})
        assert exc_info.value.status_code == 429
    finally:
        sem.release()


def test_disc_jobs_pruning_expires_old_terminal_and_preserves_active():
    from app.api.v1 import dicomdir

    old_ts = datetime.now(timezone.utc) - timedelta(hours=2)
    old_id = uuid4()
    active_id = uuid4()
    with dicomdir._disc_jobs_lock:
        dicomdir._disc_jobs.clear()
        dicomdir._disc_jobs[old_id] = dicomdir.DiscBurnJob(
            owner=1, study_uid="1.2.3", count=1, drive="D:",
            started_at=old_ts, finished_at=old_ts, phase="verified",
        )
        dicomdir._disc_jobs[active_id] = dicomdir.DiscBurnJob(
            owner=1, study_uid="1.2.4", count=1, drive="D:",
            started_at=old_ts, phase="burning",
        )
        dicomdir._prune_disc_jobs_locked()
        assert old_id not in dicomdir._disc_jobs
        assert active_id in dicomdir._disc_jobs
        dicomdir._disc_jobs.clear()
