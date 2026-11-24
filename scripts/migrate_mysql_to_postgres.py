"""One-time data copy: the retired MySQL archive database -> PostgreSQL.

PAXS Archive runs on PostgreSQL only. This script moves the rows of an existing
MySQL (or MariaDB) PAXS database into a PostgreSQL database that already has
the schema (Alembic baseline ``pg0001_baseline`` — start the server once, or run
``alembic upgrade head``). DICOM files are not touched: only database rows move,
and ``instances.file_path`` keeps pointing at the same object store.

Safety
------
* Read-only on MySQL.
* The whole PostgreSQL load is ONE transaction: any error rolls everything
  back, so a failed run leaves the target exactly as it was and can be rerun.
* Refuses to run if any target table already has rows (no merge, no overwrite).
* ``--dry-run`` performs the complete load — every insert, every constraint
  and foreign-key check — and then rolls back.
* ``--check-source`` needs no PostgreSQL at all: it reads every MySQL row,
  applies the conversions and reports anything that would fail.

Conversions (MySQL -> PostgreSQL)
---------------------------------
* TINYINT(1) 0/1 -> boolean for the model's Boolean columns.
* Naive DATETIME -> timestamptz. ``--naive-tz auto`` (default): columns that
  only MySQL's ``NOW()`` default fills are read in the MySQL server's own UTC
  offset (detected and printed); columns the app writes are read as UTC.
  ``--naive-tz UTC`` / ``+HH:MM`` forces one zone for every column.
* NUL characters (``\\x00``) are removed from text — PostgreSQL text cannot hold
  them (MySQL could). Every removal is counted per column.
* Generated columns (``patients.tenant_key``, ``patient_identities.tenant_key``)
  are recomputed by PostgreSQL, never copied.
* A value longer than its PostgreSQL VARCHAR(n) stops the run with the table,
  column and row id — patient data is never truncated silently.
* Afterwards every BIGSERIAL sequence is moved past the copied ids, and the row
  counts of both sides are compared.
* ``server_settings.db_config`` (the saved *MySQL* connection form) is skipped.

Usage
-----
    pip install pymysql            # one-time: the MySQL reader (not an app dependency)

    # 1) look at the source only (no PostgreSQL needed)
    python scripts/migrate_mysql_to_postgres.py --check-source \\
        --source "mysql+pymysql://user:pass@127.0.0.1:3306/paxs_db"

    # 2) rehearse the full load, then roll back
    python scripts/migrate_mysql_to_postgres.py --dry-run --source "mysql+..."

    # 3) copy for real (target defaults to DATABASE_URL from this project's .env)
    python scripts/migrate_mysql_to_postgres.py --source "mysql+..."

``--source`` also accepts the old ``mysql+aiomysql://...?charset=utf8mb4`` value
from the previous .env, or the environment variable ``PAXS_MYSQL_SOURCE_URL``.
Passwords are never printed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import Boolean, DateTime, String, Table, create_engine, func, inspect, select, text  # noqa: E402
from sqlalchemy.engine import Connection, Engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

BATCH_ROWS = 1000
SKIP_ROWS = {("server_settings", "key", "db_config")}


class MigrationError(RuntimeError):
    """A condition that must stop the copy (raised inside the target transaction)."""


# --------------------------------------------------------------------- URLs ----
def source_sync_url(url: str) -> str:
    """Any MySQL/MariaDB SQLAlchemy URL -> mysql+pymysql, utf8mb4."""
    for prefix in ("mysql+aiomysql://", "mysql+pymysql://", "mysql://", "mariadb+pymysql://", "mariadb://"):
        if url.startswith(prefix):
            rest = url[len(prefix):]
            base, _, query = rest.partition("?")
            params = [p for p in query.split("&") if p and not p.startswith("charset=")]
            params.append("charset=utf8mb4")
            return "mysql+pymysql://" + base + "?" + "&".join(params)
    raise SystemExit("--source must be a MySQL URL (mysql+pymysql://user:pass@host:3306/db)")


def target_sync_url(url: str) -> str:
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    raise SystemExit("--target must be a PostgreSQL URL (postgresql+asyncpg://user:pass@host:5432/db)")


def redact(url: str) -> str:
    scheme, _, rest = url.partition("://")
    if "@" not in rest:
        return url
    cred, _, host = rest.rpartition("@")
    user = cred.split(":", 1)[0]
    return f"{scheme}://{user}:****@{host.split('?', 1)[0]}"


# -------------------------------------------------------------- conversion ----
def parse_tz(value: str) -> timezone:
    """'UTC', '+02:00', '-05:30' -> fixed-offset timezone."""
    if value.upper() in ("UTC", "Z", "+00:00"):
        return timezone.utc
    sign = -1 if value.startswith("-") else 1
    hours, _, minutes = value.lstrip("+-").partition(":")
    try:
        return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes or 0)))
    except ValueError as exc:
        raise SystemExit(f"--naive-tz must be UTC or +HH:MM, not {value!r}") from exc


class Converter:
    """Per-table row conversion with counters for the report."""

    def __init__(self, table: Table, source_columns: Iterable[str],
                 app_tz: timezone, db_clock_tz: timezone):
        self.table = table
        source = set(source_columns)
        model = [c for c in table.columns if c.computed is None]
        self.copied = [c for c in model if c.name in source]
        self.missing_in_source = [c.name for c in model if c.name not in source]
        self.extra_in_source = sorted(source - {c.name for c in table.columns})
        self.nul_removed: Counter[str] = Counter()
        self._bool = {c.name for c in self.copied if isinstance(c.type, Boolean)}
        self._dt = {c.name for c in self.copied if isinstance(c.type, DateTime)}
        self._dt_tz = {c.name for c in self.copied if isinstance(c.type, DateTime) and c.type.timezone}
        # Which clock wrote each naive DATETIME: columns filled only by a server
        # default (NOW()) used MySQL's clock; the rest were written by the app in UTC.
        self._zone = {
            c.name: db_clock_tz if (c.server_default is not None and c.default is None) else app_tz
            for c in self.copied if isinstance(c.type, DateTime)
        }
        self._len = {
            c.name: c.type.length for c in self.copied
            if isinstance(c.type, String) and getattr(c.type, "length", None)
        }
        self._pk = next((c.name for c in table.primary_key.columns), None)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.copied]

    def convert(self, row: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in self.column_names:
            value = row[name]
            if value is None:
                out[name] = None
                continue
            if name in self._bool:
                value = bool(value) if not isinstance(value, (bytes, bytearray)) else value != b"\x00"
            elif name in self._dt:
                if isinstance(value, datetime):
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=self._zone[name])
                    if name not in self._dt_tz:  # naive column: store UTC wall time
                        value = value.astimezone(timezone.utc).replace(tzinfo=None)
            elif isinstance(value, str):
                if "\x00" in value:
                    self.nul_removed[name] += value.count("\x00")
                    value = value.replace("\x00", "")
                limit = self._len.get(name)
                if limit and len(value) > limit:
                    raise MigrationError(
                        f"{self.table.name}.{name} (row {self._pk}={row.get(self._pk)!r}) holds "
                        f"{len(value)} characters but PostgreSQL allows {limit}; fix the source "
                        "value or widen the column in a new migration, then rerun"
                    )
            out[name] = value
        return out


# ----------------------------------------------------------------- reading ----
def source_rows(conn: Connection, table_name: str, columns: list[str], pk: str | None):
    cols = ", ".join(f"`{c}`" for c in columns)
    order = f" ORDER BY `{pk}`" if pk and pk in columns else ""
    result = conn.execution_options(stream_results=True, yield_per=BATCH_ROWS).execute(
        text(f"SELECT {cols} FROM `{table_name}`{order}")
    )
    for row in result.mappings():
        yield dict(row)


def mysql_clock_offset(conn: Connection) -> str:
    minutes = conn.execute(text("SELECT TIMESTAMPDIFF(MINUTE, UTC_TIMESTAMP(), NOW())")).scalar() or 0
    sign = "-" if minutes < 0 else "+"
    minutes = abs(int(minutes))
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def skip(table_name: str, row: dict[str, Any]) -> bool:
    return any(t == table_name and row.get(c) == v for t, c, v in SKIP_ROWS)


# ----------------------------------------------------------------- copying ----
def plan_tables(source: Engine, metadata) -> tuple[list[tuple[Table, Converter]], list[str], list[str]]:
    from app.db.models import Base  # noqa: F401 - registers every model on the metadata

    inspector = inspect(source)
    present = set(inspector.get_table_names())
    plan, absent = [], []
    for table in metadata.sorted_tables:  # parents first (no FK cycles in this schema)
        if table.name not in present:
            absent.append(table.name)
            continue
        cols = [c["name"] for c in inspector.get_columns(table.name)]
        plan.append((table, cols))
    unknown = sorted(present - set(metadata.tables) - {"alembic_version"})
    return plan, absent, unknown


def copy_table(src: Connection, dst: Connection | None, table: Table, conv: Converter) -> tuple[int, int]:
    pk = conv._pk
    copied = skipped = 0
    batch: list[dict[str, Any]] = []
    for row in source_rows(src, table.name, conv.column_names, pk):
        if skip(table.name, row):
            skipped += 1
            continue
        batch.append(conv.convert(row))
        if len(batch) >= BATCH_ROWS:
            if dst is not None:
                dst.execute(table.insert(), batch)
            copied += len(batch)
            batch = []
    if batch:
        if dst is not None:
            dst.execute(table.insert(), batch)
        copied += len(batch)
    return copied, skipped


def reset_sequences(dst: Connection, tables: list[Table]) -> int:
    moved = 0
    for table in tables:
        for col in table.primary_key.columns:
            seq = dst.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table.name, "c": col.name}
            ).scalar()
            if not seq:
                continue
            dst.execute(text(
                f'SELECT setval(:seq, COALESCE((SELECT MAX("{col.name}") FROM "{table.name}"), 1), '
                f'(SELECT MAX("{col.name}") FROM "{table.name}") IS NOT NULL)'
            ), {"seq": seq})
            moved += 1
    return moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--source", default=os.environ.get("PAXS_MYSQL_SOURCE_URL", ""),
                        help="MySQL URL of the old archive database")
    parser.add_argument("--target", default="", help="PostgreSQL URL (default: DATABASE_URL from .env)")
    parser.add_argument("--naive-tz", default="auto",
                        help="auto (default), UTC or +HH:MM: zone of MySQL's naive DATETIME values")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-source", action="store_true", help="read + validate MySQL only")
    mode.add_argument("--dry-run", action="store_true", help="full load into PostgreSQL, then roll back")
    args = parser.parse_args(argv)

    if not args.source:
        parser.error("--source is required (or set PAXS_MYSQL_SOURCE_URL)")
    src_url = source_sync_url(args.source)

    from app.db.models import Base

    metadata = Base.metadata
    started = time.perf_counter()
    source = create_engine(src_url, poolclass=NullPool)
    target = None
    try:
        with source.connect() as probe:
            server = probe.execute(text("SELECT VERSION()")).scalar()
            offset = mysql_clock_offset(probe)
        print(f"source : {redact(src_url)}  (MySQL {server}, server clock UTC{offset})")
        if args.naive_tz.lower() == "auto":
            app_tz, db_clock_tz = timezone.utc, parse_tz(offset)
            print(f"naive DATETIME: app-written columns as UTC, NOW()-default columns as UTC{offset}")
        else:
            app_tz = db_clock_tz = parse_tz(args.naive_tz)
            print(f"naive DATETIME: every column read as {args.naive_tz}")

        plan, absent, unknown = plan_tables(source, metadata)
        if absent:
            print(f"tables not in MySQL (left empty): {', '.join(absent)}")
        if unknown:
            print(f"MySQL tables with no model (not copied): {', '.join(unknown)}")

        if not args.check_source:
            from app.core.config import settings

            dst_url = target_sync_url(args.target or settings.DATABASE_URL)
            target = create_engine(dst_url, poolclass=NullPool)
            print(f"target : {redact(dst_url)}")

        converters = []
        with source.connect() as src:
            src.execute(text("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            src.execute(text("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"))
            for table, cols in plan:
                converters.append((table, Converter(table, cols, app_tz, db_clock_tz)))

            if target is None:
                totals = run_copy(src, None, converters)
            else:
                with target.connect() as dst:
                    trans = dst.begin()
                    try:
                        busy = [
                            t.name for t in metadata.sorted_tables
                            if dst.execute(select(func.count()).select_from(t)).scalar()
                        ]
                        if busy:
                            raise MigrationError(
                                "target is not empty (rows in: " + ", ".join(busy[:8])
                                + ("..." if len(busy) > 8 else "") + "); use a fresh database"
                            )
                        totals = run_copy(src, dst, converters)
                        moved = reset_sequences(dst, [t for t, _ in converters])
                        mismatch = [
                            (t.name, totals[t.name][0],
                             dst.execute(select(func.count()).select_from(t)).scalar())
                            for t, _ in converters
                        ]
                        mismatch = [m for m in mismatch if m[1] != m[2]]
                        if mismatch:
                            raise MigrationError(f"row count mismatch after load: {mismatch}")
                        print(f"sequences advanced: {moved}")
                        if args.dry_run:
                            trans.rollback()
                            print("DRY RUN: everything loaded and validated, then rolled back.")
                        else:
                            trans.commit()
                            print("COMMITTED.")
                    except BaseException:
                        trans.rollback()
                        raise
            src.execute(text("COMMIT"))
    except MigrationError as exc:
        print(f"\nSTOPPED - nothing was written: {exc}")
        return 2
    finally:
        source.dispose()
        if target is not None:
            target.dispose()
    print(f"done in {time.perf_counter() - started:.1f}s")
    return 0


def run_copy(src: Connection, dst: Connection | None, converters) -> dict[str, tuple[int, int]]:
    totals: dict[str, tuple[int, int]] = {}
    width = max((len(t.name) for t, _ in converters), default=10)
    for table, conv in converters:
        copied, skipped = copy_table(src, dst, table, conv)
        totals[table.name] = (copied, skipped)
        notes = []
        if skipped:
            notes.append(f"{skipped} skipped")
        if conv.missing_in_source:
            notes.append("not in MySQL (defaults): " + ", ".join(conv.missing_in_source))
        if conv.extra_in_source:
            notes.append("MySQL-only columns dropped: " + ", ".join(conv.extra_in_source))
        if conv.nul_removed:
            notes.append("NUL chars removed: " + ", ".join(f"{k} x{v}" for k, v in conv.nul_removed.items()))
        print(f"  {table.name:<{width}} {copied:>9} rows" + (f"   ({'; '.join(notes)})" if notes else ""))
    print(f"total rows: {sum(c for c, _ in totals.values())}")
    return totals


if __name__ == "__main__":
    raise SystemExit(main())
