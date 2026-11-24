# PAXS Archive & Standalone `.exe` — Scalability, Heavy-Work & Stability Plan

**Date:** 2026-11-23  
**Scope:** Backend API, DICOM DIMSE SCP, background workers, multi-node HAProxy deployment, and Windows standalone executables (`PAXS-Archive.exe` / `PAXS-Server.exe`).  
**Baseline:** Follow-up to `REVIEW_SCALABILITY_SECURITY_COMPLETENESS_2026-11-17.md` (reflecting all hardening already merged since Nov 17).

---

## 1. Readiness Summary

| Deployment Profile | Current Status | Key Limiting Factors |
| :--- | :--- | :--- |
| **Small / Mid Clinic (Single Node)** | **Ready** | Strong cache caps, PostgreSQL pooling, bounded thread pools, and separated worker threads already hold up well under normal load. |
| **Heavy Multi-Modality / High Concurrency** | **Needs P1 Fixes** | Legacy bulk exports freeze the async loop & buffer whole studies in RAM; DIMSE C-STORE uses `NullPool` + `asyncio.run()` per slice; single-process Python GIL caps CPU throughput without external workers/HAProxy. |
| **Standalone Windows `.exe` (`dist/PAXS-Server`)** | **Needs Fixes + Rebuild** | Current `dist/PAXS-Server` is stale (2026-11-17), copies developer `.env`, ships a broken `install_service.bat` path, writes unbounded desktop logs, and can kill active burns/ingests on close. |

---

## 2. Safeguards Already Verified as Solid

Preserve these invariants during all upcoming changes:

- **Hard memory caps & admission control:**
  - Volume L1 cache (`VOLUME_CACHE_BUDGET_MB=1024`), assembly working-set admission (`VOLUME_ASSEMBLY_BUDGET_MB=2048`, max `16` inflight/waiters, single-flight coalescing) — `app/services/volume/cache.py`, `app/core/config.py`.
  - Global in-flight streamed response cap (`RESPONSE_BYTES_IN_FLIGHT_BUDGET_BYTES=512 MiB`, `VOLUME_MAX_BYTES=256 MiB`) returning retryable `503` — `app/core/response_admission.py`.
  - Bounded WADO decoded-frame LRU (`256 MB`), byte-range frame offset cache (`64 MB`), per-series frame-instance cache (`256` series, 15s TTL, single-flight lock) — `app/services/frame_instance_cache.py`.
  - Bounded gzipped WADO metadata body cache (`128 MB`) — `app/services/metadata_body_cache.py`.
  - Bounded AI mask cache (`256 MB` / 32 entries) — `app/services/ai_cache.py`.
  - Redis circuit-breaker + `256 MB` L1 fallback cache — `app/services/redis/cache.py`.
- **Bounded executors & DB resilience:**
  - `IO_THREAD_POOL_WORKERS=64` (`app/main/startup.py`), `BoundedBlockingExecutor` (`16` threads / `64` queue → HTTP `503`) (`app/core/concurrency.py`), `BoundedBackgroundPool` (`8` threads / `64` queue).
  - PostgreSQL-only `asyncpg` + `psycopg` pools with `pool_pre_ping=True`, `pool_recycle=1800`, read-replica routing, and `PoolTimeout` → `503` (`app/db/session.py`).
- **Worker separation & crash recovery:**
  - Dedicated threads for `compression`, `transfer`, `replication`, and `routing` (`app/services/worker.py`).
  - PostgreSQL advisory locks on transfer & replication, startup recovery for orphaned `RUNNING` transfer jobs, `NOT EXISTS` replication anti-join, and `attempts=0` reset on re-ingest (`app/services/transfer_service.py`, `app/services/replication_service.py`).
  - Streaming STOW-RS upload spooling (`STOW_MAX_CONCURRENT_PROMOTIONS=4`) and streaming DR replication (`_copy_object_streaming`).

---

## 3. Priority Fixes — Backend & Multi-Node Scalability

### P1.1 — Stream & Offload Bulk Export Endpoints (Eliminate Event-Loop Freeze & RAM OOM)

- **Files:**
  - `app/api/v1/export.py` (lines 55–86)
  - `app/services/dicomdir_service.py` (lines 158–194)
  - `app/api/v1/dicomdir.py` (reference streaming pattern at lines 50–84)
- **Problem:**
  - `GET /api/v1/export/{study_uid}/anonymized` runs `anonymizer.anonymize_files_to_zip(...)` synchronously on the ASGI event loop and returns the entire ZIP in RAM (`Response(content=data)`).
  - `POST /api/v1/export/dicomdir` calls `storage_service.exists()` and `storage_service.read_bytes()` synchronously inside `async def` and buffers the whole ZIP in `io.BytesIO()`.
  - Under a 1–4 GB CT/MR study, either route freezes all HTTP/viewer traffic on that node and risks OOM kills.
- **Fix:**
  1. Refactor `anonymize_files_to_zip` and `export_dicomdir_package` to write incrementally to a `TemporaryDirectory` file on disk (checking free disk space first, just like `client_media_service.build_client_media`).
  2. Execute the packaging inside `await run_blocking(...)` (so saturation returns HTTP `503` with `Retry-After` instead of stalling the loop).
  3. Return `AdmittedStreamingResponse` (or `StreamingResponse` tied to a generator dependency that cleans up the workspace on completion/abort) with chunked 1 MiB reads and `Content-Length`.
  4. Apply a configurable cap (`MAX_EXPORT_STUDY_BYTES`, default e.g. `8 GiB`) and concurrency limit (e.g., semaphore of `2` active bulk exports per node).
- **Verification:** Add unit tests asserting off-loop execution (event-loop responsiveness during export), bounded memory usage (no full-ZIP `BytesIO`), and guaranteed temp-file cleanup on client disconnect.

---

### P1.2 — Eliminate DIMSE C-STORE Per-Slice Connection Churn (`NullPool` + `asyncio.run`)

- **Files:**
  - `app/dicom/receiver.py` (lines 249–269, 338–343)
  - `app/db/session.py` (lines 83–85)
  - `app/services/indexing_service.py`
- **Problem:**
  - Every single C-STORE slice calls `asyncio.run(_index_async(...))` on `DimseAsyncSessionLocal` (`NullPool`), creating a new asyncio event loop and opening/closing a fresh PostgreSQL TCP/TLS connection per DICOM image.
  - Under 16 concurrent modality associations sending 2,000-slice CT studies, this causes severe connection churn, TLS handshake overhead, and `max_connections` spikes.
- **Fix (Two Options — Option A Recommended):**
  - **Option A (Dedicated DIMSE Async Loop + Pooled Engine):** Start one long-lived background `asyncio` loop thread inside `receiver.py` owning a dedicated pooled `AsyncEngine` (`pool_size=min(DICOM_MAX_ASSOCIATIONS, 16)`, `max_overflow=8`, `pool_pre_ping=True`). `handle_store` submits `_index_async(...)` via `asyncio.run_coroutine_threadsafe(..., _dimse_loop).result()`. Zero refactor of `indexing_service.py`, immediate connection reuse, and clean shutdown in `shutdown_receiver()`.
  - **Option B (Pure Sync Indexing Path):** Share `SyncSessionLocal` across DIMSE (matching C-FIND, MPPS, and Storage Commitment). Requires maintaining sync/async parity in `indexing_service.py`.
- **Additional Ingest Optimizations:**
  - Cache `(calling_ae -> tenant_id)` and `(study_uid -> storage_backend)` per active association (short TTL / association-scoped dict) to eliminate 2 extra queries per slice on a 1,500-slice series.
  - Combine the three separate `assert_tenant_owns_ingest_target` SELECTs (`Study`, `Series`, `Instance`) into a single query or skip redundant parent checks when the study/series is already verified in the same transaction.
- **Verification:** Benchmark 1,000-slice C-STORE burst across 4 parallel SCUs before/after; confirm PostgreSQL connection count stays flat (≤ pool size) and throughput increases significantly.

---

### P1.3 — Rate-Limiter Fast Path When Redis Is Unavailable

- **Files:**
  - `app/api/ratelimit.py` (lines 162–186)
  - `app/services/redis/cache.py` (lines 212–242)
- **Problem:**
  - Every rate-limited request executes `await run_blocking(get_redis_client)` even when Redis is disabled or its circuit breaker is open, pushing every request through the `16`-thread `BoundedBlockingExecutor`. Under heavy traffic without Redis (common in standalone installs), this needlessly saturates the blocking pool and triggers false `503` responses.
- **Fix:**
  1. Add a lightweight non-blocking check `is_redis_ready_fast() -> bool` in `app/services/redis/cache.py` (checks `REDIS_CACHE_ENABLED`, `_redis_available`, and breaker state without network I/O or locks).
  2. In `app/api/ratelimit.py`, only offload to `run_blocking` when Redis is enabled and not in an open-circuit state; otherwise jump directly to the in-memory sliding window.
- **Verification:** Unit test verifying zero `run_blocking` calls on rate-limited endpoints when Redis is disabled or circuit is open.

---

### P2.1 — Multi-Node Concurrency & Worker Throughput Hardening

- **Triage lock safety without Redis (`app/services/worker.py:454-471`):**
  - Replace the non-locked `SELECT ... WHERE triage_status='PENDING'` with `SELECT ... FOR UPDATE SKIP LOCKED` inside the claim transaction so multi-node deployments never double-process triage even when Redis is down.
- **Parallelize in-process compression batching & stop per-job heartbeat thread churn (`app/services/worker.py:155-177`):**
  - Support configurable `WORKER_COMPRESSION_CONCURRENCY` (default `1` in embedded mode to protect API latency, `2–4` in standalone worker mode) and replace the per-job spawned heartbeat thread with a single shared heartbeat timer/thread that renews leases for all currently owned job IDs.
- **HAProxy timeouts & stateful route handling (`deploy/haproxy.cfg:20-23, 49-58`):**
  - Increase `paxs_api` `timeout server` / `timeout client` (or add an ACL for `/api/v1/export/` and `/dicomweb/studies` STOW-RS) from `60s` to `600s` so multi-GB downloads/uploads don't get cut off mid-stream.
  - Move `_disc_jobs` (`app/api/v1/dicomdir.py:98-119`) to Redis (or DB table) if multi-node server-side export status polling is needed, or document single-node pinning for local hardware burn routes.
- **DB Pool Auto-Tuning Guidance (`app/core/config.py:187-195`):**
  - Document and warn when `DB_POOL_SIZE + DB_MAX_OVERFLOW` is far below `IO_THREAD_POOL_WORKERS` on busy single nodes, and support PgBouncer transaction pooling guidance for >3 API nodes.

---

## 4. Priority Fixes — Standalone Windows `.exe` (`PAXS-Archive.exe` / `PAXS-Server.exe`)

### S1 — Stop Leaking Developer `.env` & Clean `dist\PAXS-Server`

- **File:** `build_standalone.bat` (lines 71, 84–91)
- **Problem:** Copies the developer's local `.env` (with real DB passwords, `SECRET_KEY`, etc.) into `dist\PAXS-Server\.env`, and the current `dist\PAXS-Server` also contains `.env`, `paxs.lic`, and binaries from `2026-11-17` (missing the rich-text editor, PDF export, and client/server media export).
- **Fix:**
  1. Remove the fallback `copy /y ".env" "%DIST_DIR%\.env"` from `build_standalone.bat` (ship `.env.example` only, or preserve an existing target `.env` only when `--keep-env` is explicitly passed for local dev rebuilds).
  2. Never bundle `paxs.lic` or developer `.env` when zipping a release artifact; add a pre-release check script that fails if `.env` or `paxs.lic` or `storage/*` exists in a release archive.
  3. Rebuild `dist\PAXS-Server` cleanly after applying the fixes below and run a smoke test of `PAXS-Server.exe` and `PAXS-Archive.exe`.

---

### S2 — Fix Broken `install_service.bat` Inside `dist\PAXS-Server`

- **Files:** `scripts/install_service.bat`, `dist/PAXS-Server/install_service.bat` (lines 13–36)
- **Problem:** Hardcodes `set "APP_DIR=%~dp0..\dist\PAXS-Server"`, which fails when executed from inside `dist\PAXS-Server\` (resolves to `dist\dist\PAXS-Server`). Also prints `http://0.0.0.0` instead of `http://localhost:8000` and doesn't configure NSSM crash-restart throttling or log rotation.
- **Fix:**
  1. Resolve `APP_DIR` dynamically:
     ```bat
     if exist "%~dp0PAXS-Server.exe" (
         set "APP_DIR=%~dp0"
     ) else (
         set "APP_DIR=%~dp0..\dist\PAXS-Server"
     )
     ```
  2. Configure NSSM resilience & log rotation automatically:
     - `nssm set %SVC_NAME% AppStdout "%APP_DIR%\logs\service-stdout.log"`
     - `nssm set %SVC_NAME% AppStderr "%APP_DIR%\logs\service-stderr.log"`
     - `nssm set %SVC_NAME% AppRotateFiles 1`
     - `nssm set %SVC_NAME% AppRotateOnline 1`
     - `nssm set %SVC_NAME% AppRotateBytes 10485760` (10 MB)
     - `nssm set %SVC_NAME% AppExit Default Restart`
     - `nssm set %SVC_NAME% AppRestartDelay 5000`
     - `nssm set %SVC_NAME% AppStopMethodConsole 15000` (graceful Ctrl-C before force kill).
  3. Fix status banner URL to `http://localhost:8000`.

---

### S3 — Fix `health_monitor.bat` for `PAXS-Archive.exe` & Graceful Stop

- **File:** `health_monitor.bat` (lines 195–226)
- **Problem:** `:stop_backend` and `:free_port` only recognize `PAXS-Server.exe` and `python.exe` (ignoring `PAXS-Archive.exe`), and always use `taskkill /F` (abrupt kill without running FastAPI lifespan teardown).
- **Fix:**
  1. Recognize `PAXS-Archive.exe` alongside `PAXS-Server.exe` and `python.exe`.
  2. Attempt graceful termination first (`taskkill /T /PID ...` without `/F`), wait up to 10 seconds for `:8000` / `:10400` to release, and fall back to `/F` only if still alive.
  3. Add a PostgreSQL reachability check (`pg_isready` / TCP `:5432`) and disk-free check on `storage\` to the health dashboard menu.

---

### S4 — Rotating Logs & Safe Shutdown Guard in `PAXS-Archive.exe`

- **File:** `paxs_desktop.py` (lines 73–82, 146–149, 257–295, 337–376, 439–442)
- **Problems:**
  1. `redirect_stdio_when_windowed` opens `logs/paxs-desktop.log` in append mode with no size cap — grows unboundedly over months of operation.
  2. Closing the desktop window stops the server after 20s and kills daemon threads (`paxs-local-media-*`, `paxs-compression-worker`, `paxs-dicom-scp`) mid-burn or mid-ingest.
- **Fixes:**
  1. **Rotating stdio wrapper:** Wrap `sys.stdout` / `sys.stderr` in a tiny thread-safe size-rotating writer (e.g., 10 MB max per file, 5 backups: `paxs-desktop.log.1`..`.5`) so disk space can never be exhausted.
  2. **Close confirmation guard:** Hook `window.events.closing` in `open_window()` to check whether any `_local_jobs` are in `downloading`/`preparing`/`staging`/`burning`/`writing` (or active DIMSE C-STORE transfers via `receiver_queue_snapshot()`). If active, prompt with a Windows confirmation dialog before allowing exit, or wait until the burn completes.
  3. **Extend graceful shutdown timeout:** Allow `STOP_TIMEOUT_SECONDS` to be overridden via env (e.g., `PAXS_STOP_TIMEOUT_SECONDS=30`) and join active local media threads briefly on shutdown.

---

### S5 — Reduce `%TEMP%` Disk Footprint on `.exe` Local CD/DVD/USB Export

- **Files:** `paxs_desktop.py` (lines 268–290), `app/services/disc_burn_service.py`
- **Problem:** Local desktop export downloads the full study ZIP into `%TEMP%` and then extracts it to a second `%TEMP%` folder before burning/copying, requiring **~2×–3× the study size** free on `C:\`.
- **Fix:**
  1. **USB / local folder export:** Stream-extract the downloaded ZIP entries **directly** to the destination USB/folder (using atomic `.part` → final folder rename on the destination drive), deleting `study.zip` immediately or even streaming from HTTP directly into a temp zip on the target drive if `C:\` has less free space than the study.
  2. **Pre-flight disk check:** Check `shutil.disk_usage(tempfile.gettempdir())` against the study's `Content-Length` before downloading and surface a clear message (`"Not enough free space on C:\ temp drive; need X MB"`) instead of failing mid-download.
  3. **Local-same-machine fast path:** When `PAXS-Archive.exe` is connected to its own embedded local server (`127.0.0.1` + `STORAGE_BACKEND=local`), optionally build the DICOMDIR staging directory directly from local storage without HTTP ZIP round-tripping — cutting export time in half and halving temp disk usage.

---

## 5. Architecture & Heavy-Work Ideas (Next Phase)

1. **Package `PAXS-Worker.exe` in `paxs.spec`:**
   - Add a 3rd lightweight entry point (`paxs_worker.py` → `PAXS-Worker.exe`) sharing the same `COLLECT` `_internal/` folder (near-zero extra bundle size).
   - Busy Windows standalone clinics can then set `WORKER_ENABLED=false` on `PAXS-Server.exe` and run 2–4 `PAXS-Worker.exe` instances via NSSM to scale HTJ2K/JPEG-LS compression linearly across CPU cores without GIL contention.
2. **Async Job Pattern for Large Exports (Anonymized ZIP & DICOMDIR):**
   - For studies >500 MB (or >1,000 instances), convert exports into a background job (`POST` returns `202 Accepted` + `job_id`, UI polls progress, then downloads via signed/authenticated streaming `GET`). Prevents reverse-proxy timeouts and browser stalls on 5 GB cardiac/PET-CT studies.
3. **PostgreSQL + Storage Pre-Flight Banner in Standalone `.exe`:**
   - On startup, verify PostgreSQL reachability, free disk space on `STORAGE_ROOT` (warn at <10% or <20 GB free), and codec availability (`--check-codecs`), displaying actionable status in the desktop UI rather than failing silently in logs.
4. **Built-In Heavy-Work Stress & Soak Test Suite (`tools/loadtest/`):**
   - Create a repeatable soak test script that runs simultaneously:
     - 8 parallel DIMSE C-STORE SCUs pushing 2,000-slice CT studies
     - 4 concurrent WADO-RS viewer scrolling clients + 2 MPR clients
     - 1 concurrent anonymized study export + 1 DICOMDIR export
     - Live tracking of RSS memory, `/metrics` event-loop lag (`paxs_event_loop_lag_seconds`), PostgreSQL active connections, and HTTP `503`/`500` rates.
5. **Prometheus Alerting Rules (`deploy/alerts.yml`):**
   - Add ready-made alerts on metrics PAXS already exports:
     - Event-loop lag > `250 ms` for 2m
     - DB pool timeouts / HTTP `503` spike
     - Compression queue backlog growing monotonically for >15m
     - Cache utilisation > `95%` + high miss rate (thrashing)
     - Storage disk free < `15%`.

---

## 6. Recommended Tuning Profiles

### A. Standalone Clinic PC (`PAXS-Archive.exe` / `PAXS-Server.exe`, 16 GB RAM, 8 Cores)
```env
ENVIRONMENT=production
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=15
IO_THREAD_POOL_WORKERS=32
BLOCKING_IO_MAX_THREADS=16
VOLUME_CACHE_BUDGET_MB=1024
VOLUME_ASSEMBLY_BUDGET_MB=1536
DICOM_FRAME_CACHE_MB=256
DICOM_MAX_ASSOCIATIONS=16
REDIS_CACHE_ENABLED=false          # unless Redis is installed locally
DEFAULT_COMPRESSION=JPEG-LS        # or HTJ2K; schedule off-hours if CPU is tight
COMPRESSION_TIMING=scheduled
COMPRESSION_START_TIME=22:00
COMPRESSION_END_TIME=06:00
```

### B. Heavy Single-Server Hospital (64 GB RAM, 16+ Cores, Dedicated PostgreSQL + Redis)
```env
WORKER_ENABLED=false               # offload to 3–4 separate `python -m app.services.worker` processes
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=20
DB_MAX_CONNECTIONS=200
IO_THREAD_POOL_WORKERS=64
VOLUME_CACHE_BUDGET_MB=4096
VOLUME_ASSEMBLY_BUDGET_MB=4096
DICOM_FRAME_CACHE_MB=1024
DICOM_MAX_ASSOCIATIONS=32
REDIS_CACHE_ENABLED=true
```

### C. High-Volume Multi-Node Cluster (3+ API/DIMSE Nodes behind HAProxy + S3/MinIO + PG Primary/Replica + Redis)
- Set `PAXS_INSTANCE_ID=node-1..N`, `APP_WORKERS=3`, `STORAGE_BACKEND=s3`, `DB_READ_REPLICA_URL=...`.
- Run `WORKER_ENABLED=false` on API nodes; run dedicated worker containers/services for compression, triage, routing, and replication.
- Put **PgBouncer** in front of PostgreSQL if total `configured_max_connections` exceeds PostgreSQL's `max_connections`.
- Raise HAProxy `paxs_api` timeouts to `600s` for export/STOW routes.

---

## 7. Implementation Status & Execution Order

1. **Phase 1 (Immediate Stability — COMPLETED 2026-11-23):**
   - [x] P1.1 Streaming/off-loop exports (`app/api/v1/export.py`, `app/services/anonymizer.py`, `app/services/dicomdir_service.py`, `app/api/v1/dicomdir.py`)
   - [x] P1.3 Rate-limiter Redis fast-path (`app/api/ratelimit.py`, `app/services/redis/cache.py`, `app/services/redis_service.py`)
   - [x] P2.1 Triage `FOR UPDATE SKIP LOCKED` (`app/services/worker.py`) + HAProxy `600s` timeout (`deploy/haproxy.cfg`)
2. **Phase 2 (Heavy DICOM Ingest Throughput — COMPLETED 2026-11-23):**
   - [x] P1.2 Dedicated pooled DIMSE async loop (`app/db/session.py`, `app/dicom/receiver.py`) + association-scoped tenant/backend cache
3. **Phase 3 (Standalone `.exe` Hardening & Fresh Build — COMPLETED 2026-11-23):**
   - [x] S1 (`build_standalone.bat` no `.env` leak; opt-in `--keep-env`)
   - [x] S2 (`scripts/install_service.bat` dynamic `%~dp0` + NSSM 10 MB log rotation + crash restart)
   - [x] S3 (`health_monitor.bat` `PAXS-Archive.exe` recognition, `:5432` check, graceful stop first)
   - [x] S4 (`paxs_desktop.py` 10 MB×5 rotating log stream + active burn/ingest close guard + configurable stop timeout)
   - [x] S5 (`app/services/disc_burn_service.py` direct destination-drive staging + `paxs_desktop.py` `Content-Length` free-space precheck & early ZIP cleanup)
   - [x] Packaged `PAXS-Worker.exe` (`paxs_worker.py` + `paxs.spec`) and rebuilt `dist\PAXS-Server`
4. **Phase 4 (Heavy-Work Soak Testing, Alerts & Async Exports — COMPLETED 2026-11-23):**
   - [x] Async job queue (`202 Accepted` + poll + admitted stream download + cleanup) for multi-GB exports (`app/services/export_job_service.py`, `app/api/v1/export.py`)
   - [x] `tools/loadtest/soak_test.py` heavy-work DIMSE + HTTP soak suite with `/metrics` sampling and SLO evaluation
   - [x] Expanded `deploy/prometheus-alerts.yml` with `PaxsDbDown`, `PaxsEventLoopLagHigh`, `PaxsBlockingIoRejected`, `PaxsVolumeAssemblyWaitersHigh`, `PaxsRedisCircuitOpen`, and `PaxsRoutingQueueFailures`
