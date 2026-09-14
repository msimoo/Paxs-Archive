"""add server_settings

Revision ID: x1234567y8z9
Revises: w1234567x8y9
Create Date: 2026-08-06

Adds the `server_settings` table backing the editable admin Settings UI: a
key/value store of configuration overrides layered on top of the env defaults
in app/core/config.py. Changes persist across restarts and are applied to the
in-process settings object at boot and on every PATCH.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "x1234567y8z9"
down_revision: Union[str, None] = "w1234567x8y9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _bigint() -> sa.BigInteger:
    """BIGINT on MySQL, INTEGER on SQLite (matches the ORM pk() helper)."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "server_settings",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(length=64), nullable=False, unique=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_server_settings_key", "server_settings", ["key"])


def downgrade() -> None:
    op.drop_index("ix_server_settings_key", table_name="server_settings")
    op.drop_table("server_settings")
