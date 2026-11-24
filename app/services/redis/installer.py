"""Redis server detection, start/stop/restart, and the portable installer.

Everything here shells out to the host (`sc`, `net`, `tasklist`, `taskkill`) or
touches the filesystem, so it is synchronous by design and belongs behind a
worker thread — the routes that expose it do exactly that.

The installer downloads an executable archive, which makes it a supply-chain
boundary: the asset is pinned by **both** URL and SHA-256, and extraction
refuses absolute paths, ``..`` traversal, symlinks, special files and duplicate
members. A failure is reported to the operator as ``{"ok": False, "error"}``
rather than raised, because "install Redis" is a repair action and must not turn
a bad network into a 500.

Server commands are reached through the ``redis_service`` facade
(:func:`_facade`) so the probe/client seams stay patchable.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import ModuleType
from typing import BinaryIO

log = logging.getLogger("paxs.redis")

#: A local process probe (`sc`, `net`, `tasklist`, `wmic`, `taskkill`).
_PROC_PROBE_ERRORS = (OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TOOLS_REDIS_DIR = PROJECT_ROOT / ".tools" / "redis"

_FACADE = "app.services.redis_service"


def _facade() -> ModuleType:
    """The public ``app.services.redis_service`` namespace, resolved per call."""
    return sys.modules[_FACADE]


# ------------------------------------------------------------- Server Lifecycle (Start/Stop/Restart/Install) --
def _get_service_status(service_name: str) -> str | None:
    """Query status of a Windows service ('RUNNING', 'STOPPED', etc.)."""
    try:
        out = subprocess.check_output(
            ["sc", "query", service_name],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        if "RUNNING" in out:
            return "RUNNING"
        if "STOPPED" in out:
            return "STOPPED"
        if "PAUSED" in out:
            return "PAUSED"
        return "UNKNOWN"
    except _PROC_PROBE_ERRORS:  # no `sc` on this host is a valid answer
        return None


def get_redis_server_installation() -> dict:
    """Detect Redis/Memurai executable paths, Windows service status, and running processes."""
    candidates = [
        ("Memurai (Native Windows Redis)", r"C:\Program Files\Memurai\memurai.exe", "Memurai"),
        ("Redis for Windows", r"C:\Program Files\Redis\redis-server.exe", "Redis"),
        ("Portable Redis (.tools)", str(TOOLS_REDIS_DIR / "redis-server.exe"), None),
        ("Custom Redis (C:\\Redis)", r"C:\Redis\redis-server.exe", None),
    ]

    path_binary = shutil.which("redis-server")
    if path_binary:
        candidates.append(("Redis on PATH", path_binary, None))

    installed_exe = None
    product_name = None
    matched_service = None

    for name, path_str, s_name in candidates:
        if path_str and os.path.exists(path_str):
            installed_exe = path_str
            product_name = name
            matched_service = s_name
            break

    # Check services
    service_found = None
    service_status = None
    for s_name in ["Memurai", "Redis"]:
        st = _get_service_status(s_name)
        if st is not None:
            service_found = s_name
            service_status = st
            break

    # Check running processes
    process_running = False
    process_name = None
    pid = None

    try:
        if os.name == "nt":
            out = subprocess.check_output(
                ["tasklist", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
            for line in out.splitlines():
                row = line.replace('"', '').split(",")
                if len(row) >= 2:
                    pname = row[0].lower()
                    if pname in ("memurai.exe", "redis-server.exe", "redis.exe"):
                        process_running = True
                        process_name = row[0]
                        pid = int(row[1]) if row[1].isdigit() else None
                        break
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError) as exc:  # probe: "not installed" is a valid answer
        # A probe failure is *expected* on a host without Redis; the debug line
        # separates "probe blew up" from "probed and found nothing".
        log.debug("redis_installation_probe_failed error_type=%s", type(exc).__name__)

    return {
        "installed": bool(installed_exe or service_found or process_running),
        "product_name": product_name or ("Memurai Service" if service_found == "Memurai" else ("Redis Service" if service_found else "Not detected")),
        "exe_path": installed_exe,
        "service_name": service_found or matched_service,
        "service_status": service_status,
        "process_running": process_running,
        "process_name": process_name,
        "pid": pid,
    }


def start_redis_server() -> dict:
    """Start Redis / Memurai via Windows Service or direct background process."""
    # 0. If already running and responsive, return immediately
    probe = _facade().test_redis_connection()
    if probe.get("ok"):
        _facade().reset_redis_client()
        return {
            "ok": True,
            "message": f"Redis server is already active and responding ({probe.get('latency_ms')} ms)",
            "probe": probe,
        }

    info = get_redis_server_installation()
    service_name = info.get("service_name")
    exe_path = info.get("exe_path")

    # 1. Try starting Windows service if detected
    service_err = None
    if service_name:
        try:
            res = subprocess.run(
                ["net", "start", service_name],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            if res.returncode != 0:
                service_err = (res.stderr or res.stdout or "").strip()
        except _PROC_PROBE_ERRORS as exc:  # `net start` failing is reported, not raised
            service_err = str(exc)
            log.debug("redis_service_start_failed service=%s error_type=%s",
                      service_name, type(exc).__name__)

    # 2. Check if service start succeeded
    probe = _facade().test_redis_connection()
    if probe.get("ok"):
        _facade().reset_redis_client()
        return {
            "ok": True,
            "message": f"Redis service '{service_name}' started successfully ({probe.get('latency_ms')} ms)",
            "probe": probe,
        }

    # 3. If service couldn't start or no service, try direct executable launch
    candidates_to_try = []
    if exe_path and os.path.exists(exe_path):
        candidates_to_try.append(exe_path)
    portable_exe = TOOLS_REDIS_DIR / "redis-server.exe"
    if portable_exe.exists() and str(portable_exe) not in candidates_to_try:
        candidates_to_try.append(str(portable_exe))

    for candidate_exe in candidates_to_try:
        try:
            creation_flags = 0
            if os.name == "nt":
                creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(
                [candidate_exe],
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                cwd=str(Path(candidate_exe).parent),
            )
            break
        except (OSError, ValueError) as exc:  # Popen on a candidate exe path
            log.debug("redis_exe_start_failed error_type=%s", type(exc).__name__)

    # 4. Wait up to 4 seconds for Redis to become available
    for _ in range(8):
        time.sleep(0.5)
        probe = _facade().test_redis_connection()
        if probe.get("ok"):
            _facade().reset_redis_client()
            return {
                "ok": True,
                "message": f"Redis server started successfully (latency: {probe.get('latency_ms')} ms)",
                "probe": probe,
            }

    err_msg = "Failed to start Redis server."
    if service_err and "5" in service_err:  # Access is denied
        err_msg = f"Service '{service_name}' requires Windows Administrator rights to start. Please start the '{service_name}' service in services.msc or run the application as Administrator."
    elif not info.get("installed"):
        err_msg = "No Redis installation found on host. Click 'Install / Setup Portable Redis' to set it up automatically."
    elif service_err:
        err_msg = f"Failed to start Redis service ({service_err})."

    return {
        "ok": False,
        "error": err_msg,
        "probe": probe,
    }


def stop_redis_server() -> dict:
    """Gracefully stop Redis / Memurai service or terminate its process."""
    import redis

    info = get_redis_server_installation()
    service_name = info.get("service_name")

    # 1. Try sending graceful Redis SHUTDOWN command
    try:
        client = _facade().get_redis_client()
        if client:
            try:
                client.shutdown(nosave=True)
            except (redis.exceptions.RedisError, OSError) as exc:  # shutdown often drops the socket
                # Expected: SHUTDOWN closes the connection before replying.
                log.debug("redis_shutdown_reply_lost error_type=%s", type(exc).__name__)
    except (redis.exceptions.RedisError, OSError, ValueError) as exc:  # no reachable client is a valid state here
        log.debug("redis_shutdown_client_unavailable error_type=%s", type(exc).__name__)

    # 2. Stop Windows service if running
    if service_name:
        try:
            subprocess.run(
                ["net", "stop", service_name],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
        except _PROC_PROBE_ERRORS as exc:  # `net stop` failing is best-effort
            log.debug("redis_service_stop_failed service=%s error_type=%s",
                      service_name, type(exc).__name__)

    # 3. Terminate process if still active
    if os.name == "nt":
        for proc in ("memurai.exe", "redis-server.exe"):
            try:
                subprocess.run(["taskkill", "/F", "/IM", proc], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as exc:  # best-effort kill of a foreign process
                log.debug("redis_taskkill_failed proc=%s error_type=%s",
                          proc, type(exc).__name__)

    _facade().reset_redis_client()
    return {"ok": True, "message": "Redis server stopped."}


def restart_redis_server() -> dict:
    """Restart Redis server."""
    stop_redis_server()
    time.sleep(1.0)
    return start_redis_server()


# The version-specific GitHub release asset does not publish a checksum; this
# pin is the SHA-256 of that exact asset retrieved over the official HTTPS URL.
REDIS_ARCHIVE_SHA256 = "018ea18a35876383cbb5f4cd0258adfc87747cf9d619bce1cf73a2e36f720ccf"
_ARCHIVE_COPY_CHUNK_SIZE = 1024 * 1024


class ArchiveSecurityError(ValueError):
    """An installer archive is unsafe or fails its integrity check."""


class ArchivePathTraversalError(ArchiveSecurityError):
    """An archive member would escape or alias the extraction directory."""


class ArchiveIntegrityError(ArchiveSecurityError):
    """A downloaded installer archive does not match its pinned digest."""


def verify_archive_sha256(
    archive_path: str | os.PathLike[str],
    expected_sha256: str = REDIS_ARCHIVE_SHA256,
) -> None:
    """Reject an installer archive unless it matches the pinned SHA-256."""
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ValueError("expected SHA-256 must be 64 hexadecimal characters")

    digest = hashlib.sha256()
    with open(archive_path, "rb") as archive:
        for chunk in iter(lambda: archive.read(_ARCHIVE_COPY_CHUNK_SIZE), b""):
            digest.update(chunk)
    if not hmac.compare_digest(digest.hexdigest(), expected):
        raise ArchiveIntegrityError("Redis installer archive SHA-256 mismatch")


def _archive_member_target(root: Path, member_name: str) -> Path:
    """Return a validated canonical target for one ZIP/TAR member."""
    normalized = member_name.replace("\\", "/")
    windows_path = PureWindowsPath(member_name)
    posix_path = PurePosixPath(normalized)
    if (
        not normalized
        or "\x00" in normalized
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
        or windows_path.drive
        or windows_path.root
        or posix_path.is_absolute()
    ):
        raise ArchivePathTraversalError("archive member has an absolute or invalid path")

    parts = normalized.split("/")
    if any(part == ".." for part in parts) or any(":" in part for part in parts):
        raise ArchivePathTraversalError("archive member contains a traversal path")
    safe_parts = [part for part in parts if part not in ("", ".")]
    candidate = root.joinpath(*safe_parts)
    absolute_candidate = Path(os.path.abspath(os.fspath(candidate)))
    resolved = Path(os.path.realpath(os.fspath(absolute_candidate)))
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ArchivePathTraversalError("archive member escapes extraction directory") from exc
    if os.path.normcase(os.fspath(absolute_candidate)) != os.path.normcase(os.fspath(resolved)):
        raise ArchivePathTraversalError("archive member traverses a pre-existing link")
    return resolved


def _write_archive_member(root: Path, target: Path, source: BinaryIO) -> None:
    """Write a regular member after rechecking its canonical parent."""
    target.parent.mkdir(parents=True, exist_ok=True)
    current_target = Path(os.path.realpath(os.fspath(target)))
    try:
        current_target.relative_to(root)
    except ValueError as exc:
        raise ArchivePathTraversalError("archive member escapes extraction directory") from exc
    if current_target != target or target.is_symlink():
        raise ArchivePathTraversalError("archive member traverses a pre-existing link")
    if target.exists() and not target.is_file():
        raise ArchiveSecurityError("archive member would overwrite a non-file")
    with open(target, "wb") as output:
        shutil.copyfileobj(source, output, _ARCHIVE_COPY_CHUNK_SIZE)


def _safe_extract_zip(archive_path: Path, root: Path) -> None:
    """Preflight every ZIP member, then extract only regular files/directories."""
    with zipfile.ZipFile(archive_path, "r") as archive:
        entries: list[tuple[zipfile.ZipInfo, Path, bool]] = []
        seen: set[str] = set()
        for info in archive.infolist():
            target = _archive_member_target(root, info.filename)
            is_dir = info.is_dir()
            kind = stat.S_IFMT(info.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ArchiveSecurityError("ZIP symlinks and special files are not allowed")
            if (is_dir and kind == stat.S_IFREG) or (not is_dir and kind == stat.S_IFDIR):
                raise ArchiveSecurityError("ZIP member type does not match its name")
            if target == root and not is_dir:
                raise ArchiveSecurityError("ZIP file member has no filename")
            target_key = os.path.normcase(os.fspath(target))
            if target_key in seen:
                raise ArchiveSecurityError("ZIP archive contains duplicate paths")
            seen.add(target_key)
            entries.append((info, target, is_dir))

        for info, target, is_dir in entries:
            if is_dir:
                target.mkdir(parents=True, exist_ok=True)
            else:
                with archive.open(info, "r") as source:
                    _write_archive_member(root, target, source)


def _safe_extract_tar(archive_path: Path, root: Path) -> None:
    """Preflight every TAR member, then extract only regular files/directories."""
    with tarfile.open(archive_path, mode="r:*") as archive:
        entries: list[tuple[tarfile.TarInfo, Path, bool]] = []
        seen: set[str] = set()
        for member in archive.getmembers():
            target = _archive_member_target(root, member.name)
            is_dir = member.isdir()
            if not is_dir and not member.isfile():
                raise ArchiveSecurityError("TAR links and special files are not allowed")
            if target == root and not is_dir:
                raise ArchiveSecurityError("TAR file member has no filename")
            target_key = os.path.normcase(os.fspath(target))
            if target_key in seen:
                raise ArchiveSecurityError("TAR archive contains duplicate paths")
            seen.add(target_key)
            entries.append((member, target, is_dir))

        for member, target, is_dir in entries:
            if is_dir:
                target.mkdir(parents=True, exist_ok=True)
            else:
                source = archive.extractfile(member)
                if source is None:
                    raise ArchiveSecurityError("TAR regular file could not be read")
                with source:
                    _write_archive_member(root, target, source)


def safe_extract_archive(
    archive_path: str | os.PathLike[str],
    destination_dir: str | os.PathLike[str],
) -> None:
    """Safely extract ZIP or TAR archives without links or escaping paths."""
    archive = Path(archive_path)
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    root = Path(os.path.realpath(os.fspath(destination)))
    if zipfile.is_zipfile(archive):
        _safe_extract_zip(archive, root)
    elif tarfile.is_tarfile(archive):
        _safe_extract_tar(archive, root)
    else:
        raise ArchiveSecurityError("unsupported or invalid installer archive")


def install_redis_portable() -> dict:
    """Download and set up portable Redis for Windows in .tools/redis/."""
    TOOLS_REDIS_DIR.mkdir(parents=True, exist_ok=True)
    target_exe = TOOLS_REDIS_DIR / "redis-server.exe"

    if target_exe.exists():
        start_res = _facade().start_redis_server()
        return {
            "ok": start_res.get("ok", True),
            "message": "Portable Redis is already installed." + (" Started successfully." if start_res.get("ok") else ""),
            "path": str(target_exe),
            "start": start_res,
        }

    # The version-specific release asset is pinned both by URL and digest.
    download_url = "https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip"
    zip_path = TOOLS_REDIS_DIR / "redis_download.zip"

    try:
        urllib.request.urlretrieve(download_url, zip_path)
        verify_archive_sha256(zip_path)
        safe_extract_archive(zip_path, TOOLS_REDIS_DIR)
        if not target_exe.is_file():
            raise ArchiveSecurityError("verified Redis archive did not contain redis-server.exe")

        start_res = _facade().start_redis_server()
        return {
            "ok": True,
            "message": "Portable Redis downloaded, verified, extracted, and started successfully!",
            "path": str(target_exe),
            "start": start_res,
        }
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile, tarfile.TarError, urllib.error.URLError) as exc:  # install: every download/extract failure is reported to the operator
        return {"ok": False, "error": f"Failed to download/install portable Redis: {exc}"}
    finally:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not remove downloaded Redis installer archive")
