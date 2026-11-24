"""Storage Quota Management Service.

Enforces tenant-scoped and backend-scoped storage limits.

Architecture (C-17 / FD-7):
  - Quotas are defined in the `storage_quotas` table (StorageQuota model).
  - **Two independent scopes, never ANDed.** A *tenant* quota caps the total
    bytes a tenant owns **across every backend**; a *backend* quota caps the
    total bytes resident on one backend **across every tenant**. Both are
    evaluated for a write, and the stricter one that would be crossed is what
    refuses it.
  - This is the C-17 fix. The previous implementation filtered the quota list
    with ``tenant_id == :t AND storage_backend_name == :b`` and then measured
    tenant usage with the same backend filter, so naming a backend silently
    excluded the tenant-wide row — a tenant could exceed its cap simply by
    storing to a named backend.
  - A quota of 0 means unlimited.
  - warning_threshold_pct (default 80%) triggers advisory alerts but does not block writes.

Integration points (H08):
  - C-STORE and STOW-RS call `reserve_quota()` **before** staging an object and
    `release_quota()` if the ingest fails; `commit_quota()` is a no-op today
    because the reservation is already counted as used.
  - Every deletion path calls `release_quota()` (via
    `storage_service.delete_file` and friends) with the *exact* verified byte
    size, so a purge gives the bytes back instead of leaking them (C-17).
  - API endpoints expose current usage vs limits and allow CRUD of quota rules.
  - Worker can periodically evaluate quotas and emit warnings.
  - `recompute_used_bytes()` reconciles every denormalised counter with the
    indexed bytes; `recompute_tenant_storage(tenant_id)` is the tenant-scoped,
    idempotent variant the worker runs on a schedule (C-17).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Instance, StorageQuota, Tenant
from app.db.session import SyncSessionLocal

log = logging.getLogger("paxs.quota")


@dataclass
class QuotaInfo:
    """Aggregated quota information for a scope (tenant or backend)."""
    scope_type: str  # "tenant" | "backend"
    scope_id: str    # tenant.code or backend name
    limit_bytes: int  # 0 = unlimited
    used_bytes: int
    warning_threshold_pct: int
    enabled: bool
    warning_triggered: bool

    @property
    def available_bytes(self) -> int:
        if self.limit_bytes <= 0:
            return 2**63 - 1  # effectively unlimited
        return max(0, self.limit_bytes - self.used_bytes)

    @property
    def usage_pct(self) -> float:
        if self.limit_bytes <= 0:
            return 0.0
        return (self.used_bytes / self.limit_bytes) * 100

    @property
    def is_exceeded(self) -> bool:
        return self.limit_bytes > 0 and self.used_bytes >= self.limit_bytes

    @property
    def is_warning(self) -> bool:
        if self.limit_bytes <= 0:
            return False
        return self.usage_pct >= self.warning_threshold_pct


def get_effective_quotas(tenant_id: int | None = None,
                         backend_name: str | None = None) -> list[StorageQuota]:
    """Fetch every enabled quota that applies to a write.

    **The two scopes are ORed, not ANDed (C-17).** A tenant-wide quota applies
    to *any* write that tenant makes, whatever backend it lands on; a
    backend-wide quota applies to *any* write into that backend, whatever tenant
    made it. Deriving this list with ``AND`` — as this used to — meant that
    naming a backend narrowed a tenant quota down to that backend (and, worse,
    dropped the tenant row entirely when the tenant quota named no backend), so
    the cap an operator set to bound a customer was never enforced.

    Returns quotas ordered by ``max_bytes`` ASC (strictest first) so the caller
    can simply take the first non-zero limit as the effective limit.
    """
    with SyncSessionLocal() as session:
        stmt = select(StorageQuota).where(
            StorageQuota.enabled.is_(True),
            StorageQuota.scope_matches(tenant_id, backend_name),
        )
        quotas = session.execute(
            stmt.order_by(StorageQuota.max_bytes.asc())
        ).scalars().all()
    return list(quotas)


def get_tenant_usage_bytes(tenant_id: int, backend_name: str | None = None) -> int:
    """Bytes owned by a tenant, summed across **all** backends by default.

    ``backend_name`` narrows the total to one backend, which is only ever what a
    *backend*-scoped question wants; a **tenant** quota must call this with no
    backend filter, so the figure it measures is the one whose cap it enforces
    (C-17). Passing a tenant quota's own ``storage_backend_name`` here — as the
    old code did — turned a tenant-wide limit into a single-backend limit.
    """
    with SyncSessionLocal() as session:
        stmt = (
            select(func.coalesce(func.sum(Instance.file_size_bytes), 0))
            .join(Instance.series)
            .join(Series.study)
            .where(Study.tenant_id == tenant_id)
        )
        if backend_name:
            stmt = stmt.where(Instance.storage_backend == backend_name)
        return int(session.execute(stmt).scalar_one() or 0)


def get_backend_usage_bytes(backend_name: str | None) -> int:
    """Calculate current storage usage for a backend (all tenants)."""
    # backend_name=None means the env default backend
    with SyncSessionLocal() as session:
        stmt = select(func.coalesce(func.sum(Instance.file_size_bytes), 0)).where(
            Instance.storage_backend == backend_name
        )
        return int(session.execute(stmt).scalar_one() or 0)


def _quota_usage(q: StorageQuota) -> tuple[str, str, int]:
    """``(scope_type, scope_id, used_bytes)`` measured the way the quota means it.

    A **tenant** quota is a tenant-wide cap, so its usage is the tenant's total
    across every backend — never narrowed by the quota row's own
    ``storage_backend_name``. The C-17 defect was calling
    ``get_tenant_usage_bytes(tenant_id, q.storage_backend_name)``, which measured
    only one backend and so let a tenant exceed its cap by writing elsewhere.
    """
    if q.tenant_id is not None:
        return "tenant", _tenant_code(q.tenant_id), get_tenant_usage_bytes(q.tenant_id)
    return "backend", (q.storage_backend_name or "default"), get_backend_usage_bytes(q.storage_backend_name)


def _tenant_code(tenant_id: int) -> str:
    """Human-readable tenant code for reporting (falls back to the id)."""
    with SyncSessionLocal() as session:
        tenant = session.get(Tenant, tenant_id)
        return tenant.code if tenant else str(tenant_id)


def check_quota_before_write(tenant_id: int | None,
                             backend_name: str | None,
                             additional_bytes: int = 0) -> tuple[bool, list[QuotaInfo]]:
    """Check if a write of `additional_bytes` would exceed any applicable quota.

    Returns (allowed, quota_infos). If allowed is False, the write should be rejected.
    quota_infos contains all applicable quotas with current usage for reporting.
    """
    quotas = get_effective_quotas(tenant_id, backend_name)
    if not quotas:
        return True, []

    # Calculate usage for each quota scope (tenant total across all backends,
    # backend total across all tenants — see _quota_usage).
    infos: list[QuotaInfo] = []
    for q in quotas:
        scope_type, scope_id, used = _quota_usage(q)
        info = QuotaInfo(
            scope_type=scope_type,
            scope_id=scope_id,
            limit_bytes=q.max_bytes,
            used_bytes=used + additional_bytes,
            warning_threshold_pct=q.warning_threshold_pct,
            enabled=q.enabled,
            warning_triggered=False,
        )
        info.warning_triggered = info.is_warning
        infos.append(info)

    # Determine if any quota would be exceeded
    allowed = all(not info.is_exceeded for info in infos)

    if not allowed:
        exceeded = [f"{i.scope_type}:{i.scope_id}" for i in infos if i.is_exceeded]
        log.warning("Quota exceeded for write: %s", ", ".join(exceeded))

    # Log warnings
    for info in infos:
        if info.warning_triggered:
            log.warning(
                "Storage quota warning: %s:%s at %.1f%% (threshold %d%%)",
                info.scope_type, info.scope_id, info.usage_pct, info.warning_threshold_pct
            )

    return allowed, infos


def get_quota_status(tenant_id: int | None = None,
                     backend_name: str | None = None) -> list[QuotaInfo]:
    """Get current quota status for reporting/monitoring (does not block)."""
    quotas = get_effective_quotas(tenant_id, backend_name)
    infos: list[QuotaInfo] = []
    for q in quotas:
        scope_type, scope_id, used = _quota_usage(q)
        infos.append(QuotaInfo(
            scope_type=scope_type,
            scope_id=scope_id,
            limit_bytes=q.max_bytes,
            used_bytes=used,
            warning_threshold_pct=q.warning_threshold_pct,
            enabled=q.enabled,
            warning_triggered=(
                used > 0 and q.max_bytes > 0
                and (used / q.max_bytes * 100) >= q.warning_threshold_pct
            ),
        ))
    return infos


async def get_quota_status_async(db: AsyncSession,
                                 tenant_id: int | None = None,
                                 backend_name: str | None = None) -> list[dict]:
    """Async version for API endpoints.

    Scopes are ORed (C-17): the caller's ``tenant_id`` and ``backend_name`` each
    select the rows of that scope; a tenant row's usage is always the tenant's
    total across every backend, never narrowed by the row's own
    ``storage_backend_name``.
    """
    stmt = select(StorageQuota).where(
        StorageQuota.enabled.is_(True),
        StorageQuota.scope_matches(tenant_id, backend_name),
    )
    quotas = (await db.execute(stmt.order_by(StorageQuota.max_bytes.asc()))).scalars().all()

    results = []
    for q in quotas:
        if q.tenant_id is not None:
            # Tenant-scoped quota: total bytes across ALL backends (no backend
            # filter — the whole point of a tenant-wide cap, C-17).
            used_stmt = (
                select(func.coalesce(func.sum(Instance.file_size_bytes), 0))
                .join(Instance.series)
                .join(Series.study)
                .where(Study.tenant_id == q.tenant_id)
            )
            used = int((await db.execute(used_stmt)).scalar_one() or 0)
            scope_type = "tenant"
            tenant = await db.get(Tenant, q.tenant_id)
            scope_id = tenant.code if tenant else str(q.tenant_id)
        else:
            # Backend-scoped quota
            used = int((
                await db.execute(
                    select(func.coalesce(func.sum(Instance.file_size_bytes), 0))
                    .where(Instance.storage_backend == q.storage_backend_name)
                )
            ).scalar_one() or 0)
            scope_type = "backend"
            scope_id = q.storage_backend_name or "default"

        results.append({
            "quota_id": q.id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "limit_bytes": q.max_bytes,
            "used_bytes": used,
            "available_bytes": max(0, q.max_bytes - used) if q.max_bytes > 0 else None,
            "usage_pct": round((used / q.max_bytes * 100), 2) if q.max_bytes > 0 else 0.0,
            "warning_threshold_pct": q.warning_threshold_pct,
            "is_exceeded": q.max_bytes > 0 and used >= q.max_bytes,
            "is_warning": q.max_bytes > 0 and used > 0 and (used / q.max_bytes * 100) >= q.warning_threshold_pct,
            "enabled": q.enabled,
        })
    return results


# Import at bottom to avoid circular dependency
from app.db.models import Series, Study


# --------------------------------------------------------------------------- #
# H08 — atomic reservation
#
# `check_quota_before_write` (above) is a *reporting* helper: it computes usage
# from indexed rows and cannot stop two concurrent ingests from both passing.
# Enforcement uses a single atomic UPDATE against the counter instead, so the
# check and the reservation cannot be interleaved.
# --------------------------------------------------------------------------- #
class QuotaExceeded(RuntimeError):
    """Raised when a reservation would push a scope over its limit."""

    def __init__(self, infos: list["QuotaInfo"]):
        self.infos = infos
        exceeded = ", ".join(f"{i.scope_type}:{i.scope_id}" for i in infos)
        super().__init__(f"Storage quota exceeded for {exceeded}")

    @property
    def exceeded_infos(self) -> list["QuotaInfo"]:
        """The quotas that were actually crossed (limits that are now met)."""
        return [i for i in self.infos if i.is_exceeded]

    @property
    def tenant_limited(self) -> bool:
        """True when the *tenant-wide* cap is what refused the write (C-17).

        The distinction matters at the HTTP boundary: a tenant at its cap is a
        *client* condition — this institution may not store more — which maps to
        **413**, whereas a backend that is full is an *archive* condition and
        keeps **507**. Both are Out of Resources on the DIMSE side.
        """
        return any(i.scope_type == "tenant" for i in self.exceeded_infos)

    @property
    def http_status(self) -> int:
        """The HTTP status a STOW-RS refusal should carry (C-17)."""
        return 413 if self.tenant_limited else 507


def reserve_quota(tenant_id: int | None, backend_name: str | None,
                  size: int) -> list["QuotaInfo"]:
    """Atomically reserve `size` bytes against every applicable quota.

    Each quota is claimed with a conditional UPDATE, so the check and the
    increment happen in one statement:

        UPDATE storage_quotas
           SET used_bytes = used_bytes + :size
         WHERE id = :id AND used_bytes + :size <= max_bytes

    A `rowcount` of 0 means the limit would be crossed. The claims happen in one
    transaction, so a later refusal rolls back the earlier increments rather
    than leaving the counter inflated.

    Returns the applicable quota infos (for reporting). Raises QuotaExceeded
    when any limit would be crossed.

    C-17: the applicable set includes the tenant-wide row *and* any
    backend-scoped row (see :func:`get_effective_quotas`), so a tenant cap is
    enforced even when the write names a backend.
    """
    if size <= 0:
        return []
    quotas = get_effective_quotas(tenant_id, backend_name)
    if not quotas:
        return []

    infos: list[QuotaInfo] = []
    with SyncSessionLocal() as session:
        try:
            for q in quotas:
                # Report the usage *as if* the reservation succeeded, so a
                # refusal names the scope(s) that would have been crossed —
                # `QuotaExceeded.exceeded_infos` depends on this being accurate.
                infos.append(_info_for(session, q, additional_bytes=size))
                if not q.max_bytes or q.max_bytes <= 0:
                    continue  # unlimited
                result = session.execute(
                    text(
                        "UPDATE storage_quotas "
                        "SET used_bytes = used_bytes + :size "
                        "WHERE id = :qid "
                        "AND used_bytes + :size <= max_bytes"
                    ),
                    {"size": size, "qid": q.id},
                )
                if result.rowcount != 1:
                    session.rollback()
                    log.warning(
                        "Quota reservation refused: %s:%s would exceed %d bytes",
                        "tenant" if q.tenant_id else "backend",
                        q.tenant_id or q.storage_backend_name or "default",
                        q.max_bytes,
                    )
                    raise QuotaExceeded(infos)
            session.commit()
        except QuotaExceeded:
            raise
        except Exception:
            session.rollback()
            raise
    return infos


def release_quota(tenant_id: int | None, backend_name: str | None, size: int) -> None:
    """Give back ``size`` bytes to every applicable quota (ingest failure or delete).

    The single decrement primitive (C-17). It is called both by
    :meth:`storage_service.StagedObject.discard` when an ingest fails and by the
    deletion paths when an object is destroyed, so a purge returns the bytes a
    reservation once consumed instead of leaking them.

    The update is conditional on the row's own counter, so two concurrent
    releases cannot interleave into a lost update; ``MAX(0, ...)`` keeps the
    counter sane even if a release is duplicated (a double delete, a retried
    orphan sweep), because a counter that went negative would make every later
    write look free.
    """
    if size <= 0:
        return
    quotas = get_effective_quotas(tenant_id, backend_name)
    if not quotas:
        return
    with SyncSessionLocal() as session:
        for q in quotas:
            # MAX(0, ...) keeps the counter sane even if a release is duplicated.
            # Released for *every* applicable row, including an unlimited one:
            # an unlimited quota must still have accumulated the increment
            # charged on write, or it would drift upward forever.
            session.execute(
                text(
                    "UPDATE storage_quotas "
                    "SET used_bytes = CASE WHEN used_bytes >= :size "
                    "THEN used_bytes - :size ELSE 0 END "
                    "WHERE id = :qid"
                ),
                {"size": size, "qid": q.id},
            )
        session.commit()


def decrement_usage(tenant_id: int | None, backend_name: str | None,
                    size: int) -> int:
    """Decrement the counters after ``size`` bytes were **verified deleted**.

    An alias of :func:`release_quota` under a name that says what a deletion
    path means by it. Kept separate so the intent is legible at the call site:
    an ingest failure *releases a reservation*, a delete *gives back capacity*,
    and both must move the same counters by the same arithmetic.

    Returns the number of bytes given back (0 when nothing was charged), so a
    caller can report exactly how much the deletion returned.
    """
    if size <= 0:
        return 0
    release_quota(tenant_id, backend_name, size)
    return size


def _info_for(session, q: StorageQuota, additional_bytes: int = 0) -> QuotaInfo:
    """A QuotaInfo built from the counter (single source of truth).

    ``additional_bytes`` is added to the counter so a caller reporting a *pending*
    reservation sees the value the scope would hold if it succeeded.
    """
    if q.tenant_id is not None:
        tenant = session.get(Tenant, q.tenant_id)
        scope_type, scope_id = "tenant", (tenant.code if tenant else str(q.tenant_id))
    else:
        scope_type, scope_id = "backend", (q.storage_backend_name or "default")
    info = QuotaInfo(
        scope_type=scope_type,
        scope_id=scope_id,
        limit_bytes=q.max_bytes,
        used_bytes=q.used_bytes + additional_bytes,
        warning_threshold_pct=q.warning_threshold_pct,
        enabled=q.enabled,
        warning_triggered=False,
    )
    info.warning_triggered = info.is_warning
    return info


def recompute_used_bytes(tenant_id: int | None = None) -> int:
    """Reconcile every counter with the indexed bytes; returns rows updated.

    Each row is recomputed with the measurement its *own scope* means: a tenant
    row against the tenant's total across every backend (C-17), a backend row
    against that backend's total. Idempotent by construction — a second run over
    unchanged data changes nothing.
    """
    with SyncSessionLocal() as session:
        stmt = select(StorageQuota)
        if tenant_id is not None:
            stmt = stmt.where(StorageQuota.tenant_id == tenant_id)
        rows = session.execute(stmt).scalars().all()
        updated = 0
        for q in rows:
            actual = _measured_usage(q)
            if q.used_bytes != actual:
                log.info("reconciled quota %s (%s) %d -> %d bytes",
                         q.id, _quota_label(q), q.used_bytes, actual)
                q.used_bytes = actual
                updated += 1
        session.commit()
    return updated


def _measured_usage(q: StorageQuota) -> int:
    """The counter value this row *should* hold, from the indexed bytes."""
    if q.tenant_id is not None:
        # A tenant quota is tenant-wide: total across all backends.
        return get_tenant_usage_bytes(q.tenant_id)
    return get_backend_usage_bytes(q.storage_backend_name)


def _quota_label(q: StorageQuota) -> str:
    return (f"tenant:{q.tenant_id}" if q.tenant_id is not None
            else f"backend:{q.storage_backend_name or 'default'}")


def recompute_tenant_storage(tenant_id: int) -> dict:
    """Reconcile a tenant's storage counters from the indexed bytes (C-17).

    The idempotent background job the quota contract requires. It recomputes the
    tenant's total across **all** backends once, then writes that single figure
    into every tenant-scoped quota row for the tenant — the counters that
    reserve/release maintain incrementally and that drift whenever a deletion
    path failed to decrement (the C-17 exhaustion defect).

    Idempotent: the value written is derived entirely from the indexed instance
    sizes, never from the current counter, so running it twice (or concurrently
    with an idle archive) leaves the counters unchanged and returns
    ``reconciled == 0``. Safe to run on a schedule: it takes one short write
    transaction and never deletes or moves an object.

    Returns a small report ``{"tenant_id", "actual_bytes", "quotas",
    "reconciled", "drift_bytes"}``. ``drift_bytes`` is the net correction applied
    (positive = the counter had leaked upward), so an operator can see how far
    off the archive had been.
    """
    actual = get_tenant_usage_bytes(tenant_id)
    quotas = 0
    reconciled = 0
    drift = 0
    with SyncSessionLocal() as session:
        rows = session.execute(
            select(StorageQuota).where(StorageQuota.tenant_id == tenant_id)
        ).scalars().all()
        for q in rows:
            quotas += 1
            if q.used_bytes != actual:
                drift += q.used_bytes - actual
                q.used_bytes = actual
                reconciled += 1
        if reconciled:
            session.commit()
            log.info(
                "recompute_tenant_storage: tenant %d reconciled %d/%d quota row(s) "
                "to %d bytes (drift %+d)",
                tenant_id, reconciled, quotas, actual, drift,
            )
    return {
        "tenant_id": tenant_id,
        "actual_bytes": actual,
        "quotas": quotas,
        "reconciled": reconciled,
        "drift_bytes": drift,
    }


def scheduled_reconcile_tenants() -> dict:
    """Run :func:`recompute_tenant_storage` for every tenant with a quota row.

    The worker's scheduled entry point: a tenant can drift without anyone
    touching it (a purge that only half-decremented, a crash mid-cleanup), so the
    sweep is what actually closes C-17 over time rather than relying on every
    path being perfect forever. Returns a summary of the whole pass.
    """
    with SyncSessionLocal() as session:
        tenant_ids = [
            t for (t,) in session.execute(
                select(StorageQuota.tenant_id)
                .where(StorageQuota.tenant_id.is_not(None))
                .distinct()
            ).all()
        ]
    reports = [recompute_tenant_storage(t) for t in tenant_ids]
    total_reconciled = sum(r["reconciled"] for r in reports)
    if total_reconciled:
        log.warning("quota reconciliation corrected %d drifted row(s) across %d tenant(s)",
                    total_reconciled, len(reports))
    return {
        "tenants": len(reports),
        "reconciled": total_reconciled,
        "reports": reports,
    }


__all__ = [
    "QuotaExceeded",
    "QuotaInfo",
    "check_quota_before_write",
    "decrement_usage",
    "get_backend_usage_bytes",
    "get_effective_quotas",
    "get_quota_status",
    "get_quota_status_async",
    "get_tenant_usage_bytes",
    "recompute_tenant_storage",
    "recompute_used_bytes",
    "release_quota",
    "reserve_quota",
    "scheduled_reconcile_tenants",
]
