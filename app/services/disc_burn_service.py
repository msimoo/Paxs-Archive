"""Stage a DICOM file-set, burn it with Windows IMAPI, and verify the disc."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from typing import Callable

from pydicom import dcmread
from pydicom.fileset import FileSet

from app.services import storage_service

log = logging.getLogger("paxs.disc_burn")
SECTOR_BYTES = 2048

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
    20: "No supported CD/DVD recorder was found on the server. Check the drive letter and connection.",
    21: "Multiple CD/DVD recorders were found. Set the drive letter in Settings.",
    22: "Insert a compatible blank writable CD or DVD in the server recorder.",
    23: "The disc already contains data. Insert a blank writable CD or DVD.",
    24: "The complete DICOM study does not fit on this disc. Insert a higher-capacity disc.",
    25: "The recorder is empty. Insert a blank writable CD or DVD.",
    26: "Windows could not read the media status. Check the disc and close the tray.",
    27: "Windows could not build a disc image from the prepared study.",
}
_MEDIA_TYPES = {
    2: "CD-R", 3: "CD-RW", 6: "DVD+R", 7: "DVD+RW", 8: "DVD+R DL",
    9: "DVD-R", 10: "DVD-RW", 11: "DVD-R DL", 13: "DVD+RW DL",
}


class DiscBurnError(Exception):
    """A safe, operator-facing burn failure."""


@dataclass(frozen=True)
class DiscInstance:
    sop_uid: str
    series_uid: str
    path: str
    backend: str | None
    size: int


def _imapi(script: Path, mode: str, drive: str = "", *options: str) -> list[str]:
    if os.name != "nt":
        raise DiscBurnError("CD/DVD burning requires a Windows PAXS server.")
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
        raise DiscBurnError("Windows disc burning is unavailable on this server.") from None
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
    """List server-side recorders through the same IMAPI writer used for a burn."""
    with TemporaryDirectory(prefix="paxs-disc-check-") as temp:
        script = Path(temp) / "check-disc.vbs"
        script.write_text(_IMAPI_SCRIPT, encoding="ascii")
        output = _imapi(script, "list")
    recorders = []
    for line in output:
        drive, _, model = line.partition("\t")
        if not re.fullmatch(r"[A-Z]:", drive):
            raise DiscBurnError("Windows returned an invalid recorder list.")
        recorders.append({"drive": drive, "model": model.strip()})
    return sorted(recorders, key=lambda recorder: recorder["drive"])


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


def stage_study(
    workspace: Path, study_uid: str, instances: list[DiscInstance], capacity: int,
) -> tuple[Path, dict[Path, bytes]]:
    """Create DICOMDIR with pydicom and retain the exact source instance bytes."""
    if not instances:
        raise DiscBurnError("This study has no stored DICOM instances.")
    originals = workspace / "originals"
    originals.mkdir()
    media = workspace / "media"
    media.mkdir()
    fileset = FileSet()
    staged = {}
    series_numbers: dict[str, int] = {}
    patient_id = None
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
            if source.size > 0 and size != source.size:
                raise DiscBurnError("A stored DICOM object has an unexpected size. No disc was written.")
            ds = dcmread(original, stop_before_pixels=True)
            if (ds.StudyInstanceUID != study_uid or ds.SeriesInstanceUID != source.series_uid
                    or ds.SOPInstanceUID != source.sop_uid
                    or ds.file_meta.MediaStorageSOPInstanceUID != source.sop_uid
                    or ds.file_meta.MediaStorageSOPClassUID != ds.SOPClassUID):
                raise DiscBurnError("A stored DICOM object does not match the selected study. No disc was written.")
            if not ds.get("StudyDate") or not ds.get("StudyTime"):
                raise DiscBurnError("Study date or time is missing from a stored DICOM object. No disc was written.")
            if patient_id is None:
                patient_id = ds.PatientID
            elif ds.PatientID != patient_id:
                raise DiscBurnError("Stored instances have conflicting patient IDs. No disc was written.")
            # Fill missing catalog identifiers without modifying the archived objects.
            if not ds.get("StudyID"):
                ds.StudyID = study_id
            if ds.get("SeriesNumber") in (None, ""):
                ds.SeriesNumber = series_numbers.setdefault(source.series_uid, len(series_numbers) + 1)
            if ds.get("InstanceNumber") in (None, ""):
                ds.InstanceNumber = index + 1
            for keyword in ("PatientName", "StudyDescription", "AccessionNumber"):
                if keyword not in ds:
                    setattr(ds, keyword, "")
            fileset.add(ds)
        except DiscBurnError:
            raise
        except Exception as exc:
            log.warning("Failed to stage DICOM object error_type=%s", type(exc).__name__)
            raise DiscBurnError(
                "A stored DICOM object could not be read or indexed. No disc was written."
            ) from None
        remaining -= ((size + SECTOR_BYTES - 1) // SECTOR_BYTES) * SECTOR_BYTES
        staged[source.sop_uid] = (original, digest.digest())

    fileset.write(media)
    manifest = {}
    for instance in fileset:
        original, digest = staged[instance.SOPInstanceUID]
        relative = Path(instance.path).relative_to(media)
        original.replace(media / relative)
        manifest[relative] = digest
    if len(manifest) != len(instances):
        raise DiscBurnError("The DICOMDIR index does not contain every study instance. No disc was written.")
    with (media / "DICOMDIR").open("rb") as directory:
        manifest[Path("DICOMDIR")] = hashlib.file_digest(directory, "sha256").digest()
    return media, manifest


def verify_disc(root: Path, manifest: dict[Path, bytes]) -> None:
    for path, expected in manifest.items():
        try:
            with (root / path).open("rb") as recorded:
                matches = hashlib.file_digest(recorded, "sha256").digest() == expected
        except OSError:
            matches = False
        if not matches:
            raise DiscBurnError("Disc verification failed. The written disc could not be verified in full.")


def burn_study(
    drive: str, study_uid: str, instances: list[DiscInstance], set_phase: Callable[[str], None],
    *, volume_label: str,
) -> None:
    with TemporaryDirectory(prefix="paxs-disc-") as temp:
        workspace = Path(temp)
        script = workspace / "write-disc.vbs"
        script.write_text(_IMAPI_SCRIPT, encoding="ascii")
        selected_drive, _, capacity = _check_media(script, drive)
        study_size = sum(source.size for source in instances)
        if study_size > capacity:
            raise DiscBurnError(_MEDIA_ERRORS[24])
        if shutil.disk_usage(workspace).free < study_size:
            raise DiscBurnError("The server does not have enough temporary disk space to prepare this study.")
        set_phase("preparing")
        media, manifest = stage_study(workspace, study_uid, instances, capacity)
        set_phase("writing")
        _imapi(script, "write", selected_drive, str(media), volume_label)
        set_phase("verifying")
        root = Path(selected_drive + "\\")
        # Windows may take a few seconds to mount the newly closed session.
        for _ in range(20):
            if (root / "DICOMDIR").is_file():
                break
            time.sleep(1)
        verify_disc(root, manifest)
