"""PAXS Archive Server — FastAPI application entry point.

The application lives in :mod:`app.main.app`; this package re-exports it so
``uvicorn app.main:app`` and ``from app.main import app`` keep working (FA-3).

| module       | owns                                                    |
| ------------ | ------------------------------------------------------- |
| `app`        | the FastAPI instance, router wiring, SPA mounts          |
| `middleware` | gzip / cache / security-header / auth-gate / rate limit  |
| `startup`    | the lifespan (schema, workers, DIMSE + HL7, sweeps)      |

Startup (lifespan):
  1. create DB schema
  2. start the background compression/thumbnail worker
  3. start the DICOM DIMSE receiver (C-STORE / C-FIND / C-GET) on :10400
"""

from __future__ import annotations

from app.main.app import app

__all__ = ["app"]
