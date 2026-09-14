"""add auth sessions, lockout fields, and audit trail

Revision ID: c0d1e2f3a4b5
Revises: b3456789c0d1
Create Date: 2026-08-17

Implements the auth plan (§3.2 sessions table, §3.3 auth audit log, §4.1
account lockout): lockout/last-login columns on users, the `sessions` table
(rotating refresh tokens, hashed), and `auth_audit_log` (auth events).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, None] = "b3456789c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _bigint() -> sa.BigInteger:
    """BIGINT on MySQL, INTEGER on SQLite (matches the ORM pk() helper)."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    # Brute-force lockout (plan §4.1).
    op.add_column(
        "users",
        sa.Column("failed_attempts", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
    )
    op.add_column("users", sa.Column("locked_until", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("last_login", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("last_login_ip", sa.String(length=45), nullable=True))

    # Refresh-token sessions (plan §3.2).
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "user_id",
            _bigint(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("refresh_token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("family_id", sa.String(length=64), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("revoke_reason", sa.String(length=100), nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_family_id", "sessions", ["family_id"])

    # Auth event trail (plan §3.3).
    op.create_table(
        "auth_audit_log",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            _bigint(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
    )
    op.create_index("ix_auth_audit_log_user_id", "auth_audit_log", ["user_id"])
    op.create_index("ix_auth_audit_log_action", "auth_audit_log", ["action"])
    op.create_index("ix_auth_audit_log_timestamp", "auth_audit_log", ["timestamp"])


def downgrade() -> None:
    op.drop_index("ix_auth_audit_log_timestamp", table_name="auth_audit_log")
    op.drop_index("ix_auth_audit_log_action", table_name="auth_audit_log")
    op.drop_index("ix_auth_audit_log_user_id", table_name="auth_audit_log")
    op.drop_table("auth_audit_log")
    op.drop_index("ix_sessions_family_id", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_column("users", "last_login_ip")
    op.drop_column("users", "last_login")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_attempts")
