"""PaxsViewer Control Panel — web bridge service.

Serves the control panel's ``window.electronApi`` contract over HTTP. The
control panel (a React renderer from ``D:\\Github\\control-panel-electron``,
mounted at ``/admin``) monitors the Viewers/Orthanc stack:

* Orthanc DICOM server          (default port 8042)
* Viewer Python proxy           (default port 3000)
* Admin Node API                (default port 4000)
* Admin frontend (Vite)         (default port 5173)
* MySQL (Windows service / systemd unit)

The Electron original performed the checks and process control in its main
process; this module mirrors that behaviour on the PAXS server so the same UI
works in a plain browser. Health checks are real (TCP connects / ``sc query``),
start/stop/restart are best-effort and only ever touch processes PAXS itself
spawned (or configured local installs), and the config is persisted to a JSON
file beside the archive data.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from app.core.config import settings

log = logging.getLogger("paxs.admin_panel")

SERVICE_NAMES = {
    "orthanc": "Orthanc DICOM Server",
    "proxy": "Viewer Proxy",
    "api": "Admin API Server",
    "mysql": "MySQL Database",
    "adminFront": "Admin Frontend",
}

# Services the panel exposes start/stop/restart for (MySQL is managed by the
# OS service manager and only health-checked).
CONTROLLABLE = ("orthanc", "proxy", "api", "adminFront")

DEFAULT_CONFIG: dict = {
    "orthancPort": 8042,
    "proxyPort": 3000,
    "apiPort": 4000,
    "adminFrontPort": 5173,
    "mysqlServiceName": "MySQL80",
    "orthancPath": "../orthanc/Orthanc.exe",
    "apiDir": "../Viewers/platform/server/",
    "proxyDir": "../Viewers/platform/app/dist-orthanc/",
    "adminFrontDir": "../Viewers/admin/",
    "monitorIntervalMs": 5000,
}

_PORT_KEYS = ("orthancPort", "proxyPort", "apiPort", "adminFrontPort")


class AdminPanelService:
    """Thread-safe singleton holding config, activity log, and spawned PIDs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._logs: deque[str] = deque(maxlen=500)
        self._pids: dict[str, int] = {}
        self._config: dict = dict(DEFAULT_CONFIG)
        self._load_config()
        self._append_log(
            f"Control Panel (web) ready — monitoring at "
            f"Orthanc :{self._config['orthancPort']}, proxy :{self._config['proxyPort']}, "
            f"API :{self._config['apiPort']}, admin :{self._config['adminFrontPort']}"
        )

    # ------------------------------------------------------------------ #
    # Config persistence
    # ------------------------------------------------------------------ #
    @property
    def config_path(self) -> Path:
        if settings.ADMIN_PANEL_CONFIG_FILE:
            return Path(settings.ADMIN_PANEL_CONFIG_FILE)
        return Path(settings.STORAGE_ROOT).parent / "admin_panel.json"

    def _load_config(self) -> None:
        try:
            path = self.config_path
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                merged = dict(DEFAULT_CONFIG)
                merged.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
                self._config = self._validate(merged)
        except Exception:  # noqa: BLE001 - a broken config must never kill the panel
            log.warning("admin_panel config unreadable, using defaults", exc_info=True)

    def _validate(self, cfg: dict) -> dict:
        out = dict(cfg)
        for key in _PORT_KEYS:
            try:
                out[key] = int(out[key])
            except (TypeError, ValueError):
                out[key] = DEFAULT_CONFIG[key]
            out[key] = min(65535, max(1, out[key]))
        try:
            out["monitorIntervalMs"] = min(60000, max(1000, int(out["monitorIntervalMs"])))
        except (TypeError, ValueError):
            out["monitorIntervalMs"] = 5000
        for key in ("mysqlServiceName", "orthancPath", "apiDir", "proxyDir", "adminFrontDir"):
            out[key] = str(out.get(key) or DEFAULT_CONFIG[key])
        return out

    @property
    def config(self) -> dict:
        with self._lock:
            return dict(self._config)

    def save_config(self, cfg: dict) -> dict:
        validated = self._validate({**self._config, **(cfg or {})})
        try:
            path = self.config_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(validated, indent=2), encoding="utf-8")
        except OSError as exc:
            self._append_log(f"Config save failed: {exc}")
            return {"ok": False, "error": str(exc)}
        with self._lock:
            self._config = validated
        self._append_log("Configuration saved — monitoring uses the new settings")
        return {"ok": True, "config": validated}

    # ------------------------------------------------------------------ #
    # Activity log
    # ------------------------------------------------------------------ #
    @property
    def logs(self) -> list[str]:
        with self._lock:
            return list(self._logs)

    def _append_log(self, message: str) -> None:
        stamp = time.strftime("[%H:%M:%S]")
        with self._lock:
            self._logs.append(f"{stamp} {message}")

    # ------------------------------------------------------------------ #
    # Health checks
    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_port(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            try:
                sock.connect((host, port))
            except OSError:
                return "stopped"
        return "running"

    def _check_mysql(self, service_name: str) -> str:
        """Windows: ``sc query`` on the service; otherwise systemctl/pgrep."""
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["sc", "query", service_name],
                    capture_output=True, text=True, timeout=4,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return "running" if result.returncode == 0 else "stopped"
            for probe in (
                ["systemctl", "is-active", "--quiet", service_name],
                ["systemctl", "is-active", "--quiet", "mysqld"],
                ["pgrep", "-x", "mysqld"],
            ):
                try:
                    result = subprocess.run(probe, capture_output=True, timeout=4)
                    if result.returncode == 0:
                        return "running"
                except OSError:
                    continue
            return "stopped"
        except Exception:  # noqa: BLE001
            return "error"

    def snapshot(self) -> dict:
        cfg = self.config
        checks = {
            "orthanc": self._check_port(cfg["orthancPort"]),
            "proxy": self._check_port(cfg["proxyPort"]),
            "api": self._check_port(cfg["apiPort"]),
            "adminFront": self._check_port(cfg["adminFrontPort"]),
            "mysql": self._check_mysql(cfg["mysqlServiceName"]),
        }
        return {**checks, "lastChecked": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # ------------------------------------------------------------------ #
    # Process control (best-effort, only services PAXS can reach)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve(path: str) -> Path | None:
        """Resolve a configured path: absolute, cwd-relative, then D:\\Github."""
        candidates: list[Path] = []
        p = Path(path)
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(Path.cwd() / p)
            github = Path("D:/Github")
            if github.exists():
                candidates.append(github / p)
        for c in candidates:
            if c.exists():
                return c
        return None

    def _spawn(self, service: str, argv: list[str], cwd: Path) -> bool:
        try:
            kwargs: dict = {}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                argv, cwd=str(cwd), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **kwargs,
            )
            with self._lock:
                self._pids[service] = proc.pid or 0
            return True
        except OSError as exc:
            self._append_log(f"{SERVICE_NAMES[service]} failed to start: {exc}")
            return False

    def _wait_port(self, port: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._check_port(port) == "running":
                return True
            time.sleep(0.5)
        return False

    def start_service(self, service: str) -> dict:
        """Start one service (spawns the configured command). Returns change."""
        name = SERVICE_NAMES[service]
        if service not in CONTROLLABLE:
            return {"name": name, "status": "error"}
        cfg = self.config
        if self._check_port(cfg[f"{service}Port"]) == "running":
            self._append_log(f"✓ Running {name}")
            return {"name": name, "status": "running"}
        self._append_log(f"▶ Starting {name}")
        started = False
        if service == "orthanc":
            exe = self._resolve(cfg["orthancPath"])
            if exe:
                started = self._spawn(service, [str(exe)], exe.parent)
        elif service == "proxy":
            d = self._resolve(cfg["proxyDir"])
            script = d / "serve_orthanc.py" if d else None
            if script and script.exists():
                started = self._spawn(service, ["python", str(script)], d)
        elif service == "api":
            d = self._resolve(cfg["apiDir"])
            if d:
                node = shutil.which("node")
                if node:
                    dist = d / "dist" / "index.js"
                    target = dist if dist.exists() else d / "src" / "index.ts"
                    started = self._spawn(service, [node, str(target)], d)
        elif service == "adminFront":
            d = self._resolve(cfg["adminFrontDir"])
            if d and (d / "package.json").exists():
                started = self._spawn(service, ["npm", "run", "dev"], d)
        if not started:
            self._append_log(f"✗ Error {name} (target not found/startable — check Settings)")
            return {"name": name, "status": "error"}
        ok = self._wait_port(cfg[f"{service}Port"], timeout=30 if service == "orthanc" else 20)
        self._append_log(f"{'✓ Running' if ok else '✗ Error'} {name}")
        return {"name": name, "status": "running" if ok else "error"}

    def stop_service(self, service: str) -> dict:
        """Stop a service. Only stops processes PAXS spawned this session."""
        name = SERVICE_NAMES[service]
        if service not in CONTROLLABLE:
            return {"name": name, "status": "error"}
        with self._lock:
            pid = self._pids.pop(service, None)
        if not pid:
            status = self._check_port(self.config[f"{service}Port"])
            if status == "running":
                self._append_log(f"○ {name} is running but not managed by this session — stop it via task manager")
                return {"name": name, "status": "running"}
            self._append_log(f"○ Stopped {name}")
            return {"name": name, "status": "stopped"}
        self._append_log(f"■ Stopping {name}")
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:  # noqa: BLE001
            pass
        self._append_log(f"○ Stopped {name}")
        return {"name": name, "status": "stopped"}

    def restart_service(self, service: str) -> dict:
        self._append_log(f"⟳ Restarting {SERVICE_NAMES[service]}...")
        self.stop_service(service)
        time.sleep(1.5)
        return self.start_service(service)

    def start_all(self) -> dict:
        self._append_log("━━━ Starting all services... ━━━")
        changes = [self.start_service(s) for s in CONTROLLABLE]
        ok = all(c["status"] == "running" for c in changes)
        self._append_log("━━━ All services started ━━━" if ok else "━━━ Some services failed ━━━")
        return {"ok": ok, "changes": changes}

    def stop_all(self) -> dict:
        self._append_log("━━━ Stopping all services... ━━━")
        changes = [self.stop_service(s) for s in reversed(CONTROLLABLE)]
        self._append_log("━━━ All services stopped ━━━")
        return {"ok": True, "changes": changes}

    # ------------------------------------------------------------------ #
    # Launch helpers
    # ------------------------------------------------------------------ #
    def launch(self, target: str) -> dict:
        """open-viewer / open-admin: open the PAXS-hosted UI in a browser tab."""
        url_map = {"viewer": f"{settings.PUBLIC_BASE_URL}/viewer/", "admin": f"{settings.PUBLIC_BASE_URL}/admin/"}
        url = url_map.get(target)
        if not url:
            return {"ok": False, "error": f"unknown target {target!r}"}
        if not os.environ.get("PAXS_DISABLE_BROWSER"):
            try:
                webbrowser.open(url, new=2)
            except Exception as exc:  # noqa: BLE001
                log.warning("webbrowser.open failed: %s", exc)
        self._append_log(f"Opened {target} → {url}")
        return {"ok": True, "url": url}


_service: AdminPanelService | None = None
_service_lock = threading.Lock()


def get_admin_panel_service() -> AdminPanelService:
    global _service
    with _service_lock:
        if _service is None:
            _service = AdminPanelService()
        return _service
