"""Native DCMTK tools for registered non-TLS DICOM peers."""

import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

from pydicom import dcmread, dcmwrite

from app.core.config import get_settings


def _find_dicom_dict(tool: Path) -> Path | None:
    """Locate ``dicom.dic`` beside a portable OFFIS DCMTK installation."""
    share = tool.parent.parent / "share"
    if not share.is_dir():
        return None
    for candidate in sorted(share.glob("dcmtk*/dicom.dic"), reverse=True):
        if candidate.is_file():
            return candidate
    direct = share / "dicom.dic"
    return direct if direct.is_file() else None


def dcmtk_env(tool: Path) -> dict[str, str]:
    """Subprocess environment with ``DCMDICTPATH`` set for portable Windows DCMTK zips."""
    env = dict(os.environ)
    if not env.get("DCMDICTPATH"):
        dic = _find_dicom_dict(tool)
        if dic is not None:
            env["DCMDICTPATH"] = str(dic)
    return env


def _run(tool: Path, *arguments: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed tool path and validated node arguments
        [str(tool), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=dcmtk_env(tool),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


@lru_cache(maxsize=16)
def _verified_version(tool: Path, modified_ns: int) -> str | None:
    result = _run(tool, "--version", timeout=5)
    banner = re.search(
        rf"(?m)^\$dcmtk: {re.escape(tool.stem)} v([0-9]+\.[0-9]+\.[0-9]+)\b", result.stdout,
    )
    return banner[1] if result.returncode == 0 and banner else None


def _echoscu() -> tuple[Path, str]:
    return tool_path("echoscu")


SUPPORTED_TOOLS: tuple[tuple[str, str], ...] = (
    ("echoscu", "C-ECHO verification SCU"),
    ("findscu", "C-FIND query SCU"),
    ("dcmsend", "C-STORE send SCU"),
    ("movescu", "C-MOVE retrieve SCU"),
    ("dcmrecv", "Dedicated C-MOVE storage SCP"),
    ("dcmdump", "DICOM header dump / diagnostics"),
)


def tool_path(name: str) -> tuple[Path, str]:
    valid_names = {tool for tool, _ in SUPPORTED_TOOLS}
    if name not in valid_names:
        raise ValueError("Unsupported DCMTK tool.")
    directory = Path(get_settings().DCMTK_BIN_DIR)
    if not directory.is_absolute():
        raise ValueError("Set DCMTK_BIN_DIR to an absolute path.")
    tool = directory / (name + (".exe" if os.name == "nt" else ""))
    try:
        version = _verified_version(tool, tool.stat().st_mtime_ns)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"DCMTK {name} could not start from the configured directory.") from exc
    if not version:
        raise ValueError(f"The configured {name} did not report an OFFIS DCMTK version.")
    return tool, version


def _probe_dir_for_dcmtk(directory: Path) -> dict | None:
    if not directory.is_dir():
        return None
    suffix = ".exe" if os.name == "nt" else ""
    echo = directory / f"echoscu{suffix}"
    if not echo.is_file():
        return None
    try:
        version = _verified_version(echo, echo.stat().st_mtime_ns)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not version:
        return None
    found_tools = [name for name, _ in SUPPORTED_TOOLS if (directory / f"{name}{suffix}").is_file()]
    profile = find_default_profile(directory)
    return {
        "bin_dir": str(directory.resolve()),
        "version": version,
        "tools": found_tools,
        "profile": str(profile) if profile else "",
    }


def find_default_profile(bin_dir: Path) -> Path | None:
    """Locate ``storescp.cfg`` beside an OFFIS DCMTK installation."""
    parent = bin_dir.parent
    candidates: list[Path] = [
        parent / "etc" / "storescp.cfg",
        parent / "etc" / "dcmtk" / "storescp.cfg",
        Path("/etc/dcmtk/storescp.cfg"),
    ]
    for sub in ("share", "etc"):
        base = parent / sub
        if base.is_dir():
            candidates.extend(sorted(base.glob("dcmtk*/storescp.cfg")))
    for path in candidates:
        if path.is_absolute() and path.is_file():
            return path
    return None


def detect_installations() -> list[dict]:
    """Scan common local directories and PATH for genuine OFFIS DCMTK installations."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([exe_dir / "dcmtk" / "bin", exe_dir / ".tools" / "dcmtk" / "bin"])
    cwd = Path.cwd()
    project_root = Path(__file__).resolve().parents[2]
    candidates.extend([
        cwd / "dcmtk" / "bin",
        cwd / ".tools" / "dcmtk" / "bin",
        project_root / ".tools" / "dcmtk" / "bin",
        project_root / "dcmtk" / "bin",
    ])
    if os.name == "nt":
        for root in (Path("C:/"), Path("C:/Program Files"), Path("C:/Program Files (x86)"), Path("D:/")):
            if root.is_dir():
                for match in sorted(root.glob("dcmtk*")):
                    candidates.append(match / "bin")
                    candidates.append(match)
    else:
        candidates.extend([Path("/usr/bin"), Path("/usr/local/bin"), Path("/opt/dcmtk/bin")])
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry.strip():
            candidates.append(Path(entry.strip()))

    seen: set[str] = set()
    detected: list[dict] = []
    for cand in candidates:
        try:
            key = str(cand.resolve()).lower()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        info = _probe_dir_for_dcmtk(cand)
        if info is not None:
            detected.append(info)
    return detected


def detailed_status() -> dict:
    """Detailed per-binary DCMTK status and configuration for the Admin Services UI."""
    cfg = get_settings()
    base = status()
    tools: list[dict] = []
    for name, purpose in SUPPORTED_TOOLS:
        if not cfg.DCMTK_BIN_DIR:
            tools.append({"name": name, "purpose": purpose, "available": False, "version": None, "error": "DCMTK_BIN_DIR not set"})
            continue
        try:
            path, version = tool_path(name)
            tools.append({"name": name, "purpose": purpose, "available": True, "version": version, "path": str(path), "error": None})
        except ValueError as exc:
            tools.append({"name": name, "purpose": purpose, "available": False, "version": None, "error": str(exc)})

    detected_profile = ""
    if cfg.DCMTK_BIN_DIR and Path(cfg.DCMTK_BIN_DIR).is_absolute():
        prof = find_default_profile(Path(cfg.DCMTK_BIN_DIR))
        detected_profile = str(prof) if prof else ""
    return {
        **base,
        "bin_dir": cfg.DCMTK_BIN_DIR,
        "move_port": int(cfg.DCMTK_MOVE_PORT),
        "move_ae_title": cfg.DCMTK_MOVE_AE_TITLE,
        "move_profile": cfg.DCMTK_MOVE_PROFILE or detected_profile,
        "tls_blocked": bool(cfg.DICOM_TLS_ENABLED),
        "tools": tools,
    }


def status() -> dict:
    if not get_settings().DCMTK_BIN_DIR:
        return {"available": False, "configured": False}
    try:
        _, version = _echoscu()
    except ValueError as exc:
        return {"available": False, "configured": True, "reason": str(exc)}
    operations = ["C-ECHO"]
    for name, operation in (("findscu", "C-FIND"), ("dcmsend", "C-STORE")):
        try:
            tool_path(name)
        except ValueError:
            continue
        operations.append(operation)
    from app.dicom.dcmtk_move import availability

    move_reason = availability()
    if move_reason is None:
        operations.append("C-MOVE")
    return {
        "available": True, "configured": True, "version": version,
        "operations": operations, "move_reason": move_reason,
    }


def enabled_for(trust_cert=None) -> bool:
    settings = get_settings()
    return bool(settings.DCMTK_BIN_DIR and not (settings.DICOM_TLS_ENABLED or trust_cert))


def validate_ae(title: str) -> str:
    if not re.fullmatch(r"(?![-+@])[ -\[\]-~]{1,16}", title.strip()):
        raise ValueError("Invalid AE title for DCMTK.")
    return title.strip()


def peer_arguments(host: str, port: int, ae_title: str, our_ae_title: str) -> list[str]:
    # DCMTK expands @command-files. Only accept hostname/IP and AE values.
    if not re.fullmatch(r"[A-Za-z0-9:\[][A-Za-z0-9.:\[\]%_-]{0,254}", host) or not 1 <= port <= 65535:
        raise ValueError("Invalid hostname, IP address, or port for DCMTK.")
    return ["-aet", validate_ae(our_ae_title), "-aec", validate_ae(ae_title), host, str(port)]


def send_echo(host: str, port: int, ae_title: str) -> dict:
    result = {"engine": "dcmtk", "ok": False, "status": None}
    try:
        peer = peer_arguments(host, port, ae_title, "PAXS_SCU")
        tool, version = _echoscu()
        response = _run(
            tool, "-v", "-ta", "5", "-td", "5", "-ts", "5",
            *peer, timeout=18,
        )
    except ValueError as exc:
        return {**result, "reason": str(exc)}
    except subprocess.TimeoutExpired:
        return {**result, "reason": "DCMTK C-ECHO timed out."}
    except OSError:
        return {**result, "reason": "DCMTK C-ECHO could not start."}
    result["version"] = version
    # echoscu can exit zero after a non-success DIMSE status.
    if response.returncode == 0 and re.search(r"(?m)^I: Received Echo Response \(Success\)\s*$", response.stdout):
        return {**result, "ok": True, "status": 0}
    return {**result, "reason": "DCMTK did not receive a successful C-ECHO response."}


def send_find(host, port, ae_title, query, *, our_ae_title="PAXS_SCU", max_results=None) -> dict:
    result = {"engine": "dcmtk", "ok": False, "status": None, "matches": []}
    limit = min(max_results or 200, 200)
    try:
        peer = peer_arguments(host, port, ae_title, our_ae_title)
        tool, _ = tool_path("findscu")
        query.SpecificCharacterSet = "ISO_IR 192"
        model = "-P" if query.QueryRetrieveLevel == "PATIENT" else "-S"
        with TemporaryDirectory(prefix="paxs-find-") as directory:
            path = Path(directory) / "query.dcm"
            dcmwrite(path, query, implicit_vr=False, little_endian=True)
            response = _run(
                tool, "-v", "-sr", model, "-ta", "10", "-td", "20",
                "-X", "-od", directory, "--limit-output", str(limit + 1),
                "--cancel", str(limit + 1), *peer, str(path), timeout=40,
            )
            paths = sorted(Path(directory).glob("rsp*.dcm"))
            truncated = len(paths) > limit
            final = re.search(r"(?m)^I: Received Final Find Response \(([^\r\n]*)\)$", response.stdout)
            if response.returncode or not final or not (
                final[1] == "Success" or (truncated and final[1].startswith("Cancel"))
            ):
                return {**result, "reason": "DCMTK C-FIND did not complete successfully."}
            matches = [dcmread(path, force=True) for path in paths[:limit]]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {**result, "reason": "DCMTK C-FIND could not complete or read its responses."}
    return {**result, "ok": True, "status": 0, "matches": matches, "truncated": truncated}


def send_store(host, port, ae_title, entries, *, our_ae_title="PAXS_SCU", on_progress=None) -> dict:
    """Count peer acknowledgements by SOP UID using dcmsend's transfer report."""
    results = []
    sent = 0
    try:
        peer = peer_arguments(host, port, ae_title, our_ae_title)
        tool, _ = tool_path("dcmsend")
    except ValueError as exc:
        return {"reason": str(exc), "results": [{} for _ in entries]}
    with TemporaryDirectory(prefix="paxs-send-") as directory:
        for start in range(0, len(entries), 16):
            batch = entries[start:start + 16]
            report = Path(directory) / f"report-{start}.txt"
            statuses = _send_store_batch(tool, peer, batch, report)
            for _, uid in batch:
                status = statuses.get(uid)
                ok = status == 0
                sent += ok
                results.append({
                    "ok": ok, "status": status,
                    "reason": None if ok else (
                        "DCMTK did not confirm this instance." if status is None
                        else f"Remote C-STORE returned status 0x{status:04X}."
                    ),
                })
                if on_progress is not None:
                    on_progress(len(entries), sent, len(results) - sent)
    return {"results": results}


def _send_store_batch(tool, peer, entries, report) -> dict[str, int]:
    try:
        _run(
            tool, "-nh", "-dn", "-ta", "10", "-td", "20", "+crf", str(report),
            *peer, *(str(Path(path).absolute()) for path, _ in entries), timeout=180,
        )
        text = report.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    matches = re.findall(
        r"(?m)^SOP Instance +: ([0-9.]+)\n(?:[^\n]+\n)*?^DIMSE Status +: 0x([0-9A-Fa-f]{4})\b", text,
    )
    return {uid: int(code, 16) for uid, code in matches}
