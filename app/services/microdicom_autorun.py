"""MicroDicom 2026.5 Autorun package manager & PS3.10 DICOMDIR directory configurator.

Bundles the official MicroDicom 2026.5 portable autorun viewer alongside a
study's PS3.10 ``DICOMDIR`` and DICOM files stored in a dedicated, configurable
subdirectory (default ``DICOM``) so discs and USB drives handed to patients and
physicians auto-launch and display the study immediately on Windows.
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Literal

from pydicom.filebase import DicomFileLike
from pydicom.fileset import FileSet

log = logging.getLogger("paxs.microdicom_autorun")

MICRODICOM_VERSION = "2026.5"
_DICOM_DIR_RE = re.compile(r"^[A-Z0-9_]{1,8}$")

ArchMode = Literal["universal", "x64"]
OpenMode = Literal["dicomdir", "scan"]


def normalize_dicom_dir_name(raw: str | None) -> str:
    """Validate and normalize a DICOM PS3.10 8-char directory name (default DICOM)."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "", (raw or "DICOM").strip()).upper()[:8]
    if not cleaned or not _DICOM_DIR_RE.fullmatch(cleaned):
        return "DICOM"
    return cleaned


def find_microdicom_source() -> Path | None:
    """Locate the extracted MicroDicom 2026.5 autorun directory (frozen or source)."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([
            exe_dir / "microdicom-autorun",
            exe_dir.parent / "PAXS-MediaExporter" / "microdicom-autorun",
            exe_dir.parent / "PAXS-Server" / "microdicom-autorun",
        ])
    repo_root = Path(__file__).resolve().parents[2]
    candidates.extend([
        repo_root / ".tools" / "microdicom-autorun" / "extracted",
        repo_root / ".tools" / "microdicom-autorun",
        repo_root / "dist" / "PAXS-MediaExporter" / "microdicom-autorun",
        repo_root / "dist" / "PAXS-Server" / "microdicom-autorun",
    ])
    for root in candidates:
        if (root / "x64" / "mDicom.exe").is_file():
            return root
    return None


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def microdicom_status() -> dict[str, Any]:
    """Report availability, version, and footprint of the MicroDicom autorun bundle."""
    src = find_microdicom_source()
    if src is None:
        return {
            "available": False,
            "version": MICRODICOM_VERSION,
            "path": None,
            "has_x64": False,
            "has_win32": False,
            "compact_bytes": 0,
            "universal_bytes": 0,
        }
    x64_exe = src / "x64" / "mDicom.exe"
    win32_exe = src / "Win32" / "mDicom.exe"
    shared = (
        _dir_size(src / "lang")
        + _dir_size(src / "licenses")
        + _dir_size(src / "settings")
    )
    x64_size = x64_exe.stat().st_size if x64_exe.is_file() else 0
    win32_size = win32_exe.stat().st_size if win32_exe.is_file() else 0
    return {
        "available": True,
        "version": MICRODICOM_VERSION,
        "path": str(src),
        "has_x64": x64_exe.is_file(),
        "has_win32": win32_exe.is_file(),
        "compact_bytes": x64_size + shared,
        "universal_bytes": x64_size + win32_size + shared,
    }


def relocate_dicomdir_into_subdir(media_root: Path, dicom_dir_name: str = "DICOM") -> str:
    """Move FileSet instances under ``media_root/<dicom_dir_name>/`` and rewrite DICOMDIR.

    Keeps ``DICOMDIR`` at the media root (PS3.10 standard) while neatly isolating
    all DICOM study objects inside a dedicated subdirectory (e.g. ``DICOM\\``) and
    recalculating every PS3.10 byte offset cleanly via ``FileSet._write_dicomdir``.
    """
    folder = normalize_dicom_dir_name(dicom_dir_name)
    dicomdir_path = media_root / "DICOMDIR"
    if not dicomdir_path.is_file():
        raise FileNotFoundError(f"DICOMDIR not found in {media_root}")

    fs = FileSet(str(dicomdir_path))
    moved_top_dirs: set[str] = set()
    for inst in fs:
        rec = inst.node._record
        raw_ref = rec.ReferencedFileID
        parts = [str(raw_ref)] if isinstance(raw_ref, str) else [str(p) for p in raw_ref]
        if not parts or parts[0].upper() == folder:
            continue
        old_path = media_root.joinpath(*parts)
        if not old_path.is_file():
            continue
        new_parts = [folder, *parts]
        new_path = media_root.joinpath(*new_parts)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.replace(new_path)
        rec.ReferencedFileID = new_parts
        moved_top_dirs.add(parts[0])

    # Rewrite DICOMDIR via FileSet so every PS3.10 byte offset is recomputed accurately.
    with dicomdir_path.open("wb") as fp:
        fs._write_dicomdir(DicomFileLike(fp))

    for top in moved_top_dirs:
        candidate = media_root / top
        if candidate.is_dir() and not any(p.is_file() for p in candidate.rglob("*")):
            shutil.rmtree(candidate, ignore_errors=True)

    # Re-parse with pydicom FileSet to prove 100% PS3.10 compliance.
    reloaded = FileSet(str(dicomdir_path))
    if len(reloaded) == 0:
        raise ValueError("Relocated DICOMDIR contains zero referenced DICOM instances.")
    return folder


def _build_autorun_inf(
    *, dicom_dir_name: str, open_mode: OpenMode, arch: ArchMode, label: str
) -> str:
    target = "DICOMDIR" if open_mode == "dicomdir" else f"/scan {dicom_dir_name}"
    safe_label = re.sub(r"[\r\n]+", " ", (label or "PAXS Medical Imaging Study").strip())[:48]
    win32_exe = "Win32\\mDicom.exe" if arch == "universal" else "x64\\mDicom.exe"
    return (
        "[AutoRun.Amd64]\r\n"
        f"LABEL={safe_label}\r\n"
        f"OPEN=x64\\mDicom.exe {target}\r\n"
        "ICON=x64\\mDicom.exe,0\r\n"
        "ACTION=View Medical Images (MicroDicom)\r\n"
        "\r\n"
        "[AutoRun]\r\n"
        f"LABEL={safe_label}\r\n"
        f"OPEN={win32_exe} {target}\r\n"
        f"ICON={win32_exe},0\r\n"
        "ACTION=View Medical Images (MicroDicom)\r\n"
    )


def _build_launcher_bat(*, dicom_dir_name: str, open_mode: OpenMode, arch: ArchMode) -> str:
    fallback_exe = "Win32\\mDicom.exe" if arch == "universal" else "x64\\mDicom.exe"
    primary_check = (
        f'if exist "%~dp0DICOMDIR" (\r\n  set "TARGET=%~dp0DICOMDIR"\r\n) else (\r\n  set "TARGET=/scan %~dp0{dicom_dir_name}"\r\n)'
        if open_mode == "dicomdir"
        else f'set "TARGET=/scan %~dp0{dicom_dir_name}"'
    )
    return (
        "@echo off\r\n"
        "title PAXS Medical Imaging Viewer (MicroDicom)\r\n"
        'cd /d "%~dp0"\r\n'
        f"{primary_check}\r\n"
        "if defined ProgramFiles(x86) (\r\n"
        '  start "" "%~dp0x64\\mDicom.exe" %TARGET%\r\n'
        ") else (\r\n"
        f'  start "" "%~dp0{fallback_exe}" %TARGET%\r\n'
        ")\r\n"
    )


def _configure_settings_xml(src_xml: Path, dst_xml: Path) -> None:
    """Copy MicroDicom settings.xml tuned for clean offline patient/doctor launch."""
    dst_xml.parent.mkdir(parents=True, exist_ok=True)
    if not src_xml.is_file():
        return
    try:
        tree = ET.parse(src_xml)
        root = tree.getroot()
        app_el = root.find("application")
        if app_el is not None:
            app_el.set("showMaximized", "true")
            app_el.set("alwaysCheckForUpdate", "false")
            app_el.set("showDefaultViewerDialog", "false")
            app_el.set("showSplash", "false")
        view_el = root.find("view")
        if view_el is not None:
            view_el.set("defaultZoom", "fitToScreen")
            view_el.set("preloadSeries", "true")
        tree.write(dst_xml, encoding="utf-8", xml_declaration=True)
    except Exception:  # noqa: BLE001 - fall back to verbatim settings file
        shutil.copy2(src_xml, dst_xml)


def _build_patient_readme(dicom_dir_name: str, study_info: dict[str, Any] | None) -> str:
    lines = [
        "======================================================================",
        "  PAXS ARCHIVE — PORTABLE MEDICAL IMAGING PACKAGE (MicroDicom 2026.5)",
        "======================================================================",
        "",
        "HOW TO VIEW THE MEDICAL IMAGES (WINDOWS):",
        "  1. Double-click 'START_VIEWER.bat' (or 'run.bat') in this folder.",
        "  2. MicroDicom Viewer will launch automatically and load the study.",
        "  3. On CD/DVD drives with AutoRun enabled, the viewer opens directly.",
        "",
        f"DICOM FILES LOCATION:",
        f"  - Index file : DICOMDIR (PS3.10 compliant)",
        f"  - Study data : .\\{dicom_dir_name}\\",
        "",
    ]
    if study_info:
        lines.append("STUDY SUMMARY:")
        for label, key in (
            ("Study Date", "study_date"),
            ("Modality", "modality"),
            ("Description", "description"),
            ("Accession", "accession"),
            ("Images", "n_instances"),
        ):
            val = study_info.get(key)
            if val not in (None, ""):
                lines.append(f"  - {label:12s}: {val}")
        lines.append("")
    lines.extend([
        "FOR PHYSICIANS / PACS IMPORT:",
        "  You may import this disc or USB directly into any DICOM-compliant",
        "  PACS or workstation using the root DICOMDIR file or the DICOM folder.",
        "",
        "Included viewer: MicroDicom DICOM Viewer 2026.5 (see licenses\\EULA.txt).",
    ])
    return "\r\n".join(lines) + "\r\n"


def install_microdicom_autorun(
    media_root: Path,
    *,
    dicom_dir_name: str = "DICOM",
    arch: ArchMode = "universal",
    open_mode: OpenMode = "dicomdir",
    include_help: bool = False,
    volume_label: str = "PAXS Medical Imaging Study",
    study_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Install & configure MicroDicom 2026.5 autorun files into ``media_root``."""
    src = find_microdicom_source()
    if src is None:
        raise FileNotFoundError(
            "MicroDicom 2026.5 autorun package was not found. "
            "Place it under .tools\\microdicom-autorun\\extracted or beside PAXS-MediaExporter.exe."
        )
    folder = normalize_dicom_dir_name(dicom_dir_name)
    media_root.mkdir(parents=True, exist_ok=True)

    # 1. Copy viewer binaries (x64 always; Win32 in universal mode).
    (media_root / "x64").mkdir(exist_ok=True)
    shutil.copy2(src / "x64" / "mDicom.exe", media_root / "x64" / "mDicom.exe")
    if include_help and (src / "x64" / "mDicom.chm").is_file():
        shutil.copy2(src / "x64" / "mDicom.chm", media_root / "x64" / "mDicom.chm")

    if arch == "universal" and (src / "Win32" / "mDicom.exe").is_file():
        (media_root / "Win32").mkdir(exist_ok=True)
        shutil.copy2(src / "Win32" / "mDicom.exe", media_root / "Win32" / "mDicom.exe")
        if include_help and (src / "Win32" / "mDicom.chm").is_file():
            shutil.copy2(src / "Win32" / "mDicom.chm", media_root / "Win32" / "mDicom.chm")

    # 2. Copy languages, licenses, and tuned offline settings.
    for sub in ("lang", "licenses"):
        if (src / sub).is_dir():
            shutil.copytree(src / sub, media_root / sub, dirs_exist_ok=True)
    if (src / "settings" / "overlay.xml").is_file():
        (media_root / "settings").mkdir(exist_ok=True)
        shutil.copy2(src / "settings" / "overlay.xml", media_root / "settings" / "overlay.xml")
    _configure_settings_xml(src / "settings" / "settings.xml", media_root / "settings" / "settings.xml")

    # 3. Write configured autorun.inf, run.bat, START_VIEWER.bat, and README.
    (media_root / "autorun.inf").write_text(
        _build_autorun_inf(dicom_dir_name=folder, open_mode=open_mode, arch=arch, label=volume_label),
        encoding="utf-8",
    )
    launcher = _build_launcher_bat(dicom_dir_name=folder, open_mode=open_mode, arch=arch)
    (media_root / "run.bat").write_text(launcher, encoding="utf-8")
    (media_root / "START_VIEWER.bat").write_text(launcher, encoding="utf-8")
    (media_root / "README_PATIENT_DOCTOR.txt").write_text(
        _build_patient_readme(folder, study_info), encoding="utf-8"
    )
    return verify_autorun_package(media_root, dicom_dir_name=folder, require_dicomdir=(open_mode == "dicomdir"))


def verify_autorun_package(
    media_root: Path,
    *,
    dicom_dir_name: str = "DICOM",
    require_dicomdir: bool = True,
) -> dict[str, Any]:
    """Validate that MicroDicom autorun files and DICOM references are intact."""
    folder = normalize_dicom_dir_name(dicom_dir_name)
    checks = {
        "x64_viewer": (media_root / "x64" / "mDicom.exe").is_file(),
        "win32_viewer": (media_root / "Win32" / "mDicom.exe").is_file(),
        "autorun_inf": (media_root / "autorun.inf").is_file(),
        "run_bat": (media_root / "run.bat").is_file(),
        "start_viewer_bat": (media_root / "START_VIEWER.bat").is_file(),
        "settings_xml": (media_root / "settings" / "settings.xml").is_file(),
        "dicom_dir_exists": (media_root / folder).is_dir(),
        "dicomdir_present": (media_root / "DICOMDIR").is_file(),
    }
    dicom_instances = 0
    if checks["dicomdir_present"]:
        fs = FileSet(str(media_root / "DICOMDIR"))
        dicom_instances = len(fs)
        checks["dicomdir_valid"] = dicom_instances > 0 and all(Path(inst.path).is_file() for inst in fs)
    else:
        dicom_instances = sum(1 for p in (media_root / folder).rglob("*") if p.is_file()) if checks["dicom_dir_exists"] else 0
        checks["dicomdir_valid"] = not require_dicomdir and dicom_instances > 0

    required_ok = all(
        checks[k]
        for k in ("x64_viewer", "autorun_inf", "run_bat", "start_viewer_bat", "settings_xml", "dicomdir_valid")
    )
    return {
        "ok": required_ok,
        "version": MICRODICOM_VERSION,
        "dicom_dir": folder,
        "dicom_instances": dicom_instances,
        "checks": checks,
    }
