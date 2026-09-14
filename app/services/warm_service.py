"""Warm-on-open: pre-warm a study's read caches the moment a viewer opens it.

When a study is opened in the viewer (QIDO-RS with a specific
``StudyInstanceUID``, or a WADO-RS study-metadata fetch), ``schedule_warm``
kicks a short-lived daemon thread that:

1. pre-generates any missing 192px PNG thumbnails (``.thumbnails/{sop}.png``)
   so study-list / series thumbnail requests hit the pre-generated fast path,
2. warms the Redis thumbnail cache keys the WADO-RS thumbnail endpoint would
   compute, so even the rendered fallback path is instant,
3. pre-decodes the first few frames of the study into the in-process
   decoded-frame cache (``wado_rs_bulk_service._FRAME_CACHE``) so scrolling
   through the top of a series doesn't re-run the expensive J2K/JPEG-LS
   server-side decode.

Everything is best-effort: missing files, decode failures, or an unreachable
Redis are logged and skipped — the warm thread never raises and never delays
the HTTP response that triggered it. Dedup (a Redis lock + in-process set)
prevents concurrent or rapid re-warms of the same study.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import select

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
    """Start a background warm pass for ``study_uid`` (deduped).

    Returns True when a warm pass was scheduled (or is already running for
    this study — which counts as "taken care of"), False when warm-on-open is
    disabled or the study UID is empty. Never blocks.
    """
    if not settings.WARM_ON_OPEN_ENABLED or not study_uid:
        return False

    with _WARMING_LOCK:
        if study_uid in _WARMING:
            return True  # already being warmed by a prior trigger
        _WARMING.add(study_uid)

    thread = threading.Thread(
        target=_warm_worker,
        args=(study_uid,),
        name=f"paxs-warm-{study_uid[:12]}",
        daemon=True,
    )
    thread.start()
    return True


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
def _warm_worker(study_uid: str) -> None:
    try:
        _warm_study(study_uid)
    except Exception:
        log.exception("Warm pass failed for study %s", study_uid)
    finally:
        with _WARMING_LOCK:
            _WARMING.discard(study_uid)


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
        except Exception as exc:
            log.debug("Warm lock skipped for %s: %s", study_uid, exc)
            lock = None

    try:
        with SyncSessionLocal() as session:
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

        thumb_ok = 0
        redis_ok = 0
        frames_ok = 0
        for inst in instances:
            bk = getattr(inst, "storage_backend", None) or None
            if not storage_service.exists(inst.file_path, backend_name=bk):
                continue

            # 1) Missing pre-generated thumbnail (the WADO thumbnail fast path).
            thumb_key = storage_service.thumbnail_key(inst.sop_instance_uid)
            if settings.THUMBNAIL_ENABLED and not storage_service.exists(
                thumb_key, backend_name=bk
            ):
                if thumbnail_service.generate_thumbnail(
                    inst.file_path, thumb_key, backend_name=bk
                ):
                    thumb_ok += 1

            # 2) Redis thumbnail cache key (the rendered fallback path's key,
            #    default 256px PNG — matches wado_thumbnail's cache_key).
            from app.services.redis_service import cache_get, cache_set

            cache_key = f"paxs:thumb:{inst.sop_instance_uid}:auto:256:png"
            if cache_get(cache_key) is None:
                try:
                    ds = storage_service.read_dataset(
                        inst.file_path, backend_name=bk
                    )
                    from app.services.imaging import dataset_to_image

                    import io

                    img = dataset_to_image(ds, max_size=256)
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    if cache_set(cache_key, buf.getvalue(), expire_seconds=3600):
                        redis_ok += 1
                except Exception:
                    log.debug("Redis thumb warm failed for %s", inst.sop_instance_uid)

            # 3) Pre-decode the first frame into the in-process frame cache & Redis
            #    (bounded by WARM_MAX_FRAMES across the study).
            if frames_ok < settings.WARM_MAX_FRAMES:
                try:
                    from app.services import wado_rs_bulk_service

                    ds = storage_service.read_dataset(
                        inst.file_path, backend_name=bk
                    )
                    decoded = wado_rs_bulk_service.decode_frames_to_raw(ds, [1])
                    if decoded:
                        frames_ok += 1
                        cache_set(f"paxs:frame:{inst.sop_instance_uid}:1", decoded[0][0], expire_seconds=3600)
                except Exception:
                    log.debug("Frame warm failed for %s", inst.sop_instance_uid)

        # 4) Pre-warm 3D volume cache in Redis for volumetric series (CT/MR)
        vol_ok = 0
        if getattr(settings, "REDIS_CACHE_ENABLED", False):
            try:
                if r_client:
                    vol_series_list = (
                        session.execute(
                            select(Series.series_instance_uid)
                            .join(Study, Series.study_id == Study.id)
                            .where(Study.study_instance_uid == study_uid)
                            .where(Series.modality.in_(["CT", "MR", "PT"]))
                        )
                        .scalars()
                        .all()
                    )
                    if vol_series_list:
                        async def _warm_vol_series():
                            warmed_count = 0
                            from app.db.session import AsyncSessionLocal
                            from app.services.volume_service import load_volume_array, series_slices

                            async with AsyncSessionLocal() as a_session:
                                for s_uid in vol_series_list:
                                    canonical_pattern = f"paxs:vol:*{s_uid}*"
                                    cursor, matched = r_client.scan(cursor=0, match=canonical_pattern, count=1)
                                    if not matched:
                                        try:
                                            slices = await series_slices(a_session, s_uid)
                                            if slices and len(slices) >= 4:
                                                load_volume_array(slices, series_uid=s_uid)
                                                warmed_count += 1
                                        except Exception as v_exc:
                                            log.debug("Volume pre-warm failed for %s: %s", s_uid, v_exc)
                            return warmed_count

                        vol_ok = asyncio.run(_warm_vol_series())
            except Exception as exc:
                log.debug("Volume pre-warm pass failed: %s", exc)

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
            except Exception as exc:
                log.debug("Warm record write failed: %s", exc)

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
            except Exception:
                pass
