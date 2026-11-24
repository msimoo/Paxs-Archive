"""Study-level C-MOVE with a temporary, dedicated DCMTK storage receiver."""

import os
import re
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from pydicom import dcmread

from app.core.config import get_settings
from app.core.dicom_uids import check_uid, validate_mandatory_uids
from app.dicom import dcmtk

_RECEIVER_LOCK = Lock()


def configured_for(trust_cert=None) -> bool:
    settings = get_settings()
    return bool(
        dcmtk.enabled_for(trust_cert)
        and settings.DCMTK_MOVE_PORT
        and settings.DCMTK_MOVE_PORT != settings.DICOM_PORT
    )


def availability(trust_cert=None) -> str | None:
    settings = get_settings()
    if not dcmtk.enabled_for(trust_cert):
        return "DCMTK C-MOVE requires a configured non-TLS peer."
    if not settings.DCMTK_MOVE_PORT or settings.DCMTK_MOVE_PORT == settings.DICOM_PORT:
        return "Configure a dedicated DCMTK_MOVE_PORT for C-MOVE."
    try:
        dcmtk.validate_ae(settings.DCMTK_MOVE_AE_TITLE)
        _profile()
        dcmtk.tool_path("dcmrecv")
        dcmtk.tool_path("movescu")
    except ValueError as exc:
        return str(exc)
    return None


def _profile() -> Path:
    settings = get_settings()
    if settings.DCMTK_MOVE_PROFILE:
        profile = Path(settings.DCMTK_MOVE_PROFILE)
        if profile.is_absolute() and profile.is_file():
            return profile
    elif settings.DCMTK_BIN_DIR:
        found = dcmtk.find_default_profile(Path(settings.DCMTK_BIN_DIR))
        if found is not None:
            return found
    raise ValueError("Set DCMTK_MOVE_PROFILE to the absolute path of the DCMTK storescp.cfg.")


def _port_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


@contextmanager
def _receiver(spool: Path, port: int, title: str):
    receiver, _ = dcmtk.tool_path("dcmrecv")
    if _port_ready(port):
        raise ValueError("The dedicated DCMTK C-MOVE port is already in use.")
    with subprocess.Popen(  # noqa: S603 - verified tool and validated fixed arguments
        [str(receiver), "-aet", title, "-ta", "5", "-td", "30", "+B", "+fsu",
         "-xf", str(_profile()), "AllDICOM", "-od", str(spool), str(port)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=dcmtk.dcmtk_env(receiver),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ) as process:
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and process.poll() is None:
                if _port_ready(port):
                    yield process
                    return
                time.sleep(0.1)
            raise ValueError("DCMTK dcmrecv could not start the dedicated receiver.")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def _move_counts(output: str) -> tuple[int, int]:
    final = output.rsplit("I: Received Final Move Response", 1)
    if len(final) != 2:
        raise ValueError("DCMTK C-MOVE received no final response.")
    status = re.search(r"(?m)^(?:D: )?DIMSE Status *: *0x([0-9a-fA-F]{4})\b", final[1])
    completed = re.search(r"(?m)^(?:D: )?Completed Suboperations *: *(\d+) *$", final[1])
    if status is None:
        raise ValueError("DCMTK C-MOVE returned an unreadable final status.")
    code = int(status[1], 16)
    if code == 0 and completed is None:
        raise ValueError("DCMTK C-MOVE returned no confirmed instance count.")
    failures = re.findall(r"(?m)^(?:D: )?(?:Failed|Warning) Suboperations *: *(\d+) *$", final[1])
    if code == 0 and any(int(count) for count in failures):
        raise ValueError("The final C-MOVE response reports unsuccessful sub-operations.")
    return code, int(completed[1]) if completed else 0


def _validate_spool(spool: Path, study_uid: str, completed: int) -> list[Path]:
    files = sorted(spool.iterdir())
    if len(files) != completed:
        raise ValueError("The number of files received differs from the peer's C-MOVE count.")
    seen = set()
    for path in files:
        ds = dcmread(path, stop_before_pixels=True)
        uids = validate_mandatory_uids(ds, strict_format=True)
        sop = uids["SOPInstanceUID"]
        if (uids["StudyInstanceUID"] != study_uid or sop in seen
                or ds.file_meta.get("MediaStorageSOPInstanceUID") != sop):
            raise ValueError("C-MOVE received an unexpected or duplicate DICOM instance.")
        seen.add(sop)
    return files


def retrieve_study(node, study_uid: str, our_ae_title: str, collector) -> dict:
    """Receive into a private spool and ingest only a verified complete study."""
    result = {"ok": False, "engine": "dcmtk", "status": None, "pulled": 0, "failed": 0, "results": []}
    problem = availability(getattr(node, "trust_cert", None))
    if problem:
        return {**result, "reason": problem, "error_code": "MOVE_RECEIVER_UNAVAILABLE"}
    if not _RECEIVER_LOCK.acquire(blocking=False):
        return {**result, "reason": "The DCMTK receiver is busy.", "error_code": "MOVE_RECEIVER_UNAVAILABLE"}
    try:
        return _retrieve(node, study_uid, our_ae_title, collector, result)
    finally:
        _RECEIVER_LOCK.release()


def _retrieve(node, study_uid, our_ae_title, collector, result) -> dict:
    settings = get_settings()
    try:
        study_uid = check_uid(study_uid, "StudyInstanceUID", "0020,000D", strict_format=True)
        peer = dcmtk.peer_arguments(node.host, node.port, node.ae_title, our_ae_title)
        mover, _ = dcmtk.tool_path("movescu")
        title = dcmtk.validate_ae(settings.DCMTK_MOVE_AE_TITLE)
        with TemporaryDirectory(prefix="paxs-move-") as directory:
            spool = Path(directory)
            with _receiver(spool, settings.DCMTK_MOVE_PORT, title):
                response = dcmtk._run(
                    mover, "-d", "-S", "-ta", "10", "-td", "60", "-aem", title,
                    "-k", "QueryRetrieveLevel=STUDY", "-k", f"StudyInstanceUID={study_uid}",
                    *peer, timeout=600,
                )
            code, completed = _move_counts(response.stdout)
            result["status"] = code
            if response.returncode or code != 0:
                return {**result, "reason": "Remote C-MOVE failed or reported incomplete sub-operations."}
            files = _validate_spool(spool, study_uid, completed)
            collector.total_items = len(files)
            for path in files:
                _ingest_received(path, collector)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return {**result, "reason": "DCMTK C-MOVE could not complete or validate the received files.",
                "error_code": "MOVE_INCOMPLETE"}
    return {**result, "ok": collector.failed == 0, "pulled": collector.pulled,
            "failed": collector.failed, "results": collector.results,
            **({"error_code": "LOCAL_INGEST_FAILED"} if collector.failed else {})}


def _ingest_received(path: Path, collector) -> None:
    try:
        dataset = dcmread(path)
    except (ValueError, OSError):
        collector.failed += 1
        return
    try:
        collector(dataset)
    except Exception:  # noqa: BLE001 - the collector already recorded the failure
        return
