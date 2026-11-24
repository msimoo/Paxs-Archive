"""Cached, read-only health checks for the inbound DICOM receiver."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

from app.core.config import settings
from app.dicom.sender import send_echo

_CACHE_TTL_SECONDS = 15.0
_PROBE_TIMEOUT_SECONDS = 3.0
_cache_lock = threading.Lock()
_probe_lock = threading.Lock()
_cached_probe: tuple[float, dict[str, Any]] | None = None


def clear_probe_cache() -> None:
    """Clear cached probe state; intended for tests and controlled rechecks."""
    global _cached_probe
    with _cache_lock:
        _cached_probe = None


def probe_receiver() -> dict[str, Any]:
    """Check TCP reachability and complete a bounded DICOM C-ECHO.

    The result is cached briefly because dashboard polling must not create a
    new association for every browser refresh. Error output contains only an
    exception type, never peer addresses or DICOM data.
    """
    global _cached_probe
    now = time.monotonic()
    with _cache_lock:
        if _cached_probe is not None and now - _cached_probe[0] < _CACHE_TTL_SECONDS:
            return dict(_cached_probe[1])

    # Coalesce simultaneous dashboard polls. The second caller rechecks the
    # cache after the first bounded association finishes.
    with _probe_lock:
        now = time.monotonic()
        with _cache_lock:
            if _cached_probe is not None and now - _cached_probe[0] < _CACHE_TTL_SECONDS:
                return dict(_cached_probe[1])

        host = settings.DICOM_HOST
        connect_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
        result: dict[str, Any] = {
            "live": False,
            "tcp": False,
            "c_echo": False,
            "configured": bool(settings.DICOM_RECEIVER_ENABLED),
            "host": connect_host,
            "port": settings.DICOM_PORT,
            "ae_title": settings.DICOM_AE_TITLE,
        }
        try:
            with socket.create_connection(
                (connect_host, settings.DICOM_PORT), timeout=_PROBE_TIMEOUT_SECONDS
            ):
                result["tcp"] = True
            echo = send_echo(
                connect_host, settings.DICOM_PORT, settings.DICOM_AE_TITLE,
                our_ae_title="PAXS_HEALTH", timeout=_PROBE_TIMEOUT_SECONDS,
            )
            result["c_echo"] = bool(echo.get("ok"))
            result["live"] = result["tcp"] and result["c_echo"]
            if not result["live"]:
                result["error_type"] = "C_ECHO_FAILED"
        except Exception as exc:  # health probes must degrade to an explicit offline result
            result["error_type"] = type(exc).__name__

        with _cache_lock:
            _cached_probe = (time.monotonic(), dict(result))
        return result
