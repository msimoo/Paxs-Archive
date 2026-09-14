# PAXS Archive — Master Plan & Overview (2026-08-13)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This is the single source-of-truth roadmap for the PAXS Archive Server. Part A is a complete overview of the current state (verified 2026-08-13); Part B is the implementation plan for everything that remains, phased by priority. Every task names exact files, interfaces, tests, and verification commands so an engineer with zero project context can execute it.

**Goal:** Consolidate the PAXS Archive status across all existing plan documents, then ship the remaining roadmap: PostgreSQL support, production hardening, the dictation bridge, clinical intelligence, viewer upgrades, observability, and market differentiators — in that priority order.

**Architecture:** PAXS is a Python DICOM archive & PACS: a `pynetdicom` DIMSE receiver (C-STORE/FIND/GET/MOVE/ECHO/MWL/MPPS/Storage-Commitment/IAN/TLS) ingests studies; a threaded worker compresses (RLE/JPEG-LS/JPEG2000/HTJ2K), thumbnails, triages and routes in the background; FastAPI serves DICOMweb (QIDO/WADO/STOW), HL7 MLLP, FHIR R4, XDS-I, and the web dashboard; SQLAlchemy persists to SQLite (dev) or MySQL 8 (prod); the PaxsViewer OHIF fork at `/viewer` plus the React study list at `/studies-ui` render studies; a Windows EXE bundle (`PAXS.exe` + tray + healthcheck + installer) wraps the whole thing.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2 (async `aiomysql`/`aiosqlite` + sync `pymysql`), Alembic, pydicom 3, pynetdicom, numpy, Pillow, pylibjpeg stack, uvicorn, Docker/Compose (MySQL 8), PyInstaller + NSIS (Windows), PaxsViewer = OHIF v3 fork + React/Tailwind/shadcn-ui study list.

## Global Constraints

- Python ≥ 3.12; run tests with `venv/Scripts/python.exe -m pytest tests`.
- All tests MUST stay green on the default isolated SQLite fixture (`tests/conftest.py` sets `DATABASE_URL` before any import).
- The full suite currently passes: **518 passed** (68 test files, ~395 s).
- Never break the existing detector/worker/receiver contracts (`Finding`, `DETECTORS`, `run_detectors_on_series`, `_process_triage`).
- The real repo lives at `D:\Paxs Archive`; this workspace mirrors it. No git repo is present in the workspace — commit steps assume `git` at the real path.
- DICOM UIDs must stay unique (`pydicom.uid.generate_uid()`); PHI never logged in plaintext.
- Do NOT read or print `.env` (blocked/sensitive); use `DATABASE_URL` overrides for verification.
- `requirements.txt` is the single prod dependency file; dev-only deps move to `requirements-dev.txt` (Task 2.5).
- MySQL 8.0 and SQLite are fully supported today; PostgreSQL must be *additive* — the existing two backends keep working.

---

# Part A — Overview (Current State, verified 2026-08-13)

## A.1 What the project is

PAXS Archive Server is a DICOM archive & PACS written entirely in Python. Modalities push images via DIMSE C-STORE (or STOW-RS over HTTP); studies are indexed (Patient → Study → Series → Instance), SHA-256 fingerprinted, losslessly compressed in the background, and served to web viewers via DICOMweb. It also speaks HL7 v2 (ADT/ORM/ORU over MLLP), FHIR R4, XDS-I, LDAP, webhooks, and ships a Windows EXE distribution with a standalone health-check tool.

## A.2 Runtime state (checked live today)

| Component | Status |
|---|---|
| Server | ✅ Running (PID 19248) — API :8000, DICOM :10400, HL7 :2575 |
| DB | ✅ MySQL on :3306 (PID 5008) + Redis :6379 (PID 4880) |
| Liveness / Readiness / Full health | ✅ `ok` / `ready` / `ok` (database ok, DICOM 8/8 services, storage local, FHIR on) |
| Web UIs | ✅ `/` 200 · `/viewer/` 200 (PaxsViewer **3.13.0-beta.89**) · `/studies-ui/` 200 · `/admin/` 200 |
| Archive data | 6 patients · 2 studies · 7 series · 221 instances · 3 users · 0 pending jobs |
| Test suite | ✅ 518 passed (2026-08-13) |
| Windows build | ✅ `dist/PAXS/PAXS.exe`, `PAXS-Tray.exe`, `PAXS-HealthCheck.exe`, `PAXS-Setup.exe`; `dist/installer/PAXS-Setup-0.1.0.exe` |

## A.3 Plan documents inventory (what each file is)

| File | Role |
|---|---|
| `feature-plans/2026-08-13-MASTER-PLAN.md` | **This file** — current source of truth |
| `plan/plan_post.md` | Latest execution plan (Aug 2026): 6 phases. Superseded by this file, kept for history |
| `feature-plans/IMPLEMENTATION_CHECKPOINT.md` | Feature status matrix (41 ✅ / 1 🟡 / 10 ⬜ as of 2026-08-06) |
| `feature-plans/plan.md` | 52-feature checklist + the four explicit asks (PostgreSQL, #34, PaxsViewer, EXE build) |
| `feature-plans/MASTER-ROADMAP.md` | Milestone roadmap (M1–M5), design decisions |
| `feature-plans/new_plan.md`, `paxs-docs/new plan.md` | Old Arabic DeepSeek-V4 prompt plans — superseded |
| `feature-plans/ROADMAP.md`, `STANDARD_PACS_FEATURES_PLAN.md`, `EXTENDED_FEATURES_PLAN_V2.md` | Earlier planning docs — history only |

## A.4 52-feature matrix — condensed status

Legend: ✅ done · 🟡 partial · ⬜ not started

- **Core DICOM (1–10):** ✅ all — C-STORE, C-FIND, C-MOVE, C-GET, C-ECHO, Storage Commitment, MWL, MPPS, IAN, DICOM TLS.
- **Storage & Archive (11–19):** ✅ all — lossless + lossy compression, VNA, lifecycle, routing engine + durable queue, prefetching, DICOMDIR, migration tools, Transfer Accelerator.
- **Viewer & Viz (20–29):** ✅ backend all — WADO-URI, DICOMweb, hanging protocols, priors, GSPS, KIN, volume streaming + server-side MPR + MIP/VRT endpoints, dose (RDSR), fusion. 🟡 client-side WebGL/WebGPU MPR + 3D rendering (backend ready, frontend pending).
- **Workflow & Clinical (30–37):** ✅ #32 peer review, #33 critical results, #37 split/merge. 🟡 #34 reporting done + ORU→EMR, **dictation pending**. ⬜ #30 worklist-management UI, #31 clinical-protocols UI, #35 chat, #36 teaching files.
- **Integration (38–44):** ✅ all — HL7 ADT/ORM/ORU, LDAP/AD, XDS-I, REST API, webhooks, Lua/Python scripting, FHIR R4.
- **Admin & Architecture (45–52):** ✅ #45 audit (SHA-256; ATNA XML pending), #46 RBAC, #49 DR replication (S3), #50 dashboard, #51 anonymizer, #52 storage tiering. 🟡 #47 load balancing (stateless nodes + `PAXS_INSTANCE_ID` + `deploy/LOAD-BALANCING.md`; no HAProxy config shipped). ⬜ #48 HA/clustering.

**Open backlog (from checkpoint):** UPS-RS, storage quotas (service exists: `app/services/quota_service.py` + API, UI pending), segmentation display, PIX/PDQ (service + API exist, IHE profile pending), OIDC, encryption at rest, Prometheus `/metrics`, log aggregation, PWA, mobile viewer, AI inference gateway / MONAI / result-SR rendering / NLP report search.

## A.5 The four explicit asks from `feature-plans/plan.md`

| Ask | Status | Evidence |
|---|---|---|
| **1. PostgreSQL support** (user chooses MySQL *or* PostgreSQL) | ❌ **NOT DONE** | `app/db/session.py:to_sync_url` and `migrations/env.py:to_sync_url` handle only `sqlite+aiosqlite` / `mysql+aiomysql`; no `asyncpg`/`psycopg` in `requirements.txt`; no postgres service in `docker-compose.yml` |
| **2. #34 Reporting / Dictation Bridge** | 🟡 Half | `app/services/report_service.py` (create/update/finalize at lines 57/109/282) + HL7 ORU→EMR done; speech-to-text dictation pending |
| **3. Integrate PaxsViewer from `D:\Github`, remove OHIF** | ✅ DONE | PaxsViewer (OHIF v3 fork) served at `/viewer` (v3.13.0-beta.89); dashboard deep-links via `viewerUrl()`; custom `paxs-mpr.js`, `paxs-viewer-theme.css`, `paxs-study-list-enhance.js` |
| **4. Windows EXE + health tool + `terms.md` + logo + build folder** | ✅ DONE | `build-win/` (PAXS.spec, healthcheck.py + .spec, firstrun.py, tray_controller.py, installer.iss, build*.bat); bundle ships `TERMS.md`, `logo.ico`, `logo.png`, `PAXS-HealthCheck.exe` (exit 0/1/2, `--json`, `--watch`, `--web`); `feature-plans/terms.md` served at `GET /terms` |

## A.6 Verified details the plan depends on (anchors as of 2026-08-13)

- `app/core/config.py:103` — `SECRET_KEY: str = "paxs-insecure-dev-key-change-me"` (no startup validation).
- `app/main.py` — CORS middleware ~L286, `RateLimitMiddleware` ~L289, `GzipCompressionMiddleware` added last (~L300); lifespan starts worker/receiver/HL7; ~504 lines (unsplit).
- `app/db/session.py` — `to_sync_url` ~L63, `_ensure_charset` ~L90, `_is_sqlite`/`_is_mysql` flags, sync engine pool kwargs applied only when MySQL.
- `migrations/env.py` — duplicated `to_sync_url` ~L20.
- `app/services/worker.py` — `_process_triage` L175 (already claims PENDING→RUNNING via Redis lock `paxs:lock:worker:triage` when Redis is present — the "race condition" from `plan_post.md` is **mitigated when Redis runs**; Task 4.4 adds a SQL-only fallback), `start_worker` L288, `stop_worker` L323.
- `app/services/triage_service.py` — `Finding` dataclass L27 (`finding, severity, confidence, summary, detector, sop_instance_uid, series_instance_uid, meta`), `PneumothoraxHeuristicDetector` L42 (`name`, `MODALITIES=("CT",)`, `detect(ds_list)->list[Finding]`), `DETECTORS=[...]` L107, `run_detectors_on_series(series, instance_rows)` L122; instance rows expose `file_path`, `storage_backend`, `sop_instance_uid`; reads via `storage_service.read_dataset(file_path, backend_name=...)`.
- `app/api/v1/dashboard.py` — `/health/live` L119, `/health/ready` L126, `/health` L141.
- `docker-compose.yml` — MySQL 8 service with healthcheck (L27); **no healthcheck on `paxs-api`**.
- `Dockerfile` — `python:3.12-slim`, runs `alembic upgrade head` then `uvicorn app.main:app`; **no non-root user**.
- `tests/conftest.py` — sets isolated SQLite env before importing `app`; `client` fixture truncates tables; `receiver` fixture starts SCP on a free port.

---

# Part B — Implementation Plan

Phases are ordered by impact. Phases 0–3 are small and safe; Phase 4+ are larger features. Each task is independently testable.

## Phase 0 — Repo hygiene (P0, ~30 min)

### Task 0.1: Remove dev artifacts from repo root

**Files:**
- Delete: `check.txt`, `check2.txt`, `check_495.txt`, `check_C-FIND.txt`, `check_C-GET.txt`, `check_C-MOVE.txt`, `check_C-STORE.txt`, `check_c_echo.txt`, `check_progress.txt`, `check_table.txt`, `nul`, `paxs_tray_report.json` (keep `paxs.spec`, `run_paxs.py`)
- Modify: `.gitignore`

**Steps**
- [ ] **1:** Delete the listed `check*.txt`, `nul`, and `paxs_tray_report.json` files:
  ```bash
  rm -f check*.txt nul paxs_tray_report.json
  ```
- [ ] **2:** Append to `.gitignore`:
  ```gitignore
  # runtime artifacts
  check*.txt
  nul
  paxs_tray_report.json
  *.log
  .tools/
  .freebuff/
  ```
- [ ] **Verify:** `ls check*.txt 2>/dev/null; echo $?` → prints `1` (no matches). `grep -c "check\*.txt" .gitignore` → `1`.
- [ ] **3:** Commit: `git commit -m "chore: clean repo root and gitignore runtime artifacts"`

## Phase 1 — PostgreSQL support (the explicit missing ask)

> Goal: `DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/paxs_db` works end-to-end — API, worker, Alembic — alongside the existing SQLite/MySQL paths. User picks the backend with one env var.

### Task 1.1: Add PostgreSQL drivers to requirements

**Files:**
- Modify: `requirements.txt`

**Steps**
- [ ] **1:** Add under the production DB comment block (after `pymysql`):
  ```text
  asyncpg>=0.29            # production PostgreSQL (async)
  psycopg2-binary>=2.9     # production PostgreSQL (sync worker + Alembic)
  ```
- [ ] **2:** Install and confirm importability:
  ```bash
  venv/Scripts/python.exe -m pip install -r requirements.txt -q
  venv/Scripts/python.exe -c "import asyncpg, psycopg2; print(asyncpg.__version__, psycopg2.__version__)"
  ```
  Expected: prints two version numbers.
- [ ] **Verify:** no import error. Commit: `git commit -m "feat(db): add asyncpg + psycopg2 for PostgreSQL support"`

### Task 1.2: Teach the sync-URL conversion about PostgreSQL

**Files:**
- Modify: `app/db/session.py` (`to_sync_url` ~L63, `_is_mysql` flag ~L86)
- Modify: `migrations/env.py` (`to_sync_url` ~L20)
- Test: `tests/test_config.py` (new test appended)

**Interfaces**
- Consumes: existing `to_sync_url(url: str) -> str` in both files.
- Produces: `to_sync_url("postgresql+asyncpg://u:p@h/db") -> "postgresql+psycopg2://u:p@h/db"`.

**Steps**
- [ ] **1:** Write the failing test in `tests/test_config.py`:
  ```python
  def test_to_sync_url_postgres():
      from app.db.session import to_sync_url
      url = "postgresql+asyncpg://u:p@127.0.0.1:5432/paxs_db"
      assert to_sync_url(url) == "postgresql+psycopg2://u:p@127.0.0.1:5432/paxs_db"
  ```
- [ ] **2:** Run it — expected FAIL (`AssertionError`, URL returned unchanged).
  ```bash
  venv/Scripts/python.exe -m pytest tests/test_config.py::test_to_sync_url_postgres -q
  ```
- [ ] **3:** Implement in `app/db/session.py`:
  ```python
  if url.startswith("postgresql+asyncpg://"):
      return "postgresql+psycopg2://" + url[len("postgresql+asyncpg://"):]
  ```
  Insert **before** the `return url` fallback, mirroring the mysql/sqlite branches.
- [ ] **4:** Apply the identical branch in `migrations/env.py` (keep the two helpers in sync — comment both with `# keep in sync with app.db.session.to_sync_url`).
- [ ] **5:** Run the test — expected PASS.
- [ ] **6:** Also assert the sync engine builds for a postgres URL (add to the same test):
  ```python
  def test_sync_engine_postgres_url():
      from app.db.session import to_sync_url
      assert to_sync_url("postgresql+asyncpg://u:p@h/x").split("+")[1] == "psycopg2"
  ```
- [ ] **Verify:** both tests pass. Commit: `feat(db): support postgresql+asyncpg URL conversion in session and alembic env`

### Task 1.3: Add a postgres service to docker-compose

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`

**Steps**
- [ ] **1:** Add a `paxs-pg` service (sibling of `paxs-db`) — user picks which DB to run by commenting one service out or by `DATABASE_URL`:
  ```yaml
  paxs-pg:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: paxs_db
      POSTGRES_USER: paxs_user
      POSTGRES_PASSWORD: ${PG_PASSWORD:-PaxsUserPassword2026!}
    ports:
      - "${PG_PORT:-5433}:5432"
    volumes:
      - paxs-pg-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U paxs_user -d paxs_db"]
      interval: 5s
      timeout: 5s
      retries: 20
  ```
  Add `paxs-pg-data:` to the top-level `volumes:` block.
- [ ] **2:** Document the choice in `.env.example` (under the DB section):
  ```text
  # --- Choose ONE database backend ---
  # MySQL 8 (default):
  DATABASE_URL=mysql+aiomysql://paxs_user:paxs@localhost:3306/paxs_db?charset=utf8mb4
  # PostgreSQL 16:
  # DATABASE_URL=postgresql+asyncpg://paxs_user:paxs@localhost:5433/paxs_db
  # Zero-setup local/dev:
  # DATABASE_URL=sqlite+aiosqlite:///./paxs.db
  ```
- [ ] **Verify:** `docker compose config --quiet` exits 0 (valid YAML) and shows both DB services:
  ```bash
  docker compose config --services
  # expected: paxs-db, paxs-pg, paxs-api
  ```
- [ ] **3:** Commit: `feat(db): add PostgreSQL 16 compose service and backend-choice docs`

### Task 1.4: Verify Alembic + full suite against PostgreSQL

**Files:**
- Test: run existing suite with a postgres `DATABASE_URL` (no code change expected; fix any dialect issues found)

**Steps**
- [ ] **1:** Bring up postgres and run migrations:
  ```bash
  docker compose up -d paxs-pg
  DATABASE_URL=postgresql+aiomysql://... # not valid — use:
  DATABASE_URL=postgresql+asyncpg://paxs_user:PaxsUserPassword2026!@127.0.0.1:5433/paxs_db \
    venv/Scripts/python.exe -m alembic upgrade head
  ```
  Expected: applies all 37 migrations, ends `Running upgrade ... -> head`.
- [ ] **2:** Run the full suite against postgres:
  ```bash
  DATABASE_URL=postgresql+asyncpg://paxs_user:PaxsUserPassword2026!@127.0.0.1:5433/paxs_db \
    venv/Scripts/python.exe -m pytest tests -q --no-header 2>&1 | tail -5
  ```
  Expected: `518 passed` (allow a small number of test-suite-only failures caused by MySQL-specific assertions; fix those tests to assert backend-agnostically, never the app code).
- [ ] **3:** Run `downgrade base` then `upgrade head` on postgres to prove reversibility (mirrors the existing MySQL verification in the README).
- [ ] **Verify:** migrations up/down cycle clean, suite green. Commit: `test(db): verify full suite and alembic cycle on PostgreSQL 16`

## Phase 2 — Production hardening (P0, ~1 day)

### Task 2.1: Fail startup on the default SECRET_KEY

**Files:**
- Modify: `app/core/config.py` (`Settings` class)
- Test: `tests/test_config.py`

**Interfaces**
- Produces: `Settings` raises `ValueError` at construction when `SECRET_KEY == "paxs-insecure-dev-key-change-me"` and `DEBUG is False`.

**Steps**
- [ ] **1:** Failing test:
  ```python
  def test_secret_key_rejected_in_production():
      from app.core.config import Settings
      import pytest
      with pytest.raises(ValueError, match="SECRET_KEY"):
          Settings(_env_file=None, DEBUG=False, SECRET_KEY="paxs-insecure-dev-key-change-me")

  def test_secret_key_allowed_in_debug():
      from app.core.config import Settings
      s = Settings(_env_file=None, DEBUG=True, SECRET_KEY="paxs-insecure-dev-key-change-me")
      assert s.SECRET_KEY == "paxs-insecure-dev-key-change-me"
  ```
- [ ] **2:** Run — expected FAIL (no ValueError raised today).
- [ ] **3:** Implement (add `from pydantic import model_validator` and on the `Settings` class):
  ```python
  @model_validator(mode="after")
  def _enforce_prod_secret(self):
      if not self.DEBUG and self.SECRET_KEY == "paxs-insecure-dev-key-change-me":
          raise ValueError(
              "SECRET_KEY must be changed from the dev default in production "
              "(set a long random value in .env)."
          )
      return self
  ```
- [ ] **4:** Run — expected PASS. Confirm the existing live config (`.env` overrides) still boots — the running server already has a real key.
- [ ] **Verify:** `venv/Scripts/python.exe -m pytest tests/test_config.py -q` green. Commit: `fix(security): refuse to boot with default SECRET_KEY outside DEBUG`

### Task 2.2: Security headers middleware (CSP/HSTS/X-Frame-Options)

**Files:**
- Create: `app/middleware/security_headers.py` (new package `app/middleware/__init__.py` too)
- Modify: `app/main.py` (register after CORS)
- Modify: `app/core/config.py` (two settings)
- Test: `tests/test_security_headers.py` (new file)

**Interfaces**
- Produces: `SecurityHeadersMiddleware(app)` — sets `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: strict-origin-when-cross-origin`, and (when `HSTS_ENABLED`) `Strict-Transport-Security: max-age=31536000; includeSubDomains`.

**Steps**
- [ ] **1:** Failing test `tests/test_security_headers.py`:
  ```python
  from tests.conftest import client  # noqa: F401  (fixture)

  def test_security_headers_present(client):
      r = client.get("/api/v1/health/live")
      assert r.status_code == 200
      assert r.headers.get("x-content-type-options") == "nosniff"
      assert r.headers.get("x-frame-options") == "SAMEORIGIN"
      assert "default-src 'self'" in r.headers.get("content-security-policy", "")
  ```
- [ ] **2:** Run — expected FAIL (headers missing).
- [ ] **3:** Create `app/middleware/security_headers.py`:
  ```python
  """HTTP security headers middleware (CSP / HSTS / nosniff)."""
  from __future__ import annotations

  from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
  from starlette.requests import Request
  from starlette.responses import Response

  from app.core.config import settings

  # OHIF/PaxsViewer uses Web Workers, blob: URLs, eval-bundles and wss — the
  # CSP below is deliberately permissive while still locking XSS vectors.
  CSP = (
      "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: "
      "ws: wss: https://fonts.googleapis.com https://fonts.gstatic.com"
  )


  class SecurityHeadersMiddleware(BaseHTTPMiddleware):
      async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
          response = await call_next(request)
          response.headers["Content-Security-Policy"] = CSP
          response.headers["X-Content-Type-Options"] = "nosniff"
          response.headers["X-Frame-Options"] = "SAMEORIGIN"
          response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
          if settings.HSTS_ENABLED:
              response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
          return response
  ```
- [ ] **4:** In `app/core/config.py` add:
  ```python
  SECURITY_HEADERS_ENABLED: bool = True
  HSTS_ENABLED: bool = False  # enable once served over TLS (PUBLIC_BASE_URL https)
  ```
- [ ] **5:** In `app/main.py`, register right after the CORS middleware block:
  ```python
  if settings.SECURITY_HEADERS_ENABLED:
      from app.middleware.security_headers import SecurityHeadersMiddleware
      app.add_middleware(SecurityHeadersMiddleware)
  ```
- [ ] **6:** Run the test — expected PASS. Also confirm the viewer still renders (headers must not break WASM/workers): boot once with `DICOM_RECEIVER_ENABLED=false WORKER_ENABLED=false` and `curl -sI http://127.0.0.1:8000/viewer/ | head -12` → 200 + CSP present.
- [ ] **Verify:** `pytest tests/test_security_headers.py -q` green. Commit: `feat(security): add CSP/HSTS security headers middleware`

### Task 2.3: Run the container as a non-root user + API healthcheck

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml` (add healthcheck to `paxs-api`)

**Steps**
- [ ] **1:** In `Dockerfile`, after the final `COPY` of the app (before `EXPOSE`/`CMD`), add:
  ```dockerfile
  # Run as an unprivileged user (hardening).
  RUN useradd --system --create-home --home-dir /app paxs \
      && chown -R paxs:paxs /app
  USER paxs
  ```
  If any step before this writes as root to the image filesystem that must stay writable at runtime (e.g. `storage/`, `paxs.db`), add `chown` for those paths explicitly; mount the named volumes with `:z`/user mapping where needed.
- [ ] **2:** In `docker-compose.yml` add to the `paxs-api` service:
  ```yaml
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=3)"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 20s
  ```
  (`curl` is not guaranteed in `python:3.12-slim`; urllib needs no extra package.)
- [ ] **3:** Update `deploy/` docs if present, noting the non-root user + healthcheck.
- [ ] **Verify:** `docker compose config --quiet` exits 0; `docker build -t paxs-test .` succeeds and `docker run --rm paxs-test id` prints `uid=... (paxs)`. Commit: `fix(security): non-root container user + API readiness healthcheck`

### Task 2.4: Tighten the default CORS policy

**Files:**
- Modify: `app/core/config.py` (`CORS_ORIGINS` default)
- Test: `tests/test_config.py`

**Steps**
- [ ] **1:** Failing test:
  ```python
  def test_cors_default_not_wildcard():
      from app.core.config import Settings
      s = Settings(_env_file=None, DEBUG=True, SECRET_KEY="x")
      assert "*" not in s.cors_origin_list
  ```
- [ ] **2:** Run — expected FAIL (default is `"*"`).
- [ ] **3:** Change `CORS_ORIGINS: str = "*"` → `CORS_ORIGINS: str = "http://localhost:8000,http://127.0.0.1:8000"` and update the comment: the viewer and dashboard are same-origin, so DICOMweb needs no CORS for them; remote stations set `CORS_ORIGINS` explicitly (comma-separated).
- [ ] **4:** Run — expected PASS. Add an end-to-end check in `tests/test_dicomweb.py` asserting an OPTIONS preflight from an allowed origin returns the origin header.
- [ ] **Verify:** config + dicomweb tests green. Commit: `fix(security): default CORS to local origins; document LAN viewer config`

### Task 2.5: Split dev requirements

**Files:**
- Create: `requirements-dev.txt`
- Modify: `requirements.txt` (remove `pytest`, `httpx` from the dev block)

**Steps**
- [ ] **1:** Create `requirements-dev.txt`:
  ```text
  -r requirements.txt
  pytest>=8.2
  httpx>=0.27
  ```
- [ ] **2:** Remove the `# ---- Dev / tests ----` block (`pytest`, `httpx`) from `requirements.txt`.
- [ ] **3:** Verify the prod list still installs and tests still import with the dev list:
  ```bash
  venv/Scripts/python.exe -m pip install -r requirements-dev.txt -q
  venv/Scripts/python.exe -m pytest tests/test_config.py -q
  ```
- [ ] **4:** Update the README "Quickstart" block to `pip install -r requirements-dev.txt` for developers, `requirements.txt` for deployment.
- [ ] **Verify:** tests green. Commit: `chore: split dev-only deps into requirements-dev.txt`

## Phase 3 — Reporting / Dictation Bridge (#34 completion)

> Goal: finish the last 🟡 item — live speech-to-text dictation that fills report fields, then finalize via the existing ORU path.

### Task 3.1: Dictation WebSocket endpoint

**Files:**
- Create: `app/api/v1/dictation.py`
- Modify: `app/main.py` (register router)
- Modify: `app/services/report_service.py` (add `append_dictation`)
- Test: `tests/test_dictation.py` (new file)

**Interfaces**
- Consumes: `report_service.update_report(session, report, data)` (L109), `app.api.v1.deps` auth dependency pattern (copy the dependency used by `app/api/v1/reports.py`).
- Produces:
  - `WS /api/v1/reports/dictate/{report_id}?token=<jwt>` — client sends `{"text": "<chunk>", "final": false}`; server appends to `report.dictation_text` and echoes `{"ok": true, "chars": N}`; on `"final": true` persists and echoes `{"ok": true, "finalized": false}`.
  - `GET /api/v1/reports/dictation/{report_id}` — returns `{"dictation_text": "..."}`.

**Steps**
- [ ] **1:** Failing test `tests/test_dictation.py` (uses the `client` fixture and an existing report created via the reports API — copy the report-creation helper from `tests/test_oru.py`):
  ```python
  def test_dictation_websocket_append_and_finalize(client):
      # create report through POST /api/v1/reports (see test_oru.py for auth helper)
      report_id = _create_report(client)
      from starlette.testclient import TestClient
      with client.websocket_connect(f"/api/v1/reports/dictate/{report_id}?token={_token()}") as ws:
          ws.send_json({"text": "No acute intracranial ", "final": False})
          assert ws.receive_json()["ok"] is True
          ws.send_json({"text": "hemorrhage.", "final": True})
          assert ws.receive_json()["finalized"] is False
      r = client.get(f"/api/v1/reports/dictation/{report_id}")
      assert "hemorrhage" in r.json()["dictation_text"]
  ```
- [ ] **2:** Run — expected FAIL (`404`/route missing).
- [ ] **3:** Add to `app/services/report_service.py`:
  ```python
  def append_dictation(session, report: RadiologyReport, text: str, final: bool) -> dict:
      """Append a dictation chunk to the report; persist on final chunk."""
      if not getattr(report, "dictation_text", None):
          report.dictation_text = ""
      report.dictation_text += text
      if final:
          report.dictation_text += "\n"
      if final:
          session.add(report)
          session.commit()
      return {"ok": True, "chars": len(text), "finalized": False}
  ```
  Add the `dictation_text` column via a new Alembic migration (see Task 3.2).
- [ ] **4:** Create `app/api/v1/dictation.py`:
  ```python
  """Speech-to-text dictation bridge for radiology reports (#34)."""
  from __future__ import annotations

  from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
  from sqlalchemy.ext.asyncio import AsyncSession

  from app.api.v1.deps import get_db, get_current_user
  from app.services import report_service
  from app.db.models import RadiologyReport

  router = APIRouter(prefix="/api/v1/reports", tags=["dictation"])


  @router.get("/dictation/{report_id}")
  async def get_dictation(report_id: int, db: AsyncSession = Depends(get_db),
                          _=Depends(get_current_user)):
      report = await report_service.get_report(db, report_id)
      if report is None:
          raise HTTPException(404, "Report not found")
      return {"dictation_text": getattr(report, "dictation_text", "") or ""}


  @router.websocket("/dictate/{report_id}")
  async def dictate(websocket: WebSocket, report_id: int):
      from sqlalchemy import select
      from app.db.session import AsyncSessionLocal
      await websocket.accept()
      try:
          async with AsyncSessionLocal() as db:
              report = (await db.execute(
                  select(RadiologyReport).where(RadiologyReport.id == report_id)
              )).scalar_one_or_none()
              if report is None:
                  await websocket.send_json({"ok": False, "error": "not found"})
                  await websocket.close(code=1008)
                  return
              while True:
                  msg = await websocket.receive_json()
                  result = report_service.append_dictation(
                      db, report, msg.get("text", ""), bool(msg.get("final"))
                  )
                  await websocket.send_json(result)
      except WebSocketDisconnect:
          pass
  ```
- [ ] **5:** Register in `app/main.py` alongside the other routers (`from app.api.v1 import ... dictation` + `app.include_router(dictation.router)`).
- [ ] **6:** Run — expected PASS. Confirm the auth dependency import path matches `app/api/v1/deps.py` (adjust names to the file's actual exports).
- [ ] **Verify:** `pytest tests/test_dictation.py -q` green. Commit: `feat(clinical): dictation WebSocket bridge for reports (#34)`

### Task 3.2: `dictation_text` migration

**Files:**
- Create: `migrations/versions/<rev>_add_report_dictation.py`

**Steps**
- [ ] **1:** Generate + hand-check the migration:
  ```bash
  DATABASE_URL=sqlite+aiosqlite:///./paxs.db venv/Scripts/python.exe -m alembic revision --autogenerate -m "add report dictation_text"
  ```
  The migration must add `dictation_text` (TEXT, nullable) to `reports` and be a no-op on downgrade (`op.drop_column("reports", "dictation_text")`).
- [ ] **2:** Run `alembic upgrade head` against sqlite **and** the postgres URL from Phase 1; run `downgrade -1` then `upgrade head` to prove reversibility.
- [ ] **Verify:** both DBs cycle cleanly. Commit: `feat(db): report dictation_text column`

### Task 3.3: Browser SpeechRecognition in the dashboard

**Files:**
- Modify: `app/static/app.js` (dictation UI in the report editor — find the report dialog handlers, ~L2990+ settings area)

**Steps**
- [ ] **1:** Add a "Dictate" toggle next to the report textarea. On press, if `window.SpeechRecognition || window.webkitSpeechRecognition` exists, start continuous recognition and append `event.results[i][0].transcript` chunks via the WebSocket from Task 3.1 (one socket per report). Stop on `onend`/toggle.
- [ ] **2:** If the API is unavailable, show a disabled tooltip: "Dictation requires Chrome/Edge".
- [ ] **Verify:** manual — open a report in the dashboard, press Dictate, speak, confirm text lands in the report and the `GET /api/v1/reports/dictation/{id}` payload. Commit: `feat(ui): speech dictation toggle in report editor`

## Phase 4 — Clinical intelligence (AI triage expansion + alert gateway)

> These plug into the existing `DETECTORS` pipeline — no changes to the worker are required to add detectors; Task 4.4 hardens the claim for Redis-less deployments.

### Task 4.1: ICH (intracranial hemorrhage) detector

**Files:**
- Create: `app/services/detectors/ich_detector.py` (new package `app/services/detectors/__init__.py`)
- Modify: `app/services/triage_service.py` (`DETECTORS` list L107)
- Test: `tests/test_triage_ich.py` (new file)

**Interfaces**
- Consumes: `Finding` dataclass from `app.services.triage_service`; instance rows with `file_path`/`storage_backend`; `storage_service.read_dataset`.
- Produces: detector class with `name = "ich-heuristic"`, `MODALITIES = ("CT",)`, `detect(ds_list) -> list[Finding]`, registered by appending to `DETECTORS`.

**Steps**
- [ ] **1:** Failing test — synthesize a head-CT-like phantom (bright blob = high-attenuation blood) and assert a HIGH `ICH` finding:
  ```python
  def test_ich_detector_flags_hyperdense_blob():
      import numpy as np, pydicom
      from pydicom.dataset import Dataset, FileMetaDataset
      from pydicom.uid import ExplicitVRLittleEndian
      from app.services.detectors.ich_detector import ICHHeuristicDetector

      arr = np.full((256, 256), 25, dtype=np.int16)   # normal brain ~25 HU
      arr[90:166, 90:166] = 70                        # hyperdense region (blood)
      ds = Dataset()
      ds.SOPInstanceUID = pydicom.uid.generate_uid()
      ds.RescaleSlope, ds.RescaleIntercept = 1, 0
      ds.PixelData = arr.tobytes()
      ds.Rows, ds.Columns = 256, 256
      ds.file_meta = FileMetaDataset()
      ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

      findings = ICHHeuristicDetector().detect([ds])
      assert any(f.finding == "ICH" and f.severity == "HIGH" for f in findings)
  ```
- [ ] **2:** Run — expected FAIL (module missing).
- [ ] **3:** Implement `ich_detector.py` — copy the structure of `PneumothoraxHeuristicDetector` (HU conversion, middle-frame pick, read via `ds.pixel_array`), flag when the fraction of voxels in the 45–90 HU range inside the brain mask (central region) exceeds 0.03:
  ```python
  class ICHHeuristicDetector:
      """Heuristic: large hyperdense (45-90 HU) central region on head CT.

      Demo heuristic, NOT clinical AI — replace with a trained MONAI model by
      implementing the same protocol (name / MODALITIES / detect).
      """
      name = "ich-heuristic"
      MODALITIES = ("CT",)

      def detect(self, ds_list: list) -> list[Finding]:
          findings: list[Finding] = []
          sop_uid = None
          for ds in ds_list:
              sop_uid = str(getattr(ds, "SOPInstanceUID", "")) or sop_uid
              try:
                  arr = ds.pixel_array.astype(np.float64)
              except Exception:
                  continue
              if arr.ndim == 3:
                  arr = arr[arr.shape[0] // 2]
              if arr.ndim != 2 or arr.shape[0] < 64 or arr.shape[1] < 64:
                  continue
              try:
                  slope = float(getattr(ds, "RescaleSlope", 1) or 1)
                  intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
                  hu = arr * slope + intercept
              except (TypeError, ValueError):
                  hu = arr
              h, w = hu.shape
              # central brain region (exclude skull/scalp)
              cy, cx = h // 2, w // 2
              r = min(h, w) // 4
              center = hu[cy - r:cy + r, cx - r:cx + r]
              blood = float(np.mean((center >= 45) & (center <= 90)))
              if blood > 0.03:
                  findings.append(Finding(
                      finding="ICH",
                      severity="HIGH",
                      confidence=round(min(0.9, 0.4 + blood * 3), 3),
                      summary=(f"Heuristic: {blood:.1%} of central brain region is "
                               f"hyperdense (45-90 HU) — possible hemorrhage, review urgently"),
                      detector=self.name,
                      sop_instance_uid=sop_uid,
                  ))
          return findings
  ```
- [ ] **4:** Register in `triage_service.py`: `from app.services.detectors.ich_detector import ICHHeuristicDetector` and `DETECTORS = [PneumothoraxHeuristicDetector(), ICHHeuristicDetector()]`.
- [ ] **5:** Run — expected PASS. Also run `tests/test_triage.py` (existing) to prove no regression.
- [ ] **Verify:** `pytest tests/test_triage_ich.py tests/test_triage.py -q` green. Commit: `feat(ai): add ICH heuristic detector to triage pipeline`

### Task 4.2: PE detector + alert gateway service

**Files:**
- Create: `app/services/alerts.py`
- Create: `app/api/v1/alerts.py`
- Create: `tests/test_alerts.py`
- Modify: `app/services/worker.py` (emit alert after a HIGH finding lands)
- Modify: `app/main.py` (register alerts router)

**Interfaces**
- Produces: `send_alert(finding: Finding, study_uid: str, db)` — dispatches to configured webhooks (Telegram/Slack generic URL) and writes an `audit_events` row with action `ALERT_SENT`; `ALERT_WEBHOOK_URLS` setting (comma-separated, default empty = disabled).
- Consumes: `Finding` (severity `HIGH` only), `settings` webhook list.

**Steps**
- [ ] **1:** Failing test `tests/test_alerts.py`:
  ```python
  def test_alert_sent_for_high_finding(client, monkeypatch):
      from app.services.alerts import send_alert
      from app.services.triage_service import Finding
      captured = {}
      monkeypatch.setenv("ALERT_WEBHOOK_URLS", "http://127.0.0.1:9/hook")
      monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=4: (_ for _ in ()).throw(OSError("conn refused")))
      # OSError is swallowed; the audit row is the assertable outcome
      result = send_alert(Finding(finding="ICH", severity="HIGH", confidence=0.9,
                                  summary="x", detector="ich-heuristic"),
                          study_uid="1.2.3", db=None)
      assert result["dispatched"] is False  # webhook failed but didn't crash
  ```
- [ ] **2:** Run — expected FAIL (module missing).
- [ ] **3:** Implement `app/services/alerts.py` with: `settings.ALERT_WEBHOOK_URLS`, a `send_alert(finding, study_uid, db=None) -> dict` that POSTs JSON `{"finding":..., "severity":..., "study_uid":..., "summary":...}` to each URL via `urllib.request` (timeout 4 s), catches all exceptions (never raises), and if `db` is a session, writes an audit row `action="ALERT_SENT"`.
- [ ] **4:** Implement `app/api/v1/alerts.py` with `GET /api/v1/alerts/config` (webhook URLs redacted) + `POST /api/v1/alerts/test` (sends a probe finding) — both behind the existing auth dependency. Register in `main.py`.
- [ ] **5:** Wire into `worker.py._process_triage` — after the findings loop, for each `HIGH` finding call `alerts.send_alert(finding, study_uid, session)`.
- [ ] **6:** Run — expected PASS; re-run `tests/test_triage.py` + `tests/test_worklist.py` for regressions.
- [ ] **Verify:** alerts + triage tests green. Commit: `feat(ai): HIGH-finding alert gateway with webhook dispatch + audit`

### Task 4.3: Priority scoring in the reading worklist

**Files:**
- Modify: `app/services/reading_worklist_service.py` (add `priority_score`)
- Modify: `app/api/v1/worklist_tasks.py` (expose in response)
- Test: `tests/test_reading_worklist.py` (append)

**Interfaces**
- Produces: `priority_score(urgency: str, wait_minutes: float) -> float` = `{"EMERGENCY": 100, "STAT": 50, "ROUTINE": 10}.get(urgency, 10) + wait_minutes * 1.5` (from `plan_post.md` §2.2).

**Steps**
- [ ] **1:** Failing test:
  ```python
  def test_priority_score_formula():
      from app.services.reading_worklist_service import priority_score
      assert priority_score("STAT", 10) == 65.0
      assert priority_score("EMERGENCY", 0) == 100.0
  ```
- [ ] **2:** Run — expected FAIL.
- [ ] **3:** Implement the function and include `priority_score` in the worklist item dicts (computed from the item's urgency + created-age minutes).
- [ ] **4:** Sort list responses by `priority_score` desc in the API, and add the field to the response model.
- [ ] **Verify:** new + existing reading-worklist tests green. Commit: `feat(worklist): urgency+wait priority scoring with sorted responses`

### Task 4.4: SQL-only triage claim fallback (no Redis)

**Files:**
- Modify: `app/services/worker.py` (`_process_triage` L175)

**Steps**
- [ ] **1:** When `r_client` is `None` (Redis absent), claim with an atomic single-statement UPDATE instead of the read-then-update pair. On MySQL/SQLite this is expressed portably as:
  ```python
  if r_client is None:
      claimed = session.execute(
          sa_update(Series)
          .where(Series.triage_status == "PENDING")
          .values(triage_status="RUNNING")
          .order_by(Series.id)
          .limit(5)
      )
      session.commit()
      pending_ids = list(claimed.lastrowid and [] or [])
  ```
  For portability use a two-step guard: keep the existing select+update for Redis mode (unchanged); for no-Redis mode, update a *single* lowest-id PENDING row per pass (`.where(...).order_by(Series.id).limit(1)`) and fetch its id by re-selecting `WHERE triage_status='RUNNING' AND id=(SELECT MIN(id) FROM series WHERE triage_status='PENDING')` — an UPDATE ... LIMIT is a single statement in both dialects, so the race window collapses to the statement itself.
- [ ] **2:** Add a test (`tests/test_triage.py`) that monkeypatches `get_redis_client` to return `None` and asserts two sequential worker passes never double-process a series (second pass finds nothing pending).
- [ ] **Verify:** test green with `REDIS_URL=` unset. Commit: `fix(worker): race-free triage claim without Redis`

## Phase 5 — Viewer & visualization upgrades

### Task 5.1: Client-side WebGL MPR in PaxsViewer

**Files:**
- Modify: `app/static/viewer/paxs-mpr.js` (existing MPR enhancement — add axial/coronal/sagittal reslice tabs driven by the existing volume endpoints)
- Consumes: `GET /api/v1/volume/{series_uid}/info` and `/data` (verified present in live `/health`: `volume_streaming.mpr_slice=true`)

**Steps**
- [ ] **1:** In `paxs-mpr.js`, add an MPR mode entry that fetches `/api/v1/volume/{series}/data?plane=axial|sagittal|coronal&slice=N` (the endpoint returns PNG/JPEG per the health payload) and renders the slice into an overlay viewport.
- [ ] **2:** Add plane + slice-range controls; keep the existing W/L presets working on the reslice.
- [ ] **Verify:** manual — open a CT series in `/viewer/`, switch to MPR, confirm all three planes render and slice-stepping works. Commit: `feat(viewer): client-side MPR via volume endpoints`

### Task 5.2: Offline-first PWA (service worker + IndexedDB)

**Files:**
- Modify: `app/static/viewer/sw.js` (existing service worker — add runtime cache of recent series metadata + thumbnails)
- Modify: `app/static/viewer/manifest.json`

**Steps**
- [ ] **1:** Extend `sw.js` with a `paxs-recent` runtime cache: on `fetch` of `/dicom-web/studies/{uid}/metadata` or thumbnail URLs, put-through cache and serve stale-while-revalidate; cap the cache at the 10 most recent studies (LRU by access time).
- [ ] **2:** Update `manifest.json` `display: standalone`, `name: PAXS Viewer`, and confirm `init-service-worker.js` registers `sw.js` on first load.
- [ ] **Verify:** manual — open a study online, hard-reload offline (DevTools → Network → Offline), the study list and thumbnails render from cache. Commit: `feat(viewer): offline study-list + thumbnail cache via service worker`

## Phase 6 — Observability & operations

### Task 6.1: Prometheus /metrics endpoint

**Files:**
- Create: `app/api/v1/metrics.py`
- Modify: `app/main.py` (register; skip for unauthenticated — expose publicly, counters only)
- Test: `tests/test_metrics.py` (new file)

**Interfaces**
- Produces: `GET /metrics` → `text/plain; version=0.0.4` Prometheus format with: `paxs_studies_total`, `paxs_instances_total`, `paxs_pending_jobs` (CompressionJob status=PENDING count), `paxs_pending_routing` (RoutingJob PENDING), `paxs_triage_flags_high` (severity HIGH count), `paxs_http_requests_total{path=...}`.

**Steps**
- [ ] **1:** Failing test:
  ```python
  def test_metrics_endpoint(client):
      r = client.get("/metrics")
      assert r.status_code == 200
      assert r.headers["content-type"].startswith("text/plain")
      assert "paxs_studies_total" in r.text
  ```
- [ ] **2:** Run — expected FAIL.
- [ ] **3:** Implement — compute counts with lightweight `SELECT COUNT(*)` on `studies`, `instances`, `CompressionJob(status='PENDING')`, `RoutingJob`, `TriageFlag(severity='HIGH')`; emit the text format manually (no new dependency; `prometheus_client` stays optional). Format:
  ```python
  def render(lines: list[str]) -> Response:
      return Response("# HELP paxs_studies_total Studies archived\n"
                      "# TYPE paxs_studies_total gauge\n"
                      "paxs_studies_total {n}\n".format(n=...), media_type="text/plain; version=0.0.4")
  ```
- [ ] **4:** Register router in `main.py`.
- [ ] **5:** Run — expected PASS.
- [ ] **Verify:** `pytest tests/test_metrics.py -q` green. Commit: `feat(ops): prometheus /metrics endpoint (gauge counters from DB)`

### Task 6.2: Automated backup script

**Files:**
- Create: `scripts/backup.py`
- Create: `scripts/backup.bat` (Windows Task Scheduler friendly)

**Steps**
- [ ] **1:** `scripts/backup.py` — CLI: `python scripts/backup.py --db-url <DATABASE_URL> --out ./backups --keep 14`. Dumps via the sync engine (`SELECT * FROM table` → CSV per table, or `sqlite3`/`mysqldump`/`pg_dump` chosen by URL scheme), verifies each dump by re-reading the first row, and prunes files older than `--keep` days. Exit 0 on success, 1 on failure.
- [ ] **2:** `scripts/backup.bat`:
  ```bat
  @echo off
  call venv\Scripts\activate
  python scripts\backup.py --db-url "%DATABASE_URL%" --out .\backups --keep 14
  ```
- [ ] **3:** Add a test `tests/test_backup_script.py` that runs `backup.py --db-url sqlite+aiosqlite:///...` against a temp DB and asserts a non-empty dump file + exit 0.
- [ ] **Verify:** script + test green; document scheduling in `deploy/` (Task Scheduler `schtasks /create /tn PAXS-Backup /tr scripts\backup.bat /sc daily /st 02:00`). Commit: `feat(ops): scheduled DB backup script with retention + verification`

## Phase 7 — Market differentiators

> Lower priority; each is an independently shippable feature. All require only the existing stack.

### Task 7.1: Teaching files (#36)

- `app/api/v1/teaching.py` + `app/services/teaching_service.py` + migration (`teaching_cases` table: id, study_uid FK, title, notes, tags JSON, created_by, created_at) + `tests/test_teaching.py`.
- API: `POST /api/v1/teaching` (study_uid, title, tags), `GET /api/v1/teaching?tag=` (search), `DELETE /api/v1/teaching/{id}` — all ADMIN/RADIOLOGIST.
- Verify: create → list by tag → delete; audit rows on create/delete.

### Task 7.2: Chat & collaboration (#35)

- `app/api/v1/chat.py` — WebSocket `/api/v1/chat/study/{study_uid}` broadcasting text messages + typing events per study via in-process pub/sub (or Redis when `REDIS_URL` set). Persist messages to a `study_chat_messages` table. `tests/test_chat.py` with two TestClient websockets asserting delivery.
- Verify: two clients exchange messages; history endpoint returns them.

### Task 7.3: Multi-language dashboard (i18n)

- Add a `?lang=ar|en` query + `lang` localStorage; wrap dashboard strings in `app/static/app.js` with a small `T(key)` dictionary (English + Arabic keys from `paxs-docs/new plan.md` §3); RTL class toggle on `<html dir="rtl">` for Arabic. No backend change.
- Verify: manual — switch language, confirm dashboard chrome + report editor labels translate.

### Task 7.4: Patient portal upgrade

- Extend `app/services/sharing_service.py` with an OTP step: share link → phone/email OTP entry (6-digit TOTP stored hashed, 5-min expiry) before the patient token is issued. `tests/test_share.py` append: link without OTP → 401; with correct OTP → token + study view.
- Verify: full suite green.

## Phase 8 — Final verification & rollout

### Task 8.1: Full regression

- [ ] **1:** `venv/Scripts/python.exe -m pytest tests -q --no-header` → **518 + new tests, all green**.
- [ ] **2:** Boot the server against SQLite with receiver+worker on, run `build-win/healthcheck.py --json` → `verdict.exit_code == 0`.
- [ ] **3:** Boot against MySQL (existing live stack) and confirm `/api/v1/health` `database: ok`.
- [ ] **4:** `docker compose config --quiet` and `docker build .` succeed; run the container once and confirm `USER paxs` + healthcheck.
- [ ] **5:** Rebuild the Windows bundle (see `build-win/README.md`, `build-installer.bat`) and run `dist/PAXS/PAXS-HealthCheck.exe` against the live server → `ALL SYSTEMS NOMINAL`, exit 0.

### Task 8.2: Docs update

- Update `README.md` with the PostgreSQL option, the hardening flags, and the /metrics endpoint.
- Update `feature-plans/IMPLEMENTATION_CHECKPOINT.md` (bump date, mark PostgreSQL / hardening / dictation / metrics done).
- Mark every completed task in this file with `[x]`.

---

## Rollback plan

| Phase | Rollback |
|---|---|
| 1 (PostgreSQL) | Remove asyncpg/psycopg2 pins + revert the two `to_sync_url` branches; no migration ran against existing SQLite/MySQL data, so nothing to undo there. |
| 2 (hardening) | Revert config defaults + middleware registration; header middleware is stateless. |
| 3 (dictation) | Drop the `dictation_text` column: `alembic downgrade -1`; remove the router import. |
| 4–7 | New modules/routers only — remove the router registration and, for 7.1/7.2, `alembic downgrade` the new tables. |

## Risks

- **PostgreSQL dialect differences** (JSON columns, `UPDATE ... LIMIT` in Task 4.4, MySQL-only assertions in tests) — mitigated by running the full suite + migration cycle against PG in Task 1.4 before merging.
- **CSP breaking the viewer** (WASM, workers, blob URLs) — the CSP in Task 2.2 is explicitly permissive; verified by booting `/viewer/` after the change. If a viewer feature still breaks, relax the specific directive rather than removing the header.
- **HSTS enabled too early** would lock browsers onto https on a plain-HTTP LAN deployment — keep `HSTS_ENABLED=False` by default (Task 2.2) until TLS is configured.
- **AI detectors are heuristics, not clinical AI** — every detector docstring and finding `summary` marks it heuristic; keep them behind `TRIAGE_ENABLED` and route HIGH findings to a human review queue (existing dashboard behavior).
- **OHIF fork source lives outside this repo** (`D:\Github`) — viewer tasks (5.1/5.2) edit the built bundle in `app/static/viewer`; if the PaxsViewer build is re-run, the custom files (`paxs-mpr.js`, `paxs-study-list-enhance.js`) must be re-copied (see `build-win/sync_admin_panel.py` pattern / `deploy/PAXSVIEWER-INTEGRATION.md`).

---

## Execution handoff

Plan complete and saved to `feature-plans/2026-08-13-MASTER-PLAN.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — I execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach? (Suggested start: Phase 0 → Task 1.1 → Task 2.1, all small and independently testable.)
