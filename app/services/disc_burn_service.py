"""Stage a DICOM file-set, burn it with Windows IMAPI, or export directly to USB."""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from typing import Any, Callable
from zipfile import ZipFile

from pydicom import dcmread
from pydicom.fileset import FileSet

from app.services import storage_service

log = logging.getLogger("paxs.disc_burn")
SECTOR_BYTES = 2048
DRIVE_REMOVABLE = 2

# Use WSH to pass IMAPI's native image stream directly to the writer.
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

writer.ClientName = "PAXS Archive"
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
    24: "The complete DICOM study does not fit on this disc. Insert a higher-capacity disc.",
    25: "The recorder tray is empty. Insert a blank writable CD or DVD.",
    26: "Windows could not read the media status. Check the disc and close the tray.",
    27: "Windows could not build a disc image from the prepared study.",
}
_MEDIA_TYPES = {
    2: "CD-R", 3: "CD-RW", 6: "DVD+R", 7: "DVD+RW", 8: "DVD+R DL",
    9: "DVD-R", 10: "DVD-RW", 11: "DVD-R DL", 13: "DVD+RW DL",
}


class DiscBurnError(Exception):
    """A safe, operator-facing burn or removable-media export failure."""


@dataclass(frozen=True)
class DiscInstance:
    sop_uid: str
    series_uid: str
    path: str
    backend: str | None
    size: int


def _imapi(script: Path, mode: str, drive: str = "", *options: str) -> list[str]:
    if os.name != "nt":
        raise DiscBurnError("Direct CD/DVD burning requires Windows IMAPI.")
    cscript = Path(os.environ["SystemRoot"]) / "System32" / "cscript.exe"
    args = [str(cscript), "//B", "//NoLogo", str(script), mode, drive.upper(), *options]
    try:
        process = subprocess.run(
            args, capture_output=True, text=True, errors="replace",
            timeout=60 * 60 if mode == "write" else 30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise DiscBurnError("The drive timed out. Check the recorder and disc before retrying.") from None
    except OSError:
        raise DiscBurnError("Windows disc burning is unavailable on this machine.") from None
    if process.returncode:
        log.warning("IMAPI failed return_code=%d", process.returncode)
        raise DiscBurnError(_MEDIA_ERRORS.get(
            process.returncode,
            "Windows could not access or write the disc. Check the recorder and use a blank disc.",
        ))
    return process.stdout.splitlines()


def _check_media(script: Path, drive: str) -> tuple[str, str, int]:
    output = _imapi(script, "check", drive)
    if (len(output) != 3 or not re.fullmatch(r"[A-Z]:", output[0])
            or not re.fullmatch(r"[0-9]{1,2}", output[1])
            or not re.fullmatch(r"[0-9]{1,10}", output[2])):
        raise DiscBurnError("Windows returned an invalid recorder status.")
    selected_drive, media_type, sectors = output
    name = _MEDIA_TYPES.get(int(media_type))
    if not name or int(sectors) <= 0:
        raise DiscBurnError(_MEDIA_ERRORS[22])
    return selected_drive, name, int(sectors) * SECTOR_BYTES


def list_recorders() -> list[dict[str, str]]:
    """List optical CD/DVD recorders via Windows IMAPI2."""
    if os.name != "nt":
        return []
    with TemporaryDirectory(prefix="paxs-disc-check-") as temp:
        script = Path(temp) / "check-disc.vbs"
        script.write_text(_IMAPI_SCRIPT, encoding="ascii")
        output = _imapi(script, "list")
    recorders: list[dict[str, str]] = []
    for line in output:
        drive, _, model = line.partition("\t")
        if not re.fullmatch(r"[A-Z]:", drive):
            raise DiscBurnError("Windows returned an invalid recorder list.")
        recorders.append({"drive": drive, "model": model.strip() or "Optical CD/DVD Recorder"})
    return sorted(recorders, key=lambda recorder: recorder["drive"])


def _volume_metadata_win32(root_path: str) -> tuple[str, str]:
    """Read volume label and filesystem name without popping OS media dialogs."""
    vol_buf = ctypes.create_unicode_buffer(261)
    fs_buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root_path), vol_buf, len(vol_buf),
        None, None, None, fs_buf, len(fs_buf),
    )
    if not ok:
        return "", ""
    return vol_buf.value.strip(), fs_buf.value.strip()


def list_usb_drives() -> list[dict[str, Any]]:
    """Detect connected removable USB drives on Windows (drive letter, label, space)."""
    if os.name != "nt":
        return []
    kernel32 = ctypes.windll.kernel32
    # Suppress Windows "Insert a disk into drive X:" modal popups for empty card readers.
    prev_mode = ctypes.c_uint(0)
    kernel32.SetThreadErrorMode(0x0001, ctypes.byref(prev_mode))
    drives: list[dict[str, Any]] = []
    try:
        bitmask = int(kernel32.GetLogicalDrives())
        for idx in range(26):
            if not (bitmask & (1 << idx)):
                continue
            letter = f"{chr(65 + idx)}:"
            root = f"{letter}\\"
            if int(kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))) != DRIVE_REMOVABLE:
                continue
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            label, fs_name = _volume_metadata_win32(root)
            drives.append({
                "drive": letter,
                "label": label or "Removable USB Drive",
                "filesystem": fs_name or "USB",
                "free_bytes": int(usage.free),
                "total_bytes": int(usage.total),
            })
    finally:
        kernel32.SetThreadErrorMode(prev_mode.value, None)
    return drives


def list_removable_devices() -> dict[str, Any]:
    """Return both optical CD/DVD recorders and removable USB drives on this machine."""
    recorders: list[dict[str, str]] = []
    recorder_error: str | None = None
    try:
        recorders = list_recorders()
    except DiscBurnError as exc:
        recorder_error = str(exc)
    return {
        "platform": os.name,
        "recorders": recorders,
        "recorder_error": recorder_error,
        "usb_drives": list_usb_drives(),
    }


def check_media(drive: str) -> dict[str, str | int]:
    """Report write readiness without acquiring or changing the disc."""
    with TemporaryDirectory(prefix="paxs-disc-check-") as temp:
        script = Path(temp) / "check-disc.vbs"
        script.write_text(_IMAPI_SCRIPT, encoding="ascii")
        selected_drive, media_type, available = _check_media(script, drive)
    return {
        "drive": selected_drive,
        "media_type": media_type,
        "available_bytes": available,
        "staging_free_bytes": shutil.disk_usage(gettempdir()).free,
    }


def _prepare_catalog_dataset(
    original: Path, source: DiscInstance, study_uid: str,
    study_id: str, index: int, series_numbers: dict[str, int],
) -> Any:
    """Read DICOM header and supply safe catalog fallbacks without mutating stored pixels."""
    ds = dcmread(original, stop_before_pixels=True)
    if (str(getattr(ds, "StudyInstanceUID", "")) != study_uid
            or str(getattr(ds, "SeriesInstanceUID", "")) != source.series_uid
            or str(getattr(ds, "SOPInstanceUID", "")) != source.sop_uid):
        raise DiscBurnError("A stored DICOM object does not match the selected study. No media was written.")
    if not getattr(ds.file_meta, "MediaStorageSOPInstanceUID", None):
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    if not getattr(ds.file_meta, "MediaStorageSOPClassUID", None):
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    if (str(ds.file_meta.MediaStorageSOPInstanceUID) != source.sop_uid
            or str(ds.file_meta.MediaStorageSOPClassUID) != str(ds.SOPClassUID)):
        raise DiscBurnError("A stored DICOM object has inconsistent file meta UIDs. No media was written.")
    if not ds.get("StudyDate"):
        ds.StudyDate = "19700101"
    if not ds.get("StudyTime"):
        ds.StudyTime = "000000"
    if not ds.get("PatientID"):
        ds.PatientID = "UNKNOWN"
    if not ds.get("StudyID"):
        ds.StudyID = study_id
    if ds.get("SeriesNumber") in (None, ""):
        ds.SeriesNumber = series_numbers.setdefault(source.series_uid, len(series_numbers) + 1)
    if ds.get("InstanceNumber") in (None, ""):
        ds.InstanceNumber = index + 1
    for keyword in ("PatientName", "StudyDescription", "AccessionNumber"):
        if keyword not in ds:
            setattr(ds, keyword, "")
    return ds


def stage_study(
    workspace: Path, study_uid: str, instances: list[DiscInstance], capacity: int,
) -> tuple[Path, dict[Path, bytes]]:
    """Create DICOMDIR with pydicom and retain the exact source instance bytes."""
    if not instances:
        raise DiscBurnError("This study has no stored DICOM instances.")
    workspace = workspace.resolve()
    originals = workspace / "originals"
    originals.mkdir(exist_ok=True)
    media = workspace / "media"
    media.mkdir(exist_ok=True)
    fileset = FileSet()
    staged: dict[str, tuple[Path, bytes]] = {}
    series_numbers: dict[str, int] = {}
    study_id = hashlib.sha256(study_uid.encode()).hexdigest()[:16].upper()
    remaining = capacity

    for index, source in enumerate(instances):
        original = originals / str(index)
        digest = hashlib.sha256()
        size = 0
        try:
            with storage_service.read_stream(source.path, backend_name=source.backend) as src, original.open("wb") as dst:
                while chunk := src.read(1024 * 1024):
                    size += len(chunk)
                    if size > remaining:
                        raise DiscBurnError(_MEDIA_ERRORS[24])
                    dst.write(chunk)
                    digest.update(chunk)
            if size <= 0:
                raise DiscBurnError("A stored DICOM object is empty. No media was written.")
            ds = _prepare_catalog_dataset(original, source, study_uid, study_id, index, series_numbers)
            fileset.add(ds)
        except DiscBurnError:
            raise
        except Exception as exc:
            log.warning("Failed to stage DICOM object error_type=%s", type(exc).__name__)
            raise DiscBurnError("A stored DICOM object could not be read or indexed. No media was written.") from None
        remaining -= ((size + SECTOR_BYTES - 1) // SECTOR_BYTES) * SECTOR_BYTES
        staged[source.sop_uid] = (original, digest.digest())

    fileset.write(media)
    manifest: dict[Path, bytes] = {}
    for instance in fileset:
        original, digest_bytes = staged[instance.SOPInstanceUID]
        relative = Path(instance.path).relative_to(media)
        original.replace(media / relative)
        manifest[relative] = digest_bytes
    if len(manifest) != len(instances):
        raise DiscBurnError("The DICOMDIR index does not contain every study instance. No media was written.")
    with (media / "DICOMDIR").open("rb") as directory:
        manifest[Path("DICOMDIR")] = hashlib.file_digest(directory, "sha256").digest()
    return media, manifest


def verify_disc(root: Path, manifest: dict[Path, bytes]) -> None:
    """Verify every written file and DICOMDIR against its pre-write SHA-256."""
    for path, expected in manifest.items():
        try:
            with (root / path).open("rb") as recorded:
                matches = hashlib.file_digest(recorded, "sha256").digest() == expected
        except OSError:
            matches = False
        if not matches:
            raise DiscBurnError("Media verification failed. Written files could not be verified in full.")


def _manifest_for_directory(media_root: Path) -> dict[Path, bytes]:
    manifest: dict[Path, bytes] = {}
    for file_path in sorted(media_root.rglob("*")):
        if file_path.is_file():
            with file_path.open("rb") as fh:
                manifest[file_path.relative_to(media_root)] = hashlib.file_digest(fh, "sha256").digest()
    return manifest


def burn_directory(
    drive: str, media_dir: Path, set_phase: Callable[[str], None], *, volume_label: str,
) -> None:
    """Burn an already-staged DICOMDIR directory to a local Windows optical recorder."""
    manifest = _manifest_for_directory(media_dir)
    if Path("DICOMDIR") not in manifest:
        raise DiscBurnError("Prepared media folder is missing DICOMDIR.")
    total_size = sum((media_dir / rel).stat().st_size for rel in manifest)
    with TemporaryDirectory(prefix="paxs-disc-burn-") as temp:
        script = Path(temp) / "write-disc.vbs"
        script.write_text(_IMAPI_SCRIPT, encoding="ascii")
        selected_drive, _, capacity = _check_media(script, drive)
        if total_size > capacity:
            raise DiscBurnError(_MEDIA_ERRORS[24])
        set_phase("writing")
        _imapi(script, "write", selected_drive, str(media_dir), volume_label)
        set_phase("verifying")
        root = Path(selected_drive + "\\")
        for _ in range(20):
            if (root / "DICOMDIR").is_file():
                break
            time.sleep(1)
        verify_disc(root, manifest)


def burn_study(
    drive: str, study_uid: str, instances: list[DiscInstance], set_phase: Callable[[str], None],
    *, volume_label: str,
) -> None:
    """Stage a study from server storage, burn it via IMAPI, and verify the disc."""
    with TemporaryDirectory(prefix="paxs-disc-") as temp:
        workspace = Path(temp)
        script = workspace / "write-disc.vbs"
        script.write_text(_IMAPI_SCRIPT, encoding="ascii")
        selected_drive, _, capacity = _check_media(script, drive)
        study_size = sum(source.size for source in instances)
        if study_size > capacity:
            raise DiscBurnError(_MEDIA_ERRORS[24])
        if shutil.disk_usage(workspace).free < study_size:
            raise DiscBurnError("Not enough temporary disk space to prepare this study.")
        set_phase("preparing")
        media, manifest = stage_study(workspace, study_uid, instances, capacity)
        set_phase("writing")
        _imapi(script, "write", selected_drive, str(media), volume_label)
        set_phase("verifying")
        root = Path(selected_drive + "\\")
        for _ in range(20):
            if (root / "DICOMDIR").is_file():
                break
            time.sleep(1)
        verify_disc(root, manifest)


def _safe_folder_name(raw: str | None, study_uid: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (raw or "").strip()).strip("._-")
    if cleaned:
        return cleaned[:48]
    tail = re.sub(r"[^A-Za-z0-9]+", "", study_uid)[-12:] or "STUDY"
    return f"PAXS_DICOM_{tail}"


def _copy_verified_tree(source_dir: Path, destination_dir: Path, manifest: dict[Path, bytes]) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    for rel in sorted(manifest):
        dest = destination_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_dir / rel, dest)


def export_study_to_folder(
    target_dir: Path, study_uid: str, instances: list[DiscInstance],
    set_phase: Callable[[str], None], *, folder_name: str | None = None,
) -> str:
    """Stage a complete DICOMDIR fileset directly from local storage into a folder and verify it."""
    if not target_dir.exists() or not target_dir.is_dir():
        raise DiscBurnError("Selected destination folder is not accessible.")
    free_bytes = int(shutil.disk_usage(target_dir).free)
    study_size = sum(source.size for source in instances)
    if study_size > free_bytes:
        raise DiscBurnError("The selected destination drive does not have enough free space.")
    dest_root = target_dir / _safe_folder_name(folder_name, study_uid)
    stage_parent = target_dir / f".paxs-stage-{uuid.uuid4().hex[:8]}"
    stage_parent.mkdir(parents=True, exist_ok=True)
    try:
        set_phase("preparing")
        media, manifest = stage_study(stage_parent, study_uid, instances, free_bytes)
        set_phase("writing")
        if dest_root.exists():
            shutil.rmtree(dest_root)
        media.rename(dest_root)
        set_phase("verifying")
        verify_disc(dest_root, manifest)
        return str(dest_root)
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)


def export_study_to_usb(
    drive: str, study_uid: str, instances: list[DiscInstance],
    set_phase: Callable[[str], None], *, folder_name: str | None = None,
) -> str:
    """Stage a complete DICOMDIR fileset and copy/verify it onto a connected USB drive."""
    target_drive = drive.strip().upper()
    detected = {item["drive"]: item for item in list_usb_drives()}
    if target_drive not in detected:
        raise DiscBurnError(f"Removable USB drive {target_drive} is not connected.")
    return export_study_to_folder(
        Path(target_drive + "\\"), study_uid, instances, set_phase, folder_name=folder_name
    )


def extract_zip_safely(zip_path: Path, destination: Path) -> Path:
    """Safely extract a downloaded DICOMDIR ZIP without path traversal."""
    dest_resolved = destination.resolve()
    dest_resolved.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            target = (dest_resolved / member.filename).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError:
                raise DiscBurnError("Downloaded package contained an unsafe file path.") from None
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
    return dest_resolved


def _inspect_dicomdir_zip(zip_path: Path) -> int:
    """Validate that a downloaded ZIP contains DICOMDIR and safe paths; return total bytes."""
    total_bytes = 0
    has_dicomdir = False
    with ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            normalized = Path(member.filename)
            if normalized.is_absolute() or ".." in normalized.parts:
                raise DiscBurnError("Downloaded package contained an unsafe file path.")
            if normalized.as_posix().upper() == "DICOMDIR":
                has_dicomdir = True
            total_bytes += max(0, int(member.file_size))
    if not has_dicomdir:
        raise DiscBurnError("Downloaded package is missing DICOMDIR.")
    return total_bytes


def write_zip_to_target(
    zip_path: Path, target_dir: Path, set_phase: Callable[[str], None],
    *, folder_name: str, require_removable_drive: str | None = None,
) -> str:
    """Extract a downloaded DICOMDIR ZIP directly onto a local USB drive or folder and verify it.

    Extracting directly into a temporary staging folder on ``target_dir`` avoids
    creating a second full-study copy on ``C:\\`` ``%TEMP%`` and halves USB I/O.
    """
    if require_removable_drive:
        letter = require_removable_drive.strip().upper()
        detected = {item["drive"]: item for item in list_usb_drives()}
        if letter not in detected:
            raise DiscBurnError(f"Removable USB drive {letter} is not connected.")
        target_dir = Path(letter + "\\")
    if not target_dir.exists() or not target_dir.is_dir():
        raise DiscBurnError("Selected destination folder is not accessible.")

    set_phase("preparing")
    needed = _inspect_dicomdir_zip(zip_path)
    if shutil.disk_usage(target_dir).free < needed:
        raise DiscBurnError("Destination drive does not have enough free space for this study.")

    dest_root = target_dir / _safe_folder_name(folder_name, "STUDY")
    stage_dir = target_dir / f".paxs-stage-{uuid.uuid4().hex[:8]}"
    try:
        set_phase("writing")
        extract_zip_safely(zip_path, stage_dir)
        manifest = _manifest_for_directory(stage_dir)
        if Path("DICOMDIR") not in manifest:
            raise DiscBurnError("Downloaded package is missing DICOMDIR.")
        if dest_root.exists():
            shutil.rmtree(dest_root)
        stage_dir.rename(dest_root)
        set_phase("verifying")
        verify_disc(dest_root, manifest)
        return str(dest_root)
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
