"""PAXS Archive desktop app — the archive server plus a native window.

``PAXS-Archive.exe`` (built from this file) is the double-click way to run the
archive on a workstation:

* starts the PAXS Archive server (API, web UI, DICOM SCP) in the background —
  or attaches to one that is already running on the configured port;
* shows the UI in a native window (Microsoft Edge WebView2, built into
  Windows 10/11) that is **resizable**, opens **maximized**, and toggles
  **full screen with F11** (Esc leaves full screen; the viewer's own
  full-screen button works too);
* closing the window stops the server it started.

Options::

    PAXS-Archive.exe --fullscreen      start in full screen
    PAXS-Archive.exe --port 8000       override PAXS_PORT

The headless server (Windows service, health monitor) is ``PAXS-Server.exe``,
built from ``run_paxs.py``. Configuration comes from the ``.env`` next to the
exe, exactly as for the server.
"""

from __future__ import annotations

import argparse
import ctypes
import multiprocessing
import os
import shutil
import socket
import sys
import threading
import time
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

APP_TITLE = "PAXS Archive"
READY_TIMEOUT_SECONDS = 180
STOP_TIMEOUT_SECONDS = max(10, int(os.getenv("PAXS_STOP_TIMEOUT_SECONDS", "30")))
DESKTOP_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB per file
DESKTOP_LOG_BACKUP_COUNT = 5

# F11 toggles full screen, Esc leaves it, and same-origin target="_blank" links
# stay inside the desktop window instead of escaping to an external browser.
# Injected on every page load (the guard flag lives on the page's window).
_KEYS_JS = """
if (!window.__paxsDesktopKeys) {
  window.__paxsDesktopKeys = true;
  document.addEventListener('keydown', function (e) {
    if (!window.pywebview || !window.pywebview.api) return;
    if (e.key === 'F11') {
      e.preventDefault();
      window.pywebview.api.toggle_fullscreen();
    } else if (e.key === 'Escape') {
      window.pywebview.api.leave_fullscreen();
    }
  }, true);
  document.addEventListener('click', function (e) {
    if (!window.pywebview || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var anchor = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!anchor || anchor.hasAttribute('download')) return;
    if ((anchor.getAttribute('target') || '').toLowerCase() !== '_blank') return;
    try {
      var targetUrl = new URL(anchor.href, window.location.href);
      if (targetUrl.origin === window.location.origin && !/\\/print\\b/i.test(targetUrl.pathname)) {
        e.preventDefault();
        window.location.assign(targetUrl.href);
      }
    } catch (err) { /* leave default browser behavior for non-URL hrefs */ }
  }, true);
}
"""


# ----------------------------------------------------------------- basics ----
def app_dir() -> Path:
    """Folder holding .env, storage and logs: the exe's folder when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class _RotatingLogStream:
    """Thread-safe size-rotating file stream for windowed stdout/stderr."""

    def __init__(
        self,
        path: Path,
        max_bytes: int = DESKTOP_LOG_MAX_BYTES,
        backup_count: int = DESKTOP_LOG_BACKUP_COUNT,
    ) -> None:
        self._path = path
        self._max_bytes = max(1024, int(max_bytes))
        self._backup_count = max(1, int(backup_count))
        self._lock = threading.Lock()
        self._stream = self._open_stream()
        self.encoding = "utf-8"

    def _open_stream(self):
        return open(self._path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115

    def _rotate_if_needed(self) -> None:
        try:
            size = self._path.stat().st_size
        except OSError:
            size = 0
        if size < self._max_bytes:
            return
        self._stream.close()
        for idx in range(self._backup_count, 0, -1):
            src = self._path if idx == 1 else self._path.with_name(f"{self._path.name}.{idx - 1}")
            dst = self._path.with_name(f"{self._path.name}.{idx}")
            if src.exists():
                src.replace(dst)
        self._stream = self._open_stream()

    def write(self, data: str) -> int:
        if not data:
            return 0
        with self._lock:
            self._rotate_if_needed()
            written = self._stream.write(data)
            self._stream.flush()
            return written

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()

    def isatty(self) -> bool:
        return False


def redirect_stdio_when_windowed(base: Path) -> None:
    """A windowed exe has no console: send prints and logs to a rotating file
    instead of crashing on ``sys.stdout is None`` or growing logs unboundedly."""
    if not getattr(sys, "frozen", False) and sys.stdout is not None and sys.stderr is not None:
        return
    logs = base / "logs"
    logs.mkdir(exist_ok=True)
    stream = _RotatingLogStream(logs / "paxs-desktop.log")
    sys.stdout = sys.stderr = stream  # type: ignore[assignment]


def confirm_box(text: str) -> bool:
    """Show a Windows Yes/No confirmation dialog; returns True when user chooses Yes."""
    try:
        # MB_YESNO (0x04) | MB_ICONWARNING (0x30) | MB_DEFBUTTON2 (0x100); IDYES == 6
        return int(ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x134)) == 6
    except Exception:  # noqa: BLE001 - non-Windows / no GUI
        return True


def message_box(text: str, error: bool = True) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x10 if error else 0x40)
    except Exception:  # noqa: BLE001 - non-Windows / no GUI: the log has it
        print(text, file=sys.stderr)


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def is_paxs(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/api/v1/health/live", timeout=3) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - anything but a 200 means "not ours / not ready"
        return False


def wait_until_ready(base_url: str, server: "ServerThread | None") -> bool:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_paxs(base_url):
            return True
        if server is not None and not server.is_alive():
            return False
        time.sleep(0.5)
    return False


# ----------------------------------------------------------------- server ----
class ServerThread(threading.Thread):
    """uvicorn on a background thread (no reloader: a frozen app cannot reload)."""

    def __init__(self, host: str, port: int, ssl_kw: dict, forwarded_allow_ips: str, root_path: str):
        super().__init__(name="paxs-server", daemon=True)
        import uvicorn

        from app.main import app

        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            reload=False,
            proxy_headers=True,
            forwarded_allow_ips=forwarded_allow_ips,
            root_path=root_path,
            **ssl_kw,
        )
        self.server = uvicorn.Server(config)
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.server.run()
        except BaseException as exc:  # noqa: BLE001 - reported to the user by main()
            self.error = exc

    def stop(self) -> None:
        self.server.should_exit = True
        self.join(STOP_TIMEOUT_SECONDS)


# ----------------------------------------------------------------- window ----
class WindowApi:
    """Methods the page can call through ``window.pywebview.api``."""

    def __init__(self, base_url: str = "") -> None:
        self._window = None
        self._fullscreen = False
        self._restore_maximized = True  # the window opens maximized
        self._base_url = base_url.rstrip("/")
        self._local_jobs: dict[str, dict[str, Any]] = {}
        self._jobs_lock = threading.Lock()

    def bind(self, window: Any, fullscreen: bool) -> None:
        self._window = window
        self._fullscreen = fullscreen

    def _is_maximized(self) -> bool:
        try:
            hwnd = int(self._window.native.Handle.ToInt64())
            return bool(ctypes.windll.user32.IsZoomed(hwnd))
        except Exception:  # noqa: BLE001 - non-WinForms backend: keep the last known state
            return self._restore_maximized

    def toggle_fullscreen(self) -> bool:
        if self._window is None:
            return self._fullscreen
        print(f"[PAXS] toggle_fullscreen (currently fullscreen={self._fullscreen})")
        if not self._fullscreen:
            self._restore_maximized = self._is_maximized()
        self._window.toggle_fullscreen()
        self._fullscreen = not self._fullscreen
        if not self._fullscreen and self._restore_maximized:
            self._window.maximize()
        return self._fullscreen

    def leave_fullscreen(self) -> bool:
        if self._fullscreen:
            return not self.toggle_fullscreen()
        return False

    # ---- Client-device removable media bridge (CD/DVD + USB) ----------------
    def list_local_media(self) -> dict[str, Any]:
        """Detect CD/DVD recorders and removable USB drives on this client PC."""
        from app.services import disc_burn_service

        return {
            "desktop": True,
            **disc_burn_service.list_removable_devices(),
        }

    def check_local_disc(self, drive: str = "") -> dict[str, Any]:
        """Check blank CD/DVD media readiness on a local optical drive."""
        from app.services import disc_burn_service

        try:
            info = disc_burn_service.check_media(drive or "")
            return {"ok": True, **info}
        except disc_burn_service.DiscBurnError as exc:
            return {"ok": False, "error": str(exc)}

    def pick_local_folder(self) -> dict[str, Any]:
        """Open a native Windows folder chooser for USB / local folder export."""
        if self._window is None:
            return {"ok": False, "path": None}
        import webview

        dialog_type = getattr(getattr(webview, "FileDialog", None), "FOLDER", webview.FOLDER_DIALOG)
        chosen = self._window.create_file_dialog(dialog_type)
        if not chosen:
            return {"ok": False, "path": None}
        folder = chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen)
        return {"ok": bool(folder), "path": str(folder)}

    def open_local_path(self, path: str) -> dict[str, Any]:
        """Open an exported study folder in Windows Explorer."""
        target = Path(path)
        if not target.exists():
            return {"ok": False, "error": "Folder does not exist."}
        try:
            if os.name == "nt":
                os.startfile(str(target))  # noqa: S606 - user-initiated explorer open
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def _download_study_zip(
        self, study_uid: str, destination: Path, origin_url: str, token: str, cookie: str,
    ) -> None:
        base = (origin_url or self._base_url).rstrip("/")
        url = f"{base}/api/v1/export/dicomdir/client/{urllib.parse.quote(study_uid, safe='')}/zip"
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, headers=headers)
        ctx = ssl._create_unverified_context() if url.startswith("https://127.0.0.1") or url.startswith("https://localhost") else None
        try:
            with urllib.request.urlopen(req, timeout=600, context=ctx) as resp:
                declared = int(resp.headers.get("Content-Length") or 0)
                if declared > 0 and shutil.disk_usage(destination.parent).free < declared:
                    raise RuntimeError(
                        "Not enough free temporary disk space on this workstation to download the study."
                    )
                with destination.open("wb") as out:
                    while chunk := resp.read(1024 * 1024):
                        out.write(chunk)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Could not download study package from archive (HTTP {exc.code}).") from None
        except OSError:
            raise RuntimeError("Could not reach the archive server to download the study.") from None

    def _is_local_loopback_origin(self, origin_url: str) -> bool:
        target = (origin_url or self._base_url).strip()
        if not target:
            return False
        host = (urllib.parse.urlsplit(target).hostname or "").lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    def _verify_study_access_http(
        self, study_uid: str, origin_url: str, token: str, cookie: str
    ) -> bool:
        base = (origin_url or self._base_url).rstrip("/")
        url = f"{base}/dicomweb/studies?StudyInstanceUID={urllib.parse.quote(study_uid, safe='')}&limit=1"
        headers: dict[str, str] = {"Accept": "application/dicom+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, headers=headers)
        ctx = ssl._create_unverified_context() if url.startswith("https://127.0.0.1") or url.startswith("https://localhost") else None
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                return resp.status == 200 and len(resp.read()) > 2
        except (urllib.error.HTTPError, OSError):
            return False

    def _try_direct_local_export(
        self, study_uid: str, mode: str, target: str, folder_name: str,
        volume_label: str, origin_url: str, token: str, cookie: str, set_phase,
    ) -> str | None:
        """Same-machine fast path: stage directly from local storage without HTTP ZIP round-trip."""
        if not self._is_local_loopback_origin(origin_url):
            return None
        from app.core.config import settings
        if (settings.STORAGE_BACKEND or "local").lower() != "local":
            return None
        if not self._verify_study_access_http(study_uid, origin_url, token, cookie):
            return None
        from sqlalchemy import select
        from app.db.models import Instance, Series, Study
        from app.db.session import SyncSessionLocal
        from app.services import disc_burn_service

        with SyncSessionLocal() as session:
            rows = session.execute(
                select(
                    Instance.sop_instance_uid, Series.series_instance_uid,
                    Instance.file_path, Instance.storage_backend, Instance.file_size_bytes,
                )
                .join(Series, Instance.series_id == Series.id)
                .join(Study, Series.study_id == Study.id)
                .where(Study.study_instance_uid == study_uid)
                .order_by(Instance.id)
            ).all()
        if not rows or any(row[3] is not None for row in rows):
            return None
        instances = [disc_burn_service.DiscInstance(*row) for row in rows]
        if mode == "disc":
            disc_burn_service.burn_study(
                target, study_uid, instances, set_phase,
                volume_label=(volume_label or "PAXS_DICOM")[:16],
            )
            return target.upper() + "\\"
        if mode == "usb":
            return disc_burn_service.export_study_to_usb(
                target, study_uid, instances, set_phase, folder_name=folder_name or study_uid,
            )
        return disc_burn_service.export_study_to_folder(
            Path(target), study_uid, instances, set_phase, folder_name=folder_name or study_uid,
        )

    def _run_local_media_job(
        self, job_id: str, study_uid: str, mode: str, target: str,
        folder_name: str, volume_label: str, origin_url: str, token: str, cookie: str,
    ) -> None:
        from app.services import disc_burn_service

        def set_phase(phase: str) -> None:
            with self._jobs_lock:
                if job_id in self._local_jobs:
                    self._local_jobs[job_id]["phase"] = phase

        try:
            direct_output = self._try_direct_local_export(
                study_uid, mode, target, folder_name, volume_label,
                origin_url, token, cookie, set_phase,
            )
            if direct_output is not None:
                output_path = direct_output
            else:
                with tempfile.TemporaryDirectory(prefix="paxs-client-local-") as tmp:
                    zip_path = Path(tmp) / "study.zip"
                    set_phase("downloading")
                    self._download_study_zip(study_uid, zip_path, origin_url, token, cookie)

                    if mode == "disc":
                        set_phase("preparing")
                        staged = disc_burn_service.extract_zip_safely(zip_path, Path(tmp) / "media")
                        zip_path.unlink(missing_ok=True)
                        disc_burn_service.burn_directory(
                            target, staged, set_phase, volume_label=(volume_label or "PAXS_DICOM")[:16],
                        )
                        output_path = target.upper() + "\\"
                    elif mode == "usb":
                        output_path = disc_burn_service.write_zip_to_target(
                            zip_path, Path(target.upper() + "\\"), set_phase,
                            folder_name=folder_name or study_uid, require_removable_drive=target,
                        )
                    else:
                        output_path = disc_burn_service.write_zip_to_target(
                            zip_path, Path(target), set_phase,
                            folder_name=folder_name or study_uid,
                        )
            with self._jobs_lock:
                self._local_jobs[job_id].update({"phase": "verified", "output_path": output_path, "error": None})
        except (disc_burn_service.DiscBurnError, RuntimeError) as exc:
            with self._jobs_lock:
                self._local_jobs[job_id].update({"phase": "failed", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface safe desktop error to UI
            print(f"[PAXS] local media export failed ({type(exc).__name__}): {exc}")
            with self._jobs_lock:
                self._local_jobs[job_id].update({
                    "phase": "failed",
                    "error": "Local device export failed. Check the drive and try again.",
                })

    def has_active_local_jobs(self) -> bool:
        with self._jobs_lock:
            return any(
                job.get("phase") not in {"verified", "failed"}
                for job in self._local_jobs.values()
            )

    def start_local_media_export(
        self, job_id: str, study_uid: str, mode: str, target: str,
        folder_name: str = "", volume_label: str = "PAXS_DICOM",
        origin_url: str = "", token: str = "", cookie: str = "",
    ) -> dict[str, Any]:
        """Start a direct CD/DVD burn or USB/folder export on this client Windows PC."""
        state: dict[str, Any] = {
            "phase": "queued",
            "mode": mode,
            "drive": target,
            "output_path": None,
            "error": None,
        }
        with self._jobs_lock:
            self._local_jobs[job_id] = state
        worker = threading.Thread(
            target=self._run_local_media_job,
            args=(job_id, study_uid, mode, target, folder_name, volume_label, origin_url, token, cookie),
            daemon=True,
            name=f"paxs-local-media-{job_id[:8]}",
        )
        worker.start()
        return dict(state)

    def get_local_media_job(self, job_id: str) -> dict[str, Any]:
        """Poll the current status of a local client-device burn or USB export."""
        with self._jobs_lock:
            job = self._local_jobs.get(job_id)
            if job is None:
                return {"phase": "failed", "error": "Local export job not found.", "output_path": None}
            return dict(job)


def open_window(url: str, base: Path, fullscreen: bool) -> None:
    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True  # report PDFs, exports, backups
    api = WindowApi(base_url=url)
    window = webview.create_window(
        APP_TITLE,
        url,
        js_api=api,
        width=1440,
        height=900,
        min_size=(1024, 680),
        resizable=True,
        fullscreen=fullscreen,
        maximized=not fullscreen,
        background_color="#0B1220",
        text_select=True,
    )
    api.bind(window, fullscreen)

    def on_loaded() -> None:
        try:
            window.evaluate_js(_KEYS_JS)
            bridge = window.evaluate_js(
                "typeof window.pywebview + '/' + typeof (window.pywebview && "
                "window.pywebview.api && window.pywebview.api.toggle_fullscreen)"
            )
            print(f"[PAXS] page loaded; key handler installed; bridge={bridge}")
        except Exception as exc:  # noqa: BLE001 - keep the window usable, log why
            print(f"[PAXS] key handler not installed: {type(exc).__name__}: {exc}")

    def on_closing() -> bool:
        reasons: list[str] = []
        if api.has_active_local_jobs():
            reasons.append("a CD/DVD burn or USB export is still running")
        try:
            from app.dicom.receiver_queue import receiver_queue_snapshot

            snap = receiver_queue_snapshot()
            if snap.get("active_stores", 0) > 0:
                reasons.append("DICOM images are currently being received")
        except Exception:  # noqa: BLE001 - best-effort check
            pass
        if not reasons:
            return True
        return confirm_box(
            f"PAXS Archive is busy ({' and '.join(reasons)}).\n\n"
            "Closing now may interrupt active transfers or corrupt media.\n"
            "Do you really want to close PAXS Archive?"
        )

    window.events.loaded += on_loaded
    window.events.closing += on_closing
    # private_mode=False + a storage folder keeps the login session between launches.
    webview.start(
        gui="edgechromium",
        private_mode=False,
        storage_path=str(base / "webview-data"),
        debug=False,
    )


# ------------------------------------------------------------------- main ----
def main(argv: list[str] | None = None) -> int:
    base = app_dir()
    os.chdir(base)  # .env, ./storage, ./backups resolve beside the exe
    redirect_stdio_when_windowed(base)
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    try:
        storage_dir = base / "storage"
        storage_dir.mkdir(exist_ok=True)
        free_gb = shutil.disk_usage(storage_dir).free / (1024 ** 3)
        if free_gb < 5.0:
            print(f"[PAXS][WARN] Low disk space on {storage_dir}: {free_gb:.2f} GB free")
    except OSError:
        pass

    parser = argparse.ArgumentParser(prog="PAXS-Archive", description=APP_TITLE)
    parser.add_argument("--fullscreen", action="store_true", help="start in full screen")
    parser.add_argument("--port", type=int, default=None, help="override PAXS_PORT")
    args = parser.parse_args(argv)

    try:
        import run_paxs  # the server launcher: loads .env, production gates, TLS

        run_paxs._enforce_production_config()
        port = args.port or int(os.getenv("PAXS_PORT", "8000"))
        ssl_kw = run_paxs._load_ssl_context() or {}
    except SystemExit as exc:  # a refused production config: show it, no console here
        message_box(f"PAXS Archive cannot start:\n\n{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - e.g. invalid DATABASE_URL in .env
        message_box(f"PAXS Archive cannot start:\n\n{type(exc).__name__}: {exc}")
        return 1
    scheme = "https" if ssl_kw else "http"
    base_url = f"{scheme}://127.0.0.1:{port}"

    server: ServerThread | None = None
    if port_open(port):
        if not is_paxs(base_url):
            message_box(f"Port {port} is already used by another program.\n"
                        "Close it, or set PAXS_PORT in .env to a free port.")
            return 1
        print(f"[PAXS] Attaching to the server already running on :{port}")
    else:
        server = ServerThread(
            host=os.getenv("PAXS_HOST", "0.0.0.0"),
            port=port,
            ssl_kw=ssl_kw,
            forwarded_allow_ips=os.getenv("PAXS_FORWARDED_ALLOW_IPS", "127.0.0.1"),
            root_path=os.getenv("PAXS_ROOT_PATH", ""),
        )
        server.start()

    if not wait_until_ready(base_url, server):
        detail = f"\n\n{type(server.error).__name__}: {server.error}" if server and server.error else ""
        message_box("PAXS Archive did not start. Check that PostgreSQL is running and "
                    f"DATABASE_URL in .env is correct.\nLog: {base / 'logs'}{detail}")
        if server is not None:
            server.stop()
        return 1

    try:
        open_window(f"{base_url}/", base, args.fullscreen)
    except Exception as exc:  # noqa: BLE001 - WebView2 missing/broken: fall back to the browser
        print(f"[PAXS] Native window unavailable ({type(exc).__name__}: {exc}); opening the browser")
        webbrowser.open(f"{base_url}/")
        if server is not None:
            message_box("The PAXS window could not open, so the archive opened in your web "
                        "browser instead.\nClick OK to stop the server.", error=False)
    finally:
        if server is not None:
            server.stop()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
