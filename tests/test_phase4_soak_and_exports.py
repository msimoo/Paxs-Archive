"""Tests for Phase 4: async export jobs, Prometheus alerts, and soak test harness."""

from __future__ import annotations

import importlib.util
import re
import time
from pathlib import Path

import pytest

from app.api.v1 import metrics as metrics_mod
from app.services.export_job_service import ExportJobManager


def _load_soak_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "loadtest" / "soak_test.py"
    spec = importlib.util.spec_from_file_location("paxs_soak_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_export_job_manager_lifecycle_and_owner_isolation(tmp_path: Path) -> None:
    mgr = ExportJobManager(max_concurrent=1, max_retained=4)
    try:
        def build(ws: Path) -> tuple[Path, str, int]:
            out = ws / "study.zip"
            out.write_bytes(b"PK\x03\x04ZIPDATA")
            return out, "study.zip", out.stat().st_size

        job = mgr.create_job(owner_id=42, kind="anonymized", study_uids=["1.2.3"], builder=build)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            cur = mgr.get_for_owner(job.job_id, 42)
            assert cur is not None
            if cur.is_terminal():
                break
            time.sleep(0.02)

        final = mgr.get_for_owner(job.job_id, 42)
        assert final is not None
        assert final.phase == "ready"
        assert final.size_bytes == 11
        assert final.file_path is not None and final.file_path.is_file()
        assert final.snapshot()["download_url"] == f"/api/v1/export/jobs/{job.job_id}/download"

        # Another user cannot view or delete this job.
        assert mgr.get_for_owner(job.job_id, 99) is None
        assert mgr.delete_for_owner(job.job_id, 99) is False

        workspace = final.workspace
        assert mgr.delete_for_owner(job.job_id, 42) is True
        assert not workspace.exists()
    finally:
        mgr.clear()


def test_export_job_manager_sanitizes_errors_and_cleans_workspace() -> None:
    mgr = ExportJobManager(max_concurrent=1, max_retained=4)
    try:
        def boom(_ws: Path) -> tuple[Path, str, int]:
            raise ValueError("secret PHI patient name leaked")

        job = mgr.create_job(owner_id=1, kind="dicomdir", study_uids=["1.2.3"], builder=boom)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            cur = mgr.get_for_owner(job.job_id, 1)
            assert cur is not None
            if cur.is_terminal():
                break
            time.sleep(0.02)

        final = mgr.get_for_owner(job.job_id, 1)
        assert final is not None
        assert final.phase == "failed"
        assert final.error is not None
        assert "PHI" not in final.error
        assert not final.workspace.exists()
    finally:
        mgr.clear()


def test_prometheus_alerts_only_reference_real_exported_metrics() -> None:
    alerts_path = Path(__file__).resolve().parents[1] / "deploy" / "prometheus-alerts.yml"
    text = alerts_path.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\bpaxs_[a-z0-9_]+\b", text))
    exported = set(metrics_mod._INFO_KEYS)
    unknown = referenced - exported
    assert not unknown, f"Alert rules reference unknown metrics: {sorted(unknown)}"
    for expected in (
        "PaxsDbDown",
        "PaxsEventLoopLagHigh",
        "PaxsBlockingIoRejected",
        "PaxsVolumeAssemblyWaitersHigh",
        "PaxsRedisCircuitOpen",
        "PaxsRoutingQueueFailures",
    ):
        assert expected in text


def test_soak_harness_dry_run_and_slo_evaluation(tmp_path: Path) -> None:
    soak = _load_soak_module()
    json_out = tmp_path / "soak.json"
    exit_code = soak.main(["--dry-run", "--json-out", str(json_out)])
    assert exit_code == 0
    assert json_out.is_file()

    metrics = soak.parse_prometheus_metrics(
        "# HELP paxs_event_loop_lag_seconds lag\n"
        "paxs_event_loop_lag_seconds 0.312\n"
        "paxs_process_resident_memory_bytes 268435456\n"
        'paxs_worker_queue_depth{state="pending"} 5\n'
    )
    assert metrics["paxs_event_loop_lag_seconds"] == pytest.approx(0.312)
    assert metrics["paxs_process_resident_memory_bytes"] == pytest.approx(268435456.0)

    bad = soak.SoakResult(
        http_requests=100, http_5xx=5, dimse_sent=10, dimse_failed=2, max_event_loop_lag_s=0.4
    )
    violations = soak.evaluate_slos(bad)
    assert len(violations) == 3
