"""Raw voxel streaming: request plan, L2 cache and payload assembly (Q-6).

``api/v1/volume.py:volume_data_endpoint`` was 196 lines: range parsing, format
validation, an NPY header, a Content-Length computation, two L2 cache
round-trips and the payload build, all inside the routing layer. None of it is
HTTP shape — it is "what bytes does this request mean" — so it lives here.

The split is **plan → cap → cache → render**:

* :func:`plan_volume_stream` resolves the bundle *and* every derived number
  (sub-range, sampled dimensions, exact payload length, cache keys) once. The
  router checks the byte cap against ``plan.payload_len`` and builds headers
  from the same numbers, so the declared ``Content-Length`` and the body cannot
  disagree — the bug class H13 was about.
* :func:`read_cached_stream` / :func:`render_volume_stream` do the blocking
  Redis and pixel work off the event loop (N-4). The previous implementation
  called Redis synchronously from the ``async def`` handler.

Refusals are typed rather than ``HTTPException`` so the service does not import
FastAPI: :class:`VolumeStreamError` carries the status the refusal *means*
(404/400/416), and the router is the only place that knows about HTTP.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import numpy as np
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.concurrency import run_blocking
from app.services.volume.assembly import (
    iter_volume_bytes,
    stack_fingerprint,
    volume_bundle,
    volume_byte_length,
)
from app.services.volume.slices import VolumeVariant, npy_header, strided_dim, unique_slices

log = logging.getLogger("paxs.volume.stream")

#: Upper bound on ``?downsample=``. Above 64 an axial CT is a handful of pixels;
#: the clamp prevents requests for an effectively empty volume.
MAX_DOWNSAMPLE = 64

#: Cache only small complete payloads in one Redis value; larger streams bypass L2.
L2_MAX_BYTES = 4 * 1024 * 1024
L2_CHUNK_MAX_BYTES = L2_MAX_BYTES
STREAM_CHUNK_BYTES = 1024 * 1024
L2_TTL_SECONDS = 3600

#: What an optional L2 call can raise: the client's own error family, plus the
#: socket/value errors a dead or misconfigured Redis surfaces as. Narrow on
#: purpose (Q-8) — anything else is a programming error and must propagate.
_REDIS_CALL_ERRORS = (RedisError, OSError, ValueError, TypeError)


class VolumeStreamError(Exception):
    """A refused stream request, carrying the status it means.

    ``http_status`` is the *semantics* of the refusal (a missing series is a
    404, an out-of-range slice window a 416, a bad format a 400), not a transport
    detail: the router maps it to ``HTTPException`` in one place.
    """

    http_status = 400


class VolumeStreamNotFound(VolumeStreamError):
    """No readable volumetric series inside the caller's scope."""

    http_status = 404


class VolumeStreamRangeError(VolumeStreamError):
    """The requested slice window is empty or above ``VOLUME_MAX_SLICES``."""

    http_status = 416


class VolumeStreamFormatError(VolumeStreamError):
    """An unusable ``format``/``downsample`` value."""

    http_status = 400


@dataclass(frozen=True)
class VolumeStreamPlan:
    """Everything one raw-stream request means, resolved before any work.

    ``payload_len`` is the exact body length (``Content-Length``), computed from
    the same dimensions the renderer uses. ``slices`` is the full deduplicated
    stack; ``start``/``end``/``downsample`` select from it, so a sub-range stream
    never copies the stack.
    """

    series_uid: str
    slices: list
    start: int
    end: int
    count: int
    downsample: int
    rescale: bool
    fmt: str
    out_count: int
    out_rows: int
    out_cols: int
    samples: int
    dtype: str
    payload_len: int
    media_type: str
    cache_key: str
    canonical_key: str | None
    derived_resampled: bool = False
    gantry_tilt_corrected: bool = False

    @property
    def shape(self) -> tuple[int, ...]:
        """The numpy shape of the payload (samples-per-pixel becomes an axis)."""
        if self.samples > 1:
            return (self.out_count, self.out_rows, self.out_cols, self.samples)
        return (self.out_count, self.out_rows, self.out_cols)


def parse_slice_range(raw: str | None, total: int) -> tuple[int, int]:
    """Parse ``?slices=`` into a half-open ``[start, end)`` window over ``total``.

    Accepts ``all``/``*``/empty (the whole stack) or ``<start>-<end>`` with an
    inclusive end; a single index selects one slice. Out-of-range windows are a
    416 rather than a silent clamp, so a client that asked for the wrong study
    finds out instead of receiving the wrong pixels.
    """
    if not raw or raw.strip().lower() in ("", "all", "*"):
        return 0, total
    text = raw.strip().replace(":", "-")
    try:
        parts = text.split("-")
        if len(parts) == 1:
            index = int(parts[0])
            start, end = index, index
        elif len(parts) == 2:
            start, end = int(parts[0]), int(parts[1])
        else:
            raise ValueError
    except (TypeError, ValueError):
        raise VolumeStreamFormatError(
            "slices must be 'all' or '<start>-<end>' (0-indexed, inclusive)"
        ) from None
    start = max(start, 0)
    end = min(end, total - 1)
    if start > end:
        raise VolumeStreamRangeError(f"Slice range out of bounds (0-{total - 1})")
    return start, end + 1


def _parse_downsample(raw: str | None) -> int:
    """``?downsample=`` clamped to ``1..MAX_DOWNSAMPLE`` (default 1)."""
    try:
        value = int(raw or 1)
    except (TypeError, ValueError):
        raise VolumeStreamFormatError("downsample must be an integer") from None
    return max(1, min(MAX_DOWNSAMPLE, value))


def _parse_format(raw: str | None) -> str:
    fmt = str(raw or "raw").lower()
    if fmt not in ("raw", "npy"):
        raise VolumeStreamFormatError("format must be 'raw' or 'npy'")
    return fmt


def _cache_keys(series_uid: str, tenant_id: int | None, plan_fields: dict) -> tuple[str, str | None]:
    """L2 keys for the request and, for a canonical full stream, its alias.

    The tenant token is part of the key (C-6): two tenants may hold the same
    SeriesInstanceUID, and a cache keyed by UID alone served one institution's
    pixels to another. The canonical key lets an ``?slices=all`` request reuse a
    full-stream entry written by a request that happened to spell it out.
    """
    tenant_token = "none" if tenant_id is None else str(tenant_id)
    request_key = (
        f"paxs:vol:stream:tenant:{tenant_token}:{series_uid}:{plan_fields['fingerprint']}"
        f":{plan_fields['start']}_{plan_fields['end']}_{plan_fields['downsample']}"
        f"_{int(plan_fields['rescale'])}_{plan_fields['fmt']}"
    )
    is_canonical = (
        plan_fields["start"] == 0
        and plan_fields["end"] == plan_fields["total"]
        and plan_fields["downsample"] == 1
        and not plan_fields["rescale"]
        and plan_fields["fmt"] == "raw"
    )
    canonical = (
        f"paxs:vol:stream:tenant:{tenant_token}:{series_uid}:{plan_fields['fingerprint']}:{plan_fields['total']}"
        if is_canonical
        else None
    )
    return request_key, canonical


@dataclass(frozen=True)
class _StreamOptions:
    """The validated request: window, sampling, rescale flag and container."""

    start: int
    end: int
    count: int
    downsample: int
    rescale: bool
    fmt: str


def _resolve_options(info: dict, params: dict, max_slices: int) -> _StreamOptions:
    """Validate ``?slices``/``?rescale``/``?downsample``/``?format`` once.

    ``max_slices`` is threaded in rather than read from settings here so the
    refusal can be exercised without mutating global configuration.
    """
    start, end = parse_slice_range(params.get("slices"), info["slices"])
    source_count = end - start
    if source_count <= 0:
        raise VolumeStreamRangeError(f"Slice range out of bounds (0-{info['slices'] - 1})")
    downsample = _parse_downsample(params.get("downsample"))
    count = len(range(start, end, downsample))
    if count > max_slices:
        raise VolumeStreamRangeError(
            f"Output slice count exceeds VOLUME_MAX_SLICES ({max_slices})"
        )
    return _StreamOptions(
        start=start,
        end=end,
        count=count,
        downsample=downsample,
        rescale=str(params.get("rescale", "")).lower() in ("1", "true", "yes"),
        fmt=_parse_format(params.get("format")),
    )


def _payload_geometry(
    info: dict, slices: list, options: _StreamOptions
) -> tuple[int, int, int, int, str, int]:
    """Derive ``(out_count, out_rows, out_cols, samples, dtype, payload_len)``.

    The sampled dimensions come from the shared ``strided_dim`` (ceil division,
    H13) — using floor division here produced ``X-Volume-*`` headers and an NPY
    header that disagreed with the array, so the length is derived from the same
    function the sampler strides with.
    """
    rows = info["rows"]
    cols = info["columns"]
    samples = info["samples_per_pixel"]
    out_rows = strided_dim(rows, options.downsample)
    out_cols = strided_dim(cols, options.downsample)
    out_count = len(slices[options.start:options.end:options.downsample])
    dtype = "float32" if options.rescale else info["dtype"]

    if options.fmt == "raw":
        payload_len = volume_byte_length(
            slices, info["dtype"], rows, cols, samples,
            start=options.start, end=options.end, downsample=options.downsample,
        )
        if options.rescale:
            payload_len = out_count * out_rows * out_cols * samples * 4
    else:
        header_shape = (
            (out_count, out_rows, out_cols, samples)
            if samples > 1
            else (out_count, out_rows, out_cols)
        )
        payload_len = out_count * out_rows * out_cols * samples * np.dtype(dtype).itemsize
        payload_len += len(npy_header(dtype, header_shape))
    return out_count, out_rows, out_cols, samples, dtype, payload_len


async def plan_volume_stream(
    db: AsyncSession,
    series_uid: str,
    *,
    tenant_id: int | None,
    include_all: bool,
    params: dict,
    max_slices: int,
    variant: VolumeVariant = "auto",
) -> VolumeStreamPlan:
    """Resolve one raw-stream request: bundle, window, dimensions, length, keys.

    Raises :class:`VolumeStreamNotFound` before anything else is parsed, so an
    unknown series answers 404 rather than leaking a range error.
    """
    bundle = await volume_bundle(
        db, series_uid, tenant_id=tenant_id, include_all=include_all, variant=variant
    )
    if bundle is None:
        raise VolumeStreamNotFound("No readable volumetric series found")
    info, slices = bundle
    # B-10: a duplicated frame identity would be decoded, emitted and counted
    # twice, inflating the body and the byte estimate the cap is checked against.
    slices = unique_slices(slices)

    options = _resolve_options(info, params, max_slices)
    out_count, out_rows, out_cols, samples, dtype, payload_len = _payload_geometry(
        info, slices, options
    )
    cache_key, canonical_key = _cache_keys(series_uid, tenant_id, {
        "start": options.start, "end": options.end, "downsample": options.downsample,
        "rescale": options.rescale, "fmt": options.fmt, "total": info["slices"],
        "fingerprint": info.get("stack_fingerprint") or stack_fingerprint(slices),
    })

    return VolumeStreamPlan(
        series_uid=series_uid,
        slices=slices,
        start=options.start,
        end=options.end,
        count=options.count,
        downsample=options.downsample,
        rescale=options.rescale,
        fmt=options.fmt,
        out_count=out_count,
        out_rows=out_rows,
        out_cols=out_cols,
        samples=samples,
        dtype=dtype,
        payload_len=payload_len,
        media_type=("application/x-npy" if options.fmt == "npy"
                    else "application/octet-stream"),
        cache_key=cache_key,
        canonical_key=canonical_key,
        derived_resampled=bool(info.get("derived_resampled", False)),
        gantry_tilt_corrected=bool(info.get("gantry_tilt_corrected", False)),
    )


def _read_cached(plan: VolumeStreamPlan) -> bytes | None:
    """The L2 entry for this plan, or None (miss, disabled, or broken Redis).

    Run in a worker thread: the Redis client is synchronous, and a stalled L2
    must not block every other request on the loop (N-4). A broken cache is
    logged and treated as a miss — L1/disk still serve the request (N-5).
    """
    from app.services.redis_service import get_redis_client, redis_circuit_call

    if not getattr(settings, "REDIS_CACHE_ENABLED", False):
        return None
    client = get_redis_client()
    if client is None:
        return None
    try:
        cached = None
        for key in (plan.cache_key, plan.canonical_key):
            if not key:
                continue
            # Do not fetch an old monolithic entry before proving it is small.
            if not hasattr(client, "strlen") or redis_circuit_call(client.strlen, key) > L2_CHUNK_MAX_BYTES:
                continue
            cached = redis_circuit_call(client.get, key)
            if cached is not None:
                break
        if cached is not None and len(cached) == plan.payload_len:
            return cached
    except _REDIS_CALL_ERRORS as exc:  # L2 is optional; L1/disk still serve
        log.debug("volume_l2_cache_read_failed error_type=%s", type(exc).__name__)
    return None


async def read_cached_stream(plan: VolumeStreamPlan) -> bytes | None:
    """L2 lookup for ``plan``, off the event loop."""
    return await run_blocking(_read_cached, plan)


def _next_payload_chunk(iterator: Iterator[bytes]) -> bytes | None:
    """Read one decoded slice from the blocking iterator."""
    try:
        return next(iterator)
    except StopIteration:
        return None


def _write_cached(plan: VolumeStreamPlan, data: bytes) -> None:
    """Best-effort L2 write, capped at one small response value."""
    from app.services.redis_service import get_redis_client, redis_circuit_call

    if not getattr(settings, "REDIS_CACHE_ENABLED", False) or len(data) > L2_CHUNK_MAX_BYTES:
        return
    client = get_redis_client()
    if client is None:
        return
    try:
        redis_circuit_call(client.set, plan.cache_key, data, ex=L2_TTL_SECONDS)
        if plan.canonical_key and plan.canonical_key != plan.cache_key:
            redis_circuit_call(client.set, plan.canonical_key, data, ex=L2_TTL_SECONDS)
    except _REDIS_CALL_ERRORS as exc:  # L2 is optional; the response is already built
        log.debug("volume_l2_cache_write_failed error_type=%s", type(exc).__name__)


async def render_volume_stream(plan: VolumeStreamPlan) -> AsyncIterator[bytes]:
    """Yield the voxel payload in bounded chunks and cache only small responses."""
    cache_data = bytearray() if plan.payload_len <= L2_CHUNK_MAX_BYTES else None
    iterator = iter_volume_bytes(
        plan.slices,
        rescale=plan.rescale,
        start=plan.start,
        end=plan.end,
        downsample=plan.downsample,
    )
    header = npy_header(plan.dtype, plan.shape) if plan.fmt == "npy" else b""
    if header:
        if cache_data is not None:
            cache_data.extend(header)
        for offset in range(0, len(header), STREAM_CHUNK_BYTES):
            yield header[offset:offset + STREAM_CHUNK_BYTES]
    while True:
        data = await run_blocking(_next_payload_chunk, iterator)
        if data is None:
            break
        if cache_data is not None:
            cache_data.extend(data)
        for offset in range(0, len(data), STREAM_CHUNK_BYTES):
            yield data[offset:offset + STREAM_CHUNK_BYTES]
        del data
    if cache_data is not None and len(cache_data) == plan.payload_len:
        await run_blocking(_write_cached, plan, bytes(cache_data))


__all__ = [
    "L2_MAX_BYTES",
    "L2_TTL_SECONDS",
    "MAX_DOWNSAMPLE",
    "VolumeStreamError",
    "VolumeStreamFormatError",
    "VolumeStreamNotFound",
    "VolumeStreamPlan",
    "VolumeStreamRangeError",
    "parse_slice_range",
    "plan_volume_stream",
    "read_cached_stream",
    "render_volume_stream",
]
