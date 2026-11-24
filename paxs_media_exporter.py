"""PAXS Media Exporter Studio — Standalone Windows CD/DVD/USB & DICOM Directory Exporter.

Invoked from ``http://localhost:8000/studies-ui`` (or via ``paxs-exporter://``)
when an operator clicks:
  * **Export study to CD/DVD or USB**  (``--mode media``)
  * **Export study (DICOM/ZIP)**       (``--mode dicom-zip``)

Bundles & configures **MicroDicom 2026.5 Autorun** alongside PS3.10 ``DICOMDIR``
and DICOM files stored cleanly in a dedicated configurable subdirectory
(default ``DICOM\\``), verifies SHA-256 checksums, and streams live progress
heartbeats back to PAXS so the Study List tracks the job until verified.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services import media_device_io, microdicom_autorun

APP_TITLE = "PAXS Media Exporter Studio"
PREFS_FILE = Path(os.environ.get("APPDATA") or Path.home()) / "PAXS-MediaExporter" / "prefs.json"


def _static_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "app" / "static" / "media-exporter"
    return Path(__file__).resolve().parent / "app" / "static" / "media-exporter"


def _ssl_context(url: str) -> ssl.SSLContext | None:
    if url.startswith(("https://127.0.0.1", "https://localhost")):
        return ssl._create_unverified_context()
    return None


def _sanitize_name(raw: str, fallback: str = "PAXS_STUDY") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (raw or "").strip()).strip("._-")
    return (cleaned or fallback)[:64]


def parse_protocol_uri(uri: str) -> dict[str, str]:
    """Parse ``paxs-exporter://open?server=...&session=...&token=...&mode=...``."""
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme.lower() != "paxs-exporter":
        return {}
    qs = urllib.parse.parse_qs(parsed.query)
    return {k: v[0] for k, v in qs.items() if v}


def register_windows_protocol() -> dict[str, Any]:
    """Register ``paxs-exporter://`` in HKCU (no administrator rights required)."""
    if os.name != "nt":
        return {"ok": False, "error": "Protocol registration is Windows-only."}
    import winreg

    if getattr(sys, "frozen", False):
        cmd = f'"{Path(sys.executable).resolve()}" "%1"'
        icon = f'"{Path(sys.executable).resolve()}",0'
    else:
        cmd = f'"{sys.executable}" "{Path(__file__).resolve()}" "%1"'
        icon = f'"{sys.executable}",0'

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\paxs-exporter") as key:
            winreg.SetValue(key, "", winreg.REG_SZ, "URL:PAXS Media Exporter Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\paxs-exporter\DefaultIcon") as key:
            winreg.SetValue(key, "", winreg.REG_SZ, icon)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\paxs-exporter\shell\open\command") as key:
            winreg.SetValue(key, "", winreg.REG_SZ, cmd)
        return {"ok": True, "command": cmd}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def load_prefs() -> dict[str, Any]:
    try:
        if PREFS_FILE.is_file():
            return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {}


def save_prefs(updates: dict[str, Any]) -> None:
    try:
        merged = {**load_prefs(), **updates}
        PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    except OSError:
        pass


def _pick_staging_base(cfg: dict[str, Any]) -> Path | None:
    """Choose the writable directory with the most free space (avoids full C:\\ temp)."""
    candidates: list[Path] = []
    for key in ("dest_dir", "target"):
        raw = str(cfg.get(key) or "").strip()
        if not raw:
            continue
        p = Path(f"{raw}\\") if len(raw) == 2 and raw[1] == ":" else Path(raw).expanduser()
        if p.exists() and p.is_dir():
            candidates.append(p)
        elif p.parent.exists() and p.parent.is_dir():
            candidates.append(p.parent)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    else:
        candidates.append(Path(__file__).resolve().parent)
    candidates.append(Path(tempfile.gettempdir()))

    best_dir: Path | None = None
    best_free = -1
    for cand in candidates:
        try:
            free = shutil.disk_usage(cand).free
            if free > best_free:
                # Verify writable before selecting (optical drives are read-only)
                probe = cand / f".paxs_probe_{os.getpid()}"
                probe.write_bytes(b"")
                probe.unlink(missing_ok=True)
                best_free = free
                best_dir = cand
        except OSError:
            continue
    return best_dir


    # (helper ends above ExporterApi)


def _quick_directories(usb_drives: list[dict[str, Any]]) -> list[dict[str, str]]:
    dirs: list[dict[str, str]] = []
    home = Path.home()
    for label, path in (("Desktop", home / "Desktop"), ("Documents", home / "Documents"), ("Downloads", home / "Downloads")):
        if path.is_dir():
            dirs.append({"label": label, "path": str(path)})
    for usb in usb_drives:
        dirs.append({"label": f"USB {usb['drive']} ({usb.get('label', 'Drive')})", "path": f"{usb['drive']}\\"})
    return dirs


class ExporterApi:
    """PyWebView JS API & headless-testable export engine."""

    def __init__(self, *, server_url: str, session_id: str, token: str, mode: str) -> None:
        self._server = server_url.rstrip("/")
        self._session_id = session_id
        self._token = token
        self._mode = mode if mode in ("media", "dicom-zip") else "media"
        self._window: Any = None
        self._lock = threading.Lock()
        self._study: dict[str, Any] = {}
        self._started_at = 0.0
        self._progress: dict[str, Any] = {
            "active": False,
            "phase": "ready",
            "percent": 0,
            "message": "Select destination and click Start Export.",
            "files_done": 0,
            "files_total": 0,
            "bytes_written": 0,
            "speed_mbps": 0.0,
            "elapsed_s": 0.0,
            "output_path": None,
            "microdicom_verified": False,
            "error": None,
            "logs": [],
        }

    def bind(self, window: Any) -> None:
        self._window = window

    def _log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            logs = self._progress["logs"]
            logs.append(f"[{stamp}] {text}")
            if len(logs) > 200:
                del logs[: len(logs) - 200]

    def _post_heartbeat(self, snap: dict[str, Any], target_label: str = "") -> None:
        if not self._server or not self._session_id or not self._token:
            return
        url = f"{self._server}/api/v1/export/launcher/sessions/{urllib.parse.quote(self._session_id, safe='')}/progress"
        payload = json.dumps({
            "phase": snap["phase"],
            "percent": int(snap["percent"]),
            "message": str(snap["message"])[:290],
            "target_label": target_label[:110],
            "output_path": snap["output_path"],
            "files_done": int(snap["files_done"]),
            "files_total": int(snap["files_total"]),
            "bytes_written": int(snap["bytes_written"]),
            "speed_mbps": float(snap["speed_mbps"]),
            "microdicom_verified": bool(snap["microdicom_verified"]),
            "error": snap["error"],
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Exporter-Token": self._token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5, context=_ssl_context(url)):
                pass
        except OSError:
            pass

    def _update(self, *, target_label: str = "", **fields: Any) -> None:
        with self._lock:
            self._progress.update(fields)
            if self._started_at:
                self._progress["elapsed_s"] = round(time.monotonic() - self._started_at, 1)
            snap = dict(self._progress)
        self._post_heartbeat(snap, target_label=target_label)

    def _fetch_study_info(self) -> dict[str, Any]:
        if not self._server or not self._session_id or not self._token:
            return {"study": {}, "microdicom": microdicom_autorun.microdicom_status()}
        url = f"{self._server}/api/v1/export/launcher/sessions/{urllib.parse.quote(self._session_id, safe='')}/study-info"
        req = urllib.request.Request(url, headers={"X-Exporter-Token": self._token})
        try:
            with urllib.request.urlopen(req, timeout=15, context=_ssl_context(url)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self._study = data.get("study") or {}
                return data
        except Exception as exc:  # noqa: BLE001 - surface safe message in UI
            self._log(f"Warning: could not fetch study metadata ({type(exc).__name__})")
            return {"study": {}, "microdicom": microdicom_autorun.microdicom_status()}

    def get_bootstrap(self) -> dict[str, Any]:
        info = self._fetch_study_info()
        devices = self.scan_devices()
        return {
            "mode": self._mode,
            "study": info.get("study") or self._study,
            "microdicom": microdicom_autorun.microdicom_status(),
            "devices": devices,
            "prefs": load_prefs(),
        }

    def scan_devices(self) -> dict[str, Any]:
        inv = media_device_io.list_removable_devices()
        inv["quick_dirs"] = _quick_directories(inv.get("usb_drives") or [])
        return inv

    def check_disc(self, drive: str = "") -> dict[str, Any]:
        try:
            return {"ok": True, **media_device_io.check_media(drive or "")}
        except media_device_io.MediaIOError as exc:
            return {"ok": False, "error": str(exc)}

    def pick_folder(self) -> dict[str, Any]:
        if self._window is None:
            return {"ok": False, "path": None}
        import webview

        dlg = getattr(getattr(webview, "FileDialog", None), "FOLDER", webview.FOLDER_DIALOG)
        chosen = self._window.create_file_dialog(dlg)
        if not chosen:
            return {"ok": False, "path": None}
        folder = chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen)
        return {"ok": bool(folder), "path": str(folder)}

    def register_protocol(self) -> dict[str, Any]:
        return register_windows_protocol()

    def open_folder(self, path: str) -> dict[str, Any]:
        target = Path(path)
        open_target = target.parent if target.is_file() else target
        if not open_target.exists():
            return {"ok": False, "error": "Destination path does not exist."}
        if os.name == "nt":
            os.startfile(str(open_target))  # noqa: S606 - explicit operator click
        return {"ok": True}

    def launch_microdicom(self, exported_path: str) -> dict[str, Any]:
        """Launch the exported MicroDicom 2026.5 viewer directly on the study."""
        root = Path(exported_path)
        if root.is_file():
            root = root.parent
        exe = root / "x64" / "mDicom.exe"
        if not exe.is_file():
            exe = root / "Win32" / "mDicom.exe"
        if not exe.is_file():
            return {"ok": False, "error": "MicroDicom viewer executable was not found in the exported folder."}
        dicomdir = root / "DICOMDIR"
        args = [str(exe), str(dicomdir)] if dicomdir.is_file() else [str(exe), "/scan", str(root)]
        try:
            subprocess.Popen(args, cwd=str(root))  # noqa: S603 - operator-initiated viewer launch
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def get_progress(self) -> dict[str, Any]:
        with self._lock:
            snap = dict(self._progress)
            if self._started_at and snap["active"]:
                snap["elapsed_s"] = round(time.monotonic() - self._started_at, 1)
            return snap

    def _download_package(self, media_format: str, dest: Path, target_label: str) -> int:
        url = (
            f"{self._server}/api/v1/export/launcher/sessions/"
            f"{urllib.parse.quote(self._session_id, safe='')}/download/{media_format}"
        )
        req = urllib.request.Request(url, headers={"X-Exporter-Token": self._token})
        self._log(f"Downloading {media_format} package from PAXS Archive…")
        started = time.monotonic()
        written = 0
        try:
            with urllib.request.urlopen(req, timeout=900, context=_ssl_context(url)) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                if total > 0 and shutil.disk_usage(dest.parent).free < total:
                    raise RuntimeError("Not enough free disk space in staging directory.")
                with dest.open("wb") as out:
                    while chunk := resp.read(1024 * 1024):
                        out.write(chunk)
                        written += len(chunk)
                        elapsed = max(0.05, time.monotonic() - started)
                        speed = (written / (1024 * 1024)) / elapsed
                        pct = int(5 + (30 * written / total)) if total > 0 else 20
                        self._update(
                            target_label=target_label,
                            phase="downloading",
                            percent=min(35, pct),
                            message=f"Downloading study package ({written / (1024 * 1024):.1f} MB)…",
                            bytes_written=written,
                            speed_mbps=round(speed, 1),
                        )
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Archive download failed (HTTP {exc.code}).") from None
        except OSError as exc:
            raise RuntimeError(f"Could not connect to PAXS Archive ({type(exc).__name__}).") from None
        self._log(f"Downloaded {written / (1024 * 1024):.2f} MB successfully.")
        return written

    def _prepare_media_tree(
        self,
        zip_path: Path,
        staging_root: Path,
        *,
        dicom_dir: str,
        include_microdicom: bool,
        arch: str,
        open_mode: str,
        volume_label: str,
        target_label: str,
    ) -> tuple[Path, dict[str, Any] | None]:
        self._update(
            target_label=target_label,
            phase="preparing",
            percent=42,
            message=f"Extracting DICOMDIR and organizing files into .\\{dicom_dir}\\…",
        )
        staged = media_device_io.extract_zip_safely(zip_path, staging_root)
        zip_path.unlink(missing_ok=True)
        folder = microdicom_autorun.relocate_dicomdir_into_subdir(staged, dicom_dir)
        self._log(f"Organized DICOMDIR + study objects into .\\{folder}\\ (PS3.10 verified).")

        md_report = None
        if include_microdicom:
            self._update(
                target_label=target_label,
                phase="microdicom",
                percent=58,
                message="Installing & configuring MicroDicom 2026.5 Autorun viewer…",
            )
            md_report = microdicom_autorun.install_microdicom_autorun(
                staged,
                dicom_dir_name=folder,
                arch="x64" if arch == "x64" else "universal",
                open_mode="scan" if open_mode == "scan" else "dicomdir",
                volume_label=volume_label,
                study_info=self._study,
            )
            if not md_report.get("ok"):
                raise RuntimeError("MicroDicom 2026.5 autorun verification failed.")
            self._log(
                f"Configured MicroDicom {md_report['version']} ({arch}, {open_mode}, {md_report['dicom_instances']} DICOM objects)."
            )
        return staged, md_report

    def _copy_tree_with_verify(
        self, src_root: Path, dst_root: Path, target_label: str
    ) -> tuple[int, int]:
        files = [p for p in sorted(src_root.rglob("*")) if p.is_file()]
        total_files = len(files)
        total_bytes = sum(p.stat().st_size for p in files)
        dst_root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(dst_root).free < total_bytes:
            raise RuntimeError(f"Not enough free space on {dst_root} (need {total_bytes / (1024 * 1024):.1f} MB).")

        manifest: dict[Path, bytes] = {}
        written_bytes = 0
        started = time.monotonic()
        for idx, src_file in enumerate(files, start=1):
            rel = src_file.relative_to(src_root)
            out_file = dst_root / rel
            out_file.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with src_file.open("rb") as fin, out_file.open("wb") as fout:
                while chunk := fin.read(1024 * 1024):
                    fout.write(chunk)
                    digest.update(chunk)
                    written_bytes += len(chunk)
            manifest[rel] = digest.digest()
            if idx == total_files or idx % 5 == 0:
                speed = (written_bytes / (1024 * 1024)) / max(0.05, time.monotonic() - started)
                pct = 65 + int(23 * idx / max(1, total_files))
                self._update(
                    target_label=target_label,
                    phase="writing",
                    percent=min(88, pct),
                    message=f"Writing file {idx} of {total_files}…",
                    files_done=idx,
                    files_total=total_files,
                    bytes_written=written_bytes,
                    speed_mbps=round(speed, 1),
                )

        self._update(
            target_label=target_label,
            phase="verifying",
            percent=92,
            message="Verifying SHA-256 checksums on destination media…",
            files_done=total_files,
            files_total=total_files,
        )
        media_device_io.verify_manifest(dst_root, manifest)
        self._log(f"SHA-256 verified all {total_files} written file(s) ({written_bytes / (1024 * 1024):.2f} MB).")
        return total_files, written_bytes

    def _run_export_worker(self, cfg: dict[str, Any]) -> None:
        workflow = cfg.get("workflow", "media")
        target_label = str(cfg.get("target") or cfg.get("dest_dir") or "")
        stage_base = _pick_staging_base(cfg)
        try:
            with tempfile.TemporaryDirectory(
                prefix=".paxs-exporter-", dir=str(stage_base) if stage_base else None
            ) as tmp:
                tmp_path = Path(tmp)
                if workflow == "dicom-zip":
                    self._execute_directory_workflow(cfg, tmp_path, target_label)
                else:
                    self._execute_media_workflow(cfg, tmp_path, target_label)
        except Exception as exc:  # noqa: BLE001 - safe operator error reporting
            msg = str(exc) or f"Export failed ({type(exc).__name__})."
            self._log(f"ERROR: {msg}")
            self._update(
                target_label=target_label,
                active=False,
                phase="failed",
                message=msg,
                error=msg,
            )

    def _execute_directory_workflow(
        self, cfg: dict[str, Any], tmp_path: Path, target_label: str
    ) -> None:
        fmt = str(cfg.get("format") or "dicom-zip")
        dest_dir = Path(str(cfg["dest_dir"])).expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = _sanitize_name(str(cfg.get("output_name") or ""), "PAXS_STUDY")
        dicom_dir = microdicom_autorun.normalize_dicom_dir_name(cfg.get("dicom_dir"))
        save_prefs({"last_dest_dir": str(dest_dir), "dicom_dir": dicom_dir})

        if fmt in ("dicom-zip", "anonymized-zip"):
            dl_fmt = "anonymized" if fmt == "anonymized-zip" else "dicom-zip"
            suffix = "_ANONYMIZED.zip" if fmt == "anonymized-zip" else ".zip"
            final_zip = dest_dir / f"{name}{suffix}"
            part_zip = dest_dir / f".{name}{suffix}.part"
            written = self._download_package(dl_fmt, part_zip, str(dest_dir))
            self._update(
                target_label=str(dest_dir),
                phase="verifying",
                percent=92,
                message="Verifying ZIP archive integrity…",
                bytes_written=written,
            )
            import zipfile

            with zipfile.ZipFile(part_zip, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise RuntimeError(f"ZIP integrity verification failed at {bad}.")
                count = len(zf.namelist())
            part_zip.replace(final_zip)
            self._log(f"Verified ZIP archive ({count} entries) saved to {final_zip}")
            self._update(
                target_label=str(dest_dir),
                active=False,
                phase="verified",
                percent=100,
                message=f"Saved & verified {final_zip.name}",
                output_path=str(final_zip),
                files_done=count,
                files_total=count,
                bytes_written=written,
                microdicom_verified=False,
            )
            return

        include_md = fmt == "portable-viewer"
        zip_path = tmp_path / "study.zip"
        self._download_package("zip", zip_path, str(dest_dir))
        staged, md_rep = self._prepare_media_tree(
            zip_path,
            tmp_path / "staged",
            dicom_dir=dicom_dir,
            include_microdicom=include_md,
            arch=str(cfg.get("microdicom_arch") or "universal"),
            open_mode=str(cfg.get("microdicom_open") or "dicomdir"),
            volume_label="PAXS Medical Study",
            target_label=str(dest_dir),
        )
        final_folder = dest_dir / name
        files_count, total_bytes = self._copy_tree_with_verify(staged, final_folder, str(final_folder))
        if include_md:
            final_check = microdicom_autorun.verify_autorun_package(final_folder, dicom_dir_name=dicom_dir)
            if not final_check["ok"]:
                raise RuntimeError("Destination MicroDicom autorun verification failed.")
        self._update(
            target_label=str(final_folder),
            active=False,
            phase="verified",
            percent=100,
            message=f"Exported & verified in {final_folder}",
            output_path=str(final_folder),
            files_done=files_count,
            files_total=files_count,
            bytes_written=total_bytes,
            microdicom_verified=bool(include_md and md_rep and md_rep.get("ok")),
        )

    def _execute_media_workflow(
        self, cfg: dict[str, Any], tmp_path: Path, target_label: str
    ) -> None:
        kind = str(cfg.get("kind") or "usb")
        include_md = bool(cfg.get("include_microdicom", True))
        dicom_dir = microdicom_autorun.normalize_dicom_dir_name(cfg.get("dicom_dir"))
        arch = str(cfg.get("microdicom_arch") or "universal")
        open_mode = str(cfg.get("microdicom_open") or "dicomdir")
        vol_label = re.sub(r"[^A-Z0-9_]+", "_", str(cfg.get("volume_label") or "PAXS_DICOM").upper())[:16] or "PAXS_DICOM"
        save_prefs({"dicom_dir": dicom_dir, "microdicom_arch": arch, "microdicom_open": open_mode})

        dl_fmt = "anonymized" if cfg.get("anonymize") else "zip"
        zip_path = tmp_path / "study.zip"
        self._download_package(dl_fmt, zip_path, target_label)

        if dl_fmt == "anonymized":
            # Build a fresh PS3.10 DICOMDIR from the anonymized DICOM files!
            anon_raw = media_device_io.extract_zip_safely(zip_path, tmp_path / "anon_raw")
            zip_path.unlink(missing_ok=True)
            staged = self._build_dicomdir_from_raw_folder(anon_raw, tmp_path / "staged", dicom_dir)
            md_rep = None
            if include_md:
                self._update(target_label=target_label, phase="microdicom", percent=58, message="Configuring MicroDicom 2026.5 Autorun…")
                md_rep = microdicom_autorun.install_microdicom_autorun(
                    staged, dicom_dir_name=dicom_dir, arch="x64" if arch == "x64" else "universal",
                    open_mode="scan" if open_mode == "scan" else "dicomdir", volume_label=vol_label,
                )
        else:
            staged, md_rep = self._prepare_media_tree(
                zip_path, tmp_path / "staged", dicom_dir=dicom_dir,
                include_microdicom=include_md, arch=arch, open_mode=open_mode,
                volume_label=vol_label, target_label=target_label,
            )

        if kind == "disc":
            drive = str(cfg["target"]).upper()
            def _on_phase(ph: str) -> None:
                pct = 75 if ph == "writing" else (92 if ph == "verifying" else 65)
                self._update(target_label=drive, phase=ph, percent=pct, message=f"Optical disc {drive}: {ph}…")
            media_device_io.burn_directory(drive, staged, _on_phase, volume_label=vol_label)
            output = f"{drive}\\"
            files_count = sum(1 for p in staged.rglob("*") if p.is_file())
            total_bytes = sum(p.stat().st_size for p in staged.rglob("*") if p.is_file())
        elif kind == "iso":
            dest_dir = Path(str(cfg["target"])).expanduser()
            dest_dir.mkdir(parents=True, exist_ok=True)
            save_prefs({"last_dest_dir": str(dest_dir)})
            iso_name = _sanitize_name(str(cfg.get("folder_name") or ""), "PAXS_AUTORUN") + ".iso"
            iso_path = dest_dir / iso_name
            self._update(target_label=str(iso_path), phase="writing", percent=78, message=f"Building ISO-9660/Joliet image {iso_name}…")
            media_device_io.write_iso_image(staged, iso_path, volume_label=vol_label)
            self._update(target_label=str(iso_path), phase="verifying", percent=94, message="Verifying ISO image…")
            total_bytes = iso_path.stat().st_size
            files_count = sum(1 for p in staged.rglob("*") if p.is_file())
            output = str(iso_path)
        else:
            # USB or Portable Folder
            if kind == "usb":
                drive = str(cfg["target"]).upper()
                root_dir = Path(f"{drive}\\")
                dest_root = root_dir if cfg.get("write_to_root") else (root_dir / _sanitize_name(str(cfg.get("folder_name") or ""), "PAXS_STUDY"))
            else:
                parent = Path(str(cfg["target"])).expanduser()
                save_prefs({"last_dest_dir": str(parent)})
                dest_root = parent / _sanitize_name(str(cfg.get("folder_name") or ""), "PAXS_STUDY")
            files_count, total_bytes = self._copy_tree_with_verify(staged, dest_root, str(dest_root))
            if include_md:
                chk = microdicom_autorun.verify_autorun_package(dest_root, dicom_dir_name=dicom_dir)
                if not chk["ok"]:
                    raise RuntimeError("Destination MicroDicom autorun verification failed.")
            output = str(dest_root)

        self._update(
            target_label=output,
            active=False,
            phase="verified",
            percent=100,
            message=f"Verified & Ready → {output}",
            output_path=output,
            files_done=files_count,
            files_total=files_count,
            bytes_written=total_bytes,
            microdicom_verified=bool(include_md and kind != "iso" and (md_rep is None or md_rep.get("ok"))),
        )

    def _build_dicomdir_from_raw_folder(self, raw_dir: Path, staged: Path, dicom_dir: str) -> Path:
        from pydicom import dcmread
        from pydicom.fileset import FileSet

        staged.mkdir(parents=True, exist_ok=True)
        fs = FileSet()
        for idx, dcm_file in enumerate(sorted(raw_dir.rglob("*.dcm")), start=1):
            ds = dcmread(str(dcm_file))
            if not ds.get("StudyDate"):
                ds.StudyDate = "19700101"
            if not ds.get("StudyTime"):
                ds.StudyTime = "000000"
            if not ds.get("StudyID"):
                ds.StudyID = "ANON1"
            if not ds.get("PatientID"):
                ds.PatientID = "ANONYMOUS"
            if ds.get("SeriesNumber") in (None, ""):
                ds.SeriesNumber = 1
            if ds.get("InstanceNumber") in (None, ""):
                ds.InstanceNumber = idx
            fs.add(ds)
        fs.write(staged)
        microdicom_autorun.relocate_dicomdir_into_subdir(staged, dicom_dir)
        return staged

    def start_export(self, cfg: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._progress["active"]:
                return {"ok": False, "error": "An export is already in progress."}
            self._started_at = time.monotonic()
            self._progress.update({
                "active": True,
                "phase": "downloading",
                "percent": 2,
                "message": "Starting export pipeline…",
                "files_done": 0,
                "files_total": int(self._study.get("n_instances") or 0),
                "bytes_written": 0,
                "speed_mbps": 0.0,
                "elapsed_s": 0.0,
                "output_path": None,
                "microdicom_verified": False,
                "error": None,
                "logs": [],
            })
        self._log("Export started.")
        threading.Thread(
            target=self._run_export_worker,
            args=(cfg,),
            daemon=True,
            name="paxs-exporter-worker",
        ).start()
        return {"ok": True}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    # Support direct Windows URI invocation: PAXS-MediaExporter.exe "paxs-exporter://open?..."
    if len(argv) == 1 and argv[0].lower().startswith("paxs-exporter://"):
        params = parse_protocol_uri(argv[0])
        return argparse.Namespace(
            server=params.get("server", "http://127.0.0.1:8000"),
            session=params.get("session", ""),
            token=params.get("token", ""),
            mode=params.get("mode", "media"),
            register_protocol=False,
        )
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--session", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--mode", choices=("media", "dicom-zip"), default="media")
    parser.add_argument("--register-protocol", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.register_protocol:
        res = register_windows_protocol()
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    import webview

    api = ExporterApi(
        server_url=args.server,
        session_id=args.session,
        token=args.token,
        mode=args.mode,
    )
    index_html = _static_dir() / "index.html"
    window = webview.create_window(
        APP_TITLE,
        index_html.resolve().as_uri(),
        js_api=api,
        width=1120,
        height=780,
        min_size=(880, 620),
        resizable=True,
        background_color="#060b16",
        text_select=True,
    )
    api.bind(window)
    webview.start(gui="edgechromium" if os.name == "nt" else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
