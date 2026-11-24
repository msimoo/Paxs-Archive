"""Start packaged PAXS applications and monitor local service endpoints."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener

from desktop_viewer.preferences import OPTION_LABELS, load_preferences, reset_preferences, save_preferences
from paxs_control.dicom_probe import overall_dicom_status, probe_dicom
from paxs_control.protocol_suite import PROTOCOLS, run_synthetic_protocol_suite
from paxs_control.settings import ControlSettings, load_settings, save_settings
from paxs_control.support import create_support_report, viewer_integrity_status


def classify_http(status: int) -> str:
    """Distinguish authentication from a successful response."""
    if status in (401, 403) or 300 <= status < 400:
        return "AUTH REQUIRED"
    return "ONLINE" if status == 200 else "UNAVAILABLE"


def server_command(root: Path) -> Path | None:
    """Require a bundled server and operator-supplied runtime configuration."""
    directory = root / "PAXS-Server"
    executable = directory / "PAXS-Server.exe"
    return executable if executable.is_file() and (directory / ".env").is_file() else None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_probe(path: str, expected: str | None = None, *, base_url: str = "http://127.0.0.1:8000") -> str:
    """Probe a loopback endpoint; never follow redirects to an unrelated site."""
    try:
        with build_opener(NoRedirect).open(base_url + path, timeout=2) as response:
            if expected is not None:
                data = json.loads(response.read(4096))
                return "ONLINE" if isinstance(data, dict) and data.get("status") == expected else "UNAVAILABLE"
            return classify_http(response.status)
    except HTTPError as error:
        return classify_http(error.code)
    except (URLError, OSError, ValueError, json.JSONDecodeError):
        return "OFFLINE"


def tcp_probe(port: int, host: str = "127.0.0.1") -> str:
    """Report a TCP listener, without claiming protocol-level readiness."""
    try:
        with socket.create_connection((host, port), timeout=1):
            return "LISTENING"
    except OSError:
        return "NOT LISTENING"


def redis_probe(host: str = "127.0.0.1", port: int = 6379) -> str:
    """Send a bounded Redis PING without changing server data."""
    try:
        with socket.create_connection((host, port), timeout=1) as connection:
            connection.sendall(b"*1\r\n$4\r\nPING\r\n")
            response = connection.recv(128)
        if response.startswith(b"+PONG\r\n"):
            return "ONLINE"
        if response.startswith(b"-NOAUTH"):
            return "AUTH REQUIRED"
        return "UNAVAILABLE"
    except OSError:
        return "OFFLINE"


class ControlApp:
    """Desktop controller with nonblocking health polling."""

    def __init__(self, root: tk.Tk, package: Path) -> None:
        self.root = root
        self.package = package
        self.server: subprocess.Popen | None = None
        self.viewer: subprocess.Popen | None = None
        self.dicom_check_running = False
        self.integrity_check_running = False
        self.check_statuses = {"viewer_integrity": "UNKNOWN"}
        try:
            self.connection_settings = load_settings()
        except (OSError, ValueError):
            self.connection_settings = ControlSettings()
            messagebox.showerror("Invalid control settings", "Saved connection settings could not be read. Default monitoring targets are being used.")
        root.title("PAXS Control")
        root.geometry("820x650")
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="PAXS Control", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Local service monitoring • refreshes every 5 seconds").pack(anchor="w", pady=(0, 12))
        self.status = tk.StringVar(value="Checking services...")
        ttk.Label(frame, textvariable=self.status, justify="left", font=("Consolas", 11)).pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w", pady=18)
        ttk.Button(buttons, text="Start PAXS Server", command=self.start_server).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Open PAXS Viewer", command=self.start_viewer).pack(side="left")
        ttk.Button(buttons, text="Monitoring targets", command=self.edit_connection_settings).pack(side="left", padx=8)
        ttk.Button(buttons, text="Check DICOM", command=self.check_dicom).pack(side="left")
        diagnostics = ttk.Frame(frame)
        diagnostics.pack(anchor="w", pady=(0, 8))
        ttk.Button(diagnostics, text="Check viewer assets", command=self.check_viewer_integrity).pack(side="left", padx=(0, 8))
        ttk.Button(diagnostics, text="Export safe status", command=self.export_support_status).pack(side="left", padx=(0, 8))
        ttk.Button(diagnostics, text="Reset viewer options", command=self.reset_viewer_options).pack(side="left")
        options = load_preferences()
        self.fullscreen = tk.BooleanVar(value=options["fullscreen"])
        self.browser_options = tk.BooleanVar(value=options["browser_options"])
        ttk.Checkbutton(frame, text="Viewer full screen", variable=self.fullscreen).pack(anchor="w")
        ttk.Checkbutton(frame, text="Enable browser option overrides", variable=self.browser_options).pack(anchor="w")
        option_frame = ttk.Frame(frame)
        option_frame.pack(anchor="w")
        self.individual_options = {}
        for index, (key, label) in enumerate(OPTION_LABELS.items()):
            variable = tk.BooleanVar(value=options[key])
            self.individual_options[key] = variable
            ttk.Checkbutton(option_frame, text=label, variable=variable).grid(
                row=index // 2, column=index % 2, sticky="w", padx=(0, 12)
            )
        ttk.Button(frame, text="Save viewer settings", command=self.save_viewer_settings).pack(anchor="w", pady=6)
        ttk.Label(frame, text="Restart the viewer after saving. Debugging can expose viewer data to local users.").pack(anchor="w")
        ttk.Label(frame, text="Some Chrome settings are unavailable in WebView2; see viewer README.").pack(anchor="w")
        ttk.Label(frame, text="Viewer HTTP status checks availability only, not image rendering.\nDICOM status shows a TCP listener, not protocol health.", justify="left").pack(anchor="w")
        self.refresh()

    def check_viewer_integrity(self) -> None:
        """Check packaged local assets without claiming remote viewer integrity."""
        if self.integrity_check_running:
            return
        self.integrity_check_running = True
        directory = self.package / "PAXS-Server" / "_internal" / "app" / "static" / "viewer"

        def worker() -> None:
            result = viewer_integrity_status(directory)

            def show() -> None:
                self.integrity_check_running = False
                self.check_statuses["viewer_integrity"] = result
                messagebox.showinfo("Local viewer assets", result + "\nThis checks only the bundled local files.")

            self.root.after(0, show)

        threading.Thread(target=worker, daemon=True).start()

    def export_support_status(self) -> None:
        """Export only fixed status codes, without logs or configuration values."""
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(create_support_report(self.check_statuses), indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            messagebox.showerror("Export failed", str(error))

    def reset_viewer_options(self) -> None:
        """Restore option defaults without replacing the server address."""
        try:
            reset_preferences()
        except (OSError, ValueError) as error:
            messagebox.showerror("Reset failed", str(error))
            return
        options = load_preferences()
        self.fullscreen.set(options["fullscreen"])
        self.browser_options.set(options["browser_options"])
        for key, variable in self.individual_options.items():
            variable.set(options[key])
        messagebox.showinfo("Viewer options", "Defaults saved. Restart the viewer to apply them. Defaults enable debugging port 9222.")

    def check_dicom(self) -> None:
        """Check the live endpoint and run eight isolated synthetic tests."""
        if self.dicom_check_running:
            return
        self.dicom_check_running = True
        settings = self.connection_settings

        def worker() -> None:
            results = probe_dicom(settings)
            suite_status, suite_results = run_synthetic_protocol_suite(self.package)
            suite = "\n".join(f"{name}: {suite_results.get(name, 'NOT RUN')}" for name in PROTOCOLS)
            summary = ("Isolated source tests: " + suite_status + "\n" + suite
                       + "\n\nLive archive: " + overall_dicom_status(results) + "\n" + "\n".join(
                f"{name}: {status}" for name, status in results.items()
            ) + "\n\nThe isolated test result does not establish live archive health.")

            def show() -> None:
                self.dicom_check_running = False
                messagebox.showinfo("DICOM protocol check", summary)

            self.root.after(0, show)

        threading.Thread(target=worker, daemon=True).start()

    def edit_connection_settings(self) -> None:
        """Edit monitor targets without changing the server's own configuration."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Monitoring targets")
        entries = {}
        for row, (field, label) in enumerate((
            ("server_url", "Server URL"), ("dicom_host", "DICOM host"),
            ("dicom_port", "DICOM port"), ("ae_title", "DICOM AE title"),
            ("redis_host", "Redis host"), ("redis_port", "Redis port"),
        )):
            ttk.Label(dialog, text=label).grid(row=row, column=0, padx=8, pady=4, sticky="w")
            entry = ttk.Entry(dialog, width=45)
            entry.insert(0, str(getattr(self.connection_settings, field)))
            entry.grid(row=row, column=1, padx=8, pady=4)
            entries[field] = entry

        def save() -> None:
            try:
                values = {key: entry.get().strip() for key, entry in entries.items()}
                values["dicom_port"] = int(values["dicom_port"])
                values["redis_port"] = int(values["redis_port"])
                updated = ControlSettings(**values)
                save_settings(updated)
            except (OSError, ValueError) as error:
                messagebox.showerror("Invalid monitoring targets", str(error), parent=dialog)
                return
            self.connection_settings = updated
            dialog.destroy()

        ttk.Label(dialog, text="Monitoring only; server settings are configured separately.").grid(row=6, columnspan=2, padx=8)
        ttk.Button(dialog, text="Save", command=save).grid(row=7, columnspan=2, pady=8)

    def save_viewer_settings(self) -> None:
        """Persist preferences for the next viewer launch."""
        try:
            save_preferences(fullscreen=self.fullscreen.get(), browser_options=self.browser_options.get(),
                             individual={key: value.get() for key, value in self.individual_options.items()})
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not save viewer settings", str(error))
            return
        messagebox.showinfo("Viewer settings", "Saved. Close and reopen PAXS Viewer to apply changes.")

    def start_server(self) -> None:
        executable = server_command(self.package)
        if executable is None:
            messagebox.showerror("Server setup", "PAXS-Server.exe or its .env configuration is missing. See README.txt in the package.")
            return
        if self.server is not None and self.server.poll() is None:
            messagebox.showinfo("Server", "The server started by this window is already running.")
            return
        if tcp_probe(8000) == "LISTENING":
            messagebox.showwarning("Port in use", "Port 8000 is already in use. Check the running service before starting another server.")
            return
        try:
            self.server = subprocess.Popen([str(executable)], cwd=str(executable.parent), creationflags=subprocess.CREATE_NEW_CONSOLE)
        except OSError as error:
            messagebox.showerror("Server start failed", str(error))

    def start_viewer(self) -> None:
        candidates = ("PAXSViewer-Fixed.exe", "PAXSViewer-Updated.exe", "PAXSViewer.exe")
        executable = next((self.package / name for name in candidates
                           if (self.package / name).is_file()), self.package / candidates[-1])
        if not executable.is_file():
            messagebox.showerror("Viewer setup", "PAXSViewer.exe is missing from the package.")
            return
        if self.viewer is not None and self.viewer.poll() is None:
            messagebox.showinfo("Viewer", "The viewer started by this window is already running.")
            return
        try:
            self.viewer = subprocess.Popen([str(executable)], cwd=str(self.package))
        except OSError as error:
            messagebox.showerror("Viewer start failed", str(error))

    def refresh(self) -> None:
        def probe() -> None:
            settings = self.connection_settings
            statuses = {
                "server": http_probe('/api/v1/health/live', 'ok', base_url=settings.server_url),
                "database": http_probe('/api/v1/health/ready', 'ready', base_url=settings.server_url),
                "viewer_http": http_probe('/viewer/', base_url=settings.server_url),
                "dicom_listener": tcp_probe(settings.dicom_port, settings.dicom_host),
                "redis": redis_probe(settings.redis_host, settings.redis_port),
            }
            def update() -> None:
                self.check_statuses.update(statuses)
                labels = {"server": "Server liveness", "database": "Database readiness", "viewer_http": "Viewer HTTP", "dicom_listener": "DICOM listener", "redis": "Redis PING"}
                self.status.set("\n".join(f"{labels[key]:19} {value}" for key, value in statuses.items()))
            self.root.after(0, update)
            self.root.after(5000, self.refresh)
        threading.Thread(target=probe, daemon=True).start()


def main() -> None:
    """Launch the controller next to the packaged server and viewer."""
    package = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    root = tk.Tk()
    ControlApp(root, package)
    root.mainloop()


if __name__ == "__main__":
    main()
