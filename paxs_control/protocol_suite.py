"""Run the repository's DICOM tests in an isolated synthetic environment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

PROTOCOLS = ("C-ECHO", "C-STORE", "C-FIND", "C-GET", "C-MOVE", "MWL", "MPPS", "Storage Commitment")


def protocol_suite_summary(results: dict[str, str], exit_code: int) -> str:
    """Require a successful run and an explicit pass for every protocol."""
    if exit_code != 0 or any(value in {"FAIL", "ERROR"} for value in results.values()):
        return "FAILED"
    return "ALL OK" if all(results.get(name) == "PASS" for name in PROTOCOLS) else "INCOMPLETE"


def run_synthetic_protocol_suite(package: Path) -> tuple[str, dict[str, str]]:
    """Execute isolated source tests; never use the configured archive database."""
    root = package.resolve().parent
    runner = root / "paxs_control" / "protocol_pytest_runner.py"
    python = shutil.which("python")
    if not python or not runner.is_file() or not (root / "tests" / "conftest.py").is_file():
        return "UNAVAILABLE", {}
    with tempfile.TemporaryDirectory(prefix="paxs-control-dicom-") as temporary:
        temporary_path = Path(temporary)
        result_path = temporary_path / "result.json"
        allowed = ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "USERPROFILE", "LOCALAPPDATA")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env.update({"PYTHONPATH": str(root), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "TEMP": temporary, "TMP": temporary, "TMPDIR": temporary,
                    "STORAGE_BACKEND": "local", "REDIS_CACHE_ENABLED": "false",
                    "REDIS_URL": "redis://127.0.0.1:1/0", "WEBHOOK_URLS": "",
                    "WARM_ON_OPEN_ENABLED": "false", "DICOM_TLS_ENABLED": "false"})
        try:
            completed = subprocess.run(
                [python, str(runner), str(root), str(result_path)], cwd=temporary,
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=180, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if not result_path.is_file() or result_path.stat().st_size > 16384:
                return "FAILED", {}
            results = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(results, dict) or any(
                name not in PROTOCOLS or value not in {"PASS", "FAIL", "SKIP", "NOT RUN"}
                for name, value in results.items()
            ):
                return "FAILED", {}
            return protocol_suite_summary(results, completed.returncode), results
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return "FAILED", {}
