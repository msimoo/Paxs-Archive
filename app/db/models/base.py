"""Shared SQLAlchemy 2.0 model primitives.

The archive runs on PostgreSQL only. Primary keys are 64-bit ``BIGSERIAL``
columns.  Timestamp values are represented as UTC-aware datetimes at
the ORM boundary and are populated by the database clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedColumn, mapped_column


class Base(DeclarativeBase):
    """Declarative root for every PAXS SQLAlchemy model."""


# 64-bit auto-incrementing primary key (PostgreSQL BIGSERIAL).
AutoBigIntPK = BigInteger()


class TimestampMixin:
    """Provide database-generated UTC creation and modification timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantScopedMixin:
    """Provide a required tenant scope with a safe default tenant id."""

    tenant_id: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        index=True,
    )


def pk() -> MappedColumn[Any]:
    """Return a dialect-safe auto-incrementing integer primary-key column.

    Lives here rather than in the package ``__init__`` so a domain module can
    declare its own tables without importing the package facade (which would be
    a circular import — FA-3).
    """
    return mapped_column(AutoBigIntPK, primary_key=True, autoincrement=True)


__all__ = ["AutoBigIntPK", "Base", "TenantScopedMixin", "TimestampMixin", "pk"]
