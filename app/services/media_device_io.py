"""Standalone-safe media I/O helpers (Windows IMAPI2, USB detection, ISO, ZIP, SHA-256).

Deliberately depends ONLY on Python stdlib + ``pycdlib`` (zero SQLAlchemy,
FastAPI, or server storage imports) so ``PAXS-MediaExporter.exe`` remains
lightweight and fast to boot.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from typing import Any, Callable
from zipfile import ZipFile

import pycdlib

SECTOR_BYTES = 2048
DRIVE_REMOVABLE = 2

_IMAPI_SCRIPT = r"""
Option Explicit
Dim master, recorder, candidate, writer, image, result, stream
Dim i, volume, mode, drive, chosenDrive, count, mediaType, freeBlocks, supported, mediaError

mode = WScript.Arguments(0)
drive = UCase(WScript.Arguments(1))
Set master = CreateObject("IMAPI2.MsftDiscMaster2")
If Not master.IsSupportedEnvironment Then
    If mode = "list" Then WScript.Quit 0
    WScript.Quit 20
End If
Set writer = CreateObject("IMAPI2.MsftDiscFormat2Data")
count = 0
For i = 0 To master.Count - 1
    Set candidate = CreateObject("IMAPI2.MsftDiscRecorder2")
    candidate.InitializeDiscRecorder master.Item(i)
    If writer.IsRecorderSupported(candidate) Then
        For Each volume In candidate.VolumePathNames
            If Len(volume) = 3 And Mid(volume, 2) = ":\" Then
                If mode = "list" Then
                    WScript.Echo UCase(Left(volume, 2)) & vbTab & Trim(candidate.VendorId & " " & candidate.ProductId)
                    Exit For
                ElseIf drive = "" Or UCase(Left(volume, 2)) = drive Then
                    Set recorder = candidate
                    chosenDrive = UCase(Left(volume, 2))
                    count = count + 1
                    Exit For
                End If
            End If
        Next
    End If
Next
If mode = "list" Then WScript.Quit 0
If count = 0 Then WScript.Quit 20
If count > 1 Then WScript.Quit 21

writer.ClientName = "PAXS Media Exporter"
Set writer.Recorder = recorder
If mode = "write" Then recorder.AcquireExclusiveAccess False, writer.ClientName
On Error Resume Next
supported = writer.IsCurrentMediaSupported(recorder)
mediaError = Err.Number
On Error GoTo 0
If mediaError = &HC0AA0202 Then WScript.Quit 25
If mediaError <> 0 Then WScript.Quit 26
If Not supported Then WScript.Quit 22
mediaType = writer.CurrentPhysicalMediaType
Select Case mediaType
    Case 2, 3, 6, 7, 8, 9, 10, 11, 13
    Case Else
        WScript.Quit 22
End Select
If Not writer.MediaHeuristicallyBlank Then WScript.Quit 23
freeBlocks = writer.FreeSectorsOnMedia
If freeBlocks <= 0 Then WScript.Quit 22
If mode = "check" Then
    WScript.Echo chosenDrive
    WScript.Echo mediaType
    WScript.Echo freeBlocks
    WScript.Quit 0
End If

Set image = CreateObject("IMAPI2FS.MsftFileSystemImage")
image.ChooseImageDefaultsForMediaType mediaType
If mediaType = 2 Or mediaType = 3 Then
    image.FileSystemsToCreate = 1
Else
    image.FileSystemsToCreate = 4
    image.UDFRevision = &H102
End If
image.FreeMediaBlocks = freeBlocks
image.VolumeName = WScript.Arguments(3)
image.StageFiles = False
On Error Resume Next
image.Root.AddTree WScript.Arguments(2), False
If Err.Number = 0 Then Set result = image.CreateResultImage()
mediaError = Err.Number
On Error GoTo 0
If mediaError = &HC0AAB121 Then WScript.Quit 24
If mediaError <> 0 Then WScript.Quit 27
If result.TotalBlocks > freeBlocks Then WScript.Quit 24
writer.ForceMediaToBeClosed = True
Set stream = result.ImageStream
writer.Write stream
recorder.ReleaseExclusiveAccess
"""

_MEDIA_ERRORS = {
    20: "No supported CD/DVD recorder was found. Check the drive letter and connection.",
    21: "Multiple CD/DVD recorders were found. Select a specific drive before burning.",
    22: "Insert a compatible blank writable CD or DVD in the recorder.",
    23: "The disc already contains data. Insert a blank writable CD or DVD.",
    24: "The study + MicroDicom package does not fit on this disc. Try 64-bit Compact mode or a DVD.",
    25: "The recorder tray is empty. Insert a blank writable CD or DVD.",
    26: "Windows could not read the media status. Check the disc and close the tray.",
    27: "Windows could not build a disc image from the prepared study.",
}
_MEDIA_TYPES = {
    2: "CD-R", 3: "CD-RW", 6: "DVD+R", 7: "DVD+RW", 8: "DVD+R DL",
    9: "DVD-R", 10: "DVD-RW", 11: "DVD-R DL", 13: "DVD+RW DL",
}


class MediaIOError(Exception):
    """Operator-safe optical/removable media error."""


def _imapi(script: Path, mode: str, drive: str = "", *options: str) -> list[str]:
    if os.name != "nt":
        raise MediaIOError("Direct CD/DVD burning requires Windows IMAPI2.")
    cscript = Path(os.environ["SystemRoot"]) / "System32" / "cscript.exe"
    args = [str(cscript), "//B", "//NoLogo", str(script), mode, drive.upper(), *options]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, errors="replace",
            timeout=3600 if mode == "write" else 30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise MediaIOError("Optical drive timed out. Check the disc and retry.") from None
    except OSError:
        raise MediaIOError("Windows disc burning is unavailable on this machine.") from None
    if proc.returncode:
        raise MediaIOError(_MEDIA_ERRORS.get(proc.returncode, "Could not access or write the optical disc."))
    return proc.stdout.splitlines()


def list_recorders() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    with TemporaryDirectory(prefix="paxs-rec-") as tmp:
        script = Path(tmp) / "rec.vbs"
        script.write_text(_IMAPI_SCRIPT, encoding="ascii")
        lines = _imapi(script, "list")
    out: list[dict[str, str]] = []
    for line in lines:
        drive, _, model = line.partition("\t")
        if re.fullmatch(r"[A-Z]:", drive):
            out.append({"drive": drive, "model": model.strip() or "Optical CD/DVD Recorder"})
    return sorted(out, key=lambda r: r["drive"])


def _volume_meta_win32(root_path: str) -> tuple[str, str]:
    vol = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root_path), vol, len(vol), None, None, None, fs, len(fs)
    )
    return (vol.value.strip(), fs.value.strip()) if ok else ("", "")


def list_usb_drives() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    k32 = ctypes.windll.kernel32
    prev = ctypes.c_uint(0)
    k32.SetThreadErrorMode(0x0001, ctypes.byref(prev))
    drives: list[dict[str, Any]] = []
    try:
        mask = int(k32.GetLogicalDrives())
        for idx in range(26):
            if not (mask & (1 << idx)):
                continue
            letter = f"{chr(65 + idx)}:"
            root = f"{letter}\\"
            if int(k32.GetDriveTypeW(ctypes.c_wchar_p(root))) != DRIVE_REMOVABLE:
                continue
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            label, fs_name = _volume_meta_win32(root)
            drives.append({
                "drive": letter,
                "label": label or "Removable USB Drive",
                "filesystem": fs_name or "USB",
                "free_bytes": int(usage.free),
                "total_bytes": int(usage.total),
            })
    finally:
        k32.SetThreadErrorMode(prev.value, None)
    return drives


def list_removable_devices() -> dict[str, Any]:
    recorders: list[dict[str, str]] = []
    err: str | None = None
    try:
        recorders = list_recorders()
    except MediaIOError as exc:
        err = str(exc)
    return {
        "platform": os.name,
        "recorders": recorders,
        "recorder_error": err,
        "usb_drives": list_usb_drives(),
    }


def check_media(drive: str) -> dict[str, str | int]:
    with TemporaryDirectory(prefix="paxs-chk-") as tmp:
        script = Path(tmp) / "chk.vbs"
        script.write_text(_IMAPI_SCRIPT, encoding="ascii")
        lines = _imapi(script, "check", drive)
    if len(lines) != 3 or not re.fullmatch(r"[A-Z]:", lines[0]):
        raise MediaIOError("Windows returned an invalid optical recorder status.")
    name = _MEDIA_TYPES.get(int(lines[1]))
    sectors = int(lines[2])
    if not name or sectors <= 0:
        raise MediaIOError(_MEDIA_ERRORS[22])
    return {
        "drive": lines[0],
        "media_type": name,
        "available_bytes": sectors * SECTOR_BYTES,
        "staging_free_bytes": shutil.disk_usage(gettempdir()).free,
    }


def extract_zip_safely(zip_path: Path, destination: Path) -> Path:
    dest_root = destination.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            parts = [p for p in Path(info.filename.replace("\\", "/")).parts if p not in ("", ".")]
            if not parts or any(p == ".." or ":" in p for p in parts):
                raise MediaIOError("Archive contains an unsafe path.")
            target = (dest_root / Path(*parts)).resolve()
            if not target.is_relative_to(dest_root):
                raise MediaIOError("Archive path escapes staging root.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
    return dest_root


def verify_manifest(root: Path, manifest: dict[Path, bytes]) -> None:
    for rel, expected in manifest.items():
        try:
            with (root / rel).open("rb") as fh:
                ok = hashlib.file_digest(fh, "sha256").digest() == expected
        except OSError:
            ok = False
        if not ok:
            raise MediaIOError(f"SHA-256 verification failed for {rel}.")


def burn_directory(
    drive: str, media_dir: Path, set_phase: Callable[[str], None], *, volume_label: str
) -> None:
    manifest: dict[Path, bytes] = {}
    for file_path in sorted(media_dir.rglob("*")):
        if file_path.is_file():
            with file_path.open("rb") as fh:
                manifest[file_path.relative_to(media_dir)] = hashlib.file_digest(fh, "sha256").digest()
    if Path("DICOMDIR") not in manifest:
        raise MediaIOError("Prepared media folder is missing DICOMDIR.")
    total_size = sum((media_dir / rel).stat().st_size for rel in manifest)
    with TemporaryDirectory(prefix="paxs-burn-") as tmp:
        script = Path(tmp) / "burn.vbs"
        script.write_text(_IMAPI_SCRIPT, encoding="ascii")
        lines = _imapi(script, "check", drive)
        selected_drive, capacity = lines[0], int(lines[2]) * SECTOR_BYTES
        if total_size > capacity:
            raise MediaIOError(_MEDIA_ERRORS[24])
        set_phase("writing")
        _imapi(script, "write", selected_drive, str(media_dir), volume_label)
        set_phase("verifying")
        root = Path(selected_drive + "\\")
        for _ in range(20):
            if (root / "DICOMDIR").is_file():
                break
            time.sleep(1)
        verify_manifest(root, manifest)


def _iso_entry_path(rel_parts: tuple[str, ...], *, is_dir: bool) -> str:
    cleaned = ["".join(ch if ch.isalnum() or ch in "_." else "_" for ch in part.upper()) for part in rel_parts]
    joined = "/" + "/".join(cleaned)
    if is_dir:
        return joined
    last = cleaned[-1]
    return joined + (";1" if "." in last else ".;1")


def write_iso_image(fileset: Path, target: Path, volume_label: str = "PAXS_DICOM") -> None:
    image = pycdlib.PyCdlib()
    safe_vol = re.sub(r"[^A-Z0-9_]+", "_", (volume_label or "PAXS_DICOM").upper())[:16] or "PAXS_DICOM"
    image.new(interchange_level=3, joliet=3, vol_ident=safe_vol)
    try:
        for path in sorted(fileset.rglob("*")):
            rel = path.relative_to(fileset)
            joliet = "/" + rel.as_posix()
            if path.is_dir():
                image.add_directory(iso_path=_iso_entry_path(rel.parts, is_dir=True), joliet_path=joliet)
            else:
                image.add_file(str(path), iso_path=_iso_entry_path(rel.parts, is_dir=False), joliet_path=joliet)
        image.write(str(target))
    finally:
        image.close()
