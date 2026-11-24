"""Bounded thread-to-async progress delivery for long-running transfers."""

from __future__ import annotations

import asyncio
import threading
from typing import Any


class CoalescedProgressChannel:
    """Retain only the newest progress event and one terminal result.

    Worker threads can produce progress faster than a network client consumes
    it. Coalescing prevents a slow or disconnected client from retaining an
    event per completed batch, while a separate terminal slot preserves the
    final outcome.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._lock = threading.Lock()
        self._ready = asyncio.Event()
        self._progress: dict[str, Any] | None = None
        self._terminal: dict[str, Any] | None = None
        self._wake_scheduled = False

    @property
    def pending_progress(self) -> dict[str, Any] | None:
        """Return a copy of the coalesced progress slot for diagnostics/tests."""
        with self._lock:
            return dict(self._progress) if self._progress is not None else None

    @property
    def wakeups_scheduled(self) -> int:
        """Return the number of queued loop notifications, bounded to one."""
        with self._lock:
            return int(self._wake_scheduled)

    def publish_progress(self, done: int, total: int) -> None:
        """Replace the pending progress event with the newest totals."""
        self._publish({"event": "progress", "done": done, "total": total}, False)

    def publish_terminal(self, event: dict[str, Any]) -> None:
        """Publish a reliable terminal done/error event exactly once."""
        if event.get("event") not in {"done", "error"}:
            raise ValueError("terminal event must be done or error")
        self._publish(dict(event), True)

    def _publish(self, event: dict[str, Any], terminal: bool) -> None:
        """Update the bounded slots and schedule at most one loop callback."""
        should_wake = False
        with self._lock:
            if terminal:
                if self._terminal is not None:
                    return
                self._terminal = event
            else:
                self._progress = event
            if not self._wake_scheduled:
                self._wake_scheduled = True
                should_wake = True
        if should_wake:
            try:
                self._loop.call_soon_threadsafe(self._wake_consumer)
            except RuntimeError:
                # The request loop can close after a client disconnects. The
                # worker must still finish its bounded transfer and audit.
                with self._lock:
                    self._wake_scheduled = False

    def _wake_consumer(self) -> None:
        """Wake the async iterator after a worker publishes an event."""
        with self._lock:
            self._wake_scheduled = False
        self._ready.set()

    async def receive(self) -> dict[str, Any]:
        """Wait for progress or the final result, preserving both slots."""
        while True:
            with self._lock:
                if self._progress is not None:
                    event, self._progress = self._progress, None
                    self._ready.clear()
                    return event
                if self._terminal is not None:
                    event, self._terminal = self._terminal, None
                    self._ready.clear()
                    return event
                self._ready.clear()
            await self._ready.wait()
