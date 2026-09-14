"""Database administration — the engine behind Settings → Database.

Everything the dashboard's Database tab needs, kept out of the API layer:

* connection-string parsing / masking / building (sqlite + MySQL)
* persisting a configured backend to ``.env`` (DATABASE_URL) and to a
  ``server_settings`` row (``db_config``) so Test / Initialize can reuse it
* Alembic state (current vs. head revision) and running migrations against an
  arbitrary target URL (a fresh install, or the configured MySQL)
* MySQL connection probes and ``CREATE DATABASE`` for new installations
* database backups — SQLite hot copy via the sqlite3 backup API, MySQL via
  ``mysqldump`` with a pure-SQLAlchemy export fallback
* seeding the first ADMIN account on a brand-new installation

All functions are synchronous; endpoints call them via ``asyncio.to_thread``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.security import hash_password
from app.db.models import ServerSetting, User
from app.db.session import SyncSessionLocal, to_sync_url

log = logging.getLogger("paxs.db_admin")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
DB_CONFIG_KEY = "db_config"
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# --------------------------------------------------------------- parsing ----
def parse_db_url(url: str) -> dict:
    """Split a SQLAlchemy URL into its parts (backend + connection fields)."""
    if url.startswith("sqlite+aiosqlite://"):
        return {"backend": "sqlite", "file": url[len("sqlite+aiosqlite://"):]}
    if url.startswith("sqlite://"):
        return {"backend": "sqlite", "file": url[len("sqlite://"):]}
    for prefix in ("mysql+aiomysql://", "mysql+pymysql://", "mysql://"):
        if url.startswith(prefix):
            rest = url[len(prefix):]
            cred, _, hostport = rest.partition("@")
            user, _, password = cred.partition(":")
            hostport, _, database = hostport.partition("/")
            # Strip URL query params (e.g. ?charset=utf8mb4) — they belong to
            # the driver, not the database name.
            database = database.split("?", 1)[0]
            host, _, port = hostport.partition(":")
            return {
                "backend": "mysql",
                "user": urllib.parse.unquote(user),
                "password": urllib.parse.unquote(password),
                "host": host,
                "port": int(port) if port.isdigit() else 3306,
                "database": database,
            }
    return {"backend": url.split("://")[0], "url": url}


def build_mysql_url(cfg: dict) -> str:
    """Assemble a mysql+aiomysql URL from {host, port, user, password, name}."""
    user = urllib.parse.quote_plus(cfg["user"])
    password = urllib.parse.quote_plus(cfg.get("password") or "")
    port = int(cfg.get("port") or 3306)
    name = cfg.get("name") or ""
    # utf8mb4 is the MySQL default for PAXS (full Unicode — DICOM names
    # carry accented + Arabic characters; latin1 would truncate them).
    return f"mysql+aiomysql://{user}:{password}@{cfg['host']}:{port}/{name}?charset=utf8mb4"


def masked_url(url: str) -> str:
    """Connection string with the password hidden."""
    p = parse_db_url(url)
    if p["backend"] == "sqlite":
        return f"sqlite://{p.get('file') or ':memory:'}"
    cred = f"{p['user']}:{'*' * max(4, len(p.get('password') or ''))}" if p.get("user") else "*" * 4
    return f"mysql://{cred}@{p['host']}:{p['port']}/{p['database']}"


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
    """The saved MySQL password, read back from the .env DATABASE_URL."""
    try:
        url = read_env().get("DATABASE_URL", "")
        if url.startswith("mysql"):
            return parse_db_url(url).get("password") or ""
    except Exception:  # pragma: no cover - defensive
        log.warning("Could not read saved password from env file", exc_info=True)
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
        backend = cfg.get("backend") or "mysql"
        if backend == "sqlite":
            return "sqlite+aiosqlite:///./paxs.db"
        if cfg.get("host") and cfg.get("name"):
            return build_mysql_url(cfg)
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
    except Exception:  # pragma: no cover - defensive
        log.warning("Could not resolve alembic head", exc_info=True)
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


def _is_already_exists(exc: Exception) -> bool:
    """True when a migration failed because the schema is already present
    (sqlite "table x already exists", MySQL "Duplicate column name")."""
    msg = str(exc).lower()
    return "already exists" in msg or msg.startswith("duplicate ")


def run_migrations(url: str, stamp_on_exists: bool = True) -> tuple[list[str], bool]:
    """Run every pending migration on *url* up to head.

    Alembic's env.py derives its connection from ``settings.DATABASE_URL``, so
    we temporarily point the runtime settings object at the target URL, run
    ``alembic upgrade head``, then restore. The app's own engines are already
    bound to their original URL and are unaffected.

    Returns ``(log_lines, stamped)``. When the target schema already contains
    objects a pending migration would create (common on databases bootstrapped
    with ``create_all`` where ``alembic_version`` lags reality) and
    ``stamp_on_exists`` is set, the database is reconciled with ``stamp head``
    instead of failing — exactly what an operator would do by hand.
    """
    from alembic import command
    from alembic.config import Config

    prev = settings.DATABASE_URL
    settings.DATABASE_URL = url
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    mig_logger = logging.getLogger("alembic.runtime.migration")
    mig_logger.addHandler(handler)
    cfg = Config(str(ALEMBIC_INI))
    try:
        command.upgrade(cfg, "head")
        return [ln for ln in buf.getvalue().splitlines() if ln.strip()], False
    except Exception as exc:
        if not (stamp_on_exists and _is_already_exists(exc)):
            raise
        buf.write("Schema objects already present — alembic state drifted; "
                  "stamping to head.\n")
        command.stamp(cfg, "head")
        return [ln for ln in buf.getvalue().splitlines() if ln.strip()], True
    finally:
        mig_logger.removeHandler(handler)
        settings.DATABASE_URL = prev


def ensure_database(url: str) -> bool:
    """Create the MySQL database if it does not exist (no-op for sqlite)."""
    p = parse_db_url(url)
    if p["backend"] != "mysql" or not p["database"]:
        return False
    if not _NAME_RE.match(p["database"]):
        raise ValueError("Invalid database name (letters, digits, underscore only)")
    import pymysql

    conn = pymysql.connect(
        host=p["host"], port=p["port"], user=p["user"], password=p["password"],
        connect_timeout=5, charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{p['database']}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()
    return True


# ------------------------------------------------------------ connectivity ----
def test_mysql_connection(cfg: dict) -> dict:
    """Probe a MySQL server (connect_timeout=3). No state is written."""
    import pymysql

    t0 = time.perf_counter()
    try:
        conn = pymysql.connect(
            host=cfg.get("host") or "127.0.0.1",
            port=int(cfg.get("port") or 3306),
            user=cfg.get("user") or "",
            password=cfg.get("password") or "",
            connect_timeout=3,
        )
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "error": str(exc),
        }
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
            name = cfg.get("name") or ""
            db_exists = False
            if name:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
                    (name,),
                )
                db_exists = cur.fetchone()[0] > 0
    finally:
        conn.close()
    return {
        "ok": True,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "version": version,
        "database": cfg.get("name") or "",
        "database_exists": db_exists,
    }


# ---------------------------------------------------------------- backup -----
def backup_dir() -> Path:
    path = Path(settings.BACKUP_DIR or "./backups")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backup_sqlite(src_path: str, dest: Path) -> None:
    src = sqlite3.connect(src_path)
    try:
        # The app's engines use busy_timeout=30s; match it here so a backup
        # during active ingest waits instead of failing with SQLITE_BUSY.
        src.execute("PRAGMA busy_timeout=30000")
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _dump_mysql_mysqldump(url: str, dest: Path) -> bool:
    if not shutil.which("mysqldump"):
        return False
    p = parse_db_url(url)
    env = {**os.environ, "MYSQL_PWD": p.get("password") or ""}
    cmd = [
        "mysqldump", "-h", p["host"], "-P", str(p["port"] or 3306),
        "-u", p["user"], "--single-transaction", "--routines",
        "--default-character-set=utf8mb4", p["database"],
    ]
    try:
        with open(dest, "wb") as fh:
            result = subprocess.run(cmd, env=env, stdout=fh, stderr=subprocess.PIPE, timeout=300)
        return result.returncode == 0
    except Exception:  # pragma: no cover - mysqldump unavailable / broken
        return False


def _dump_mysql_sqlalchemy(url: str, dest: Path) -> None:
    """Pure-Python export fallback — CREATE TABLE + literal-binds INSERTs."""
    from sqlalchemy import MetaData
    from sqlalchemy.schema import CreateTable

    eng = create_engine(to_sync_url(url), poolclass=NullPool)
    meta = MetaData()
    meta.reflect(bind=eng)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = [
        "-- PAXS database dump (SQLAlchemy export)",
        f"-- generated {stamp}",
        "SET FOREIGN_KEY_CHECKS=0;",
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
                except Exception:
                    out.append(f"-- row skipped (unencodable literal) in {table.name}")
            out.append("")
    out.append("SET FOREIGN_KEY_CHECKS=1;")
    eng.dispose()
    dest.write_text("\n".join(out), encoding="utf-8")


def create_backup() -> dict:
    """Back up the *running* database into BACKUP_DIR."""
    url = settings.DATABASE_URL
    p = parse_db_url(url)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir() / (
        f"paxs-sqlite-{stamp}.sqlite" if p["backend"] == "sqlite"
        else f"paxs-mysql-{stamp}.sql"
    )
    if p["backend"] == "sqlite":
        _backup_sqlite(p["file"], dest)
    elif not _dump_mysql_mysqldump(url, dest):
        _dump_mysql_sqlalchemy(url, dest)
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
        "database": p.get("database") or p.get("file"),
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
    """Create the first ADMIN account on a target DB if none exists."""
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
                )
            )
            session.commit()
        return {"created": True, "username": username}
    finally:
        eng.dispose()
