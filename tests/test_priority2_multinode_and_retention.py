"""Tests for Priority 2 multi-node scale & DB retention features.

Covers:
  1. PgBouncer transaction-pooling compatibility (`DB_PGBOUNCER_COMPAT` disables
     asyncpg statement caches & ships `deploy/pgbouncer.ini`).
  2. Multi-node export job affinity (`owner_node`, Redis mirror fallback for
     cross-node status polling, owner isolation, and HAProxy sticky routing).
  3. Bounded `compression_jobs` pruning and compliance-safe `audit_logs`
     verified NDJSON.gz archival before deletion.
  4. Upgraded soak harness (`--dimse-engine dcmtk|pynetdicom`, `/dicom-web/`).
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.core.config import settings
from app.db.session import _dimse_pool_options, async_pool_options
from app.services import retention_service
from app.services.export_job_service import ExportJobManager
from tools.loadtest import soak_test


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pgbouncer_compat_disables_asyncpg_statement_caches(monkeypatch):
    monkeypatch.setattr(settings, "DB_PGBOUNCER_COMPAT", False)
    assert "connect_args" not in async_pool_options(settings.DATABASE_URL)
    assert "connect_args" not in _dimse_pool_options()

    monkeypatch.setattr(settings, "DB_PGBOUNCER_COMPAT", True)
    api_opts = async_pool_options(settings.DATABASE_URL)
    dimse_opts = _dimse_pool_options()
    assert api_opts["connect_args"]["statement_cache_size"] == 0
    assert api_opts["connect_args"]["prepared_statement_cache_size"] == 0
    assert dimse_opts["connect_args"]["statement_cache_size"] == 0

    pgbouncer = (REPO_ROOT / "deploy" / "pgbouncer.ini").read_text(encoding="utf-8")
    assert "pool_mode = transaction" in pgbouncer
    assert "DB_PGBOUNCER_COMPAT=true" in pgbouncer


def test_haproxy_pins_stateful_export_routes():
    cfg = (REPO_ROOT / "deploy" / "haproxy.cfg").read_text(encoding="utf-8")
    assert "use_backend paxs_api_sticky if is_stateful_export" in cfg
    assert "backend paxs_api_sticky" in cfg
    assert "cookie PAXS_NODE insert indirect nocache httponly" in cfg


def test_export_job_manager_stamps_owner_node_and_reads_redis_mirror(monkeypatch):
    monkeypatch.setattr(settings, "PAXS_INSTANCE_ID", "node-alpha")
    fake_store: dict[str, str] = {}

    class _FakeRedis:
        def setex(self, key: str, _ttl: int, value: str) -> None:
            fake_store[key] = value

        def get(self, key: str) -> bytes | None:
            val = fake_store.get(key)
            return val.encode("utf-8") if val is not None else None

        def delete(self, key: str) -> None:
            fake_store.pop(key, None)

    monkeypatch.setattr("app.services.redis.cache.is_redis_ready_fast", lambda: True)
    monkeypatch.setattr("app.services.redis.cache.get_redis_client", lambda: _FakeRedis())

    node_a = ExportJobManager()
    node_b = ExportJobManager()
    try:
        def _builder(ws: Path) -> tuple[Path, str, int]:
            out = ws / "study.zip"
            out.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            return out, "study.zip", out.stat().st_size

        job = node_a.create_job(owner_id=7, kind="anonymized", study_uids=["1.2.3"], builder=_builder)
        assert job.owner_node == "node-alpha"

        # Sibling node B sees status for owner 7 via Redis mirror, but denies owner 99.
        mirrored = node_b.get_status_for_owner(job.job_id, owner_id=7)
        assert mirrored is not None
        assert mirrored["owner_node"] == "node-alpha"
        assert "owner_id" not in mirrored
        assert node_b.get_status_for_owner(job.job_id, owner_id=99) is None
    finally:
        node_a.clear()
        node_b.clear()


def test_retention_prunes_compression_jobs_and_archives_audit_logs_before_delete(tmp_path: Path):
    executed: list[object] = []

    class _FakeResult:
        def __init__(self, items, rowcount: int = 0):
            self._items = items
            self.rowcount = rowcount

        def scalars(self):
            return self

        def all(self):
            return list(self._items)

    # 1. Compression jobs pruning (disabled when days=0; deletes matched IDs when >0).
    class _CompSession:
        def __init__(self):
            self._step = 0

        def execute(self, stmt):
            executed.append(stmt)
            self._step += 1
            if self._step == 1:
                return _FakeResult([10, 11, 12])
            return _FakeResult([], rowcount=3)

    assert retention_service.prune_completed_compression_jobs(_CompSession(), retention_days=0) == 0
    assert retention_service.prune_completed_compression_jobs(_CompSession(), retention_days=30) == 3

    # 2. Audit log archival verifies gzip NDJSON on disk BEFORE deleting rows.
    old_ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
    sample_rows = [
        SimpleNamespace(
            id=101, user_id=1, performed_at=old_ts, action="STORED", performed_by="dicom-receiver",
            sop_instance_uid="1.2.3.1", study_instance_uid="1.2.3", sha256_hash="abc",
            config_key=None, old_value=None, new_value=None,
        ),
        SimpleNamespace(
            id=102, user_id=2, performed_at=old_ts, action="EXPORTED", performed_by="admin",
            sop_instance_uid=None, study_instance_uid="1.2.3", sha256_hash=None,
            config_key=None, old_value=None, new_value=None,
        ),
    ]

    class _AuditSession:
        def __init__(self):
            self._step = 0

        def execute(self, stmt):
            self._step += 1
            if self._step == 1:
                return _FakeResult(sample_rows)
            return _FakeResult([], rowcount=len(sample_rows))

    # Default retention_days=0 keeps all audit logs forever.
    noop = retention_service.archive_and_prune_audit_logs(_AuditSession(), retention_days=0, archive_dir=tmp_path)
    assert noop == {"archived": 0, "pruned": 0, "archive_path": None}

    res = retention_service.archive_and_prune_audit_logs(_AuditSession(), retention_days=365, archive_dir=tmp_path)
    assert res["archived"] == 2
    assert res["pruned"] == 2
    archive = Path(res["archive_path"])
    assert archive.is_file()
    with gzip.open(archive, "rt", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh]
    assert [r["id"] for r in records] == [101, 102]
    assert records[0]["action"] == "STORED"


def test_soak_harness_detects_dcmsend_and_dry_run_passes():
    dcmsend = soak_test._resolve_dcmsend()
    assert dcmsend is not None and dcmsend.is_file()
    rc = soak_test.main(["--dry-run", "--dimse-engine", "dcmtk", "--pixel-dim", "128"])
    assert rc == 0
