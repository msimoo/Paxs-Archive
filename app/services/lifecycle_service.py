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
engine for manual runs and per-study actions. Every tier move follows the
stage-then-publish contract (A-1/A-14/FD-1) through
``storage_service.StagedObject``: the destination object is staged and verified
before anything is published, and the source object is unlinked only after the
commit that points at it.

All database work is *sync* (its own SyncSessionLocal) because it is shared
between the daemon worker thread and the async REST layer — the API wraps
these calls in ``asyncio.to_thread`` like the DIMSE SCU helpers do.

All-or-nothing tiering and purge (A-14 / A-15 / FD-2)
----------------------------------------------------
Two ways this used to leave the archive disagreeing with itself:

* **Tiering** set ``ARCHIVED`` whenever ``moved > 0``, so a study whose files
  only half moved was marked cold while the rest stayed hot — a split study that
  no later sweep would repair. The state now changes **only when every eligible
  instance moved**, and a failure anywhere reverts the objects this call already
  published, leaving the study on its original tier.
* **Purge** deleted the metadata row and *then* unlinked the objects, and a
  failed unlink was only logged. The row was already gone, so the object stayed
  on the filesystem with nothing pointing at it — PHI invisible to the
  application and impossible to find without a full storage sweep. The order is
  now inverted for a purge: objects first, and **the metadata row is deleted only
  once every object is confirmed gone**. Anything that cannot be removed is
  queued in ``orphaned_objects`` for an operator to retry.

The purge order is deliberately the opposite of a tier move's. A tier move must
publish before committing (the committed row points at the destination), whereas
a purge must delete before committing (a committed row deletion cannot be undone,
and a leftover object has no owner). Each order is chosen so that whatever
inconsistency can survive is the harmless one.

Batched sweeps (C-12 / FP-8)
----------------------------
A sweep used to load the entire ``studies`` table and then let the ORM lazy-load
``series_list`` -> ``instances`` for every row, so an archive of *N* studies cost
*N* extra round-trips and *N* resident row objects. It now walks the table with
**keyset pagination** (``WHERE id > :last_id ORDER BY id ASC LIMIT :batch``),
**eager-loads** both relationship levels per chunk with ``selectinload``, and
calls ``session.expunge_all()`` between chunks. Peak memory is therefore one
chunk rather than the archive, and the statement count is ``O(N / batch)``.

Keyset (not ``OFFSET``) is deliberate: an ``OFFSET`` page re-walks everything it
skips and can silently skip or repeat a row when a concurrent ingest inserts
into the range already passed. The transaction shape is unchanged — every chunk
still flushes into the *single* transaction the caller commits, so A-14's
all-or-nothing tier move still holds across the whole sweep.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload

from app.db.models import OrphanedObject, Patient, RadiologyReport, Series, Study, StudyLifecycleRule
from app.db.session import SyncSessionLocal
from app.services import audit_service, query_service, storage_service

log = logging.getLogger("paxs.lifecycle")

# Cold-tier object prefix. Keep it short; thumbnails live under `.thumbnails/`.
ARCHIVE_PREFIX = ".archive/"

#: Studies loaded (and eagerly hydrated) per sweep chunk (C-12). Bounds peak
#: memory and turns the per-study lazy loads into a fixed number of set-based
#: queries per chunk.
SWEEP_BATCH_SIZE = 50

#: A study holding one of these report statuses is a signed medical record and
#: must not be destroyed by a purge (C-19). `PURGING` is intentionally excluded:
#: it is a lifecycle state, not a report status.
SIGNED_REPORT_STATUSES = ("FINALIZED", "AMENDED")

VALID_ACTIONS = ("ARCHIVE", "RESTORE", "PURGE")


async def list_studies_by_lifecycle_state(
    session: AsyncSession,
    state: str,
    tenant_id: int | None,
    include_all: bool = False,
) -> list[tuple[Study, str, str]]:
    """Return archived-study rows only when both Study and Patient are in scope."""
    stmt = (
        select(Study, Patient.patient_id, Patient.patient_name)
        .join(Patient, Study.patient_id == Patient.id)
        .where(Study.lifecycle_state == state)
        .order_by(Study.study_date.desc().nulls_last(), Study.id.desc())
        .limit(200)
    )
    stmt = query_service.apply_study_tenant_scope(stmt, tenant_id, include_all)
    return list((await session.execute(stmt)).all())


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
def _utc_datetime(value: datetime) -> datetime:
    """Treat stored naive datetimes as UTC and normalize aware values to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _study_age_days(study: Study, now_utc: datetime | None = None) -> int:
    """Age in whole UTC days from StudyDate, falling back to created_at."""
    now = _utc_datetime(now_utc or datetime.now(timezone.utc))
    study_datetime = None
    if study.study_date and len(study.study_date) == 8 and study.study_date.isdigit():
        try:
            study_datetime = datetime.strptime(study.study_date, "%Y%m%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError as exc:
            # An 8-digit-but-invalid StudyDate (e.g. 20260231) falls back to
            # created_at below; the type is enough to spot bad source data.
            log.debug("study_date_unparsable error_type=%s", type(exc).__name__)
    if study_datetime is None and study.created_at is not None:
        study_datetime = _utc_datetime(study.created_at)
    if study_datetime is None:
        return 0
    return (now - study_datetime).days


def _candidate_predicate(rules: list[StudyLifecycleRule], now_utc: datetime | None = None):
    """SQL superset covering the age cutoff of **every** rule at once.

    DICOM StudyDate is stored as ``YYYYMMDD``, so lexicographic comparison is
    chronological. The SQL condition is a superset for legacy rows whose
    ``created_at`` is old; :func:`_rule_matches` applies the exact age check and
    rejects rows with a newer valid StudyDate.

    A rule's candidate query only has to be a *superset*, so the union over the
    rules is simply the most inclusive cutoff — the smallest ``min_age_days``.
    That is what lets the sweep be one pass instead of one pass per rule (C-12).
    """
    now = _utc_datetime(now_utc or datetime.now(timezone.utc))
    min_age_days = min(max(0, int(rule.min_age_days or 0)) for rule in rules)
    oldest_inclusive_date = now.date() - timedelta(days=min_age_days)
    exclusive_date = oldest_inclusive_date + timedelta(days=1)
    cutoff_date = exclusive_date.strftime("%Y%m%d")
    created_cutoff = (now - timedelta(days=min_age_days)).replace(tzinfo=None)
    return or_(
        Study.study_date < cutoff_date,
        Study.created_at <= created_cutoff,
    )


def iter_sweep_batches(
    session: Session,
    rules: list[StudyLifecycleRule],
    *,
    batch_size: int = SWEEP_BATCH_SIZE,
    now_utc: datetime | None = None,
) -> Iterator[list[Study]]:
    """Yield age-eligible studies in ascending id order, one eager-loaded chunk
    at a time (C-12 / FP-8).

    ``WHERE id > :last_id ORDER BY id ASC LIMIT :batch_size`` is stable under
    concurrent inserts (unlike ``OFFSET``) and is an index range scan no matter
    how deep the sweep has walked. The two ``selectinload`` calls hydrate
    ``series_list`` and ``instances`` for the whole chunk in one query each, so a
    chunk costs three statements instead of one lazy load per study.
    """
    predicate = _candidate_predicate(rules, now_utc)
    last_id = 0
    while True:
        statement = (
            select(Study)
            .options(selectinload(Study.series_list).selectinload(Series.instances))
            .where(predicate, Study.id > last_id)
            .order_by(Study.id.asc())
            .limit(batch_size)
        )
        batch = list(session.execute(statement).scalars().all())
        if not batch:
            return
        yield batch
        last_id = batch[-1].id


def _candidate_studies(
    session: Session,
    rule: StudyLifecycleRule,
    *,
    now_utc: datetime | None = None,
) -> list[Study]:
    """Every age-eligible study for one rule, eagerly loaded.

    Kept for callers that want the whole set in hand (the cutoff boundary tests
    do). The sweep itself pages through :func:`iter_sweep_batches` so it never
    holds more than one chunk.
    """
    return [
        study
        for batch in iter_sweep_batches(session, [rule], now_utc=now_utc)
        for study in batch
    ]


@dataclass(frozen=True)
class ObjectRef:
    """One stored object, plus what it belongs to."""

    key: str
    backend: str | None
    study_instance_uid: str | None = None
    sop_instance_uid: str | None = None
    #: Indexed size, charged back to the quota when this object is deleted
    #: (C-17). 0 for a derived object (a thumbnail), which is not charged.
    size_bytes: int = 0
    #: Owning tenant, so a delete on a *different* tenant's object still credits
    #: the right quota. ``None`` = legacy unscoped scope.
    tenant_id: int | None = None


@dataclass(frozen=True)
class FailedDeletion:
    """An object that could not be unlinked (A-15)."""

    ref: ObjectRef
    #: Exception type name, or "delete_returned_false" — never a path (N-2).
    reason: str


def _study_object_keys(study: Study) -> list[ObjectRef]:
    """Every object a study owns (instances + thumbnails).

    Collected *before* the study row is deleted, because the cascade takes the
    ``file_path`` values with it. Each instance object carries its indexed size
    so a purge can charge exactly those bytes back to the quota (C-17); the
    thumbnail travels with its instance but is derived and so is not charged.
    """
    refs: list[ObjectRef] = []
    for series in study.series_list:
        for inst in series.instances:
            if inst.file_path:
                refs.append(ObjectRef(inst.file_path, inst.storage_backend,
                                      study.study_instance_uid, inst.sop_instance_uid,
                                      size_bytes=int(inst.file_size_bytes or 0),
                                      tenant_id=study.tenant_id))
            refs.append(ObjectRef(
                storage_service.thumbnail_key(inst.sop_instance_uid),
                inst.storage_backend, study.study_instance_uid, inst.sop_instance_uid,
                tenant_id=study.tenant_id,
            ))
    return refs


def _delete_objects(refs: list[ObjectRef],
                    tenant_id: int | None = None) -> tuple[int, list[FailedDeletion]]:
    """Unlink objects; return ``(deleted, failures)``. Never raises.

    A failure is *reported* rather than swallowed, because only the caller knows
    whether it is fatal: for a purge it is (the metadata row must survive), while
    for a superseded source copy after a committed tier move it is merely
    recorded.

    C-17: each object's indexed bytes are given back to the quota only once it is
    confirmed gone, so a purge that refuses to discard a surviving object also
    refuses to credit back bytes it did not actually free. The ref's own
    ``tenant_id`` wins when set (a sweep touches several tenants at once); the
    ``tenant_id`` argument is the fallback for refs that carry none.
    """
    deleted = 0
    failures: list[FailedDeletion] = []
    for ref in refs:
        try:
            if storage_service.delete_file(
                ref.key, ref.backend,
                tenant_id=ref.tenant_id if ref.tenant_id is not None else tenant_id,
                charge_release_bytes=ref.size_bytes,
            ):
                deleted += 1
            else:
                log.warning(
                    "lifecycle storage backend operation failed backend=%s operation=delete result=false",
                    ref.backend or "default",
                )
                failures.append(FailedDeletion(ref, "delete_returned_false"))
        except Exception as exc:  # noqa: BLE001 - boundary guard: returned to the caller, never dropped
            log.warning(
                "lifecycle storage backend operation failed backend=%s operation=delete error_type=%s",
                ref.backend or "default",
                type(exc).__name__,
            )
            failures.append(FailedDeletion(ref, type(exc).__name__))
    return deleted, failures


def _queue_failed_deletions(session: Session, failures: list[FailedDeletion],
                            operation: str, tenant_id: int | None) -> int:
    """Record failed deletions in the operator-visible retry queue (A-15).

    Re-queuing a key that is already outstanding bumps its attempt counter rather
    than adding a second row, so the queue stays a work list instead of a log.
    """
    for failure in failures:
        existing = session.execute(
            select(OrphanedObject).where(
                OrphanedObject.storage_key == failure.ref.key,
                OrphanedObject.operation == operation,
                OrphanedObject.resolved.is_(False),
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.attempts += 1
            existing.reason = failure.reason[:255]
            existing.last_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None)
            continue
        session.add(OrphanedObject(
            tenant_id=failure.ref.tenant_id if failure.ref.tenant_id is not None else tenant_id,
            storage_key=failure.ref.key[:512],
            storage_backend=failure.ref.backend,
            study_instance_uid=failure.ref.study_instance_uid,
            sop_instance_uid=failure.ref.sop_instance_uid,
            operation=operation,
            reason=failure.reason[:255],
        ))
    if failures:
        log.error("queued %d orphaned object(s) for retry (operation=%s)",
                  len(failures), operation)
    return len(failures)


def list_orphaned_objects(include_resolved: bool = False) -> list[dict]:
    """The failed-deletion queue, newest first — the operator's view (A-15)."""
    with SyncSessionLocal() as session:
        stmt = select(OrphanedObject).order_by(OrphanedObject.id.desc())
        if not include_resolved:
            stmt = stmt.where(OrphanedObject.resolved.is_(False))
        return [
            {
                "id": row.id,
                "operation": row.operation,
                "storage_key": row.storage_key,
                "storage_backend": row.storage_backend,
                "study_instance_uid": row.study_instance_uid,
                "sop_instance_uid": row.sop_instance_uid,
                "reason": row.reason,
                "attempts": row.attempts,
                "resolved": row.resolved,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in session.execute(stmt).scalars().all()
        ]


def retry_orphaned_deletions(limit: int = 200) -> dict:
    """Re-attempt every outstanding failed deletion; returns a small report.

    Safe by construction: an object only reaches this queue when the database no
    longer references it (that is *why* the row was kept), so a retry can only
    ever remove something nobody points at.
    """
    retried = resolved = 0
    with SyncSessionLocal() as session:
        rows = session.execute(
            select(OrphanedObject)
            .where(OrphanedObject.resolved.is_(False))
            .order_by(OrphanedObject.id.asc())
            .limit(max(1, limit))
        ).scalars().all()
        for row in rows:
            retried += 1
            row.attempts += 1
            row.last_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None)
            # C-17: the orphan row remembers the tenant, so a delete that finally
            # succeeds here gives the bytes back the same way the purge would
            # have. A retry of an object already gone is a no-op (delete_file
            # reports False and releases nothing).
            _deleted, failures = _delete_objects([
                ObjectRef(row.storage_key, row.storage_backend,
                          row.study_instance_uid, row.sop_instance_uid)
            ], row.tenant_id)
            if failures:
                row.reason = failures[0].reason[:255]
            else:
                row.resolved = True
                resolved += 1
        session.commit()
    return {"retried": retried, "resolved": resolved, "remaining": retried - resolved}


def _tier_destination(key: str, to_archive: bool) -> str | None:
    """Destination key for a tier move, or None when the object is already there."""
    if not key:
        return None
    if to_archive:
        return None if key.startswith(ARCHIVE_PREFIX) else f"{ARCHIVE_PREFIX}{key}"
    return None if not key.startswith(ARCHIVE_PREFIX) else key[len(ARCHIVE_PREFIX):]


def _stage_instance(inst, to_archive: bool, study: Study
                    ) -> tuple[storage_service.StagedObject, ObjectRef]:
    """Stage one instance at its tier destination. Raises on any failure.

    Phase 1 + 2 of the contract: the bytes go to an isolated staging area and are
    read back and hashed before anything can replace the source. A failure here
    aborts the whole move, so the caller's rollback sees a study that is still
    entirely on its original tier.
    """
    key = inst.file_path
    dest = _tier_destination(key, to_archive)
    if dest is None:  # unreachable — the caller filters with the same helper
        raise storage_service.StorageError(f"instance {inst.id} has no tier destination")
    backend = inst.storage_backend
    # charge_bytes=0: a tier move relocates an existing object, so re-charging
    # would double-count it (the source row already holds those bytes). The
    # source ref therefore releases 0 on unlink too — the bytes never left the
    # archive, they only changed key.
    try:
        obj = storage_service.stage_bytes(
            dest, storage_service.read_bytes(key, backend),
            backend_name=backend, charge_bytes=0,
        )
        obj.verify()
    except Exception as exc:
        log.warning(
            "lifecycle storage backend operation failed backend=%s operation=stage error_type=%s",
            backend or "default",
            type(exc).__name__,
        )
        raise
    return obj, ObjectRef(key, backend, study.study_instance_uid,
                          inst.sop_instance_uid, tenant_id=study.tenant_id)


def _stage_tier_study(
    study: Study, to_archive: bool, session: Session, performed_by: str
) -> tuple[list[storage_service.StagedObject], list[ObjectRef], int, int]:
    """Stage a study's instance objects hot <-> cold without publishing them.

    Returns ``(staged, source_refs, moved, total)``; ``total`` counts every
    instance that *needs* moving. Nothing at the destination exists and no source
    has been touched, so a failure leaves the study exactly where it was. The
    caller promotes, commits, then unlinks the sources — see ``_tier_and_publish``.
    """
    eligible = [
        inst
        for series in study.series_list
        for inst in series.instances
        if _tier_destination(inst.file_path or "", to_archive) is not None
    ]
    total = len(eligible)
    staged: list[storage_service.StagedObject] = []
    sources: list[ObjectRef] = []
    try:
        for inst in eligible:
            obj, source = _stage_instance(inst, to_archive, study)
            staged.append(obj)
            sources.append(source)
            inst.file_path = obj.key
    except Exception:
        # A failure part-way through leaves staged temp files behind; the sources
        # were never touched, so dropping them is the whole rollback.
        storage_service.discard_staged(staged)
        raise
    moved = len(staged)

    if moved != total:
        # _stage_instance raises rather than half-succeeding, so this is
        # unreachable today — but "moved > 0 means ARCHIVED" is precisely the bug
        # being fixed (A-14), and a future `continue` would reintroduce it
        # silently. Fail loudly instead of marking a split study cold.
        raise storage_service.StorageError(
            f"tier move incomplete for {study.study_instance_uid}: "
            f"{moved} of {total} instance(s) staged"
        )

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
        log.info("%s study %s (%d/%d objects)",
                 "Archived" if to_archive else "Restored",
                 study.study_instance_uid, moved, total)
    return staged, sources, moved, total


def _purge_objects(study: Study, session, performed_by: str) -> None:
    """Record the purge and delete the study row. **Files must be gone first.**

    Called only once every object has been confirmed deleted (A-14/A-15): a
    committed row deletion cannot be undone, so an object left behind would have
    no owner at all.
    """
    audit_service.add_audit(
        session,
        sop_instance_uid=None,
        action="PURGED",
        performed_by=performed_by,
        study_instance_uid=study.study_instance_uid,
    )
    session.delete(study)


def signed_report_blocker(session: Session, study: Study) -> str | None:
    """Return a human reason when the default purge forbids destroying a study.

    C-19: a finalized report is a medical document; the images it describes are
    data that may lawfully be purged. ``reports.study_id`` is
    ``ON DELETE RESTRICT``, so the database would refuse the delete anyway — but
    a raw ``IntegrityError`` mid-purge is not an answer an operator can act on.
    Ordinary lifecycle callers ask this first and report *why* they refused,
    before unlinking a single object. An explicit ADMIN override may bypass it.

    Returns ``None`` when the study may be destroyed.
    """
    signed = (
        session.query(RadiologyReport)
        .filter(
            RadiologyReport.study_id == study.id,
            RadiologyReport.status.in_(SIGNED_REPORT_STATUSES),
        )
        .first()
    )
    if signed is None:
        return None
    return (
        f"study {study.study_instance_uid} has a {signed.status} report "
        f"(signed by {signed.radiologist or 'unknown'}); a signed report is a "
        "medical record and outlives its images — retract or archive the report "
        "first"
    )


def study_has_signed_report(study_id: int) -> str | None:
    """Session-owning wrapper around :func:`signed_report_blocker`.

    The REST layer runs on an async session, and borrowing its underlying sync
    session from a worker thread is a documented hazard. This opens its own
    short-lived ``SyncSessionLocal`` instead, so the caller can simply do
    ``await asyncio.to_thread(study_has_signed_report, study.id)``.
    """
    with SyncSessionLocal() as session:
        study = session.get(Study, study_id)
        if study is None:
            return None
        return signed_report_blocker(session, study)


def discard_reports(
    session: Session, study: Study, *, include_signed: bool = False,
    performed_by: str | None = None,
) -> int:
    """Delete study reports and audit signed reports only for an admin purge.

    The foreign key is ``ON DELETE RESTRICT`` and knows nothing about status, so
    it would refuse to delete a study holding even a DRAFT report. A draft is not
    a medical record — it has no signature and no legal standing — so purging the
    images should take it with them. Signed reports are included only when the
    caller explicitly authorizes the ADMIN-only purge path.

    Returns the number of reports removed (for the audit line).
    """
    query = session.query(RadiologyReport).filter(
        RadiologyReport.study_id == study.id
    )
    if not include_signed:
        query = query.filter(RadiologyReport.status.notin_(SIGNED_REPORT_STATUSES))
    reports = query.all()
    for report in reports:
        if include_signed and performed_by:
            audit_service.add_audit(
                session, sop_instance_uid=None, action="REPORT_DELETED",
                performed_by=performed_by,
                study_instance_uid=study.study_instance_uid,
            )
        session.delete(report)
    return len(reports)


def discard_unsigned_reports(session: Session, study: Study) -> int:
    """Backward-compatible wrapper for ordinary lifecycle purges."""
    return discard_reports(session, study)


def _series_modalities(study: Study) -> set[str]:
    return {s.modality.upper() for s in study.series_list if s.modality}


def _series_body_parts(study: Study) -> set[str]:
    return {s.body_part_examined.upper() for s in study.series_list if s.body_part_examined}


@dataclass(frozen=True)
class _RuleSpec:
    """A lifecycle rule's matching fields, copied out of the ORM.

    The sweep calls ``session.expunge_all()`` between chunks, so nothing that
    outlives a chunk may depend on ORM state — including the rule rows
    themselves.
    """

    action: str
    min_age_days: int
    modality: str | None
    body_part_examined: str | None

    @classmethod
    def of(cls, rule: StudyLifecycleRule) -> _RuleSpec:
        return cls(
            action=(rule.action or "").upper(),
            min_age_days=max(0, int(rule.min_age_days or 0)),
            modality=rule.modality,
            body_part_examined=rule.body_part_examined,
        )


def _rule_matches(
    rule: StudyLifecycleRule | _RuleSpec,
    study: Study,
    *,
    now_utc: datetime | None = None,
) -> bool:
    """Exact match test: age, then optional modality / body-part filters.

    The candidate query is only a superset, so this is where the real age
    comparison happens.
    """
    if _study_age_days(study, now_utc) < rule.min_age_days:
        return False
    if rule.modality and rule.modality.upper() not in _series_modalities(study):
        return False
    if rule.body_part_examined:
        return rule.body_part_examined.upper() in _series_body_parts(study)
    return True


# --------------------------------------------------------------------------- #
# Public entry points (sync; safe from worker thread or asyncio.to_thread)
# --------------------------------------------------------------------------- #
def _publish_tier(staged: list[storage_service.StagedObject],
                  published: list[ObjectRef]) -> None:
    """Promote every staged object, verifying each destination as it lands.

    The source objects are unlinked immediately afterwards, so a backend that
    reports success without writing anything must not be trusted: the destination
    is checked before anything is removed.
    """
    for obj in staged:
        obj.promote()
        published.append(ObjectRef(obj.key, obj.backend_name))
        if not storage_service.exists(obj.key, obj.backend_name):
            raise storage_service.StorageError(
                f"tier destination missing after publish: {obj.key!r}"
            )


def _rollback_tier(staged: list[storage_service.StagedObject],
                   published: list[ObjectRef], tenant_id: int | None,
                   session: Session) -> None:
    """Undo a partially applied tier move (A-14).

    The source objects were never unlinked, so reverting means removing the
    destination copies this call published and dropping the staged temp files —
    the study ends up byte-identical to what it was. The session is rolled back
    first, so the in-memory ``file_path`` re-pointing cannot outlive the failure.
    """
    session.rollback()
    storage_service.discard_staged(staged)
    # The published copies carry no indexed size (they were never committed as
    # rows), so they release no quota — their bytes were charged as 0 by the
    # tier move's stage_bytes(..., charge_bytes=0) and the source objects were
    # never unlinked, so the tenant's total is unchanged by the rollback.
    reverted, failures = _delete_objects(published, tenant_id)
    if failures:
        _queue_failed_deletions(session, failures, "tier_rollback", tenant_id)
        session.commit()
    log.warning("tier move rolled back: %d object(s) reverted, %d queued for retry",
                reverted, len(failures))


def _tier_and_publish(study: Study, to_archive: bool, session, performed_by: str
                      ) -> tuple[int, str]:
    """Stage, verify, promote and clean up one tier move; returns (moved, state).

    All-or-nothing (A-14). Promotion precedes the commit because the committed
    rows point at the destination key; any failure reverts every object this call
    published, so the study is left on its original tier rather than half moved.
    """
    # Captured before anything can fail: after a rollback the ORM instance is
    # expired and a lazy attribute read would raise on the failed transaction.
    tenant_id = study.tenant_id
    staged, sources, moved, _total = _stage_tier_study(
        study, to_archive, session, performed_by
    )
    state = study.lifecycle_state          # capture before the commit expires it
    published: list[ObjectRef] = []
    try:
        _publish_tier(staged, published)         # Phase 3 — publish + verify
        session.commit()                         # Phase 2 — the DB commit
    except Exception:
        _rollback_tier(staged, published, tenant_id, session)
        raise
    _deleted, failures = _delete_objects(sources, tenant_id)  # Phase 4 — superseded sources
    if failures:
        # The committed rows already point at the destination, so a source that
        # survived is an unreferenced duplicate, not a lost object — record it,
        # but do not fail an operation that has already succeeded.
        _queue_failed_deletions(session, failures, "tier_source", tenant_id)
        session.commit()
    return moved, state


def archive_study(study_uid: str, performed_by: str = "lifecycle") -> dict:
    with SyncSessionLocal() as session:
        study = (session.query(Study).filter(Study.study_instance_uid == study_uid)
                 .one_or_none())
        if study is None:
            return {"ok": False, "error": "Study not found"}
        if study.lifecycle_state == "ARCHIVED":
            return {"ok": True, "moved": 0, "state": "ARCHIVED"}
        moved, state = _tier_and_publish(study, True, session, performed_by)
        return {"ok": True, "moved": moved, "state": state}


def restore_study(study_uid: str, performed_by: str = "lifecycle") -> dict:
    with SyncSessionLocal() as session:
        study = (session.query(Study).filter(Study.study_instance_uid == study_uid)
                 .one_or_none())
        if study is None:
            return {"ok": False, "error": "Study not found"}
        moved, state = _tier_and_publish(study, False, session, performed_by)
        return {"ok": True, "moved": moved, "state": state}


def _purge_refusal(study_uid: str, study: Study, blocker: str) -> dict:
    """The refusal payload for a study holding a signed report (C-19)."""
    log.warning("purge refused for study %s: signed report present", study_uid)
    return {
        "ok": False,
        "purged": False,
        "state": study.lifecycle_state,
        "blocked_by": "signed_report",
        "error": blocker,
    }


def _purge_object_failure(study_uid: str, deleted: int, failures: list) -> dict:
    """The refusal payload when one or more objects could not be unlinked (A-14)."""
    log.error("purge refused for study %s: %d object(s) could not be deleted; "
              "metadata kept and failures queued", study_uid, len(failures))
    return {
        "ok": False,
        "purged": False,
        "state": "PURGING",
        "deleted": deleted,
        "failed": len(failures),
        "error": "object deletion failed; the study metadata was not deleted",
    }


def purge_study(
    study_uid: str, performed_by: str = "lifecycle", *,
    allow_signed_report: bool = False,
) -> dict:
    """Delete a study's objects (hot + cold) and its metadata — all-or-nothing.

    A-14 / A-15 / FD-2, and the order is the *opposite* of a tier move's:

    1. mark the study ``PURGING`` and commit — a durable marker, so a crash
       mid-purge leaves a row an operator can see instead of a silent half-state;
    2. unlink every object. **If any deletion fails the metadata row is kept**,
       the failures are queued in ``orphaned_objects``, and the study stays
       ``PURGING``;
    3. only once every object is confirmed gone is the row deleted.

    Deleting the row first — as this used to — meant a failed unlink left PHI on
    the filesystem with nothing pointing at it.

    **C-19:** a study carrying a signed report is refused *before* any object is
    unlinked by default. An explicitly authorized ADMIN override may remove the
    report in the same purge transaction; refusing later would be worse than
    useless because the images would already be gone while metadata stayed.
    """
    with SyncSessionLocal() as session:
        study = (session.query(Study).filter(Study.study_instance_uid == study_uid)
                 .one_or_none())
        if study is None:
            return {"ok": False, "error": "Study not found"}

        blocker = None if allow_signed_report else signed_report_blocker(session, study)
        if blocker is not None:
            return _purge_refusal(study_uid, study, blocker)

        refs = _study_object_keys(study)
        tenant_id = study.tenant_id
        study.lifecycle_state = "PURGING"
        session.commit()                     # Phase 1 — durable marker

        deleted, failures = _delete_objects(refs, tenant_id)
        if failures:
            _queue_failed_deletions(session, failures, "purge", tenant_id)
            session.commit()                 # the row is deliberately NOT deleted
            return _purge_object_failure(study_uid, deleted, failures)

        # C-19: the FK is RESTRICT and knows nothing about report status, so
        # reports must be removed explicitly before the study row.
        discarded = discard_reports(
            session, study, include_signed=allow_signed_report,
            performed_by=performed_by,
        )
        _purge_objects(study, session, performed_by)
        session.commit()                     # Phase 3 — metadata goes last
        log.warning("Purged study %s (%d objects, %d report(s) discarded)",
                    study_uid, deleted, discarded)
        return {
            "ok": True,
            "purged": True,
            "deleted": deleted,
            "failed": 0,
            "reports_discarded": discarded,
        }


def _enabled_rules(session: Session, rule_id: int | None) -> list[StudyLifecycleRule]:
    """Enabled rules, highest priority first."""
    stmt = select(StudyLifecycleRule).where(StudyLifecycleRule.enabled.is_(True))
    if rule_id:
        stmt = stmt.where(StudyLifecycleRule.id == rule_id)
    return list(
        session.execute(
            stmt.order_by(StudyLifecycleRule.priority.asc(), StudyLifecycleRule.id.asc())
        ).scalars().all()
    )


def _sweep_rules(session: Session, rules: list[StudyLifecycleRule], counts: dict,
                 performed_by: str, *, batch_size: int = SWEEP_BATCH_SIZE
                 ) -> tuple[list[str], list[storage_service.StagedObject],
                            list[ObjectRef]]:
    """Apply the first matching rule to every study, one chunk at a time.

    C-12/FP-8: the sweep walks the candidate set with keyset pagination and
    eager-loads ``series_list``/``instances`` per chunk, then drops the chunk
    from the identity map, so peak memory is one chunk rather than the whole
    archive. Every chunk still flushes into the *same* transaction, so the
    caller's single commit keeps the tier moves all-or-nothing (A-14).

    Tier moves are staged here and published by the caller in one commit. Purges
    are only *collected*: a purge deletes its objects before its row, so it needs
    its own transaction and cannot share one with the tier moves.
    """
    purge_uids: list[str] = []
    staged_all: list[storage_service.StagedObject] = []
    sources_all: list[ObjectRef] = []
    now_utc = datetime.now(timezone.utc)
    # Snapshot the rule config up front: `expunge_all()` below detaches the ORM
    # rule rows along with each chunk's studies.
    specs = [_RuleSpec.of(rule) for rule in rules]
    saw_candidate = False

    for batch in iter_sweep_batches(session, rules, batch_size=batch_size, now_utc=now_utc):
        saw_candidate = True
        for study in batch:
            for spec in specs:
                if not _rule_matches(spec, study, now_utc=now_utc):
                    continue
                counts["scanned"] += 1
                if spec.action == "ARCHIVE" and study.lifecycle_state != "ARCHIVED":
                    staged, sources, _moved, _total = _stage_tier_study(
                        study, True, session, performed_by)
                    staged_all += staged
                    sources_all += sources
                    counts["archived"] += 1
                elif spec.action == "RESTORE" and study.lifecycle_state == "ARCHIVED":
                    staged, sources, _moved, _total = _stage_tier_study(
                        study, False, session, performed_by)
                    staged_all += staged
                    sources_all += sources
                    counts["restored"] += 1
                elif spec.action == "PURGE" and study.lifecycle_state != "ARCHIVED":
                    purge_uids.append(study.study_instance_uid)
                break
        # The chunk's UPDATEs must reach the transaction *before* the rows are
        # dropped — `expunge_all()` discards pending work, not flushed work.
        session.flush()
        session.expunge_all()

    if not saw_candidate:
        log.info("Lifecycle sweep found no age-eligible studies (rules=%d)", len(rules))
    return purge_uids, staged_all, sources_all


def _publish_and_commit(session: Session, staged_all: list[storage_service.StagedObject],
                        sources_all: list[ObjectRef]) -> None:
    """Publish the sweep's staged tier moves, commit, then unlink the sources."""
    try:
        storage_service.publish_staged(staged_all)   # Phase 3
        session.commit()                             # Phase 2 — one commit
    except Exception:
        storage_service.discard_staged(staged_all)
        raise
    _deleted, failures = _delete_objects(sources_all)  # Phase 4 — superseded
    if failures:
        # Each ref carries its own tenant (a sweep spans tenants), so the queued
        # row attributes the failure correctly; the fallback is the legacy scope.
        _queue_failed_deletions(session, failures, "tier_source", None)
        session.commit()


def evaluate_and_run(rule_id: int | None = None,
                     performed_by: str = "lifecycle-worker") -> dict:
    """Scan studies, apply the first matching enabled rule, report counts.

    Tier moves are one stage-then-publish unit. Purges run afterwards, each in
    its own all-or-nothing transaction, and a refused purge is counted separately
    rather than reported as done.
    """
    counts = {"scanned": 0, "archived": 0, "restored": 0, "purged": 0, "purge_refused": 0}
    with SyncSessionLocal() as session:
        rules = _enabled_rules(session, rule_id)
        if not rules:
            return counts
        purge_uids, staged_all, sources_all = _sweep_rules(session, rules, counts, performed_by)
        _publish_and_commit(session, staged_all, sources_all)

    for study_uid in purge_uids:
        if purge_study(study_uid, performed_by).get("purged"):
            counts["purged"] += 1
        else:
            counts["purge_refused"] += 1
    return counts
