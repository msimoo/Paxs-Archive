"""Self-contained live heavy-work benchmark runner for PAXS Archive (Priority 3).

Boots a real PAXS Archive Uvicorn + FastAPI + DIMSE SCP process against the
configured PostgreSQL database with an isolated temporary storage root, drives
parallel native OFFIS DCMTK 3.7.1 ``dcmsend`` bursts + concurrent HTTP load,
samples Prometheus ``/metrics``, cleans up all synthetic ``SOAK-*`` rows from
PostgreSQL afterward, and writes a JSON benchmark report.

Usage::

    venv\\Scripts\\python.exe tools/loadtest/run_live_benchmark.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _wait_ready(base_url: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"{base_url}/api/v1/health/ready"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"PAXS benchmark server did not become ready at {url} within {timeout_s}s")


def _cleanup_soak_rows(study_uids: list[str]) -> dict[str, int]:
    """Delete only the synthetic SOAK-* studies created during this benchmark."""
    if not study_uids:
        return {"studies": 0, "instances": 0}
    from sqlalchemy import delete, select

    from app.db.models import AuditLog, CompressionJob, Instance, Patient, Series, Study
    from app.db.session import SyncSessionLocal

    with SyncSessionLocal() as session:
        study_ids = list(
            session.execute(
                select(Study.id).where(Study.study_instance_uid.in_(study_uids))
            ).scalars().all()
        )
        patient_ids = list(
            session.execute(
                select(Study.patient_id).where(Study.id.in_(study_ids))
            ).scalars().all()
        ) if study_ids else []
        series_ids = list(
            session.execute(
                select(Series.id).where(Series.study_id.in_(study_ids))
            ).scalars().all()
        ) if study_ids else []
        inst_ids = list(
            session.execute(
                select(Instance.id).where(Instance.series_id.in_(series_ids))
            ).scalars().all()
        ) if series_ids else []
        if inst_ids:
            session.execute(delete(CompressionJob).where(CompressionJob.instance_id.in_(inst_ids)))
            session.execute(delete(Instance).where(Instance.id.in_(inst_ids)))
        if series_ids:
            session.execute(delete(Series).where(Series.id.in_(series_ids)))
        if study_ids:
            session.execute(delete(AuditLog).where(AuditLog.study_instance_uid.in_(study_uids)))
            session.execute(delete(Study).where(Study.id.in_(study_ids)))
        if patient_ids:
            session.execute(
                delete(Patient).where(
                    Patient.id.in_(patient_ids), Patient.patient_id.like("SOAK-%")
                )
            )
        session.commit()
    return {"studies": len(study_ids), "instances": len(inst_ids)}


def _run_scenario(
    python_exe: str,
    name: str,
    *,
    base_url: str,
    dicom_port: int,
    engine: str,
    scus: int,
    slices: int,
    pixel_dim: int,
    http_workers: int,
    duration: float,
) -> dict[str, Any]:
    cmd = [
        python_exe,
        str(REPO_ROOT / "tools" / "loadtest" / "soak_test.py"),
        "--base-url", base_url,
        "--dicom-host", "127.0.0.1",
        "--dicom-port", str(dicom_port),
        "--called-ae", "PAXS_BENCH",
        "--dimse-engine", engine,
        "--dimse-scus", str(scus),
        "--slices-per-scu", str(slices),
        "--pixel-dim", str(pixel_dim),
        "--http-workers", str(http_workers),
        "--duration", str(duration),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    data = json.loads(proc.stdout)
    data["scenario"] = name
    data["exit_code"] = proc.returncode
    return data


def main() -> int:
    http_port = 18080
    dicom_port = 11400
    base_url = f"http://127.0.0.1:{http_port}"
    python_exe = sys.executable
    temp_root = Path(tempfile.mkdtemp(prefix="paxs-bench-storage-"))
    env = os.environ.copy()
    env.update(
        {
            "ENVIRONMENT": "development",
            "STORAGE_BACKEND": "local",
            "STORAGE_ROOT": str(temp_root),
            "DICOM_HOST": "127.0.0.1",
            "DICOM_PORT": str(dicom_port),
            "DICOM_AE_TITLE": "PAXS_BENCH",
            "DICOM_RECEIVER_ENABLED": "true",
            "DICOM_MAX_PDU_SIZE": "131072",
            "WORKER_ENABLED": "true",
            "WORKER_COMPRESSION_CONCURRENCY": "2",
            "HL7_ENABLED": "false",
            "REDIS_CACHE_ENABLED": "false",
            "RATE_LIMIT_ENABLED": "false",
            "METRICS_EVENT_LOOP_SAMPLE_SECONDS": "0.25",
        }
    )

    log_file = temp_root / "benchmark_server.log"
    server_log_fh = open(log_file, "w", encoding="utf-8")
    server = subprocess.Popen(
        [
            python_exe, "-m", "uvicorn", "app.main.app:app",
            "--host", "127.0.0.1", "--port", str(http_port), "--log-level", "info",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=server_log_fh,
        stderr=subprocess.STDOUT,
        text=True,
    )
    all_study_uids: list[str] = []
    report: dict[str, Any] = {"scenarios": [], "cleanup": {}}
    try:
        _wait_ready(base_url, timeout_s=35.0)
        # Wait up to 5s for the DIMSE SCP thread to bind 127.0.0.1:dicom_port.
        import socket
        for _ in range(25):
            try:
                with socket.create_connection(("127.0.0.1", dicom_port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)

        sc_dcmtk = _run_scenario(
            python_exe,
            "A_dcmtk_3_7_1_parallel_dcmsend_plus_http",
            base_url=base_url,
            dicom_port=dicom_port,
            engine="dcmtk",
            scus=4,
            slices=50,
            pixel_dim=256,
            http_workers=8,
            duration=12.0,
        )
        all_study_uids.extend(sc_dcmtk.get("dimse_study_uids", []))
        report["scenarios"].append(sc_dcmtk)

        sc_pynet = _run_scenario(
            python_exe,
            "B_pynetdicom_128k_pdu_sustained_plus_http",
            base_url=base_url,
            dicom_port=dicom_port,
            engine="pynetdicom",
            scus=4,
            slices=75,
            pixel_dim=128,
            http_workers=12,
            duration=12.0,
        )
        all_study_uids.extend(sc_pynet.get("dimse_study_uids", []))
        report["scenarios"].append(sc_pynet)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        server_log_fh.close()
        if not all(s.get("passed") for s in report["scenarios"]) and log_file.is_file():
            tail = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            report["server_log_tail"] = tail
        report["cleanup"] = _cleanup_soak_rows(all_study_uids)
        shutil.rmtree(temp_root, ignore_errors=True)

    report["all_passed"] = all(s.get("passed") for s in report["scenarios"])
    out_path = REPO_ROOT / "SOAK_BENCHMARK_REPORT_2026-11-24.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
