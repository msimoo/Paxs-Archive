"""Non-secret connection settings for PAXS Control."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import ipaddress
import json
import os
from pathlib import Path
import re

from desktop_viewer.links import normalize_server_url


def _valid_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", value))


@dataclass(frozen=True)
class ControlSettings:
    """Validated endpoints; credentials remain in the server configuration."""

    server_url: str = "http://127.0.0.1:8000"
    dicom_host: str = "127.0.0.1"
    dicom_port: int = 10400
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    ae_title: str = "PAXS_SCP"

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_url", normalize_server_url(self.server_url))
        if not _valid_host(self.dicom_host) or not _valid_host(self.redis_host):
            raise ValueError("Invalid service host")
        if any(isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
               for port in (self.dicom_port, self.redis_port)):
            raise ValueError("Ports must be between 1 and 65535")
        if not 1 <= len(self.ae_title) <= 16 or not re.fullmatch(r"[ -\[\]-~]+", self.ae_title):
            raise ValueError("AE title must contain 1-16 printable ASCII characters without backslash")


def settings_path() -> Path:
    """Return a per-user location for non-secret control settings."""
    root = Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else Path.home() / ".config"
    return root / "PAXS Control" / "settings.json"


def load_settings(path: Path | None = None) -> ControlSettings:
    """Load settings; invalid data is reported to the caller."""
    try:
        data = json.loads((path or settings_path()).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ControlSettings()
    if not isinstance(data, dict):
        raise ValueError("Invalid control settings")
    try:
        return ControlSettings(**data)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid control settings") from error


def save_settings(settings: ControlSettings, path: Path | None = None) -> None:
    """Atomically save non-secret connection settings."""
    target = path or settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(settings), indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
