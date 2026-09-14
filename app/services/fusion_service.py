"""Fusion (roadmap #29) — Frame-of-Reference based series alignment.

Series acquired in the same patient coordinate system share a
FrameOfReferenceUID (FoR). This service groups a study's series by FoR,
builds the aligned pair matrix the viewer needs to overlay PET on CT /
MRI on US, and attaches per-series volume geometry (reusing the volume
streaming engine) so the client can spatially register the pair without
re-deriving the transforms.

Alignment contract: two series with the same FoR UID are registered to a
common patient space *by definition* (same coordinate system + same
PixelSpacing / ImagePositionPatient semantics). The identity transform is
correct for same-FoR pairs; the geometry metadata is included so the
viewer can build the patient→voxel matrices per series.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Series
from app.services import query_service, volume_service

log = logging.getLogger("paxs.fusion")


async def study_frame_of_references(
    db: AsyncSession, study_uid: str, *, tenant_id: int | None = None,
    include_all: bool = True,
) -> dict:
    """Fusion manifest for one study: FoR groups + aligned pairs + geometry.

    Returns a dict with ``study_instance_uid``, ``frames_of_reference`` (one
    entry per distinct FoR, each listing its series), and ``pairs`` — every
    same-FoR series combination as ``[series_a, series_b]`` with the common
    FoR UID. Series without a FoR are reported in ``unregistered`` (they
    cannot be fused). Empty ``frames_of_reference`` / ``pairs`` when the study
    has nothing fusable — callers decide whether that's a 404 or an empty
    manifest.
    """
    # Fetch the study (tenant scoped) so a wrong/foreign UID is a clean miss.
    stmt = query_service.study_query(
        {"StudyInstanceUID": study_uid},
        tenant_id=tenant_id, include_all=include_all,
    )
    study = (await db.execute(stmt)).scalars().first()
    if study is None:
        return {"study_instance_uid": study_uid, "not_found": True}

    series_rows = (
        await db.execute(
            select(Series)
            .where(Series.study_id == study.id)
            .order_by(Series.series_number, Series.id)
        )
    ).scalars().all()

    by_for: dict[str, list[Series]] = {}
    unregistered: list[dict] = []
    for s in series_rows:
        entry = {
            "series_instance_uid": s.series_instance_uid,
            "modality": s.modality,
            "series_number": s.series_number,
            "series_description": s.series_description,
            "body_part_examined": s.body_part_examined,
        }
        if not s.frame_of_reference_uid:
            unregistered.append(entry)
            continue
        by_for.setdefault(s.frame_of_reference_uid, []).append(s)

    frames: list[dict] = []
    pairs: list[dict] = []
    for for_uid, group in sorted(by_for.items()):
        group_entries = []
        for s in group:
            entry = {
                "series_instance_uid": s.series_instance_uid,
                "modality": s.modality,
                "series_number": s.series_number,
                "series_description": s.series_description,
                "body_part_examined": s.body_part_examined,
                "geometry": await _series_geometry(db, s),
            }
            group_entries.append(entry)
        frames.append({
            "frame_of_reference_uid": for_uid,
            "series": group_entries,
        })
        # All unordered same-FoR combinations are aligned pairs.
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                pairs.append({
                    "frame_of_reference_uid": for_uid,
                    "series_a": group_entries[i],
                    "series_b": group_entries[j],
                    "transform": {
                        "type": "IDENTITY",
                        "registered": True,
                        "note": "Same Frame of Reference — common patient space",
                    },
                })

    return {
        "study_instance_uid": study_uid,
        "not_found": False,
        "series_count": len(series_rows),
        "frames_of_reference": frames,
        "pairs": pairs,
        "unregistered": unregistered,
    }


async def _series_geometry(db: AsyncSession, series: Series) -> dict | None:
    """Volume geometry for one series (best effort — None when not volumetric)."""
    try:
        info = await volume_service.volume_info(db, series.series_instance_uid)
    except Exception:
        log.debug("geometry unavailable for %s", series.series_instance_uid, exc_info=True)
        info = None
    if info is None:
        return None
    keys = (
        "rows", "columns", "slices", "samples_per_pixel", "dtype",
        "pixel_spacing", "slice_spacing", "image_position_patient",
        "image_orientation_patient", "orientation", "isotropic",
        "rescale_slope", "rescale_intercept", "window_center", "window_width",
        "multi_frame", "source_multi_frame",
    )
    return {k: info.get(k) for k in keys}
