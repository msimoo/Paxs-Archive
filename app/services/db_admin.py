"""Database administration — the engine behind Settings → Database.

Everything the dashboard's Database tab needs, kept out of the API layer:

* connection-string parsing / masking / building (PostgreSQL only)
* persisting a configured backend to ``.env`` (DATABASE_URL) and to a
  ``server_settings`` row (``db_config``) so Test / Initialize can reuse it
* Alembic state (current vs. head revision) and running migrations against an
  arbitrary target URL (a fresh install, or the configured PostgreSQL)
* PostgreSQL connection probes and ``CREATE DATABASE`` for new installations
* database backups — ``pg_dump`` (custom format) with a pure-SQLAlchemy
  export fallback
* seeding the first ADMIN account on a brand-new installation

All functions are synchronous; endpoints call them via ``asyncio.to_thread``.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.security import hash_password
from app.db.models import AuthSource, ServerSetting, User
from app.db.session import SyncSessionLocal, to_sync_url

log = logging.getLogger("paxs.db_admin")


class MigrationDriftError(RuntimeError):
    """Raised when a pending migration conflicts with existing schema objects."""

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
DB_CONFIG_KEY = "db_config"
BACKUP_SCHEDULE_KEY = "backup_schedule"
_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# --------------------------------------------------------------- parsing ----
_PG_PREFIXES = ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql://", "postgres://")
DEFAULT_PG_PORT = 5432


def parse_db_url(url: str) -> dict:
    """Split a PostgreSQL SQLAlchemy URL into its parts."""
    for prefix in _PG_PREFIXES:
        if url.startswith(prefix):
            rest = url[len(prefix):]
            if "@" in rest:
                cred, _, hostport = rest.rpartition("@")
            else:
                cred, hostport = "", rest
            user, _, password = cred.partition(":")
            hostport, _, database = hostport.partition("/")
            # Strip URL query params (e.g. ?ssl=require) — they belong to the
            # driver, not the database name.
            database = database.split("?", 1)[0]
            host, _, port = hostport.partition(":")
            return {
                "backend": "postgresql",
                "user": urllib.parse.unquote(user),
                "password": urllib.parse.unquote(password),
                "host": host or "127.0.0.1",
                "port": int(port) if port.isdigit() else DEFAULT_PG_PORT,
                "database": urllib.parse.unquote(database),
            }
    return {"backend": url.split("://")[0], "url": url}


def build_postgres_url(cfg: dict) -> str:
    """Assemble a postgresql+asyncpg URL from {host, port, user, password, name}."""
    user = urllib.parse.quote_plus(cfg["user"])
    password = urllib.parse.quote_plus(cfg.get("password") or "")
    port = int(cfg.get("port") or DEFAULT_PG_PORT)
    name = cfg.get("name") or ""
    return f"postgresql+asyncpg://{user}:{password}@{cfg['host']}:{port}/{name}"


def masked_url(url: str) -> str:
    """Connection string with the password hidden."""
    p = parse_db_url(url)
    if p["backend"] != "postgresql":
        return f"{p['backend']}://****"
    cred = f"{p['user']}:{'*' * max(4, len(p.get('password') or ''))}" if p.get("user") else "*" * 4
    return f"postgresql://{cred}@{p['host']}:{p['port']}/{p['database']}"


def _connect_kwargs(p: dict, database: str | None = None) -> dict:
    """psycopg connection keywords from parsed URL parts (no URL string to leak)."""
    return {
        "host": p.get("host") or "127.0.0.1",
        "port": int(p.get("port") or DEFAULT_PG_PORT),
        "user": p.get("user") or "",
        "password": p.get("password") or "",
        "dbname": database if database is not None else (p.get("database") or "postgres"),
    }


# -------------------------------------------------- persisted configuration ----
def saved_config() -> dict | None:
    """The operator's last saved backend config (server_settings.db_config)."""
    with SyncSessionLocal() as session:
        row = session.execute(
            select(ServerSetting).where(ServerSetting.key == DB_CONFIG_KEY)
        ).scalar_one_or_none()
    if row is None:
        return None
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return None


def save_config(cfg: dict) -> None:
    """Persist a backend configuration (server_settings row + .env).

    Only non-secret fields are stored in the database; the password lives
    exclusively in the .env DATABASE_URL (see saved_password), so credentials
    never end up in the archive's own database or its backups.
    """
    stored = {k: cfg.get(k) for k in ("backend", "host", "port", "user", "name")}
    stored["has_password"] = bool(cfg.get("password"))
    stored["saved_at"] = datetime.now().isoformat(timespec="seconds")
    with SyncSessionLocal() as session:
        row = session.execute(
            select(ServerSetting).where(ServerSetting.key == DB_CONFIG_KEY)
        ).scalar_one_or_none()
        payload = json.dumps(stored, ensure_ascii=False)
        if row is None:
            session.add(ServerSetting(key=DB_CONFIG_KEY, value=payload))
        else:
            row.value = payload
        session.commit()


def saved_password() -> str:
    """The saved PostgreSQL password, read back from the .env DATABASE_URL."""
    try:
        url = read_env().get("DATABASE_URL", "")
        if url.startswith(_PG_PREFIXES):
            return parse_db_url(url).get("password") or ""
    except Exception as exc:  # Defensive fallback; the exception may contain DATABASE_URL.
        log.warning(
            "db_saved_password_read_failed error_type=%s", type(exc).__name__
        )
    return ""


def clear_saved_config() -> None:
    with SyncSessionLocal() as session:
        row = session.execute(
            select(ServerSetting).where(ServerSetting.key == DB_CONFIG_KEY)
        ).scalar_one_or_none()
        if row is not None:
            session.delete(row)
            session.commit()


def resolve_url(cfg: dict | None = None) -> str:
    """Turn a (possibly partial) config dict into a DATABASE_URL."""
    if cfg:
        if cfg.get("host") and cfg.get("name"):
            return build_postgres_url(cfg)
    return settings.DATABASE_URL


def env_file_path() -> Path:
    return Path(settings.ENV_FILE_PATH or ".env")


def read_env() -> dict[str, str]:
    path = env_file_path()
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip("\"'")
    return data


def write_env_url(url: str) -> Path:
    """Rewrite DATABASE_URL inside the .env file (preserving other keys)."""
    path = env_file_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new_line = f'DATABASE_URL="{url}"'
    found = False
    for i, raw in enumerate(lines):
        if raw.strip().startswith("DATABASE_URL="):
            lines[i] = new_line
            found = True
            break
    if not found:
        lines.append(new_line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


# ------------------------------------------------------------- alembic ------
def head_revision() -> str | None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    try:
        sd = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
        return sd.get_current_head()
    except Exception as exc:  # Defensive discovery fallback; omit local config details.
        log.warning("db_alembic_head_unavailable error_type=%s", type(exc).__name__)
        return None


def current_revision(url: str) -> str | None:
    """The revision the target database is actually at (None = no version)."""
    from alembic.runtime.migration import MigrationContext

    eng = create_engine(to_sync_url(url), poolclass=NullPool)
    try:
        with eng.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    except Exception:
        return None
    finally:
        eng.dispose()


def revision_status(database_url: str) -> tuple[set[str], set[str]]:
    """Return ``(current database revisions, code migration heads)``."""
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    cfg = Config(str(ALEMBIC_INI))
    heads = set(ScriptDirectory.from_config(cfg).get_heads())
    engine = create_engine(to_sync_url(database_url), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            current = set(MigrationContext.configure(connection).get_current_heads())
    finally:
        engine.dispose()
    return current, heads


def _is_already_exists(exc: Exception) -> bool:
    """True when a migration failed because the schema is already present
    (PostgreSQL ``relation "x" already exists`` / ``column "y" ... already exists``)."""
    msg = str(exc).lower()
    return "already exists" in msg or msg.startswith("duplicate ")


def schema_drift(url: str) -> list[str]:
    """Objects the models declare but the database lacks (H11).

    The parity check that gates stamping: if this is non-empty the schema is
    *incomplete*, so declaring it "at head" would strand those objects forever.
    """
    from sqlalchemy import create_engine, inspect

    from app.db.models import Base

    engine = create_engine(to_sync_url(url), poolclass=NullPool)
    try:
        inspector = inspect(engine)
        present = set(inspector.get_table_names())
        missing: list[str] = []
        for table_name, table in Base.metadata.tables.items():
            if table_name not in present:
                missing.append(f"table {table_name}")
                continue
            columns = {c["name"] for c in inspector.get_columns(table_name)}
            for column in table.columns:
                if column.name not in columns:
                    missing.append(f"column {table_name}.{column.name}")
        return missing
    finally:
        engine.dispose()


def _migration_config(url: str):
    """An Alembic Config bound to *url* without touching global settings (H12)."""
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    # env.py prefers this over settings.DATABASE_URL.
    cfg.attributes["paxs_migration_url"] = url
    return cfg


_MIGRATION_LOCK_KEY = 0x50415853  # "PAXS" — one cluster-wide advisory lock id
_MIGRATION_LOCK_WAIT_SECONDS = 30


def _advisory_lock(engine):
    """Hold a PostgreSQL advisory lock for the whole migration run.

    Returns the connection that owns the lock (session-level locks live as long
    as their connection), or None when it could not be taken in time — the
    caller then proceeds, and Alembic's transactional DDL still keeps a
    concurrent run from half-applying a revision.
    """
    try:
        conn = engine.connect()
    except Exception as exc:  # noqa: BLE001 - the upgrade itself will surface the error
        log.warning("db_advisory_lock_unavailable error_type=%s", type(exc).__name__)
        return None
    deadline = time.monotonic() + _MIGRATION_LOCK_WAIT_SECONDS
    try:
        while True:
            got = conn.exec_driver_sql(
                f"SELECT pg_try_advisory_lock({_MIGRATION_LOCK_KEY})"
            ).scalar()
            conn.commit()
            if got:
                return conn
            if time.monotonic() >= deadline:
                log.warning("db_advisory_lock_timeout seconds=%d", _MIGRATION_LOCK_WAIT_SECONDS)
                conn.close()
                return None
            time.sleep(0.5)
    except Exception as exc:  # noqa: BLE001 - documented fallback, no driver details
        log.warning("db_advisory_lock_unavailable error_type=%s", type(exc).__name__)
        conn.close()
        return None


def _release_advisory_lock(conn) -> None:
    if conn is None:
        return
    try:
        conn.exec_driver_sql(f"SELECT pg_advisory_unlock({_MIGRATION_LOCK_KEY})")
        conn.commit()
    except Exception as exc:  # Release failure must not mask the migration result.
        log.warning("db_advisory_lock_release_failed error_type=%s", type(exc).__name__)
    finally:
        conn.close()


def run_migrations(url: str, stamp_on_exists: bool = False) -> tuple[list[str], bool]:
    """Run every pending migration on *url* up to head.

    H11: Alembic is the single schema authority and there is **no automatic
    stamping**. If the target already contains an object a pending migration
    would create, the migration fails with the drift report instead of being
    declared "at head" — a stamp on a partially-migrated database would strand
    the remaining objects permanently.

    H12: the target URL is passed to Alembic explicitly; the process-global
    ``settings.DATABASE_URL`` is never mutated.

    Returns ``(log_lines, stamped)``. ``stamped`` is always False here; stamping
    is a separate, parity-checked operator action (:func:`stamp_head`).
    """
    from alembic import command

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    mig_logger = logging.getLogger("alembic.runtime.migration")
    mig_logger.addHandler(handler)
    cfg = _migration_config(url)
    engine = create_engine(to_sync_url(url), poolclass=NullPool)
    lock_conn = None
    try:
        lock_conn = _advisory_lock(engine)
        command.upgrade(cfg, "head")
        return [ln for ln in buf.getvalue().splitlines() if ln.strip()], False
    except Exception as exc:
        if not _is_already_exists(exc):
            raise
        drift = schema_drift(url)
        detail = ", ".join(drift[:10]) if drift else "no missing objects detected"
        raise MigrationDriftError(
            "Migration stopped: the target already contains objects a pending "
            f"migration would create, so its schema state is ambiguous. "
            f"Missing per the models: {detail}. "
            "Reconcile manually (or run the explicit stamp procedure, which "
            "requires a clean parity check) — this is never done automatically."
        ) from exc
    finally:
        _release_advisory_lock(lock_conn)
        engine.dispose()
        mig_logger.removeHandler(handler)


def stamp_head(url: str) -> dict:
    """Explicitly mark *url* as being at head — only when the schema is complete.

    H11: stamping is an operator action that must pass the schema parity check
    first. An incomplete schema is refused with the drift report, because a
    stamp would otherwise declare missing objects as already applied.
    """
    from alembic import command

    drift = schema_drift(url)
    if drift:
        return {
            "ok": False,
            "error": "Schema is incomplete — refusing to stamp.",
            "missing": drift,
            "missing_count": len(drift),
        }
    cfg = _migration_config(url)
    command.stamp(cfg, "head")
    return {"ok": True, "revision": current_revision(url), "missing": []}


def ensure_database(url: str) -> bool:
    """Create the PostgreSQL database if it does not exist (UTF8 encoding)."""
    p = parse_db_url(url)
    if p["backend"] != "postgresql" or not p["database"]:
        return False
    if not _NAME_RE.match(p["database"]):
        raise ValueError("Invalid database name (letters, digits, underscore only)")
    import psycopg

    with psycopg.connect(**_connect_kwargs(p, "postgres"), connect_timeout=5, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (p["database"],)
        ).fetchone()
        if exists:
            return False
        # CREATE DATABASE cannot run inside a transaction; the name is validated
        # above (letters, digits, underscore) and quoted as an identifier.
        conn.execute(
            f"CREATE DATABASE \"{p['database']}\" ENCODING 'UTF8' TEMPLATE template0"
        )
    return True


# ------------------------------------------------------------ connectivity ----
def test_postgres_connection(cfg: dict) -> dict:
    """Probe a PostgreSQL server (connect_timeout=3). No state is written."""
    import psycopg

    t0 = time.perf_counter()
    p = {
        "host": cfg.get("host") or "127.0.0.1",
        "port": int(cfg.get("port") or DEFAULT_PG_PORT),
        "user": cfg.get("user") or "",
        "password": cfg.get("password") or "",
    }
    try:
        conn = psycopg.connect(**_connect_kwargs(p, "postgres"), connect_timeout=3)
    except Exception:
        # Driver messages can echo the submitted username, password, or URL.
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "error": "Database connection failed",
        }
    try:
        version = str(conn.execute("SHOW server_version").fetchone()[0])
        name = cfg.get("name") or ""
        db_exists = bool(name) and conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone() is not None
    finally:
        conn.close()
    return {
        "ok": True,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "version": f"PostgreSQL {version}",
        "database": cfg.get("name") or "",
        "database_exists": db_exists,
    }


# ---------------------------------------------------------------- backup -----
def backup_dir() -> Path:
    path = Path(settings.BACKUP_DIR or "./backups")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _dump_pg_dump(url: str, dest: Path) -> bool:
    """``pg_dump`` custom-format archive (restore with ``pg_restore``)."""
    if not shutil.which("pg_dump"):
        return False
    p = parse_db_url(url)
    env = {**os.environ, "PGPASSWORD": p.get("password") or ""}
    cmd = [
        "pg_dump", "-h", p["host"], "-p", str(p["port"] or DEFAULT_PG_PORT),
        "-U", p["user"], "-F", "c", "--no-owner", "-f", str(dest), p["database"],
    ]
    try:
        result = subprocess.run(cmd, env=env, stderr=subprocess.PIPE, timeout=1800)
        return result.returncode == 0
    except Exception:  # pragma: no cover - pg_dump unavailable / broken
        return False


def _dump_sqlalchemy(url: str, dest: Path) -> None:
    """Pure-Python export fallback — CREATE TABLE + literal-binds INSERTs (plain SQL)."""
    from sqlalchemy import MetaData
    from sqlalchemy.schema import CreateTable

    eng = create_engine(to_sync_url(url), poolclass=NullPool)
    meta = MetaData()
    meta.reflect(bind=eng)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = [
        "-- PAXS database dump (SQLAlchemy export, PostgreSQL)",
        f"-- generated {stamp}",
        "BEGIN;",
        "",
    ]
    with eng.connect() as conn:
        for table in meta.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=eng.dialect))
            out.append(ddl + ";")
            rows = conn.execute(select(table)).mappings().all()
            for row in rows:
                try:
                    stmt = table.insert().values(**{k: row[k] for k in row.keys()})
                    sql = str(
                        stmt.compile(dialect=eng.dialect, compile_kwargs={"literal_binds": True})
                    )
                    out.append(sql + ";")
                except Exception as exc:  # noqa: BLE001 - one bad row must not abort the dump
                    # The dump stays usable: the row is skipped and the reason is
                    # recorded in the SQL text itself, so the operator sees it.
                    log.debug("db_dump_row_skipped error_type=%s", type(exc).__name__)
                    out.append(f"-- row skipped (unencodable literal) in {table.name}")
            out.append("")
    out.append("COMMIT;")
    eng.dispose()
    dest.write_text("\n".join(out), encoding="utf-8")


def create_backup() -> dict:
    """Back up the *running* database into BACKUP_DIR."""
    url = settings.DATABASE_URL
    p = parse_db_url(url)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir() / f"paxs-postgresql-{stamp}.dump"
    if not _dump_pg_dump(url, dest):
        dest.unlink(missing_ok=True)
        dest = backup_dir() / f"paxs-postgresql-{stamp}.sql"
        _dump_sqlalchemy(url, dest)
    return {
        "file": dest.name,
        "size": dest.stat().st_size,
        "backend": p["backend"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def backup_path(name: str) -> Path:
    """Safe lookup of a backup file by name (rejects traversal)."""
    if not _BACKUP_NAME_RE.match(name) or ".." in name:
        raise ValueError("Invalid backup filename")
    path = backup_dir() / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def list_backups() -> list[dict]:
    if not Path(settings.BACKUP_DIR or "./backups").exists():
        return []
    return [
        {"file": p.name, "size": p.stat().st_size,
         "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
        for p in sorted(Path(settings.BACKUP_DIR).glob("paxs-*"), key=lambda x: x.stat().st_mtime, reverse=True)
    ]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup(name: str) -> dict[str, Any]:
    """Verify a backup archive's readability, format markers, and SHA-256 checksum."""
    path = backup_path(name)
    size = path.stat().st_size
    if size <= 0:
        return {"ok": False, "file": name, "size": 0, "error": "Backup file is empty"}
    sha256 = _file_sha256(path)
    if path.suffix.lower() == ".dump":
        with path.open("rb") as fh:
            magic = fh.read(5)
        if magic != b"PGDMP":
            return {"ok": False, "file": name, "size": size, "sha256": sha256, "error": "Invalid pg_dump header"}
        if shutil.which("pg_restore"):
            res = subprocess.run(["pg_restore", "--list", str(path)], capture_output=True, timeout=60)
            if res.returncode != 0:
                return {"ok": False, "file": name, "size": size, "sha256": sha256, "error": "pg_restore verification failed"}
        return {"ok": True, "file": name, "format": "pg_dump", "size": size, "sha256": sha256}
    text = path.read_text(encoding="utf-8", errors="strict")
    has_markers = "BEGIN;" in text and "COMMIT;" in text
    if not has_markers:
        return {"ok": False, "file": name, "size": size, "sha256": sha256, "error": "SQL dump missing BEGIN/COMMIT markers"}
    return {"ok": True, "file": name, "format": "sql", "size": size, "sha256": sha256}


def prune_old_backups(retention_count: int) -> int:
    """Keep the newest ``retention_count`` backup files and remove older ones."""
    keep = max(1, int(retention_count))
    files = sorted(backup_dir().glob("paxs-*"), key=lambda x: x.stat().st_mtime, reverse=True)
    removed = 0
    for stale in files[keep:]:
        stale.unlink(missing_ok=True)
        removed += 1
    return removed


def get_backup_schedule() -> dict[str, Any]:
    """Return persisted scheduled-backup settings from ``server_settings``."""
    default: dict[str, Any] = {
        "enabled": False, "time": "02:00", "retention_count": 7,
        "last_run_date": None, "last_file": None, "last_verified": None,
    }
    try:
        with SyncSessionLocal() as session:
            row = session.execute(
                select(ServerSetting).where(ServerSetting.key == BACKUP_SCHEDULE_KEY)
            ).scalar_one_or_none()
        if row is not None:
            parsed = json.loads(row.value)
            if isinstance(parsed, dict):
                default.update(parsed)
    except Exception as exc:  # noqa: BLE001 - safe fallback on fresh DB
        log.debug("backup_schedule_read_failed error_type=%s", type(exc).__name__)
    return default


def save_backup_schedule(
    enabled: bool, schedule_time: str, retention_count: int,
    *, last_run_date: str | None = None, last_file: str | None = None, last_verified: bool | None = None,
) -> dict[str, Any]:
    """Persist scheduled-backup settings in ``server_settings``."""
    clean_time = schedule_time.strip()
    if not _TIME_RE.match(clean_time):
        raise ValueError("schedule_time must be HH:MM (24-hour)")
    keep = max(1, min(90, int(retention_count)))
    current = get_backup_schedule()
    updated = {
        "enabled": bool(enabled),
        "time": clean_time,
        "retention_count": keep,
        "last_run_date": last_run_date if last_run_date is not None else current.get("last_run_date"),
        "last_file": last_file if last_file is not None else current.get("last_file"),
        "last_verified": last_verified if last_verified is not None else current.get("last_verified"),
    }
    with SyncSessionLocal() as session:
        row = session.execute(
            select(ServerSetting).where(ServerSetting.key == BACKUP_SCHEDULE_KEY)
        ).scalar_one_or_none()
        payload = json.dumps(updated, ensure_ascii=False)
        if row is None:
            session.add(ServerSetting(key=BACKUP_SCHEDULE_KEY, value=payload))
        else:
            row.value = payload
        session.commit()
    return updated


def run_scheduled_backup_if_due() -> dict[str, Any] | None:
    """Run at most one verified backup per day when the scheduled window is reached."""
    sched = get_backup_schedule()
    if not sched.get("enabled"):
        return None
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if sched.get("last_run_date") == today:
        return None
    if now.strftime("%H:%M") < str(sched.get("time") or "02:00"):
        return None
    created = create_backup()
    check = verify_backup(created["file"])
    pruned = prune_old_backups(int(sched.get("retention_count") or 7))
    save_backup_schedule(
        True, str(sched["time"]), int(sched["retention_count"]),
        last_run_date=today, last_file=created["file"], last_verified=bool(check.get("ok")),
    )
    log.info("Scheduled backup created=%s verified=%s pruned=%d", created["file"], check.get("ok"), pruned)
    return {"created": created, "verification": check, "pruned": pruned}


# ------------------------------------------------------------------ status ----
def db_status() -> dict:
    url = settings.DATABASE_URL
    p = parse_db_url(url)
    head = head_revision()
    cur = current_revision(url)
    saved = saved_config()
    saved_public = None
    if saved:
        saved_public = {k: saved.get(k) for k in ("backend", "host", "port", "user", "name")}
    return {
        "backend": p["backend"],
        "url_masked": masked_url(url),
        "host": p.get("host"),
        "port": p.get("port"),
        "database": p.get("database"),
        "head_revision": head,
        "current_revision": cur,
        "up_to_date": bool(head and head == cur),
        "saved_config_present": saved is not None,
        "saved": saved_public,
        "backup_count": len(list_backups()),
        "backup_dir": str(backup_dir()),
    }


# --------------------------------------------------------- admin seeding -----
def seed_admin(url: str, username: str, password: str, full_name: str | None = None) -> dict:
    """Create the first ADMIN account on a target DB if none exists.

    B-4: the account is flagged ``must_change_password`` so the client forces a
    rotation at first login — this path is automation-driven and the password
    may have been generated or chosen by an operator on someone else's behalf.
    """
    from sqlalchemy.orm import sessionmaker

    eng = create_engine(to_sync_url(url), poolclass=NullPool)
    Session = sessionmaker(eng, expire_on_commit=False)
    try:
        with Session() as session:
            total = session.execute(select(func.count(User.id))).scalar_one()
            if total:
                return {"created": False, "reason": "users already exist"}
            session.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    full_name=full_name,
                    role="ADMIN",
                    auth_source=AuthSource.LOCAL,
                    must_change_password=True,
                )
            )
            session.commit()
        return {"created": True, "username": username, "must_change_password": True}
    finally:
        eng.dispose()
