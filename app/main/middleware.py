"""ASGI middleware stack: gzip, caching, security headers, auth gate, rate limit.

Order matters, and it runs **outside-in** — which is the reverse of the order
the ``add_middleware`` calls appear in ``app/main/app.py``: Starlette *prepends*
on every call, so the last one added is the outermost layer. Resolved order,
outermost first::

    SecurityHeaders -> XdsRequestLimit -> ForwardedPrefix -> ViewerAuthGate
        -> CsrfProtection -> Metrics -> GzipCompression -> RateLimit
        -> RuntimeCORS -> routes

Two consequences, both of which this docstring once stated back-to-front:

* ``RuntimeCORSMiddleware`` is added **first**, so it is the **innermost**
  layer — it decorates the response closest to the routes, and every wrapper
  listed above it inspects that response afterwards.
* ``SecurityHeadersMiddleware`` is added **last**, so it is the **outermost**
  layer and stamps its headers on whatever all the inner layers produced —
  including a 303 from the auth gate and a 429 from the rate limiter. Likewise
  ``ForwardedPrefixMiddleware`` sits outside the gate on purpose: the gate
  matches on ``scope["path"]``, so a non-stripping proxy's ``/paxs/viewer``
  must be stripped to ``/viewer`` before the gate looks at it.

``ViewerAuthGateMiddleware`` is fourth from the outside: it decides browser-shell
access before gzip, metrics or the rate limiter do any work, so a redirect costs
neither a compression pass nor a rate-limit token. See its docstring for the
cookie rules. The resolved order is asserted by
``tests/test_main_and_middleware_hygiene.py::test_middleware_runs_outside_in``,
so this paragraph cannot drift again without a red test.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import re
import traceback
from collections.abc import Mapping
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

log = logging.getLogger("paxs.main")

_SENSITIVE_QUERY_VALUE = re.compile(
    r"(?i)([?&](?:share|token|access_token|refresh_token|session)=)[^&#\s\"']+"
)
_SENSITIVE_COOKIE_VALUE = re.compile(
    r"(?i)\b(paxs_share|paxs_session|refresh_token|access_token)=([^;\s,\"']+)"
)
_SHARE_PATH_TOKEN = re.compile(
    r"(?i)(/api/v1/share/)(?!exchange(?:[/ ?\"']|$))[^/?\s\"']+"
)
_SHARE_HEADER_VALUE = re.compile(
    r"(?i)(x-paxs-share-token\s*[:=]\s*)[^\s,;\"']+"
)


class SensitiveLogRedactionFilter(logging.Filter):
    """Remove share/session credentials from formatted request and error logs."""

    @staticmethod
    def _redact(value: str) -> str:
        value = _SENSITIVE_QUERY_VALUE.sub(r"\1[REDACTED]", value)
        value = _SENSITIVE_COOKIE_VALUE.sub(r"\1=[REDACTED]", value)
        value = _SHARE_PATH_TOKEN.sub(r"\1[REDACTED]", value)
        return _SHARE_HEADER_VALUE.sub(r"\1[REDACTED]", value)

    @classmethod
    def _redact_arg(cls, value: object) -> object:
        """Redact one format argument; numbers/None keep their type for %d-style placeholders."""
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return cls._redact(value if isinstance(value, str) else str(value))

    @classmethod
    def _redact_args(cls, args: object) -> object:
        if isinstance(args, Mapping):
            return {key: cls._redact_arg(value) for key, value in args.items()}
        if isinstance(args, tuple):
            return tuple(cls._redact_arg(value) for value in args)
        return args

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact in place, keeping ``args`` so arg-unpacking formatters keep working.

        uvicorn's AccessFormatter unpacks exactly five args; clearing them made
        every access line a "--- Logging error ---". The template and each arg
        are redacted separately; if that differs from redacting the fully
        formatted line (a secret split across template and arg), the record is
        collapsed to the redacted line instead — redaction always wins.
        """
        try:
            full = self._redact(record.getMessage())
            msg = self._redact(record.msg if isinstance(record.msg, str) else str(record.msg))
            args = self._redact_args(record.args) if record.args else record.args
            record.msg, record.args = msg, args
            if record.getMessage() != full:
                record.msg, record.args = full, ()
        except (TypeError, ValueError, KeyError):
            record.msg = "[REDACTED LOG MESSAGE]"
            record.args = ()
        if record.exc_info:
            record.exc_text = self._redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)
        return True


def install_log_redaction() -> None:
    """Redact credential-bearing fields before app and HTTP log handlers emit them."""
    redactor = SensitiveLogRedactionFilter()
    loggers = (logging.getLogger(), logging.getLogger("uvicorn.access"),
               logging.getLogger("uvicorn.error"), logging.getLogger("httpx"))
    for logger in loggers:
        if not any(isinstance(item, SensitiveLogRedactionFilter) for item in logger.filters):
            logger.addFilter(redactor)
        for handler in logger.handlers:
            if not any(isinstance(item, SensitiveLogRedactionFilter) for item in handler.filters):
                handler.addFilter(redactor)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, SensitiveLogRedactionFilter) for item in handler.filters):
            handler.addFilter(redactor)



# --------------------------------------------------------------------------- #
# Gzip compression for text assets (viewer + dashboard SPAs).
#
# The PaxsViewer bundle is a few MB of minified JS/CSS across ~70 chunks —
# gzip cuts that ~70%. Only *text* responses are compressed:
#  - binary endpoints (/dicom-web pixel data, /api/v1/volume voxel blocks)
#    are already compressed codecs — gzip would waste CPU for ~0 gain, so they
#    are skipped by path AND by content-type.
#  - responses with an existing Content-Encoding are never touched.
#  - bodies are buffered only when a content-length is known and within a
#    sane cap, so streaming responses pass through untouched.
#
# A plain ASGI middleware (not BaseHTTPMiddleware) is required here: Starlette's
# FileResponse/StreamingResponse have no readable `.body`, and only an ASGI
# send-wrapper can re-chunk them.
# --------------------------------------------------------------------------- #
_COMPRESSIBLE_TYPE = re.compile(
    r"^(text/|application/(json|dicom\+json|javascript|x-javascript|xml|xhtml\+xml|problem\+json)"
    r"|image/svg\+xml)"
)
_GZIP_MIN_BYTES = 1024
# Keep the per-response copy bounded under concurrent metadata reads; larger
# JSON passes through unchanged. S4 (viewer speed plan): raised from 16 MB —
# a 1,800-slice CT series' metadata is ~19 MB and gzips to <1 MB (≈98 %), and
# the body is already in memory as one bytes object (no extra join copy below).
_GZIP_MAX_BYTES = 48 * 1024 * 1024
# DICOMweb is deliberately NOT skipped by path any more: `/metadata` is JSON and
# gains ~90%, while the pixel routes (`application/dicom`, `multipart/related`,
# `image/jpeg`) still fail the content-type test below and are never buffered.
_BINARY_PATH_PREFIXES = ("/api/v1/volume/", "/api/v1/fusion/", "/api/v1/mpr/")


class XdsRequestLimitMiddleware:
    """Bound XDS registration JSON before FastAPI buffers and parses it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = settings.XDS_MAX_REQUEST_BYTES
        path = scope.get("path", "")
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or not path.endswith("/api/v1/xdsi/register") or limit == 0):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared:
            try:
                if int(declared) > limit:
                    await self._reject(send)
                    return
            except ValueError:
                await self._reject(send)
                return

        chunks: list[bytes] = []
        size = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await self._reject(send)
                return
            chunks.append(chunk)
            more_body = message.get("more_body", False)

        body = b"".join(chunks)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = b'{"detail":"XDS registration request exceeds the configured size limit"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})


class GzipCompressionMiddleware:
    """Compress gzip-capable text responses on the fly (ASGI send-wrapper)."""

    def __init__(self, app, min_bytes: int = _GZIP_MIN_BYTES):
        self.app = app
        self.min_bytes = min_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or "gzip" not in scope.get("headers", []).__str__():
            # Cheap guard: only bother parsing headers when the client actually
            # advertises gzip (the header scan below does the real check).
            await self.app(scope, receive, send)
            return
        headers = MutableHeaders(scope=scope)
        if "gzip" not in headers.get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith(_BINARY_PATH_PREFIXES):
            await self.app(scope, receive, send)
            return

        state = {"active": False, "start": None, "chunks": [], "size": 0, "sent": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                out_headers = MutableHeaders(scope=message)
                state["active"] = _compressible_start(message, out_headers, self.min_bytes)
                if state["active"]:
                    state["start"] = message
                    state["headers"] = out_headers
                    state["declared"] = int(out_headers["content-length"])
                    return  # hold the start until the body is complete
                await send(message)
                return
            if message["type"] == "http.response.body":
                if not state["active"]:
                    await send(message)
                    return
                chunk = message.get("body", b"")
                # S4 (viewer speed plan): a body with a declared Content-Length
                # (the only kind `_compressible_start` admits) is bounded, so it
                # is buffered even when it arrives in several messages. Starlette's
                # BaseHTTPMiddleware (RateLimitMiddleware, inside this one)
                # re-sends every response as body + empty final message, which
                # used to make every JSON response — 7 MB series metadata
                # included — pass through uncompressed. A body that outgrows its
                # declared length or the cap still passes through as before.
                if state["size"] + len(chunk) > min(_GZIP_MAX_BYTES, state["declared"]):
                    await send(state["start"])
                    for buffered in state["chunks"]:
                        await send({"type": "http.response.body", "body": buffered, "more_body": True})
                    await send(message)
                    state["chunks"].clear()
                    state["active"] = False
                    return
                if message.get("more_body", False):
                    state["chunks"].append(chunk)
                    state["size"] += len(chunk)
                    return
                state["chunks"].append(chunk)
                state["size"] += len(chunk)
                await _send_compressed(send, state, path)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _compressible_start(message: dict, headers: MutableHeaders, min_bytes: int) -> bool:
    """True when a response.start describes a text body worth gzipping.

    Only *text* responses qualify: binary endpoints are already compressed
    codecs, 204/304 carry no body, an already-encoded body must not be encoded
    twice, and a body outside ``min_bytes.._GZIP_MAX_BYTES`` costs more CPU
    than it saves.
    """
    status = message["status"]
    clen = headers.get("content-length", "")
    return (
        status >= 200
        and status not in (204, 304)
        and not headers.get("content-encoding", "")
        and bool(_COMPRESSIBLE_TYPE.search(headers.get("content-type", "")))
        and clen.isdigit()
        and min_bytes <= int(clen) <= _GZIP_MAX_BYTES
    )


async def _send_compressed(send: Send, state: dict, path: str) -> None:
    """Gzip the buffered body, then emit the held response.start and the body."""
    parts = [chunk for chunk in state["chunks"] if chunk]
    # One non-empty chunk (the usual case, plus BaseHTTPMiddleware's empty
    # final message) is compressed in place instead of copied by join().
    data = parts[0] if len(parts) == 1 else b"".join(parts)
    state["chunks"].clear()
    state["size"] = 0
    # Viewer bundles are cold-start critical. Level 1 reduces compression CPU,
    # while level 6 keeps metadata smaller on wire.
    is_ui_asset = path.startswith(("/viewer/", "/admin/"))
    compressed = await asyncio.to_thread(
        gzip.compress, data, compresslevel=1 if is_ui_asset else 6
    )
    out_headers = state["headers"]
    out_headers["content-encoding"] = "gzip"
    out_headers["content-length"] = str(len(compressed))
    vary = out_headers.get("vary", "")
    if vary:
        if "accept-encoding" not in vary.lower():
            out_headers["vary"] = f"{vary}, Accept-Encoding"
    else:
        out_headers["vary"] = "Accept-Encoding"
    await send({
        "type": "http.response.start",
        "status": state["start"]["status"],
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in out_headers.items()
        ],
    })
    await send({"type": "http.response.body", "body": compressed, "more_body": False})


# --------------------------------------------------------------------------- #
# Static-file caching.
#
# The viewer + admin SPAs are code-split bundles: the ~70 hashed chunk files
# are immutable once built (the build embeds a content hash in every name), so
# browsers may cache them forever — without Cache-Control headers every reload
# re-downloads (or at best revalidates) the whole bundle. index.html, configs
# and the service worker must instead revalidate every load so updates ship.
# --------------------------------------------------------------------------- #
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
DEFAULT_CACHE = "public, max-age=86400"

# Non-hashed entry points / configs that must always revalidate.
_NO_CACHE_NAMES = {
    "index.html", "app-config.js", "manifest.json", "sw.js",
    "viewer-version.txt", "silent-refresh.html", "google.js",
    "init-service-worker.js", "paxs-mpr.js", "paxs-mpr-geometry.js",
    "paxs-study-list-enhance.js", "paxs-study-list-theme.css",
    # Hand-maintained viewer assets: no content hash in the name, so a stale
    # copy is served until the cache expires unless they revalidate.
    "paxs-thumb-engine.js", "paxs-sidebar-theme.css", "paxs-viewer-theme.css",
    "paxs-dialog-engine.js", "paxs-hu-probe.js", "three.min.js",
    "paxs-pixel-pipeline.js", "paxs-viewport-safety.js", "paxs-runtime-guard.js",
    # C-19: the shared report addendum renderer. Served to both the Study List
    # and the viewer, so a stale copy would render one surface's chain with the
    # other's escaping.
    "paxs-report-addenda.js",
    "paxs-toolbar-enhance.js",
    # V-03..V-05 reading module (hand-maintained, no content hash in the name).
    "paxs-reading-safety.js",
    # This upstream OHIF chunk (dicom-video) has a patched diagnostic
    # (`dataSource=`); revalidate it so an immutable cached copy cannot keep the
    # warning alive. The 100.bundle.* chunks it replaced were pruned (V-12).
    "5154.bundle.a0d27024.js",
}
_HASH_STEM = re.compile(r"[0-9a-fA-F]{12,}")


def _cache_control_for(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    # Directory requests (e.g. "/viewer/") resolve to index.html inside
    # StaticFiles, but the path we see here is the raw mount path — treat
    # empty/dir paths as index.html so the entry point revalidates.
    if not name or name in (".", ".."):
        name = "index.html"
    if name in _NO_CACHE_NAMES:
        return NO_CACHE
    stem, _, _ext = name.rpartition(".")
    # hash-prefixed assets: 0ddf166f7a343eda.wasm, a34f9d1faa5f3315.woff2
    if _HASH_STEM.fullmatch(stem):
        return IMMUTABLE_CACHE
    # viewer bundle chunks: app.bundle.29858d89fb4b9b9d.js
    if ".bundle." in stem and re.search(r"\.bundle\.[0-9a-fA-F]{6,}$", stem):
        return IMMUTABLE_CACHE
    # admin hashed assets: assets/index-DZaX6ItW.js / index-iyLN2ywI.css
    if re.fullmatch(r"index-[A-Za-z0-9_-]{8,}", stem):
        return IMMUTABLE_CACHE
    return DEFAULT_CACHE


# --------------------------------------------------------------------------- #
# SPA shell responses (V2-4 — sub-path / reverse-proxy support).
#
# Every SPA entry point is served through here. When the app is mounted under a
# sub-path, the shell's root-absolute asset references are prefixed and a small
# runtime boot shim is injected so runtime-built URLs (and the dashboard's
# literal '/api/v1/...' calls) resolve too.
#
# With no prefix configured this is exactly FileResponse — same bytes, same
# headers — so a root-mounted deployment is unchanged.
# --------------------------------------------------------------------------- #
_SHELL_CACHE: dict[str, tuple[float, str]] = {}


def _read_shell(path: Path) -> str:
    """Read a shell/config file, memoised on mtime (re-read on every page load)."""
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:  # a missing shell is a 404, not a crash
        # mtime 0.0 is the sentinel that disables the memo, so a shell created
        # after the first request is picked up.
        log.debug("shell_stat_failed error_type=%s", type(exc).__name__)
        mtime = 0.0
    key = str(path)
    hit = _SHELL_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    text = path.read_text(encoding="utf-8")
    _SHELL_CACHE[key] = (mtime, text)
    return text


def _active_prefix(request: Request) -> str:
    """The sub-path for this request: X-Forwarded-Prefix (via scope) else ROOT_PATH."""
    from app.core.proxy_prefix import normalise_prefix

    prefix = normalise_prefix(request.scope.get("root_path") or "")
    return prefix or normalise_prefix(settings.ROOT_PATH or "")


def _spa_shell(path: Path, request: Request, *, media_type: str = "text/html") -> Response:
    """Serve an SPA entry HTML, prefix-aware when a sub-path is in play."""
    prefix = _active_prefix(request)
    if not prefix:
        response = FileResponse(path, media_type=media_type)
        response.headers["Cache-Control"] = NO_CACHE
        return response
    from app.core.config import resolve_public_base_url
    from app.core.proxy_prefix import inject_boot

    body = inject_boot(_read_shell(path), prefix, resolve_public_base_url(request))
    response = HTMLResponse(body, media_type=media_type)
    response.headers["Cache-Control"] = NO_CACHE
    return response


def _prefixed_text_asset(path: Path, request: Request, media_type: str) -> Response:
    """Serve a text asset (e.g. app-config.js) with root-absolute paths prefixed."""
    prefix = _active_prefix(request)
    cache = _cache_control_for(path.name)
    if not prefix:
        response = FileResponse(path, media_type=media_type)
        response.headers["Cache-Control"] = cache
        return response
    from app.core.proxy_prefix import prefix_markup

    response = Response(prefix_markup(_read_shell(path), prefix), media_type=media_type)
    response.headers["Cache-Control"] = cache
    return response


# S1 (plans/09): the viewer shipped ~6.5 MB of JS uncompressed on first load.
# Content-hashed assets never change, so each is gzipped once and kept in a
# bounded LRU; later requests (any client) are a dict lookup. Range requests and
# small or already-compressed files are served as-is.
_GZIP_TYPES = (".js", ".css", ".wasm", ".json", ".svg", ".mjs")
_GZIP_MIN_BYTES = 1024
_GZIP_CACHE_MAX_BYTES = 96 * 1024 * 1024


class _GzipCache:
    """Thread-safe LRU of gzip bodies keyed by (path, mtime_ns, size)."""

    def __init__(self, max_bytes: int) -> None:
        from collections import OrderedDict
        from threading import Lock

        self._items: OrderedDict[tuple, bytes] = OrderedDict()
        self._bytes = 0
        self._max = max_bytes
        self._lock = Lock()

    def get(self, key: tuple) -> bytes | None:
        with self._lock:
            body = self._items.get(key)
            if body is not None:
                self._items.move_to_end(key)
            return body

    def put(self, key: tuple, body: bytes) -> None:
        if len(body) > self._max:
            return
        with self._lock:
            if key in self._items:
                return
            self._items[key] = body
            self._bytes += len(body)
            while self._bytes > self._max and self._items:
                _, evicted = self._items.popitem(last=False)
                self._bytes -= len(evicted)


_GZIP_CACHE = _GzipCache(_GZIP_CACHE_MAX_BYTES)


def _gzip_file(path: str) -> bytes:
    with open(path, "rb") as handle:
        return gzip.compress(handle.read(), compresslevel=6, mtime=0)


def _accepts_gzip(scope) -> bool:
    for name, value in scope.get("headers") or ():
        if name == b"accept-encoding":
            return b"gzip" in value.lower()
    return False


def _has_range(scope) -> bool:
    return any(name == b"range" for name, _ in scope.get("headers") or ())


class CachedStaticFiles(StaticFiles):
    """StaticFiles that adds Cache-Control based on the asset's hashing,
    and gzips immutable text/wasm assets for clients that accept it."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code >= 400:
            return response
        cache_control = _cache_control_for(path)
        response.headers["Cache-Control"] = cache_control
        if (
            response.status_code == 200
            and cache_control == IMMUTABLE_CACHE
            and isinstance(response, FileResponse)
            and str(response.path).lower().endswith(_GZIP_TYPES)
            and _accepts_gzip(scope)
            and not _has_range(scope)
        ):
            stat = response.stat_result
            if stat is not None and stat.st_size >= _GZIP_MIN_BYTES:
                key = (str(response.path), stat.st_mtime_ns, stat.st_size)
                body = _GZIP_CACHE.get(key)
                if body is None:
                    body = await asyncio.to_thread(_gzip_file, str(response.path))
                    _GZIP_CACHE.put(key, body)
                headers = {
                    "Cache-Control": cache_control,
                    "Content-Encoding": "gzip",
                    "Vary": "Accept-Encoding",
                }
                etag = response.headers.get("etag")
                if etag:
                    headers["ETag"] = etag
                if response.headers.get("last-modified"):
                    headers["Last-Modified"] = response.headers["last-modified"]
                return Response(body, media_type=response.media_type, headers=headers)
        return response


# 08-1: one list of browser shells drives *both* halves of the gate — the
# redirect check and the entry-document set — so a newly mounted shell cannot
# be half-gated. It was: /admin is mounted in app/main/app.py and was left out
# here, so any anonymous visitor got the control-panel shell (its API is
# admin-gated, so nothing leaked, but the surface advertised itself as public).
# Keep this tuple in step with the mounts in app/main/app.py; the hygiene suite
# asserts that every mounted SPA appears in it.
_SPA_SHELL_PREFIXES = ("/viewer", "/studies-ui", "/settings", "/admin")
# Entry documents (mount root, trailing slash, index) — a deep link lands on one
# of these, so the session is validated in the DB rather than just its cookie's
# shape. "/viewer/viewer" is a legacy alias route of the /viewer block.
_SPA_ENTRY_PATHS = frozenset(
    {"/", "/viewer/viewer"}
    | {
        entry
        for prefix in _SPA_SHELL_PREFIXES
        for entry in (prefix, f"{prefix}/", f"{prefix}/index.html")
    }
)
# Static assets under the SPA mounts: the PAXS bundle chunks, wasm, configs.
# They are only requested *after* the entry HTML passed the gate, so a cheap
# structural cookie check is enough — no DB hit per bundle chunk.
_SPA_ASSET_EXT = {".js", ".css", ".wasm", ".json", ".map", ".png", ".jpg",
                  ".svg", ".woff", ".woff2", ".ttf", ".ico", ".txt"}


def _looks_like_asset(path: str) -> bool:
    stem, _, ext = path.rsplit("/", 1)[-1].rpartition(".")
    return bool(stem) and f".{ext}" in _SPA_ASSET_EXT


def _is_share_link(path: str, query: str) -> bool:
    """True for the exact legacy share parameter on a viewer URL.

    Scoped to /viewer on purpose: a share token authorizes reads of exactly one
    study (AGENTS.md §7) and says nothing about the other browser shells. While
    the bypass was path-agnostic, ``/admin?share=anything`` served the control
    panel shell to an anonymous visitor — re-opening 08-1 through the query
    string. The token itself is still verified where it is consumed.
    """
    values = parse_qs(query, keep_blank_values=True).get("share", [])
    if not any(values):
        return False
    return path == "/viewer" or path.startswith("/viewer/")


def _is_spa_shell(path: str) -> bool:
    """True when ``path`` is a mounted browser shell or anything beneath it.

    A prefix match would let "/settingsfoo" through, so the separator is part of
    the test: exact, or ``prefix + "/"`` and then anything.
    """
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in _SPA_SHELL_PREFIXES
    )


# Global security headers (auth plan §4.2). Applied to every HTTP response
# via a plain ASGI send-wrapper (no body buffering). The CSP allows inline
# script/style because the dashboard + PAXS bundles rely on it; everything
# else stays same-origin, images allow data:/blob: (renderer blobs).
_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "worker-src 'self' blob:; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
# HSTS must only be sent over HTTPS (RFC 6797 §7.2); sending it over plain
# HTTP confuses browsers and can break LAN access after a protocol switch.
if settings.HTTPS_ENABLED:
    _SECURITY_HEADERS["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    """Attach the global security headers to every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        query = scope.get("query_string", b"").lower()
        request_headers = {
            key.lower(): value for key, value in scope.get("headers", [])
        }
        is_share_request = (
            path == "/api/v1/share"
            or path.startswith("/api/v1/share/")
            or b"share=" in query
            or b"paxs_share=" in request_headers.get(b"cookie", b"").lower()
            or b"x-paxs-share-token" in request_headers
        )
        if not settings.SECURITY_HEADERS_ENABLED and not is_share_request:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if settings.SECURITY_HEADERS_ENABLED:
                    for key, value in _SECURITY_HEADERS.items():
                        headers[key] = value
                    if (path.startswith("/viewer/") or path == "/viewer") and not is_share_request:
                        content_type = headers.get("content-type", "")
                        if content_type.startswith("text/html"):
                            headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=()"
                if is_share_request:
                    headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, send_wrapper)


class ViewerAuthGateMiddleware:
    """Redirect unauthenticated browser-shell requests to /login.

    Shells: /viewer, /studies-ui, /settings and /admin (``_SPA_SHELL_PREFIXES``);
    "/" is the legacy dashboard entry.

    Checks the HttpOnly refresh-token cookie (auth plan §2.3):
      * HTML entry points (/, index.html, /viewer) are validated against the
        sessions table — revoked/expired sessions are redirected to /login;
      * static assets (bundle chunks, wasm, fonts) only need a structurally
        plausible cookie — they are fetched after the entry page passed the
        gate, so a per-chunk DB hit would be wasted;
      * valid legacy share links redirect to the exchange route; a valid share
        cookie then authorizes only the clean /viewer entry;
      * plain ASGI (not BaseHTTPMiddleware) so FileResponse/StreamingResponse
        pass through untouched.
    Inert while AUTH_GATE_ENABLED is false (checked per request).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not settings.AUTH_GATE_ENABLED:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        gated = path == "/" or _is_spa_shell(path)
        if not gated:
            await self.app(scope, receive, send)
            return
        if _looks_like_asset(path):
            await self.app(scope, receive, send)
            return
        query = scope.get("query_string", b"").decode("latin-1")
        if _is_share_link(path, query):  # externally-delivered patient share link
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        token = _cookie_value(headers.get("cookie", ""), settings.AUTH_COOKIE_NAME)
        if path in _SPA_ENTRY_PATHS and path.startswith("/viewer"):
            share_cookie = _cookie_value(headers.get("cookie", ""), "paxs_share")
            if share_cookie:
                from starlette.requests import Request

                from app.api.v1.deps import get_share_scope
                if await get_share_scope(Request(scope)) is not None:
                    await self.app(scope, receive, send)
                    return
        if not token:
            await _redirect_to_login(scope, receive, send, path, query)
            return
        if path in _SPA_ENTRY_PATHS:
            # Entry point or deep link — validate the session in the DB.
            from starlette.requests import Request

            from app.api.v1.deps import get_current_user_from_cookie
            user = await get_current_user_from_cookie(Request(scope))
            if user is None:
                await _redirect_to_login(scope, receive, send, path, query)
                return
        await self.app(scope, receive, send)


async def _redirect_to_login(
    scope: Scope, receive: Receive, send: Send, path: str, query: str
) -> None:
    """303 the browser to /login, preserving where it was going.

    V2-4: this is a browser-facing redirect, so it must carry the mount prefix
    (otherwise /paxs/studies-ui bounces to /login, outside the app). Both the
    missing-cookie and the failed-DB-check paths use it, so the prefix rule is
    stated once instead of twice.
    """
    from app.core.proxy_prefix import prefix_path

    root = scope.get("root_path") or ""
    next_url = quote(prefix_path(root, path) + (f"?{query}" if query else ""), safe="")
    response = RedirectResponse(
        f"{prefix_path(root, '/login')}?next={next_url}", status_code=303
    )
    await response(scope, receive, send)


def _cookie_value(cookie_header: str, name: str) -> str:
    """Pull one cookie value out of a raw Cookie header ('' when absent)."""
    if not cookie_header:
        return ""
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
        morsel = jar.get(name)
        return morsel.value if morsel else ""
    except (CookieError, ValueError, KeyError) as exc:  # malformed cookie -> treat as absent
        log.debug("Cookie parse failed for %s (error_type=%s); treating as absent", name,
                  type(exc).__name__)
        return ""


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Applies a sliding-window per-IP cap to every request."""

    def __init__(self, app, rate_limiter):
        super().__init__(app)
        self._rate_limiter = rate_limiter

    async def dispatch(self, request: Request, call_next):
        # Static assets and rendered frames are requested in bursts by the
        # viewer (a 200-image study load); never let the global cap throttle
        # those — the per-endpoint auth limiter is the real brute-force gate.
        if request.url.path.startswith(("/static", "/dicom-web")):
            return await call_next(request)
        try:
            await self._rate_limiter(request)
        except HTTPException as exc:
            # BaseHTTPMiddleware.dispatch must return a Response (an
            # HTTPException would crash the server instead of returning 429).
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
        return await call_next(request)


class RuntimeCORSMiddleware:
    """Apply the current CORS policy rather than a startup-time snapshot.

    ``CORS_ORIGINS`` is editable by administrators. Constructing Starlette's
    middleware per request keeps that runtime override effective without
    mutating shared middleware state while requests are in flight.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        cors = CORSMiddleware(
            self.app,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        await cors(scope, receive, send)
