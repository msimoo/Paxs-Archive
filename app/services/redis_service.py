"""Redis client helper, caching layer, and full administration engine.

Provides a thread-safe Redis connection pool, key namespace helpers, cache decorators,
real-time performance metrics, server process/service lifecycle controls (Start/Stop/Restart/Install),
and administrative controls for fast sub-5ms DICOM metadata & rendered image retrieval.
Gracefully falls back to Tier-1 In-Memory LRU RAM cache if Redis is unreachable or disabled.

**Layout.** This module is the public API and the test seam; the implementation
is split inside the :mod:`app.services.redis` package (it was ~1,400 lines in one
file):

| module      | owns                                                          |
| ----------- | ------------------------------------------------------------- |
| `cache`     | client pool, Tier-1 RAM LRU cache, ``cache_get``/``cache_set`` |
| `config`    | URL parse/mask, secret scrubbing, .env + ``server_settings``   |
| `installer` | detection, start/stop/restart, portable install (pinned digest)|
| `panel`     | probes, status metrics, key browsing, flushes                  |

Every public name below is re-exported unchanged. The suite patches this
module's attributes (``redis_service.get_redis_client``, ``.cache_get``,
``.install_redis_portable``) and expects the cache and admin paths to observe
the substitution, so the submodules look those names up here at call time
instead of binding them at import.

**The L1 state stays mutable through this namespace.** ``_MEM_MAX_BYTES``,
``_MEM_MAX_ITEMS``, ``_CURRENT_MEM_BYTES`` and friends are *state*, not API: the
eviction loop compares against them, so a re-exported copy would freeze the
facade at import time while the cache moved on. ``tests/test_background_and_
concurrency_fixes.py`` also sets ``_MEM_MAX_BYTES`` here to shrink the budget and
reads ``_CURRENT_MEM_BYTES`` back. They are therefore deliberately *not*
imported: the module-class proxy at the bottom of this file forwards any name it
does not define to the submodule that owns it, so reads see the live value and
writes land where the eviction loop looks. Every other private helper
(``_redis_lock``, ``_URL_CREDENTIALS``, …) behaves the same way for free.
"""

from __future__ import annotations

# Public functions only. The private state (``_MEM_*``, ``_redis_lock``, …) is
# forwarded by the proxy below rather than copied: a copy would not track the
# real budget the eviction loop uses. The same proxy is why there is no
# ``import urllib.request`` here even though the installer tests patch
# ``redis_service.urllib.request.urlretrieve``: the submodule imports it, so the
# undefined name resolves through the proxy to the one shared stdlib module.
from app.services.redis.cache import (
    cache_delete_pattern,
    cache_get,
    cache_set,
    cache_set_mem,
    flush_l1_cache,
    get_l1_cache_stats,
    get_redis_client,
    is_redis_ready_fast,
    redis_circuit_call,
    redis_circuit_stats,
    reset_redis_client,
    reset_redis_circuit,
)
from app.services.redis.config import (
    REDIS_CONFIG_KEY,
    build_redis_url,
    env_file_path,
    masked_redis_url,
    parse_redis_url,
    read_env,
    redact_redis_config,
    save_redis_config,
    saved_redis_config,
    saved_redis_config_raw,
    saved_redis_password,
    scrub_secrets,
    write_env_redis,
)
from app.services.redis.installer import (
    REDIS_ARCHIVE_SHA256,
    ArchiveIntegrityError,
    ArchivePathTraversalError,
    ArchiveSecurityError,
    get_redis_server_installation,
    install_redis_portable,
    restart_redis_server,
    safe_extract_archive,
    start_redis_server,
    stop_redis_server,
    verify_archive_sha256,
)
from app.services.redis.panel import (
    delete_redis_key,
    flush_redis_cache,
    get_key_detail,
    get_redis_raw_info,
    get_redis_status,
    list_redis_keys,
    ping_redis,
    test_redis_connection,
)

# Alphabetical, as RUF022 requires: the grouping this list used to carry is in
# the table above and in each submodule's own ``__all__``.
__all__ = [
    "REDIS_ARCHIVE_SHA256",
    "REDIS_CONFIG_KEY",
    "ArchiveIntegrityError",
    "ArchivePathTraversalError",
    "ArchiveSecurityError",
    "build_redis_url",
    "cache_delete_pattern",
    "cache_get",
    "cache_set",
    "cache_set_mem",
    "delete_redis_key",
    "env_file_path",
    "flush_l1_cache",
    "flush_redis_cache",
    "get_key_detail",
    "get_l1_cache_stats",
    "get_redis_client",
    "get_redis_raw_info",
    "get_redis_server_installation",
    "get_redis_status",
    "install_redis_portable",
    "is_redis_ready_fast",
    "list_redis_keys",
    "masked_redis_url",
    "parse_redis_url",
    "ping_redis",
    "read_env",
    "redact_redis_config",
    "reset_redis_client",
    "redis_circuit_call",
    "redis_circuit_stats",
    "reset_redis_circuit",
    "restart_redis_server",
    "safe_extract_archive",
    "save_redis_config",
    "saved_redis_config",
    "saved_redis_config_raw",
    "saved_redis_password",
    "scrub_secrets",
    "start_redis_server",
    "stop_redis_server",
    "test_redis_connection",
    "verify_archive_sha256",
    "write_env_redis",
]


# ------------------------------------------------ live-state facade proxy --
# `redis_service` is imported across the app *and* is the suite's monkeypatch
# seam. Re-exporting functions covers the call surface, but the L1 cache's
# budgets and byte counter are mutable module *state*: `cache_set_mem` compares
# against `cache._MEM_MAX_BYTES`, while a test (rightly) sets
# `redis_service._MEM_MAX_BYTES`. A copied binding would silently ignore that and
# the cache would keep evicting against the 256 MB default — the exact failure
# the C-3 tests caught when this file was split. Swapping the module class makes
# an undefined attribute resolve in — and write through to — the submodule that
# defines it, so both spellings address one object.
import sys as _sys
import types as _types

from app.services.redis import cache as _cache
from app.services.redis import config as _config
from app.services.redis import installer as _installer
from app.services.redis import panel as _panel

_OWNERS: tuple[_types.ModuleType, ...] = (_cache, _config, _installer, _panel)


def _owner_of(name: str) -> _types.ModuleType:
    """The submodule that defines ``name`` (first match in a fixed order)."""
    for module in _OWNERS:
        if name in vars(module):
            return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _LiveStateModule(_types.ModuleType):
    """A module whose *undefined* attributes are the submodules' own globals.

    Only the fallback path is proxied: every name this file defines (each public
    re-export, ``__all__``, the helpers here) is read and written locally, so the
    functions the app imports remain the objects this module holds.
    """

    def __getattr__(self, name: str) -> object:
        return getattr(_owner_of(name), name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in self.__dict__ or name.startswith("__"):
            super().__setattr__(name, value)
            return
        try:
            owner = _owner_of(name)
        except AttributeError:
            super().__setattr__(name, value)
            return
        setattr(owner, name, value)


_sys.modules[__name__].__class__ = _LiveStateModule
