"""Prefetching Engine Service.

Automates background prefetching of historical prior studies from remote archives
when a new Modality Worklist step or ADT trigger arrives, eliminating radiologist wait times.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from functools import partial
from typing import Any

from pydicom.dataset import Dataset
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db.models import Patient, PrefetchJob, PrefetchRule, ScheduledProcedureStep, Study
from app.dicom import sender
from app.services import audit_service, query_service

log = logging.getLogger("paxs.prefetch")


async def _trigger_target_is_in_scope(
    session: AsyncSession,
    patient_id: str,
    worklist_step_id: int | None,
    tenant_id: int | None,
    include_all: bool,
) -> bool:
    """Validate the P0.1 prefetch target before any remote C-FIND is sent."""
    patient_stmt = select(Patient.id).where(Patient.patient_id == patient_id)
    patient_stmt = query_service.apply_tenant_scope(
        patient_stmt, Patient.tenant_id, tenant_id, include_all
    )
    patient_exists = (await session.execute(patient_stmt)).scalars().first()
    if patient_exists is None:
        return False

    if worklist_step_id is None:
        return True

    step_stmt = (
        select(ScheduledProcedureStep.id)
        .join(Patient, ScheduledProcedureStep.patient_id == Patient.id)
        .where(
            ScheduledProcedureStep.id == worklist_step_id,
            Patient.patient_id == patient_id,
        )
    )
    step_stmt = query_service.apply_tenant_scope(
        step_stmt, Patient.tenant_id, tenant_id, include_all
    )
    return (await session.execute(step_stmt)).scalars().first() is not None


def query_remote_priors_sync(
    host: str,
    port: int,
    ae_title: str,
    patient_id: str,
    modality: str | None = None,
    our_ae_title: str = "PAXS_SCU",
) -> list[dict[str, Any]]:
    """Query remote DICOM archive node via C-FIND for prior studies matching PatientID."""
    from pynetdicom.sop_class import PatientRootQueryRetrieveInformationModelFind

    query_ds = Dataset()
    query_ds.QueryRetrieveLevel = "STUDY"
    query_ds.PatientID = patient_id
    query_ds.StudyInstanceUID = ""
    query_ds.StudyDate = ""
    query_ds.StudyTime = ""
    query_ds.ModalitiesInStudy = modality or ""
    query_ds.StudyDescription = ""

    result = sender.send_find(
        host, port, ae_title, query_ds,
        query_model=PatientRootQueryRetrieveInformationModelFind,
        our_ae_title=our_ae_title,
    )
    if not result.get("ok"):
        log.warning(
            "prefetch_remote_c_find_failed source_ae=%s reason_type=%s",
            ae_title,
            type(result.get("reason")).__name__,
        )
        return []

    priors = []
    for identifier in result["matches"]:
        st_uid = getattr(identifier, "StudyInstanceUID", None)
        if st_uid:
            priors.append(
                {
                    "study_instance_uid": str(st_uid),
                    "study_date": str(getattr(identifier, "StudyDate", "") or ""),
                    "modalities": str(getattr(identifier, "ModalitiesInStudy", "") or ""),
                }
            )
    return priors


def _study_move_query(study_instance_uid: str) -> Dataset:
    """A STUDY-level C-MOVE query identifying exactly one prior study."""
    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    query.StudyInstanceUID = study_instance_uid
    return query


def _within_max_age(study_date: str | None, max_age_days: int | None,
                    now: datetime) -> bool:
    """True when a prior's study date is within ``max_age_days`` of ``now`` (L-13).

    A non-positive or missing limit means "no age restriction". An absent or
    unparseable date is kept rather than dropped — the remote C-FIND already
    matched it, and silently discarding priors is worse than fetching a slightly
    older one.
    """
    if not max_age_days or int(max_age_days) <= 0:
        return True
    raw = str(study_date or "").strip()
    # DICOM DA may be a single date or a range "YYYYMMDD-YYYYMMDD"; use the start.
    first = raw.split("-", 1)[0]
    if len(first) != 8 or not first.isdigit():
        return True
    try:
        parsed = datetime.strptime(first, "%Y%m%d")
    except ValueError:
        return True
    return (now - parsed).days <= int(max_age_days)


async def evaluate_and_trigger_prefetch(
    session: AsyncSession,
    patient_id: str,
    worklist_step_id: int | None = None,
    modality: str | None = None,
    body_part: str | None = None,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[PrefetchJob]:
    """Evaluate active prefetch rules and dispatch background C-MOVE requests for prior studies."""
    if not await _trigger_target_is_in_scope(
        session, patient_id, worklist_step_id, tenant_id, include_all
    ):
        return []

    # 1. Query active rules
    stmt = (
        select(PrefetchRule)
        .options(joinedload(PrefetchRule.source_node))
        .where(PrefetchRule.enabled.is_(True))
        .order_by(PrefetchRule.priority.asc(), PrefetchRule.id.asc())
    )
    rules = (await session.execute(stmt)).scalars().unique().all()
    if not rules:
        return []

    # 2. Get local existing StudyInstanceUIDs for patient (avoid re-fetching)
    existing_uids_stmt = (
        select(Study.study_instance_uid)
        .join(Patient, Study.patient_id == Patient.id)
        .where(Patient.patient_id == patient_id)
    )
    existing_uids_stmt = query_service.apply_study_tenant_scope(
        existing_uids_stmt, tenant_id, include_all
    )
    try:
        existing_uids = set((await session.execute(existing_uids_stmt)).scalars().all())
    except Exception as exc:  # noqa: BLE001 - boundary guard: an empty set only means "re-fetch everything"
        # Prefetching is an optimisation: on a lookup failure we would rather
        # issue a redundant C-MOVE than skip a legitimate one.
        log.debug("prefetch_existing_lookup_failed error_type=%s", type(exc).__name__)
        existing_uids = set()

    jobs = []
    loop = asyncio.get_running_loop()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for rule in rules:
        if not rule.source_node or not rule.source_node.enabled:
            continue

        if rule.modality and modality:
            if rule.modality.upper() != "ALL" and rule.modality.upper() != modality.upper():
                continue

        if rule.body_part_examined and body_part:
            if rule.body_part_examined.upper() != body_part.upper():
                continue

        # C-FIND remote priors
        priors = await loop.run_in_executor(
            None,
            query_remote_priors_sync,
            rule.source_node.host,
            rule.source_node.port,
            rule.source_node.ae_title,
            patient_id,
            rule.modality if rule.modality != "ALL" else None,
            settings.DICOM_AE_TITLE,
        )

        # Filter out already stored studies, then honour the rule's age window
        # (L-13) before capping the count.
        needed_priors = [p for p in priors if p["study_instance_uid"] not in existing_uids]
        needed_priors = [
            p for p in needed_priors
            if _within_max_age(p.get("study_date"), rule.max_prior_age_days, now)
        ]
        needed_priors = needed_priors[: rule.max_priors_count]

        job = PrefetchJob(
            tenant_id=tenant_id,
            is_archive_wide=include_all,
            patient_id=patient_id,
            worklist_step_id=worklist_step_id,
            source_node_id=rule.source_node.id,
            status="IN_PROGRESS",
            priors_found=len(priors),
            priors_retrieved=0,
            created_at=now,
            updated_at=now,
        )
        job.source_node = rule.source_node
        session.add(job)
        await session.flush()

        retrieved_count = 0
        for prior_info in needed_priors:
            st_uid = prior_info["study_instance_uid"]
            try:
                # Trigger C-MOVE to fetch the prior study to local PAXS. The
                # destination AE is this archive: the remote SCP pushes the
                # instances back to us. (H-1: this previously called the
                # non-existent `sender.c_move_study`, so every prefetch raised
                # AttributeError and was swallowed — priors were never fetched.)
                result = await loop.run_in_executor(
                    None,
                    partial(
                        sender.send_move,
                        rule.source_node.host,
                        rule.source_node.port,
                        rule.source_node.ae_title,
                        settings.DICOM_AE_TITLE,          # dest_ae_title (ourselves)
                        query=_study_move_query(st_uid),
                        our_ae_title=settings.DICOM_AE_TITLE,
                        # B-16: verify the peer like every other SCU call.
                        trust_cert=getattr(rule.source_node, "trust_cert", None),
                    ),
                )
                if isinstance(result, dict) and result.get("ok"):
                    retrieved_count += 1
                else:
                    reason = result.get("reason") if isinstance(result, dict) else result
                    log.warning(
                        "C-MOVE prefetch did not succeed for study %s from %s "
                        "(reason_type=%s)",
                        st_uid, rule.source_node.ae_title, type(reason).__name__,
                    )
            except Exception as exc:
                log.warning(
                    "C-MOVE prefetch failed for study %s from %s (error_type=%s)",
                    st_uid, rule.source_node.ae_title, type(exc).__name__,
                )

        job.priors_retrieved = retrieved_count
        job.status = "COMPLETED" if retrieved_count > 0 or len(needed_priors) == 0 else "FAILED"
        job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

        audit_service.add_audit(
            session,
            None,
            "PREFETCH_JOB_COMPLETED",
            performed_by=f"prefetch:{rule.name}",
        )
        jobs.append(job)

    await session.flush()
    return jobs


# CRUD Helpers
async def create_rule(session: AsyncSession, data: dict[str, Any]) -> PrefetchRule:
    rule = PrefetchRule(**data)
    session.add(rule)
    await session.flush()
    return rule


async def get_rule(session: AsyncSession, rule_id: int) -> PrefetchRule | None:
    return (
        await session.execute(
            select(PrefetchRule)
            .options(joinedload(PrefetchRule.source_node))
            .where(PrefetchRule.id == rule_id)
        )
    ).scalar_one_or_none()


async def list_rules(session: AsyncSession) -> list[PrefetchRule]:
    return list(
        (
            await session.execute(
                select(PrefetchRule)
                .options(joinedload(PrefetchRule.source_node))
                .order_by(PrefetchRule.priority.asc(), PrefetchRule.id.asc())
            )
        ).scalars().unique().all()
    )


async def delete_rule(session: AsyncSession, rule_id: int) -> bool:
    rule = await get_rule(session, rule_id)
    if not rule:
        return False
    await session.delete(rule)
    await session.flush()
    return True


async def list_jobs(
    session: AsyncSession,
    patient_id: str | None = None,
    limit: int = 100,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[PrefetchJob]:
    stmt = (
        select(PrefetchJob)
        .options(joinedload(PrefetchJob.source_node), joinedload(PrefetchJob.worklist_step))
    )
    if patient_id:
        stmt = stmt.where(PrefetchJob.patient_id == patient_id)
    stmt = query_service.apply_tenant_scope(
        stmt, PrefetchJob.tenant_id, tenant_id, include_all
    )
    if not include_all:
        stmt = stmt.where(PrefetchJob.is_archive_wide.is_(False))
    stmt = stmt.order_by(PrefetchJob.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().unique().all())
