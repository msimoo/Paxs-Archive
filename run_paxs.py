"""PAXS Archive Server — unified entry point.

The DICOM receiver and background worker are started automatically by the
FastAPI lifespan, so a single process runs everything:

    python run_paxs.py
"""

import os
import ssl
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()  # read .env before settings/os.getenv
except ImportError:
    pass

import uvicorn

from app.main import app
from app.core.config import is_production_environment, settings

def _is_production() -> bool:
    """H06: production means fail-closed startup and no auto-reload.

    Shares ``app.core.config.is_production_environment`` so the runner and the
    application startup gate can never disagree about what "production" means.
    """
    return is_production_environment(settings)


def _load_ssl_context():
    """Return the uvicorn TLS kwargs if HTTPS is enabled and usable, else None.

    H06: a certificate that is present but unreadable is just as fatal as a
    missing one — both would otherwise degrade to plaintext silently.
    """
    if not settings.HTTPS_ENABLED:
        return None
    cert = Path(settings.HTTPS_CERT).resolve()
    key = Path(settings.HTTPS_KEY).resolve()
    missing = [str(p) for p in (cert, key) if not p.exists()]
    unreadable = [str(p) for p in (cert, key) if p.exists() and not os.access(p, os.R_OK)]
    if not missing and not unreadable:
        return {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    # H06: never silently downgrade to plaintext in production.
    problems = []
    if missing:
        problems.append("missing: " + ", ".join(missing))
    if unreadable:
        problems.append("unreadable: " + ", ".join(unreadable))
    message = f"HTTPS is enabled but the certificate/key are unusable ({'; '.join(problems)})"
    if _is_production():
        raise SystemExit(
            f"[FATAL] {message} - refusing to start. "
            "Set HTTPS_ENABLED=false explicitly to opt out."
        )
    print(f"[WARN] {message} - continuing over HTTP (ENVIRONMENT != production)")
    print("       Falling back to plain HTTP.")
    return None

if __name__ == "__main__":
    host = os.getenv("PAXS_HOST", "0.0.0.0")
    port = int(os.getenv("PAXS_PORT", "8000"))
    ssl_kw = _load_ssl_context() or {}
    proto = "https" if ssl_kw else "http"
    print(f"[PAXS] Starting on {proto}://{host}:{port}")
    if ssl_kw:
        print(f"[PAXS] TLS enabled — certificate: {ssl_kw['ssl_certfile']}")
    # V2-4: reverse-proxy support.
    #   proxy_headers/forwarded_allow_ips make uvicorn trust X-Forwarded-Proto
    #   and X-Forwarded-For. The default allow-list is loopback only, so a proxy
    #   in another container (Docker/HAProxy) is ignored until it is listed —
    #   set PAXS_FORWARDED_ALLOW_IPS to the proxy address, subnet, or "*".
    #   root_path is the static sub-path the app is mounted under; the per-request
    #   equivalent is X-Forwarded-Prefix, handled by ForwardedPrefixMiddleware.
    forwarded_allow_ips = os.getenv("PAXS_FORWARDED_ALLOW_IPS", "127.0.0.1")
    root_path = os.getenv("PAXS_ROOT_PATH", "")
    if root_path:
        print(f"[PAXS] Mounted under sub-path: {root_path}")
    if forwarded_allow_ips != "127.0.0.1":
        print(f"[PAXS] Trusting X-Forwarded-* from: {forwarded_allow_ips}")
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        log_level="info",
        reload=not _is_production(),
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        root_path=root_path,
        **ssl_kw,
    )
