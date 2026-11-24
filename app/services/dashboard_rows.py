"""Bounded row loading and serialization for the study dashboard."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RadiologyReport, Series, Study, StudyDictation
from app.services import query_service, reading_claim_service, tenant_service


async def _first_series_by_study(
    db: AsyncSession, study_ids: list[int]
) -> dict[int, str]:
    """Load one display series per study with a single bounded query."""
    rows = await db.execute(
        select(Series.study_id, Series.series_instance_uid)
        .where(Series.study_id.in_(study_ids))
        .order_by(Series.id)
    )
    first_by_study: dict[int, str] = {}
    for study_id, series_uid in rows.all():
        first_by_study.setdefault(study_id, series_uid)
    return first_by_study


async def _reports_by_study(
    db: AsyncSession, study_ids: list[int]
) -> dict[int, Any]:
    """Load report badges for the displayed page in one query."""
    rows = await db.execute(
        select(
            RadiologyReport.study_id,
            RadiologyReport.id,
            RadiologyReport.status,
            RadiologyReport.oru_status,
        ).where(RadiologyReport.study_id.in_(study_ids))
    )
    return {row.study_id: row for row in rows.all()}


def _serialize_study_row(
    study: Study,
    aggregates: dict[str, Any],
    tenant_names: dict[int, str],
    first_series_uid: str | None,
    report: Any,
    dictation_count: int | None,
) -> dict[str, Any]:
    """Build the dashboard's stable study-row response shape."""
    return {
        "study_uid": study.study_instance_uid,
        "patient_name": study.patient.patient_name,
        "patient_id": study.patient.patient_id,
        "patient_source": study.patient.source,
        "patient_birth_date": study.patient.patient_birth_date if study.patient else None,
        "patient_sex": study.patient.patient_sex if study.patient else None,
        "tenant_id": study.tenant_id,
        "tenant_name": tenant_names.get(study.tenant_id),
        "study_date": study.study_date,
        "study_time": study.study_time,
        "description": study.study_description,
        "accession": study.accession_number,
        "modalities": aggregates.get("modalities", []),
        "n_series": aggregates.get("series", 0),
        "n_instances": aggregates.get("instances", 0),
        "created_at": study.created_at.isoformat() if study.created_at else None,
        "lifecycle_state": study.lifecycle_state,
        "thumbnail": (
            f"/api/v1/studies/{study.study_instance_uid}/series/{first_series_uid}"
            "/thumbnail.png"
            if first_series_uid is not None else None
        ),
        "dictation_count": dictation_count,
        "report_id": report.id if report else None,
        "report_status": report.status if report else None,
        "oru_status": report.oru_status if report else None,
        # Reading claim (plans/09 C1): the reader holding a live claim, else None.
        "claimed_by": (
            study.claimed_by
            if reading_claim_service.is_active(study.claimed_by, study.claimed_at)
            else None
        ),
    }


async def page_rows(
    db: AsyncSession, studies: list[Study], *, include_dictations: bool = False
) -> list[dict[str, Any]]:
    """Serialize one page with bounded aggregate, tenant, series, and report queries."""
    study_ids = [study.id for study in studies]
    aggregates = await query_service.study_aggregates(db, study_ids)
    tenant_names = await tenant_service.get_tenant_names(db)
    first_series = await _first_series_by_study(db, study_ids)
    reports = await _reports_by_study(db, study_ids)
    counts = {}
    if include_dictations and study_ids:
        rows = await db.execute(
            select(StudyDictation.study_id, func.count(StudyDictation.id))
            .where(StudyDictation.study_id.in_(study_ids))
            .group_by(StudyDictation.study_id)
        )
        counts = dict(rows.all())
    return [
        _serialize_study_row(
            study,
            aggregates.get(study.id, {}),
            tenant_names,
            first_series.get(study.id),
            reports.get(study.id),
            counts.get(study.id, 0) if include_dictations else None,
        )
        for study in studies
    ]
