"""PAXS Archive Server — FastAPI application entry point.

Startup (lifespan):
  1. create DB schema
  2. start the background compression/thumbnail worker
  3. start the DICOM DIMSE receiver (C-STORE / C-FIND / C-GET) on :10400
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from http.cookies import SimpleCookie
from urllib.parse import quote
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.db.session import close_db, init_db
from app.dicom.receiver import shutdown_receiver, start_receiver_in_thread
from app.services.worker import start_worker, stop_worker
from app.api.ratelimit import rate_limit
from app.dicom.hl7 import shutdown_hl7, start_hl7_listener
from app.services.settings_service import apply_db_overrides

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("paxs.main")

STATIC_DIR = Path(__file__).parent / "static"


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
    r"^(text/|application/(json|javascript|x-javascript|xml|xhtml\+xml|problem\+json)"
    r"|image/svg\+xml)"
)
_GZIP_MIN_BYTES = 1024
_GZIP_MAX_BYTES = 32 * 1024 * 1024  # never buffer more than this for compression
_BINARY_PATH_PREFIXES = ("/dicom-web/", "/api/v1/volume/", "/api/v1/fusion/", "/api/v1/mpr/")


class GzipCompressionMiddleware:
    """Compress gzip-capable text responses on the fly (ASGI send-wrapper)."""

    def __init__(self, app, min_bytes: int = _GZIP_MIN_BYTES):
        self.app = app
        self.min_bytes = min_bytes

    async def __call__(self, scope, receive, send):
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
                status = message["status"]
                out_headers = MutableHeaders(scope=message)
                ctype = out_headers.get("content-type", "")
                clen = out_headers.get("content-length", "")
                ce = out_headers.get("content-encoding", "")
                state["active"] = (
                    status >= 200
                    and status not in (204, 304)
                    and not ce
                    and bool(_COMPRESSIBLE_TYPE.search(ctype))
                    and clen.isdigit()
                    and self.min_bytes <= int(clen) <= _GZIP_MAX_BYTES
                )
                if state["active"]:
                    state["start"] = message
                    state["headers"] = out_headers
                    return  # hold the start until the body is complete
                await send(message)
                return
            if message["type"] == "http.response.body":
                if not state["active"]:
                    await send(message)
                    return
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                data = b"".join(state["chunks"])
                compressed = gzip.compress(data, compresslevel=6)
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
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)


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
    "init-service-worker.js", "paxs-mpr.js", "paxs-study-list-enhance.js",
    "paxs-study-list-theme.css",
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
    stem, _, ext = name.rpartition(".")
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


class CachedStaticFiles(StaticFiles):
    """StaticFiles that adds Cache-Control based on the asset's hashing."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code < 400:
            response.headers["Cache-Control"] = _cache_control_for(path)
        return response


_SPA_ENTRY_PATHS = {"/", "/viewer", "/viewer/", "/viewer/index.html", "/viewer/viewer",
                    "/studies-ui", "/studies-ui/", "/studies-ui/index.html",
                    "/settings", "/settings/", "/settings/index.html"}
# Static assets under the SPA mounts: the PAXS bundle chunks, wasm, configs.
# They are only requested *after* the entry HTML passed the gate, so a cheap
# structural cookie check is enough — no DB hit per bundle chunk.
_SPA_ASSET_EXT = {".js", ".css", ".wasm", ".json", ".map", ".png", ".jpg",
                  ".svg", ".woff", ".woff2", ".ttf", ".ico", ".txt"}


def _looks_like_asset(path: str) -> bool:
    stem, _, ext = path.rsplit("/", 1)[-1].rpartition(".")
    return bool(stem) and f".{ext}" in _SPA_ASSET_EXT


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

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.SECURITY_HEADERS_ENABLED:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in _SECURITY_HEADERS.items():
                    headers[key] = value
            await send(message)

        await self.app(scope, receive, send_wrapper)


class ViewerAuthGateMiddleware:
    """Redirect unauthenticated /viewer + /studies-ui requests to /login.

    Checks the HttpOnly refresh-token cookie (auth plan §2.3):
      * HTML entry points (/, index.html, /viewer) are validated against the
        sessions table — revoked/expired sessions are redirected to /login;
      * static assets (bundle chunks, wasm, fonts) only need a structurally
        plausible cookie — they are fetched after the entry page passed the
        gate, so a per-chunk DB hit would be wasted;
      * patient share links (/viewer?share=…) stay public — they are
        consumed by external recipients who have no login;
      * plain ASGI (not BaseHTTPMiddleware) so FileResponse/StreamingResponse
        pass through untouched.
    Inert while AUTH_GATE_ENABLED is false (checked per request).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not settings.AUTH_GATE_ENABLED:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        gated = (
            path == "/"
            or path == "/viewer" or path.startswith("/viewer/")
            or path == "/studies-ui" or path.startswith("/studies-ui/")
            or path == "/settings" or path.startswith("/settings/")
        )
        if not gated:
            await self.app(scope, receive, send)
            return
        if _looks_like_asset(path):
            await self.app(scope, receive, send)
            return
        query = scope.get("query_string", b"").decode("latin-1")
        if "share=" in query:  # externally-delivered patient share link
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        token = _cookie_value(headers.get("cookie", ""), settings.AUTH_COOKIE_NAME)
        if not token:
            next_url = quote(path + (f"?{query}" if query else ""), safe="")
            response = RedirectResponse(f"/login?next={next_url}", status_code=303)
            await response(scope, receive, send)
            return
        if path in _SPA_ENTRY_PATHS:
            # Entry point or deep link — validate the session in the DB.
            from app.api.v1.deps import get_current_user_from_cookie
            from starlette.requests import Request
            user = await get_current_user_from_cookie(Request(scope))
            if user is None:
                next_url = quote(path + (f"?{query}" if query else ""), safe="")
                response = RedirectResponse(f"/login?next={next_url}", status_code=303)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _cookie_value(cookie_header: str, name: str) -> str:
    """Pull one cookie value out of a raw Cookie header ('' when absent)."""
    if not cookie_header:
        return ""
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
        morsel = jar.get(name)
        return morsel.value if morsel else ""
    except Exception:
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Widen the default executor before anything starts serving requests —
    # WADO-RS frame/rendered/thumbnail retrieval offloads disk I/O + pydicom
    # decode via asyncio.to_thread(), and asyncio's built-in default pool
    # (min(32, cpu+4)) is too small for a CT/MRI viewer firing dozens of
    # concurrent frame requests per series (see app/api/v1/dicomweb.py).
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=settings.IO_THREAD_POOL_WORKERS, thread_name_prefix="paxs-io")
    )
    await init_db()
    # Replay persisted admin-settings overrides before any service binds, so
    # boot-consumed keys (AE title, ports, toggles) honor the last edit.
    apply_db_overrides()
    if settings.WORKER_ENABLED:
        start_worker()
    if settings.DICOM_RECEIVER_ENABLED:
        start_receiver_in_thread()
    if settings.HL7_ENABLED:
        start_hl7_listener()
    from app.core.licensing import get_license_manager
    _lic = get_license_manager()
    log.info("PAXS License: %s (%s) — Server HW-FP: %s",
             _lic.status.value, _lic.status_message, _lic.get_summary().get("server_fingerprint"))

    async def _heartbeat_loop():
        while True:
            await asyncio.sleep(max(3600, settings.LICENSE_HEARTBEAT_HOURS * 3600))
            try:
                await asyncio.to_thread(_lic.validate_online)
            except Exception as exc:
                log.warning("License background heartbeat error: %s", exc)

    _hb_task = asyncio.create_task(_heartbeat_loop()) if settings.LICENSE_AUTO_VALIDATE_ONLINE else None

    log.info("%s v%s ready — API http://0.0.0.0  DICOM %s:%s%s",
             settings.APP_NAME, settings.APP_VERSION,
             settings.DICOM_HOST, settings.DICOM_PORT,
             f"  HL7 {settings.HL7_HOST}:{settings.HL7_PORT}" if settings.HL7_ENABLED else "")
    yield
    if _hb_task:
        _hb_task.cancel()
    shutdown_receiver()
    stop_worker()
    shutdown_hl7()
    await close_db()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Python DICOM archive & PACS platform (DIMSE + DICOMweb).",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,  # required when allow_origins includes "*"
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global per-IP guard; specific endpoints (auth) add tighter windows.
# The middleware is inert while RATE_LIMIT_ENABLED is false (checked per request).
app.add_middleware(
    RateLimitMiddleware,
    rate_limiter=rate_limit("global", settings.RATE_LIMIT_GLOBAL_PER_MINUTE, 60),
)

# Compress text responses (viewer/dashboard JS/CSS, API JSON) — added last so
# it wraps everything; binary DICOM/voxel paths are skipped inside.
app.add_middleware(GzipCompressionMiddleware)
from app.api.v1 import metrics as metrics_module  # noqa: E402
app.add_middleware(metrics_module.MetricsMiddleware)
# Viewer auth gate — added late so it is the outermost middleware: it decides
# /viewer + /studies-ui access before gzip / metrics / rate-limit ever run.
app.add_middleware(ViewerAuthGateMiddleware)
# Security headers — added last so they wrap everything (outermost).
app.add_middleware(SecurityHeadersMiddleware)

# API routers
from app.api.v1 import (  # noqa: E402
    admin_panel,
    ai,
    auth,
    clinical_protocols,
    commitments,
    config,
    database,
    critical_results,
    dashboard,
    db_panel,
    dicomdir,
    dicomweb,
    dose,
    export,
    fhir,
    fusion,
    gsps,
    hanging_protocols,
    ian,
    kin,
    ldap,
    license,
    lifecycle,
    lossy_compression,
    metrics,
    migration,
    quota,
    pix_pdq,
    mpps,
    nodes,
    prefetch,
    peer_review,
    priors,
    redis_panel,
    reports,
    replication,
    routing,
    scripts,
    share,
    split_merge,
    storage,
    tenants,
    users,
    volume,
    worklist,
    worklist_tasks,
    xdsi,
)

app.include_router(dicomweb.router)
app.include_router(dashboard.router)
app.include_router(metrics.router)
app.include_router(ai.router)
app.include_router(config.router)
app.include_router(database.router)
app.include_router(license.router)
app.include_router(auth.router)
app.include_router(admin_panel.router)
app.include_router(share.router)
app.include_router(export.router)
app.include_router(nodes.router)
app.include_router(worklist.router)
app.include_router(worklist_tasks.router)
app.include_router(mpps.router)
app.include_router(commitments.router)
app.include_router(gsps.router)
app.include_router(migration.router)
app.include_router(split_merge.router)
app.include_router(clinical_protocols.router)
app.include_router(critical_results.router)
app.include_router(routing.router)
app.include_router(kin.router)
app.include_router(lossy_compression.router)
app.include_router(lifecycle.router)
app.include_router(ian.router)
app.include_router(dicomdir.router)
app.include_router(prefetch.router)
app.include_router(dose.router)
app.include_router(xdsi.router)
app.include_router(priors.router)
app.include_router(hanging_protocols.router)
app.include_router(ldap.router)
app.include_router(peer_review.router)
app.include_router(reports.router)
app.include_router(tenants.router)
app.include_router(users.router)
app.include_router(storage.router)
app.include_router(quota.router)
app.include_router(pix_pdq.router)
app.include_router(volume.router)
app.include_router(scripts.router)
app.include_router(replication.router)
app.include_router(db_panel.router)
app.include_router(redis_panel.router)
from app.api.v1 import settings as settings_v1_router  # noqa: E402
app.include_router(settings_v1_router.router)
if settings.FHIR_ENABLED:
    app.include_router(fhir.router)
    app.include_router(fusion.router)

# Web dashboard + viewer
if STATIC_DIR.exists():
    app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")


    @app.get("/", include_in_schema=False)
    async def index():
        response = FileResponse(STATIC_DIR / "index.html")
        response.headers["Cache-Control"] = NO_CACHE
        return response
else:  # pragma: no cover
    log.warning("static/ directory not found — dashboard UI disabled")

# Single-page-app static mount: unknown paths under the mount (deep links the
# client-side router handles) serve index.html instead of 404. Used by both
# the PaxsViewer bundle (/viewer) and the Control Panel bundle (/admin).
class SpaStaticFiles(CachedStaticFiles):
    """StaticFiles with a single-page-app fallback to index.html on 404."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # staticfiles raises starlette.exceptions.HTTPException, which is
            # the *base* of fastapi.HTTPException — catching only the fastapi
            # subclass would let the 404 escape and break client-side deep links.
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise

# PaxsViewer (the PaxsViewer v3 fork) — DICOMweb-based study browser, served
# same-origin from app/static/viewer so its QIDO/WADO/STOW requests to
# /dicom-web need no CORS and ride the archive's own tenant scope. Deep links
# use ?StudyInstanceUIDs= (the standard PaxsViewer mode query param). When the dist
# is absent (fresh clone) the lightweight built-in viewer.html takes over at
# /viewer instead (see deploy/PAXSVIEWER-INTEGRATION.md).
VIEWER_DIR = STATIC_DIR / "viewer"
STUDIES_UI_DIR = STATIC_DIR / "studies-ui"

if VIEWER_DIR.exists():

    @app.get("/viewer", include_in_schema=False)
    @app.get("/viewer/", include_in_schema=False)
    @app.get("/viewer/index.html", include_in_schema=False)
    @app.get("/viewer/viewer", include_in_schema=False)
    async def viewer_root_study_list(request: Request):
        """Serve the upgraded React + Tailwind + shadcn/ui PACS Study List for root /viewer.
        
        Deep-links with ?StudyInstanceUIDs=... pass through to PAXs's DICOM image viewer.
        """
        if request.query_params.get("StudyInstanceUIDs") or request.query_params.get("study"):
            response = FileResponse(VIEWER_DIR / "index.html")
            response.headers["Cache-Control"] = NO_CACHE
            return response
        if STUDIES_UI_DIR.exists():
            response = FileResponse(STUDIES_UI_DIR / "index.html")
            response.headers["Cache-Control"] = NO_CACHE
            return response
        response = FileResponse(VIEWER_DIR / "index.html")
        response.headers["Cache-Control"] = NO_CACHE
        return response

    app.mount("/viewer", SpaStaticFiles(directory=VIEWER_DIR, html=True), name="viewer")
    log.info("PaxsViewer mounted at /viewer (v%s)",
             (VIEWER_DIR / "viewer-version.txt").read_text().strip()
             if (VIEWER_DIR / "viewer-version.txt").exists() else "?")

    # The viewer bundle hardcodes /PaxsViewer-logo.png at the server root for
    # its loading splash AND the study-list / viewer header logos. Serve the
    # PaxsViewer wordmark there (canonical copy in logo/paxsviewer-logo.png),
    # falling back to the PAXS emblem so the header is never a broken image.
    paxsviewer_logo = STATIC_DIR / "logo" / "paxsviewer-logo.png"
    paxs_logo = STATIC_DIR / "logo" / "logo.png"

    @app.get("/PaxsViewer-logo.png", include_in_schema=False)
    async def viewer_splash_logo():
        logo = paxsviewer_logo if paxsviewer_logo.exists() else paxs_logo
        if logo.exists():
            response = FileResponse(logo, media_type="image/png")
            response.headers["Cache-Control"] = DEFAULT_CACHE
            return response
        return Response(status_code=404)

else:

    @app.get("/viewer", include_in_schema=False)
    async def viewer_fallback():
        """Built-in lightweight viewer while the PaxsViewer dist is absent."""
        return FileResponse(STATIC_DIR / "viewer.html")

    log.warning("app/static/viewer not found — using built-in viewer.html "
                "(see deploy/PAXSVIEWER-INTEGRATION.md)")

# PaxsViewer Control Panel — the Electron control-panel renderer served in the
# browser with a web bridge that maps window.electronApi calls to /api/v1/admin-panel.
ADMIN_DIR = STATIC_DIR / "admin"
if settings.ADMIN_PANEL_ENABLED and ADMIN_DIR.exists():

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def admin_panel_page():
        return FileResponse(ADMIN_DIR / "index.html")

    app.mount("/admin", SpaStaticFiles(directory=ADMIN_DIR, html=True), name="admin")
    log.info("PaxsViewer Control Panel mounted at /admin (v%s)",
             (ADMIN_DIR / "admin-version.txt").read_text().strip()
             if (ADMIN_DIR / "admin-version.txt").exists() else "?")

# Modern React + Tailwind CSS + shadcn/ui PACS Study List UI
STUDIES_UI_DIR = STATIC_DIR / "studies-ui"
if STUDIES_UI_DIR.exists():
    @app.get("/studies-ui", include_in_schema=False)
    @app.get("/studies-ui/", include_in_schema=False)
    async def studies_ui_page():
        return FileResponse(STUDIES_UI_DIR / "index.html")

    app.mount("/studies-ui", SpaStaticFiles(directory=STUDIES_UI_DIR, html=True), name="studies-ui")
    log.info("Upgraded PACS Study List UI (shadcn/ui) mounted at /studies-ui")

# Organization Settings & Institutional Reporting Dashboard
SETTINGS_UI_DIR = STATIC_DIR / "settings"
if SETTINGS_UI_DIR.exists():
    @app.get("/settings", include_in_schema=False)
    @app.get("/settings/", include_in_schema=False)
    async def settings_ui_page():
        return FileResponse(SETTINGS_UI_DIR / "index.html")

    app.mount("/settings", SpaStaticFiles(directory=SETTINGS_UI_DIR, html=True), name="settings")
    log.info("Organization Settings Dashboard mounted at /settings")

# Unified login page + logout (auth plan §2.1/§2.3). /login serves a
# self-contained HTML page (app/static/login.html) to every role; an already
# authenticated visitor is 303'd straight to their role landing page (or the
# ?next= target). /logout clears the HttpOnly session cookie.
LOGIN_PAGE = STATIC_DIR / "login.html"


@app.get("/login", include_in_schema=False)
@app.get("/login/", include_in_schema=False)
async def login_page(request: Request):
    from app.api.v1.auth import role_landing
    from app.api.v1.deps import get_current_user_from_cookie

    user = await get_current_user_from_cookie(request)
    if user is not None:
        # Already signed in — jump straight to their landing (or ?next=).
        next_url = request.query_params.get("next")
        return RedirectResponse(role_landing(user.role, next_url), status_code=303)
    if not LOGIN_PAGE.exists():
        raise HTTPException(404, "login page not built")
    response = FileResponse(LOGIN_PAGE)
    response.headers["Cache-Control"] = NO_CACHE
    return response


@app.get("/logout", include_in_schema=False)
async def logout_page():
    from app.core.security import clear_session_cookie

    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


# Terms & Conditions / Privacy Policy (feature-plans/terms.md is the source of
# truth — the dashboard footer popup renders it via GET /terms).
TERMS_PATH = Path(__file__).resolve().parent.parent / "feature-plans" / "terms.md"


@app.get("/terms", include_in_schema=False)
async def terms():
    if not TERMS_PATH.exists():
        raise HTTPException(404, "Terms not found")
    return Response(
        TERMS_PATH.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
    )


# PAXS Viewer user guide (paxs_viewer_explain.md is the source of truth —
# linked from the viewer footer and the MPR hotkeys modal).
PAXS_VIEWER_GUIDE_PATH = Path(__file__).resolve().parent.parent / "paxs_viewer_explain.md"


@app.get("/paxs-viewer-guide", include_in_schema=False)
async def paxs_viewer_guide():
    if not PAXS_VIEWER_GUIDE_PATH.exists():
        raise HTTPException(404, "Viewer guide not found")
    return Response(
        PAXS_VIEWER_GUIDE_PATH.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
    )

