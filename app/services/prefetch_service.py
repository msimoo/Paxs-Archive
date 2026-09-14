"""Prefetching Engine Service.

Automates background prefetching of historical prior studies from remote archives
when a new Modality Worklist step or ADT trigger arrives, eliminating radiologist wait times.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from pydicom.dataset import Dataset
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db.models import PrefetchJob, PrefetchRule, Study
from app.dicom import sender
from app.services import audit_service

log = logging.getLogger("paxs.prefetch")


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
        log.warning("C-FIND query failed on remote node %s (%s)",
                    ae_title, result.get("reason"))
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


async def evaluate_and_trigger_prefetch(
    session: AsyncSession,
    patient_id: str,
    worklist_step_id: int | None = None,
    modality: str | None = None,
    body_part: str | None = None,
) -> list[PrefetchJob]:
    """Evaluate active prefetch rules and dispatch background C-MOVE requests for prior studies."""
    # 1. Query active rules
    stmt = (
        select(PrefetchRule)
        .options(joinedload(PrefetchRule.source_node))
        .where(PrefetchRule.enabled == True)  # noqa: E712
        .order_by(PrefetchRule.priority.asc(), PrefetchRule.id.asc())
    )
    rules = (await session.execute(stmt)).scalars().unique().all()
    if not rules:
        return []

    # 2. Get local existing StudyInstanceUIDs for patient (avoid re-fetching)
    from app.db.models import Patient
    existing_uids_stmt = select(Study.study_instance_uid).join(Patient).where(Patient.patient_id == patient_id)
    try:
        existing_uids = set((await session.execute(existing_uids_stmt)).scalars().all())
    except Exception:
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

        # Filter out already stored studies
        needed_priors = [p for p in priors if p["study_instance_uid"] not in existing_uids]
        needed_priors = needed_priors[: rule.max_priors_count]

        job = PrefetchJob(
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
                # Trigger C-MOVE to fetch prior study to local PAXS
                cmove_ok = await loop.run_in_executor(
                    None,
                    sender.c_move_study,
                    rule.source_node.host,
                    rule.source_node.port,
                    rule.source_node.ae_title,
                    st_uid,
                    settings.DICOM_AE_TITLE,
                )
                if cmove_ok:
                    retrieved_count += 1
            except Exception as exc:
                log.warning("C-MOVE prefetch failed for study %s from %s: %s", st_uid, rule.source_node.ae_title, exc)

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


async def list_jobs(session: AsyncSession, patient_id: str | None = None, limit: int = 100) -> list[PrefetchJob]:
    stmt = (
        select(PrefetchJob)
        .options(joinedload(PrefetchJob.source_node), joinedload(PrefetchJob.worklist_step))
    )
    if patient_id:
        stmt = stmt.where(PrefetchJob.patient_id == patient_id)
    stmt = stmt.order_by(PrefetchJob.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().unique().all())
