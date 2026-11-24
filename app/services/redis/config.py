"""Redis connection configuration: parse, mask, scrub, persist.

A Redis connection string carries a password, and this module is where that
secret is handled: the raw config is stored in ``server_settings`` and written
to ``.env``, while everything that leaves the process goes through
:func:`redact_redis_config` / :func:`masked_redis_url` /
:func:`scrub_secrets`. Keeping the three in one place is deliberate — a second
implementation of "mask this" is how a password ends up in a response body
(B-3 / FS-8).

Persistence has two sinks on purpose: ``.env`` is what a restarted process
reads, and ``server_settings`` is what the admin panel edits. They are written
together, then the live client is reloaded.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import urllib.parse
from pathlib import Path
from types import ModuleType

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.models import ServerSetting
from app.db.session import SyncSessionLocal

log = logging.getLogger("paxs.redis")

REDIS_CONFIG_KEY = "redis_config"

_FACADE = "app.services.redis_service"


def _facade() -> ModuleType:
    """The public ``app.services.redis_service`` namespace, resolved per call.

    ``reset_redis_client`` lives in ``cache``; going through the facade avoids a
    config -> cache -> config import cycle and keeps the client-swap test seam
    working (the suite patches ``redis_service.get_redis_client``).
    """
    return sys.modules[_FACADE]


# ------------------------------------------------------------- URL helpers --
def parse_redis_url(url: str) -> dict:
    """Split a Redis URL into component connection fields."""
    if not url:
        return {
            "host": "127.0.0.1",
            "port": 6379,
            "db": 0,
            "password": "",
            "ssl": False,
            "user": "",
        }
    try:
        parsed = urllib.parse.urlparse(url)
        scheme = parsed.scheme.lower()
        ssl = scheme in ("rediss",)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 6379
        path = parsed.path.lstrip("/")
        db = int(path) if path.isdigit() else 0
        user = parsed.username or ""
        password = parsed.password or ""
        return {
            "host": host,
            "port": port,
            "db": db,
            "password": password,
            "ssl": ssl,
            "user": user,
        }
    except (ValueError, KeyError) as exc:  # a malformed REDIS_URL is reported, never raised
        # The URL is never logged: it may carry the password.
        log.debug("redis_url_parse_failed error_type=%s", type(exc).__name__)
        return {
            "host": "127.0.0.1",
            "port": 6379,
            "db": 0,
            "password": "",
            "ssl": False,
            "user": "",
        }


def build_redis_url(cfg: dict) -> str:
    """Assemble a redis:// URL from {host, port, db, password, ssl, user}."""
    host = cfg.get("host") or "127.0.0.1"
    port = int(cfg.get("port") or 6379)
    db = int(cfg.get("db") or 0)
    password = cfg.get("password") or ""
    user = cfg.get("user") or ""
    ssl = bool(cfg.get("ssl", False))
    scheme = "rediss" if ssl else "redis"

    auth = ""
    if user and password:
        auth = f"{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@"
    elif password:
        auth = f":{urllib.parse.quote_plus(password)}@"

    return f"{scheme}://{auth}{host}:{port}/{db}"


def masked_redis_url(url: str) -> str:
    """Return connection string with password hidden."""
    p = parse_redis_url(url)
    scheme = "rediss" if p.get("ssl") else "redis"
    host = p.get("host") or "127.0.0.1"
    port = p.get("port") or 6379
    db = p.get("db", 0)
    user = p.get("user")
    pwd = p.get("password")
    if user and pwd:
        auth = f"{user}:{'*' * max(4, len(pwd))}@"
    elif pwd:
        auth = f":{'*' * max(4, len(pwd))}@"
    else:
        auth = ""
    return f"{scheme}://{auth}{host}:{port}/{db}"


#: ``scheme://user:password@host`` — the credential half of a connection string.
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-zA-Z][\w+.\-]*://)(?P<user>[^/@\s:]*):(?P<pwd>[^@/\s]*)@"
)

#: Shortest secret worth scanning for; below this a substring match would mask
#: ordinary text ("a", "12") and produce nonsense.
_MIN_SECRET_LEN = 4


def _mask_url_credentials(match: re.Match[str]) -> str:
    """Replace a URL's password with stars — idempotently.

    An already-masked password is left as-is, so scrubbing twice (or scrubbing a
    value that came out of :func:`masked_redis_url`) does not change the text.
    """
    if set(match.group("pwd")) == {"*"}:
        return match.group(0)
    return f"{match['scheme']}{match['user']}:***@"


def scrub_secrets(text: str, *known_secrets: str) -> str:
    """Mask credentials inside free text (B-3 / FS-8).

    Applied to anything that originates below the Redis driver — an exception
    message can echo the connection URL, and that URL carries the password.
    Masks both the URL form and any literal secret value we already hold, so a
    message we cannot predict is still safe to return.
    """
    scrubbed = _URL_CREDENTIALS.sub(_mask_url_credentials, text)
    for secret in known_secrets:
        if secret and len(secret) >= _MIN_SECRET_LEN and secret in scrubbed:
            scrubbed = scrubbed.replace(secret, "***")
    return scrubbed


# ------------------------------------------------------------- Persistence --
def env_file_path() -> Path:
    return Path(getattr(settings, "ENV_FILE_PATH", ".env") or ".env")


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


def saved_redis_password() -> str:
    """Read saved Redis password from .env or database."""
    try:
        url = read_env().get("REDIS_URL", "")
        if url.startswith("redis"):
            return parse_redis_url(url).get("password") or ""
    except (OSError, ValueError, KeyError) as exc:  # probe: a missing/odd .env must not fail the caller
        # Debug only, and never the message: a redis URL carries the password.
        log.debug("redis_password_env_read_failed error_type=%s", type(exc).__name__)
    try:
        saved = saved_redis_config_raw()
        if saved and saved.get("password"):
            return saved["password"]
    except (SQLAlchemyError, OSError, ValueError, KeyError) as exc:  # a legacy DB row may lack the column
        log.debug("redis_password_db_read_failed error_type=%s", type(exc).__name__)
    return ""


def saved_redis_config_raw() -> dict | None:
    """The operator's saved Redis configuration, **including the password**.

    Internal only. This is the JSON persisted in ``server_settings`` and it holds
    the connection password in cleartext, so it must never be returned from an
    API response. Use :func:`saved_redis_config` (redacted) for anything that
    leaves the process, or :func:`saved_redis_password` when the secret itself is
    genuinely needed (reconnecting, pre-filling a probe).
    """
    with SyncSessionLocal() as session:
        row = session.execute(
            select(ServerSetting).where(ServerSetting.key == REDIS_CONFIG_KEY)
        ).scalar_one_or_none()
        if not row or not row.value:
            return None
        try:
            return json.loads(row.value)
        except (ValueError, TypeError):  # a legacy row may hold a non-JSON value
            return None


#: Keys in a stored Redis config that carry a credential. Dropped (not masked)
#: from anything returned to a caller — the UI only needs the connection *shape*.
_CREDENTIAL_FIELDS = frozenset({"password", "pass", "secret", "token", "url", "uri", "dsn"})


def redact_redis_config(cfg: dict | None) -> dict | None:
    """The same config with every credential replaced by a presence flag.

    B-3 / FS-8: ``saved`` used to be embedded verbatim in
    ``GET /api/v1/redis/status``, so *any* authenticated user — whatever their
    role — could read the Redis password out of the JSON. Only the shape is
    needed by the UI (host/port/db, and whether a password is configured).
    """
    if cfg is None:
        return None
    redacted = {k: v for k, v in cfg.items() if k not in _CREDENTIAL_FIELDS}
    redacted["has_password"] = bool(cfg.get("password"))
    return redacted


def saved_redis_config() -> dict | None:
    """The operator's saved Redis configuration, **redacted** (safe to return)."""
    return redact_redis_config(saved_redis_config_raw())


def write_env_redis(url: str, enabled: bool) -> Path:
    """Rewrite REDIS_URL and REDIS_CACHE_ENABLED inside .env (preserving other keys)."""
    path = env_file_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    url_line = f'REDIS_URL="{url}"'
    enabled_line = f'REDIS_CACHE_ENABLED={"true" if enabled else "false"}'

    url_found = False
    enabled_found = False
    for i, raw in enumerate(lines):
        if raw.strip().startswith("REDIS_URL="):
            lines[i] = url_line
            url_found = True
        elif raw.strip().startswith("REDIS_CACHE_ENABLED="):
            lines[i] = enabled_line
            enabled_found = True

    if not url_found:
        lines.append(url_line)
    if not enabled_found:
        lines.append(enabled_line)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def save_redis_config(cfg: dict, persist_env: bool = True) -> dict:
    """Persist Redis configuration to database and .env, then live-reload client."""
    host = cfg.get("host") or "127.0.0.1"
    port = int(cfg.get("port") or 6379)
    db = int(cfg.get("db") or 0)
    password = cfg.get("password") or ""
    enabled = bool(cfg.get("enabled", True))
    ssl = bool(cfg.get("ssl", False))

    if not password:
        password = saved_redis_password()

    clean_cfg = {
        "host": host,
        "port": port,
        "db": db,
        "password": password,
        "enabled": enabled,
        "ssl": ssl,
    }
    url = build_redis_url(clean_cfg)

    # Save to server_settings table
    with SyncSessionLocal() as session:
        payload = json.dumps(clean_cfg)
        row = session.execute(
            select(ServerSetting).where(ServerSetting.key == REDIS_CONFIG_KEY)
        ).scalar_one_or_none()
        if row is None:
            session.add(ServerSetting(key=REDIS_CONFIG_KEY, value=payload))
        else:
            row.value = payload

        # Also store REDIS_URL & REDIS_CACHE_ENABLED keys
        row_url = session.execute(
            select(ServerSetting).where(ServerSetting.key == "REDIS_URL")
        ).scalar_one_or_none()
        if row_url is None:
            session.add(ServerSetting(key="REDIS_URL", value=url))
        else:
            row_url.value = url

        row_en = session.execute(
            select(ServerSetting).where(ServerSetting.key == "REDIS_CACHE_ENABLED")
        ).scalar_one_or_none()
        if row_en is None:
            session.add(ServerSetting(key="REDIS_CACHE_ENABLED", value="1" if enabled else "0"))
        else:
            row_en.value = "1" if enabled else "0"

        session.commit()

    env_path = None
    if persist_env:
        env_path = str(write_env_redis(url, enabled))

    # Live reload the client
    _facade().reset_redis_client(new_url=url, enabled=enabled)

    return {
        "ok": True,
        "url_masked": masked_redis_url(url),
        "enabled": enabled,
        "env_path": env_path,
    }
