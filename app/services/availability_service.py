"""Read-only sampled storage availability for the viewer study browser."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.concurrency import run_blocking
from app.services import query_service, storage_service


class StudyAvailabilityNotFound(LookupError):
    """Raised when the study is absent from the caller's tenant scope."""


def _sample_indices(count: int, sample: int) -> list[int]:
    """Return bounded representative indices; three means first/middle/last."""
    if count <= 0 or sample <= 0:
        return []
    candidates = [0, count // 2, count - 1]
    if sample > 3:
        candidates.extend(round(index * (count - 1) / (sample - 1)) for index in range(sample))
    return list(dict.fromkeys(candidates))[: min(sample, count)]


def _status(checked: int, missing: int) -> str:
    if checked == 0:
        return "unknown"
    if missing == 0:
        return "online"
    if missing == checked:
        return "missing"
    return "partial"


async def study_availability(
    db: AsyncSession,
    study_uid: str,
    *,
    tenant_id: int | None,
    include_all: bool,
    sample: int,
    max_checks: int,
) -> dict[str, Any]:
    """Sample stored objects for one tenant-scoped study without changing metadata."""
    stmt = query_service.study_query(
        {"StudyInstanceUID": study_uid}, tenant_id=tenant_id, include_all=include_all
    )
    if (await db.execute(stmt)).scalars().first() is None:
        raise StudyAvailabilityNotFound(study_uid)
    instances = await query_service.instances_of(
        db, study_uid, tenant_id=tenant_id, include_all=include_all
    )
    identities = await query_service.series_identities(db, {item.series_id for item in instances})
    grouped: dict[int, list[Any]] = defaultdict(list)
    for instance in instances:
        grouped[instance.series_id].append(instance)

    checked_total = 0
    series: list[dict[str, Any]] = []
    for series_id in sorted(grouped, key=lambda value: identities.get(value, ("", "", None))[1]):
        rows = sorted(grouped[series_id], key=lambda row: (
            row.instance_number is None, row.instance_number or 0, row.sop_instance_uid
        ))
        remaining = max(0, max_checks - checked_total)
        selected = _sample_indices(len(rows), min(sample, remaining))
        missing = 0
        for index in selected:
            instance = rows[index]
            present = await run_blocking(
                storage_service.exists, instance.file_path, backend_name=instance.storage_backend
            )
            missing += int(not present)
        checked = len(selected)
        checked_total += checked
        series.append({
            "series_instance_uid": identities[series_id][1], "instances": len(rows),
            "checked": checked, "missing": missing, "status": _status(checked, missing),
        })
    return {"study_instance_uid": study_uid, "sampled": True, "series": series}
