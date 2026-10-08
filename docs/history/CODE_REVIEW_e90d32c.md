# PAXS Archive — Code Review (baseline `e90d32c`, superseded)

> 🗄️ **Historical document.** This is the original review of commit `e90d32c`. The current review of `5a32aa4`, with the status of every finding below, is [../CODE_REVIEW.md](../CODE_REVIEW.md). Links in this file were rewritten to point one level up.

> Full-codebase review of commit `e90d32c` (main), 2026-10-08. Static analysis only: the code was read, not run.
> The Python dependencies are not installed in the review environment, and the committed tree cannot import
> (see B-01). Ten area reviews were done in parallel; the lead reviewer then re-checked the blocking findings in the
> source. The area deep-dives with every finding, its evidence and the open questions are in [reference/](../reference/).
> This document is the consolidated, de-duplicated and prioritized view.

**Verification legend**

| Tag | Meaning |
|---|---|
| ✅ | Re-verified by the lead reviewer directly in the source (lines quoted or read) |
| 🔎 | Verified by the area review (code read and cited in the linked reference); not independently re-checked |
| ❓ | Depends on runtime or library behaviour, or on code that is missing from the repo; needs a test |

**Severity:** 🔴 blocking (fix before any production or PHI use) · 🟡 important (should fix) · 🟢 nit / hygiene ·
🎉 praise.

---

## 0. Summary and decision

**Decision: 🔄 Request changes. The codebase is not ready to deploy with real patient data.**

PAXS Archive covers a remarkable amount of ground for one process. It handles DIMSE, DICOMweb, HL7, FHIR and IHE
profiles, plus a VNA, reporting, routing, lifecycle and licensing. The code is generally readable, and its comments
explain *why* decisions were made. Most of the architecture is sound. The blocking issues fall into four groups:

1. **It does not build from a clean checkout.** The storage backend package was never committed, because a
   `.gitignore` rule catches it. `redis` is also imported but not listed in the requirements. Nothing (server or
   tests) runs from git as-is.
2. **Authorization is missing where it matters most.** 39 routes have no auth and 23 accept anonymous callers through
   `get_optional_user`, including routes that **write** clinical reports, store DICOM, upload files and change
   settings. Every image, FHIR patient record and study detail is readable without a login. On top of that, the
   default JWT key is public, self-registration is open, a test-only LDAP backdoor shipped, and the vendor's
   license-signing private key is committed. Any one of these is enough to take over a default install.
3. **Destructive operations have no role checks.** Any logged-in user, including one who just self-registered, can
   purge the archive with a lifecycle rule, import from or delete files under any server path, route studies to any
   node, or transcode images lossily.
4. **Several pipelines silently do the wrong thing.** Examples: auto-routing never fires, the scheduled lifecycle
   sweep is a no-op, prefetch calls a function that doesn't exist, and quotas are never enforced. Concurrent or
   retransmitted stores can delete archived objects. The frame cache can serve the wrong pixel bytes. The React study
   list *pretends* to delete, forward and share.

| Severity | Count (consolidated) |
|---|---|
| 🔴 Blocking | 24 (B-01–B-03, S-01–S-12, D-01–D-09) |
| 🟡 Important | 45+ (grouped below; ≈200 in the area references) |
| 🟢 Nits | ≈120 in the area references (highlights below) |

**Fix first (stop-ship list):** B-01, B-02, S-01, S-02, S-03, S-04, S-05, S-06, S-07, S-08, S-10, D-01, D-05.
A phased plan is in [§7](#7-remediation-plan).

---

## 1. 🔴 Build and deploy blockers

### B-01 ✅ The storage backend package is not in the repository, so the app cannot import
- `app/services/storage_service.py:35` `from app.services.storage import get_storage_backend` and `:38`
  `from app.services.storage.backend import StorageError`. The importers are `app/main.py:31-32` (via
  `receiver.py:42` and `worker.py:21`), `app/api/v1/storage.py:34`, `replication_service.py:39` and four test modules.
- `.gitignore:9` has `storage/`, an unanchored pattern meant for the runtime `./storage` data folder. It also matches
  `app/services/storage/`. `git check-ignore -v app/services/storage/__init__.py` reports `.gitignore:9:storage/`.
  The folder does not exist on disk and appears in no commit.
- **Failure:** on a fresh clone, `python run_paxs.py` fails with `ModuleNotFoundError: No module named
  'app.services.storage'`. `pytest` collects 0 of 621 tests. The Docker image builds and then crashes on start.
- **Fix:** change line 9 to `/storage/` (repo-root data folder only), then `git add app/services/storage/` from
  the working copy that has it. Add a CI job that runs `pip install -r requirements.txt && pytest` on a clean
  checkout.

### B-02 ✅ `redis` is imported unconditionally but commented out of `requirements.txt`
- `requirements.txt:32` `# redis>=5.0`, while `app/services/redis_service.py:26` has `import redis` at module level.
  The import is reached through `redis_panel.py:28` from `main.py:491`.
- **Failure:** a clean `pip install -r requirements.txt` produces an app that cannot import, even after B-01 is
  fixed. `ratelimit.py:52` imports `redis_service` outside its try block, so a guarded fallback would still 500.
- **Fix:** add `redis>=5.0` (and `ldap3` as an optional extra), or guard the import and degrade to the in-process
  LRU. Split dev requirements (`pytest`, `httpx` stays because it is a runtime import).

### B-03 🔎 Standalone builds depend on a file that is not in the repo
- `build_standalone.bat:64` and `scripts/build_exe.bat:14` run PyInstaller with `paxs.spec`, which is excluded by
  `.gitignore:15` (`*.spec`). `build_standalone.bat` also copies the live `.env` (with `SECRET_KEY` and the DB
  password) into `dist\` ([09 S9/S12](../reference/09-deploy-scripts-licensing.md)).
- **Fix:** commit the spec (un-ignore it), and ship `.env.example` instead of `.env`.

---

## 2. 🔴 Security blockers

### S-01 ✅ The default JWT signing key is public and never rejected, so anyone can become ADMIN, and an ADMIN can run arbitrary code
- `app/core/config.py:136` `SECRET_KEY: str = "paxs-insecure-dev-key-change-me"`. No code checks for it. The
  Dockerfile and compose files never set it. `.env.example:39` ships a second guessable placeholder.
- `deps.py:46-63` trusts any correctly signed token and loads the user named in `sub`.
- **Failure:** sign `{"sub":"admin","exp":<future>}` with HS256 and the default key to get full ADMIN.
  `PUT /api/v1/scripts/{name}` (ADMIN) writes a `.py` file that `scripting_service._load` runs with `exec_module` in
  the server process (`scripting_service.py:117-123`). That is remote code execution.
- **Fix:** refuse to start (outside an explicit dev profile) when `SECRET_KEY` equals a known default or is shorter
  than 32 bytes. Generate a key on first run into a protected file. Treat script upload as a privileged
  capability: disable by default, and allow-list it by file hash.

### S-02 ✅ The vendor RSA **private** license-signing key is committed and matches the key PAXS trusts
- `deploy/vendor-portal-php/config.php:43-71` contains `PAXS_VENDOR_PRIVATE_KEY` (PKCS#8, added in commit
  `8af1381`). The public key at `config.php:75-84` is byte-identical to `DEFAULT_PUBLIC_KEY_PEM` in
  `app/core/licensing/crypto.py:29-37` (checked by the lead reviewer). The area review confirmed that the private key
  derives that same public key (SHA-256 digest match). The AES container key is also a public constant
  (`crypto.py:40-42`, `config.php:40`), and so is `PORTAL_SECRET_KEY` (`config.php:36`). The repo has a GitHub
  remote.
- **Failure:** anyone with read access to the repo can mint enterprise licenses with any features, seat count,
  expiry or fingerprint `*`, and PAXS will accept them.
- **Fix:** treat the key as compromised. Generate a new pair, embed the new public key, and purge the old key from
  history (`git filter-repo`). Load the private key from a secrets store or HSM, never from source. Reissue existing
  licenses. Load the AES key and the portal secret from the environment.

### S-03 ✅ Anonymous **write** endpoints: clinical reports, settings, file upload, templates and DICOM ingest
All of these declare `user: User | None = Depends(get_optional_user)` and never check `user`:

| Route | What an anonymous caller can do | Evidence |
|---|---|---|
| `POST /api/v1/reports/save` | Create a report, overwrite **FINALIZED** findings and impression, and mark any report FINALIZED. The signer is taken from the body, or defaults to `"Dr. Radiologist"` | `reports.py:173-224` (no status check on the update branch) |
| `POST /api/v1/settings/organization` | Rewrite org name and the header/footer HTML that is injected into every printed report, and upload a file of **any extension** to `app/static/uploads/`, which is served same-origin at `/static/uploads/…` | `settings.py:73-114`, `UPLOADS_DIR` at `settings.py:26`, `/static` mount `main.py:560` |
| `POST/PATCH/DELETE /api/v1/templates…` | Create, alter or delete report templates | `settings.py:137-206` |
| `POST /dicom-web/studies[/{uid}]` (STOW-RS) | Store arbitrary DICOM. Reusing existing UIDs overwrites stored objects and repoints `Instance.file_path` across studies and tenants. The global rate limiter skips `/dicom-web` | `dicomweb.py:804-903`, `storage_service.py:293-297`, `indexing_service.py:215-221`, `main.py:358` |

- **Failure:** anyone on the network can alter a signed diagnostic report, or plant `/static/uploads/logo_x.html`
  with script that runs on the PAXS origin and reads `localStorage["paxs_token"]` from an admin's browser.
- **Fix:** use `get_current_user_any` plus a role check on every write. Gate report finalization with
  `require_destructive_role`, and keep FINALIZED reports immutable (add an explicit amendment flow). Accept only
  image MIME types and extensions for logo uploads, cap their size, and store them outside the static root.
  Require auth on STOW and refuse UID collisions whose content or study differs.

### S-04 ✅ Patient data is readable without authentication, and the `DICOMWEB_PUBLIC` switch does nothing
- **No auth dependency at all:** WADO-URI, every WADO-RS retrieve (study, series, instance, metadata, rendered,
  thumbnail, frames, bulk) (`dicomweb.py:136-759`); `GET /api/v1/studies/{uid}` with patient name, ID, birth date and
  **phone** (`dashboard.py:697-748`), plus series instances and thumbnails; all `/fhir/*` routes, where `name=%`
  pages through every patient (`fhir.py:104-248`, mounted by default because `FHIR_ENABLED=true`);
  `GET /api/v1/reports/study/{uid}` and `/print/{uid}` (`reports.py:160, 227`); GSPS reads (`gsps.py:44-64`).
- **Optional auth, anonymous allowed:** QIDO-RS (`dicomweb.py:197-300`) and `/api/v1/studies`. Anonymous callers
  get scope `(None, False)`, i.e. every study with `tenant_id IS NULL`, which in a non-VNA install is **every
  study** (`tenant_service.py:83-95`, `query_service.py:149-151`). The same applies to volume voxels, MIP and MPR
  (`volume.py:65-345`), fusion, and Redis key listing and value previews (`redis_panel.py:95-114`).
- `DICOMWEB_PUBLIC` (`config.py:253`) is read by **no code** (`grep` finds only its definition and the settings
  registry), yet README, `.env.example` and the admin UI present it as the control. Turning it off changes nothing.
- **Fix:** default-deny. Put `dependencies=[Depends(get_current_user_any)]` on routers, and decide explicitly which
  routes stay public (health, login, share redemption). If LAN-open viewing is a product requirement, wire
  `DICOMWEB_PUBLIC` into one dependency that applies to *reads only*, default it to `false`, and say so in the UI.
  Add the 401 tests that are missing today ([10-tests §4.1](../reference/10-tests.md)).

### S-05 ✅ The anonymous Redis status endpoint returns the saved Redis password
- `GET /api/v1/redis/status` uses `get_optional_user` (`redis_panel.py:63-69`) and returns
  `get_redis_status()["saved"]` (`redis_service.py:748`). That value is `saved_redis_config()`, the JSON stored
  by `save_redis_config` with `"password": password` in plain text (`redis_service.py:218-236`). The same request
  also spawns `sc query` and `tasklist` subprocesses (`redis_service.py:669`).
- **Fix:** require ADMIN on every redis-panel route, never return the password (send `has_password` only), and store
  it encrypted.

### S-06 ✅ A test LDAP backdoor shipped in production code
- `app/services/ldap_service.py:59-60`: `if username.startswith("ldap_") and password == "domainpass":` returns an
  identity. `LDAP_SERVER` is not a declared setting, so the real `ldap3` bind (`:39-56`) never runs and this mock is
  the only path. `POST /api/v1/ldap/login` (`ldap.py:29-56`, no rate limit, no audit) then issues a JWT.
- Provisioned accounts get the fixed password `"ldap_managed_password"` (`ldap_service.py:88`), which also works on
  the normal `/api/v1/auth/login`. The sync also overwrites the role of any existing local user named `ldap_…`.
- The minted role is lowercase `"radiologist"` or `"physician"`, so `require_destructive_role` (uppercase) rejects it.
  It is still a valid login for every *any-user* route, including S-08.
- **Fix:** delete the mock. Declare the `LDAP_*` settings. Generate unusable random password hashes for directory
  users. Map roles by explicit group configuration. Rate-limit and audit the route.

### S-07 ✅ Open self-registration, caller-chosen destructive role, and first-registrant-is-ADMIN
- `ALLOW_REGISTER=True` by default (`config.py:145`). `auth.py:183-191` accepts any role except ADMIN, so
  `RADIOLOGIST` is allowed, and `require_destructive_role` lets RADIOLOGIST `DELETE /api/v1/studies/{uid}`
  (`dashboard.py:834-864`). On an empty database the first caller becomes ADMIN (`auth.py:184-185`).
- **Fix:** default `ALLOW_REGISTER=false`, and always give self-registered users the least-privileged role, pending
  admin approval. Create the first admin from the CLI or a one-time setup token. The DB-init endpoint also seeds
  `admin` / `admin123` by default (`database.py:59-61`), so require an explicit password there.

### S-08 ✅ Destructive and egress operations are open to **any** logged-in user
| Operation | Gate today | Evidence |
|---|---|---|
| Create and run lifecycle **PURGE** rules, which delete studies and objects | `get_current_user` | `lifecycle.py:79-155`, `lifecycle_service.py:257-259` |
| Folder migration from **any server path**, with `delete_after_import` (unlinks the source files) | `get_current_user` | `migration.py:32, 55-71`, `migration_service.py:50-81` |
| Route any study to any registered node, edit rules | `get_current_user` | `routing.py:80-166` 🔎 |
| Lossy compression rules that rewrite stored objects in place (see D-03) | `get_current_user` | `lossy_compression.py:148-155` 🔎 |
| Split/merge studies and relabel PatientID | `get_current_user` | `split_merge.py`, `split_merge_service.py:172-216` 🔎 |
| Reading-worklist claim, assign, STAT; peer-review assign and score; critical-result acknowledge; PIX identity writes | `get_current_user` | [06 F-05, F-06, F-17, F-19](../reference/06-clinical-ihe-ai.md) 🔎 |
- Together with S-06 and S-07, anyone can register and then wipe the archive.
- **Fix:** define a permission matrix: ADMIN for policy and rules and folder import; RADIOLOGIST for clinical
  sign-off. Enforce it with dependencies, and add a negative test per route.

### S-09 ✅ Stored and DOM XSS
- **Legacy dashboard:** `app/static/app.js:374, 393-395` (and `:336, :1461, :1484-1487`) put
  `esc(s.patient_name)` inside inline `onclick="fn('…')"` attributes. The browser decodes `&#39;` back into `'`
  before the JavaScript runs, so a DICOM PatientName such as `x');alert(document.domain);//` (sent anonymously by
  C-STORE or STOW) executes. `viewerLink()` (`app.js:334-338`) interpolates `studyUid` with no escaping at all.
  `localStorage` holds the bearer token, and the CSP allows `'unsafe-inline' 'unsafe-eval'` (`main.py:229`).
- **Print report:** `reports.py:247-431` builds HTML with f-strings and no escaping of PatientName, findings,
  impression, radiologist, or the org header and footer HTML. All of these are anonymously writable (S-03).
- **Fix:** stop building handlers as strings. Use `addEventListener` with `data-*` attributes, or JSON-encode values
  for JS contexts. Escape everything in the print template (Jinja2 autoescape). Sanitize admin-supplied HTML with an
  allow-list. Move toward a nonce-based CSP.

### S-10 ✅ The server imports the repo's own tooling scripts, triggered by an unauthenticated request
- `SCRIPTS_DIR` defaults to `"scripts"` relative to the working directory (`config.py:239`). Started from the repo
  root, as `run_paxs.py` and `run_all.bat` do, that is the repo's tooling folder. `GET /api/v1/stats` has no auth
  and calls `_script_count()`, which calls `scripting_service.list_scripts()` (`dashboard.py:103-107, 357-358`).
  That function runs `exec_module` on **every** `scripts/*.py`, ignoring the `.disabled` markers
  (`scripting_service.py:64-74, 107-130, 133-161`).
- At import time, `scripts/sync_agents.py:5-24` creates `~/.gemini/config/agents`, sends a request to
  `127.0.0.1:8899`, and **deletes every `*.md` file** in that folder. `unified_gateway_server.py` builds a second
  FastAPI app, and `generate_license.py` imports the licensing crypto. The modules are cached by mtime, so this
  happens once per process and again after every reload.
- **Fix:** default `SCRIPTS_DIR` to a dedicated, empty `data/scripts/`. Never import scripts just to list them (parse
  their metadata with `ast`). Move the AI-gateway, InfinityFree and agent-sync tooling out of this repository.

### S-11 🔎 The DIMSE SCP accepts any AE, and Q/R retrieve can return the whole archive
- No `EVT_REQUESTED` or calling-AE allow-list exists (grep ✅). The SCP binds `0.0.0.0` with TLS off by default.
  Any host can run C-STORE (overwrite, see D-01), MPPS N-CREATE/N-SET (rewrite the worklist), and Storage Commitment.
- C-GET and C-MOVE with `QueryRetrieveLevel=PATIENT`, or with a STUDY/SERIES identifier that lacks the UID or has an
  **empty** UID, fall through to an unfiltered instance query (`receiver.py:425-457`, `query_service.py:186-187`).
  C-MOVE then pushes everything to any registered node, and reads every dataset into memory first
  (`receiver.py:503-508`).
- C-FIND at SERIES and INSTANCE level ignores the parent UIDs (`receiver.py:264-291`).
- **Fix:** accept only AEs registered in `dicom_nodes` (or an allow-list setting) and require the called AE. Reject
  retrieve identifiers that lack the unique key for their level. Authorize the move destination per caller. Make TLS
  with client certificates the production profile ([02 F-02, F-03, F-11](../reference/02-dicom-network-ingest.md)).

### S-12 🔎 Tenant (VNA) isolation applies only to list queries
- By-UID access ignores tenants: `_get_instance` uses `instance_query(..., include_all=True)` (`dicomweb.py:390-400`).
  The same is true of the volume/MIP/MPR paths, GSPS, KIN, DICOMDIR and anonymized export, share creation,
  lifecycle, routing, split/merge, priors and commitments
  ([03 F2](../reference/03-dicomweb-imaging.md), [05 E-09](../reference/05-nodes-routing-storage.md)).
  Deleting a tenant releases its studies into the unscoped set that every tenant-less user can see (E-20).
- **Fix:** one helper (`get_study_for_user`, `get_instance_for_user`) that applies `scope_from_user` on every by-UID
  query, plus cross-tenant 404 tests.

---

## 3. 🔴 Data-integrity and clinical-safety blockers

### D-01 🔎 Concurrent or retransmitted stores can delete archived objects
- Object keys are deterministic and `save_bytes` overwrites (`storage_service.py:293-297`). The C-STORE error path
  deletes the key (`receiver.py:104-109`). Study, series and instance inserts are check-then-insert without
  savepoints (`indexing_service.py:94-221`). A retransmission that hits an IntegrityError deletes the object the
  first, committed request points to. STOW shares one session across parts with no savepoint
  ([03 F22](../reference/03-dicomweb-imaging.md)).
- **Fix:** write to a temp key and rename after commit. Never delete a key that existed before the request. Add a
  savepoint and IntegrityError re-select for study, series and instance (the patient path already does this).
  Compare SHA-256 before overwriting.

### D-02 ✅ A DICOM UID of `..` passes the path sanitizer
- `_UID_OK = r"^[0-9.]{1,64}$"` (`storage_service.py:44`) accepts `".."`, so `instance_key("..", …)` yields
  `"../…"` and `delete_study_files("..")` calls `delete_tree("..")`. Whether this escapes `STORAGE_ROOT` depends on
  the backend, which is missing (B-01) ❓.
- **Fix:** require at least one digit and forbid empty, leading or trailing dot components. Resolve and bound every
  path in the backend.

### D-03 🔎 Lossy compression irreversibly rewrites diagnostic images
- The lossy engine renders CT/MR through window/level to 8-bit, writes JPEG **in place**, keeps the rescale tags, and
  labels the bytes with a caller-chosen transfer syntax that does not match the encoder
  (`lossy_compression_service.py:43-91`, `imaging.py:72-75`). Any user can create the rules (S-08).
- **Fix:** keep the original; transcode with a real codec at native bit depth; set `LossyImageCompression` and the
  ratio; restrict to ADMIN; verify before replacing ([05 E-03, E-04](../reference/05-nodes-routing-storage.md)).

### D-04 🔎 Split and merge lose their header rewrite; merge relabels other patients' images
- `split_merge_service.py:98-117` writes the rewritten dataset to the new key, then overwrites it with the
  *original* bytes. The DB says the study moved, but the files keep the old StudyInstanceUID and PatientID. Merge
  changes the PatientID of any study, with no patient or tenant check (`:172-216`).

### D-05 ✅ PIX A40 merge deletes the source patient's studies instead of moving them
- `pix_pdq_service.py:405-427` sets `study.patient_id = target.id` (a column write), then `session.delete(source)`.
  `Patient.studies` is `cascade="all, delete-orphan"` (`models.py:219-221`) and still holds those studies in memory,
  so the delete cascades to them, along with their series and instances (DB rows). ❓ Confirm with a test. The logic
  points to deletion.
- **Fix:** move through the relationship (`target.studies.append(study)`) or expire the collection before the
  delete. Add a merge test that asserts the studies survive.

### D-06 ✅ A frame cache key collision can serve the wrong pixel bytes
- `dicomweb.py:729-730` stores the *response* bytes (native, compressed or multipart) under
  `paxs:frame:{sop}:{n}` as well. `wado_rs_bulk_service.py:162-167` reads that same key as **raw decoded pixels**
  and returns it labelled `RAW_FRAME_CONTENT_TYPE`. A viewer can then render a corrupted image with no error.
- **Fix:** delete the write at `dicomweb.py:730` (nothing reads it), or namespace raw frames separately. Store the
  media type with the value. Add a regression test that requests the same frame as native and then as raw.

### D-07 ✅ The TLS bootstrap deletes the operator's certificate and key
- `app/dicom/tls.py:49-62`: on any load error (passphrase-protected key, permission error, half-present pair) both
  files are unlinked and replaced with a self-signed pair.
- **Fix:** never delete. Fail with a clear error, and generate a pair only when both files are absent.

### D-08 ✅ The React study list fakes destructive actions and shows invented clinical data
- `DeleteModal.tsx:19-27` and `ForwardModal.tsx:22-28` run a `setTimeout` and report success without any API call.
  `ShareModal.tsx:21` shows a made-up `https://paxs.local/share/...` URL. `StudyDetailDrawer.tsx:40-42, 130-141`
  shows `DOB: 1980-01-01`, sex `M` and a canned "unremarkable" report for **every** study. Real endpoints exist:
  `DELETE /api/v1/studies/{uid}`, `/api/v1/nodes/{ae}/send`, `POST /api/v1/share`.
- **Failure:** a radiologist "deletes" or "forwards" a study, sees success, and nothing happened. Or they read a fake
  normal report next to a real patient name.
- **Fix:** wire up the real endpoints with error handling, and remove the placeholder content
  ([08 B1-B6](../reference/08-study-list-ui.md)).

### D-09 🔎 Demo "AI" is presented as clinical output and runs by default
- `triage_service.py:54-104` flags HIGH-severity "PNEUMOTHORAX" from a whole-slice HU threshold. With no body mask,
  background air counts. It is on by default (`TRIAGE_ENABLED=True`). `ai_service.py:89-211` returns fixed
  confidences and canned normal conclusions, and an unknown `model_type` silently runs organ volumetry
  ([02 F-18](../reference/02-dicom-network-ingest.md), [06 F-09](../reference/06-clinical-ihe-ai.md)).
- **Fix:** default off, label it clearly as a demo in the UI and DICOM SR, and reject unknown models.

---

## 4. 🟡 Silent functional failures

| ID | Issue | Evidence |
|---|---|---|
| F-01 ✅ | **Auto-routing never matches a new study.** `evaluate_and_route_study` opens a *new* session before the ingest transaction commits, can't see the study, and returns `[]` without logging | `indexing_service.py:145-148`, `routing_service.py:46-55, 224-226` |
| F-02 ✅ | **The scheduled lifecycle sweep is a no-op.** `evaluate_and_run("lifecycle-worker")` passes the string as `rule_id`, so the query becomes `id == 'lifecycle-worker'` | `worker.py:234`, `lifecycle_service.py:229-236` |
| F-03 ✅ | **Prefetch never retrieves anything.** It calls `sender.c_move_study`, which doesn't exist (the AttributeError is swallowed). There is also no automatic trigger on order arrival | `prefetch_service.py:149-151`; `grep "def c_move_study"` finds nothing |
| F-04 🔎 | **Quotas are never enforced.** `save_*_with_quota` and `check_quota_before_write` have no callers, and `GET /quotas/status` is shadowed by `/{quota_id}` | `storage_service.py:256, 301`; [05 E-08, E-21](../reference/05-nodes-routing-storage.md) |
| F-05 ✅ | **The HL7 listener never sends an ACK** (there is no socket write in `hl7.py`). PIX/PDQ responses are built but never sent. An ORM without OBR raises and drops the connection along with any buffered messages. A04 never schedules the default worklist step | `hl7.py:178, 607, 739-758`; [02 F-07, F-24](../reference/02-dicom-network-ingest.md) |
| F-06 ✅ | **Volume pre-warm is dead code.** `asyncio` is never imported in `warm_service.py` (line 211 only); the NameError is logged at DEBUG | `warm_service.py:211` |
| F-07 🔎 | **The license is not enforced.** `require_valid_license` has no route callers. Revocation is in-memory only. `UNTRACKED` heartbeat replies count as valid. The portal can un-revoke with "+1 year" | [01 F-11, F-24](../reference/01-core-config-db.md), [09 R7](../reference/09-deploy-scripts-licensing.md) |
| F-08 ✅ | **Compression jobs can be processed twice.** `_claim_jobs` re-selects every `RUNNING` id in its candidate list, including ids another worker just claimed. Startup also resets *all* RUNNING jobs, including those owned by live workers, and two workers share the same `.tmp` path | `worker.py:63-92, 367-386`, `compression_service.py:232` |
| F-09 🔎 | **The read-replica dependency turns endpoint 4xx into 500** (it yields twice after catching the thrown exception). It also runs a sync DB query on the event loop for every request | `session.py:195-220`; [01 F-14](../reference/01-core-config-db.md) |
| F-10 🔎 | **Schema drift.** `create_all` runs on boot. "Migrate to head" stamps head when a table "already exists", which skips column migrations. Five tables have no migration. Two migrations use BIGINT PKs that don't autoincrement on SQLite | `session.py:234-240`, `db_admin.py:265-271`; [01 F-15–F-17](../reference/01-core-config-db.md) |
| F-11 ✅ | **The test suite can't run (B-01), and its fixtures wipe whatever `DATABASE_URL` points to** (`setdefault` lets an exported URL win, then every table is deleted) | `tests/conftest.py:18-20, 43-46, 63-65, 79-83` |
| F-12 🔎 | **Storage Commitment's 2-second sweep reports in-flight instances as permanent failures.** Transaction UIDs can be reopened or hijacked, and the fallback association skips TLS | `commit_sender.py:41, 205, 248`; [02 F-09](../reference/02-dicom-network-ingest.md) |
| F-13 🔎 | **Pulls report success even when sub-operations failed**, and remote archives are told "success" for local ingest errors | `sender.py:314-326, 469-480` |
| F-15 ✅ | **The shipped UI finalizes reports through the anonymous path, which skips the role gate and the EMR.** The PaxsViewer report modal (`viewer/paxs-study-list-enhance.js`) calls `/api/v1/reports/save`, which sets `FINALIZED` and commits without `require_destructive_role` and without `finalize_report`'s ORU^R01 delivery. No shipped UI calls the gated `POST /api/v1/reports/{id}/finalize`; the dashboard only reads `GET /api/v1/reports?study_uid=` (`app.js:474`). Reports finalized in the viewer never reach the EMR | `reports.py:173-224` vs `:117-130`, `report_service.py:263-312` |
| F-14 🔎 | **Lifecycle and purge are not transactional.** Partial moves leave rows pointing at missing objects. Purge deletes metadata even when object deletion fails, and ignores DR replicas. Replication backfill covers only the 500 newest studies | [05 E-13–E-15](../reference/05-nodes-routing-storage.md) |

---

## 5. 🟡 Important: security hardening, correctness and performance

**Auth and session**
- ✅ The rate limiter keys on the client-controlled first `X-Forwarded-For` value, which defeats login throttling
  (`ratelimit.py:29-33`). Trust it only from configured proxies.
- ✅ The cookie session check ignores `sessions.expires_at` (`deps.py:36-37`). `GET /logout` clears the cookie without
  revoking the session (`main.py:713-719`). Access tokens survive logout, password reset and disable for up to 15 min.
- ✅ Open-redirect bypass: `next=/\evil.example` passes `startswith("/") and not startswith("//")`
  (`auth.py:66-72`), and `login.html:242` then assigns it to `window.location`.
- ✅ The access token lives in `localStorage`, the CSP allows unsafe-inline/eval, `AUTH_COOKIE_SECURE=False`, and
  CORS is `*`.
- 🔎 Lockout can be used for DoS (anyone can lock `admin`). The unknown-user path skips PBKDF2 (timing oracle).
  There is no CSRF token (SameSite=Lax only). Admin password reset doesn't revoke sessions or record the actor
  ([04 F11–F17](../reference/04-auth-admin-ops.md)).
- 🔎 The viewer gate is bypassed by `share=` anywhere in the query string, and `/admin` is not gated (`main.py:295-310`).

**Secrets and PHI handling**
- 🔎 Plain-text secrets in the DB: `DicomNode.web_password`, the storage backend `access_key`/`secret_key`, the Redis
  config, and raw share tokens.
- 🔎 Unauthenticated `/api/v1/license*` returns customer PII and hardware serials, and `/fingerprint` spawns
  PowerShell/WMIC **on the event loop** ([01 F-12](../reference/01-core-config-db.md)).
- 🔎 Webhooks send PHI over plain HTTP with no SSRF guard and unbounded threads. The slow-query log keeps raw
  parameters (PHI). The PHI access audit is missing for most WADO/QIDO/volume reads.
- 🔎 Share links don't protect image data: after expiry or deletion, the study UID alone still opens every image
  (follows from S-04).
- 🔎 The anonymizer is top-level only and covers PS3.15 incompletely, while the UI claims de-identification
  ([05 E-23](../reference/05-nodes-routing-storage.md)).

**Denial of service and performance**
- 🔎 Anonymous memory exhaustion: STOW part headers and preamble have no cap (quadratic `find`), `/api/v1/volume/{uid}/data`
  builds up to 4096 slices in memory, C-MOVE reads every dataset up front, and WADO study/series multipart holds
  about 2× the study size ([03 F4, F6, F24](../reference/03-dicomweb-imaging.md)).
- 🔎 Blocking work on the event loop: synchronous Redis `cache_get/set` in WADO handlers (with a 1.5 s reconnect
  attempt on every call while Redis is down), MD5 ETags over full payloads, gzip of up to 32 MiB, DICOMDIR/export
  ZIP building, and settings writes ([03 F10](../reference/03-dicomweb-imaging.md), [01 F-26](../reference/01-core-config-db.md)).
- 🔎 Anonymous `/api/v1/stats` runs about 45 sequential `COUNT(*)` queries, plus the script import in S-10. The
  `/studies` sort by series, instances or modality runs `GROUP BY` over whole tables.
- 🔎 One DICOM association per instance when sending (`sender.py:750`). A new event loop and DB connection per
  C-STORE. A full scan of PENDING commitments on every store.
- 🔎 Process caches (`_BUNDLE_CACHE`, `_VOL_CACHE`, `_FRAME_CACHE`) are never invalidated on delete or re-import.
  `_FRAME_CACHE` is used across threads without a lock. PHI responses are sent `immutable` for a year.

**Deployment**
- 🔎 The container runs as root. Default credentials are in compose (MySQL `root`, `paxs`, MinIO
  `minioadmin/minioadmin` published on :9000/:9001, Grafana `admin/admin`). HAProxy Prometheus stats on :8405 have
  no auth. There is no TLS anywhere in the LB topology. There is no Redis service, although multi-node rate limits
  and locks need one. Images are unpinned. `reload=True` is hard-coded in `run_paxs.py:49`
  ([09 D1–D9](../reference/09-deploy-scripts-licensing.md)).
- 🔎 Vendor portal: the SQLite DB sits in the web root with no `.htaccess`; activation has races; PHP and Python
  canonical JSON differ for non-ASCII names, so those licenses fail verification ([09 R8–R16](../reference/09-deploy-scripts-licensing.md)).

**Clinical correctness**
- 🔎 Worklist claim is last-writer-wins with no ownership. Accession-only matching can close another patient's step.
  An MPPS retry regresses a completed exam. There are no state-transition guards. Dose totals are overwritten
  instead of aggregated. FHIR output is not valid R4 ([06 F-13–F-21, F-36](../reference/06-clinical-ihe-ai.md)).
- 🔎 Inbound C-STORE overwrites patient demographics (last writer wins), and missing UIDs become the shared
  placeholder `"UNKNOWN"`, which merges unrelated objects ([02 F-13](../reference/02-dicom-network-ingest.md)).
- 🔎 The UI thumbnail matching can show another study's image on a row. MPR sagittal is broken on the CPU path
  ([07 F16–F17](../reference/07-web-ui-static.md)).

---

## 6. 🟢 Nits and maintainability (highlights)

- **Repository hygiene:** about 149 MB of prebuilt viewer bundles plus several generations of stale hashed
  `studies-ui` assets are committed (`emptyOutDir: false`). Unrelated tooling lives in `scripts/` (Gemini gateway,
  agent sync, an InfinityFree anti-bot solver, a PHP password reset for another project). `settings.example.json`
  belongs to a different tool.
- **Docs drift:** README repeats its Architecture and Quick Start sections twice. Settings `HTTPS_PORT`,
  `LICENSE_FILE_PATH` and `LICENSE_GRACE_PERIOD_DAYS` are unused. Config comments promise HTTPS cert generation that
  does not exist. `quota_service` and `storage_service` docstrings contradict each other. `.env.example` defaults
  differ from `config.py` (token lifetime 480 vs 15 min, RLE vs JPEG-LS).
- **Code health:** `fusion` is mounted only under `FHIR_ENABLED`. `STUDIES_UI_DIR` is defined twice in `main.py`.
  There is a fragile `"gzip" in str(headers)` pre-check. Imports, mixins and helpers are unused (`uuid`,
  `TimestampMixin`, `session_token_from_cookie`, `parse_multipart`, `clear_frame_cache`). `requirements.txt` has no
  pins, lock file or hashes.
- **Frontend:** TypeScript `strict` is off. A null `patient_name` blanks the whole React app (no error boundary).
  The patient sort key isn't recognized by the backend. The Vite dev proxy targets :8042 instead of :8000. A full
  mock viewer with fake patient data ships unreachable in the bundle.
- **Tests:** 42 fixed `sleep`s are used as synchronization, a "concurrency" test runs sequentially, one assertion is
  tautological (`test_gsps.py:296`), env vars leak between tests, and the routes for quota, PIX, AI and settings have
  no auth tests ([10-tests](../reference/10-tests.md)).

---

## 🎉 What is done well

- **Refresh-token design:** opaque tokens stored only as SHA-256, rotated on every use, with family-wide revocation
  when a token is replayed (`security.py`, `auth.py`). Passwords and JWT signatures are compared in constant time.
  Roles are re-read from the DB instead of trusted from the token.
- **The storage-key sanitizer** (`_safe_component`) covers almost every traversal vector (the `..` gap in D-02 is a
  one-line fix), and object keys are deterministic and human-readable.
- **The DICOMweb client has an SSRF guard** that blocks loopback, private, link-local and metadata ranges, and
  redirects are disabled.
- **The STOW parser streams** the request part by part, with per-part and total caps, instead of buffering the body.
  QIDO uses keyset pagination to avoid deep OFFSET scans on MySQL.
- **The worker's claim pattern** (guarded `UPDATE … WHERE status='PENDING'` then commit) is the right idea and only
  needs a claim token. The read-replica fallback and the slow-query panel show thoughtful operations design.
- **Security headers, the HSTS-only-over-HTTPS rule, the open-redirect check, the last-admin guard, and refusing
  ADMIN at self-registration** show security intent. Most of the blockers come from inconsistent *application* of
  these patterns, not from their absence.
- **The test suite is broad:** 621 tests, real negative 403 tests for many admin surfaces, a real DICOM SCP fixture,
  and an isolated temp DB.
- **Comments explain decisions** (NullPool across loops, rollback journal on Windows, cache headers). That makes the
  code much easier to review and maintain.

---

## 7. Remediation plan

| Phase | Goal | Items |
|---|---|---|
| **0 — Stop-ship (days)** | Make it build. Close the takeover paths | B-01, B-02 · rotate and purge the vendor key (S-02) · reject the default `SECRET_KEY` (S-01) · `ALLOW_REGISTER=false`, ignore the requested role, setup-token first admin (S-07) · delete the LDAP mock (S-06) · router-level default-deny auth with an explicit public list (S-03, S-04) · ADMIN-only Redis panel, stop returning the password (S-05) · dedicated `SCRIPTS_DIR`, no import-to-list (S-10) · role matrix for lifecycle, migration, routing, lossy, split/merge (S-08) |
| **1 — Integrity (1–2 weeks)** | Stop silent data loss | D-01 temp-key and rename with savepoints · D-02 sanitizer · D-05 PIX merge · D-06 cache key · D-07 TLS bootstrap · D-03/D-04 disable lossy and split/merge until fixed · S-09 escaping and handler refactor · S-11 AE allow-list and Q/R identifier validation · S-12 tenant-scoped lookups |
| **2 — Correctness (2–4 weeks)** | Make features do what the UI says | F-01 routing after commit (outbox) · F-02 lifecycle arg · F-03 prefetch · F-04 quotas · F-05 HL7 ACKs · F-06 warm import · F-07 license gate · F-08 claim tokens and leases · F-09/F-10 DB dependency and migrations · D-08 wire the React actions · D-09 demo AI off |
| **3 — Hardening and performance** | Production readiness | Async Redis, or thread offload with backoff · streaming WADO multipart, volume and exports · association reuse · PHI audit hook · encrypt secrets at rest · CSP nonces · deploy: non-root, secrets, TLS, Redis service, pinned images |
| **4 — Hygiene** | Long-term maintainability | Move tooling and prebuilt bundles out of git (release artifacts) · pin dependencies · docs cleanup · TS strict · replace sleeps in tests · CI on a clean checkout with an auth-matrix test generated from [API_REFERENCE.md](API_REFERENCE_e90d32c.md) |

### Tests to add first
1. A table-driven **auth matrix** test: for every route in the API reference, assert 401 for anonymous callers and
   403 for the wrong role, with an explicit allow-list of public routes.
2. Concurrent C-STORE/STOW of the same SOP and the same new study: no object deleted, no 500.
3. A PIX A40 merge keeps the studies. Split/merge rewrites the stored headers.
4. Requesting a frame as native, then as raw: identical pixels.
5. Cross-tenant 404 on every by-UID route.
6. A new study matching a routing rule is enqueued. The worker lifecycle sweep applies rules.
7. A C-GET/C-MOVE identifier with no UID or an empty UID is rejected.
