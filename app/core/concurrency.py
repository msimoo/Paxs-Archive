"""One bounded pool for every background task (C-4 / C-5).

Before this module, background work was dispatched with a raw
``threading.Thread(...).start()`` at each call site — one per study opened
(warm-on-open) and one per event dispatch (scripting). Neither was metered, so
a burst of viewer opens or a chained script event could spawn unbounded OS
threads until the process hit its thread limit (AGENTS.md §1 N-5).

Every background task now goes through :func:`submit_background`, which hands
the work to a single shared :class:`~concurrent.futures.ThreadPoolExecutor`
sized by ``BACKGROUND_MAX_THREADS`` (default and hard cap 8). The pool:

* **bounds concurrency** — at most ``MAX_BACKGROUND_THREADS`` tasks run at once
  (the configured count may lower this cap), so a burst cannot exhaust threads
  or starve the event loop's workers;
* **bounds the backlog** — at most ``BACKGROUND_QUEUE_MAX`` tasks may be queued;
  past that :func:`submit_background` returns ``False`` and the caller drops or
  debounces the task (a repeated warm of the same study is worthless anyway);
* **never raises** — a saturated pool or a pool that cannot accept work is a
  ``False`` return, never an exception into an HTTP handler or a DICOM path;
* **is observable** — :func:`pool_stats` exposes in-flight/active/peak counts so
  a burst can be proven bounded (this is what the regression test asserts).

A ``0`` for either setting means *disabled* and is honoured literally (§1 N-5):
``BACKGROUND_MAX_THREADS=0`` disables background dispatch entirely rather than
silently falling back to a default.
"""

from __future__ import annotations

import atexit
import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import HTTPException
from app.core.config import settings

log = logging.getLogger("paxs.concurrency")


class BlockingCapacityError(HTTPException):
    """Raised when bounded request I/O admission is full (retryable HTTP 503)."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail="Blocking I/O capacity is temporarily exhausted",
            headers={"Retry-After": "1"},
        )


class BoundedBlockingExecutor:
    """Dedicated bounded executor for synchronous request-path I/O."""

    def __init__(self, max_workers: int, queue_max: int) -> None:
        self.max_workers = max(0, int(max_workers))
        self.queue_max = max(0, int(queue_max))
        self._lock = threading.Lock()
        self._admission = threading.BoundedSemaphore(self.max_workers + self.queue_max)
        self._executor: ThreadPoolExecutor | None = None
        self._active = self._inflight = self._rejected = 0
        self._wait_count = 0
        self._wait_total = self._wait_max = 0.0

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                if self.max_workers <= 0:
                    raise BlockingCapacityError()
                self._executor = ThreadPoolExecutor(
                    max_workers=self.max_workers, thread_name_prefix="paxs-io"
                )
            return self._executor

    async def run(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if not self._admission.acquire(blocking=False):
            with self._lock:
                self._rejected += 1
            raise BlockingCapacityError()
        loop = asyncio.get_running_loop()
        submitted_at = time.monotonic()

        def invoke() -> Any:
            started_at = time.monotonic()
            wait = started_at - submitted_at
            with self._lock:
                self._active += 1
                self._wait_count += 1
                self._wait_total += wait
                self._wait_max = max(self._wait_max, wait)
            try:
                return function(*args, **kwargs)
            finally:
                with self._lock:
                    self._active -= 1
                    self._inflight -= 1
                self._admission.release()

        inflight_recorded = False
        try:
            executor = self._get_executor()
            with self._lock:
                self._inflight += 1
                inflight_recorded = True
            future = loop.run_in_executor(executor, invoke)
        except BaseException:
            if inflight_recorded:
                with self._lock:
                    self._inflight -= 1
            self._admission.release()
            raise
        return await asyncio.shield(future)

    def stats(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "max_workers": self.max_workers,
                "queue_max": self.queue_max,
                "active": self._active,
                "inflight": self._inflight,
                "queue_depth": max(0, self._inflight - self._active),
                "rejected": self._rejected,
                "wait_count": self._wait_count,
                "wait_total_seconds": self._wait_total,
                "wait_max_seconds": self._wait_max,
            }

    def reset(self) -> None:
        with self._lock:
            if self._inflight:
                raise RuntimeError("Cannot reset blocking I/O executor while work is active")
            executor, self._executor = self._executor, None
            self._active = self._inflight = self._rejected = self._wait_count = 0
            self._wait_total = self._wait_max = 0.0
        if executor is not None:
            executor.shutdown(wait=True)

    def shutdown(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)


_BLOCKING_EXECUTOR = BoundedBlockingExecutor(
    getattr(settings, "BLOCKING_IO_MAX_THREADS", 16),
    getattr(settings, "BLOCKING_IO_QUEUE_MAX", 64),
)
_BLOCKING_EXECUTOR_LOCK = threading.Lock()


def _refresh_blocking_executor_if_idle() -> None:
    """Apply live executor settings after the current bounded workload drains."""
    global _BLOCKING_EXECUTOR
    workers = max(0, int(getattr(settings, "BLOCKING_IO_MAX_THREADS", 16)))
    queue_max = max(0, int(getattr(settings, "BLOCKING_IO_QUEUE_MAX", 64)))
    with _BLOCKING_EXECUTOR_LOCK:
        current = _BLOCKING_EXECUTOR.stats()
        if (current["max_workers"], current["queue_max"]) == (workers, queue_max):
            return
        if current["inflight"]:
            return
        _BLOCKING_EXECUTOR.reset()
        _BLOCKING_EXECUTOR = BoundedBlockingExecutor(workers, queue_max)


async def run_blocking(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run bounded synchronous I/O outside the event loop with queue-wait metrics."""
    _refresh_blocking_executor_if_idle()
    return await _BLOCKING_EXECUTOR.run(function, *args, **kwargs)


def run_blocking_stats() -> dict[str, int | float]:
    """Return PHI-free request I/O executor gauges and wait-time counters."""
    return _BLOCKING_EXECUTOR.stats()


def reset_blocking_executor() -> None:
    """Recreate the request I/O executor from the current settings (tests/reload)."""
    global _BLOCKING_EXECUTOR
    with _BLOCKING_EXECUTOR_LOCK:
        _BLOCKING_EXECUTOR.reset()
        _BLOCKING_EXECUTOR = BoundedBlockingExecutor(
            getattr(settings, "BLOCKING_IO_MAX_THREADS", 16),
            getattr(settings, "BLOCKING_IO_QUEUE_MAX", 64),
        )

#: Public fixed upper bound required for background worker threads.
MAX_BACKGROUND_THREADS = 8

#: Default worker count for the shared background pool. Runtime settings may
#: lower this cap (including 0 to disable dispatch) but default to the limit.
DEFAULT_MAX_BACKGROUND_THREADS = MAX_BACKGROUND_THREADS

#: Default backlog cap (queued but not yet running tasks).
DEFAULT_BACKGROUND_QUEUE_MAX = 64

#: Thread-name prefix, so a stack dump or profiler names the pool's threads.
_THREAD_PREFIX = "paxs-bg"


def max_background_threads() -> int:
    """Configured worker count, capped at the fixed process-wide maximum.

    ``0`` means **disabled** and is returned as 0, never masked with a default
    (N-5): an ``or`` fallback would turn a deliberate disable into a live pool.
    """
    raw = getattr(settings, "BACKGROUND_MAX_THREADS", DEFAULT_MAX_BACKGROUND_THREADS)
    try:
        return min(MAX_BACKGROUND_THREADS, max(0, int(raw)))
    except (TypeError, ValueError) as exc:
        log.warning(
            "Invalid BACKGROUND_MAX_THREADS configuration; using default "
            "(error_type=%s)", type(exc).__name__,
        )
        return DEFAULT_MAX_BACKGROUND_THREADS


def max_background_queue() -> int:
    """Configured backlog cap (``0`` = disabled, honoured literally)."""
    raw = getattr(settings, "BACKGROUND_QUEUE_MAX", DEFAULT_BACKGROUND_QUEUE_MAX)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError) as exc:
        log.warning(
            "Invalid BACKGROUND_QUEUE_MAX configuration; using default "
            "(error_type=%s)", type(exc).__name__,
        )
        return DEFAULT_BACKGROUND_QUEUE_MAX


class BoundedBackgroundPool:
    """A bounded executor plus the counters that prove it stayed bounded.

    ``submitted``/``completed``/``dropped`` are cumulative; ``inflight`` is the
    number of accepted-but-unfinished tasks and ``active`` the number currently
    executing. ``peak_active`` is the high-water mark of ``active`` — the figure
    a regression test asserts against ``max_workers``.
    """

    def __init__(self, max_workers: int, queue_max: int) -> None:
        self.max_workers = max(0, int(max_workers))
        self.queue_max = max(0, int(queue_max))
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self.submitted = 0
        self.completed = 0
        self.dropped = 0
        self.failed = 0
        self.inflight = 0
        self.active = 0
        self.peak_active = 0

    # -- lifecycle ------------------------------------------------------- #
    def _ensure_executor(self) -> ThreadPoolExecutor:
        """Build the executor on first use (caller holds ``_lock``)."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix=_THREAD_PREFIX
            )
        return self._executor

    def _run(self, fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        """Execute one task, keeping the active/peak counters accurate."""
        with self._lock:
            self.active += 1
            if self.active > self.peak_active:
                self.peak_active = self.active
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # a task failure must not kill the worker or expose payloads
            self._bump("failed")
            log.error("background_task_failed error_type=%s", type(exc).__name__)
        finally:
            with self._lock:
                self.active -= 1
                self.inflight -= 1
                self.completed += 1

    def _bump(self, field: str) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)

    # -- submission ------------------------------------------------------ #
    def submit(self, fn: Callable[..., Any], *args, **kwargs) -> bool:
        """Run ``fn(*args, **kwargs)`` in the pool; ``False`` when saturated.

        Returns ``False`` (never raises) when background work is disabled, the
        backlog is full, or the executor refuses the task. Callers treat that as
        "dropped" — a warm pass that is already queued makes a duplicate
        worthless, so dropping is the correct backpressure.
        """
        if self.max_workers <= 0 or self.queue_max <= 0:
            self._bump("dropped")
            return False
        with self._lock:
            if self.inflight >= self.queue_max:
                self.dropped += 1
                log.debug(
                    "background pool saturated (%d in flight, cap %d); dropping %r",
                    self.inflight, self.queue_max, getattr(fn, "__name__", fn),
                )
                return False
            executor = self._ensure_executor()
            self.submitted += 1
            self.inflight += 1
        try:
            executor.submit(self._run, fn, args, kwargs)
        except RuntimeError as exc:
            # The executor is shutting down (or already shut down).
            with self._lock:
                self.inflight -= 1
                self.dropped += 1
            log.debug("background pool refused a task: %s", exc)
            return False
        return True

    # -- observability --------------------------------------------------- #
    def stats(self) -> dict:
        with self._lock:
            return {
                "max_workers": self.max_workers,
                "queue_max": self.queue_max,
                "submitted": self.submitted,
                "completed": self.completed,
                "dropped": self.dropped,
                "failed": self.failed,
                "inflight": self.inflight,
                "active": self.active,
                "peak_active": self.peak_active,
            }

    def wait(self, timeout: float | None = 30.0) -> bool:
        """Block until accepted tasks finish (tests + graceful shutdown).

        Returns True when the backlog drained, False on timeout. Safe to call
        when nothing was ever submitted.
        """
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                pending = self.inflight
            if pending == 0:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def reset(self) -> None:
        """Drop the executor and zero the counters (tests / config reload).

        Waits for in-flight work first, so a reset never leaves a task writing
        to a torn-down pool.
        """
        self.wait(timeout=10.0)
        with self._lock:
            executor, self._executor = self._executor, None
            self.submitted = self.completed = self.dropped = self.failed = 0
            self.inflight = self.active = self.peak_active = 0
        if executor is not None:
            executor.shutdown(wait=True)

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=wait)


#: The process-wide pool every background task shares.
_POOL = BoundedBackgroundPool(
    max_workers=max_background_threads(),
    queue_max=max_background_queue(),
)


def submit_background(fn: Callable[..., Any], *args, **kwargs) -> bool:
    """Run ``fn`` on the shared bounded pool. ``False`` = dropped (see above)."""
    return _POOL.submit(fn, *args, **kwargs)


def pool_stats() -> dict:
    """Counters/gauges for the shared background pool (tests + /metrics)."""
    return _POOL.stats()


def wait_for_background(timeout: float | None = 30.0) -> bool:
    """Block until the shared pool's backlog drains (tests + shutdown)."""
    return _POOL.wait(timeout=timeout)


def reset_background_pool() -> None:
    """Rebuild the shared pool from current settings (tests / config reload)."""
    _POOL.max_workers = max_background_threads()
    _POOL.queue_max = max_background_queue()
    _POOL.reset()


atexit.register(_POOL.shutdown, wait=False)
atexit.register(_BLOCKING_EXECUTOR.shutdown)
