"""Transfer Accelerator (feature 19) — HTTP-based fast transfer bypassing DIMSE.

A study's instances are split into batches and pushed to a peer's DICOMweb
service over N **parallel** STOW-RS connections, each streaming its
multipart/related body with chunked transfer encoding (see
:func:`app.services.dicomweb_client.stow_upload` with ``stream=True``). For a
large multi-series study this runs many HTTP transfers at once instead of one
sequential DIMSE association — the receiver ingests parts as they arrive.

A batch that dies on the wire (connection error or HTTP >= 500) is retried up
to ``retries`` times before its instances are counted as failed; per-instance
rejections reported by the remote in a 200/202/409 body (FailedSOPSequence)
are final, never retried.

The REST layer wraps this in ``asyncio.to_thread`` exactly like the other SCU
helpers — the whole engine is synchronous (httpx).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.core.config import settings
from app.services.dicomweb_client import stow_upload, validate_web_url

log = logging.getLogger("paxs.accelerator")

# Cap concurrency so a misconfigured worker count can never open a silly
# number of sockets against a peer.
_MAX_WORKERS = 16


def accelerated_stow_upload(node, targets: list[dict], workers: int | None = None,
                            batch_size: int | None = None, retries: int = 1,
                            on_progress: Callable[[int, int], None] | None = None) -> dict:
    """Push `targets` to ``node.web_url`` over parallel streamed STOW-RS.

    `targets` is the :func:`app.dicom.sender.resolve_keys_sync` shape:
    ``[{"sop_uid", "key", "study_uid"}, ...]``. Instances are grouped into
    batches of ``batch_size`` (default ``TRANSFER_ACCELERATOR_BATCH_SIZE``)
    and up to ``workers`` batches upload concurrently (default
    ``TRANSFER_ACCELERATOR_WORKERS``), each streaming its multipart body with
    chunked transfer encoding.

    ``on_progress(done, total)`` — when given, called after each batch
    resolves (success or final failure) with the cumulative confirmed-sent
    count and the total instance count, so a caller can stream live progress
    to a client. ``done`` only counts instances the remote acknowledged, so a
    partially failing upload never reports a falsely complete bar.

    Returns the ``stow_upload`` shape plus accelerator stats:
    ``{ok, sent, failed, http_status, reason, results, streamed, bytes,
    accelerator, batches, workers, batch_size, retries, elapsed_seconds,
    mb_per_sec}`` (``mb_per_sec`` ≈ megabytes per second).
    ``accelerator=False`` (with a plain HTTP upload) when the feature is
    disabled. Raises ValueError when the node is unsafe (SSRF guard
    re-check).
    """
    if not targets:
        raise ValueError("no instances to upload")

    if not settings.TRANSFER_ACCELERATOR_ENABLED:
        # Feature switched off: still transfer over HTTP (STOW-RS), just not
        # in parallel — never silently fall back to a DIMSE association.
        # (stow_upload performs its own per-call SSRF re-check.)
        out = stow_upload(node, targets, stream=False)
        return {**out, "accelerator": False}

    validate_web_url(node.web_url)  # per-call re-check (DNS rebinding guard)

    workers = max(1, min(int(workers or settings.TRANSFER_ACCELERATOR_WORKERS), _MAX_WORKERS))
    batch_size = max(1, int(batch_size or settings.TRANSFER_ACCELERATOR_BATCH_SIZE))
    batches = [targets[i:i + batch_size] for i in range(0, len(targets), batch_size)]

    def _run_batch(batch: list[dict]) -> dict:
        """One streamed STOW-RS upload; never raises."""
        try:
            return stow_upload(node, batch, stream=True)
        except Exception as exc:  # connection refused/reset, timeout, DNS…
            log.warning(
                "transfer_accelerator_batch_upload_failed error_type=%s",
                type(exc).__name__,
            )
            return {
                "ok": False, "sent": 0, "failed": len(batch),
                "http_status": None, "reason": str(exc), "bytes": 0,
                "results": [
                    {"sop_uid": t["sop_uid"], "study_uid": t.get("study_uid"),
                     "ok": False, "reason": str(exc)}
                    for t in batch
                ],
            }

    def _transient(out: dict) -> bool:
        """True when the whole batch failed in a way worth retrying (network
        error / HTTP >= 500). Per-instance rejections inside a successful
        response are final — retrying would re-POST rejected data."""
        status = out.get("http_status")
        return status is None or (status is not None and status >= 500)

    start = time.monotonic()
    sent = failed = 0
    total_bytes = 0
    http_status: int | None = None
    results: list[dict] = []

    pending = list(batches)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for attempt in range(retries + 1):
            if not pending:
                break
            futures = {pool.submit(_run_batch, b): b for b in pending}
            next_pending: list[list[dict]] = []
            for fut in as_completed(futures):
                batch, out = futures[fut], fut.result()
                if _transient(out) and attempt < retries:
                    next_pending.append(batch)  # whole-batch retry on the next pass
                    continue
                total_bytes += out.get("bytes", 0)
                http_status = out.get("http_status") or http_status
                for item in out.get("results", []):
                    if item.get("ok"):
                        sent += 1
                    else:
                        failed += 1
                    results.append(item)  # every item, ok or not (audit + UI)
                if on_progress is not None:
                    on_progress(sent, len(targets))
            pending = next_pending

    elapsed = time.monotonic() - start
    return {
        "ok": failed == 0 and sent > 0,
        "sent": sent,
        "failed": failed,
        "http_status": http_status,
        "reason": None if failed == 0 else "one or more batches failed",
        "results": results,
        "streamed": True,
        "bytes": total_bytes,
        "accelerator": True,
        "batches": len(batches),
        "workers": workers,
        "batch_size": batch_size,
        "retries": retries,
        "elapsed_seconds": round(elapsed, 3),
        "mb_per_sec": round(total_bytes / 1e6 / elapsed, 2) if elapsed > 0 and total_bytes else 0.0,
    }
