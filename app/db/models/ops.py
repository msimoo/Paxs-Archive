"""Administrative and operational entities.

Everything the archive needs to run itself rather than to describe an exam:
who may sign in and under which tenant, what is written down about what they
did, where objects physically live, how much room each scope may use, and where
incoming studies are forwarded. The clinical record itself is in
:mod:`app.db.models.study` and :mod:`app.db.models.clinical`.

Three things here are load-bearing and must not be "simplified":

* **``users.role`` is a ``String(16)`` validated against
  :data:`app.core.roles.ROLES`, not a database ``ENUM``.** The vocabulary is
  ``ADMIN`` / ``RADIOLOGIST`` / ``PHYSICIAN`` / ``TECHNOLOGIST``. A native
  ``ENUM`` does not exist on SQLite and would make every future role a
  migration. Note the spelling: **``TECHNOLOGIST``, not ``TECHNICIAN``** — a
  role outside ``ROLES`` is accepted by generic authentication but rejected by
  ``normalize_role``, so the same token ends up simultaneously "privileged
  enough to read" and "too privileged to act" (the bug the roles module
  docstring records).
* **``users.tenant_id`` is nullable.** ``NULL`` is the legacy/unscoped scope
  (pre-VNA accounts), which ``tenant_service.scope_from_user`` still resolves
  explicitly. Making it NOT NULL would silently reclassify every legacy account
  into one tenant.
* **Quota accounting lives on ``storage_quotas``, not on ``tenants``.**
  ``used_bytes`` there is the reservation counter that makes check-and-reserve a
  single atomic UPDATE (H08). A second counter on ``tenants`` would be a copy
  that can disagree with the one the write path actually trusts.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import false, true
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import AutoBigIntPK, Base, TimestampMixin, pk

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    # Defined in the package `__init__`; importing it at runtime here would be a
    # circular import. SQLAlchemy resolves the string forward references below
    # from the shared registry at mapper-configuration time.
    from app.db.models import DicomNode

__all__ = [
    "AuditLog",
    "AuthAuditLog",
    "AuthSource",
    "OrphanedObject",
    "RoutingQueueItem",
    "RoutingRule",
    "ServerSetting",
    "Session",
    "StorageBackendConfig",
    "StorageNode",
    "StorageQuota",
    "Tenant",
    "TenantAeTitle",
    "User",
]


class Tenant(Base, TimestampMixin):
    """A VNA (Vendor Neutral Archive) tenant — an institution, department or
    customer whose imaging data is isolated from every other tenant.

    Tenant isolation is applied at three boundaries:
      * ingest — instances stored via a DICOM AE title mapped to this tenant
        (or via STOW by a user assigned to it) are tagged with tenant_id;
      * query  — C-FIND / C-MOVE / C-GET from a mapped AE title and REST/QIDO
        browsing by an assigned user only see that tenant's studies;
      * admin  — tenant admins manage mappings; unmapped traffic and
        non-assigned users keep the legacy "unscoped" scope (tenant_id NULL),
        so pre-VNA archives keep working unchanged.
    """

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # Short stable slug used in URLs / labels, e.g. "rad-a".
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )


class TenantAeTitle(Base):
    """Maps a DICOM AE title to a tenant — the heart of "isolate per AE".

    Any association whose calling AE title is listed here (C-STORE ingest,
    C-FIND / C-MOVE / C-GET queries) operates inside that tenant's scope.
    Unmapped AE titles stay in the legacy unscoped scope.
    """

    __tablename__ = "tenant_ae_titles"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ae_title: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class AuthSource(StrEnum):
    """Trusted namespace that owns an account's credentials and permissions."""

    LOCAL = "LOCAL"
    LDAP = "LDAP"
    OIDC = "OIDC"


class User(Base):
    """An archive account.

    The password is only ever stored as a PBKDF2 digest in ``password_hash``
    (``app/core/security.hash_password``) — never a plaintext value, and never a
    reversible encoding. ``role`` is validated against ``app.core.roles.ROLES``
    by every writer; see the module docstring for why it is not an ``ENUM``.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Identity namespace is immutable across login providers: an LDAP/OIDC login
    # must never claim or rewrite a LOCAL account with the same username.
    auth_source: Mapped[AuthSource] = mapped_column(
        SAEnum(
            AuthSource,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
            create_constraint=True,
            name="ck_users_auth_source",
            length=6,
        ),
        default=AuthSource.LOCAL,
        server_default=text("'LOCAL'"),
        nullable=False,
    )
    # PBKDF2-HMAC-SHA256 digest, formatted "pbkdf2_sha256$<iters>$<salt>$<dk>".
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(192))
    # Validated against app.core.roles.ROLES.
    role: Mapped[str] = mapped_column(String(16), default="PHYSICIAN", nullable=False)
    # Disabled accounts can no longer log in or hold API tokens (checked in
    # get_current_user). Defaults to active for every pre-existing row.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    # B-4: set when an administrator account is created by automation
    # (db/init seeding or the setup-token bootstrap) with a generated password.
    # Login still succeeds, but the API reports the flag so the client forces a
    # password change before anything else, and the flag clears when the
    # password is changed. Existing rows default to False.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # Brute-force lockout (auth plan §4.1): failed_attempts increments on bad
    # logins; when locked_until is in the future the account is locked and
    # login is rejected with the remaining minutes. Both reset on success.
    failed_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(45))
    # VNA: the tenant this user belongs to. NULL = legacy scope (unscoped data),
    # which the resolver still matches explicitly — NOT a missing value.
    # Admins see every tenant regardless of this value.
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class Session(Base):
    """A login session for refresh-token rotation (auth plan §3.2).

    One row per refresh token: the token itself is only ever stored as a
    SHA-256 hash, and every use rotates it (new hash, same family_id). If a
    token is ever presented that does not match the stored hash, it has been
    reused after rotation — a theft signal — and the entire session family is
    revoked (plan §4.3).
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUIDv4
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )  # SHA-256 hex of the opaque refresh token
    family_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_revoked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    revoke_reason: Mapped[str | None] = mapped_column(String(100))


class AuthAuditLog(Base):
    """Auth event trail (auth plan §3.3): login/refresh/theft/lockout/roles.

    Written on login_success, login_failed, account_locked, logout,
    token_refresh, token_theft_detected, password_change, role_changed.
    details holds small JSON context (attempt counts, reasons) — never PHI.
    """

    __tablename__ = "auth_audit_log"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    details: Mapped[str | None] = mapped_column(Text)  # JSON string, small


class AuditLog(Base):
    """DICOM activity and administrator configuration audit trail.

    DICOM actions record **UIDs**, not patient identifiers. Configuration
    changes use dedicated nullable columns for actor ID, setting key, and
    serialized old/new values; they never use a free-text details field.
    ``performed_by`` holds the authenticated username, not a client-supplied
    name (AGENTS.md N-2).

    ``action`` is ``String(32)`` — every action constant must fit, or MySQL
    truncates silently (SQLite would not complain, which is how that bug class
    ships).
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sop_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    study_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    sha256_hash: Mapped[str | None] = mapped_column(String(64))
    # STORED / RETRIEVED / COMPRESSED / DELETED / SHARED / SETTING_OVERRIDE
    # (max 32 chars). `CONFIG_UPDATED` is the pre-2026-10-21 spelling of the
    # settings action; old rows still carry it.
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    performed_by: Mapped[str | None] = mapped_column(String(128))
    config_key: Mapped[str | None] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    performed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )


class RoutingRule(Base, TimestampMixin):
    """An auto-routing rule evaluated when new DICOM studies arrive.

    If incoming DICOM study metadata matches the specified filters (modality,
    body_part, source_aet, study_description_pattern), PAXS automatically
    dispatches C-STORE requests to the destination DICOM node.
    """

    __tablename__ = "routing_rules"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    destination_node_id: Mapped[int] = mapped_column(
        ForeignKey("dicom_nodes.id", ondelete="CASCADE"), nullable=False
    )
    modality: Mapped[str | None] = mapped_column(String(16))
    body_part_examined: Mapped[str | None] = mapped_column(String(64))
    # The *calling* AE title the rule matches. Named `source_aet` because a
    # `ae_title` column already means "this node's own AE" on `dicom_nodes`.
    source_aet: Mapped[str | None] = mapped_column(String(64))
    study_description_pattern: Mapped[str | None] = mapped_column(String(255))
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    destination_node: Mapped[DicomNode] = relationship()


class RoutingQueueItem(Base, TimestampMixin):
    """A durable auto-routing dispatch job (Phase C1 send queue).

    When a routing rule matches a study, PAXS enqueues one item per
    (study, destination node) instead of dispatching fire-and-forget. The
    background worker claims PENDING items, C-STORE's the study to the
    destination node and marks COMPLETED; failed sends are retried with
    exponential backoff up to ``max_attempts``, then flagged FAILED for a
    human (admin panel) to review and retry.

    status: PENDING (due) / SENDING (claimed by worker) / COMPLETED /
    FAILED (exhausted retries).
    """

    __tablename__ = "routing_queue"
    __table_args__ = (
        Index("ix_routing_queue_tenant_status_id", "tenant_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    study_instance_uid: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    # NULL is retained for historical/ambiguous work and remains ADMIN-only.
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True
    )
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("routing_rules.id", ondelete="SET NULL"), nullable=True
    )
    node_id: Mapped[int] = mapped_column(
        ForeignKey("dicom_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", server_default="PENDING", nullable=False, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    rule: Mapped[RoutingRule | None] = relationship()
    node: Mapped[DicomNode] = relationship()


class StorageBackendConfig(Base):
    """A named storage backend for DICOM objects (multi-storage).

    ``kind`` is ``local`` (filesystem under ``root_path``) or ``s3`` (an
    S3-compatible object store — AWS S3 / MinIO). At most one row is the
    default; when no default row exists, the environment-configured backend
    (``STORAGE_BACKEND`` / ``S3_*``) remains the active default.

    ``instances.storage_backend`` records which backend holds each object;
    NULL means the environment default. Studies can be moved between backends
    with the Admin → Storage → Move action (cross-backend copy + delete).

    The table is ``storage_backends``; :data:`StorageNode` is an alias for this
    class so the "storage node" vocabulary used in the operations docs resolves
    to the same mapper.
    """

    __tablename__ = "storage_backends"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # local | s3
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    # --- local ---
    root_path: Mapped[str | None] = mapped_column(String(512))
    # --- s3 ---
    endpoint_url: Mapped[str | None] = mapped_column(String(512))
    region: Mapped[str | None] = mapped_column(String(64))
    bucket: Mapped[str | None] = mapped_column(String(255))
    access_key: Mapped[str | None] = mapped_column(String(255))
    secret_key: Mapped[str | None] = mapped_column(String(255))
    presign_expires: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


#: The operations vocabulary for a storage backend. Same mapper, same table —
#: ``StorageNode`` is an alias, never a second declaration (two classes on one
#: ``__tablename__`` is a hard SQLAlchemy error, and a second table would fork
#: the source of truth for which backend holds an object).
StorageNode = StorageBackendConfig


class StorageQuota(Base, TimestampMixin):
    """Storage quota limits for tenants and/or storage backends.

    Enforces maximum storage consumption at two independent granularities:
      * tenant-scoped: limits total bytes across **all** backends for a tenant
      * backend-scoped: limits total bytes on a specific backend (all tenants)

    Both scopes are always evaluated for a write and the stricter one that would
    be crossed refuses it — they are **never** ANDed (C-17). A quota of 0 or NULL
    means unlimited. The ``warning_threshold_pct`` triggers advisory alerts
    (logged and exposed via API) when usage crosses the percentage but does not
    block writes.
    """

    __tablename__ = "storage_quotas"

    id: Mapped[int] = mapped_column(
        AutoBigIntPK, primary_key=True, autoincrement=True
    )
    # Scope: exactly one of tenant_id or storage_backend_name must be set
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    storage_backend_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    max_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # H08: reserved+committed bytes for this scope. A denormalised counter is
    # what makes check-and-reserve a single atomic UPDATE, closing the
    # check-then-act race. `recompute_used_bytes()` reconciles it if it drifts.
    used_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    warning_threshold_pct: Mapped[int] = mapped_column(
        Integer, default=80, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )

    tenant: Mapped[Tenant | None] = relationship()

    @classmethod
    def scope_matches(cls, tenant_id: int | None, backend_name: str | None):
        """SQL predicate selecting every quota that governs this write (C-17).

        The two scopes are **ORed**: a tenant quota applies to any write by that
        tenant regardless of backend, and a backend quota applies to any write
        into that backend regardless of tenant. Filtering with ``AND`` was the
        C-17 defect — it narrowed a tenant-wide quota to a single backend and
        omitted the tenant row entirely when the quota named no backend.

        A quota row is exactly one scope (``tenant_id`` XOR
        ``storage_backend_name``; the API enforces that), so an OR over the two
        membership tests can never match a foreign scope: the tenant arm matches
        only tenant rows, the backend arm only backend rows.
        """
        from sqlalchemy import or_

        clauses = []
        if tenant_id is not None:
            clauses.append(cls.tenant_id == tenant_id)
        if backend_name is not None:
            clauses.append(cls.storage_backend_name == backend_name)
        if not clauses:
            # No scope context: only unscoped/global rows could apply, and none
            # are defined today. Return a never-true predicate so a caller
            # without tenant *or* backend context enforces nothing.
            return cls.id.is_(None)
        return or_(*clauses) if len(clauses) > 1 else clauses[0]


# --------------------------------------------------------------------------- #
# FA-3: operational entities moved out of the package ``__init__``.
# --------------------------------------------------------------------------- #
class OrphanedObject(Base, TimestampMixin):
    """A stored object whose deletion failed — the operator-visible retry queue.

    A-14 / A-15 (FD-2). Purging a study is two operations that can disagree: the
    metadata row and the object on disk. When the object delete fails, deleting
    the row anyway leaves PHI on the filesystem with nothing pointing at it —
    invisible to the application and impossible to find later without a full
    storage sweep. So the failure is recorded here, the row is kept, and an
    operator (or ``lifecycle_service.retry_orphaned_deletions``) can retry.

    Deliberately tenant-scoped: a leaked object needs the same isolation as a
    live one. ``reason`` is PHI-free by construction — the exception type, never
    a filesystem path or a patient value (AGENTS.md N-2).
    """

    __tablename__ = "orphaned_objects"

    id: Mapped[int] = pk()
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The object that could not be removed.
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_backend: Mapped[str | None] = mapped_column(String(64))
    # What it belonged to, so an operator can judge what is at stake.
    study_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    sop_instance_uid: Mapped[str | None] = mapped_column(String(128))
    # Why the object is here: "purge" | "tier_source" | "tier_rollback".
    operation: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False, index=True
    )

    __table_args__ = (
        # The operator's query is always "what is still outstanding?".
        Index("ix_orphaned_objects_open", "resolved", "operation"),
    )


class ServerSetting(Base):
    """A runtime-editable server configuration override (admin Settings UI).

    The Settings view in the dashboard reads the live value of every editable
    key as ``DB override (this table) ?? env default (app.core.config.settings)``
    and writes changes here. On startup PAXS applies all stored overrides to
    the in-process ``settings`` object, so persisted changes survive restarts
    for the keys that are consumed at boot (AE title, ports, toggles). Keys
    whose consumers bind at import time (routers, listeners) carry a
    "restart required" hint in the UI registry.
    """

    __tablename__ = "server_settings"

    id: Mapped[int] = pk()
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
