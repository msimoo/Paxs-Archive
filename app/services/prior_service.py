"""Prior Study Linking Service.

Automatically locates, scores, and links relevant historical prior imaging studies
for a patient based on Patient ID, Modality, Body Part, and Study Date.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import Patient, Series, Study

log = logging.getLogger("paxs.priors")


@dataclass
class PriorStudyMatch:
    study_instance_uid: str
    patient_id: str
    study_date: str | None
    study_time: str | None
    study_description: str | None
    modalities: list[str]
    body_parts: list[str]
    relevance_score: int          # 0 - 100
    is_exact_modality_match: bool
    is_exact_body_part_match: bool


async def find_prior_studies(
    session: AsyncSession,
    study_instance_uid: str,
    patient_id: str | None = None,
) -> list[PriorStudyMatch]:
    """Find all prior studies for a patient given a current study_instance_uid."""
    # 1. Fetch current study details
    stmt_curr = (
        select(Study)
        .options(joinedload(Study.patient), joinedload(Study.series_list))
        .where(Study.study_instance_uid == study_instance_uid)
    )
    curr_study = (await session.execute(stmt_curr)).scalars().first()

    target_patient_id = patient_id
    curr_modalities = set()
    curr_body_parts = set()
    curr_date_str = None

    if curr_study:
        if not target_patient_id and curr_study.patient:
            target_patient_id = curr_study.patient.patient_id
        curr_date_str = curr_study.study_date
        for s in curr_study.series_list:
            if s.modality:
                curr_modalities.add(s.modality.upper())
            if s.body_part_examined:
                curr_body_parts.add(s.body_part_examined.upper())

    if not target_patient_id:
        return []

    # 2. Query all other studies for the same patient
    stmt_priors = (
        select(Study)
        .join(Patient)
        .options(joinedload(Study.patient), joinedload(Study.series_list))
        .where(
            Patient.patient_id == target_patient_id,
            Study.study_instance_uid != study_instance_uid,
        )
        # Portable NULLS-LAST: `is_not(None).desc()` works on MySQL 8, MariaDB
        # (10.4+ — no NULLS LAST support before 10.8) and SQLite alike.
        .order_by(Study.study_date.is_not(None).desc(), Study.study_date.desc(),
                  Study.created_at.desc())
    )
    priors = (await session.execute(stmt_priors)).scalars().unique().all()

    results = []
    for prior in priors:
        prior_mods = set()
        prior_bps = set()
        for s in prior.series_list:
            if s.modality:
                prior_mods.add(s.modality.upper())
            if s.body_part_examined:
                prior_bps.add(s.body_part_examined.upper())

        # Compute relevance score
        score = 50  # Base score for same patient
        exact_mod = bool(curr_modalities & prior_mods)
        exact_bp = bool(curr_body_parts & prior_bps)

        if exact_mod:
            score += 25
        if exact_bp:
            score += 25

        # Newer priors score higher
        results.append(
            PriorStudyMatch(
                study_instance_uid=prior.study_instance_uid,
                patient_id=target_patient_id,
                study_date=prior.study_date,
                study_time=prior.study_time,
                study_description=prior.study_description,
                modalities=list(prior_mods),
                body_parts=list(prior_bps),
                relevance_score=score,
                is_exact_modality_match=exact_mod,
                is_exact_body_part_match=exact_bp,
            )
        )

    # Sort by relevance score desc, then study date desc
    results.sort(key=lambda x: (x.relevance_score, x.study_date or ""), reverse=True)
    return results
