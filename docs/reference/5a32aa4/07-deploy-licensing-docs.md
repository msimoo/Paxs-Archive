# Deployment, licensing, environment and project docs — changes in 5a32aa4

> **Re-review of commit `5a32aa4`** (baseline `e90d32c`), part of the [PAXS Archive documentation](../../README.md). Static analysis, 2026-10-08; line numbers refer to `5a32aa4`. Baseline area reference: [09-deploy-scripts-licensing.md](../09-deploy-scripts-licensing.md). Findings are consolidated and prioritized in [CODE_REVIEW.md](../../CODE_REVIEW.md), which takes precedence where the two differ. Finding IDs here (N1, N-01, V4-01, …) are local to this file; CODE_REVIEW.md maps them to NEW-xx IDs.

Range: `e90d32c` (old) to `HEAD` = `5a32aa4` (new).

**Method and limits.** Static review only: `git diff`, `git show`, `git grep`, and reading HEAD files. No server, Docker, pytest, or PHP run was performed. uvicorn is not installed in the sandbox, so uvicorn behaviour is marked "unverified". Key material was never printed. Key checks used hashes, boolean matches, and derived public-key comparison.

Severity legend: 🔴 blocks deployment or exposes data/access · 🟡 real defect or misleading security claim · 🟢 minor / doc drift.

---

## 1. Change summary (per file, e90d32c to HEAD)

| File | Change | Notes |
|---|---|---|
| `deploy/vendor-portal-php/config.php` | +98/−47 | Removes the embedded 2048-bit RSA PKCS#8 private key. Adds `paxs_env()` (lines 40-48), loads git-ignored `config.local.php` (52-55), makes DB/admin-hash/PORTAL_SECRET_KEY/AES salt env-overridable (59-91). Adds `paxs_vendor_private_key()` (115-135): env `PAXS_VENDOR_PRIVATE_KEY`, then file `PAXS_VENDOR_PRIVATE_KEY_FILE` (default `__DIR__/vendor_private.pem`, line 119), then constant. Throws when none is set. Public key unchanged (94-104). |
| `deploy/vendor-portal-php/crypto.php` | +7/−2 | `signLicense()` uses `paxs_vendor_private_key()` (line 68). Fail-closed. |
| `deploy/vendor-portal-php/config.local.php.example` | new, 59 lines | Operator template. Line 20 defines constant `PAXS_VENDOR_PRIVATE_KEY_FILE`, which the resolver never reads (see N-09). |
| `deploy/vendor-portal-php/README.md` | +12/−4 | Documents config.local and the key resolution order. Still documents default admin credentials (line 34) and the `public_html` install with `chmod 755` (43-46). |
| `deploy/vendor-portal-php/` other files (`index.php`, `api/activate.php`, `api/validate.php`, `login.php`, `db.php`, `auth.php`, `schema.sql`) | unchanged | Revoke/extend/validate logic unchanged (R7). Login page unchanged (R12). No `.htaccess` added (R8). |
| `app/core/licensing/crypto.py`, `manager.py` | unchanged | `git diff` empty. `DEFAULT_PUBLIC_KEY_PEM` identical (see S-02 row). |
| `app/api/v1/license.py` | +4/−1 | `/license/status` now needs any user (`get_current_user_any`); `/license/fingerprint` now needs admin. Positive change. |
| `.env.example` | 147 changed lines | Adds sections for cache budgets, metrics ACL, production gate, cookies, HTTPS, sub-path, LDAP, Redis, storage legacy paths. Safer defaults: `ALLOW_REGISTER` true→false, `DICOMWEB_PUBLIC` true→false, `PUBLIC_BASE_URL` `http://0.0.0.0`→empty. `ENVIRONMENT=development` and placeholder `SECRET_KEY` kept. See §5. |
| `run_all.bat` | 113 changed lines (rewritten) | Validates `venv\Scripts\python.exe` (no global `python` fallback). Runs `import app.main` and `init_db()` in-window before spawning windows (lines 41, 44). Server runs in `cmd /k`. Printed URLs use `:8000`. Redis auto-detect (48-64). |
| `health_monitor.bat` | +6/−4 | Printed URLs now `:8000` (67-68). `cmd /k`. Probe still wrong (96-97), see S-6. |
| `run_paxs.py` | 53 changed lines | `_is_production()` gate for HTTPS fail-closed (33-61) and `reload=not _is_production()` (89). Passes `forwarded_allow_ips` and `root_path` from `PAXS_FORWARDED_ALLOW_IPS` / `PAXS_ROOT_PATH` to uvicorn only (78-79, 91-92). |
| `deploy/MONITORING.md` | +75/−4 | Decode-cache metrics docs and two alerts. Notes section now says `/metrics` "is **not** public" (this is false for the shipped LB stack, see N-03). |
| `deploy/REVERSE-PROXY.md` | new, 230 lines | nginx, Traefik, Caddy, Compose and HAProxy snippets, plus verification steps. Contains env-name and test errors (N-05, N-06). |
| `requirements.txt` | +4/−1 | `boto3` uncommented; `botocore` added; `ldap3` commented as optional. `redis` still commented (line 34). Versions are lower bounds only. |
| `.gitignore` | +3 | Ignores `deploy/vendor-portal-php/config.local.php` and `*.pem`. Line 9 `storage/` is still unanchored (see N-01). |
| `deploy/Dockerfile`, `docker-compose.lb.yml`, `docker-compose.monitoring.yml`, `haproxy.cfg`, `prometheus.yml`, `grafana/*` | unchanged | Prior findings D1-D9 remain. |
| `README.md` | rewritten, 239 lines | Front page, quick start, URL map, security notes. See §4. |
| `PROJECT_STRUCTURE.md` (729), `RUN_AND_BUILD_GUIDE.md` (818) | new | Arabic/English structure and run guides. |
| `audit_and_roadmap/01-04*.md` | new (281/590/178/272) | Architecture, bug register, performance/DICOM audit, execution plan. |
| `PAXS_VIEWER_DIAGNOSTIC_AUDIT.md` (1066), `PAXS_VIEWER_REMEDIATION_ROUND1-3.md` (349/381/338) | new | Viewer audit and remediation rounds with status tables. |
| `plan_9_2026.md` (422) + `outputs/plan_9_2026.md` (identical) | new | Review, fix plan, C01-C10 register. |
| `plan_viewer_process.md` (899) | new | Architecture plan, status "Pending Implementation". |
| `.workbuddy-ai/` (about 60 files), `.vscode/mcp.json`, `logo/index`, 18 PNGs in `app/static/uploads/` | new | AI-tool scratch directory committed with this change. Not part of V6 scope, noted under N-14. |

---

## 2. Prior findings status

| ID | Status | Evidence (HEAD) | Notes |
|---|---|---|---|
| **S-02 / 09-R1** vendor RSA private key | **PARTIAL** | Key removed from the HEAD tree (`git grep` for a PRIVATE KEY header in HEAD: no matches). Added in `8af1381`, removed in `5a32aa4` (`git log --all -S`). The key in `e90d32c` derives the same public key as the shipped verifier: SHA-256 of the whitespace-stripped body is `01ca506e…` for the old `config.php`, HEAD `config.php`, and `crypto.py:29-37`. None of the 25 key body lines appears anywhere in the HEAD tree (control: a public-key line matches `crypto.py` and `config.php`). | The leaked key still produces licences that PAXS accepts. Not rotated, no new public key, history not purged. Fail-closed resolver is in place (`config.php:117-133`, `crypto.php:68`). AES salt (`config.php:91`) and `PORTAL_SECRET_KEY` (`config.php:84`) are env-overridable but still default to public constants. |
| **B-03** standalone build depends on `paxs.spec` | **OPEN** | `build_standalone.bat:64`, `scripts/build_exe.bat:14` run `paxs.spec`. `.gitignore:15` (`*.spec`). No spec file in tree. Both scripts unchanged. | |
| **09-R7** licence revocation not enforceable | **OPEN** | (a) `deploy/vendor-portal-php/index.php:145` extend sets `status='ACTIVE'` with no status check (un-revoke). (b) `api/validate.php:52` unknown licence returns `success:true, status:UNTRACKED`. `manager.py:330-333` accepts unless `revoked`. (c) Revocation is only in memory (`manager.py:331`). First heartbeat is after 24 h (`main.py:514`). `LICENSE_AUTO_VALIDATE_ONLINE=True` (`config.py:498`). | Portal and client logic unchanged. |
| **09-R8** portal SQLite in web root | **OPEN** | `config.php:62` DB path in web root. No `.htaccess` in the repo (`git ls-files` for htaccess: empty). Portal README:43-46 says `chmod 755`. | New related issue: default key path is also in web root (N-10). |
| **09-R12** default admin credentials on login page | **OPEN** | `login.php:204` still renders the default credentials on the public login page. Portal README:34 documents them. | |
| **09-R4** open registration / first caller becomes ADMIN | **PARTIAL** | Default `ALLOW_REGISTER=false` (`config.py:207`, `.env.example:126`). Role restriction (`auth.py:196-212`). Bootstrap: when the users table is empty, the first caller becomes ADMIN regardless of the flag (`auth.py:191-193`). | Any fresh reachable install is claimable by the first HTTP caller, including the LB stack with an empty MySQL. |
| **09-R2 / S-01** public default JWT key | **OPEN** | Shipped default `SECRET_KEY` (`config.py:189`). Production gate is off when `ENVIRONMENT` is unset and `DEBUG` is not pinned false (`config.py:610-630`). `Dockerfile:21-26` and `docker-compose.lb.yml:36-46` set neither. | See N-04 and the LB exposure in N-03. |
| **09-R3** DICOMweb open | **PARTIAL (likely fixed)** | Default `DICOMWEB_PUBLIC=false` (`config.py:427`, `.env.example:158`). Enforced at `deps.py:251` and `dicomweb.py:201`. STOW-specific 401 not re-verified. | |
| **09-R5 / B-01** `app/services/storage/` missing | **OPEN (confirmed)** | Package absent from disk and from `git log --all -- app/services/storage` (empty). Imported at `api/v1/storage.py:34`, `storage_service.py:35,43`, `replication_service.py:39`. Router mounted at `main.py:624,670`. | See N-01. |
| **09-R6 / B-02** hard `import redis` | **OPEN** | `services/redis_service.py:26` (unguarded, module level). `redis_panel` mounted at `main.py:617,677`. `requirements.txt:34` still commented. | See N-02. |
| **09-R13** `/metrics` and HAProxy stats on public ports | **PARTIAL** | `/metrics` is now IP-gated (`metrics.py:59`, `config.py:359-361`). At `e90d32c` it had no auth at all (`metrics.py:88`). Via the shipped HAProxy path, every client reaches the node from an RFC1918 peer, so it is anonymous. HAProxy `:8405` still published (compose:20). | See N-03. Round 3 §4b says the old route "required authentication". `e90d32c` does not show that. |
| **09-D1** container runs as root | **OPEN** | `deploy/Dockerfile` unchanged; no `USER` directive (0 matches). | |
| **09-D2** default credentials in compose/Dockerfile, MinIO published | **OPEN** | `docker-compose.lb.yml:41-42, 92-93` default MinIO credential; `:75, :78` default MySQL credentials; `:82` healthcheck passes the MySQL password on the command line; `:96-98` MinIO ports published; `Dockerfile:22` and `compose:38` contain a literal DB credential. | Values not reproduced. |
| **09-D3** HAProxy `:8404` healthcheck and port reference nonexistent frontend | **OPEN** | `haproxy.cfg` frontends are `metrics_in :8405`, `http_in :8000`, `dimse_in :10400`, `hl7_in :2575`. No 8404. `docker-compose.lb.yml:19, 26` still publish and check 8404. | RUN guide:473 repeats the 8404 claim. |
| **09-D4** Grafana / Prometheus defaults and exposure | **OPEN** | `docker-compose.monitoring.yml:38` Grafana admin password is the literal default. `:21-22` Prometheus `9090` and `:35` Grafana `3000` published. | |
| **09-D5** Grafana datasource lacks `uid` | **OPEN** | `deploy/grafana/provisioning/datasources/prometheus.yml` has no `uid:` (0 matches). | |
| **09-D6** Redis absent from compose | **OPEN** | No redis service in `docker-compose.lb.yml` (0 matches). `REDIS_URL` defaults to `127.0.0.1` (`config.py`, `.env.example:58`). | `.env.example:57` says Redis should be on for real deployments. The LB stack has no Redis. |
| **09-D7** rate limit trusts client `X-Forwarded-For` | **PARTIAL** | XFF is now believed only when the TCP peer is in `TRUSTED_PROXIES` (default `127.0.0.1,::1`, `config.py:232`; `ratelimit.py:43-45`). It still takes the left-most entry (`ratelimit.py:47`). In the default layout this is mitigated because uvicorn already rewrote `request.client`, so the peer is no longer trusted. | See N-08. |
| **09-D8** `reload=True` in entrypoint | **PARTIAL** | `run_paxs.py:89` `reload=not _is_production()`. Dockerfile and LB compose do not set `ENVIRONMENT`, so every shipped container runs with reload on. | |
| **09-D9** unpinned images | **OPEN** | `Dockerfile:3` `python:3.12-slim`. `docker-compose.lb.yml:11,72,88` `haproxy:3.0-alpine`, `mysql:8.0`, `minio/minio:latest`. `docker-compose.monitoring.yml:16,28` `:latest`. `requirements.txt` uses `>=` only. | |
| **09-S6** health monitor probes wrong port | **PARTIAL** | Printed URLs fixed (`health_monitor.bat:67-68`, `run_all.bat:87-89`). Probe still `http://0.0.0.0/health` (`health_monitor.bat:96-97`): port 80, and no root `/health` route exists. Unauthenticated routes are `/api/v1/health/live` and `/ready` (`dashboard.py:127-134`). `/api/v1/health` is admin-only (`dashboard.py:235-236`). | |
| **09-S12** build copies live `.env` | **OPEN** | `build_standalone.bat:76-78` still copies `.env` into `dist\`. `scripts/build_exe.bat` copies only `.env.example`. | |

---

## 3. New findings

### N-01 🔴 `app/services/storage/` is missing, so `app.main` cannot be imported from this tree
- **Where:** imports at `app/api/v1/storage.py:34`, `app/services/storage_service.py:35,43`, `app/services/replication_service.py:39`. Router mounted at `app/main.py:624,670`. `.gitignore:9` (`storage/`) is unanchored.
- **What is wrong:** the package is not on disk and has never been in git (`git log --all -- app/services/storage` is empty). `PROJECT_STRUCTURE.md` lists only `storage_service.py`. `plan_9_2026.md` §3 says "Implemented".
- **Failure scenario:** `import app.main` raises `ModuleNotFoundError`. `run_all.bat` pre-flight stops at line 41 with a clear message. `python run_paxs.py` and the Docker image fail at start. Nodes restart-loop.
- **Fix:** commit the package. Anchor `.gitignore` to `/storage/`. Add a CI step `python -c "import app.main"`. Confirm whether a copy exists on the developer machine outside this checkout (open question 1).

### N-02 🔴 Hard `import redis` breaks a fresh install; README claims Redis is optional
- **Where:** `app/services/redis_service.py:26` (unguarded). `app/api/v1/redis_panel.py:28` imports it at module level. `app/main.py:617,677` mounts the panel. `requirements.txt:34` leaves `redis` commented. README:114 and :124 say "PAXS runs standalone without it".
- **Failure scenario:** `pip install -r requirements.txt` followed by start raises `ModuleNotFoundError: redis` at import, before any Redis use.
- **Fix:** add `redis>=5.0` to requirements, or guard the import with `try/except ImportError` and degrade the panel.

### N-03 🔴 `/metrics` is anonymously reachable through the public HAProxy port in the shipped LB stack
- **Where:** `deploy/haproxy.cfg:37-43` (`http_in` on `*:8000`, `option forwardfor`), `:45-53` (`paxs_api` forwards every path). `deploy/docker-compose.lb.yml:16` publishes `8000`. `app/api/v1/metrics.py:182` `/metrics` depends on `require_metrics_access` (line 37-60). `app/core/config.py:359-361` default `METRICS_ALLOW_IPS` includes `172.16.0.0/12`. Nodes do not trust the HAProxy peer (`run_paxs.py:78,91` default `127.0.0.1`, `config.py:56`), so `request.client` is the HAProxy container IP.
- **What is wrong:** `MONITORING.md` (Notes) says `/metrics` "is **not** public". `.env.example:88-96` and Round 3 §4b say the RFC1918 default is "exactly what the shipped Docker/HAProxy topology needs ... keeping the endpoint off the public internet". Both are false for the shipped topology. Round 3's "verified" checks used direct peers, not the HAProxy path.
- **Failure scenario:** anyone who can reach `http://<host>:8000/metrics` gets node identity, DB up/down, replica lag, queue depths, cache pressure, and HAProxy traffic data with no credentials. Docker's default bridge range (172.17-31.x) falls inside `172.16.0.0/12`. Not verified at runtime.
- **Fix:** (a) block `/metrics` on the public frontend: `http-request deny if { path /metrics }` in `http_in`. (b) Set the node-side trust to the HAProxy subnet (`FORWARDED_ALLOW_IPS`, see N-05) so the real client IP is used. (c) Narrow `METRICS_ALLOW_IPS` to the Prometheus subnet. (d) Do not publish `8405` to all interfaces. Add a test that goes through HAProxy.

### N-04 🟡 Copy-pasting `.env.example` produces a forgeable, development-mode server
- **Where:** `.env.example:105` `ENVIRONMENT=development`. `:111` placeholder `SECRET_KEY` (in `INSECURE_SECRET_KEYS`, `config.py:597-603`, but only enforced when production, `config.py:635-640`). `:115` `AUTH_COOKIE_SECURE=false`. `:122` `TOKEN_EXPIRE_MINUTES=480`. `config.py:199` defaults to 15, with a comment saying "15-minute lifetime". `:159` `CORS_ORIGINS=*`. `:58` unauthenticated Redis URL. `:7` DB URL with a literal password.
- **What is wrong:** the production gate is opt-in. A verbatim copy leaves the server in development mode, so the public placeholder key and an 8-hour access token are accepted.
- **Failure scenario:** a copied `.env` on a reachable host lets anyone forge `sub=<admin user>` tokens (see R2). Access tokens last 8 h, and logout does not revoke them (CODE_REVIEW §5).
- **Fix:** change the example to refuse startup unless set (for example `ENVIRONMENT=` with no default), or set `TOKEN_EXPIRE_MINUTES=15` to match code. Put a bold "set ENVIRONMENT=production and SECRET_KEY before exposure" line at the top.

### N-05 🟡 Documented `PAXS_ROOT_PATH` and `PAXS_FORWARDED_ALLOW_IPS` are not read by the app
- **Where:** `app/core/config.py:51` `ROOT_PATH` and `:56` `FORWARDED_ALLOW_IPS`. `model_config` (`config.py:500`) has no `env_prefix` or alias. The app's `ForwardedPrefixMiddleware` uses `settings.FORWARDED_ALLOW_IPS` (`app/main.py:575-578`). `resolve_public_base_url` uses `settings.ROOT_PATH` (`config.py:586-594`). Only `run_paxs.py:78-79` reads the `PAXS_` names, and only for uvicorn.
- **Documented names that do nothing at app level:** `.env.example:185,192`, `deploy/REVERSE-PROXY.md:23,30,169-170`, `README.md:234-235`, `RUN_AND_BUILD_GUIDE.md:522`, DIAG audit V2-4 and Round 2 §1.
- **Failure scenario:** with `PAXS_FORWARDED_ALLOW_IPS=<proxy subnet>` set as documented, uvicorn accepts the proxy's `X-Forwarded-For`, but `ForwardedPrefixMiddleware` still ignores `X-Forwarded-Prefix`, `X-Forwarded-Host` and `X-Forwarded-Proto` from it. Sub-path shells are not prefixed, asset URLs 404, and share/QR/FHIR links use the wrong origin.
- **Fix:** use `validation_alias=AliasChoices("PAXS_ROOT_PATH","ROOT_PATH")` (and the same for forwarded IPs), or rename the docs. Pass the same value to both uvicorn and the middleware. Add an env-level test.

### N-06 🟡 `deploy/REVERSE-PROXY.md` has wrong verification steps and advice
- **§8 step 4** (lines 203-205) expects `404` for an "untrusted peer". The curl runs from localhost, and `127.0.0.1` is trusted by default (`config.py:56`). The prefix is honoured, so the expected code is 200, not 404.
- **§8 steps 3 and 5** (197-201, 209) grep `public_base_url` from `/api/v1/health`. That route requires admin (`dashboard.py:235-236`), so an unauthenticated curl returns 401. The doc gives no token.
- **§5 Traefik** (144-146) says "Add `/paxs` to `PAXS_FORWARDED_ALLOW_IPS`". That setting takes peer IPs or subnets, not paths.
- **§5** (148-149) says "Do not use both" stripprefix and the app's stripping. The example itself uses both middlewares, and the header is still required. Contradictory.
- **§6 Caddy** (151-161) has no trust setting. Without one, headers are ignored (same symptom as N-05).
- **§1 table** (line 8) says `ROOT_PATH`, the body says `PAXS_ROOT_PATH` (N-05).
- **§1** (27) lists `"*"` as a trust option without the consequence (N-07).
- **Fix:** correct the tests (expect 200, use a bearer token for `/api/v1/health`, or use `/api/v1/health/live`). Fix the Traefik and Caddy notes. Resolve the `ROOT_PATH` naming.

### N-07 🟡 `PAXS_FORWARDED_ALLOW_IPS="*"` is documented without its spoofing consequence (unverified)
- **Where:** `REVERSE-PROXY.md:27`, `run_paxs.py:75`, `.env.example:191` ("Accepts `*`").
- **What is wrong:** with `*`, uvicorn's proxy-headers handling is believed to take the left-most `X-Forwarded-For` entry. That entry is client-controlled when a proxy appends. The IP is then used by `require_metrics_access` (`metrics.py:59`) and by the rate limiter (`ratelimit.py:43`, `request.client`). I could not confirm this in source because uvicorn is not installed in the sandbox (`requirements.txt` pins `uvicorn[standard]>=0.30`).
- **Failure scenario (if confirmed):** `X-Forwarded-For: 127.0.0.1` on `/metrics` passes the allow-list. Rotating the header defeats the login limit.
- **Fix:** do not recommend `*`. Document an explicit proxy IP or subnet. Confirm uvicorn's behaviour with the pinned version and add a test.

### N-08 🟡 Behind a containerised proxy, all clients share one rate-limit bucket (login DoS)
- **Where:** `app/api/ratelimit.py:43-47` keys on `request.client.host`, which is the HAProxy IP unless it is trusted (`run_paxs.py:91`, `config.py:56`). `TRUSTED_PROXIES` (`config.py:232`) is not in `.env.example` or any deploy doc.
- **What is wrong:** every user in the LB stack is one key. Login is limited to 10/min and global to 300/min (`.env.example:131-132`).
- **Failure scenario:** one attacker exhausts the login bucket for the whole LB and locks out legitimate users.
- **Fix:** trust the HAProxy subnet (N-05), or document `TRUSTED_PROXIES` and set it in the LB compose.

### N-09 🟡 Portal: `PAXS_VENDOR_PRIVATE_KEY_FILE` in `config.local.php.example` is ignored
- **Where:** `deploy/vendor-portal-php/config.local.php.example:20` defines the constant `PAXS_VENDOR_PRIVATE_KEY_FILE`. `config.php:119` reads only the environment (`paxs_env`), and `config.php:124` checks only `PAXS_VENDOR_PRIVATE_KEY`.
- **What is wrong:** the template's "preferred" path is silently ignored.
- **Failure scenario:** the operator copies the template, places the PEM at `/etc/paxs/...`, and licence issuance fails with "Vendor signing key is not configured". Fail-closed, so no unsigned licences are issued.
- **Fix:** in `paxs_vendor_private_key()`, also check `defined('PAXS_VENDOR_PRIVATE_KEY_FILE')`.

### N-10 🟡 Portal: the default private-key location is inside the web root
- **Where:** `config.php:119` default `__DIR__ . '/vendor_private.pem'`. `README.md:89-93` says to store it outside the web root. The install steps (README:43-46) put the whole folder under `public_html`. No `.htaccess` ships (R8).
- **Failure scenario:** the operator follows the default path. On Apache a `.pem` under the docroot is served as a static file, which leaks the signing key.
- **Fix:** default to a directory outside the docroot (for example `dirname(__DIR__) . '/secure/vendor_private.pem'`), and ship a deny rule for `*.pem`, `*.db`, `config*.php`.

### N-11 🟡 `resolve_public_base_url` trusts `X-Forwarded-Host` and `X-Forwarded-Proto` from any peer
- **Where:** `app/core/config.py:570-572` reads `x-forwarded-host` and `host` directly from request headers, with no peer check. `x-forwarded-proto` is read the same way.
- **What is wrong:** when `PUBLIC_BASE_URL` is empty, share links, QR codes and FHIR absolute URLs take the origin from client headers. The Round 1 V2-2 table shows this by design.
- **Failure scenario:** an attacker who can reach the node sends `X-Forwarded-Host: evil.example` on a request that creates a share, or on a QR or FHIR request. Links point to the attacker's host. The attacker needs a token for most paths, but the header is also read by unauthenticated paths that build absolute URLs.
- **Fix:** honour `X-Forwarded-*` only when the peer is in `FORWARDED_ALLOW_IPS`, and otherwise use the server's own `PUBLIC_BASE_URL` or Host.

### N-12 🟡 `LDAP_USE_TLS` is declared but never used; LDAP bind is cleartext by default
- **Where:** `app/core/config.py:282` declares `LDAP_USE_TLS` ("startTLS on ldap://"). No `start_tls` call and no read of this flag anywhere in `app/` (only the declaration). `app/services/ldap_service.py:197-206` binds with the user's password, using `use_ssl` only when the URL starts with `ldaps://`. `.env.example:206` `LDAP_USE_TLS=false` and the comment imply the flag works.
- **Failure scenario:** with `LDAP_ENABLED=true` and `LDAP_SERVER=ldap://...`, the operator password is sent in cleartext. Setting `LDAP_USE_TLS=true` does nothing.
- **Fix:** call `conn.start_tls()` when the flag is set, or refuse a non-TLS bind unless explicitly allowed. Document that `ldaps://` is required.

### N-13 🟡 Test coverage for the reverse-proxy and metrics work is outside the suite
- **Where:** `.workbuddy-ai/tmp/proxy-prefix-test.py` and `proxy-integration-test.py` (cited in Round 2 lines 124-133, 353-356) are not under `tests/`, and `grep` finds no `X-Forwarded-Prefix` test in `tests/`. Round 2's text says 69+54 = 123 assertions; its table says 81+62 = 143 (lines 325-326).
- **Failure scenario:** a regression in `proxy_prefix.py` or `metrics.py` is not caught by pytest or CI. The N-03 and N-05 bugs are invisible to the suite.
- **Fix:** move these checks into `tests/`, add a case that uses `PAXS_FORWARDED_ALLOW_IPS` through the environment, and add an HAProxy-path metrics test.

### N-14 🟡 `.workbuddy-ai/` (AI-tool scratch directory) is committed with this change
- **Where:** about 60 files under `.workbuddy-ai/` (logs, XML reports, `memory/*.md`, `tmp/*` test scripts), committed as "A" in this range.
- **Checked:** a pattern scan for assignment-shaped secrets in `.workbuddy-ai/` and `.vscode/` found one hit, a fake token literal in `auth-guard-test.js:86`, which is not a real credential. The PEM header scan of the whole HEAD tree is clean.
- **Not checked:** `memory/*.md` session notes were not read in full. Logs may contain paths and user-specific context.
- **Fix:** remove `.workbuddy-ai/` from git and ignore it, or move reusable tests into `tests/`.

### N-15 🟢 `.env.example` and config drift
- `DEFAULT_COMPRESSION`: example `RLE` (`.env.example:36`), code default `JPEG-LS` (`config.py`).
- `WORKER_POLL_SECONDS`: example `5` (`.env.example:45`), code `3.0`.
- `.env.example:50` documents cache key `paxs:frame:{sop}:{idx}`. `dicomweb.py:818` uses a mode-qualified key. `wado_rs_bulk_service.py:227` still uses the bare key.
- Not documented in `.env.example`: `TRUSTED_PROXIES` (`config.py:232`), `ROOT_PATH` and `FORWARDED_ALLOW_IPS` (unprefixed, N-05), `AUTH_GATE_ENABLED` (`config.py:225`), `REFRESH_TOKEN_EXPIRE_DAYS` (`config.py:203`), `LICENSE_PORTAL_URL` (`config.py:495`, default `http://127.0.0.1:8080`, plain HTTP), `LICENSE_AUTO_VALIDATE_ONLINE`, `LICENSE_HEARTBEAT_HOURS`, `LICENSE_FILE_PATH`, `LICENSE_GRACE_PERIOD_DAYS`, `PAXS_HOST`/`PAXS_PORT` (`run_paxs.py:64-65`), `PAXS_INSTANCE_ID`.

### N-16 🟢 API docs and OpenAPI always enabled
- `app/main.py:543-544` sets `docs_url="/api/docs"` and `openapi_url="/api/openapi.json"` with no production switch. This discloses the full API surface to anonymous users.

### Positive changes (for balance)
- `run_all.bat` now refuses to start the server when imports or DB init fail (lines 41, 44), and no longer falls back to a global `python`.
- `run_paxs.py` fails closed when HTTPS is enabled with a missing or unreadable certificate in production (lines 47-58).
- Portal signing is fail-closed (`config.php:127-133`, `crypto.php:68`). The `.gitignore` covers `config.local.php` and `*.pem`.
- `/license/status` now requires a user and `/license/fingerprint` requires admin (`api/v1/license.py`).
- `ALLOW_REGISTER` default is false, and self-registration cannot mint ADMIN or RADIOLOGIST (`auth.py:196-212`).

---

## 4. Project docs summary and claim spot-checks

### 4a. Doc purpose and status

| Doc | Purpose | Verdict on checked claims |
|---|---|---|
| `README.md` (239, rewritten) | Front page: features, quick start, URL map, security notes. | Mixed. Several claims false (see 4b). |
| `PROJECT_STRUCTURE.md` (729, new, AR/EN) | Tree, module roles, middleware order. | Describes `storage_service.py` but not the missing `app/services/storage/` (N-01). Middleware order not verified. |
| `RUN_AND_BUILD_GUIDE.md` (818, new, AR) | Setup, `run_all.bat`, manual uvicorn, Docker, first-admin bootstrap. | Bootstrap claim true (`auth.py:191-193`). `:8404` stats false. `PAXS_FORWARDED_ALLOW_IPS` name wrong (N-05). Direct `uvicorn ... --reload` skips `proxy_headers` (line 270). |
| `audit_and_roadmap/01_CODEBASE_ARCHITECTURE.md` (281) | Architecture audit and module map. | Not verified beyond spot checks. Cites Python 3.14 host and a path `D:/Paxs Archive`. |
| `audit_and_roadmap/02_CRITICAL_BUGS_AND_ISSUES.md` (590) | Bug register C/H/M/L with per-item status. | C-5 "REMEDIATED" verified true (`fhir.py:68-73, 156, 219`). |
| `audit_and_roadmap/03_PERFORMANCE_AND_DICOM_AUDIT.md` (178) | Codec availability, performance, DICOM conformance. | Not verified. |
| `audit_and_roadmap/04_UPGRADE_AND_EXECUTION_PLAN.md` (272) | Phased plan with DoD. | Plan document; not verified. |
| `PAXS_VIEWER_DIAGNOSTIC_AUDIT.md` (1066) | Viewer audit, vectors V1-V3, status table. | V1-5, V2-1, V3-1 true. V2-4 partial (N-05). |
| `PAXS_VIEWER_REMEDIATION_ROUND1.md` (349) | Fixes V1-1, V1-5, V2-2, V3-1, V3-2 with counts. | V2-2 true (`config.py:502-523`). |
| `PAXS_VIEWER_REMEDIATION_ROUND2.md` (381) | Reverse-proxy prefix middleware and frontend fixes. | Middleware trust gating true. Config-name claim false (N-05). Counts inconsistent (N-13). |
| `PAXS_VIEWER_REMEDIATION_ROUND3.md` (338) | Memory governance, observability, Prometheus fix (§4b). | §4b "fixed" false for the LB path (N-03). Prior-state description does not match e90d32c. |
| `plan_9_2026.md` (422) + `outputs/` copy | Review, fix plan, C01-C10 register, done-vs-open table. | C01 "fails closed" true. §3 "Storage: Implemented" false (N-01). |
| `plan_viewer_process.md` (899) | Hybrid viewer architecture, "Pending Implementation". | Consistent: `VIEWER_PROCESSING_DISTRIBUTION` absent from config. |
| `deploy/MONITORING.md` | Prometheus/Grafana, decode-cache metrics, alerts. | Metric names verified. "/metrics is not public" false (N-03). Transfer-syntax log example matches `dicom_diagnostics.py:114`. |
| `deploy/REVERSE-PROXY.md` (230, new) | Reverse-proxy guide. | Wrong tests and env names (N-05, N-06). |

### 4b. Spot-checks: 10 "fixed / implemented" claims

| # | Doc and claim | Code evidence | Verdict |
|---|---|---|---|
| 1 | MONITORING.md Notes and Round 3 §4b: `/metrics` access "is now granted by source address", and "both are fixed" | `metrics.py:59`, `config.py:359-361`, `haproxy.cfg:37-53`, `compose.lb:16` | **FALSE** for the shipped LB topology (N-03). True for direct RFC1918 peers. |
| 2 | Round 1 V2-2: unroutable `PUBLIC_BASE_URL` is downgraded to "not configured" | `config.py:502-523` validator returns `""` for `0.0.0.0`, `::`, empty | **TRUE** (caveat: N-11) |
| 3 | Round 2 §1 "Applied — config & runner": `ROOT_PATH`/`FORWARDED_ALLOW_IPS` configurable through `PAXS_*` | `config.py:51,56,500` (no alias). `run_paxs.py:78-79` uses `PAXS_*` for uvicorn only | **FALSE** for the app layer (N-05) |
| 4 | Round 2 §1: trusted-peer gating for `X-Forwarded-*` (`*`, glob, CIDR, untrusted ignored) | `proxy_prefix.py:77-108` (`TrustedHosts`), `:126-134` gate | **TRUE** in code. Not in CI (N-13). |
| 5 | DIAG V1-5: SPA fallback no longer masks missing assets | `main.py:718-738`: asset extensions re-raise 404 | **TRUE** |
| 6 | DIAG V2-4: root_path and `X-Forwarded-Prefix` support, loopback-only proxy trust, marked Fixed | Middleware and prefix logic present (`proxy_prefix.py`, `main.py:575-578`). Documented config names do not reach the app | **PARTIAL** |
| 7 | DIAG V3-1: volume materialisation race fixed (in-flight coalescing) | `volume_service.py:762-773` (`_IN_FLIGHT_VOLUMES`, lock, wait bounds) | **TRUE** (tests not run) |
| 8 | Audit 02 C-5: FHIR `/Patient` tenant scope "REMEDIATED" | `fhir.py:68-73` predicate, used at `:156` and `:219` | **TRUE** |
| 9 | README:225: `/studies-ui`, `/viewer`, **`/admin`**, `/settings` are behind the session-cookie gate | `main.py:364-368`: gated list is `/`, `/viewer`, `/studies-ui`, `/settings` only. `/admin` mount at `main.py:842-851` has no gate | **FALSE** for `/admin` |
| 10 | README:231 and `.env.example:163-164`: a bind address "is rejected at startup" | `config.py:502-523` downgrades to `""`. It does not raise | **FALSE** |
| (extra) | plan_9 §3: "Local and S3 storage: Implemented" | Package missing (N-01); `api/v1/storage.py:34` | **FALSE** |

### 4c. Additional claims checked

| Doc and claim | Code evidence | Verdict |
|---|---|---|
| README:114, :124: Redis optional, standalone without it | `redis_service.py:26`, `requirements.txt:34` (N-02) | **FALSE** |
| README:177: `DICOMWEB_PUBLIC=false` in example and enforced | `.env.example:158`, `config.py:427`, `deps.py:251`, `dicomweb.py:201` | **TRUE** |
| README:188 and `.env.example:123-127`: self-registration cannot create ADMIN or RADIOLOGIST | `auth.py:196-212`, `config.py:207,210` | **TRUE** |
| README:181: "The vendor portal never stores a signing key in source" | HEAD `config.php` has no key | **TRUE for HEAD tree, FALSE for history** (S-02) |
| `crypto.php` and plan C01: signing fails closed without a configured key | `crypto.php:68`, `config.php:127-133` | **TRUE** (except N-09 for the template constant) |
| README:114: startup order (DB schema, worker, DIMSE, HL7) | `main.py:497` `init_db()`, then worker, receiver, HL7 | **TRUE** |
| README:23: "REST Routers 47+" | 48 modules with `APIRouter(` in `app/api/v1/` | **TRUE** |
| README:25: "Pytest Suites 77" | 101 `tests/test_*.py` plus 9 `tests/js/*.test.js` | **FALSE** (stale) |
| Round 2: "PAXS has no WebSocket endpoints" | no websocket route decorators in `app/` | **TRUE** |
| Round 2 §1: "123 assertions across two suites" | text 69+54; table 81+62 (`ROUND2:325-326`) | **PARTIAL** (inconsistent, scripts outside `tests/`) |
| Round 3 §4b: prior `/metrics` "required authentication (`get_current_user_any`)" | `e90d32c` `metrics.py:88`: no dependency | **FALSE** for the committed prior state |
| RUN guide §3.4: first admin via `/api/v1/auth/register` | `auth.py:191-193` bootstrap bypasses the flag | **TRUE** (see R4 partial) |
| RUN guide:473: HAProxy stats on `:8404` | no 8404 frontend in `haproxy.cfg` | **FALSE** |
| `.env.example:206` `LDAP_USE_TLS` | declared at `config.py:282`, unused | **FALSE** (N-12) |
| audit/ldap C02: no development fallback | `ldap_service.py:14-18` | **TRUE** |
| MONITORING.md: "transfer_syntax=" log line exists | `dicom_diagnostics.py:114` | **TRUE** (example line is illustrative) |

---

## 5. Documentation deltas

### 5a. New deployment option: reverse proxy and sub-path (`deploy/REVERSE-PROXY.md`)
- Shapes: root-mounted (default, no config) and sub-path (needs prefix plus trust settings).
- Intended env names are `PAXS_ROOT_PATH` and `PAXS_FORWARDED_ALLOW_IPS`. The app reads `ROOT_PATH` and `FORWARDED_ALLOW_IPS` (N-05). Doc must be corrected or code aliased.
- The shipped `haproxy.cfg` does not set `X-Forwarded-Host` or `X-Forwarded-Prefix`. The doc says to add them (§7), so sub-path through the shipped LB needs edits.
- Behind any proxy, `/metrics` is IP-gated by the proxy-visible peer. See N-03 before publishing the LB.

### 5b. New or changed environment variables in `.env.example` (name, code default, example value, meaning)

| Variable | Code default | Example | Meaning |
|---|---|---|---|
| `DICOM_TLS_ENABLED` | `false` | `false` | TLS for DIMSE. Requires cert and key. |
| `DICOM_TLS_CERT` / `DICOM_TLS_KEY` | `./certs/paxs.crt` / `.key` | same | Certificate and key paths for DICOM TLS. |
| `DICOM_TLS_CA` | `""` | empty | Set to require client certs (mTLS). |
| `STORAGE_ALLOW_LEGACY_PATHS` | `false` | `false` | Migration-only switch for absolute or CWD-relative storage keys. |
| `LOSSY_COMPRESSION_ENABLED` | `false` | `false` | Gate for destructive lossy re-encoding. |
| `REDIS_CACHE_ENABLED` | `true` | `true` | Use Redis as the L2 cache. Falls back to L1 if unreachable. |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | same | Redis URL. No auth in the example. |
| `VOLUME_CACHE_BUDGET_MB` | `1024` | `1024` | Byte budget for decoded HU volumes. |
| `DICOM_FRAME_CACHE_MB` | `256` | `256` | Byte budget for decoded frames (WADO bulk). |
| `METRICS_ALLOW_IPS` | loopback + RFC1918 | same | Peers allowed to scrape `/metrics` without auth. Unsafe through a proxy (N-03). |
| `ENVIRONMENT` | `development` | `development` | Production gate. Any value not in the dev set is production. Unset means dev. |
| `DEBUG` | unset | commented `false` | Pinning `DEBUG=false` also enables the production gate. |
| `AUTH_COOKIE_SECURE` / `AUTH_COOKIE_SAMESITE` | `false` / `lax` | same | Session cookie policy. Production requires Secure. |
| `HTTPS_ENABLED`, `HTTPS_CERT`, `HTTPS_KEY` | `false`, `./certs/https.*` | same | Native TLS for HTTP. Production refuses to start without usable cert. |
| `REGISTER_ALLOWED_ROLES` | `PHYSICIAN` | same | Roles self-registration may request. ADMIN and RADIOLOGIST always refused. |
| `PAXS_ROOT_PATH` | not read by app (`ROOT_PATH` = `""`) | empty | Sub-path mount (N-05). |
| `PAXS_FORWARDED_ALLOW_IPS` | not read by app (`FORWARDED_ALLOW_IPS` = `127.0.0.1`) | `127.0.0.1` | Trusted proxy peers (uvicorn only, N-05). |
| `LDAP_ENABLED` / `LDAP_SERVER` / `LDAP_BASE_DN` | `false` / `""` / `""` | same | LDAP login. Requires `ldap3`. |
| `LDAP_USER_DN_TEMPLATE` | `uid={username},{base_dn}` | same | Bind DN template. |
| `LDAP_GROUP_ROLE_MAP` / `LDAP_DEFAULT_ROLE` | `""` / `""` | empty | Group to role map. Empty default role means deny. |
| `LDAP_TIMEOUT_SECONDS` | `5` | `5` | Connect and receive timeout. |
| `LDAP_USE_TLS` | `false` | `false` | Declared, but unused (N-12). |
| `HL7_ENABLED`, `HL7_HOST`, `HL7_PORT` | `false`, `0.0.0.0`, `2575` | same | HL7 ADT listener. |
| `FHIR_ENABLED` | `true` | `true` | FHIR gateway. |
| `TRIAGE_*` | enabled | same | Worker-side detectors. |

Changed values (old to new): `ALLOW_REGISTER` true to false; `DICOMWEB_PUBLIC` true to false; `PUBLIC_BASE_URL` `http://0.0.0.0` to empty.

Missing from `.env.example` but read by config: `TRUSTED_PROXIES`, `AUTH_GATE_ENABLED`, `REFRESH_TOKEN_EXPIRE_DAYS`, `LICENSE_*` (`config.py:494-498`), `PAXS_HOST`/`PAXS_PORT`, `PAXS_INSTANCE_ID`.

### 5c. Changed run instructions
- `run_all.bat`: now requires `venv\Scripts\python.exe`. No global `python`. Pre-flight `import app.main` and `init_db()` run in the launcher window. The server opens in `cmd /k`. Printed URLs use `:8000`. This pre-flight will fail on this tree (N-01, N-02).
- `health_monitor.bat`: printed URLs `:8000`. The probe still targets `http://0.0.0.0/health` (S-6).
- `run_paxs.py`: the TLS and reload gates depend on `ENVIRONMENT`/`DEBUG` (N-04). `PAXS_FORWARDED_ALLOW_IPS` and `PAXS_ROOT_PATH` now apply to uvicorn only.
- Portal: copy `config.local.php.example` to `config.local.php`. Note that its `PAXS_VENDOR_PRIVATE_KEY_FILE` line is ignored (N-09).
- Docker and compose: no change. They still run with development defaults (N-03, R2).

---

## 6. Open questions

1. **Storage package (N-01):** does a copy of `app/services/storage/` exist on the developer machine outside this checkout? If not, nothing starts from this tree, and `plan_9` §3 is wrong.
2. **Key exposure (S-02):** is the GitHub remote public, and were `8af1381` or `e90d32c` ever pushed? Were any licences issued with the old key? The portal `licenses` table would show this.
3. **LB deployment (N-03, R2):** is `docker-compose.lb.yml` deployed anywhere reachable? If yes, `/metrics` is open and default-key tokens are forgeable.
4. **uvicorn behaviour (N-07):** with the pinned `uvicorn[standard]>=0.30`, does `forwarded_allow_ips="*"` take the left-most or right-most `X-Forwarded-For`? Needs the package installed.
5. **Round 2 uvicorn claim:** does "uvicorn 0.52 ignores `X-Forwarded-Prefix`" hold for the version the team actually installs?
6. **Bootstrap window (R4):** is the first-caller-becomes-ADMIN window acceptable for new installs, or should bootstrap require a one-time token?
7. **`.workbuddy-ai/memory/*.md`:** are these session notes intended to be in the repository? I did not read them in full.
8. **Default compression (N-15):** which is intended, RLE (`.env.example`) or JPEG-LS (code)?
9. **Portal admin default:** is the live portal still using the documented default credentials?
