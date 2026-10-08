# PAXS Archive — Code Review

> **Current review of commit `5a32aa4`** (pulled 2026-10-08). It re-reviews the codebase against the baseline
> `e90d32c`, whose review is archived at [history/CODE_REVIEW_e90d32c.md](history/CODE_REVIEW_e90d32c.md).
> Static analysis only: the tree still cannot be imported (B-01), and no Python dependencies are installed in the
> review environment. Seven area re-reviews are in [reference/5a32aa4/](reference/5a32aa4/), and the lead reviewer
> re-checked every blocker in the source. Baseline IDs (B-, S-, D-, F-) are kept. Findings new in `5a32aa4` are
> numbered **NEW-xx**.

| Tag | Meaning |
|---|---|
| ✅ | Re-verified by the lead reviewer in the source at `5a32aa4` |
| 🔎 | Verified by an area re-review (cited in [reference/5a32aa4/](reference/5a32aa4/)) |
| ❓ | Depends on runtime, database or library behaviour; needs a test run |

**Severity:** 🔴 blocking · 🟡 important · 🟢 nit / hygiene. **Status:** FIXED · PARTIAL · OPEN · NEW.

---

## 0. Summary and decision

**Decision: 🔄 Request changes.** `5a32aa4` (295 files, +47k / −3.4k lines, about 13k of them tests) is a large and
mostly well-targeted remediation, but the tree still does not build from git and is not yet safe for patient data.

**What improved:**
- Routes with no auth went from 39 to 6. Routes that let anonymous callers through went from 23 to 0
  ([API_REFERENCE.md → Changes since e90d32c](API_REFERENCE.md#changes-since-e90d32c)).
- `DICOMWEB_PUBLIC` is now wired and defaults to `false`. Share tokens authorize reads of one study.
- Tenant guards cover DICOMweb, volume, FHIR, export and priors.
- Reports go through the service: login required, role gate, ORU delivery, immutable FINALIZED reports and addenda.
- The LDAP backdoor and the vendor private key are gone from the tree. Uploads are validated, and path-dangerous
  UIDs are rejected.
- Ingest is staged and enforces quotas. Lossy compression is safe and off by default. There is a production
  configuration gate. Five tables gained their missing migrations, and the React actions call the real APIs.

**What still blocks:**
- The missing storage package and `redis` dependency.
- A license key that leaked and was never rotated.
- A secret-key guard that is inactive by default.
- Two new clinical-safety defects: finalize signs a stale draft, and the GPU MPR view shows the wrong pixels.
- A patient-identity migration that breaks MRN uniqueness and probably fails on MySQL.
- Baseline blockers left untouched because their files did not change.

| Baseline 🔴 blockers (24) | Count | IDs |
|---|---|---|
| FIXED | 7 | S-03, S-04, S-06, D-02, D-03, D-06, D-08 |
| PARTIAL | 10 | S-01, S-02, S-05, S-07, S-08, S-09, S-10, S-12, D-01, D-04 |
| OPEN | 7 | B-01, B-02, B-03, S-11, D-05, D-07, D-09 |
| **NEW 🔴** | **6** | NEW-01 … NEW-06 |

**Stop-ship:** B-01, B-02, S-02, S-01, NEW-01, NEW-02, NEW-03, NEW-04, S-05, S-10, the S-08 residuals, S-09.

---

## 1. Status of every baseline finding

### 1.1 Build and deploy
| ID | Status | Evidence at `5a32aa4` |
|---|---|---|
| B-01 storage package missing | **OPEN** ✅ | `app/services/storage/` is still absent, and `.gitignore:9 storage/` still ignores it. The author's committed JUnit logs (`.workbuddy-ai/`) show tests passing on a machine that has the package |
| B-02 `redis` not in requirements | **OPEN** ✅ | `requirements.txt:34 # redis>=5.0`, but `redis_service.py:26 import redis`. README now (wrongly) calls Redis optional |
| B-03 standalone build needs `paxs.spec`, ships `.env` | **OPEN** 🔎 | Unchanged |

### 1.2 Security
| ID | Status | Evidence and residual risk |
|---|---|---|
| S-01 default `SECRET_KEY` | **PARTIAL** ✅ | `validate_production_settings` (`config.py:597-668`, `main.py:490`) refuses insecure keys and cookies, **but only in production**. `ENVIRONMENT` defaults to `development` (`config.py:191`). The Dockerfile and LB compose set neither `ENVIRONMENT` nor `SECRET_KEY`, and `.env.example:105,111` ships dev mode plus a placeholder key. Shipped deployments still accept forged ADMIN tokens, and ADMIN can upload in-process Python scripts |
| S-02 vendor private key committed | **PARTIAL** ✅ | Removed from `config.php` (now loaded from env or `config.local.php`). **Not rotated:** the key is still in pushed history, and `crypto.py`'s embedded public key still matches it (checked), so licenses can still be forged. `PORTAL_SECRET_KEY` keeps a literal fallback (`config.php:84`) |
| S-03 anonymous writes | **FIXED** ✅ | `/reports/save` requires a login, ignores the client-sent signer, enforces the role gate and runs `finalize_report` (`reports.py:193-267`). Org settings and templates are ADMIN. The logo is Pillow-validated PNG/JPEG/WEBP ≤ 2 MiB. STOW requires a login unless public mode is on. Residuals: NEW-01, NEW-05, NEW-20 |
| S-04 anonymous PHI reads | **FIXED** ✅ | Router guard `require_dicomweb_access` (`deps.py:329-344`), `DICOMWEB_PUBLIC=false`. Volume uses `require_series_read`. FHIR, GSPS, study detail and Redis need a login. 6 deliberate public routes remain |
| S-05 Redis password in `/redis/status` | **PARTIAL** ✅ | No longer anonymous (`redis_panel.py:64`), but **any role** still gets `"saved"` with the plain-text password (`redis_service.py:668, 748`). `/keys` and `/key` are any-role |
| S-06 LDAP backdoor | **FIXED** ✅ | Mock removed (`ldap_service.py:14-18, 38`), real bind in place. Residuals: NEW-08, NEW-10 |
| S-07 registration and bootstrap | **PARTIAL** ✅ | `ALLOW_REGISTER=false`; self-registration can only grant PHYSICIAN (`auth.py:199-217`). **Open:** the first caller on an empty DB becomes ADMIN (`auth.py:193-194`), and `db/init` seeds `admin123` by default (`database.py:59-61`) |
| S-08 destructive ops for any user | **PARTIAL** ✅ | Fixed: lifecycle, routing, lossy, split/merge, node writes. **Still any user:** `migration/folder` with `delete_after_import` on any server path (`migration.py:31-71`), `migration/cmove`, worklist-tasks assign/STAT, peer review, critical-result ack, PIX `POST /identities`, `POST /share`, `POST /reports` and `PATCH /reports/{id}` (signer taken from the body) |
| S-09 XSS | **PARTIAL** ✅ | Print report is escaped (`reports.py:314-341`). The dashboard still puts `esc()` inside inline `onclick` strings (`app.js:342, 399-401, 1467, 1490`) |
| S-10 server imports tooling scripts | **PARTIAL** ✅ | `/stats` now requires a login, but it still runs `exec_module` on every `scripts/*.py` (`dashboard.py:108-112, 406`). `SCRIPTS_DIR="scripts"` is unchanged, so viewing the dashboard still runs `sync_agents.py`, which deletes files |
| S-11 DIMSE accepts any AE, broad retrieve | **OPEN** 🔎 | No calling-AE allow-list (`receiver.py:631`). SERIES/INSTANCE C-FIND ignore the parent UIDs |
| S-12 tenant isolation | **PARTIAL** 🔎 | Fixed for DICOMweb, volume, FHIR, export, priors and share creation. Open for reports, the worklist, KIN/dose/critical reads, split/merge, GSPS (fails open) and `zorder` frames. See NEW-05, NEW-09, NEW-22 |

### 1.3 Data integrity and clinical safety
| ID | Status | Evidence and residual risk |
|---|---|---|
| D-01 retransmits delete archived objects | **PARTIAL** 🔎 | Staged → index → promote, and failures discard only the staged file (`receiver.py:85-166`, `dicomweb.py:1019-1074`). **Residual:** check-then-insert without savepoints (`indexing_service.py:295-424`), the S3 path writes before commit, and a promote failure after commit leaves a dangling row |
| D-02 `..` UID | **FIXED** ✅ | `validate_component`/`unsafe_reason` hard-reject it (`storage_service.py:57-130`) |
| D-03 lossy rewrites to 8-bit | **FIXED** 🔎 | Off by default, native pixels, safe codecs only, verification gates. Residual: NEW-12 |
| D-04 split/merge | **PARTIAL** 🔎 | The rewrite is verified before unlink. Merge still has no patient or tenant check, split creates a NULL-tenant study, and unlinks happen before commit (NEW-12) |
| D-05 PIX A40 deletes studies | **OPEN** ✅ | `pix_pdq_service.py` is unchanged. PIX now also matches MRNs across tenants |
| D-06 frame-cache collision | **FIXED** ✅ | Mode-qualified key (`dicomweb.py:818, 858`). Minor residual on decode-failure fallback (`warm_service.py:172`) |
| D-07 TLS bootstrap deletes certs | **OPEN** ✅ | `tls.py` is unchanged |
| D-08 React fakes actions and data | **FIXED** 🔎 | Real API calls. Residuals in NEW-30 |
| D-09 demo AI/triage on by default | **OPEN** 🔎 | `TRIAGE_ENABLED=True`; `ai_service.py` is unchanged |

### 1.4 Silent failures and important items
| ID | Status |
|---|---|
| F-01 auto-routing never matches | **OPEN** ✅ (`indexing_service.py:340-350`) |
| F-02 lifecycle sweep no-op | **OPEN** ✅ (`worker.py` unchanged) |
| F-03 prefetch calls a missing function | **FIXED** ✅ (`send_move`) |
| F-04 quotas never enforced | **PARTIAL** ✅: enforced on C-STORE/STOW/lossy; scoping and counter broken (NEW-11); pull/KIN/migration paths still unchecked |
| F-05 HL7 sends no ACK · F-06 warm `asyncio` import · F-08 double job claims | **OPEN** ✅ (files unchanged) |
| F-07 license gate · F-09 replica double-yield · F-12 · F-13 · F-14 | **OPEN** 🔎 |
| F-10 migrations | **PARTIAL** 🔎: 5 tables migrated, no boot `create_all`, no auto-stamp; new defects NEW-03/04/25 |
| F-11 tests can't run; fixtures wipe DB | **PARTIAL** 🔎: DB guard added; collection still blocked by B-01 |
| F-15 UI finalize skips gate and ORU | **FIXED** ✅ (replaced by NEW-01) |
| XFF spoofing · cookie session expiry | **FIXED** ✅ (`ratelimit.py:43-50`, `deps.py:50-52`). Introduced NEW-15 |
| `GET /logout` doesn't revoke · `/\host` open redirect · `share=` gate bypass · `/admin` ungated · CSP/CORS/cookie defaults | **OPEN** ✅ (`main.py:906-915`, `auth.py:77`, `main.py:377`) |
| `/metrics` · `reload=True` · license PII · memory DoS | **PARTIAL**: logins/allow-lists added, see NEW-13/14 |
| Container root, default credentials, no TLS, portal web-root DB and revocation bypass | **OPEN** 🔎 (`deploy/` unchanged) |

---

## 2. 🔴 New blocking findings

### NEW-01 ✅ Finalizing an existing draft signs the stored draft and drops the edits just made
- `reports.py:249-262`: when a report exists and `wants_final` is true, `update_report` is skipped, and
  `finalize_report(report.id)` signs the previously stored text.
- The viewer sends the edited text with `status: "FINALIZED"` in one request
  (`viewer/paxs-study-list-enhance.js:643-662`). The stored `radiologist` (the draft author) is kept as signer 🔎.
- **Failure:** the EMR and the printout get stale text, signed under the wrong name. The UI reports success.
- **Fix:** apply the submitted fields to the DRAFT in the same transaction, then finalize and record the finalizer.
  Add a regression test.

### NEW-02 ✅ GPU MPR shows the wrong image region
- `paxs-mpr.js:2036-2037` sizes the render canvas to `max(cols,rows)` × `max(rows,slices)`. `:2203` renders into
  `viewport(0,0,pw,ph)`, which is bottom-left in GL coordinates. `:2229` copies `drawImage(renderCv,0,0,pw,ph,…)`,
  which reads from the top-left.
- **Failure:** any plane shorter than the canvas shows the wrong rows. For a 512×512×300 CT that is sagittal and
  coronal; with more than 512 slices, axial too. No error is shown (reproduced headless by the area review). The
  code predates this commit.
- **Fix:** read from `(0, renderCv.height - ph, pw, ph)` or size the canvas per draw. Add a phantom pixel test.

### NEW-03 ✅ The tenant-scoped identity migration removes MRN uniqueness on non-VNA installs
- `b4567890c1d2:150-155` creates `UNIQUE(tenant_id, patient_id)`. NULLs are distinct in unique indexes, so the
  default bucket (`tenant_id IS NULL`, which on a single site is every patient) has no MRN uniqueness.
- **Failure:** concurrent ingest creates duplicate patients, and lookups that expect one row then fail 🔎.
- **Fix:** use a non-NULL sentinel tenant, or a unique index on `COALESCE(tenant_id, 0)`.

### NEW-04 ❓ The same migration probably fails on MySQL 8, and the old constraint survives
- `b4567890c1d2:132` gives a TEXT column the literal default `'{}'`. MySQL 8 rejects that (error 1101), which would
  block `alembic upgrade head` on the default database.
- On MySQL, batch mode only ALTERs the table, so the old `UNIQUE(patient_id)` survives 🔎.
- This also hits **first boot of a fresh MySQL install**: `init_db()` now runs `alembic upgrade head` on an empty
  database (`app/db/session.py:254-263`) ✅.
- **Fix:** remove the server default (or use an expression default), drop the old index explicitly, and run the
  migration chain in CI on MySQL 8.

### NEW-05 🔎 STOW can replace another tenant's images through SOP/series UID reuse
- `_reject_cross_tenant_ingest` checks only the study's tenant (`dicomweb.py:249-266`). Upserts still adopt an
  existing SOP or series row and repoint `file_path` (`indexing_service.py:370-424`).
- **Fix:** return 409 when a UID belongs to another study or tenant.

### NEW-06 🔎 PET/CT fusion is aligned by array index, not patient geometry
- `paxs-mpr.js:2246-2288, 3268-3320` sample the overlay by normalized index and apply the CT window to PET values.
- **Failure:** uptake is drawn over the wrong anatomy whenever the PET and CT grids differ.
- **Fix:** resample in patient coordinates with a PET-specific window, or label fusion non-diagnostic or disable it.

---

## 3. 🟡 New important findings

| ID | Finding | Evidence |
|---|---|---|
| NEW-07 ✅ | First caller on an empty DB becomes ADMIN, with no setup token | `auth.py:191-194` |
| NEW-08 ✅ | `LDAP_USE_TLS` is never read, so `ldap://` binds send passwords in clear | `config.py:282`; `ldap_service.py:197-208` |
| NEW-09 🔎 | Reports (list/get/patch/save/finalize), worklist listing and KIN/dose/critical reads are not tenant-scoped; the auth-matrix tenant scan skips 27 routes | [5a32aa4/05](reference/5a32aa4/05-clinical-ldap-frontend.md) V4-02, V4-18; [5a32aa4/06](reference/5a32aa4/06-tests-hygiene.md) |
| NEW-10 🔎 | LDAP login adopts any local account with the same username and overwrites its role, even when the account is disabled | `ldap_service.py:287-291` |
| NEW-11 ✅ | Quota filters AND tenant and backend, so a tenant-wide quota is skipped when a backend is named; `used_bytes` never decreases; recompute is never called | `quota_service.py:71-89` |
| NEW-12 ✅ | Split/merge unlink after `flush()` but before commit; lossy replaces originals before commit; split leaves the new study with no tenant 🔎 | `split_merge_service.py:303, 365, 378` |
| NEW-13 🔎 | Volume memory exhaustion by any user: `/data` has no byte cap; `/mip` and `/mpr/slice` have no slice cap; `frames` accepts repeated indices; `/tiles` returns up to 4096 PNGs | [5a32aa4/02](reference/5a32aa4/02-dicomweb-volume.md) N2–N4, N8 |
| NEW-14 ✅ | `/metrics` is open to every RFC1918 client by default, including traffic arriving through HAProxy | `config.py:359-361` |
| NEW-15 🔎 | Behind HAProxy all clients share one rate-limit bucket (`TRUSTED_PROXIES` is loopback only), so one attacker can lock out every login | `ratelimit.py:43-50` |
| NEW-16 ✅ | Docs use `PAXS_ROOT_PATH`/`PAXS_FORWARDED_ALLOW_IPS`, which only `run_paxs.py` reads (for uvicorn); the app reads `ROOT_PATH`/`FORWARDED_ALLOW_IPS`; `REVERSE-PROXY.md` tests are wrong | `run_paxs.py:78-79`; `config.py:51-56` |
| NEW-17 🔎 | `resolve_public_base_url` trusts `X-Forwarded-Host`/`-Proto` from any peer, and the result is injected unescaped into a script, so share links can be pointed at another host | [5a32aa4/01](reference/5a32aa4/01-core-config-auth.md) N-11, N-12 |
| NEW-18 🔎 | The production gate runs before DB overrides; security toggles are runtime-editable with no audit trail | `main.py:490` vs `:500` |
| NEW-19 🔎 | `GET /api/v1/config` shows the full registry (LDAP, S3) and the admin health block to any role | [5a32aa4/01](reference/5a32aa4/01-core-config-auth.md) N-09 |
| NEW-20 🔎 | `DICOMWEB_PUBLIC=true` also allows anonymous STOW, and its read scope differs between WADO and QIDO | `deps.py:273-292, 358-370` |
| NEW-21 🔎 | Share tokens are accepted in query strings (leak risk); recipients can't load study metadata; ShareModal creates a public link as soon as it opens | [5a32aa4/02](reference/5a32aa4/02-dicomweb-volume.md) N12; [5a32aa4/05](reference/5a32aa4/05-clinical-ldap-frontend.md) V4-11, V4-15 |
| NEW-22 🔎 | GSPS checks fail open and leak `file_path`; rendered images can apply another tenant's GSPS; `zorder` frames bypass tenant scope; WADO-URI ignores bearer tokens | [5a32aa4/02](reference/5a32aa4/02-dicomweb-volume.md) N13, N14, N17 |
| NEW-23 🔎 | Viewer defects: mis-oriented 3D cut planes, overlays and HU probe ignore the letterbox, blob URL leak, failed texture upload reported as OK, MutationObserver loop, broken XHR retry, placeholder patient text | [5a32aa4/03](reference/5a32aa4/03-viewer-js.md) N3–N10 |
| NEW-24 🔎 | ORU doesn't escape CR/LF; double-finalize sends two ORUs; print shows "FINALIZED" with no report; addenda are not shown, sent or audited; study delete cascades to signed reports | [5a32aa4/05](reference/5a32aa4/05-clinical-ldap-frontend.md) V4-05–V4-09 |
| NEW-25 🔎 | Migration ops: advisory lock released before the upgrade, no reconcile path, standalone worker still calls `create_all`, SQLite primary key and index regressions | [5a32aa4/04](reference/5a32aa4/04-ingest-storage-schema.md) N-14–N-16 |
| NEW-26 ✅ | Missing UIDs fall back to `{field}_{sha1("")}`, a constant, so all such objects share one key | `storage_service.py:111-115` |
| NEW-27 ✅ | The auth-matrix test omits `require_metrics_access`, so it probably fails on `/metrics`; its assertions are weak | `tests/test_route_auth_matrix.py:32-59, 254` |
| NEW-28 🔎 | Portal ignores `PAXS_VENDOR_PRIVATE_KEY_FILE`; the default key path is inside the web root | [5a32aa4/07](reference/5a32aa4/07-deploy-licensing-docs.md) N-09, N-10 |
| NEW-29 ✅ | A copied `.env.example` gives dev mode, a placeholder key, insecure cookies, `CORS=*` and 480-minute tokens | `.env.example:105-159` |
| NEW-30 🔎 | React residuals: Forward always says "Queued", Delete leaves a stale row, drawer hides fetch errors, null `patient_name` crash, wrong sort key, no 401 handling, TS strict off | [5a32aa4/05](reference/5a32aa4/05-clinical-ldap-frontend.md) |

## 4. 🟢 Hygiene and documentation drift
- **NEW-31 ✅ Committed artefacts.** `.workbuddy-ai/` (about 56 files of AI-tool logs, memory notes, source
  backups, and the machine name and user paths), `.vscode/mcp.json` (an unvetted `npx` server), `logo/index` (a
  foreign project's **git index**, starting with `DIRC`), 17 test-generated PNGs in `app/static/uploads/`, and
  `plan_9_2026.md` duplicated in `outputs/`. The secret-scan test skips most of these. Remove them and ignore them.
- **NEW-32 🔎 Docs drift.** README says Redis is optional and "77 suites" (there are 101 test files).
  `MONITORING.md` says "not public". Module docstrings still say "guests stay readable". The 10-claim spot-check
  found 4 false.

## 🎉 What improved
Default-deny in practice, with a structural auth-matrix test. Share tokens are designed properly. Tenant guards
return 404, so other institutions' UIDs can't be probed. Reports have a single service path with addenda. Ingest is
staged and atomic. The lossy pipeline was rebuilt safely. The production gate fails closed on unknown environments.
LDAP is real now. There are 1,085 Python and 158 JS test cases, including MPR geometry phantoms. The React list no
longer fabricates data.

## 5. Remediation plan
| Phase | Items |
|---|---|
| **0 Stop-ship** | Commit the storage package, anchor `/storage/`, add `redis`, run CI on a clean checkout · rotate the vendor key and purge history · production as the default, `SECRET_KEY` required · NEW-01 · NEW-02 · NEW-03/04 with a MySQL CI run · S-05 · S-10 · S-08 residuals · S-09 |
| **1 Integrity** | NEW-05/S-12 residuals · D-01 savepoints · NEW-12 · D-04 · D-05 · D-07 · NEW-06 · NEW-11 · NEW-26 |
| **2 Correctness** | F-01, F-02, F-05, F-06, F-07, F-08 · NEW-08/10 LDAP · NEW-24 ORU and addenda · NEW-23 viewer · NEW-30 React |
| **3 Hardening** | NEW-13 caps · NEW-14–17 proxy, metrics and rate-limit config · NEW-18/19 · open redirect, `/logout`, CSP, cookie, CORS · deploy: non-root, TLS, Redis |
| **4 Hygiene** | NEW-31, NEW-32, NEW-27, pinned dependencies |

**Tests to add:** edit-then-finalize in one request (NEW-01) · GL vs CPU MPR pixels on a non-square phantom
(NEW-02) · `alembic upgrade head` on MySQL 8 and SQLite plus a concurrent same-MRN ingest (NEW-03/04) · cross-tenant
SOP reuse refused (NEW-05) · no password in non-admin `/redis/status`, and `/stats` imports no scripts (S-05/S-10) ·
cross-tenant 404 on every study-scoped route (NEW-09).
