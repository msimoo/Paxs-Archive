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
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Series
from app.services import query_service, volume_service

log = logging.getLogger("paxs.fusion")


async def study_frame_of_references(
    db: AsyncSession, study_uid: str, *, tenant_id: int | None = None,
    include_all: bool = False,
) -> dict:
    """Fusion manifest for one study: FoR groups + aligned pairs + geometry.

    Returns a dict with ``study_instance_uid``, ``frames_of_reference`` (one
    entry per distinct FoR, each listing its series), and ``pairs`` — every
    same-FoR series combination as ``[series_a, series_b]`` with the common
    FoR UID. Series without a FoR are reported in ``unregistered`` (they
    cannot be fused). Empty ``frames_of_reference`` / ``pairs`` when the study
    has nothing fusable — callers decide whether that's a 404 or an empty
    manifest.

    ``include_all`` defaults to ``False`` (N-1/N-8, fail closed): the caller
    passes ``tenant_service.scope_from_user`` through verbatim, and a caller
    that forgets the flag gets the scoped read rather than every tenant's
    studies. The manifest also carries patients' series descriptions and body
    parts, so a fail-open default here leaks PHI, not just an identifier.
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

    by_for, unregistered = _group_by_frame_of_reference(series_rows)
    frames, pairs = await _frames_and_pairs(db, by_for)
    return {
        "study_instance_uid": study_uid,
        "not_found": False,
        "series_count": len(series_rows),
        "frames_of_reference": frames,
        "pairs": pairs,
        "unregistered": unregistered,
    }


def _series_entry(series: Series) -> dict:
    """The identity fields every manifest entry carries (no geometry yet)."""
    return {
        "series_instance_uid": series.series_instance_uid,
        "modality": series.modality,
        "series_number": series.series_number,
        "series_description": series.series_description,
        "body_part_examined": series.body_part_examined,
    }


def _group_by_frame_of_reference(
    rows: Sequence[Series],
) -> tuple[dict[str, list[Series]], list[dict]]:
    """Partition a study's series into FoR groups and unregistered entries.

    A series without a FoR cannot be registered to anything, so it is reported
    rather than guessed into a group: inventing a shared coordinate system is
    how an overlay lands a PET series on the wrong patient space.
    """
    by_for: dict[str, list[Series]] = {}
    unregistered: list[dict] = []
    for series in rows:
        if not series.frame_of_reference_uid:
            unregistered.append(_series_entry(series))
            continue
        by_for.setdefault(series.frame_of_reference_uid, []).append(series)
    return by_for, unregistered


async def _frames_and_pairs(
    db: AsyncSession, by_for: dict[str, list[Series]],
) -> tuple[list[dict], list[dict]]:
    """One frame entry per FoR (with geometry) plus every same-FoR pair."""
    frames: list[dict] = []
    pairs: list[dict] = []
    for for_uid, group in sorted(by_for.items()):
        entries: list[dict] = []
        for series in group:
            entry = _series_entry(series)
            entry["geometry"] = await _series_geometry(db, series)
            entries.append(entry)
        frames.append({"frame_of_reference_uid": for_uid, "series": entries})
        pairs.extend(_aligned_pairs(for_uid, entries))
    return frames, pairs


def _aligned_pairs(for_uid: str, entries: Sequence[dict]) -> list[dict]:
    """Every unordered pair of series that share one patient space."""
    pairs: list[dict] = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            pairs.append({
                "frame_of_reference_uid": for_uid,
                "series_a": entries[i],
                "series_b": entries[j],
                "transform": {
                    "type": "IDENTITY",
                    "registered": True,
                    "note": "Same Frame of Reference — common patient space",
                },
            })
    return pairs


async def _series_geometry(db: AsyncSession, series: Series) -> dict | None:
    """Volume geometry for one series (best effort — None when not volumetric)."""
    try:
        info = await volume_service.volume_info(db, series.series_instance_uid)
    except Exception as exc:
        # Geometry is optional for this manifest; keep the response usable without logging details.
        log.debug(
            "fusion_geometry_unavailable series_uid=%s error_type=%s",
            series.series_instance_uid,
            type(exc).__name__,
        )
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
