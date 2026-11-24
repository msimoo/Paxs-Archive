"""The server-side voxel cache (L1 memory + L2 Redis).

``load_volume_array`` is the only entry point: it consults the per-process
``_VOL_CACHE``, then Redis, then assembles from storage. Both tiers are bounded
(``volume_cache_budget_bytes``) and the in-flight map collapses concurrent
misses onto one assembly (N-5: no unbounded resource).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

import numpy as np

from app.core import cache_stats
from app.core.config import settings
from app.services import storage_service
from app.services.volume.assembly import (
    _expected_shape,
    _frame_array,
    stack_fingerprint,
    volume_cache_key,
    volume_redis_key,
)
from app.services.volume.slices import VolumeSlice
from app.services.volume.calibration import apply_frame_calibration, resolve_frame_calibration
from app.services.volume.deskew import DeskewPlan, build_deskew_plan_from_slices, deskew_slice_2d

log = logging.getLogger("paxs.volume")

__all__ = [
    "_IN_FLIGHT_MAX_WAITS",
    "_IN_FLIGHT_VOLUMES",
    "_IN_FLIGHT_VOLUMES_LOCK",
    "_IN_FLIGHT_WAIT_SECONDS",
    "_VOL_CACHE",
    "_VOL_CACHE_LOCK",
    "_VOL_REDIS_TTL",
    "VolumeAssemblyCapacityError",
    "_vol_cache_put_locked",
    "_vol_redis_get",
    "_vol_redis_set",
    "load_volume_array",
    "volume_cache_budget_bytes",
    "volume_cache_stats",
    "volume_assembly_budget_bytes",
    "volume_assembly_stats",
]


_VOL_CACHE: OrderedDict[str, np.ndarray] = OrderedDict()


_VOL_CACHE_LOCK = threading.Lock()
_VOL_CACHE_BUDGET_BYTES: int | None = None


class VolumeAssemblyCapacityError(RuntimeError):
    """A volume build cannot reserve its bounded temporary working set."""


class VolumeAssemblyTooLargeError(VolumeAssemblyCapacityError):
    """A volume's minimum working set exceeds the configured assembly budget."""


_ASSEMBLY_CONDITION = threading.Condition()
_ASSEMBLY_RESERVED_BYTES = 0
_ASSEMBLY_WAITERS = 0
_ACTIVE_VOLUME_BUILDS = 0


def volume_assembly_budget_bytes() -> int:
    """Return the live temporary-work budget; zero disables cache-miss builds."""
    return max(0, int(settings.VOLUME_ASSEMBLY_BUDGET_MB)) * 1024 * 1024


def volume_assembly_stats() -> dict[str, int]:
    """Return PHI-free counters for live assembly admission."""
    with _ASSEMBLY_CONDITION:
        stats = {
            "budget_bytes": volume_assembly_budget_bytes(),
            "reserved_bytes": _ASSEMBLY_RESERVED_BYTES,
            "waiters": _ASSEMBLY_WAITERS,
        }
    with _IN_FLIGHT_VOLUMES_LOCK:
        stats["inflight_keys"] = len(_IN_FLIGHT_VOLUMES)
        stats["active_builds"] = _ACTIVE_VOLUME_BUILDS
    return stats


def _reserve_volume_assembly(estimated_bytes: int) -> int:
    """Reserve estimated peak bytes or refuse after the bounded wait policy."""
    budget = volume_assembly_budget_bytes()
    if estimated_bytes <= 0 or estimated_bytes > budget:
        raise VolumeAssemblyTooLargeError(
            "Estimated volume working set exceeds the configured assembly budget"
        )
    max_waiters = max(0, int(settings.VOLUME_ASSEMBLY_MAX_WAITERS))
    wait_seconds = max(0.0, float(settings.VOLUME_ASSEMBLY_WAIT_SECONDS))
    deadline = time.monotonic() + wait_seconds
    global _ASSEMBLY_RESERVED_BYTES, _ASSEMBLY_WAITERS
    with _ASSEMBLY_CONDITION:
        if _ASSEMBLY_RESERVED_BYTES + estimated_bytes <= budget:
            _ASSEMBLY_RESERVED_BYTES += estimated_bytes
            return estimated_bytes
        if max_waiters == 0 or _ASSEMBLY_WAITERS >= max_waiters:
            raise VolumeAssemblyCapacityError(
                "Volume assembly capacity is busy; retry the request"
            )
        _ASSEMBLY_WAITERS += 1
        try:
            while _ASSEMBLY_RESERVED_BYTES + estimated_bytes > budget:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VolumeAssemblyCapacityError(
                        "Volume assembly capacity is busy; retry the request"
                    )
                _ASSEMBLY_CONDITION.wait(timeout=remaining)
            _ASSEMBLY_RESERVED_BYTES += estimated_bytes
            return estimated_bytes
        finally:
            _ASSEMBLY_WAITERS -= 1


def _release_volume_assembly(reserved_bytes: int) -> None:
    """Release a successful or failed assembly reservation and wake waiters."""
    global _ASSEMBLY_RESERVED_BYTES
    with _ASSEMBLY_CONDITION:
        _ASSEMBLY_RESERVED_BYTES = max(0, _ASSEMBLY_RESERVED_BYTES - reserved_bytes)
        _ASSEMBLY_CONDITION.notify_all()


def _estimated_assembly_bytes(slices: list[VolumeSlice], shape: tuple[int, ...]) -> int:
    """Conservative build peak from output, largest decoded source, and file bytes."""
    voxel_count = len(slices)
    for dimension in shape:
        if dimension <= 0:
            raise ValueError("Volume source has invalid pixel dimensions")
        voxel_count *= dimension
    output_bytes = voxel_count * np.dtype(np.float32).itemsize
    max_instance_bytes = max(
        (int(item.decoded_instance_bytes or 0) for item in slices), default=0
    )
    max_file_bytes = max(
        (int(item.file_size_bytes or 0) for item in slices), default=0
    )
    # L2 serialization temporarily holds both ``tobytes()`` and its framed copy.
    output_copies = 3 if getattr(settings, "REDIS_CACHE_ENABLED", False) else 1
    working_bytes = output_bytes * output_copies + max_instance_bytes + max_file_bytes
    return (working_bytes * 5 + 3) // 4


def _decode_grouped_volume(
    slices: list[VolumeSlice],
    shape: tuple[int, ...],
    deskew_plan: DeskewPlan | None = None,
) -> np.ndarray:
    """Fill one output volume while decoding only one source object at a time."""
    out_shape = (
        (deskew_plan.out_rows, deskew_plan.out_cols, *shape[2:])
        if deskew_plan is not None and deskew_plan.active
        else shape
    )
    volume = np.empty((len(slices), *out_shape), dtype=np.float32)
    groups: dict[tuple[str | None, str], list[tuple[int, VolumeSlice]]] = {}
    for index, item in enumerate(slices):
        groups.setdefault((item.backend, item.file_path), []).append((index, item))
    for (_backend, file_path), indexed_slices in groups.items():
        source = indexed_slices[0][1]
        dataset = storage_service.read_dataset(file_path, backend_name=source.backend)
        dataset_cache = {file_path: dataset}
        for index, current in indexed_slices:
            pixels = _frame_array(current, dataset_cache)
            if tuple(pixels.shape) != shape:
                raise ValueError(
                    f"slice {current.sop_uid} shape {tuple(pixels.shape)} diverges "
                    f"from stack shape {shape} — not a uniform volume"
                )
            calibration = current.calibration or resolve_frame_calibration(
                dataset, current.frame or 0
            )
            calibrated = apply_frame_calibration(pixels, calibration)
            if deskew_plan is not None and deskew_plan.active:
                volume[index] = deskew_slice_2d(calibrated, deskew_plan, index)
            else:
                volume[index] = calibrated
        del pixels, dataset_cache, dataset
    return volume


def _assemble_volume_array(
    slices: list[VolumeSlice],
    cache_key: str,
    alt_cache_key: str,
    series_uid: str | None,
) -> np.ndarray:
    """Read dimensions, reserve the bounded peak, decode, and populate caches."""
    first = storage_service.read_dataset(
        slices[0].file_path,
        stop_before_pixels=True,
        backend_name=slices[0].backend,
    )
    shape = _expected_shape(first)
    del first
    deskew_plan = build_deskew_plan_from_slices(slices, shape[:2])
    out_shape = (
        (deskew_plan.out_rows, deskew_plan.out_cols, *shape[2:])
        if deskew_plan is not None and deskew_plan.active
        else shape
    )
    reserved = _reserve_volume_assembly(_estimated_assembly_bytes(slices, out_shape))
    try:
        try:
            volume = _decode_grouped_volume(slices, shape, deskew_plan=deskew_plan)
        except MemoryError as exc:
            raise VolumeAssemblyCapacityError(
                "Insufficient memory to assemble the requested volume"
            ) from exc
        with _VOL_CACHE_LOCK:
            _vol_cache_put_locked(cache_key, volume)
        if getattr(settings, "REDIS_CACHE_ENABLED", False):
            _vol_redis_set(volume_redis_key(cache_key), volume)
            if series_uid and alt_cache_key != cache_key:
                _vol_redis_set(volume_redis_key(alt_cache_key), volume)
        return volume
    finally:
        _release_volume_assembly(reserved)


def volume_cache_budget_bytes() -> int:
    """Current budget from settings (re-read each call so an override applies live).

    ``0`` means **caching disabled** and is returned as 0, not masked with a
    default: an ``or`` fallback would silently turn a deliberate disable into the
    full default budget.
    """
    raw = getattr(settings, "VOLUME_CACHE_BUDGET_MB", 1024)
    try:
        mb = int(raw)
    except (TypeError, ValueError) as exc:
        log.warning(
            "Invalid VOLUME_CACHE_BUDGET_MB configuration; using default (error_type=%s)",
            type(exc).__name__,
        )
        mb = 1024
    return max(0, mb) * 1024 * 1024


def volume_cache_stats() -> dict[str, int]:
    """Counters + gauges for the L1 volume cache (used by tests and /metrics)."""
    return cache_stats.snapshot()["volume"]


def _trim_volume_cache_locked(budget: int) -> None:
    """Evict least-recently-used arrays until the live budget is satisfied."""
    global _VOL_CACHE_BUDGET_BYTES
    total = sum(int(volume.nbytes) for volume in _VOL_CACHE.values())
    while _VOL_CACHE and total > budget:
        _key, evicted = _VOL_CACHE.popitem(last=False)
        total -= int(evicted.nbytes)
        cache_stats.incr("volume", "evictions")
    cache_stats.set_gauges("volume", bytes=total, entries=len(_VOL_CACHE))
    _VOL_CACHE_BUDGET_BYTES = budget


def _sync_volume_cache_budget_locked(budget: int) -> None:
    """Reconcile cached arrays only when the live configured budget changes."""
    if _VOL_CACHE_BUDGET_BYTES != budget:
        _trim_volume_cache_locked(budget)


def _vol_cache_put_locked(cache_key: str, vol: np.ndarray) -> None:
    """Insert into L1 with byte accounting + LRU eviction. Caller holds the lock.

    The total is recomputed from the dict rather than tracked as a running
    delta: ``_VOL_CACHE.clear()`` is called directly by tests and by cache
    resets, and a stale counter would then evict everything or nothing.
    """
    budget = volume_cache_budget_bytes()
    _trim_volume_cache_locked(budget)
    if budget <= 0 or int(vol.nbytes) > budget:
        # Never admit a volume that cannot fit, even when the cache is empty.
        _VOL_CACHE.pop(cache_key, None)
        _trim_volume_cache_locked(budget)
        return

    _VOL_CACHE.pop(cache_key, None)
    _VOL_CACHE[cache_key] = vol
    cache_stats.incr("volume", "inserts")
    _trim_volume_cache_locked(budget)


_IN_FLIGHT_VOLUMES: dict[str, threading.Event] = {}


_IN_FLIGHT_VOLUMES_LOCK = threading.Lock()


_IN_FLIGHT_WAIT_SECONDS = 180.0


_IN_FLIGHT_MAX_WAITS = 3


_VOL_REDIS_TTL = 3600


def _claim_volume_event(cache_key: str) -> tuple[threading.Event, bool]:
    """Claim a coalescing event or join the active build for this key."""
    with _IN_FLIGHT_VOLUMES_LOCK:
        current = _IN_FLIGHT_VOLUMES.get(cache_key)
        if current is not None:
            return current, False
        _claim_volume_build_slot_locked()
        event = threading.Event()
        _IN_FLIGHT_VOLUMES[cache_key] = event
        return event, True


def _claim_volume_build_slot_locked() -> None:
    """Reserve one build slot while holding ``_IN_FLIGHT_VOLUMES_LOCK``."""
    global _ACTIVE_VOLUME_BUILDS
    maximum = max(0, int(settings.VOLUME_ASSEMBLY_MAX_INFLIGHT))
    if maximum == 0 or _ACTIVE_VOLUME_BUILDS >= maximum:
        raise VolumeAssemblyCapacityError(
            "Active volume build limit reached; retry after capacity is available"
        )
    _ACTIVE_VOLUME_BUILDS += 1


def _claim_uncoalesced_volume_build() -> None:
    """Reserve a bounded fallback build after repeated coalesced-owner failures."""
    with _IN_FLIGHT_VOLUMES_LOCK:
        _claim_volume_build_slot_locked()


def _release_volume_build_slot() -> None:
    """Release one active materialization slot and preserve a non-negative gauge."""
    global _ACTIVE_VOLUME_BUILDS
    with _IN_FLIGHT_VOLUMES_LOCK:
        _ACTIVE_VOLUME_BUILDS = max(0, _ACTIVE_VOLUME_BUILDS - 1)


def load_volume_array(slices: list[VolumeSlice], series_uid: str | None = None, *,  # noqa: PLR0915 - one decode pass; splitting it would re-read slices
                      tenant_id: int | None = None,
                      study_uid: str | None = None) -> np.ndarray:
    """Materialize the stack as one float32 HU array, shape (nz, ny, nx).

    Supports multi-node Redis binary cache with local LRU fallback. Multi-frame
    / Enhanced instances are expanded into per-frame slices, so each entry here
    is one frame of the source instance. This is CPU + disk heavy (every slice
    is read and decoded); callers run it via ``asyncio.to_thread``.

    Concurrent callers for the *same* series are coalesced (V3-1): the first one
    materialises the volume and the rest block on a ``threading.Event`` and then
    read the finished array out of ``_VOL_CACHE``, instead of each re-reading and
    re-stacking ~500 MB of voxels in parallel.

    **C-6:** the cache key is tenant-scoped
    (``tenant:{tenant_id}:volume:{study_uid}:{series_uid}:{frame_hash}``) and
    keyed by the stack's frame fingerprint rather than its slice count. The
    tenant is part of the identity because voxels are PHI: without it, a second
    tenant holding the same StudyInstanceUID/SeriesInstanceUID would read the
    first tenant's pre-decoded array straight out of the shared L1/Redis cache.
    """
    if not slices:
        return np.empty((0, 0, 0), dtype=np.float32)
    fingerprint = stack_fingerprint(slices)
    primary_series = series_uid or slices[0].sop_uid
    cache_key = volume_cache_key(tenant_id, study_uid, primary_series, fingerprint)
    alt_cache_key = volume_cache_key(tenant_id, study_uid, slices[0].sop_uid, fingerprint)

    def _cached_any() -> np.ndarray | None:
        with _VOL_CACHE_LOCK:
            _sync_volume_cache_budget_locked(volume_cache_budget_bytes())
            # Resolve WHICH key hit before touching recency: move_to_end raises
            # KeyError for an absent key, and the hit may have come via the
            # alternate key.
            key = cache_key if cache_key in _VOL_CACHE else (
                alt_cache_key if alt_cache_key in _VOL_CACHE else None
            )
            if key is None:
                cache_stats.incr("volume", "misses")
                return None
            _VOL_CACHE.move_to_end(key, last=True)   # V3-6: LRU, not insertion order
            cache_stats.incr("volume", "hits")
            return _VOL_CACHE[key]

    # 1. Check local fast LRU cache
    cached = _cached_any()
    if cached is not None:
        return cached

    # 2. Check Redis cluster cache if available (shared pooled client)
    if getattr(settings, "REDIS_CACHE_ENABLED", False):
        vol = _vol_redis_get(volume_redis_key(cache_key))
        if vol is None and alt_cache_key != cache_key:
            vol = _vol_redis_get(volume_redis_key(alt_cache_key))
        if vol is not None:
            with _VOL_CACHE_LOCK:
                _vol_cache_put_locked(cache_key, vol)
            return vol

    # 3. Single-flight (V3-1): claim ownership of this cache_key, or wait for
    #    the thread that already owns it. The loop matters — after a waiter is
    #    woken it must RE-CHECK the cache and, if the owner failed without
    #    caching anything, try to claim the key itself. Exactly one thread
    #    materialises per round; the rest read the finished array out of
    #    _VOL_CACHE. (Unconditionally re-claiming on wake made every waiter an
    #    owner and re-introduced the parallel decode this exists to prevent.)
    owns_event = False
    owns_build_slot = False
    event: threading.Event | None = None
    waits = 0
    max_waits = max(0, int(getattr(settings, "VOLUME_SINGLEFLIGHT_MAX_WAITS", _IN_FLIGHT_MAX_WAITS)))
    wait_seconds = max(0.0, float(getattr(settings, "VOLUME_SINGLEFLIGHT_WAIT_SECONDS", _IN_FLIGHT_WAIT_SECONDS)))
    while True:
        event, owns_event = _claim_volume_event(cache_key)
        if owns_event:
            owns_build_slot = True
            break
        if waits >= max_waits:
            _claim_uncoalesced_volume_build()
            owns_build_slot = True
            break
        waits += 1
        event.wait(timeout=wait_seconds)
        cached = _cached_any()
        if cached is not None:
            return cached

    try:
        return _assemble_volume_array(
            slices, cache_key, alt_cache_key, series_uid
        )
    finally:
        # Always release waiters and drop the claim, success or failure — an
        # exception here must not leave every later caller blocked until the
        # timeout expires. Only the thread that REGISTERED the event may release
        # it, so a non-owner can never prematurely wake another owner's waiters.
        if owns_event and event is not None:
            with _IN_FLIGHT_VOLUMES_LOCK:
                if _IN_FLIGHT_VOLUMES.get(cache_key) is event:
                    _IN_FLIGHT_VOLUMES.pop(cache_key, None)
            event.set()
        if owns_build_slot:
            _release_volume_build_slot()


def _vol_redis_get(r_key: str) -> np.ndarray | None:
    """Fetch a cached volume from the shared pooled Redis client (best-effort)."""
    from app.services.redis_service import get_redis_client, redis_circuit_call

    client = get_redis_client()  # shared pool, 1.5s socket timeouts
    if client is None:
        return None
    try:
        cached_bytes = redis_circuit_call(client.get, r_key)
        if not cached_bytes or len(cached_bytes) < 12:
            return None
        nz, ny, nx = np.frombuffer(cached_bytes[:12], dtype=np.int32)
        return np.frombuffer(cached_bytes[12:], dtype=np.float32).reshape((nz, ny, nx))
    except Exception as exc:  # noqa: BLE001 - boundary guard: best-effort cache read; logged type-only
        # Corrupt/unavailable cache bytes must never block canonical decoding.
        log.warning("Redis volume-cache GET failed (error_type=%s); treating as cache miss",
                    type(exc).__name__)
        return None


def _vol_redis_set(r_key: str, vol: np.ndarray) -> None:
    """Write a volume to the shared pooled Redis client (best-effort)."""
    from app.services.redis_service import get_redis_client, redis_circuit_call

    client = get_redis_client()
    if client is None:
        return
    try:
        shape_hdr = np.array(vol.shape, dtype=np.int32).tobytes()
        redis_circuit_call(client.set, r_key, shape_hdr + vol.tobytes(), ex=_VOL_REDIS_TTL)
    except Exception as exc:  # noqa: BLE001 - boundary guard: best-effort cache write; logged type-only
        # Redis is an acceleration tier only; the complete local volume remains
        # valid and cached, so failure here is a safe cache-write miss.
        log.warning(
            "Redis volume-cache SET failed (error_type=%s); local volume remains available",
            type(exc).__name__,
        )
