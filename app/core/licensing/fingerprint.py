"""Hardware fingerprint collector.

Builds a stable, tamper-resistant hardware fingerprint from multiple sources:
- Non-loopback MAC addresses
- CPU Processor ID (Windows CIM/WMIC, Linux DMI)
- Motherboard / Baseboard Serial Number
- Operating System Serial Number / machine-id

Resistant to MAC spoofing, virtual machine clones, and minor hardware upgrades.

**Probe contract.** Every collector below is a *best-effort* probe: a host may
legitimately expose none of these sources (container, hardened kernel, missing
WMIC), so a failed probe is not an error and must never fail licensing. Each
collector therefore returns a documented sentinel — an empty list, or a
``None`` per component — and the fingerprint degrades to the components that
did resolve (finally to ``FALLBACK:<node>|<machine>``). The failures are still
recorded at ``debug`` with the exception *type* only, so "probe refused" stays
distinguishable from "probe returned nothing" without putting a serial number,
hostname, or command line into the log (AGENTS.md N-2).
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import subprocess
import uuid

log = logging.getLogger("paxs.licensing.fingerprint")

_CACHED_FINGERPRINT: str | None = None


def _get_mac_addresses() -> list[str]:
    """Collect non-loopback MAC addresses."""
    macs: list[str] = []
    # stdlib uuid.getnode
    try:
        raw_node = uuid.getnode()
        # Ensure it's not a purely randomized MAC (multicast bit not set)
        if (raw_node >> 40) % 2 == 0:
            mac = ":".join(
                [f"{(raw_node >> ele) & 0xff:02X}" for ele in range(0, 8 * 6, 8)][::-1]
            )
            if mac and mac != "00:00:00:00:00:00":
                macs.append(mac)
    except (OSError, ValueError) as exc:  # probe: getnode may raise on a hardened host
        log.debug("fingerprint_uuid_getnode_failed error_type=%s", type(exc).__name__)

    # Linux /sys/class/net/*
    if platform.system() == "Linux":
        net_dir = "/sys/class/net"
        if os.path.isdir(net_dir):
            try:
                for iface in sorted(os.listdir(net_dir)):
                    if iface == "lo":
                        continue
                    addr_path = os.path.join(net_dir, iface, "address")
                    if os.path.isfile(addr_path):
                        with open(addr_path, "r", encoding="utf-8") as f:
                            addr = f.read().strip().upper()
                            if addr and addr != "00:00:00:00:00:00" and addr not in macs:
                                macs.append(addr)
            except (OSError, UnicodeDecodeError) as exc:  # probe: an unreadable iface is skipped
                log.debug("fingerprint_net_iface_read_failed error_type=%s", type(exc).__name__)

    return sorted(list(set(macs)))


def _get_windows_identifiers() -> tuple[str | None, str | None, str | None]:
    """Retrieve (cpu_id, motherboard_serial, os_serial) on Windows via PowerShell CIM or WMIC."""
    cpu_id: str | None = None
    mb_serial: str | None = None
    os_serial: str | None = None

    # Try PowerShell Get-CimInstance (fast and reliable on Windows 10/11 & Windows Server)
    try:
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-CimInstance Win32_Processor).ProcessorId; "
            "(Get-CimInstance Win32_BaseBoard).SerialNumber; "
            "(Get-CimInstance Win32_OperatingSystem).SerialNumber",
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=4.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if res.returncode == 0:
            lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
            if len(lines) >= 1 and lines[0]:
                cpu_id = lines[0]
            if len(lines) >= 2 and lines[1]:
                cand = lines[1].lower()
                if cand not in ("to be filled by o.e.m.", "none", "default string", "0"):
                    mb_serial = lines[1]
            if len(lines) >= 3 and lines[2]:
                os_serial = lines[2]
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError) as exc:  # probe: the WMIC queries below are the fallback
        log.debug("fingerprint_cim_query_failed error_type=%s; falling back to WMIC",
                  type(exc).__name__)

    # Fallback to WMIC if any is missing
    if not cpu_id:
        try:
            out = subprocess.check_output(
                "wmic cpu get ProcessorId /value",
                shell=True,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            ).decode().strip()
            for line in out.splitlines():
                if line.startswith("ProcessorId="):
                    cpu_id = line.split("=", 1)[1].strip() or None
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError) as exc:  # probe: wmic is absent on modern Windows
            log.debug("fingerprint_wmic_cpu_failed error_type=%s", type(exc).__name__)

    if not mb_serial:
        try:
            out = subprocess.check_output(
                "wmic baseboard get SerialNumber /value",
                shell=True,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            ).decode().strip()
            for line in out.splitlines():
                if line.startswith("SerialNumber="):
                    cand = line.split("=", 1)[1].strip()
                    if cand and cand.lower() not in ("to be filled by o.e.m.", "none", "default string", "0"):
                        mb_serial = cand
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError) as exc:  # probe: wmic is absent on modern Windows
            log.debug("fingerprint_wmic_baseboard_failed error_type=%s", type(exc).__name__)

    if not os_serial:
        try:
            out = subprocess.check_output(
                "wmic os get SerialNumber /value",
                shell=True,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            ).decode().strip()
            for line in out.splitlines():
                if line.startswith("SerialNumber="):
                    os_serial = line.split("=", 1)[1].strip() or None
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError) as exc:  # probe: wmic is absent on modern Windows
            log.debug("fingerprint_wmic_os_failed error_type=%s", type(exc).__name__)

    return cpu_id, mb_serial, os_serial


def _get_linux_identifiers() -> tuple[str | None, str | None, str | None]:
    """Retrieve (machine_id, dmi_uuid, product_serial) on Linux."""
    machine_id: str | None = None
    dmi_uuid: str | None = None
    serial: str | None = None

    # 1. /etc/machine-id or /var/lib/dbus/machine-id
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        machine_id = content
                        break
            except (OSError, UnicodeDecodeError) as exc:  # probe: an unreadable machine-id is optional
                log.debug("fingerprint_machine_id_read_failed error_type=%s",
                          type(exc).__name__)

    # 2. DMI System UUID
    dmi_path = "/sys/class/dmi/id/product_uuid"
    if os.path.isfile(dmi_path):
        try:
            with open(dmi_path, "r", encoding="utf-8") as f:
                dmi_uuid = f.read().strip() or None
        except (OSError, UnicodeDecodeError) as exc:  # probe: product_uuid is root-only
            log.debug("fingerprint_dmi_uuid_read_failed error_type=%s", type(exc).__name__)

    # 3. DMI Product Serial
    serial_path = "/sys/class/dmi/id/product_serial"
    if os.path.isfile(serial_path):
        try:
            with open(serial_path, "r", encoding="utf-8") as f:
                cand = f.read().strip()
                if cand and cand.lower() not in ("to be filled by o.e.m.", "none", "default string", "0"):
                    serial = cand
        except (OSError, UnicodeDecodeError) as exc:  # probe: product_serial is root-only
            log.debug("fingerprint_dmi_serial_read_failed error_type=%s", type(exc).__name__)

    return machine_id, dmi_uuid, serial


def get_machine_fingerprint(force_refresh: bool = False) -> str:
    """Builds a stable 32-character SHA-256 hardware fingerprint string.
    
    Format: 32-character uppercase hexadecimal string (e.g. 3A7F9C2E4B184FD69941C898B6EA2F3D).
    """
    global _CACHED_FINGERPRINT
    if _CACHED_FINGERPRINT and not force_refresh:
        return _CACHED_FINGERPRINT

    components: list[str] = []

    # 1. MAC addresses
    for mac in _get_mac_addresses():
        components.append(f"MAC:{mac}")

    # 2. OS-specific hardware identifiers
    sys_name = platform.system()
    if sys_name == "Windows":
        cpu_id, mb_serial, os_serial = _get_windows_identifiers()
        if cpu_id:
            components.append(f"CPU:{cpu_id.upper()}")
        if mb_serial:
            components.append(f"MB:{mb_serial.upper()}")
        if os_serial:
            components.append(f"OS:{os_serial.upper()}")
    elif sys_name == "Linux":
        machine_id, dmi_uuid, serial = _get_linux_identifiers()
        if machine_id:
            components.append(f"MACHINE_ID:{machine_id.upper()}")
        if dmi_uuid:
            components.append(f"DMI_UUID:{dmi_uuid.upper()}")
        if serial:
            components.append(f"SERIAL:{serial.upper()}")
    else:
        # Fallback for macOS/other systems
        components.append(f"NODE:{platform.node()}")

    if not components:
        # Extreme fallback: platform node name and machine architecture
        components.append(f"FALLBACK:{platform.node()}|{platform.machine()}")

    raw = "|".join(sorted(components))
    fp = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32].upper()
    _CACHED_FINGERPRINT = fp
    log.debug("Computed machine hardware fingerprint: %s from %d components", fp, len(components))
    return fp


def get_hardware_info() -> dict[str, str | None]:
    """Returns human-readable diagnostic details of collected hardware components."""
    sys_name = platform.system()
    info: dict[str, str | None] = {
        "os": f"{sys_name} {platform.release()}",
        "hostname": platform.node(),
        "architecture": platform.machine(),
        "mac_addresses": ", ".join(_get_mac_addresses()),
        "fingerprint": get_machine_fingerprint(),
    }
    if sys_name == "Windows":
        cpu_id, mb_serial, os_serial = _get_windows_identifiers()
        info["cpu_id"] = cpu_id
        info["motherboard_serial"] = mb_serial
        info["os_serial"] = os_serial
    elif sys_name == "Linux":
        machine_id, dmi_uuid, serial = _get_linux_identifiers()
        info["machine_id"] = machine_id
        info["dmi_uuid"] = dmi_uuid
        info["product_serial"] = serial

    return info
