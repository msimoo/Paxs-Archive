"""Small process-local health signals shared by runtime probes and /metrics."""

from __future__ import annotations

import threading
import time
import inspect
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any

_lock = threading.Lock()
_event_loop_lag_seconds = 0.0
_dicom_decode_failures = 0
_STORAGE_OPERATIONS = ("read", "read_stream", "write", "write_stream", "delete", "exists", "list", "other")
_STORAGE_RESULTS = ("success", "failure")
_STORAGE_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_storage_stats: dict[tuple[str, str], dict[str, Any]] = {
    (operation, result): {"count": 0, "sum": 0.0, "buckets": [0] * len(_STORAGE_BUCKETS)}
    for operation in _STORAGE_OPERATIONS
    for result in _STORAGE_RESULTS
}
_DIMSE_OPERATIONS = ("c_store", "c_find", "c_get", "c_move", "n_create", "n_set", "n_action", "other")
_DIMSE_RESULTS = ("success", "warning", "failure")
_DIMSE_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_dimse_stats: dict[tuple[str, str], dict[str, Any]] = {
    (operation, result): {"count": 0, "sum": 0.0, "buckets": [0] * len(_DIMSE_BUCKETS)}
    for operation in _DIMSE_OPERATIONS
    for result in _DIMSE_RESULTS
}
_DB_POOL_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0)
_db_pool_checkout = {"count": 0, "sum": 0.0, "buckets": [0] * len(_DB_POOL_BUCKETS)}


def record_event_loop_lag(seconds: float) -> None:
    """Store the latest non-negative event-loop scheduling delay."""
    global _event_loop_lag_seconds
    with _lock:
        _event_loop_lag_seconds = max(0.0, float(seconds))


def event_loop_lag_seconds() -> float:
    """Return the latest loop delay sample, initially zero before startup."""
    with _lock:
        return _event_loop_lag_seconds


def record_dicom_decode_failure() -> None:
    """Count a failed pixel decode without storing identifiers or error text."""
    global _dicom_decode_failures
    with _lock:
        _dicom_decode_failures += 1


def dicom_decode_failures() -> int:
    """Return the process-lifetime count of pixel decode failure contexts."""
    with _lock:
        return _dicom_decode_failures


def _record_storage_sample(operation: str, seconds: float, success: bool) -> None:
    """Update bounded S3 operation telemetry using fixed operation labels."""
    label = operation if operation in _STORAGE_OPERATIONS else "other"
    result = "success" if success else "failure"
    with _lock:
        sample = _storage_stats[(label, result)]
        sample["count"] += 1
        sample["sum"] += max(0.0, seconds)
        for index, bound in enumerate(_STORAGE_BUCKETS):
            if seconds <= bound:
                sample["buckets"][index] += 1


@contextmanager
def _storage_timer(operation: str) -> Iterator[None]:
    """Time one storage operation, recording success or failure without payloads."""
    started = time.perf_counter()
    success = False
    try:
        yield
        success = True
    finally:
        _record_storage_sample(operation, time.perf_counter() - started, success)


def measure_storage_operation(operation: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a bounded S3 operation with latency and outcome telemetry."""
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.isasyncgenfunction(function):
            @wraps(function)
            async def async_generator(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
                with _storage_timer(operation):
                    async for chunk in function(*args, **kwargs):
                        yield chunk
            return async_generator
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_function(*args: Any, **kwargs: Any) -> Any:
                with _storage_timer(operation):
                    return await function(*args, **kwargs)
            return async_function

        @wraps(function)
        def sync_function(*args: Any, **kwargs: Any) -> Any:
            with _storage_timer(operation):
                return function(*args, **kwargs)
        return sync_function
    return decorate


def storage_operation_snapshot() -> dict[tuple[str, str], dict[str, Any]]:
    """Return a copy of the fixed-cardinality S3 telemetry snapshot."""
    with _lock:
        return {
            key: {"count": value["count"], "sum": value["sum"],
                  "buckets": list(value["buckets"]), "bounds": _STORAGE_BUCKETS}
            for key, value in _storage_stats.items()
        }


def record_dimse_operation(operation: str, result: str, seconds: float) -> None:
    """Record bounded DIMSE operation outcome and duration without identifiers."""
    operation_label = operation if operation in _DIMSE_OPERATIONS else "other"
    result_label = result if result in _DIMSE_RESULTS else "failure"
    duration = max(0.0, float(seconds))
    with _lock:
        sample = _dimse_stats[(operation_label, result_label)]
        sample["count"] += 1
        sample["sum"] += duration
        for index, bound in enumerate(_DIMSE_BUCKETS):
            if duration <= bound:
                sample["buckets"][index] += 1


def dimse_operation_snapshot() -> dict[tuple[str, str], dict[str, Any]]:
    """Return a copy of the fixed-cardinality DIMSE telemetry snapshot."""
    with _lock:
        return {
            key: {"count": value["count"], "sum": value["sum"],
                  "buckets": list(value["buckets"]), "bounds": _DIMSE_BUCKETS}
            for key, value in _dimse_stats.items()
        }


def record_db_pool_checkout(seconds: float) -> None:
    """Record sync database pool acquisition time, including new connections."""
    duration = max(0.0, float(seconds))
    with _lock:
        _db_pool_checkout["count"] += 1
        _db_pool_checkout["sum"] += duration
        for index, bound in enumerate(_DB_POOL_BUCKETS):
            if duration <= bound:
                _db_pool_checkout["buckets"][index] += 1


def db_pool_checkout_snapshot() -> dict[str, Any]:
    """Return a detached snapshot of sync pool acquisition telemetry."""
    with _lock:
        return {"count": _db_pool_checkout["count"], "sum": _db_pool_checkout["sum"],
                "buckets": list(_db_pool_checkout["buckets"]),
                "bounds": _DB_POOL_BUCKETS}


def process_resident_memory_bytes() -> int | None:
    """Read current process RSS using a platform-native source when available."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        succeeded = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return int(counters.WorkingSetSize) if succeeded else None

    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            resident_pages = int(statm.read().split()[1])
        return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, IndexError, ValueError, AttributeError):
        return None


__all__ = [
    "dicom_decode_failures",
    "db_pool_checkout_snapshot",
    "dimse_operation_snapshot",
    "event_loop_lag_seconds",
    "measure_storage_operation",
    "process_resident_memory_bytes",
    "record_dicom_decode_failure",
    "record_db_pool_checkout",
    "record_dimse_operation",
    "record_event_loop_lag",
    "storage_operation_snapshot",
]
