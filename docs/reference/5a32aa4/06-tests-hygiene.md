# Tests and repository hygiene — changes in 5a32aa4

> **Re-review of commit `5a32aa4`** (baseline `e90d32c`), part of the [PAXS Archive documentation](../../README.md). Static analysis, 2026-10-08; line numbers refer to `5a32aa4`. Baseline area reference: [10-tests.md](../10-tests.md). Findings are consolidated and prioritized in [CODE_REVIEW.md](../../CODE_REVIEW.md), which takes precedence where the two differ. Finding IDs here (N1, N-01, V4-01, …) are local to this file; CODE_REVIEW.md maps them to NEW-xx IDs.

Scope: tests (79 paths changed, 36 added, 43 modified, +13,129 / -188 lines) and hygiene of newly committed non-code files.

**Method and limits.** Static analysis only. Nothing in the repo was modified. The suite was not run and nothing was installed. Failures I predict below come from replaying the test logic statically (AST and text scans run from `local/`); they still need one real run to confirm. The secrets and PHI checks are pattern-based. They can prove presence, not absence.

Legend: 🔴 blocks the suite or leaks a secret, 🟡 real defect or exposure to fix soon, 🟢 hygiene or polish.

---

## 0. Headline

1. 🔴 **F-11 is still open.** `app/services/storage/` is absent from the working tree and from every commit. `.gitignore:9` (`storage/`) would hide it. `conftest.py:64` imports `app.main`, which fails. The suite collects 0 tests.
2. 🔴 **The vendor RSA private-key header is still in git history** (added in `8af1381`, present in `e90d32c`, removed in `5a32aa4`). The new secret scanner only checks the working tree, so it cannot see this.
3. 🔴 **One new security test fails on HEAD (static prediction).** `test_route_auth_matrix.py::test_every_route_is_public_by_allowlist_or_authenticated` flags `app/api/v1/metrics.py:182 GET /metrics`.
4. 🟡 **Several new tests pass for the wrong reason or accept the wrong status.** Examples: the tenant scan skips 27 study-id routes, the H03 exemption hides a route, and some auth checks accept any non-401/403 response.
5. 🟡 **Prior test findings were not fixed:** the `test_gsps` tautology, the `test_licensing` env leak, the `test_db_perf` role-named test, and the non-concurrent "concurrency" test in `test_scalability`.
6. 🟡 **Repo hygiene:** `.workbuddy-ai/` (56 tracked agent files), a foreign project's git index committed as `logo/index`, an unvetted `npx` MCP server in `.vscode/mcp.json`, test-written PNGs committed under `app/static/uploads/`, and `docs/` untracked.

---

## 1. Test infrastructure changes

### 1.1 F-11 status: NOT FIXED (the suite cannot import)

| Check | Result |
|---|---|
| `ls app/services/storage` | Does not exist. `app/services/` has `storage_service.py` and `storage_commit_service.py` only. |
| `git ls-files app/services/storage` | Empty. |
| `git log --all --name-only` for any `services/storage` path | No commit has ever contained it. |
| `git check-ignore -v app/services/storage/__init__.py` | `.gitignore:9:storage/`. The unanchored rule matches the directory name anywhere in the tree. |
| Importers | `app/services/storage_service.py:35,43`; `app/api/v1/storage.py:34` (router mounted at `app/main.py:670`); `app/services/replication_service.py:39`. Tests: `test_cstore.py:426`, `test_multi_storage.py:28`, `test_p1_hardening.py:572`, `test_replication.py:20`, `test_storage.py:7`, `test_storage_backend.py:16`, `test_storage_containment.py:25-26`. |
| Import chain | `tests/conftest.py:64` `from app.main import app` → `app/main.py:33` → `app/services/worker.py:21` → `storage_service.py:35` → `ModuleNotFoundError`. |
| Consequence | Pytest errors while loading conftest. 0 tests are collected, including the new guard tests. |
| Was it ever present locally? | Yes. The JUnit reports `.workbuddy-ai/c10.xml`, `final.xml`, `p1.xml` (hostname `DESKTOP-2P4RQ80`, 2026-09-23) record 1,155 to 1,191 executed tests with no `ModuleNotFoundError`. The author's machine likely still has the package. It is not in this checkout, the history, or `.workbuddy-ai/backups-phase2/` (which holds only 3 unrelated files). |

Fix: restore the package from the machine that produced those runs. Then change `.gitignore:9` to `/storage/`, run `git add app/services/storage/`, and confirm with `git check-ignore`. Re-run `git check-ignore` after the change to confirm it is no longer ignored.

### 1.2 Database guard (C08): fixed for the stated risk

- `tests/conftest.py:19-37`. The guard runs before any app import (`:64`). The old `os.environ.setdefault` is replaced by a direct assignment (`:35-37`), so an exported `DATABASE_URL` can no longer win silently. Good.
- `tests/_db_guard.py`:
  - Policy text `:8-17`. `TEST_DB_SENTINEL = "test"` at `:24`.
  - `is_disposable` `:58-65` tests `"test" in database_name(...)`, a substring match. It accepts `paxs_latest`, `contest_db`, `attestation_db`. 🟡 Use a token match, for example `re.search(r"(^|[_\-])test([_\-]|$)", name)`.
  - SQLite is checked on the file name only (`database_name` `:37-47`), so the directory is ignored.
  - Non-SQLite requires `PAXS_TEST_ALLOW_EXTERNAL_DB=1` (`:96-104`). This part is sound.
- `tests/test_test_db_guard.py`:
  - `:158-166`: a production-shaped URL aborts before the app is imported. This passes, because the guard fires first.
  - `:169-173`: positive control. `import tests.conftest` with no URL imports `app.main`, so it FAILS while F-11 persists.
  - `:142-155`: each run leaks a `paxs_test_*` temp dir (`conftest.py:26`). 🟢

### 1.3 Environment the suite forces (`conftest.py:35-60`)

| Line | Setting | Effect |
|---|---|---|
| `:54` | `ALLOW_REGISTER=true` | Config default is `false` (`app/core/config.py:207`). Tests self-register freely. |
| `:60` | `DICOMWEB_PUBLIC=true` | Config default is `false` (`config.py:427`). The whole suite runs DICOMweb in legacy public mode. See 3.6. |
| `:49` | `AUTH_GATE_ENABLED=false` | Viewer gate off for the general suite. |
| `:110` | `TEST_USER_PASSWORD = "TestPassw0rd!2026"` | Test-only fixture credential. |
| `helpers.py:139-140` | `BOOTSTRAP_PASSWORD = "secret123"` | Test-only fixture credential. |

### 1.4 Auth fixtures and helpers

- `conftest.py:113-131` `_create_test_user` writes users straight into the DB. That bypasses H02, so fixture-created privileged users never use the admin-API path. Acceptable for speed, but the provisioning path is covered only by `test_registration_guard.py` and `test_users.py`.
- 🟡 `conftest.py:200-206` `anonymous_client` is an alias for `client`. It is the same TestClient and the same cookie jar as `auth_client` and `radiologist_client`. Any login made through `client` makes later "anonymous" calls authenticated by cookie. The docstring claims isolation (`:103-108`, `:141-147`), but that holds only for the `Authorization` header. Fix: return a fresh `TestClient(app)` for anonymous requests.
- 🟢 `conftest.py:141-174` `_AuthedClient` injects the header only for get/post/put/patch/delete. `head`, `options`, `stream` and `websocket_connect` fall through `__getattr__` without auth. Grep found no current caller.
- 🟡 `helpers.py:157-188` `auth_for_role` accepts HTTP 409 (`:178`) and then logs in as the existing account. An existing account can keep the wrong role. Assert the role instead.
- 🟢 `raising=False` appears about 50 times across new and changed modules (examples: `test_route_auth_matrix.py:291,302,325,359,374,401,431,541,555`; `test_registration_guard.py:30-69`; `test_p1_hardening.py:154-155,175,456`; `test_ldap.py` 24 times; `test_tenant_isolation.py:256,272`; `test_storage_containment.py:217,223`). A renamed setting then becomes a silent no-op. Remove `raising=False` and fix the names.
- `helpers.py:143-216`: `admin_headers`, `auth_for_role`, `create_user`. These are the H02-compliant way to create roles.

### 1.5 Node harness (`tests/js/`)

- No `package.json` exists anywhere. The documented command is `node --test tests/js/` (`RUN_AND_BUILD_GUIDE.md:387`), plus per-file lines `:390-394`. Local Node is v24.14.0.
- `extract.js` (106 lines) runs real functions out of `app/static/viewer/paxs-mpr.js` in a `vm` sandbox, using brace matching. `phantom.js` (104 lines) builds synthetic voxel phantoms whose values encode their own coordinates.
- 🟡 `paxs-mpr-wiring.test.js` (16 tests) and parts of other suites assert on source text (`source.includes(needle)`, line 30-36). They test strings, not behaviour, and pass if a comment contains the needle.
- Open point: on Node 21 and later, `--test` arguments are glob patterns. Confirm that `node --test tests/js/` discovers the `.test.js` files. The safer form is `node --test tests/js/*.test.js`.

---

## 2. New test inventory and changed tests

### 2.1 New Python modules (24 test modules plus `_db_guard.py` helper; 437 test functions before parametrize expansion)

"#" counts `def test_` functions, including class methods.

| File | Module(s) under test | # | Behaviours covered |
|---|---|---|---|
| `test_fhir_tenant_isolation.py` | `api/v1/fhir.py` (Patient, ImagingStudy) | 6 | C05: Patient search/read/identifier probes scoped per tenant; ImagingStudy search still scoped. |
| `test_hybrid_engine.py` | `api/v1/volume.py` hybrid resolver | 27 | Pure decision matrix: client/server/split thresholds, zero WebGL, session and cache saturation, latency promotion, thresholds read from settings. |
| `test_hybrid_integration.py` | `volume.py` negotiate/info/data/mpr/mip/tiles | 28 | Split mode end to end; both pipelines available; load-ratio headers on every volume endpoint; mid-session re-evaluation. |
| `test_lossy_compression_safety.py` | `lossy_compression_service`, DICOM parser | 7 | C-2/3/4: 16-bit preserved; unencodable target aborts; eligibility matrix; already-lossy skipped; disable flag; atomic replacement. |
| `test_negotiate.py` | `volume /negotiate`, `settings_service` registry | 32 | Default hybrid; explicit modes; role override; invalid role map tolerated; overload; response headers; anonymous rejected; streaming disabled. |
| `test_no_embedded_secrets.py` | repo tree (no module) | 7 | PEM private-key scan of `app, deploy, scripts, tests, migrations, feature-plans` plus root files; vendor `config.php` and `crypto.php` fail-closed checks; gitignore covers on-disk key files. |
| `test_no_fabricated_clinical_content.py` | `study-list-ui/src` and `app/static/studies-ui` | 12 | C07: no fabricated demographics or report text; mock dashboard removed; finalize gated by role; UI actions bound to real endpoints. |
| `test_p1_hardening.py` | `api/ratelimit`, `deps`, `settings` (logo), `security`, config, `run_paxs` | 32 | H04 session expiry and reset revokes sessions; H07 XFF cap; H03 privileged mutations; logo upload validation; print escaping; production fail-closed config; HTTPS fallback; boto3 requirements. |
| `test_patient_identity.py` | `indexing_service` patient resolution, models | 14 | C10: same MRN in two tenants gives two patients; authority recorded; conflicting demographics quarantined; anonymous studies not merged; STOW isolation. |
| `test_prefetch_and_routing.py` | `prefetch_service`, `routing_service`, DICOM sender, node ping | 5 | H-1 prefetch dispatches move; prior-age cap; S3 temp files cleaned even when send fails; `/health/live` under 50 ms during a C-ECHO (timing, see 2.3). |
| `test_quota_and_atomic_ingest.py` | `quota_service`, storage staging, STOW and C-STORE | 8 | H08 reservation atomic and never negative; H09 staging publish/discard; delta charge on re-import; stale temp sweep; 507 and A700 when full. |
| `test_registration_guard.py` | `api/v1/auth` register, `core/roles`, config | 10 | H02: bootstrap admin; registration off by default; destructive roles refused; least privilege; allowed-roles setting; registered user gets 403 on delete. |
| `test_report_integrity.py` | `report_service`, reports API, HL7 ORU | 12 | C06: anonymous save rejected; author from the authenticated actor; finalized report immutable; finalize needs a destructive role; ORU sent once; addenda preserve the original. |
| `test_route_auth_matrix.py` | every `router.<verb>` in `api/v1` (AST); DICOMweb; share; viewer | 29 | C03 structural allowlist scan; PHI anonymous and authenticated checks; DICOMweb private vs public; share scope; viewer cookie; C04 tenant AST scan; H03 mutation guards. Detail in section 3. |
| `test_schema_authority.py` | `db_admin`, Alembic `env.py`, fusion registration | 10 | H11/H12/H18: drifted schema refused; stamp rules; `env.py` takes an explicit URL; fusion routes registered. |
| `test_split_merge_integrity.py` | `split_merge_service`, DICOM parser | 4 | C-1: split and merge rewrite the stored header; write failure rolls back. |
| `test_storage_containment.py` | `storage/local` (**package missing, F-11**), `storage_service` keys | 18 | C05: key component validation; escape on write and delete refused; links and junctions; absolute keys refused unless flagged; malformed UIDs in STOW and C-STORE. Cannot import until F-11 is fixed. |
| `test_tenant_isolation.py` | tenants, dashboard, DICOMweb, FHIR, share | 11 | C04: cross-tenant read, delete and append refused; admin archive-wide access; QIDO and FHIR do not leak; share token does not widen access; unassigned user sees legacy studies only. |
| `test_test_db_guard.py` | `tests/_db_guard.py`, conftest subprocess | 12 | Name parsing; accepted and refused URLs; opt-in; conftest aborts before app import; positive control (depends on F-11). |
| `test_viewer_role_overrides.py` | `volume` role map parse and resolve, negotiate, dashboard stats | 53 | Role-map parsing fail-safe (malformed, array, scalar, unknown modes); override priority; audit line carries no patient IDs or paths; dashboard viewer telemetry. |
| `test_volume_client_mode.py` | volume guards (slice, MIP), raw stream, companions | 35 | Client mode returns 503 with hint on slice and MIP; force bypass; raw streaming values; companion endpoints available. |
| `test_volume_geometry.py` | `volume_service` plane math | 8 | C09: axial, sagittal and coronal row/column convention; MIP projection; patient transform; orientation labels. |
| `test_volume_server_mode.py` | volume server mode, tiles endpoint, sessions, cache | 49 | Server-mode guard on raw stream; rendered slices; tile batch and guards; server-session tracking; tile cache keys. |
| `test_volume_strided_dims.py` | `volume_service` strided data | 8 | H13: strided dimension equals numpy slicing; Content-Length and NPY header match the body; odd dimensions. |

### 2.2 New JS modules (9 test files, 156 `test(` calls; 2 helpers)

| File | Module under test | # | Behaviours covered |
|---|---|---|---|
| `paxs-mpr-client-mode.test.js` | `paxs-mpr.js` client-mode probe | 32 | Capability score (WebGL2 = 30 points); probe context released; repeated probes do not accumulate contexts. |
| `paxs-mpr-disposal.test.js` | `paxs-mpr.js` dispose | 6 | Every tracked geometry, material and texture freed; scene and camera cleared; listeners removed. |
| `paxs-mpr-geometry.test.js` | `app/static/viewer/paxs-mpr-geometry.js` | 19 | C09 plane axes, dimensions and voxel/index math on a unique-encoding phantom. |
| `paxs-mpr-plane-extraction.test.js` | `paxs-mpr.js` extractors | 3 | Axial, coronal and sagittal extraction order against the phantom. |
| `paxs-mpr-server-mode.test.js` | `paxs-mpr.js` server requests | 27 | Server cache key includes series, plane, slice and window; PNG request parameters. |
| `paxs-mpr-wiring.test.js` | `paxs-mpr.js` source text | 16 | C09 wiring by string presence: uses the geometry module, no duplicated math. Weak (see 1.5). |
| `paxs-mpr-worker-lifecycle.test.js` | decode worker pool | 9 | Worker error and timeout reject pending requests; shutdown rejects in-flight with `CancelledError`. Timeout injected as 60 ms (`:24`). |
| `paxs-study-list-refresh.test.js` | `paxs-study-list-enhance.js` | 6 | H14: concurrent 401s share one refresh; retry at most once; credentials `same-origin`. |
| `test_viewer_fallback.test.js` | `paxs-mpr.js` load-ratio and fallback | 38 | Parse load ratios; fold headers into state; fallback chain transitions. |

Helpers: `tests/js/extract.js`, `tests/js/phantom.js`.

### 2.3 Quality scan (check 5)

Static scan of all 1,085 `def test_` functions in `tests/*.py` (the `quality_scan.py` output in `local/`).

- **Sleeps.** 55 sleep call sites in test files. Only 3 are new: `test_prefetch_and_routing.py:274` (`time.sleep(1.0)` inside a worker thread), `:286` (asyncio loop probe, 10 ms), `:300` (polling loop, 5 ms steps). The rest are pre-existing (`test_mpps.py` 12, `test_orm.py` 7, `test_storage_commit.py` 7, `test_hl7.py` 5, `test_nodes.py` 4, `test_worklist.py` 4, and others).
- **Wall-clock thresholds (new).** `test_prefetch_and_routing.py:314` asserts `/health/live` under 50 ms during a C-ECHO, and `:317` asserts loop lag under 250 ms. Both are timing-dependent and will flake on a loaded CI runner. Consider a mocked clock or a larger budget with a clear reason.
- **Skips.** New: `test_no_fabricated_clinical_content.py:89,137` (skip if the built UI is missing; present in this checkout, so they run), `test_storage_containment.py:202` (skip if no symlink or junction can be created; the Windows junction fallback at `:184` usually succeeds). No `xfail` anywhere in `tests/`. The memory note `.workbuddy-ai/memory/2026-09-22.md:577` says "13 passed, 1 xfailed" for `test_patient_identity`, but the module now has 14 tests and no xfail. Documentation drift.
- **Weak assertions (new).**
  - `test_route_auth_matrix.py:254` and `:319-320`: authenticated check is `status not in (401, 403)`. A 404, 422 or 500 passes.
  - `test_route_auth_matrix.py:550-559` (`test_public_mode_ignores_a_stale_share_cookie`) asserts only `isinstance(list)`. The name promises "no change in what the caller sees", which it never checks.
  - `test_volume_client_mode.py:219` and `:382`: named `..._still_404s_in_client_mode`, but assert `in (403, 404)`. A 403 regression passes.
  - `test_report_integrity.py:130`: `"radiologist" in text or "admin" in text`.
  - `test_schema_authority.py:148` and `test_route_auth_matrix.py:661-671` (and `test_registration_guard.py:54-61`): assert on source text (`inspect.getsource`), not behaviour.
- **Tautology.** None new. The old tautology is still present (section 4).
- **Env writes.** `test_prefetch_and_routing.py` none. New tests use `monkeypatch` throughout. The only direct `os.environ` writes are in `conftest.py:35-60` (process-wide, intentional) and old `test_licensing.py:126,214` (section 4).
- **Network.** New modules make no real outbound calls. `http://test` and `http://www.w3.org/2000/svg` are fixture strings.
- **Wall clock (new).** `test_quota_and_atomic_ingest.py:175` uses `time.time()` with `os.utime`, which is deterministic.

### 2.4 Existing test modules changed (43 files in the diff, including `conftest.py` and `helpers.py`)

Grouped by nature of change:

**A. Mechanical migration to the new auth model (about 35 files).** The `auth_client`, `auth_for_role` or `create_user` helpers replaced ad-hoc self-registration and anonymous calls. Examples: `test_cfind.py:384`, `test_cget.py:378-411`, `test_config.py:13-30`, `test_cstore.py:353-396`, `test_database.py:25-30`, `test_fusion.py:62-82,175-212`, `test_nodes.py:28-30`, `test_priors.py:44-46`, `test_users.py:17-22`, `test_vna.py:123-149`, `test_worklist.py:333`, `test_sessions.py:22,201`, `test_sessions_admin.py:22,136`. Anonymous calls that now need a bearer token: `test_dicomweb.py:58,611`, `test_fhir.py:93`, `test_gsps.py:344`, `test_hl7.py:187`, `test_mip.py:180`, `test_monitoring.py:92`, `test_multiframe_volume.py:437`, `test_perf_serving.py:13,290`, `test_scalability.py:12,240`, `test_viewer.py:17,230`, `test_volume.py:282`, `test_volume_mpr_slice.py:179`, `test_wado_bulk.py:5,102`, `test_warm_service.py:11,234`, `test_dashboard_services.py:10,95`, `test_lifecycle.py:292`, `test_scripting.py:219`, `test_replication.py:175,245`, `test_multi_storage.py:84`, `test_ratelimit.py:68`.

**B. Behaviour and expectation changes.** I checked each against the diff.

- `test_redis_panel.py:66-84`: anonymous `GET /api/v1/redis/status` was 200 with `admin=False`. It is now 401, and an admin sees `admin=True`. Matches C03.
- `test_database.py:238-272`: "drifted schema is stamped to head" became "refused with HTTP 500, revision unchanged". The refusal is a `RuntimeError` (`app/services/db_admin.py:336-343`), so the endpoint returns 500. 🟡 See section 4 (`:262`): the test enshrines a server error as the refusal status. It should be 409 or 422.
- `test_storage.py:37-77` and `:120-139`: absolute and CWD-relative legacy keys now refused by default (`STORAGE_ALLOW_LEGACY_PATHS`); untrusted UIDs rejected instead of rewritten.
- `test_auth_defenses.py:130-155`: HSTS asserted only when `HTTPS_ENABLED` (H06). Matches the code.
- `test_ldap.py:95-104,189-200,306-313`: fixture group map is `CN=Radiologists=ADMIN;CN=Doctors=PHYSICIAN`. The old assertion `role == "radiologist"` (lowercase) became `ADMIN`. Because the fixture maps "Radiologists" to ADMIN, the RADIOLOGIST mapping is exercised only by the regression test `test_role_is_never_lowercase_from_the_directory`. Disabled LDAP now returns 503 where it returned 401 before.
- `test_compression.py:147-157`: the HTJ2K worker test selects its own job by instance ID instead of "any PENDING job". This removes an order dependence.
- `test_config.py:140` and the `GROUP_KEYS` set (in `test_config.py`): adds the `viewer` group.
- `test_share.py:97`: physician can read the study (200) before asserting delete is refused.
- `test_vna.py:348-371`: anonymous `GET /api/v1/studies` now asserted 401.
- `test_lossy_compression.py` (11 hunks): expectations updated for C-2/3/4.

**C. Support.** `conftest.py` (+146 lines, section 1). `helpers.py` (+87 lines, section 1.4).

---

## 3. Auth matrix coverage analysis (`test_route_auth_matrix.py`)

### 3.1 How it enumerates routes

- Structural layer (`:122-160` `collect_routes`, `:163-181`). An AST walk over `app/api/v1/*.py` only (`API_DIR`, `:125`), matching `router.<get|post|put|patch|delete>` decorators. It does not see `@app.*` routes in `app/main.py`, the FastAPI built-ins, or mounts.
- Behavioural layer (`:228-410`). About 30 hand-picked URLs.
- Replay on HEAD (`(local verification file)`):
  - 278 decorators in `app/api/v1`.
  - 267 pass an `AUTH_DEPS` name.
  - 10 are PUBLIC-ALLOW. These match `PUBLIC_ROUTES` (`:43-59`) exactly, and no entry is stale (`test_public_allowlist_has_no_stale_entries` passes).
  - 1 is UNPROTECTED: `app/api/v1/metrics.py:182` `GET /metrics`, guarded only by `require_metrics_access` (`metrics.py:37-62`). That name is not in `AUTH_DEPS` (`:32-40`), and `/metrics` is not on the allowlist.

### 3.2 🔴 The structural test fails on HEAD (static prediction)

`test_every_route_is_public_by_allowlist_or_authenticated` (`:163-174`) will report `metrics.py:182 GET /metrics`. The design is deliberate. `require_metrics_access` lets trusted-network scrapers in without credentials (loopback and RFC 1918 by default), and every other caller must authenticate. The test simply does not know about it.

Fix options:
- (preferred) Add `("metrics", "GET", "/metrics")` to `PUBLIC_ROUTES` with a comment stating the trusted-network exemption, and assert the IP-allowlist behaviour in a separate test.
- Or add `require_metrics_access` to `AUTH_DEPS`. That would hide the exemption.

### 3.3 Anonymous and authenticated assertions

| Assertion | Where | Verdict |
|---|---|---|
| Anonymous rejected with 401 or 403 | `:249-252` (PHI loop, 13 URLs) | Loose. Acceptable. |
| Anonymous rejected with exactly 401 | `:316-317`, `:332` (DICOMweb private, STOW) | Good. |
| Authenticated accepted | `:253-256`, `:318-321` | 🟡 `not in (401, 403)`. A 404, 422 or 500 passes. Change to `== 200` or the expected code. |
| Wrong role gets 403 | none in this file | Covered elsewhere: `test_registration_guard.py:118` (PHYSICIAN delete = 403), `test_share.py:97`, `test_redis_panel.py:103`, `test_p1_hardening.py` (`ordinary_user_is_denied_privileged_mutations`). |
| Required mutation guards present | `REQUIRED_MUTATION_GUARDS` `:696-717` | Replayed: all 17 keys exist and carry the expected `Depends(...)`. |

### 3.4 Which routes are NOT covered

- Structural layer covers all 278 `api/v1` decorators.
- Behavioural layer covers about 30 URLs out of 297 known routes (278 API plus 19 page routes).
- Not scanned at all (19 `@app.get` routes, `app/main.py:691-938`): `/`, `/viewer`, `/viewer/`, `/viewer/index.html`, `/viewer/viewer`, `/viewer/app-config.js`, `/PaxsViewer-logo.png`, `/admin`, `/admin/`, `/studies-ui`, `/studies-ui/`, `/settings`, `/settings/`, `/login`, `/login/`, `/logout`, `/terms`, `/paxs-viewer-guide`, plus the `/viewer` SPA mount. These are UI pages, not PHI APIs. `/admin` and `/settings` are admin pages, and nothing asserts they are gated.
- FastAPI built-ins `/api/docs` and `/api/openapi.json`. `docs/API_REFERENCE.md:46` lists them as Public.

### 3.5 Route list versus `docs/API_REFERENCE.md` (old)

Replay (`(local verification file)`): 288 documented rows against 297 code routes.
- 286 documented rows match code. Two are doc-only: `/api/docs` and `/api/openapi.json` (FastAPI-generated).
- 7 code routes are undocumented:
  - `app/main.py:842` `/admin`, `:859` `/studies-ui`, `:870` `/settings`, `:793` `/viewer/app-config.js`.
  - `app/api/v1/volume.py:461` `GET /api/v1/volume/{series}/negotiate`, `:926` `GET /api/v1/volume/{series}/tiles`.
  - `app/api/v1/reports.py:275` `POST /api/v1/reports/{study}/addendum`.
- The three `api/v1` routes are protected in the structural replay, so this is a documentation gap, not an exposure.

### 3.6 Public DICOMweb mode: an untested write path (open risk)

- `app/api/v1/deps.py:282-283`: in public mode `require_dicomweb_write` returns `get_optional_user`, so an anonymous caller can STOW-RS.
- `conftest.py:60` sets `DICOMWEB_PUBLIC=true` for the whole suite. Most DICOMweb tests therefore run in a mode where anonymous writes are allowed.
- `test_route_auth_matrix.py:288-294` asserts only public reads. No test pins or rejects anonymous STOW in public mode.
- The H03 test accepts `Depends(get_optional_user` as an auth marker (`:728`). `dicomweb.py:941` (`POST /studies` and `POST /studies/{study_uid}`) matches that marker. Their protection comes from the router-level guard, which this test does not check.

### 3.7 Tenant scan (C04, `:566-658`): a narrow detector

- `touches_data` (`:627-629`) triggers only on the text `.study_query(`, `.series_query(`, `.instance_query(` or `select(Study)`. Any route that resolves study data another way is silently skipped.
- Markers that satisfy the check (`:576-586`) include `tenant_id` and `include_all=`. Any route passing `include_all=True` also satisfies the check, because it is a substring match.
- Replay (`(local verification file)`): 27 routes take a study, series or instance identifier but never reach the detector.
  - Authenticated-only reads, no scope marker, and no tenant argument passed to the service: `app/api/v1/kin.py:82` `GET /api/v1/kin/study/{study_uid}`, `kin.py:93` `GET /instance/{sop_uid}/notes`, `kin.py:104` `GET /{sop_uid}`, `dose.py:52` `GET /study/{study_uid}`, `dose.py:84` `GET /report/{sop_uid}`, `critical_results.py:135` `GET /study/{study_uid}`. Each depends only on `get_current_user`.
  - Admin or destructive-role mutations, lower risk: `lifecycle.py:163,174,185,196`, `routing.py:161`, `replication.py:48`, `storage.py:268`, `worklist_tasks.py:70,82,98,109`.
  - DICOMweb study reads, `dicomweb.py:445-869` (10 routes). They are guarded at the router level (`dicomweb.py:64`), not by a marker, so the test cannot see them.
  - Whether these breach C04 depends on whether KIN, RDSR and critical-result data carry tenant scope. Not verified here.
- `test_query_builders_default_to_tenant_scoped` (`:648-658`) checks only the three function defaults, not that callers pass `include_all=False`.

### 3.8 H03 mutation guard (`:681-744`)

- No mutating route in `app/api/v1` is missing an auth marker (replay: zero flagged). Good.
- 🟡 `MUTATION_AUTH_EXEMPT` (`:685-692`) contains `("share", "POST", "")` at `:691`. The comment says "guarded by require_destructive_role today", but `app/api/v1/share.py:33-36` uses `Depends(get_current_user)`, so any logged-in role can create a share link. The comment is wrong and the exemption is unnecessary. Remove it, and decide whether share creation (which exposes PHI) should require a clinical role (see section 7).
- 🟡 `AUTH_DEPENDENCY_MARKERS` `:719-729` includes `Depends(get_optional_user` (`:728`). That marker does not enforce auth. Remove it, or require the router-level guard explicitly.
- 🟢 `ROUTER_GUARDED_MODULES = {"dicomweb"}` (`:62`) is defined and never used.
- 🟢 Assertion message typos: `"...dependency:/n  "` at `test_route_auth_matrix.py:742` and `"...violations:/n  "` at `:761`. The literal `/n` should be `\n`.

### 3.9 Shared cookie jar

`anonymous_client` shares a cookie jar with `auth_client` (see 1.4). Today no test logs in through `client` and then expects anonymous rejection, so the matrix's current results are not affected. It is still a trap.

---

## 4. Prior findings status

| Prior item | Source | Status now | Evidence |
|---|---|---|---|
| F-11 part A: suite cannot import (storage package missing) | `CODE_REVIEW.md` B-01, F-11 | 🔴 **STILL OPEN** | Section 1.1. |
| F-11 part B: fixtures wipe whatever `DATABASE_URL` points at | `CODE_REVIEW.md` F-11 | **FIXED** | `conftest.py:19-37`, `_db_guard.py`. Sentinel weakness remains (1.2). |
| Sleeps in tests | `10-tests.md` | **STILL PRESENT.** 52 pre-existing call sites, plus 3 new (`test_prefetch_and_routing.py:274,286,300`). | Section 2.3. |
| Tautology | `10-tests.md` MAJOR-11, `test_gsps.py:296` | 🟡 **STILL PRESENT, now at `test_gsps.py:298`**: `assert result.size == (64, 32) or result.size[0] != img.size[0]`. The first disjunct accepts an unrotated image. | Read at `test_gsps.py:292-298`. |
| Non-concurrent concurrency test | `10-tests.md` (`test_scalability.py`) | 🟡 **STILL PRESENT.** `test_worker_claims_atomically` (`test_scalability.py:117-149`) says "claiming concurrently" but calls `_claim_jobs` sequentially. `invalidate_usage_cache()` (`:146-149`) is not in a `finally`. | Read in full. |
| Env leak | `10-tests.md` MAJOR-4, `test_licensing.py:126` | 🟡 **STILL PRESENT.** `os.environ["PAXS_LICENSE_PUBLIC_KEY"]` set at `:126` and `:214`; popped only at `:249`. `test_licensing.py` is unchanged in this commit. | Read at `:120-130`. |
| Role-named test that checks only anonymous | `10-tests.md` MAJOR-3/13, `test_db_perf.py:20` | 🟡 **STILL PRESENT.** `test_perf_panel_requires_destructive_role` (`test_db_perf.py:20-21`) asserts only anonymous `401/403`. `test_db_perf.py` is unchanged. | Read at `:17-24`. |
| Environment-dependent Redis assertions | `10-tests.md` MAJOR-8 | 🟡 **STILL PRESENT.** `next(k for k in vol_keys if ...)` (`test_redis_panel.py:169`, `:200`, `:225`) raises `StopIteration` rather than a clear failure. | |
| Weak truthiness assertions | `10-tests.md` section 4 | 🟡 New weak ones listed in 2.3. | |

---

## 5. Repository hygiene findings

Secrets-scanner question (check 3): `tests/test_no_embedded_secrets.py` scans `app, deploy, scripts, tests, migrations, feature-plans` plus root-level files (`:60`, `:79-85`). It does not scan `.workbuddy-ai`, `.vscode`, `outputs`, `docs`, `study-list-ui`, `audit_and_roadmap`, `logo` (`_SKIP_DIRS` `:41-46`). It skips files by suffix including `.pem`, `.key`, `.crt`, `.png` (`:49-54`). It checks only PEM headers. It does not check git history. It would flag nothing currently in `.workbuddy-ai` or `.vscode`: those are excluded, and a pattern scan of them found no PEM headers. The one PEM literal in the working tree is in `docs/reference/01-core-config-db.md` and `09-deploy-scripts-licensing.md`, which is excluded and contains no key body.

| Sev | Path (file:line) | What it is | Why it matters | Fix |
|---|---|---|---|---|
| 🔴 H-1 | `app/services/storage/` (missing); `.gitignore:9` | The storage backend package is absent and was never committed. The `storage/` rule is unanchored and matches it. | The app and the whole test suite fail to import from a clean checkout. | Restore from the author's machine. Change `.gitignore:9` to `/storage/`. `git add app/services/storage/`. |
| 🔴 H-2 | `deploy/vendor-portal-php/config.php` in commits `8af1381` (added) and `e90d32c` (1 PEM header line); removed in `5a32aa4`. Remote: `origin` = `github.com/msimoo/Paxs-Archive`. | The vendor RSA private key was committed and remains in history. The working tree is clean (HEAD `config.php` has 0 PEM headers and no `PAXS_VENDOR_PRIVATE_KEY` define). The scanner checks only the working tree. | Anyone with read access to the history, or the remote if it is public, has the signing key. Licence signatures can be forged. | Treat the vendor key as compromised: rotate it (this changes the licence trust chain, so plan it). Rewrite history (for example `git filter-repo`) and force-push if the remote is public. Add a history check (gitleaks, or `git log -G` in CI). Never paste the key into any file. |
| 🟡 H-3 | `.workbuddy-ai/` (56 tracked files; not ignored, `.gitignore` has no rule) | Agent workspace: 14 logs, 10 JUnit XML reports, 9 memory notes, 19 `tmp/` scripts, 3 backup source copies, 1 collected list. | Leaks: machine hostname `hostname="DESKTOP-2P4RQ80"` on line 1 of 10 XML files; Windows profile paths with username at `.workbuddy-ai/h01.log:105,124,137` and `memory/MEMORY.md:13-14`; `/Users/<user>` at `final.xml:4,7` and `h11.xml:4,7`. Stale duplicates `backups-phase2/app/api/v1/volume.py` (660 lines), `app/services/volume_service.py` (1,215 lines), `app/static/viewer/paxs-mpr.js` (2,487 lines) confuse search and graph tools. `tmp/` holds the project's JS and Python test suites outside `tests/`, which neither pytest nor `node --test tests/js/` runs. | Secrets: none found. Credential-keyword hits are test names and log lines (for example `phase1_run.log:228` `KeyError: 'access_token'`, `collected3.txt:40` test name), plus JS fixture strings (`tmp/auth-guard-test.js:86`). No JWT, AWS, GitHub, Slack or OpenAI-format token; no PEM header. PHI: no DICOM records and no patient identifier values. One caret-format name literal at `tmp/memory-observability-test.py:388` looks like a placeholder (confirm). Memory notes discuss PatientID, MRN and DOB fields without values. Private IPs are test fixtures (`10.0.0.9`, `10.9.9.9`). | `git rm -r --cached .workbuddy-ai` and add it to `.gitignore`. Move `tmp/` suites into `tests/js/` or `tests/`, and document them. Delete `backups-phase2/` (the code is in history). |
| 🟡 H-4 | `logo/index` (30,765 bytes, added) | A binary Git index (magic `DIRC`, version 2, 315 entries) from an unrelated restaurant-POS project. Its entries name `RestaurantSystem.spec`, `routes_orders.py`, `routes_employees.py`, `dashboard-shell/...`, `dev_tools/screens/raw/20_employees.png`, `Start_system.bat`, and so on. | Foreign project contamination. Leaks another codebase's file tree, and suggests a misdirected `.git/index` copy. | `git rm logo/index`. Confirm none of those files are needed here. |
| 🟡 H-5 | `.vscode/mcp.json` (added, 18 lines) | Registers third-party MCP server `report-intelligence` as `cmd.exe /d /c npx.cmd --yes @report-intelligence/mcp-server@0.1.2` (lines 5-11). Hard-codes a Windows drive path in `env.RIP_ALLOWED_REPORT_ROOTS` (line 14). `.vscode/` is not ignored. | Starting the workspace downloads and runs an npm package whose publisher is not reviewed in the repo. The version is pinned, but there is no integrity check. The absolute path is machine-specific. | Remove from the repo, or keep it in user settings. If kept, review the publisher, use `${workspaceFolder}` and drop the absolute path. Add `.vscode/` to `.gitignore` if it is per-user. |
| 🟡 H-6 | `app/static/uploads/logo_*.png` (17 new; 18 tracked in total) | 16 are byte-identical 77-byte PNGs (one SHA-256), written by `test_p1_hardening.py:231-245` through the real upload endpoint (`app/api/v1/settings.py:73`). `logo_858c5e60…png` (28,801 bytes) is byte-identical to pre-existing `logo_logomain.png`. | `UPLOADS_DIR` (`settings.py:30-31`) is a module constant that tests never redirect. Every run writes into the source tree, and nothing ignores the output. | Monkeypatch `app.api.v1.settings.UPLOADS_DIR` to `tmp_path` in `conftest.py`. `git rm` the 16 test artifacts and the duplicate. Ignore `app/static/uploads/logo_*.png` except the branding file. |
| 🟢 H-7 | `outputs/plan_9_2026.md` and `plan_9_2026.md` | Byte-identical copies (49,806 bytes; SHA-256 prefix `c1613f61df930dfd`). | Two copies drift. `outputs/` is excluded from the secret scan (`test_no_embedded_secrets.py:44`), so a key placed there would not be caught. | Keep one canonical copy. Generate or ignore `outputs/`. |
| 🟡 H-8 | `.gitignore` | Good additions: `deploy/vendor-portal-php/config.local.php` and `*.pem` (`:33-34`). Missing: `.workbuddy-ai/`, `app/static/uploads/logo_*.png`, `logo/index`. Unanchored rules: `storage/` (`:9`), `nul` (`:48`), `query` (`:49`), which hide any directory of those names anywhere in the tree. | Missing rules let H-3, H-4 and H-6 happen. Unanchored rules can hide real source. | Add the missing rules. Anchor `storage/` (`/storage/`), `query` and `nul`. |
| 🟡 H-9 | `docs/` (untracked: `?? docs/` in `git status`; 0 tracked files in both commits) | The review baseline (`docs/CODE_REVIEW.md`, `docs/reference/10-tests.md`, `docs/API_REFERENCE.md`) is not version controlled. | This review cannot be reproduced from the repo, and a clone has no docs. | Commit `docs/` or move it to a tracked path. |
| 🟢 H-10 | `tests/test_no_embedded_secrets.py:147-188`, `:51` | The gitignore test checks ignore rules with `fnmatch`, not `git ls-files`. Suffixes `.pem`, `.key`, `.crt` are skipped (`:51`). | A force-added `.pem` in `deploy/` would pass all seven tests. | Add a `git ls-files` check for key suffixes. Scan the whole tracked set (`git ls-files -z`) rather than fixed directories. |
| 🟢 H-11 | `app/static/studies-ui/assets/` | Several committed `index-*.css` bundles (for example `index-B97LyhE3.css`, `index-BEOPGbbi.css`, `index-BNd7lPFx.css`, `index-BXyClNyH.css`). | Generated output accumulates. I did not verify which are stale. | Keep only the bundles `index.html` references. Clean the output directory on build. |
| ✅ | `deploy/vendor-portal-php/config.php`, `crypto.php` (HEAD) | Current HEAD has no PEM header and `crypto.php` contains `paxs_vendor_private_key()` (fail-closed). `config.local.php.example` is tracked as intended. | Positive. The C01 working-tree fix is sound. | None. |

---

## 6. Documentation deltas

**How to run the tests now**
- Python: `pytest tests` from the repo root. No `pytest.ini` or `pyproject.toml` exists, so there is no `testpaths` and no marker registration. Markers in use: `anyio` (29), `parametrize` (36), `skipif` (5). Dependencies to add to the dev notes: `anyio` (provides the marker), `httpx` (`ASGITransport` in `test_prefetch_and_routing.py`), `Pillow` (`test_p1_hardening.py`), `pydicom`, `numpy`.
- Database: default is an isolated SQLite file under the temp dir (`conftest.py:26`, `_db_guard.py:109-116`). Set `DATABASE_URL` only to a database whose name contains `test`. A non-SQLite URL also needs `PAXS_TEST_ALLOW_EXTERNAL_DB=1` (`_db_guard.py:27`). This is new and must be documented.
- JavaScript: `node --test tests/js/` (`RUN_AND_BUILD_GUIDE.md:387`), or per file (`:390-394`).
- Fixtures to document: `client` (anonymous), `auth_client` (ADMIN bearer), `radiologist_client`, `auth_headers`, `anonymous_client` (alias that shares cookies). Helpers: `auth_for_role`, `create_user`, `admin_headers`.
- Environment the suite forces (`conftest.py:35-60`): `DATABASE_URL`, `STORAGE_ROOT`, `SECRET_KEY`, `DICOM_RECEIVER_ENABLED=false`, `WORKER_ENABLED=false`, `THUMBNAIL_ENABLED=true`, `PUBLIC_BASE_URL`, `RATE_LIMIT_ENABLED=false`, `HL7_ENABLED=false`, `AUTH_GATE_ENABLED=false`, `ALLOW_REGISTER=true`, `DICOMWEB_PUBLIC=true`.

**Docs that are now wrong or stale**
- `docs/reference/10-tests.md`: says no markers are registered and no xfail exists (both still true), but lists the old file set and has no entries for the 24 new modules or the 9 JS suites. Needs a rewrite for the new modules.
- `docs/API_REFERENCE.md`: missing 7 routes (section 3.5) and lists two FastAPI built-ins without a note.
- `plan_viewer_process.md:821-823` and `:882`: names JS tests that do not exist (`test_viewer_negotiate.js`, `test_viewer_modes.js`). Actual files are the ten `tests/js` modules above.
- `.workbuddy-ai/memory/2026-09-22.md:577`: "13 passed, 1 xfailed" for `test_patient_identity`. The module now has 14 tests and no xfail.
- `.workbuddy-ai/memory/MEMORY.md` "Test suites" section documents suites that live outside `tests/` (see H-3).
- The old `conftest.py` comment suggesting `DATABASE_URL` be pointed at MySQL was removed in this commit. Any doc that still says so (check `RUN_AND_BUILD_GUIDE.md` and `10-tests.md`) must add the sentinel and `PAXS_TEST_ALLOW_EXTERNAL_DB=1` requirements.

---

## 7. Open questions

1. **Vendor key.** Is `origin` public? If so the key is public, and rotation plus a history rewrite is urgent. Who holds the signing key today, and can the licence chain be rotated without breaking installed copies?
2. **Storage package.** Can the author restore `app/services/storage/` from the machine that produced the 2026-09-23 runs? If not, is a rewrite of the backend interface planned?
3. **Public DICOMweb mode.** Is anonymous STOW-RS intended when `DICOMWEB_PUBLIC=true` (`deps.py:282-283`)? No test pins either way, and the whole suite runs in that mode.
4. **Share creation.** Should `POST /api/v1/share` (`share.py:33-36`, `get_current_user`) require a clinical or destructive role? The H03 exemption (`test_route_auth_matrix.py:691`) hides it, and its comment is wrong.
5. **Tenant scope.** Are KIN, RDSR and critical-result study reads (`kin.py:82-104`, `dose.py:52-92`, `critical_results.py:135`) tenant-scoped under C04? The test cannot see them.
6. **Public API docs.** Is anonymous access to `/api/docs` and `/api/openapi.json` (listed Public in `API_REFERENCE.md:46`) intended? It discloses the full route list.
7. **Node.** Does `node --test tests/js/` discover the files on Node 24? If not, switch to a glob.
8. **Confirm after F-11 is fixed (one real run):** expect failures in `test_every_route_is_public_by_allowlist_or_authenticated` (metrics), the positive control in `test_test_db_guard.py:169`, `test_storage_containment.py` (imports), and possibly `test_login_gate.py:208` (passes via the bootstrap admin's cookie, not `dr.i`). Also check `test_database.py` (`:262` expects 500).
9. **Test uploads.** Should the logo upload tests write into `app/static/uploads` at all? The current behaviour is an accident of the module constant.
