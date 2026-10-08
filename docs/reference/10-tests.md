# Test suite: infrastructure, inventory, coverage map and quality

> **Area deep-dive** — part of the [PAXS Archive documentation](../README.md). Produced by static analysis of commit `e90d32c` (2026-10-08); line numbers refer to that commit. Findings here are consolidated, de-duplicated and prioritized in [CODE_REVIEW.md](../CODE_REVIEW.md) — where the two differ, CODE_REVIEW.md takes precedence. Labels such as `[read]`, `[verified]`, `[V]`, `[H]` or `[inferred]` describe how each claim was checked (read in code vs. inferred).

> ⚠️ **Baseline: this deep-dive describes commit `e90d32c`.** What changed in `5a32aa4` (fixes, regressions, new code and endpoints) is in [5a32aa4/06-tests-hygiene.md](5a32aa4/06-tests-hygiene.md). The current status of every finding is in [CODE_REVIEW.md](../CODE_REVIEW.md).

Scope: `tests/` (77 test modules, `conftest.py`, `helpers.py`, `__init__.py`), cross-checked against `app/`.
Method: static analysis only. Nothing was executed. Evidence came from reading `conftest.py` and `helpers.py` in full, the first 40 lines of every test module, Grep over the whole tree, and two read-only Python AST passes. Coverage figures are import and route-reference heuristics, not line coverage.

Severity labels: **CRIT** (blocks the suite or a security/data risk), **MAJOR** (wrong or misleading tests, real gap), **MINOR** (hygiene or flakiness risk).

---

## 0. Blocking finding: the suite cannot be imported as committed

Static chain (every link is module-level, so this fails at import time, before any test runs):

1. `tests/conftest.py:37` `from app.main import app`
2. `app/main.py:31` `from app.dicom.receiver import ...`
3. `app/dicom/receiver.py:38-42` `from app.services import (..., storage_service, ...)`
4. `app/services/storage_service.py:35` `from app.services.storage import get_storage_backend` and `:38` `from app.services.storage.backend import StorageError`

`app/services/storage/` (the package with `get_storage_backend`, `reset_storage_backend`, `LocalStorageBackend`, `S3StorageBackend`, `StorageError`) does not exist in the working tree, and no local git ref has ever contained `app/services/storage.py` or `app/services/storage/`. The cause is probably `.gitignore:9`, which has the unanchored pattern `storage/` and therefore matches `app/services/storage/`. `git check-ignore -v app/services/storage/__init__.py` returns `.gitignore:9:storage/`.

Other broken imports found by the AST pass (16 entries in total: 11 in test modules, 5 in app modules):

- `tests/test_storage_backend.py:16` (4 names) and `:190`
- `tests/test_multi_storage.py:28` (2 names)
- `tests/test_replication.py:20` (2 names)
- `tests/test_cstore.py:405` (in-function `from app.services.storage import ...`, 2 names)
- `app/api/v1/storage.py:34` (2 names)
- `app/services/replication_service.py:39`
- `app/services/storage_service.py:35` and `:38` (`app.services.storage.backend`)

Expected result, inferred and not executed: `ModuleNotFoundError: No module named 'app.services.storage'` raised while loading `conftest.py`, so no test collects. The app server cannot import `app.main` either.

Fix: commit the missing package and anchor the ignore rule (`/storage/` or `!app/services/storage/`). Then add a smoke test that imports `app.main` and every `app.api.v1.*` module, so this class of break fails in one obvious place.

---

## 1. Test infrastructure

### 1.1 How the suite runs

- No `pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini`, or `.github/workflows/` exists in the repo. The only run configuration is `tests/conftest.py`.
- Command: `pytest tests` from the repo root. `tests/__init__.py` makes `from tests.helpers import ...` resolve.
- Markers: none registered. The suite uses `@pytest.mark.anyio` (provided by the anyio pytest plugin, which arrives transitively) plus built-in `skipif` (`test_compression.py:47,62,138,173`, and `pytestmark` at `test_tls.py:32`). There are no `xfail` or `pytest.skip` calls.
- `requirements.txt:35-36` lists `pytest>=8.2` and `httpx>=0.27` (FastAPI `TestClient` needs httpx). Both are unpinned. No pytest-asyncio, pytest-timeout, or xdist configuration. `boto3` and `redis` are commented out (`requirements.txt:30-32`) even though the S3 and Redis tests exist.
- Database: `DATABASE_URL` is chosen with `os.environ.setdefault` (`conftest.py:18-20`), so an already-set value wins. The documented MySQL invocation is at `conftest.py:15-16`.

### 1.2 Environment forced by conftest (set before `app` is imported)

`conftest.py:21-33` sets, unconditionally: `STORAGE_ROOT` (temp dir), `SECRET_KEY=test-secret-key`, `DICOM_RECEIVER_ENABLED=false`, `WORKER_ENABLED=false`, `THUMBNAIL_ENABLED=true`, `ALLOW_REGISTER=true`, `PUBLIC_BASE_URL=http://paxs.test`, `RATE_LIMIT_ENABLED=false` (so rate limiting never runs through the app), `HL7_ENABLED=false`, `AUTH_GATE_ENABLED=false`. `TEST_ROOT` is a `mkdtemp` directory (`conftest.py:17`) that is never removed.

### 1.3 conftest.py fixtures

| Fixture | Scope / autouse | Lines | What it does |
|---|---|---|---|
| `schema` | session, autouse | 41-46 | `Base.metadata.drop_all(sync_engine)`, then `create_all`, then `yield`, then `drop_all`. Runs against whatever `DATABASE_URL` resolves to. |
| `client` | function | 49-66 | Points `settings.SCRIPTS_DIR` at an empty temp dir, enters `TestClient(app)` (runs the lifespan on every test), then `DELETE`s every row of every table (`:63-65`) before the test body, then yields. |
| `receiver` | function | 75-88 | `DELETE`s every row, starts the DICOM SCP with `start_receiver("127.0.0.1", free_port(), ae_title="PAXS_TEST_SCP")`, yields `(port, ae)`, calls `shutdown_receiver()` on teardown. |
| `free_port()` (helper, not a fixture) | - | 69-72 | Binds port 0, reads the port, closes the socket. This is a TOCTOU race. |

Notes:
- The `conftest.py:3` docstring says every test uses "an isolated temp DB ... so nothing touches a real archive". That is only true when `DATABASE_URL` is unset (see Section 4, CRIT-2).
- `client` re-runs the app lifespan (startup hooks, including schema init) for every test. That is likely a large share of runtime. This is a performance note, not a defect.

### 1.4 Module-level autouse and shared fixtures

| File:line | Fixture | Effect |
|---|---|---|
| `test_admin_panel.py:20` | `_admin_config` (autouse) | `ADMIN_PANEL_CONFIG_FILE` to tmp; `PAXS_DISABLE_BROWSER=1` |
| `test_config.py:31` | `_restore_runtime_settings` (autouse) | Restores the global `settings` object and `server_settings` rows after each test |
| `test_database.py:38` | `_isolate_db_admin` (autouse) | `ENV_FILE_PATH` and `BACKUP_DIR` to tmp; deletes `server_settings` on teardown |
| `test_licensing.py:27` | `rsa_keys` (not autouse) | Generates an RSA key pair per test |
| `test_login_gate.py:31` | `gate` | Flips `AUTH_GATE_ENABLED=True` via monkeypatch |
| `test_multi_storage.py:46,52` | `admin` (autouse), `root_dir` | Registers `store_admin` before every test; temp root |
| `test_ratelimit.py:11` | `_clean` (autouse) | Enables `RATE_LIMIT_ENABLED` and resets limiter state |
| `test_redis_panel.py:56` | `_isolate_redis_env` (autouse) | `ENV_FILE_PATH` to a `mkdtemp` dir, not cleaned up |
| `test_replication.py:38,43` | `admin` (autouse), `_dr_off` (autouse) | Registers `rep_admin` before every test; DR replication off |
| `test_scripting.py:60,212` | `_scripts_env` (autouse), `admin` (autouse) | Per-test `SCRIPTS_DIR`, clears `scripting_service._cache`, sets `TEST_SCRIPT_LOG`; registers `script_admin` |
| `test_storage_backend.py:148` | `s3_backend` | Fake S3 client (module missing, see Section 0) |
| `test_tls.py:57` | `tls_receiver` | TLS-wrapped SCP with a temp self-signed cert |
| `test_users.py:27` | `_admin` (autouse) | Registers `root_admin` before every test |

Autouse fixtures that register an admin also mean the test-file database has a seeded user before the body runs, which quietly couples those files to the "first user is ADMIN" bootstrap rule.

### 1.5 helpers.py

- `make_instance(...)` (`helpers.py:15-106`): synthetic single-frame CT (64x64 uint16 disk by default). Keyword arguments control patient, study, series, and SOP UIDs, position and orientation, rescale, slice thickness, and frame of reference. Pixel data is set by `pixel=` or a default disk.
- `ds_bytes(ds)` (`:109-112`), `build_stow_body(datasets, boundary)` (`:115-123`), `stow_headers(boundary)` (`:126-129`), constant `CT` (`:12`).
- Duplicated outside helpers: `_upload_study` (`test_dicomweb.py:14`, reused by five modules), `_ingest_series` (`test_volume.py:23`, reused by three), `_store_via_scu` (`test_receiver.py:20`, reused by eight modules). See Section 4, MAJOR-6.

### 1.6 Environment variables, global mutations, and external resources

- Process environment mutated directly: `PAXS_LICENSE_PUBLIC_KEY` set at `test_licensing.py:126` and `:214`, popped only in the `finally` of `:246-249`. The `:126` assignment is never restored.
- Global `settings` mutated directly (not via monkeypatch): `test_receiver.py:35,65,81` (`COMPRESSION_ENABLED`); `test_mip.py:175,179`, `test_volume.py:216,221,227,235`, `test_volume_mpr_slice.py:174,178` (`VOLUME_STREAMING_ENABLED`, `VOLUME_MAX_SLICES`), each with a manual restore; `test_routing_queue.py:272-273,327-328` (`ROUTING_MAX_ATTEMPTS`, `ROUTING_RETRY_BACKOFF_SECONDS`); `test_database.py:46-50` and `test_redis_panel.py:61-63` (`ENV_FILE_PATH`, `BACKUP_DIR`).
- Real sockets on 127.0.0.1: pynetdicom SCP/SCU on free ports, `ThreadingHTTPServer` and `HTTPServer` mocks on port 0, MLLP sockets, and TLS handshakes. No test reaches a non-loopback host, and `test_web_nodes.py:61-75` monkeypatches `getaddrinfo` so the public-host check needs no DNS.
- Hard-coded addresses: `test_web_nodes.py` port 9000 (`:74,92,101,108,232,233`); `test_redis_panel.py:133` (`127.0.0.1:6379`, configure only); `test_database.py:142,162` (port 3307, `paxs_db`, as config strings); node rows on 10401-10411 (`test_routing.py:15-124`, `test_prefetch.py:15-83`, `test_ian.py:16-98`); "port 1 is refused" assumptions (`test_echo.py:120`, `test_cfind.py:149,368,392`, `test_cget.py:163,358,387`, `test_cstore.py:136,355`, `test_mpps.py:369`); disabled node on port 104 (`test_echo.py:184`).
- Read-only inputs from the tree: `app/static/viewer/*.bundle.*.js` (`test_perf_serving.py:18-26`, raises `AssertionError` if no hashed bundle), `app/static/index.html` (`test_terms.py:12`), `app/static/admin/` (`test_admin_panel.py`). All three are present.
- Process-working-directory dependence: `test_licensing.py:216` snapshots `Path("paxs.lic")` relative to CWD (see MINOR-2), and `test_storage.py:56-69` writes a legacy relative-path fixture under CWD and removes it with `rmtree`.

---

## 2. Test inventory

621 test functions in 77 modules, 2,618 `assert` statements. Counts are from the AST pass; "Module(s) under test" is from the imports and docstrings.

| Test file | Module(s) under test | Tests | Behaviours covered |
|---|---|---|---|
| test_accelerator.py | services/accelerator_service, dicomweb_client; POST /api/v1/nodes/{ae}/web/upload?accelerated=1 | 12 | Parallel chunked STOW push, NDJSON progress, batching, transient retry, partial rejection, plain-HTTP fallback, all-fail 502, SSRF recheck 422, scope and destructive-role gate |
| test_admin_panel.py | api/v1/admin_panel, services/admin_panel_service; /admin mount | 16 | /admin mount and bridge asset, status/config/actions API, port clamping, auth and ADMIN-role gate on actions (PHYSICIAN is 403) |
| test_anonymizer.py | services/anonymizer; /api/v1/export | 5 | PHI removal, consistent UID remap, anonymized ZIP export, auth, 404 |
| test_auth.py | api/v1/auth | 4 | First user becomes ADMIN, duplicate 409, login ok or 401, /me needs token |
| test_auth_defenses.py | api/v1/auth, core/config, AuthAuditLog | 16 | Lockout on the 5th failure (not the 4th), generic error text, unknown user never locks, audit rows for login, lockout, logout, refresh, theft, security headers, audit admin endpoints |
| test_cfind.py | dicom/sender (send_find, query_remote_node_sync), services/prefetch, nodes/{ae}/find | 13 | C-FIND SCU against own SCP at all levels, wildcards, filters, unknown and disabled node, REST validation, destructive-role 403 |
| test_cget.py | dicom/sender (send_get, pull_from_node_sync), nodes/{ae}/pull | 12 | C-GET SCU, idempotent re-pull, partial failure continues, VNA tenant tagging, REST error paths, role 403 |
| test_clinical_protocols.py | services/clinical_protocol_service; /api/v1/clinical-protocols | 10 | CRUD, curation-role writes, description matching, enabled flag, priority tie-break, ranked suggestions |
| test_compression.py | services/compression_service, storage_service.compress, worker | 7 | RLE, JPEG-LS and HTJ2K round trips, idempotence, codec fallback, `check_codecs` CLI exit code, default HTJ2K via worker. Four tests `skipif` when the encoder is absent |
| test_config.py | api/v1/config, services/settings_service | 13 | Registry shape, PATCH admin-only (403), unknown key, wrong type, invalid option, empty batch, persist and live apply, restart flag, stray stored keys |
| test_critical_results.py | services/critical_results_service; /api/v1/critical-results | 3 | Create and acknowledge via service, list filters, API |
| test_cstore.py | dicom/sender (send_file, send_to_node_sync), nodes/{ae}/send | 12 | C-STORE forward at study and series scope, audit, partial failure, VNA scoping, destructive-role 403, S3 materialize. **Imports a broken module (Section 0)** |
| test_dashboard_services.py | api/v1/dashboard (/health, /stats) | 5 | DIMSE service matrix (8 services), web and integration flags, viewer base URL helper |
| test_db_perf.py | app/db/perf, api/v1/db_panel | 5 | Perf panel shape, slow-query toggle, named and custom EXPLAIN, replica toggle, SQL guard rejects DROP and DELETE (422) |
| test_dicomdir.py | services/dicomdir_service; /api/v1/export/dicomdir | 3 | DICOMDIR hierarchy, export package service, API |
| test_database.py | api/v1/database, services/db_admin | 14 | Status shape, admin gate, MySQL probe errors, save config to temp .env, SQLite reset, idempotent init, schema-drift stamp, backup lifecycle, backup name validation |
| test_dicomweb.py | api/v1/dicomweb, services/dicomweb_service, wado_rs_bulk_service, dashboard search | 22 | STOW multipart (chunked, 413 per-part and total caps, summary), QIDO exact, wildcard, date range, series and instances, WADO-RS instance, study, metadata, rendered, thumbnail, frames, dashboard search and sort |
| test_dose.py | services/dose_service; /api/v1/dose | 3 | RDSR parse and index, patient cumulative dose, API |
| test_echo.py | dicom/sender (send_echo), receiver C-ECHO, nodes/{ae}/ping | 9 | C-ECHO round trip, echo-only peer interop, graceful failure for unreachable and non-DICOM peers, REST ping errors |
| test_fusion.py | services/fusion_service; /api/v1/fusion | 6 | Frame-of-reference grouping, same-FoR pairs, single series, unknown study 404, tenant scoping, non-volumetric series |
| test_fhir.py | api/v1/fhir | 8 | CapabilityStatement, Patient search and read, ImagingStudy read and search, 404 and 400 on bad IDs. **No auth tests** |
| test_gsps.py | services/gsps_service, api/v1/gsps, STOW | 15 | GSPS parse (rotation, flip, text, shutter, multiple refs), STOW and list and get by UID, rendering transforms (one weak assertion, Section 4) |
| test_hanging_protocols.py | services/hanging_protocol_service; /api/v1/hanging-protocols | 1 | One monolithic test covering CRUD and matching |
| test_hl7.py | dicom/hl7 (ADT parse, listener), HIS attach | 8 | ADT^A01 parse, non-ADT rejection, missing PID, listener upsert, DICOM and C-STORE attach to HL7 patient |
| test_ian.py | services/ian_service; /api/v1/ian | 3 | IAN N-CREATE dataset, notify service, API |
| test_kin.py | services/kin_service; /api/v1/kin | 3 | KO or KIN synthesis and indexing, find by instance, API |
| test_ldap.py | api/v1/ldap | 1 | Login contract: 401 on bad password; 200 with provisioning fields on success. The success path implies a stub (not verified) |
| test_licensing.py | core/licensing/* (crypto, deps, fingerprint, manager); app.main; /api/v1/license | 6 | Fingerprint format and stability, RSA sign and tamper detection, encrypted container, manager lifecycle and expiry, API status and activation |
| test_lifecycle.py | services/lifecycle_service; /api/v1/lifecycle; worker sweep | 7 | Rule CRUD, archive and restore move objects, purge removes objects and rows, evaluate-and-run, purge 403 for PHYSICIAN, stats |
| test_lossy_compression.py | services/lossy_compression_service; /api/v1/compression/lossy | 3 | JPEG transcode of a dataset, rule-driven transcode run, rule CRUD |
| test_login_gate.py | app/main ViewerAuthGateMiddleware, /login, /logout, api/v1/auth | 22 | Anonymous redirect to /login with `next`, open-redirect block, valid, invalid and tampered cookie, role landing, HttpOnly cookie, logout clears cookie, gate off |
| test_migration.py | services/migration_service; /api/v1/migration | 4 | Folder scan and import (junk file counted as failed), delete-after-import, C-MOVE migration API validation |
| test_mip.py | services/volume_service (MIP and VRT); /api/v1/volume/{s}/mip | 9 | MIP, MinIP, average and VRT on three axes, slab offset and thickness, PNG, raw and npy, expanded multi-frame, guards (404, 400, 416, 503) |
| test_monitoring.py | services/compression_service.codec_status; /metrics; /api/v1/health | 5 | Codec availability per method, health exposes codecs and config mirrors it, metrics series, HTTP counter |
| test_mpps.py | dicom/receiver (MPPS N-CREATE and N-SET), services/mpps_service, /api/v1/mpps | 15 | N-CREATE and N-SET lifecycle with SPS status transitions, idempotent create, 0112 for unknown instance, C-STORE completes SPS, REST read, SCU helpers, unreachable peer. 12 `time.sleep` calls |
| test_multiframe_volume.py | services/volume_service (Enhanced CT), /api/v1/volume | 13 | Enhanced CT expansion to frames, z-order, rescale, dimension-index ordering, fallbacks, parity with WADO frames |
| test_multi_storage.py | api/v1/storage, services/storage (**missing**) | 11 | Backend CRUD with 401 and 403, validation, default backend, delete guard, factory resolution, ingest and move. **Broken import** |
| test_mysql_default.py | core/config (default URL), services/db_admin (URL build), models (index metadata) | 4 | Default DATABASE_URL is mysql+aiomysql with utf8mb4, URL builder, performance indexes declared |
| test_nodes.py | api/v1/nodes, dicom/receiver C-MOVE, dicomweb WADO-URI, services/webhook_service | 11 | Node CRUD and admin gate, ping, C-MOVE to registered and unknown destination, WADO-URI, webhook fires only for new study, C-MOVE SCU |
| test_orm.py | dicom/hl7 (ORM^O01), services/worklist_service, MLLP | 13 | NW, XO, CA, OC, DC, CR order handling, idempotent resend, cancel scope, MWL visibility, MLLP end to end. 7 `time.sleep` calls |
| test_oru.py | services/report_service, api/v1/reports, dicom/hl7 ORU | 9 | Report CRUD, ORU^R01 structure and escaping, finalize over MLLP, EMR down then resend, double finalize idempotent, PHYSICIAN 403 on finalize |
| test_parser.py | dicom/parser.parse_metadata | 2 | Curated field extraction, missing tags tolerated |
| test_peer_review.py | services/peer_review_service, api/v1/peer_review | 2 | RADPEER workflow (one monolithic test), invalid score rejected |
| test_perf_serving.py | app/main GzipCompressionMiddleware, static cache headers, services/thumbnail_service, redis | 15 | gzip on bundle and JSON, no gzip on DICOM binary, immutable and no-cache headers, pre-generated and on-the-fly thumbnails, Redis-cached thumbnails |
| test_prefetch.py | services/prefetch_service, api/v1/prefetch | 2 | Rule evaluation queries remote node and creates job, rule CRUD API |
| test_priors.py | services/prior_service, api/v1/priors | 2 | Prior matching by modality and body part, relevance score, API |
| test_ratelimit.py | api/ratelimit | 4 | 429 over limit, independent windows per IP and route, headers, disabled flag. Unit-level only |
| test_reading_worklist.py | services/reading_worklist_service, api/v1/worklist_tasks | 6 | UNREAD, DICTATED, READ and STAT states, STAT overlays, assign radiologist, auth and unknown study |
| test_receiver.py | dicom/receiver (C-STORE, C-ECHO, C-FIND SCP) plus indexing, storage, compression job | 3 | Full C-STORE pipeline (file, rows, audit, compression job), C-ECHO, C-FIND at study level |
| test_redis_panel.py | api/v1/redis_panel, services/redis_service, warm, volume and frame caches | 10 | Status open vs authenticated, admin-only server start, ping, key browser, configure (no .env write), volume and frame cache visibility (environment-dependent, Section 4) |
| test_replication.py | services/replication_service, api/v1/replication, /health/live, /health/ready | 11 | Target resolution, idempotent enqueue, sweep to named target, failure records, requeue on new instance, auth and admin gates, health probes. **Broken import** |
| test_routing.py | services/routing_service, api/v1/routing | 3 | Rule create and evaluate with priority, tag filters, CRUD API |
| test_routing_queue.py | services/routing_service (queue), api/v1/routing | 9 | Idempotent enqueue, batch dispatch over DIMSE, exponential retry to FAILED, manual retry, evaluate enqueues, API auth, PHYSICIAN refused, migration up, down, up |
| test_scalability.py | services/storage_service (cached usage), services/worker (claim), api/v1/dicomweb (QIDO keyset) | 8 | TTL cache hit and invalidate, claim (sequential, not concurrent), partial pending claim, worker entrypoint import, keyset pagination and filters |
| test_scripting.py | services/scripting_service, api/v1/scripts | 13 | Discovery, dispatch, event filter, `on_<event>` handler, crash isolation, mtime hot reload, `.disabled` marker, ingest hook, run now, API auth and admin gate. Sleep-based negatives |
| test_sessions.py | core/security (JWT, refresh hash), api/v1/auth, db Session | 15 | Session rows, short-lived access token, refresh rotation, replay revokes family, unknown or missing refresh 401, expiry, logout revocation, cookie /me, cookie precedence over bearer |
| test_sessions_admin.py | services/worker (prune), api/v1/users (sessions) | 10 | Prune expired and revoked, active listing, per-session revoke, revoke all of a user, unknown 404, audit event, RADIOLOGIST 403 |
| test_share.py | services/sharing_service, api/v1/share | 4 | Create share with QR PNG, public read, validation, PHYSICIAN cannot delete share |
| test_split_merge.py | services/split_merge_service, api/v1/studies | 3 | Split series to new study, merge studies, API |
| test_storage.py | services/storage_service | 7 | Key layout and SHA-256, legacy absolute and CWD-relative path resolution, delete study and instance files, usage count, UID sanitization of untrusted input |
| test_storage_backend.py | services/storage (**missing**), fake S3 | 9 | Backend selection, STOW to WADO through backend, S3 save, read, exists, delete, presigned URL, usage, missing-read error. **Broken import** |
| test_storage_commit.py | dicom/receiver (N-ACTION, N-EVENT-REPORT), services/storage_commit_service, dicom/commit_sender, api/v1/commitments | 7 | Success, auto-report after C-STORE, 0112 unknown instance, 0110 missing file, node-fallback retry, REST list and auth. Module-global `received` list (Section 4) |
| test_stow_upload.py | services/dicomweb_client (STOW SCU), nodes web upload | 9 | Real STOW to mock server, series and instance scope, partial failure, 404, 409, 422, 502, PHYSICIAN 403 |
| test_terms.py | app/main /terms route, static index.html | 2 | /terms markdown contains required clauses; footer modal markup present (string match on HTML source) |
| test_triage.py | services/triage_service, worker, api triage | 11 | Pneumothorax heuristic (positive, negative, non-CT, corrupt pixels), worker triage once, late instance requeues, list and review, auth, delete cleanup |
| test_tls.py | dicom/tls, dicom/receiver TLS | 5 | TLS C-ECHO and C-STORE, plaintext refused, untrusted client refused, self-signed generation. Module-level `skipif` |
| test_users.py | api/v1/users | 9 | Admin gate (403), CRUD, validation, reset password, disabled account blocked, self and last-admin guards, self-register cannot mint admin |
| test_viewer.py | app/main viewer mount, dicomweb (OHIF and PaxsViewer request shapes) | 12 | QIDO with OHIF accept header, fuzzy patient name, WADO-RS study multipart, instance, metadata, series metadata, WADO-URI legacy, viewer mounts |
| test_vna.py | api/v1/tenants, services/tenant_service, receiver isolation, dicomweb scoping | 7 | Tenant CRUD and permissions, AE-title normalization, ingest, C-FIND and C-MOVE isolation, QIDO scoping, legacy release on tenant delete |
| test_volume.py | services/volume_service, api/v1/volume | 11 | Info metadata, z-order by position, raw, rescaled and npy data, sagittal orientation, guards, max-slices cap, non-volumetric skip |
| test_volume_mpr_slice.py | api/v1/volume /mpr/slice | 8 | Axial, sagittal and coronal vs ground-truth voxels, oblique bilinear, PNG, raw and npy, window override, guards |
| test_wado_auto_archive.py | services/dicomweb_client, services/lifecycle_service via web retrieve | 3 | WADO pull with `auto_archive` tiers to `.archive/`, default stays online, global setting archives every pull |
| test_wado_bulk.py | services/wado_rs_bulk_service, api/v1/dicomweb frames and bulk | 2 | Single and multi-frame extraction, frame and bulk API |
| test_warm_service.py | services/warm_service, services/redis_service | 9 | Warm pre-generates thumbnails and Redis keys, bounded warm, idempotent, disabled no-op, dedup, DICOMweb triggers, frames unaffected (dedup test is racy, Section 4) |
| test_web_nodes.py | services/dicomweb_client (validate_web_url SSRF guard), api/v1/nodes web fields, QIDO and WADO via node | 12 | SSRF guard blocks loopback, private and metadata addresses by default, bad URL shapes, node web-field CRUD and SSRF check on create, web query, WADO retrieve ingests, 502 surfacing, role 403 |
| test_worklist.py | services/worklist_service, dicom/receiver MWL, dicom/hl7 ADT, api/v1/worklist | 13 | MWL C-FIND keys and wildcards, canceled hidden, ADT schedules step, C-STORE completes step, accession link, API CRUD and role gating |
| test_xdsi.py | services/xdsi_service, api/v1/xdsi | 2 | XDS-I.b register and ITI-18 query, API |

Notes on the inventory:
- The 77 modules are `test_*` files only. `conftest.py`, `helpers.py`, and `__init__.py` are infrastructure.
- Per-module counts sum to the 621 AST-counted test functions (verified).

---

## 3. Coverage map

Legend: **DIRECT** = a test module imports the module. **INDIRECT** = reached through an API route or DIMSE path that tests exercise. **NO TESTS** = no test module imports it and no test reaches it through a route or DIMSE path. "Routes referenced" means at least one test string matches the route path.

### 3.1 app/services/*.py

| Module | Coverage | Covered by |
|---|---|---|
| accelerator_service.py | DIRECT | test_accelerator |
| admin_panel_service.py | INDIRECT | test_admin_panel |
| ai_service.py | **NO TESTS** | api/v1/ai.py has 0 of 4 routes referenced |
| anonymizer.py | DIRECT | test_anonymizer |
| audit_service.py | INDIRECT | used by dashboard, dicomweb, nodes, critical_results, dicomdir, dose; asserted through those tests |
| clinical_protocol_service.py | DIRECT | test_clinical_protocols |
| compression_service.py | DIRECT | test_compression, test_monitoring |
| critical_results_service.py | DIRECT | test_critical_results |
| db_admin.py | DIRECT | test_database, test_mysql_default |
| db_panel.py | INDIRECT | test_db_perf |
| dicomdir_service.py | DIRECT | test_dicomdir |
| dicomweb_client.py | DIRECT | test_accelerator, test_stow_upload, test_web_nodes, test_wado_auto_archive |
| dicomweb_service.py | DIRECT | test_dicomweb |
| dose_service.py | DIRECT | test_dose |
| fusion_service.py | INDIRECT | test_fusion |
| gsps_service.py | DIRECT | test_gsps |
| hanging_protocol_service.py | INDIRECT | test_hanging_protocols |
| ian_service.py | DIRECT | test_ian |
| imaging.py | INDIRECT | test_dicomweb, test_perf_serving, test_warm_service |
| indexing_service.py | DIRECT | test_accelerator, test_compression, test_critical_results, test_dicomdir, plus most ingest tests |
| kin_service.py | DIRECT | test_kin |
| ldap_service.py | INDIRECT | test_ldap (API only) |
| lifecycle_service.py | DIRECT | test_lifecycle, test_wado_auto_archive |
| lossy_compression_service.py | DIRECT | test_lossy_compression |
| migration_service.py | DIRECT | test_migration |
| mpps_service.py | INDIRECT | test_mpps (via dicom/receiver) |
| node_service.py | INDIRECT | test_nodes, test_cstore, test_storage_commit |
| peer_review_service.py | INDIRECT | test_peer_review |
| pix_pdq_service.py | **NO TESTS** | api/v1/pix_pdq.py has 0 of 9 routes referenced; reached only from the HL7 path |
| prefetch_service.py | DIRECT | test_prefetch, test_cfind |
| prior_service.py | DIRECT | test_priors |
| query_service.py | INDIRECT | test_dicomweb, test_fhir, test_share, test_cfind |
| quota_service.py | **NO TESTS** (behaviour) | Called on every ingest via storage_service.check_quota_before_write, but no test mentions quota |
| reading_worklist_service.py | INDIRECT | test_reading_worklist |
| redis_service.py | DIRECT | test_perf_serving, test_warm_service (test_redis_panel via API) |
| replication_service.py | DIRECT | test_replication (module imports a missing package) |
| report_service.py | DIRECT | test_oru |
| routing_service.py | DIRECT | test_routing, test_routing_queue |
| scripting_service.py | DIRECT | test_scripting |
| settings_service.py | DIRECT | test_config |
| sharing_service.py | INDIRECT | test_share |
| split_merge_service.py | DIRECT | test_split_merge |
| storage_commit_service.py | INDIRECT | test_storage_commit |
| storage_service.py | DIRECT | test_storage, test_accelerator, test_cget, test_compression, test_critical_results (import chain broken, Section 0) |
| storage/ (package) | **MISSING** | test_storage_backend, test_multi_storage, test_replication, test_cstore (Section 0) |
| tenant_service.py | INDIRECT | test_vna |
| thumbnail_service.py | DIRECT | test_perf_serving |
| triage_service.py | DIRECT | test_triage |
| volume_service.py | DIRECT | test_volume, test_volume_mpr_slice, test_mip, test_multiframe_volume |
| wado_rs_bulk_service.py | DIRECT | test_wado_bulk, test_dicomweb |
| warm_service.py | DIRECT | test_warm_service, test_redis_panel |
| webhook_service.py | INDIRECT | test_nodes (webhook tests) |
| worker.py | DIRECT | test_compression, test_scalability, test_sessions_admin, test_triage |
| worklist_service.py | INDIRECT | test_worklist, test_orm, test_mpps |
| xdsi_service.py | DIRECT | test_xdsi |

Services with **no test of their own behaviour**: `ai_service`, `pix_pdq_service`, `quota_service`, plus `audit_service` (only indirectly asserted).

### 3.2 app/api/v1 routers (route level)

"With 401/403" = at least one referencing test function contains a 401 or 403 integer constant. This is a heuristic, not a proof that the check targets the route.

| Router file | Routes | Referenced | With 401/403 | Test files |
|---|---|---|---|---|
| admin_panel.py | 5 | 5 | 4 | test_admin_panel |
| ai.py | 4 | **0** | 0 | **none** |
| auth.py | 5 | 5 | 5 | test_auth, test_auth_defenses, test_login_gate, test_sessions |
| clinical_protocols.py | 7 | 7 | 5 | test_clinical_protocols |
| commitments.py | 1 | 1 | 1 | test_storage_commit |
| config.py | 2 | 2 | 2 | test_config |
| critical_results.py | 5 | 5 | **0** | test_critical_results |
| dashboard.py | 13 | 10 | 3 | test_dashboard_services, test_dicomweb |
| database.py | 8 | 8 | 6 | test_database |
| db_panel.py | 5 | 5 | 1 | test_db_perf |
| dicomdir.py | 2 | 2 | **0** | test_dicomdir |
| dicomweb.py | 19 | 15 | **0** | test_dicomweb, test_viewer, test_perf_serving, test_wado_bulk, test_gsps, test_monitoring |
| dose.py | 4 | 4 | **0** | test_dose |
| export.py | 1 | 1 | 1 | test_anonymizer |
| fhir.py | 5 | 5 | **0** | test_fhir |
| fusion.py | 1 | 1 | **0** | test_fusion |
| gsps.py | 3 | 3 | **0** | test_gsps |
| hanging_protocols.py | 5 | 5 | **0** | test_hanging_protocols |
| ian.py | 3 | 3 | **0** | test_ian |
| kin.py | 4 | 4 | **0** | test_kin |
| ldap.py | 1 | 1 | 1 | test_ldap |
| license.py | 8 | 3 | **0** | test_licensing |
| lifecycle.py | 10 | 10 | 1 | test_lifecycle |
| lossy_compression.py | 6 | 6 | **0** | test_lossy_compression |
| metrics.py | 1 | 1 | **0** | test_monitoring |
| migration.py | 4 | 4 | **0** | test_migration |
| mpps.py | 1 | 1 | 1 | test_mpps |
| nodes.py | 11 | 11 | 10 | test_nodes, test_echo, test_cfind, test_cget, test_cstore, test_accelerator, test_stow_upload, test_web_nodes, test_wado_auto_archive |
| peer_review.py | 6 | 6 | **0** | test_peer_review |
| pix_pdq.py | 9 | **0** | 0 | **none** |
| prefetch.py | 7 | 7 | **0** | test_prefetch |
| priors.py | 2 | 2 | **0** | test_priors |
| quota.py | 8 | **0** | 0 | **none** |
| redis_panel.py | 13 | 7 | 1 | test_redis_panel |
| replication.py | 4 | 4 | 2 | test_replication |
| reports.py | 9 | 6 | 3 | test_oru |
| routing.py | 9 | 8 | 3 | test_routing, test_routing_queue |
| scripts.py | 6 | 6 | 3 | test_scripting |
| settings.py | 7 | **0** | 0 | **none** (organization and templates) |
| share.py | 4 | 4 | 3 | test_share |
| split_merge.py | 2 | 2 | **0** | test_split_merge |
| storage.py | 6 | 6 | 2 | test_multi_storage (broken import) |
| tenants.py | 10 | 10 | 7 | test_vna |
| users.py | 11 | 11 | 10 | test_users, test_sessions_admin |
| volume.py | 4 | 4 | **0** | test_volume, test_volume_mpr_slice, test_mip, test_multiframe_volume |
| worklist.py | 4 | 4 | 4 | test_worklist, test_orm, test_mpps |
| worklist_tasks.py | 6 | 6 | 2 | test_reading_worklist |
| xdsi.py | 4 | 4 | **0** | test_xdsi |
| app/main.py (direct routes) | 18 | 13 | 2 | test_terms, test_login_gate, test_perf_serving, test_viewer, test_monitoring |
| app/api/ratelimit.py | (dependency) | - | - | test_ratelimit |

Router-level picture: 294 routes total, 239 referenced by at least one test (81%), 84 with a 401/403 check in the referencing test. Most negative-auth coverage is concentrated in the users, tenants, nodes, and auth routers.

### 3.3 app/dicom, app/core, app/db

| Module | Coverage | Covered by |
|---|---|---|
| dicom/receiver.py | DIRECT | test_receiver, test_tls (and most DIMSE tests via the `receiver` fixture) |
| dicom/sender.py | DIRECT | test_cfind, test_cget, test_cstore, test_echo |
| dicom/hl7.py | DIRECT | test_hl7, test_orm, test_worklist (test_dicomweb also reaches it) |
| dicom/parser.py | DIRECT | test_parser (plus most ingest tests) |
| dicom/commit_sender.py | INDIRECT | test_storage_commit |
| dicom/tls.py | DIRECT | test_tls |
| core/config.py | DIRECT | test_admin_panel, test_auth_defenses, test_compression, test_config, test_mysql_default, and others |
| core/security.py | DIRECT | test_sessions, test_sessions_admin, test_redis_panel |
| core/licensing/__init__.py, crypto.py, deps.py, fingerprint.py, manager.py | DIRECT | test_licensing |
| api/ratelimit.py | DIRECT | test_ratelimit (dependency level only) |
| api/v1/deps.py | INDIRECT | every auth-gated test |
| db/models.py | DIRECT | many |
| db/session.py | DIRECT | many |
| db/perf.py | DIRECT | test_db_perf |
| main.py | DIRECT | test_terms, test_login_gate, test_perf_serving, test_monitoring, test_viewer, test_licensing |

### 3.4 Routes that no test references (55 of 294)

- `api/v1/pix_pdq.py` (9): all PIX and PDQ routes, including assigning-authority and identity writes.
- `api/v1/quota.py` (8): all quota routes, including `/api/v1/quotas/status/*`.
- `api/v1/ai.py` (4): `/api/v1/ai/models`, `/infer`, `/mask/{series}`, `/sr`.
- `api/v1/settings.py` (7): `/api/v1/settings/organization` (GET, POST), `/api/v1/templates` (GET, POST), `/{id}` (GET, PATCH, DELETE).
- `api/v1/license.py` (5): `GET /api/v1/license`, `reload`, `upload`, `activate-online`, `validate-online`.
- `api/v1/redis_panel.py` (6): `/test`, `/flush`, `/info`, `/server/stop`, `/server/restart`, `/server/install`.
- `api/v1/dashboard.py` (3): `GET /api/v1/audit`, the study series instance listing, and the thumbnail PNG.
- `api/v1/dicomweb.py` (4): top-level `/dicom-web/series`, `/dicom-web/instances`, `/dicom-web/studies/{uid}/instances`, `/dicom-web/studies/{uid}/series/{uid}`.
- `api/v1/reports.py` (3): `GET /study/{uid}`, `POST /save`, `GET /print/{uid}`.
- `api/v1/routing.py` (1): `POST /api/v1/routing/evaluate/{study_uid}`.
- `app/main.py` (5): `/`, `/viewer/index.html`, `/settings/`, `/login/`, `/paxs-viewer-guide`.

Caveat: route matching is textual. Tests that build URLs from variables (`base + "/x"`) or split a path across concatenated constants can be missed, so a few of these may be tested in a way the matcher cannot see.

---

## 4. Quality observations

Severity: CRIT / MAJOR / MINOR as defined at the top. File:line references are the cited sites.

### 4.1 Security and authorization

**CRIT-1. The suite cannot import (see Section 0).** `conftest.py:37` to `storage_service.py:35`. Every test is blocked.

**CRIT-2. Destructive database fixtures bound to whatever `DATABASE_URL` is.**
- `conftest.py:18-20`: `os.environ.setdefault("DATABASE_URL", ...)`. An already-exported value wins, including one a developer set for the dev server.
- `conftest.py:43` and `:46`: session-autouse `schema` fixture runs `Base.metadata.drop_all` before and after the whole session.
- `conftest.py:63-65`: every `client` test runs `DELETE FROM` every table before the body. `conftest.py:81-83` does the same for `receiver`.
- The documented MySQL invocation (`conftest.py:15-16`, `mysql+aiomysql://user:pass@127.0.0.1:3307/paxs_db`) names the database `paxs_db`, the same name that appears in `test_database.py:162`. Nothing checks the name, host, or a "test" marker before wiping.
- The `conftest.py:3` docstring says nothing touches a real archive. That is only true when the variable is unset.
- Fix: require an explicit `PAXS_TEST_DATABASE_URL` and refuse any non-SQLite URL unless it names a database ending in `_test` and an opt-in flag is set.

**CRIT-3. Anonymous DICOMweb switch is dead code, and the unauthenticated routes have no tests.**
- `DICOMWEB_PUBLIC` is documented as the control for anonymous QIDO, WADO, and STOW (`README.md:827`, `.env.example:68`, `deploy/PAXSVIEWER-INTEGRATION.md:22`, and the admin label at `app/services/settings_service.py:57-61`). It is defined with default `True` at `app/core/config.py:253`. A repo-wide Grep finds no code that reads it.
- The enforcement that does exist is weak. QIDO uses `get_optional_user` (`app/api/v1/dicomweb.py:197-199`, `:256-260`, `:277-283`, STOW at `:804-808`). WADO-URI (`:136-137`), WADO-RS study, series, instance, metadata, rendered, thumbnail, frames, and bulk (`:327-742`) take only `get_db`.
- FHIR routes (`app/api/v1/fhir.py:124-126`, `:158-159`, `:170-172`, `:234-235`) and GSPS routes (`app/api/v1/gsps.py:44-64`) have no auth dependency at all. These expose patient search and presentation state.
- `ViewerAuthGateMiddleware` (`app/main.py:268-331`) gates only `/`, `/viewer`, `/studies-ui`, and `/settings`. `app/main.py:452` is the only middleware registration for it.
- Negative coverage for these routers is zero: DICOMweb 0 of 15 referenced routes, FHIR 0 of 5, GSPS 0 of 3.
- Action: confirm the intended policy for each endpoint. If auth is required, the current code fails it. If anonymous access is intended, the `DICOMWEB_PUBLIC=false` toggle must actually do something and needs a test.

**MAJOR-1. Settings and routers with auth dependencies but no 401/403 test.** These have role dependencies in code (so exposure is not evident) but no test asserts the gate: `quota.py` (8 routes, 9 auth refs, 0 tests), `pix_pdq.py` (9 routes, 11 auth refs, 0 tests), `ai.py` (4 routes, 8 auth refs, 0 tests), `settings.py` (7 routes, 5 auth refs, 0 tests). Also `redis_panel.py`: `test_redis_server_start_requires_admin` checks `/server/start` only, so `/flush`, `/server/stop`, `/server/restart`, `/server/install` have no gate test. `license.py` `upload` and `activate-online` have no gate test.

**MAJOR-2. Routers with role dependencies but no 401/403 assertion among referenced routes** (regression risk): `critical_results.py` (5), `dicomdir.py` (2), `dose.py` (4), `fusion.py` (1), `gsps.py` (3), `hanging_protocols.py` (5), `ian.py` (3), `kin.py` (4), `lossy_compression.py` (6), `migration.py` (4), `peer_review.py` (6), `prefetch.py` (7), `priors.py` (2), `split_merge.py` (2), `volume.py` (4), `xdsi.py` (4). Of these, `migration.py` (server-side folder import and C-MOVE), `prefetch.py` (rules that trigger remote queries), `split_merge.py` (mutates study structure), and `peer_review.py` (writes) are the most consequential.

**MAJOR-3. Role-named test that checks only anonymous access.** `test_db_perf.py:20-21` `test_perf_panel_requires_destructive_role` asserts `status in (401, 403)` on an anonymous request. It never tries a logged-in PHYSICIAN, so the role gate is untested. The name promises more than the body delivers.

**Positive finding.** Admin and role negative tests are real for most admin surfaces. Non-admin (PHYSICIAN, RADIOLOGIST) requests get 403 in: users (`test_users.py:41`), config PATCH (`test_config.py:61`), DB actions (`test_database.py:73`), storage (`test_multi_storage.py:88`), scripts (`test_scripting.py:223`), replication (`test_replication.py:179`), sessions console (`test_sessions_admin.py:137`), admin-panel actions (`test_admin_panel.py:186`), node registry (`test_nodes.py:71`), DIMSE and web triggers (`test_cfind.py:389`, `test_cget.py:384`, `test_cstore.py:351`, `test_web_nodes.py:223`, `test_stow_upload.py:285`, `test_accelerator.py:418`), lifecycle purge (`test_lifecycle.py:209`), ORU finalize (`test_oru.py:217`), routing queue (`test_routing_queue.py:475`), Redis start (`test_redis_panel.py:89`), worklist (`test_worklist.py:377`), share delete (`test_share.py:77`). So the direct answer to "does any test check that non-admin gets 403 on admin endpoints" is yes, for the surfaces above.

**MINOR-1. Auth-adjacent public endpoints without tests.** `metrics.py` (`/metrics`, no auth refs) and the FastAPI docs (`app/main.py:426-427`, `/api/docs` and `/api/openapi.json` public by default). Confirm the intent; no test pins it.

### 4.2 Isolation, global state, and order dependence

**MAJOR-4. Process-wide env var leaked.** `test_licensing.py:126` sets `os.environ["PAXS_LICENSE_PUBLIC_KEY"]` in `test_license_manager_lifecycle` with no restore. Only `test_licensing.py:249`, in a different test, pops it.

**MAJOR-5. Module-level and class-level mutable state shared across tests.**
- `test_storage_commit.py:30`: `received: list = []` module global, appended from SCU handler threads, never reset.
- `test_wado_auto_archive.py:39-40`: `_WebHandler.datasets: list = []` class attribute shared across tests.
- `test_warm_service.py:162-172`: see MAJOR-7.
- `test_scalability.py:145`: `invalidate_usage_cache()` runs at the end of the test body, not in `finally`, so a failing assertion leaves the cache populated for later tests.

**MAJOR-6. Cross-test-module imports (fragile coupling).** Test modules import helpers from other test modules, so renaming one helper breaks several files:
- `_store_via_scu` from `test_receiver.py` in `test_cfind.py:31`, `test_cget.py:28`, `test_cstore.py:26`, `test_nodes.py:21`, `test_storage_commit.py:27`, `test_hl7.py:160`, `test_mpps.py:219,376`, `test_worklist.py:157,263,296`.
- `_upload_study` from `test_dicomweb.py` in `test_anonymizer.py:10`, `test_fhir.py:3`, `test_monitoring.py:4`, `test_scalability.py:119,152,191,222`, `test_triage.py:8`; and `_upload` from `test_dicomweb.py` in `test_triage.py:77,109,177`.
- `_ingest_series` from `test_volume.py` in `test_mip.py:17`, `test_volume_mpr_slice.py:16`, `test_redis_panel.py:147,175,206`.
- `FakeS3Client` from `test_storage_backend.py` in `test_cstore.py:406`; `free_port` from `tests.conftest` in `test_storage_commit.py:25`.
- Fix: move these into `tests/helpers.py` or `conftest.py` fixtures.

**MAJOR-7. Warm-dedup test is racy and leaks state.** `test_warm_service.py:162-172` monkeypatches `warm_service._warm_worker` with a lambda. The real worker removes the UID from `_WARMING` (`app/services/warm_service.py:84-85`, inside `_warm_worker`). The patched version never does, so `"1.2.840.1"` stays in `_WARMING` for the rest of the process. The test also asserts on `calls` right after `schedule_warm` returns, with no join on the thread `schedule_warm` started (`warm_service.py:65-71`), so the list can still be empty when asserted.

**MAJOR-8. Environment-dependent Redis assertions.** `test_redis_panel.py:145-172` requires `paxs:vol:*` keys to appear in `/api/v1/redis/keys`. That needs a live Redis or the in-process fallback to be enumerable. `:164` `target_key = next(...)` raises `StopIteration` rather than a clear failure if no key is found. There is no skip guard. (Whether the fallback enumerates keys was not verified.)

**MINOR-2. Working-directory writes and no-op chdir.** `test_licensing.py:216` snapshots `Path("paxs.lic")` relative to the process working directory. The activation call most likely writes the configured license path (`LICENSE_FILE_PATH` defaults to `./paxs.lic`, `app/core/config.py:315`; `manager.py:200-220`). The test restores pre-existing bytes only (`:246-248`), so a file the activation created would be left in the repo root. Verify whether the default destination is used. `.gitignore` covers `*.lic`, so the risk is clutter, not a commit. `test_storage.py:69` is `os.chdir(os.getcwd())`, which does nothing. The intended restore is missing.

**MINOR-3. Temp directories are never removed.** `conftest.py:17`, `test_redis_panel.py:59`, and `test_database.py:44` call `mkdtemp` and never clean up.

### 4.3 Flakiness and timing

**MAJOR-9. Fixed sleeps used as synchronization.** 42 `time.sleep` calls across 29 test functions. The worst files are `test_mpps.py` (12 calls, 0.1-0.2 s), `test_orm.py` (7 calls, 0.3-0.8 s, listener start), `test_storage_commit.py` (7 calls), `test_hl7.py` (5 calls, 0.3-0.8 s, listener start), `test_nodes.py` (4 calls, including `:365` 0.5 s for webhooks), `test_worklist.py` (4 calls), and `test_cstore.py:388` (0.3 s). Under load these are flaky; on a fast machine they are wasted time. Replace with readiness polling (wait for the port, or the SCP's started state) and an explicit completion check.

**MAJOR-10. Negative assertions made vacuous by fixed sleeps.** `test_scripting.py:96` and `:153` sleep 0.4 s and then assert the handler did NOT log. If the handler thread is slower than 0.4 s, the test passes for the wrong reason. Pair the negative with a positive sentinel (fire a second event that must log, then assert the first left no trace).

**MINOR-4. Wall-clock dependence.**
- `test_worklist.py:248`: `date=datetime.now().strftime("%Y%m%d")` flakes across midnight and when the server and client timezones differ.
- `test_licensing.py` (`:56`, `:158`, `:174`), `test_sessions.py:57,140`, `test_sessions_admin.py:83`: `datetime.now` with mixed naive and aware datetimes.
- `test_scripting.py:46` `os.utime` mtime bump depends on filesystem mtime granularity (safe on NTFS).

**MINOR-5. Port and environment assumptions.** Hard-coded ports 9000, 10401-10411, 104, and "port 1 is refused" (Section 1.6). `conftest.py:69-72` `free_port` is a TOCTOU race. Skip-guards mask environment gaps: `test_compression.py:47,62,138,173` skip when encoders are missing, even though `requirements.txt:19-27` lists those codecs as required. A skipped test in CI is invisible unless someone counts skips.

### 4.4 Assertion quality

- Real zero-assertion tests: **none**. The AST pass flagged two, both false positives: `test_gsps.py:298` (`TestGSPSRendering.test_apply_flip`) uses `np.testing.assert_array_equal`, and `test_web_nodes.py:50` uses a `try/except/else` idiom.
- Status-only tests (every assert is on `status_code`): 94 functions. Of these, 91 include a non-2xx status check and are legitimate negative tests. Only 3 are positive-only without a body check: `test_login_gate.py:84`, `:91`, and `test_ratelimit.py:65`.
- Weak (truthiness or `is not None` only) tests: 6, at `test_mysql_default.py:40`, `test_orm.py:241`, `test_ratelimit.py:56`, `test_replication.py:126`, `test_scalability.py:175`, `test_storage_backend.py:85`.
- Overall: 2,618 asserts over 621 tests (average 4.2). Most tests check content, not just status.

**MAJOR-11. Tautological assertion.** `test_gsps.py:296`: `assert result.size == (64, 32) or result.size[0] != img.size[0]`. The first disjunct accepts an unrotated 64x32 image, so a no-op `apply_gsps_to_image` passes. Assert the rotated size `(32, 64)` exactly.

**MAJOR-12. Concurrency test that is not concurrent.** `test_scalability.py:115-142` is titled and documented as "Two workers claiming concurrently" (`:116-117`). The body calls `_claim_jobs` twice sequentially from two sessions. It proves that a second claim after a first one returns nothing, not that two overlapping claims are safe. Related production code, to verify: `app/services/worker.py:51-92` selects pending IDs, runs a guarded `UPDATE ... WHERE status='PENDING'`, then re-selects `WHERE id IN (ids) AND status='RUNNING'`. In an interleaving where worker A claims some of worker B's IDs, B's re-select can return A's rows, which would process the same job twice. That interleaving is narrow, but nothing tests it.

**MAJOR-13. Test named for a role but checking a different thing.** See MAJOR-3 (`test_db_perf.py:20`).

**MINOR-6. Style.** `test_web_nodes.py:50-58` and `:340-355` hand-roll `try/except ValueError: pass / else: raise` where `pytest.raises` (or parametrize) reads better and fails more clearly. `test_hanging_protocols.py:24`, `test_peer_review.py:25-102`, and `test_ldap.py:8` are single long tests covering several behaviours.

### 4.5 Duplication

Same-named helpers defined in many modules (AST count of `def` names in tests):

| Helper | Definitions | Notes |
|---|---|---|
| `_auth` | 16 | Register or log in, sometimes bootstrapping ADMIN first |
| `_get_auth_headers` | 15 | Same pattern, different password literals |
| `_register` | 10 | Passes role and full_name variously |
| `_auth_headers` | 7 | Same pattern |
| `_free_port` | 6 | Duplicates `conftest.free_port` |
| `_login` | 4 | Duplicates |
| `_ingest_study` | 4 | Each saves and indexes a study differently |
| `_create_node` | 3 | Duplicates |

The variation is real (some helpers handle the "first user is ADMIN" rule, others don't), which is why these diverged. Fix: `make_user(role)` and `admin_headers` fixtures in conftest, a `dicom_node` factory, and one `free_port`.

Also: the admin bootstrap rule (first registered user is ADMIN) is assumed in at least seven modules, each with its own workaround.

---

## 5. Recommended tests to add (prioritized)

**P0. Blockers and security**

1. Smoke test: import `app.main` and every `app.api.v1.*` and `app.services.*` module. Put it in its own file so a broken import fails in one obvious place. Pair with restoring `app/services/storage/` (Section 0).
2. DB safety guard test: a test that asserts `DATABASE_URL` is SQLite or a database name ending in `_test` before any fixture runs, and that `conftest.py` refuses otherwise (CRIT-2).
3. DICOMweb auth matrix: parametrize QIDO, WADO-URI, WADO-RS (study, series, instance, metadata, rendered, thumbnail, frames, bulk), and STOW across {anonymous, PHYSICIAN, ADMIN} x `DICOMWEB_PUBLIC` {true, false}. Expect 401 for anonymous when false. This fails today, which documents CRIT-3. Decide the intended behaviour first.
4. FHIR and GSPS anonymous access: assert the intended status (likely 401) for `/fhir/Patient`, `/fhir/ImagingStudy`, `/api/v1/gsps/{sop}`, `/api/v1/gsps/study/{uid}`.
5. Path traversal: POST `/api/v1/scripts` with a name like `../x` or `..\\x`; migration folder import with a path outside an allowed root; DICOMDIR export names; storage keys containing `..`. Add a negative to `test_storage.py:34-63`: an absolute legacy path outside `STORAGE_ROOT` must be refused, not resolved.
6. Rate limit end to end: `RATE_LIMIT_ENABLED=true` against `POST /api/v1/auth/login`, expecting 429 after N attempts. Current tests exercise only the dependency (`test_ratelimit.py:28-40`), and conftest disables the limiter globally (`conftest.py:28`).
7. Cookie-session hardening: `Set-Cookie` includes `Secure` and `SameSite` when `AUTH_COOKIE_SECURE` is set (`test_login_gate.py:123-132` checks HttpOnly only). Add a test that a cookie-authenticated mutating request from a cross-site origin is rejected, or document that it is accepted.
8. JWT negatives: `alg=none`, wrong signature, expired, token for a deleted or disabled user, against `core/security.decode_token`. `test_sessions.py` covers expiry only.

**P1. Concurrency and correctness**

9. Real concurrent claim: N threads (or processes against MySQL), each with its own `SyncSessionLocal`, released by a `threading.Barrier`, calling `CompressionWorker._claim_jobs`. Assert the union equals the pending set and no ID appears twice. Include an overlapping-ID case that exercises the re-select path (MAJOR-12). Run on MySQL in CI, since SQLite serialises.
10. Refresh-token race: two concurrent `/auth/refresh` calls with the same token; exactly one succeeds and the family is revoked. `test_sessions.py:95` covers only the sequential replay.
11. Routing queue: concurrent `process_queue_batch` calls must not dispatch the same COMPLETED item twice.
12. Duplicate C-STORE of one SOP instance from two associations at once: exactly one `Instance` row.
13. Rewrite `test_warm_service.py:162-172` to join the started thread (or patch `threading.Thread`) and assert `_WARMING` is empty afterwards (MAJOR-7).
14. Replace fixed sleeps (MAJOR-9) with readiness polling, and convert the vacuous negatives at `test_scripting.py:96,153` into a positive-sentinel pattern (MAJOR-10).

**P2. Coverage gaps (routes and modules)**

15. Quota enforcement: no test mentions quota (Section 3.1). Add tests that a STOW or C-STORE over the limit is refused, plus CRUD and the 403 gate on `/api/v1/quotas` (8 routes, none referenced).
16. PIX and PDQ: 9 routes and `pix_pdq_service` have no tests. Cover identity registration, query, and an admin gate on writes.
17. AI endpoints: `/api/v1/ai/infer` and `/api/v1/ai/sr` create or run work. Cover auth (401 and 403) and the model-listing contract.
18. Settings and templates: `/api/v1/settings/organization` and `/api/v1/templates` (7 routes). Cover auth and the PATCH and DELETE paths.
19. Audit endpoint: `GET /api/v1/audit` (`dashboard.py`) has no test. Cover admin-only access and filtering.
20. Redis destructive routes: `/api/v1/redis/flush`, `/server/stop`, `/server/restart`, `/server/install` each need an anonymous and a PHYSICIAN 403 check (MAJOR-1).
21. License upload and online activation: `/api/v1/license/upload`, `/activate-online`, `/validate-online`, `/reload` need auth tests.
22. Reports: `POST /api/v1/reports/save` and `GET /api/v1/reports/print/{uid}` (PHI output) need auth and content tests.
23. Routing evaluate: `POST /api/v1/routing/evaluate/{study_uid}` (no tests) needs a tenant-scope check.
24. Metrics: decide whether `/metrics` should require auth. If yes, add the test; if not, add a test that pins the current anonymous behaviour so a change is deliberate.

**P3. Hygiene**

25. Move `_auth`, `_auth_headers`, `_get_auth_headers`, `_register`, `_login`, `_free_port`, `_create_node`, `_ingest_study`, `_upload_study`, `_ingest_series`, `_store_via_scu` into `tests/helpers.py` or conftest fixtures (`make_user(role)`, `admin_headers`, `dicom_node`, `free_port`). Replace the cross-test-module imports (MAJOR-6).
26. Add `pytest.ini`: `testpaths = tests`, registered markers (`dimse`, `mysql`, `slow`), `--strict-markers`, and `-ra` so skips are listed. Pin test dependencies in a dev requirements file. Add a CI workflow that runs against SQLite and MySQL and fails if the skip count changes unexpectedly.
27. Rename `test_db_perf.py:20` to match its body, and add a real PHYSICIAN 403 case (MAJOR-3).
28. Split the monolithic tests: `test_hanging_protocols.py:24`, `test_peer_review.py:25`, `test_ldap.py:8`.
29. Replace `datetime.now()` in `test_worklist.py:248` with a fixed date, or freeze time. Clean up `mkdtemp` directories (`conftest.py:17`, `test_redis_panel.py:59`, `test_database.py:44`).
30. Give `test_licensing.py:126` a fixture that restores `PAXS_LICENSE_PUBLIC_KEY`, and write the `paxs.lic` test into a `tmp_path` with `LICENSE_FILE_PATH` monkeypatched (`test_licensing.py:216`). Fix or remove the no-op `os.chdir` at `test_storage.py:69`.

---

## 6. Method and limits

- Static only: no test was collected or run, and no network or DB was touched. The analysis scripts parse source with `ast` and `re` only.
- The import-break finding is inferred from the static chain and the git and filesystem state. It was not reproduced by importing the module.
- Coverage is a heuristic. "Referenced" means a test string matches the route path. Route matching misses variable-built URLs. "With 401/403" means the referencing test function contains such a constant, not that the constant targets that route.
- Module "DIRECT" means a test imports it. "INDIRECT" means it is reached through a route or DIMSE path, which says nothing about line coverage.
- Items marked "verify" (DICOMWEB_PUBLIC enforcement, FHIR and GSPS intent, the worker re-select interleaving, Redis key enumeration) are static hypotheses that need confirmation before they are filed as defects.
