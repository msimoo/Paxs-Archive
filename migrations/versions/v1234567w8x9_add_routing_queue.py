"""add routing_queue

Revision ID: v1234567w8x9
Revises: u1234567v8w9
Create Date: 2026-08-06

Adds the durable auto-routing send queue (Phase C1): failed dispatches are
retried with backoff instead of being lost.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "v1234567w8x9"
down_revision: Union[str, None] = "u1234567v8w9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _bigint() -> sa.BigInteger:
    """BIGINT on MySQL, INTEGER on SQLite (matches the ORM pk() helper)."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "routing_queue",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "study_instance_uid",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "rule_id",
            _bigint(),
            sa.ForeignKey("routing_rules.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "node_id",
            _bigint(),
            sa.ForeignKey("dicom_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_routing_queue_study_instance_uid", "routing_queue", ["study_instance_uid"]
    )
    op.create_index("ix_routing_queue_node_id", "routing_queue", ["node_id"])
    op.create_index("ix_routing_queue_status", "routing_queue", ["status"])
    op.create_index("ix_routing_queue_next_retry_at", "routing_queue", ["next_retry_at"])


def downgrade() -> None:
    op.drop_index("ix_routing_queue_next_retry_at", table_name="routing_queue")
    op.drop_index("ix_routing_queue_status", table_name="routing_queue")
    op.drop_index("ix_routing_queue_node_id", table_name="routing_queue")
    op.drop_index("ix_routing_queue_study_instance_uid", table_name="routing_queue")
    op.drop_table("routing_queue")
