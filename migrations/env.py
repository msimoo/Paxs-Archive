"""Alembic migration environment.

Reads the DATABASE_URL from the app settings (so .env works as usual) and
derives the sync driver URL Alembic needs (mysql+pymysql / sqlite).
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.db.models import Base


def to_sync_url(url: str) -> str:
    """Alembic needs a sync driver URL (pymysql / plain sqlite).
    Defined here (not in app.db.session) so importing this module never
    constructs the app's engines."""
    if url.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + url[len("sqlite+aiosqlite://"):]
    if url.startswith("mysql+aiomysql://"):
        return "mysql+pymysql://" + url[len("mysql+aiomysql://"):]
    return url


config = context.config
# H12: a caller may pass the target URL explicitly (config.attributes), so a
# migration never depends on — or mutates — the process-global settings.
_override = None
try:
    _override = config.attributes.get("paxs_migration_url")
except Exception:  # noqa: BLE001 - attributes is optional
    _override = None
config.set_main_option("sqlalchemy.url", to_sync_url(_override or settings.DATABASE_URL))

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

logger = logging.getLogger("paxs.migrations")
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to the DB)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
