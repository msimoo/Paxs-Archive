"""Study Lifecycle Management service (item 14 — auto-archive, restore, purge).

Tiers studies between hot and cold storage based on rules, mirroring the
lossy-compression rule engine: a rule matches by minimum study age (+ optional
modality / body part) and applies one lifecycle action:

  * ARCHIVE — move every instance object under the cold ``.archive/`` prefix
    (same backend — S3 copy+delete, local rename) and mark the study ARCHIVED.
  * RESTORE — bring an ARCHIVED study back to the hot tier (reverse move).
  * PURGE   — delete the study's objects and metadata (retention expiry).
    Purge is destructive and gated behind `require_destructive_role` at the
    API layer; it is only ever applied automatically to studies whose rule
    matched — operators own the decision to enable such a rule.

The worker sweeps enabled rules on a schedule; the REST API exposes the same
engine for manual runs and per-study actions. File moves happen on the backend
via `StorageBackend.move` (no bytes through the app process).

All database work is *sync* (its own SyncSessionLocal) because it is shared
between the daemon worker thread and the async REST layer — the API wraps
these calls in ``asyncio.to_thread`` like the DIMSE SCU helpers do.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import Study, StudyLifecycleRule
from app.db.session import SyncSessionLocal
from app.services import audit_service, storage_service

log = logging.getLogger("paxs.lifecycle")

# Cold-tier object prefix. Keep it short; thumbnails live under `.thumbnails/`.
ARCHIVE_PREFIX = ".archive/"

VALID_ACTIONS = ("ARCHIVE", "RESTORE", "PURGE")


# --------------------------------------------------------------------------- #
# Rule CRUD (async — used directly by the REST API)
# --------------------------------------------------------------------------- #
async def create_rule(session, data: dict) -> StudyLifecycleRule:
    action = (data.get("action") or "ARCHIVE").upper()
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {VALID_ACTIONS}")
    rule = StudyLifecycleRule(**{**data, "action": action})
    session.add(rule)
    await session.flush()
    return rule


def create_rule_sync(session, data: dict) -> StudyLifecycleRule:
    """Sync create helper (worker / tests share the sync engine)."""
    action = (data.get("action") or "ARCHIVE").upper()
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {VALID_ACTIONS}")
    rule = StudyLifecycleRule(**{**data, "action": action})
    session.add(rule)
    session.flush()
    return rule


async def get_rule(session, rule_id: int) -> StudyLifecycleRule | None:
    return (
        await session.execute(select(StudyLifecycleRule).where(StudyLifecycleRule.id == rule_id))
    ).scalar_one_or_none()


async def list_rules(session) -> list[StudyLifecycleRule]:
    return list(
        (
            await session.execute(
                select(StudyLifecycleRule).order_by(
                    StudyLifecycleRule.priority.asc(), StudyLifecycleRule.id.asc()
                )
            )
        ).scalars().all()
    )


async def delete_rule(session, rule_id: int) -> bool:
    rule = await get_rule(session, rule_id)
    if not rule:
        return False
    await session.delete(rule)
    await session.flush()
    return True


# --------------------------------------------------------------------------- #
# Study tiering (sync core)
# --------------------------------------------------------------------------- #
def _study_age_days(study: Study) -> int:
    """Age in days from the study date, falling back to created_at."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if study.study_date and len(study.study_date) == 8 and study.study_date.isdigit():
        try:
            return (now - datetime.strptime(study.study_date, "%Y%m%d")).days
        except ValueError:
            pass
    return (now - study.created_at).days if study.created_at else 0


def _tier_study(study: Study, to_archive: bool, session, performed_by: str) -> int:
    """Move a study's instance objects hot <-> cold. Returns object count."""
    prefix = ARCHIVE_PREFIX
    moved = 0
    for series in study.series_list:
        for inst in series.instances:
            key = inst.file_path
            if not key:
                continue
            if to_archive:
                if key.startswith(prefix):
                    continue
                dest = f"{prefix}{key}"
            else:
                if not key.startswith(prefix):
                    continue
                dest = key[len(prefix):]
            if storage_service.move_object(key, dest, backend_name=inst.storage_backend):
                inst.file_path = dest
                moved += 1
    if moved:
        if to_archive:
            study.lifecycle_state = "ARCHIVED"
            study.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            study.lifecycle_state = "ONLINE"
            study.archived_at = None
        audit_service.add_audit(
            session,
            sop_instance_uid=None,
            action="ARCHIVED" if to_archive else "RESTORED",
            performed_by=performed_by,
            study_instance_uid=study.study_instance_uid,
        )
        log.info("%s study %s (%d objects)",
                 "Archived" if to_archive else "Restored",
                 study.study_instance_uid, moved)
    return moved


def _purge_study(study: Study, session, performed_by: str) -> int:
    """Delete a study's objects (hot + cold tiers) and metadata. Destructive."""
    deleted = 0
    for series in study.series_list:
        for inst in series.instances:
            key = inst.file_path
            if key:
                if storage_service.delete_file(key, backend_name=inst.storage_backend):
                    deleted += 1
                storage_service.delete_file(
                    storage_service.thumbnail_key(inst.sop_instance_uid),
                    backend_name=inst.storage_backend,
                )
    audit_service.add_audit(
        session,
        sop_instance_uid=None,
        action="PURGED",
        performed_by=performed_by,
        study_instance_uid=study.study_instance_uid,
    )
    log.warning("Purged study %s (%d objects)", study.study_instance_uid, deleted)
    return deleted


def _series_modalities(study: Study) -> set[str]:
    return {s.modality.upper() for s in study.series_list if s.modality}


def _series_body_parts(study: Study) -> set[str]:
    return {s.body_part_examined.upper() for s in study.series_list if s.body_part_examined}


def _rule_matches(rule: StudyLifecycleRule, study: Study) -> bool:
    if _study_age_days(study) < rule.min_age_days:
        return False
    if rule.modality and rule.modality.upper() not in _series_modalities(study):
        return False
    if rule.body_part_examined and rule.body_part_examined.upper() not in _series_body_parts(study):
        return False
    return True


# --------------------------------------------------------------------------- #
# Public entry points (sync; safe from worker thread or asyncio.to_thread)
# --------------------------------------------------------------------------- #
def archive_study(study_uid: str, performed_by: str = "lifecycle") -> dict:
    with SyncSessionLocal() as session:
        study = (session.query(Study).filter(Study.study_instance_uid == study_uid)
                 .one_or_none())
        if study is None:
            return {"ok": False, "error": "Study not found"}
        if study.lifecycle_state == "ARCHIVED":
            return {"ok": True, "moved": 0, "state": "ARCHIVED"}
        moved = _tier_study(study, True, session, performed_by)
        session.commit()
        return {"ok": True, "moved": moved, "state": study.lifecycle_state}


def restore_study(study_uid: str, performed_by: str = "lifecycle") -> dict:
    with SyncSessionLocal() as session:
        study = (session.query(Study).filter(Study.study_instance_uid == study_uid)
                 .one_or_none())
        if study is None:
            return {"ok": False, "error": "Study not found"}
        moved = _tier_study(study, False, session, performed_by)
        session.commit()
        return {"ok": True, "moved": moved, "state": study.lifecycle_state}


def purge_study(study_uid: str, performed_by: str = "lifecycle") -> dict:
    with SyncSessionLocal() as session:
        study = (session.query(Study).filter(Study.study_instance_uid == study_uid)
                 .one_or_none())
        if study is None:
            return {"ok": False, "error": "Study not found"}
        _purge_study(study, session, performed_by)
        session.delete(study)
        session.commit()
        return {"ok": True, "purged": True}


def evaluate_and_run(rule_id: int | None = None,
                     performed_by: str = "lifecycle-worker") -> dict:
    """Scan studies, apply the first matching enabled rule, report counts."""
    counts = {"scanned": 0, "archived": 0, "restored": 0, "purged": 0}
    with SyncSessionLocal() as session:
        stmt = select(StudyLifecycleRule).where(StudyLifecycleRule.enabled.is_(True))
        if rule_id:
            stmt = stmt.where(StudyLifecycleRule.id == rule_id)
        rules = (
            session.execute(stmt.order_by(StudyLifecycleRule.priority.asc(),
                                          StudyLifecycleRule.id.asc()))
            .scalars().all()
        )
        if not rules:
            return counts

        studies = session.query(Study).all()
        for study in studies:
            for rule in rules:
                if not _rule_matches(rule, study):
                    continue
                counts["scanned"] += 1
                if rule.action == "ARCHIVE" and study.lifecycle_state != "ARCHIVED":
                    _tier_study(study, True, session, performed_by)
                    counts["archived"] += 1
                elif rule.action == "RESTORE" and study.lifecycle_state == "ARCHIVED":
                    _tier_study(study, False, session, performed_by)
                    counts["restored"] += 1
                elif rule.action == "PURGE" and study.lifecycle_state != "ARCHIVED":
                    _purge_study(study, session, performed_by)
                    session.delete(study)
                    counts["purged"] += 1
                session.flush()
                break
        session.commit()
    return counts
