"""Process-local admission control for large streamed response bodies."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

from starlette.types import Receive, Scope, Send

from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.concurrency import run_blocking

_P = ParamSpec("_P")
_T = TypeVar("_T")


async def bounded_to_thread(
    function: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
) -> _T:
    """Offload blocking work with a process-wide cap on active/pending calls."""
    return await run_blocking(function, *args, **kwargs)


class ResponseBytesAdmission:
    """Reserve declared response bytes while they are queued or being sent."""

    def __init__(self, budget_bytes: int) -> None:
        self.budget_bytes = max(0, int(budget_bytes))
        self.inflight_bytes = 0
        self._lock = threading.Lock()

    def try_reserve(self, byte_count: int) -> bool:
        """Atomically admit one response; zero budget disables the cap."""
        size = max(0, int(byte_count))
        with self._lock:
            if self.budget_bytes > 0 and size > self.budget_bytes - self.inflight_bytes:
                return False
            self.inflight_bytes += size
            return True

    def release(self, byte_count: int) -> None:
        """Release a previously admitted response reservation."""
        size = max(0, int(byte_count))
        with self._lock:
            self.inflight_bytes = max(0, self.inflight_bytes - size)

    def reset(self) -> None:
        """Reset accounting for isolated tests or process reconfiguration."""
        with self._lock:
            self.inflight_bytes = 0


RESPONSE_BYTES_ADMISSION = ResponseBytesAdmission(
    settings.RESPONSE_BYTES_IN_FLIGHT_BUDGET_BYTES
)


class AdmittedStreamingResponse(StreamingResponse):
    """A streaming response that always releases its process-wide reservation."""

    def __init__(self, *args: Any, reserved_bytes: int, **kwargs: Any) -> None:
        self._reserved_bytes = reserved_bytes
        super().__init__(*args, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            RESPONSE_BYTES_ADMISSION.release(self._reserved_bytes)


def reserve_response_bytes(byte_count: int) -> None:
    """Raise a retryable 503 when a process has no response-byte capacity."""
    from fastapi import HTTPException

    if not RESPONSE_BYTES_ADMISSION.try_reserve(byte_count):
        raise HTTPException(
            status_code=503,
            detail="Response capacity is temporarily exhausted",
            headers={"Retry-After": "1"},
        )


def release_response_bytes(byte_count: int) -> None:
    """Release a reservation when response construction fails before send."""
    RESPONSE_BYTES_ADMISSION.release(byte_count)

