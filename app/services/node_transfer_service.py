"""Bounded streamed STOW orchestration and transfer audit persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Sequence
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.audit_service import add_audit
from app.services.transfer_progress import CoalescedProgressChannel

log = logging.getLogger("paxs.node_transfer")
_STOW_TRANSFER_SLOTS = threading.BoundedSemaphore(
    settings.NODE_TRANSFER_STOW_MAX_CONCURRENT
)
_ACTIVE_STOW_TRANSFERS: set[asyncio.Task[None]] = set()


class TransferCapacityError(RuntimeError):
    """Raised when all bounded outbound-STOW slots are occupied."""


async def audit_forwarded_async(
    session: AsyncSession, results: Sequence[dict[str, Any]], performed_by: str,
) -> bool:
    """Commit audit rows for remotely confirmed objects in the request session.

    The response iterator waits for its transfer task before releasing the
    request scope, and this commit makes the audit durable before the client
    can disconnect from the final result.
    """
    confirmed = [item for item in results if item.get("ok")]
    if not confirmed:
        return True
    try:
        for item in confirmed:
            add_audit(
                session,
                item.get("sop_uid"),
                "FORWARDED",
                performed_by=performed_by,
                study_instance_uid=item.get("study_uid"),
                file_path=None,
            )
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        log.error("node transfer audit commit failed (error_type=%s)", type(exc).__name__)
        return False
    return True


def _transfer_stow(
    node: Any, targets: list[dict[str, Any]], accelerated: bool,
    workers: int | None, channel: CoalescedProgressChannel | None,
) -> dict[str, Any]:
    """Run the synchronous DICOMweb client outside the event loop."""
    if accelerated:
        from app.services.accelerator_service import accelerated_stow_upload

        options: dict[str, Any] = {"workers": workers}
        if channel is not None:
            options["on_progress"] = channel.publish_progress
        return accelerated_stow_upload(node, targets, **options)
    from app.services.dicomweb_client import stow_upload

    return stow_upload(node, targets)


async def run_stow_upload(
    node: Any, targets: list[dict[str, Any]], *, accelerated: bool,
    workers: int | None,
) -> dict[str, Any]:
    """Run a non-streamed STOW request inside the shared transfer capacity."""
    if not _STOW_TRANSFER_SLOTS.acquire(blocking=False):
        raise TransferCapacityError("Too many active DICOMweb transfers; retry shortly")
    try:
        return await asyncio.to_thread(
            _transfer_stow, node, targets, accelerated, workers, None
        )
    finally:
        _STOW_TRANSFER_SLOTS.release()


async def _run_streamed_stow(
    node: Any, targets: list[dict[str, Any]], accelerated: bool,
    workers: int | None, channel: CoalescedProgressChannel, db: AsyncSession,
    performed_by: str, ae_title: str,
) -> None:
    """Run upload, audit confirmed instances, then publish one terminal event."""
    try:
        result = await asyncio.to_thread(
            _transfer_stow, node, targets, accelerated, workers, channel
        )
    except ValueError as exc:
        channel.publish_terminal({"event": "error", "message": f"Unsafe web_url: {exc}"})
        return
    except Exception as exc:  # remote HTTP and storage failures have varied concrete types
        log.warning("DICOMweb STOW upload to %s failed (error_type=%s)",
                    ae_title, type(exc).__name__)
        channel.publish_terminal(
            {"event": "error", "message": "DICOMweb STOW transfer failed"}
        )
        return

    if result.get("sent", 0) == 0 and result.get("failed", 0) > 0:
        channel.publish_terminal({"event": "error", "message": "STOW-RS upload failed"})
        return
    audit_ok = await audit_forwarded_async(db, result.get("results", []), performed_by)
    channel.publish_terminal({
        "event": "done",
        "result": {"ae_title": ae_title, "transport": "dicomweb",
                   **result, "audit_recorded": audit_ok},
    })


def _transfer_finished(task: asyncio.Task[None]) -> None:
    """Release a slot only when transfer and audit have both completed."""
    _ACTIVE_STOW_TRANSFERS.discard(task)
    _STOW_TRANSFER_SLOTS.release()
    if not task.cancelled():
        task.exception()


def stream_stow_events(
    node: Any, targets: list[dict[str, Any]], *, accelerated: bool,
    workers: int | None, db: AsyncSession, performed_by: str, ae_title: str,
) -> AsyncIterator[str]:
    """Start a bounded STOW task and return its coalesced NDJSON event iterator."""
    if not _STOW_TRANSFER_SLOTS.acquire(blocking=False):
        raise TransferCapacityError("Too many active DICOMweb transfers; retry shortly")
    try:
        channel = CoalescedProgressChannel(asyncio.get_running_loop())
        task = asyncio.create_task(_run_streamed_stow(
            node, targets, accelerated, workers, channel, db, performed_by, ae_title
        ))
        _ACTIVE_STOW_TRANSFERS.add(task)
        task.add_done_callback(_transfer_finished)
    except BaseException:  # release the admission slot if task setup is interrupted
        _STOW_TRANSFER_SLOTS.release()
        raise

    async def _events() -> AsyncIterator[str]:
        try:
            while True:
                event = await channel.receive()
                yield json.dumps(event) + "\n"
                if event.get("event") in {"done", "error"}:
                    return
        finally:
            # Cancelling to_thread cannot stop the network call; keep the
            # request DB scope alive until the worker has also audited results.
            await asyncio.shield(task)

    return _events()
