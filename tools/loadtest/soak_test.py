"""Heavy-work soak and stress test harness for PAXS Archive.

Drives concurrent DIMSE C-STORE bursts and HTTP API / DICOMweb requests while
sampling Prometheus ``/metrics`` (event-loop lag, RSS memory, DB pool usage,
blocking-I/O rejections) and evaluating pass/fail SLOs.

Usage::

    venv\\Scripts\\python.exe tools/loadtest/soak_test.py --dry-run
    venv\\Scripts\\python.exe tools/loadtest/soak_test.py \\
        --base-url http://127.0.0.1:8000 \\
        --dicom-host 127.0.0.1 --dicom-port 10400 \\
        --dimse-scus 4 --slices-per-scu 25 \\
        --http-workers 8 --duration 30
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid


@dataclass
class SoakResult:
    http_requests: int = 0
    http_rps: float = 0.0
    http_5xx: int = 0
    http_503: int = 0
    http_p50_ms: float = 0.0
    http_p95_ms: float = 0.0
    http_p99_ms: float = 0.0
    dimse_engine_used: str = "pynetdicom"
    dimse_sent: int = 0
    dimse_failed: int = 0
    dimse_slices_per_sec: float = 0.0
    dimse_study_uids: list[str] = field(default_factory=list)
    max_event_loop_lag_s: float = 0.0
    max_rss_mb: float = 0.0
    max_db_pool_checked_out: int = 0
    blocking_io_rejected_delta: int = 0
    slo_violations: list[str] = field(default_factory=list)


def parse_prometheus_metrics(text: str) -> dict[str, float]:
    """Extract numeric scalar gauge/counter values from Prometheus exposition text."""
    parsed: dict[str, float] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "{" in stripped:
            continue
        parts = stripped.split()
        if len(parts) != 2:
            continue
        try:
            parsed[parts[0]] = float(parts[1])
        except ValueError:
            continue
    return parsed


def percentile(values: list[float], pct: float) -> float:
    """Return the ``pct``-th percentile (0..100) of ``values``."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[idx]


def evaluate_slos(
    result: SoakResult,
    *,
    max_lag_s: float = 0.25,
    max_5xx_rate: float = 0.01,
    max_dimse_fail_rate: float = 0.01,
) -> list[str]:
    """Return human-readable SLO violation descriptions (empty = passed)."""
    violations: list[str] = []
    if result.max_event_loop_lag_s > max_lag_s:
        violations.append(
            f"event_loop_lag {result.max_event_loop_lag_s:.3f}s > {max_lag_s:.3f}s"
        )
    if result.http_requests > 0:
        rate_5xx = result.http_5xx / result.http_requests
        if rate_5xx > max_5xx_rate:
            violations.append(f"http_5xx_rate {rate_5xx:.2%} > {max_5xx_rate:.2%}")
    total_dimse = result.dimse_sent + result.dimse_failed
    if total_dimse > 0:
        fail_rate = result.dimse_failed / total_dimse
        if fail_rate > max_dimse_fail_rate:
            violations.append(f"dimse_fail_rate {fail_rate:.2%} > {max_dimse_fail_rate:.2%}")
    return violations


def _build_synthetic_slice(
    study_uid: str, series_uid: str, index: int, pixel_dim: int = 64
) -> FileDataset:
    sop_uid = generate_uid()
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("soak.dcm", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID = f"SOAK-{study_uid[-8:]}"
    ds.PatientName = f"SOAK^TEST^{index}"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.Modality = "OT"
    ds.InstanceNumber = index
    ds.Rows = pixel_dim
    ds.Columns = pixel_dim
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = bytes([index % 256]) * (pixel_dim * pixel_dim)
    # Match TransferSyntaxUID=ExplicitVRLittleEndian so pynetdicom & dcmsend encode cleanly.
    ds.set_original_encoding(False, True)
    return ds


def _resolve_dcmsend() -> Path | None:
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        Path(os.environ.get("DCMTK_BIN_DIR", "")) / "dcmsend.exe",
        repo_root / ".tools" / "dcmtk" / "bin" / "dcmsend.exe",
        repo_root / ".tools" / "dcmtk" / "bin" / "dcmsend",
    ]
    for path in candidates:
        if path.is_file():
            return path
    found = shutil.which("dcmsend")
    return Path(found) if found else None


def _run_dimse_scu_pynetdicom(
    host: str, port: int, called_ae: str, calling_ae: str, slices: int, pixel_dim: int
) -> tuple[int, int, str]:
    from pydicom.uid import ImplicitVRLittleEndian
    from pynetdicom import AE

    ae = AE(ae_title=calling_ae)
    ae.maximum_pdu_size = 131072
    ae.add_requested_context(
        SecondaryCaptureImageStorage, [ExplicitVRLittleEndian, ImplicitVRLittleEndian]
    )
    assoc = ae.associate(host, port, ae_title=called_ae)
    study_uid, series_uid = generate_uid(), generate_uid()
    if not assoc.is_established:
        return 0, slices, study_uid
    sent = failed = 0
    try:
        for idx in range(1, slices + 1):
            status = assoc.send_c_store(
                _build_synthetic_slice(study_uid, series_uid, idx, pixel_dim=pixel_dim)
            )
            if status is not None and getattr(status, "Status", None) == 0x0000:
                sent += 1
            else:
                failed += 1
    finally:
        assoc.release()
    return sent, failed, study_uid


def _run_dimse_scu_dcmtk(
    dcmsend_exe: Path, host: str, port: int, called_ae: str, calling_ae: str, slices: int, pixel_dim: int
) -> tuple[int, int, str]:
    study_uid, series_uid = generate_uid(), generate_uid()
    with tempfile.TemporaryDirectory(prefix="paxs-soak-dcmtk-") as tmp:
        files: list[str] = []
        for idx in range(1, slices + 1):
            out = Path(tmp) / f"slice_{idx:04d}.dcm"
            ds = _build_synthetic_slice(study_uid, series_uid, idx, pixel_dim=pixel_dim)
            ds.save_as(str(out), enforce_file_format=True)
            files.append(str(out))
        env = os.environ.copy()
        for dic in dcmsend_exe.parents[1].glob("share/dcmtk*/dicom.dic"):
            env.setdefault("DCMDICTPATH", str(dic))
            break
        cmd = [
            str(dcmsend_exe),
            "-aet", calling_ae,
            "-aec", called_ae,
            "--max-pdu", "131072",
            host, str(port),
            *files,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env, check=False)
        if proc.returncode == 0:
            return slices, 0, study_uid
        return 0, slices, study_uid


def _http_get(url: str, token: str | None) -> tuple[int, float]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
            return int(resp.status), (time.perf_counter() - started) * 1000.0
    except urllib.error.HTTPError as exc:
        return int(exc.code), (time.perf_counter() - started) * 1000.0
    except OSError:
        return 599, (time.perf_counter() - started) * 1000.0


def _sample_metrics(base_url: str, token: str | None) -> dict[str, float]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(f"{base_url.rstrip('/')}/metrics", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return parse_prometheus_metrics(resp.read().decode("utf-8", errors="replace"))
    except OSError:
        return {}


def _run_http_load(
    base_url: str, workers: int, duration_s: float, token: str | None
) -> tuple[list[float], int, int]:
    deadline = time.monotonic() + duration_s
    urls = [f"{base_url.rstrip('/')}/api/v1/health/live", f"{base_url.rstrip('/')}/api/v1/health/ready"]
    if token:
        urls.append(f"{base_url.rstrip('/')}/dicom-web/studies?limit=20")

    def _worker(seed: int) -> tuple[list[float], int, int]:
        lat: list[float] = []
        c5xx = c503 = 0
        idx = seed
        while time.monotonic() < deadline:
            code, ms = _http_get(urls[idx % len(urls)], token)
            lat.append(ms)
            if code >= 500:
                c5xx += 1
            if code == 503:
                c503 += 1
            idx += 1
        return lat, c5xx, c503

    all_lat: list[float] = []
    total_5xx = total_503 = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for lat, c5xx, c503 in pool.map(_worker, range(workers)):
            all_lat.extend(lat)
            total_5xx += c5xx
            total_503 += c503
    return all_lat, total_5xx, total_503


def run_soak(args: argparse.Namespace) -> SoakResult:
    """Execute the configured HTTP + DIMSE soak workload and evaluate SLOs."""
    result = SoakResult()
    if args.dry_run:
        sample = _build_synthetic_slice(generate_uid(), generate_uid(), 1)
        assert sample.Rows == 64 and len(sample.PixelData) == 4096
        result.slo_violations = evaluate_slos(result)
        return result

    engine_pref = getattr(args, "dimse_engine", "auto")
    pixel_dim = int(getattr(args, "pixel_dim", 64))
    dcmsend_exe = _resolve_dcmsend() if engine_pref in ("auto", "dcmtk") else None
    use_dcmtk = dcmsend_exe is not None and engine_pref != "pynetdicom"
    result.dimse_engine_used = "dcmtk" if use_dcmtk else "pynetdicom"

    before = _sample_metrics(args.base_url, args.token)
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.dimse_scus) + 2) as pool:
        http_fut = pool.submit(_run_http_load, args.base_url, args.http_workers, args.duration, args.token)
        dimse_futs = [
            pool.submit(
                _run_dimse_scu_dcmtk, dcmsend_exe, args.dicom_host, args.dicom_port,
                args.called_ae, f"SOAK_SCU_{i}", args.slices_per_scu, pixel_dim,
            )
            if use_dcmtk
            else pool.submit(
                _run_dimse_scu_pynetdicom, args.dicom_host, args.dicom_port,
                args.called_ae, f"SOAK_SCU_{i}", args.slices_per_scu, pixel_dim,
            )
            for i in range(args.dimse_scus)
        ]
        while not http_fut.done() or any(not f.done() for f in dimse_futs):
            snap = _sample_metrics(args.base_url, args.token)
            result.max_event_loop_lag_s = max(
                result.max_event_loop_lag_s, snap.get("paxs_event_loop_lag_seconds", 0.0)
            )
            result.max_rss_mb = max(
                result.max_rss_mb, snap.get("paxs_process_resident_memory_bytes", 0.0) / (1024 * 1024)
            )
            result.max_db_pool_checked_out = max(
                result.max_db_pool_checked_out, int(snap.get("paxs_db_pool_checked_out", 0.0))
            )
            time.sleep(0.5)
        latencies, c5xx, c503 = http_fut.result()
        for fut in dimse_futs:
            sent, failed, study_uid = fut.result()
            result.dimse_sent += sent
            result.dimse_failed += failed
            if sent > 0:
                result.dimse_study_uids.append(study_uid)

    elapsed = max(0.001, time.perf_counter() - started)
    after = _sample_metrics(args.base_url, args.token)
    result.http_requests = len(latencies)
    result.http_rps = round(len(latencies) / elapsed, 1)
    result.http_5xx = c5xx
    result.http_503 = c503
    result.http_p50_ms = round(percentile(latencies, 50.0), 2)
    result.http_p95_ms = round(percentile(latencies, 95.0), 2)
    result.http_p99_ms = round(percentile(latencies, 99.0), 2)
    result.dimse_slices_per_sec = round(result.dimse_sent / elapsed, 2)
    result.blocking_io_rejected_delta = int(
        after.get("paxs_blocking_io_rejected_total", 0.0)
        - before.get("paxs_blocking_io_rejected_total", 0.0)
    )
    result.slo_violations = evaluate_slos(result, max_lag_s=args.max_lag_s)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PAXS Archive heavy-work soak test harness.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=None, help="Optional Bearer JWT for authenticated routes")
    parser.add_argument("--dicom-host", default="127.0.0.1")
    parser.add_argument("--dicom-port", type=int, default=10400)
    parser.add_argument("--called-ae", default="PAXS_SCP")
    parser.add_argument("--dimse-engine", choices=("auto", "dcmtk", "pynetdicom"), default="auto")
    parser.add_argument("--dimse-scus", type=int, default=4)
    parser.add_argument("--slices-per-scu", type=int, default=20)
    parser.add_argument("--pixel-dim", type=int, default=64)
    parser.add_argument("--http-workers", type=int, default=8)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--max-lag-s", type=float, default=0.25)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Self-check payload generation without network I/O")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_soak(args)
    payload: dict[str, Any] = asdict(result)
    payload["passed"] = not result.slo_violations
    text = json.dumps(payload, indent=2)
    print(text)
    if args.json_out is not None:
        args.json_out.write_text(text + "\n", encoding="utf-8")
    return 0 if not result.slo_violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
