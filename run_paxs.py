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
from app.core.config import settings

def _load_ssl_context():
    """Return (certfile, keyfile) if HTTPS is enabled and certs exist, else None."""
    if not settings.HTTPS_ENABLED:
        return None
    cert = Path(settings.HTTPS_CERT).resolve()
    key = Path(settings.HTTPS_KEY).resolve()
    if cert.exists() and key.exists():
        return {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    print(f"[WARN] HTTPS enabled but certs not found: {cert} / {key}")
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
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        log_level="info",
        reload=True,
        **ssl_kw,
    )
