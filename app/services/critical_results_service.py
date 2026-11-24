"""Critical Results Tracking Service.

Manages recording, notifying, and formal acknowledgement of critical or urgent
radiology findings (e.g. pneumothorax, ICH, PE) for patient safety & legal compliance.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import CriticalResult, Study
from app.services import audit_service, query_service, webhook_service

log = logging.getLogger("paxs.critical_results")


def _audit_acknowledgement(
    session: AsyncSession, result: CriticalResult, acknowledged_by: str
) -> None:
    """Persist the acknowledgement audit event using the established audit service."""
    audit_service.add_audit(
        session,
        sop_instance_uid=result.sop_instance_uid,
        study_instance_uid=result.study_instance_uid,
        action="CRITICAL_RESULT_ACKNOWLEDGED",
        performed_by=acknowledged_by,
    )


async def create_critical_result(
    session: AsyncSession,
    study_instance_uid: str,
    description: str,
    reported_by: str,
    finding_level: str = "CRITICAL_RED",
    series_instance_uid: str | None = None,
    sop_instance_uid: str | None = None,
    patient_id: str | None = None,
    communicated_to: str | None = None,
    communication_channel: str | None = None,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> CriticalResult:
    """Create and record a critical finding for a study.

    B-9: the study lookup that backfills ``patient_id`` is tenant-scoped, so a
    caller cannot attach another institution's patient identifier to a finding
    they are not allowed to see. The route proves study access first; this is
    defence in depth for any other caller.
    """
    # Look up study to backfill patient_id if omitted
    if not patient_id:
        stmt = query_service.apply_study_tenant_scope(
            select(Study)
            .options(joinedload(Study.patient))
            .where(Study.study_instance_uid == study_instance_uid),
            tenant_id,
            include_all,
        )
        study = (await session.execute(stmt)).scalar_one_or_none()
        if study and study.patient:
            patient_id = study.patient.patient_id

    result = CriticalResult(
        study_instance_uid=study_instance_uid,
        series_instance_uid=series_instance_uid,
        sop_instance_uid=sop_instance_uid,
        patient_id=patient_id,
        finding_level=finding_level.upper(),
        description=description,
        reported_by=reported_by,
        communicated_to=communicated_to,
        communication_channel=communication_channel,
        status="PENDING_ACK",
    )
    session.add(result)
    await session.flush()

    # Log audit event
    audit_service.add_audit(
        session,
        sop_instance_uid=sop_instance_uid,
        study_instance_uid=study_instance_uid,
        action="CRITICAL_RESULT_FLAGGED",
        performed_by=reported_by,
    )

    # Fire webhook notification + custom server scripts
    webhook_service.fire(
        "CriticalResultFlagged",
        study_instance_uid=study_instance_uid,
        patient_id=patient_id,
        finding_level=finding_level,
        description=description,
        reported_by=reported_by,
    )
    from app.services.scripting_service import fire as script_fire

    script_fire(
        "CriticalResultFlagged",
        study_instance_uid=study_instance_uid,
        patient_id=patient_id,
        finding_level=finding_level,
        description=description,
        reported_by=reported_by,
    )

    log.info(
        "Critical result #%d (%s) created for study %s by %s",
        result.id,
        finding_level,
        study_instance_uid,
        reported_by,
    )
    return result


async def acknowledge_critical_result(
    session: AsyncSession,
    result_id: int,
    acknowledged_by: str,
    notes: str | None = None,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> CriticalResult:
    """Formally acknowledge a critical finding.

    B-9: the row is looked up **within the caller's tenant scope**, so a foreign
    finding is indistinguishable from a missing one (``ValueError`` → HTTP 404
    at the route) rather than acknowledgeable across institutions.
    """
    stmt = query_service.apply_study_uid_tenant_scope(
        select(CriticalResult).where(CriticalResult.id == result_id),
        CriticalResult,
        CriticalResult.study_instance_uid,
        tenant_id,
        include_all,
    )
    result = (await session.execute(stmt)).scalar_one_or_none()
    if not result:
        raise ValueError(f"Critical result #{result_id} not found")

    if result.status == "ACKNOWLEDGED":
        return result  # Idempotent

    result.status = "ACKNOWLEDGED"
    result.acknowledged_by = acknowledged_by
    result.acknowledged_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if notes:
        result.acknowledgement_notes = notes

    await session.flush()

    # Audit log
    _audit_acknowledgement(session, result, acknowledged_by)
    log.info("Critical result #%d acknowledged by %s", result_id, acknowledged_by)
    return result


async def get_critical_result(
    session: AsyncSession,
    result_id: int,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> CriticalResult | None:
    """Get critical result by ID, within the caller's tenant scope (B-9)."""
    stmt = query_service.apply_study_uid_tenant_scope(
        select(CriticalResult).where(CriticalResult.id == result_id),
        CriticalResult,
        CriticalResult.study_instance_uid,
        tenant_id,
        include_all,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_critical_results(
    session: AsyncSession,
    study_instance_uid: str | None = None,
    patient_id: str | None = None,
    status: str | None = None,
    finding_level: str | None = None,
    limit: int = 100,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[CriticalResult]:
    """Query critical findings with filters, scoped to the caller's tenant (B-9).

    ``CriticalResult`` carries no tenant column, so the predicate is applied to
    the owning study through :func:`query_service.apply_study_uid_tenant_scope`
    (outer join → a finding whose study is not indexed falls into the legacy
    bucket instead of vanishing for a pre-VNA caller).
    """
    stmt = select(CriticalResult)
    if study_instance_uid:
        stmt = stmt.where(CriticalResult.study_instance_uid == study_instance_uid)
    if patient_id:
        stmt = stmt.where(CriticalResult.patient_id == patient_id)
    if status:
        stmt = stmt.where(CriticalResult.status == status.upper())
    if finding_level:
        stmt = stmt.where(CriticalResult.finding_level == finding_level.upper())

    stmt = query_service.apply_study_uid_tenant_scope(
        stmt, CriticalResult, CriticalResult.study_instance_uid, tenant_id, include_all
    )
    stmt = stmt.order_by(CriticalResult.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())
