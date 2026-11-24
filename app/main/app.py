"""The FastAPI application: middleware wiring, routers, SPA mounts.

Imported for its side effect — ``app`` is built at import time. `app.main`
re-exports it, and uvicorn is pointed at ``app.main:app``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.ratelimit import rate_limit
from app.api.v1.deps import require_admin
from app.core.config import settings
from app.core.csrf import CsrfProtectionMiddleware
from app.core.proxy_prefix import prefix_path
from app.main.middleware import (
    DEFAULT_CACHE,
    NO_CACHE,
    CachedStaticFiles,
    GzipCompressionMiddleware,
    RateLimitMiddleware,
    RuntimeCORSMiddleware,
    SecurityHeadersMiddleware,
    XdsRequestLimitMiddleware,
    ViewerAuthGateMiddleware,
    install_log_redaction,
    _prefixed_text_asset,
    _spa_shell,
)
from app.main.startup import lifespan

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
install_log_redaction()
log = logging.getLogger("paxs.main")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Python DICOM archive & PACS platform (DIMSE + DICOMweb).",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.add_middleware(RuntimeCORSMiddleware)

# Global per-IP guard; specific endpoints (auth) add tighter windows.
# The middleware is inert while RATE_LIMIT_ENABLED is false (checked per request).
app.add_middleware(
    RateLimitMiddleware,
    rate_limiter=rate_limit("global", settings.RATE_LIMIT_GLOBAL_PER_MINUTE, 60),
)

# Compress text responses (viewer/dashboard JS/CSS, API JSON) — added last so
# it wraps everything; binary DICOM/voxel paths are skipped inside.
app.add_middleware(GzipCompressionMiddleware)
from app.api.v1 import metrics as metrics_module  # noqa: E402 - registered on the line below

app.add_middleware(metrics_module.MetricsMiddleware)
# B-19/FS-10: reject cookie-authenticated state changes (POST/PUT/PATCH/DELETE)
# that lack a valid CSRF token. Bearer-token API clients and the /auth session
# lifecycle are exempt by design (see app/core/csrf.py). Added before the
# prefix middleware so the exempt-path check sees the root_path-stripped path.
app.add_middleware(CsrfProtectionMiddleware)
# Viewer auth gate — added after gzip / metrics / rate-limit, so it runs
# OUTSIDE them and decides /viewer, /studies-ui, /settings and /admin access
# before any of them does work. `add_middleware` prepends, so "added later"
# means "more outer"; see the stack diagram in app/main/middleware.py, and
# tests/test_main_and_middleware_hygiene.py for the asserted order.
app.add_middleware(ViewerAuthGateMiddleware)
# Reverse-proxy prefix (V2-4) — must run OUTSIDE the auth gate, because the gate
# matches on scope["path"]: a non-stripping proxy (Traefik without StripPrefix)
# delivers "/paxs/viewer/..." and the gate would not recognise it. Added after
# the gate so it is more outer; SecurityHeaders is still added last/outermost.
from app.core.proxy_prefix import ForwardedPrefixMiddleware  # noqa: E402 - see the stack comment above

app.add_middleware(
    ForwardedPrefixMiddleware,
    trusted_hosts=settings.FORWARDED_ALLOW_IPS,
    allowed_hosts=settings.ALLOWED_HOSTS,
)
# XDS request size guard runs before request-body parsing; keep it inside the
# security-header wrapper so its early 413 responses receive the usual headers.
app.add_middleware(XdsRequestLimitMiddleware)
# Security headers — added last so they wrap everything (outermost).
app.add_middleware(SecurityHeadersMiddleware)

# API routers
from app.api.v1 import (  # noqa: E402 - routers follow the middleware stack by design
    admin_panel,
    availability,
    ai,
    auth,
    clinical_protocols,
    commitments,
    config,
    critical_results,
    dashboard,
    database,
    db_panel,
    dicomdir,
    dicomweb,
    dictations,
    dose,
    export,
    export_launcher,
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
    mpps,
    nodes,
    peer_review,
    pix_pdq,
    prefetch,
    priors,
    quota,
    redis_panel,
    replication,
    reports,
    routing,
    scripts,
    services_control,
    share,
    split_merge,
    storage,
    tenants,
    transfers,
    users,
    view_state,
    volume,
    worklist,
    worklist_tasks,
    xdsi,
)

app.include_router(dicomweb.router)
app.include_router(availability.router)
app.include_router(view_state.router)
app.include_router(dashboard.router)


@app.get("/api/dimse/queue", dependencies=[Depends(require_admin)])
async def dimse_receiver_queue(response: Response):
    """Return the live receiver queue to archive administrators."""
    from app.dicom.receiver import receiver_queue_snapshot

    response.headers["Cache-Control"] = "no-store"
    return receiver_queue_snapshot()


app.include_router(metrics.router)
app.include_router(ai.router)
app.include_router(config.router)
app.include_router(database.router)
app.include_router(license.router)
app.include_router(auth.router)
app.include_router(admin_panel.router)
app.include_router(share.router)
app.include_router(export.router)
app.include_router(export_launcher.router)
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
app.include_router(dictations.router)
app.include_router(tenants.router)
app.include_router(transfers.router)
app.include_router(users.router)
app.include_router(storage.router)
app.include_router(quota.router)
app.include_router(pix_pdq.router)
app.include_router(volume.router)
app.include_router(scripts.router)
app.include_router(services_control.router)
app.include_router(replication.router)
app.include_router(db_panel.router)
app.include_router(redis_panel.router)
from app.api.v1 import settings as settings_v1_router  # noqa: E402 - registered after the main block

app.include_router(settings_v1_router.router)
if settings.FHIR_ENABLED:
    app.include_router(fhir.router)
# H18: registered independently — turning FHIR off must not remove fusion.
if settings.FUSION_ENABLED:
    app.include_router(fusion.router)

# Web dashboard + viewer
if STATIC_DIR.exists():
    app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")


    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        return _spa_shell(STATIC_DIR / "index.html", request)
else:  # pragma: no cover
    log.warning("static/ directory not found — dashboard UI disabled")

# Single-page-app static mount: unknown paths under the mount (deep links the
# client-side router handles) serve index.html instead of 404. Used by both
# the PaxsViewer bundle (/viewer) and the Control Panel bundle (/admin).
#
# V1-5: the fallback must NOT swallow missing *assets*. Serving index.html for a
# missing .wasm/.js/.css returns 200 with the wrong MIME type, which the browser
# reports as a cryptic "WebAssembly.instantiateStreaming failed" or
# "Unexpected token '<'" far from the real cause — that masking is what made a
# genuinely absent codec file look like a decoding bug. Only extension-less
# (navigation) paths fall back.
_ASSET_EXTENSIONS = {
    ".js", ".mjs", ".cjs", ".css", ".wasm", ".map", ".json", ".jsonld",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".txt", ".xml", ".pdf", ".csv", ".dcm", ".nii", ".npy",
    ".mp4", ".webm", ".mp3", ".wav",
    # Archives / raw binary — never a client-side navigation route.
    ".gz", ".br", ".zip", ".7z", ".tar", ".bin", ".dat",
}


class SpaStaticFiles(CachedStaticFiles):
    """StaticFiles with a single-page-app fallback to index.html on 404.

    The fallback applies only to navigation-style paths (no file extension), so
    a missing bundle chunk / wasm / image still returns an honest 404 instead of
    an HTML document with a 200 (V1-5).
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # staticfiles raises starlette.exceptions.HTTPException, which is
            # the *base* of fastapi.HTTPException — catching only the fastapi
            # subclass would let the 404 escape and break client-side deep links.
            if exc.status_code != 404:
                raise
            # A path that names a file type is an asset request: 404 honestly.
            ext = os.path.splitext(path.rsplit("/", 1)[-1])[1].lower()
            if ext in _ASSET_EXTENSIONS:
                raise
            return await super().get_response("index.html", scope)

# PAXs Viewer (OHIF v3 fork from Viewers-master) — DICOMweb-based diagnostic
# viewer served same-origin at /viewer so its QIDO/WADO/STOW requests to
# /dicom-web need no CORS and ride the archive's own session & tenant scope.
# Deep links use /viewer/viewer?StudyInstanceUIDs=... (and /viewer/diagnostic,
# /viewer/segmentation). The legacy lightweight viewer.html fallback was removed.
def _resolve_viewer_dir(configured: str = "", default_dir: Path | None = None) -> Path:
    """Resolve the active OHIF/PAXs Viewer directory (external Viewers-master or bundled)."""
    fallback = default_dir or (STATIC_DIR / "viewer")
    raw = (configured or "").strip()
    if not raw:
        return fallback
    src = Path(raw).expanduser()
    release_root = src / "packaging" / "release"
    if release_root.is_dir():
        releases = sorted(
            (p for p in release_root.glob("paxs-viewer-*") if p.is_dir() and (p / "index.html").is_file()),
            key=lambda p: p.name,
            reverse=True,
        )
        if releases:
            return releases[0]
    dist_dir = src / "platform" / "app" / "dist"
    if dist_dir.is_dir() and (dist_dir / "index.html").is_file():
        return dist_dir
    if src.is_dir() and (src / "index.html").is_file():
        return src
    log.warning("PAXS_VIEWER_DIR=%s has no built OHIF release; falling back to %s", raw, fallback)
    return fallback


BUNDLED_VIEWER_DIR = STATIC_DIR / "viewer"
VIEWER_DIR = _resolve_viewer_dir(settings.PAXS_VIEWER_DIR, BUNDLED_VIEWER_DIR)
# One binding for the study-list shell: `/viewer` serves it as the study list
# when the deep link carries no StudyInstanceUIDs, so the name has to be bound
# before the /viewer block below and before its own mount further down.
STUDIES_UI_DIR = STATIC_DIR / "studies-ui"

_VIEWER_HELPER_FILES = {
    "paxs-study-list-enhance.js": "application/javascript",
    "paxs-thumb-engine.js": "application/javascript",
    "paxs-study-list-theme.css": "text/css",
}

if VIEWER_DIR.exists() and (VIEWER_DIR / "app-config.js").is_file():

    @app.get("/viewer", include_in_schema=False)
    @app.get("/viewer/", include_in_schema=False)
    @app.get("/viewer/index.html", include_in_schema=False)
    @app.get("/viewer/viewer", include_in_schema=False)
    async def viewer_root_study_list(request: Request):
        """Serve the upgraded React + Tailwind + shadcn/ui PACS Study List for root /viewer.

        Deep-links with ?StudyInstanceUIDs=... pass through to OHIF / PAXs Viewer.
        """
        share_token = request.query_params.get("share")
        if share_token:
            from urllib.parse import urlencode

            if settings.SHARE_LEGACY_VIEWER_LINKS_ENABLED:
                target = prefix_path(
                    request.scope.get("root_path") or "", "/api/v1/share/exchange"
                )
                location = f"{target}?{urlencode({'share': share_token})}"
            else:
                location = prefix_path(request.scope.get("root_path") or "", "/viewer")
            response = RedirectResponse(location, status_code=303)
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        if request.query_params.get("StudyInstanceUIDs") or request.query_params.get("study"):
            shell = VIEWER_DIR / "index.html"
        elif STUDIES_UI_DIR.exists():
            shell = STUDIES_UI_DIR / "index.html"
        else:
            shell = VIEWER_DIR / "index.html"
        return _spa_shell(shell, request)

    @app.get("/viewer/app-config.js", include_in_schema=False)
    async def viewer_app_config(request: Request):
        """PaxsViewer data-source config, prefix-aware under a sub-path (V2-4)."""
        return _prefixed_text_asset(
            VIEWER_DIR / "app-config.js", request, "application/javascript"
        )

    @app.get("/viewer/viewer-version.txt", include_in_schema=False)
    async def viewer_version_txt():
        version_file = VIEWER_DIR / "viewer-version.txt"
        ver = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else VIEWER_DIR.name
        return Response(content=ver + "\n", media_type="text/plain; charset=utf-8", headers={"Cache-Control": NO_CACHE})

    for _helper_name, _helper_mime in _VIEWER_HELPER_FILES.items():
        if not (VIEWER_DIR / _helper_name).is_file() and (BUNDLED_VIEWER_DIR / _helper_name).is_file():
            def _make_helper_route(fname: str, mime: str):
                async def _serve_helper():
                    return FileResponse(BUNDLED_VIEWER_DIR / fname, media_type=mime)
                return _serve_helper
            app.add_api_route(f"/viewer/{_helper_name}", _make_helper_route(_helper_name, _helper_mime), methods=["GET"], include_in_schema=False)

    app.mount("/viewer", SpaStaticFiles(directory=VIEWER_DIR, html=True), name="viewer")
    log.info("PAXs Viewer (OHIF v3) mounted at /viewer from %s", VIEWER_DIR)

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
    @app.get("/viewer/{rest:path}", include_in_schema=False)
    async def viewer_missing(rest: str = ""):
        """Refuse to fall back to the retired legacy HTML viewer when OHIF/PAXs Viewer is absent."""
        raise HTTPException(
            503,
            "PAXs Viewer (OHIF v3) bundle is not installed. Run scripts/sync_paxs_viewer.py "
            "or set PAXS_VIEWER_DIR to your Viewers-master path.",
        )

    log.warning("OHIF/PAXs Viewer bundle not found in %s — run scripts/sync_paxs_viewer.py", VIEWER_DIR)

# PaxsViewer Control Panel — the Electron control-panel renderer served in the
# browser with a web bridge that maps window.electronApi calls to /api/v1/admin-panel.
ADMIN_DIR = STATIC_DIR / "admin"
if settings.ADMIN_PANEL_ENABLED and ADMIN_DIR.exists():

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def admin_panel_page(request: Request):
        # The Control Panel entry HTML embeds the hashed asset names, so it must
        # revalidate on every load — the same policy the /viewer and /studies-ui
        # entries apply. Without this the browser kept serving a stale shell that
        # pointed at chunk files from a previous build.
        return _spa_shell(ADMIN_DIR / "index.html", request)

    app.mount("/admin", SpaStaticFiles(directory=ADMIN_DIR, html=True), name="admin")
    log.info("PaxsViewer Control Panel mounted at /admin (v%s)",
             (ADMIN_DIR / "admin-version.txt").read_text().strip()
             if (ADMIN_DIR / "admin-version.txt").exists() else "?")

# Modern React + Tailwind CSS + shadcn/ui PACS Study List UI
if STUDIES_UI_DIR.exists():
    @app.get("/studies-ui", include_in_schema=False)
    @app.get("/studies-ui/", include_in_schema=False)
    async def studies_ui_page(request: Request):
        return _spa_shell(STUDIES_UI_DIR / "index.html", request)

    app.mount("/studies-ui", SpaStaticFiles(directory=STUDIES_UI_DIR, html=True), name="studies-ui")
    log.info("Upgraded PACS Study List UI (shadcn/ui) mounted at /studies-ui")

# Organization Settings & Institutional Reporting Dashboard
SETTINGS_UI_DIR = STATIC_DIR / "settings"
if SETTINGS_UI_DIR.exists():
    @app.get("/settings", include_in_schema=False)
    @app.get("/settings/", include_in_schema=False)
    async def settings_ui_page(request: Request):
        return _spa_shell(SETTINGS_UI_DIR / "index.html", request)

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
        return RedirectResponse(
            role_landing(user.role, next_url, request.scope.get("root_path") or ""),
            status_code=303,
        )
    if not LOGIN_PAGE.exists():
        raise HTTPException(404, "login page not built")
    # Prefix-aware so the sign-in form and its /api/v1/auth + /static assets
    # resolve when the archive is mounted under a sub-path (V2-4).
    return _spa_shell(LOGIN_PAGE, request)


@app.get("/logout", include_in_schema=False)
async def logout_page(request: Request):
    from app.core.csrf import clear_csrf_cookie
    from app.core.proxy_prefix import prefix_path
    from app.core.security import clear_session_cookie

    response = RedirectResponse(
        prefix_path(request.scope.get("root_path") or "", "/login"), status_code=303
    )
    clear_session_cookie(response)
    clear_csrf_cookie(response)
    return response


# Terms & Conditions / Privacy Policy (feature-plans/terms.md is the source of
# truth — the dashboard footer popup renders it via GET /terms).
TERMS_PATH = Path(__file__).resolve().parent.parent.parent / "feature-plans" / "terms.md"


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
#
# The page is rendered, not served as text/markdown: a browser shows raw
# Markdown as a wall of pipes and asterisks (or downloads it), which made the
# guide unusable from the footer link. `/paxs-viewer-guide.md` keeps the raw
# source reachable for anything that wants the text.
PAXS_VIEWER_GUIDE_PATH = Path(__file__).resolve().parent.parent.parent / "paxs_viewer_explain.md"


def _doc_page_response(body: str, request: Request) -> Response:
    """Serve a rendered doc page, prefix-aware for sub-path deployments."""
    from app.core.proxy_prefix import prefix_markup

    root = request.scope.get("root_path") or ""
    if root:
        body = prefix_markup(body, root)
    response = HTMLResponse(body)
    response.headers["Cache-Control"] = NO_CACHE
    return response


@app.get("/paxs-viewer-guide", include_in_schema=False)
async def paxs_viewer_guide(request: Request) -> Response:
    from app.core.doc_page import file_stamp, render_doc_page

    if not PAXS_VIEWER_GUIDE_PATH.exists():
        raise HTTPException(404, "Viewer guide not found")
    body = render_doc_page(
        PAXS_VIEWER_GUIDE_PATH.read_text(encoding="utf-8"),
        doc_label="دليل PaxsViewer",
        raw_href="/paxs-viewer-guide.md",
        raw_name="paxs_viewer_explain.md",
        updated=file_stamp(PAXS_VIEWER_GUIDE_PATH),
    )
    return _doc_page_response(body, request)


@app.get("/paxs-viewer-guide.md", include_in_schema=False)
async def paxs_viewer_guide_raw() -> Response:
    if not PAXS_VIEWER_GUIDE_PATH.exists():
        raise HTTPException(404, "Viewer guide not found")
    return Response(
        PAXS_VIEWER_GUIDE_PATH.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
    )

