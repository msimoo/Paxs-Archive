"""Warm-on-open: pre-warm a study's read caches the moment a viewer opens it.

When a study is opened in the viewer (QIDO-RS with a specific
``StudyInstanceUID``, or a WADO-RS study-metadata fetch), ``schedule_warm``
enqueues a short warm pass on the shared **bounded** background pool
(:mod:`app.core.concurrency`, C-4) that:

1. pre-generates any missing 192px PNG thumbnails (``.thumbnails/{sop}.png``)
   so study-list / series thumbnail requests hit the pre-generated fast path,
2. warms the Redis thumbnail cache keys the WADO-RS thumbnail endpoint would
   compute, so even the rendered fallback path is instant,
3. pre-decodes the first few frames of the study into the in-process
   decoded-frame cache (``wado_rs_bulk_service._FRAME_CACHE``) so scrolling
   through the top of a series doesn't re-run the expensive J2K/JPEG-LS
   server-side decode.

Everything is best-effort: missing files, decode failures, or an unreachable
Redis are logged and skipped — the warm task never raises and never delays the
HTTP response that triggered it. Dedup (a Redis lock + in-process set) prevents
concurrent or rapid re-warms of the same study, and the pool's backlog cap is
the second line of defence: when a burst of opens saturates the pool the
duplicate warm is **dropped** rather than queued without bound (C-4).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.concurrency import run_blocking, submit_background
from app.core.config import settings
from app.db.models import Instance, Series, Study
from app.db.session import SyncSessionLocal
from app.services import storage_service, thumbnail_service

log = logging.getLogger("paxs.warm")

# In-process guard: studies currently being warmed (thread-safe set).
_WARMING: set[str] = set()
_WARMING_LOCK = threading.Lock()

# Redis lock TTL for the whole warm pass (safety against two nodes warming the
# same study concurrently; a stale lock expires and the next open retries).
_WARM_LOCK_TTL = 120


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def schedule_warm(study_uid: str) -> bool:
    """Enqueue a background warm pass for ``study_uid`` (deduped, bounded).

    Returns True when a warm pass was accepted (or is already running for this
    study — which counts as "taken care of"), False when warm-on-open is
    disabled, the study UID is empty, or the background pool is saturated and
    the task was dropped. Never blocks.
    """
    if not settings.WARM_ON_OPEN_ENABLED or not study_uid:
        return False

    with _WARMING_LOCK:
        if study_uid in _WARMING:
            return True  # already being warmed by a prior trigger
        _WARMING.add(study_uid)

    # C-4: the shared bounded pool, not a raw daemon thread per open. When the
    # pool is saturated the warm is dropped and the in-process guard released,
    # so a later open can retry instead of the study being stuck "warming".
    if not submit_background(_warm_worker, study_uid):
        with _WARMING_LOCK:
            _WARMING.discard(study_uid)
        log.debug("warm for study %s dropped: background pool saturated", study_uid)
        return False
    return True



# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
def _warm_worker(study_uid: str) -> None:
    try:
        _warm_study(study_uid)
    except Exception as exc:  # noqa: BLE001 - boundary guard: background warming must not escape the worker
        log.error("warm_pass_failed error_type=%s", type(exc).__name__)
    finally:
        with _WARMING_LOCK:
            _WARMING.discard(study_uid)


def _warm_instance(
    inst, r_client, frames_budget: int, tenant_id: int | None
) -> tuple[int, int, int]:
    """Warm one instance: thumbnail, Redis thumb key, first frame.

    Extracted from the study loop so each step stays readable and the loop
    itself is a three-line accumulator. Returns
    ``(thumbnails_generated, redis_keys_warmed, frames_cached)``; every step is
    best-effort and a failure is logged, never raised.
    """
    bk = getattr(inst, "storage_backend", None) or None
    if not storage_service.exists(inst.file_path, backend_name=bk):
        return 0, 0, 0

    thumb_ok = redis_ok = frames_ok = 0

    # 1) Missing pre-generated thumbnail (the WADO thumbnail fast path).
    thumb_key = storage_service.thumbnail_key(inst.sop_instance_uid)
    if (
        settings.THUMBNAIL_ENABLED
        and not storage_service.exists(thumb_key, backend_name=bk)
        and thumbnail_service.generate_thumbnail(
            inst.file_path, thumb_key, backend_name=bk
        )
    ):
        thumb_ok = 1

    # 2) Redis thumbnail cache key (the rendered fallback path's key,
    #    default 256px PNG — matches wado_thumbnail's cache_key).
    from app.services.redis_service import cache_get, cache_set

    from app.services import content_version as cv

    version = cv.version_of(inst)
    cache_key = cv.thumbnail_key(inst.sop_instance_uid, version, "auto", "256", "png")
    if cache_get(cache_key) is None:
        try:
            import io

            from app.services.imaging import dataset_to_image

            ds = storage_service.read_dataset(inst.file_path, backend_name=bk)
            img = dataset_to_image(ds, max_size=256)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            if cache_set(cache_key, buf.getvalue(), expire_seconds=3600):
                redis_ok = 1
        except Exception:  # noqa: BLE001 - boundary guard: a warm step must never fail the pass
            log.debug("Redis thumb warm failed for %s", inst.sop_instance_uid)

    # 3) Pre-decode the first frame into the in-process frame cache & Redis
    #    (bounded by WARM_MAX_FRAMES across the study).
    if frames_budget > 0:
        try:
            from app.services import wado_rs_bulk_service

            ds = storage_service.read_dataset(inst.file_path, backend_name=bk)
            decoded = wado_rs_bulk_service.decode_frames_to_raw(
                ds, [1], tenant_id=tenant_id, content_version=version
            )
            if decoded:
                frames_ok = 1
                frame_key = cv.raw_frame_key(tenant_id, inst.sop_instance_uid, version, 1)
                cache_set(
                    frame_key,
                    decoded[0][0], expire_seconds=3600,
                )
        except Exception:  # noqa: BLE001 - boundary guard: a warm step must never fail the pass
            log.debug("Frame warm failed for %s", inst.sop_instance_uid)

    return thumb_ok, redis_ok, frames_ok


async def _warm_vol_series(r_client, series_uids: list[str], *,
                           tenant_id: int | None,
                           study_uid: str | None) -> int:
    """Materialise volumetric series into the tenant-scoped volume cache.

    Every step is scoped to ``tenant_id`` (C-6): the slice query is tenant-
    filtered, and both the Redis presence probe and the cache write use the
    tenant-scoped key/pattern, so a warm pass for one institution can neither
    read nor populate another's cache entry.

    Runs on the background pool's worker thread (``asyncio.run`` owns the loop),
    so the sync DB session above is never used here.
    """
    from app.db.session import AsyncSessionLocal
    from app.services.volume_service import (
        load_volume_array,
        series_slices,
        volume_redis_scan_pattern,
    )
    from app.services.redis_service import redis_circuit_call

    warmed = 0
    async with AsyncSessionLocal() as a_session:
        for s_uid in series_uids:
            if r_client is not None:
                try:
                    _cursor, matched = await run_blocking(redis_circuit_call, r_client.scan,
                        cursor=0,
                        match=volume_redis_scan_pattern(tenant_id, s_uid),
                        count=1,
                    )
                    if matched:
                        continue  # already cached for this tenant
                except Exception as exc:  # noqa: BLE001 - boundary guard: a cache probe must never fail the warm
                    log.debug(
                        "volume_cache_probe_failed series_uid=%s error_type=%s",
                        s_uid, type(exc).__name__,
                    )
            try:
                slices = await series_slices(a_session, s_uid, tenant_id=tenant_id)
                if slices and len(slices) >= 4:
                    await run_blocking(load_volume_array,
                        slices, series_uid=s_uid,
                        tenant_id=tenant_id, study_uid=study_uid,
                    )
                    warmed += 1
            except Exception as v_exc:  # noqa: BLE001 - boundary guard: one series must not fail the pass
                log.debug(
                    "volume_pre_warm_failed series_uid=%s error_type=%s",
                    s_uid, type(v_exc).__name__,
                )
    return warmed


def _warm_study(study_uid: str) -> None:
    """One warm pass over a study's instances (bounded, best-effort)."""
    # Distributed dedup: skip if another node is already warming this study.
    from app.services.redis_service import get_redis_client

    r_client = get_redis_client()
    lock = None
    if r_client:
        try:
            lock = r_client.lock(f"paxs:warm:lock:{study_uid}", timeout=_WARM_LOCK_TTL)
            if not lock.acquire(blocking=False):
                log.debug("Another node is warming %s; skipping", study_uid)
                return
        except Exception as exc:  # noqa: BLE001 - boundary guard: the cache lock may be unavailable
            log.debug("warm_lock_skipped error_type=%s", type(exc).__name__)
            lock = None

    try:
        with SyncSessionLocal() as session:
            # The owning tenant scopes every cache this pass touches (C-6).
            tenant_id = session.execute(
                select(Study.tenant_id).where(Study.study_instance_uid == study_uid)
            ).scalar_one_or_none()
            instances = (
                session.execute(
                    select(Instance)
                    .join(Series, Instance.series_id == Series.id)
                    .join(Study, Series.study_id == Study.id)
                    .where(Study.study_instance_uid == study_uid)
                    .order_by(Instance.id)
                    .limit(settings.WARM_MAX_INSTANCES)
                )
                .scalars()
                .all()
            )
            # Read inside the session — the volumetric series list is needed by
            # the async pass below, which runs after this block closes.
            vol_series_list = (
                session.execute(
                    select(Series.series_instance_uid)
                    .join(Study, Series.study_id == Study.id)
                    .where(Study.study_instance_uid == study_uid)
                    .where(Series.modality.in_(["CT", "MR", "PT"]))
                )
                .scalars()
                .all()
                if getattr(settings, "REDIS_CACHE_ENABLED", False)
                else []
            )

        thumb_ok = 0
        redis_ok = 0
        frames_ok = 0
        for inst in instances:
            t, r, f = _warm_instance(
                inst, r_client, settings.WARM_MAX_FRAMES - frames_ok, tenant_id
            )
            thumb_ok += t
            redis_ok += r
            frames_ok += f

        # 4) Pre-warm 3D volume cache in Redis for volumetric series (CT/MR),
        #    under the study's tenant scope (C-6).
        vol_ok = 0
        if vol_series_list:
            try:
                # _warm_study is submitted to the shared bounded background
                # pool. Own the loop here and schedule the coroutine as a task;
                # never call asyncio.run() from a request/event-loop thread.
                with asyncio.Runner() as runner:
                    loop = runner.get_loop()
                    task = loop.create_task(
                        _warm_vol_series(
                            r_client, vol_series_list,
                            tenant_id=tenant_id, study_uid=study_uid,
                        )
                    )
                    vol_ok = runner.run(task)
            except Exception as exc:  # noqa: BLE001 - boundary guard: volume pre-warm is a background pass
                log.warning(
                    "Volume pre-warm pass failed (error_type=%s)",
                    type(exc).__name__,
                )

        # 5) Record warm summary entry in Redis under paxs:warm:{study_uid}
        if r_client:
            try:
                warm_record = json.dumps({
                    "study_instance_uid": study_uid,
                    "status": "WARMED",
                    "thumbnails_generated": thumb_ok,
                    "redis_thumbnails": redis_ok,
                    "frames_cached": frames_ok,
                    "volumes_cached": vol_ok,
                    "instances_count": len(instances),
                    "warmed_at": datetime.now(timezone.utc).isoformat(),
                }, indent=2)
                r_client.set(f"paxs:warm:{study_uid}", warm_record, ex=86400)
            except Exception as exc:  # noqa: BLE001 - boundary guard: a warm record is advisory state
                log.debug("warm_record_write_failed error_type=%s", type(exc).__name__)

        if thumb_ok or redis_ok or frames_ok or vol_ok:
            log.info(
                "Warmed study %s: %d thumbnails, %d redis keys, %d frames, %d volumes "
                "(of %d instances)",
                study_uid, thumb_ok, redis_ok, frames_ok, vol_ok, len(instances),
            )
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception as exc:  # noqa: BLE001 - boundary guard: the cache lock may already have expired
                # A Redis lock that expired mid-warm is expected; the study was
                # still warmed, so this must not surface as a failure.
                log.debug("warm_lock_release_failed error_type=%s", type(exc).__name__)
