# Deployment, operations scripts, monitoring stack and the vendor licensing portal

> **Area deep-dive** — part of the [PAXS Archive documentation](../README.md). Produced by static analysis of commit `e90d32c` (2026-10-08); line numbers refer to that commit. Findings here are consolidated, de-duplicated and prioritized in [CODE_REVIEW.md](../CODE_REVIEW.md) — where the two differ, CODE_REVIEW.md takes precedence. Labels such as `[read]`, `[verified]`, `[V]`, `[H]` or `[inferred]` describe how each claim was checked (read in code vs. inferred).

> ⚠️ **Baseline: this deep-dive describes commit `e90d32c`.** What changed in `5a32aa4` (fixes, regressions, new code and endpoints) is in [5a32aa4/07-deploy-licensing-docs.md](5a32aa4/07-deploy-licensing-docs.md). The current status of every finding is in [CODE_REVIEW.md](../CODE_REVIEW.md).

Severity legend: 🔴 blocking (security or deploy-breaking), 🟡 important, 🟢 nit.

---

## 0. Scope and what was actually verified

Every in-scope file was read in full with the Read tool. Deploy docs were read in full.

Verification beyond reading (all read-only; nothing was built, deployed, or networked):

| Check | Result |
|---|---|
| `git log --all -p -S "BEGIN PRIVATE KEY"` | Private key first committed in `8af1381`; still tracked in `deploy/vendor-portal-php/config.php` |
| Key-pair check on the portal's embedded RSA-2048 private/public PEMs (openssl, via stdin, key never printed) | Pair is consistent; SHA-256 of DER public key = `a999b9de…18bb` |
| Portal public key vs `app/core/licensing/crypto.py:29-37` DEFAULT_PUBLIC_KEY_PEM | Identical DER (same `a999b9de…18bb`) |
| `php -l` on all portal PHP files and `scripts/reset_admin_password.php` (PHP 8.2.12 from xampp) | No syntax errors |
| bcrypt check: `password_verify("Admin1234!", <hash in config.php:35>)` (PHP 8.2) | **FALSE**. Harness self-test (hash of a known string) returns TRUE, so the check itself works. The documented default password does not match the seeded hash. |
| PowerShell `""""` literal (`pwsh -Command`) | Evaluates to a single `"` character |
| Canonical JSON bytes, PHP `LicenseCrypto::getCanonicalJson` vs Python `get_canonical_payload_bytes` for the same dict with a non-ASCII customer name | **Byte-different** (PHP raw UTF-8, Python `\uXXXX` escapes); SHA-256 prefixes `32424b87…` vs `9b99fba0…` |
| Existence checks: `app/services/storage/`, `paxs.spec`, `build-win/`, `.tools/`, `.dockerignore`, `.htaccess` in the portal folder, `scripts/vendor_keys/`, `../portfolio` sibling | All absent. `git log --all -- app/services/storage` is empty (never committed). `.gitignore:9` (`storage/`) matches that path. |
| `git ls-files` / `git check-ignore` | `paxs.spec` ignored by `.gitignore:15` (`*.spec`); `app/services/storage/backend.py` ignored by `.gitignore:9` |
| Route prefixes (`metrics.py:31` no prefix; `dashboard.py:62` prefix `/api/v1`) | Prometheus `/metrics` and HAProxy `/api/v1/health/ready` paths are correct |
| Grafana metric names used by the dashboard | All exist in `app/api/v1/metrics.py` (`paxs_worker_queue_depth`, `paxs_replica_lag_seconds`, `paxs_replica_connected`, `paxs_replica_configured`, `paxs_db_up`) |

Not verified (stated where relevant): Docker image build/run, Grafana rendering, running the portal under a web server, running PAXS, actual JWT forgery, the PowerShell gateway launcher end-to-end, uvicorn HTTP/2 support (uvicorn not installed locally).

Context files read outside scope (targeted, not full reviews): `alembic.ini`, `requirements.txt`, `settings.example.json`, `app/core/licensing/crypto.py`, `app/core/licensing/manager.py` (lines 224-347), `app/core/config.py` (greps), `app/core/security.py` (141-153), `app/api/v1/deps.py` (40-85), `app/api/v1/auth.py` (165-200), `app/api/v1/dicomweb.py` (greps and 800-830), `app/api/ratelimit.py` (25-40), `app/api/v1/metrics.py` (80-92), `app/api/v1/dashboard.py` (122-150), `app/main.py` (268-300, 385-412), `app/services/settings_service.py` (50-66, 390-470), `app/services/storage_service.py` (imports), `app/services/redis_service.py` (20-30), `app/services/worker.py` (CLI, 489-499).

---

## 1. Files covered

| Path | Lines | Purpose |
|---|---|---|
| deploy/Dockerfile | 33 | Single stateless PAXS node image (python:3.12-slim) |
| deploy/docker-compose.lb.yml | 102 | 3 PAXS nodes + HAProxy + MySQL 8 + MinIO |
| deploy/docker-compose.monitoring.yml | 43 | Prometheus + Grafana |
| deploy/haproxy.cfg | 88 | HTTP/DIMSE/HL7 load balancing + metrics frontend |
| deploy/prometheus.yml | 28 | Scrape jobs `paxs-haproxy`, `paxs-nodes` |
| deploy/grafana/provisioning/datasources/prometheus.yml | 9 | Prometheus datasource (no `uid`) |
| deploy/grafana/provisioning/dashboards/paxs.yml | 11 | File provider for dashboards |
| deploy/grafana/dashboards/paxs-overview.json | 289 | "PAXS Archive Overview" dashboard (6 panels, 3 rows) |
| deploy/paxsviewer-app-config.js | 58 | PaxsViewer (OHIF fork) data-source config |
| deploy/CONTROL-PANEL-INTEGRATION.md | 109 | `/admin` control panel web bridge |
| deploy/LOAD-BALANCING.md | 55 | Statelessness rules, HAProxy layout |
| deploy/MONITORING.md | 114 | Metrics, dashboard, alert rules |
| deploy/PAXSVIEWER-INTEGRATION.md | 202 | Viewer at `/viewer`, MPR/MIP endpoints, deploy steps |
| deploy/PERFORMANCE.md | 147 | Caching/gzip/warm-on-open notes |
| deploy/SCALE-OUT-GUIDE.md | 341 | Standalone workers, replica, Redis, systemd |
| deploy/vendor-portal-php/README.md | 87 | Portal setup and deployment |
| deploy/vendor-portal-php/config.php | 130 | Portal config, default admin hash, AES root key, RSA private+public PEM |
| deploy/vendor-portal-php/db.php | 183 | PDO layer, SQLite/MySQL schema, admin seeding |
| deploy/vendor-portal-php/crypto.php | 140 | Canonical JSON, RSA signing, AES-GCM container, key/ID generation |
| deploy/vendor-portal-php/auth.php | 61 | Session auth, CSRF helpers, login/logout |
| deploy/vendor-portal-php/index.php | 913 | Admin dashboard and all license/key actions |
| deploy/vendor-portal-php/login.php | 208 | Login page (shows default credentials) |
| deploy/vendor-portal-php/logout.php | 12 | GET logout |
| deploy/vendor-portal-php/schema.sql | 71 | MySQL/SQLite schema (duplicates db.php) |
| deploy/vendor-portal-php/api/activate.php | 196 | Public activation API |
| deploy/vendor-portal-php/api/validate.php | 94 | Public heartbeat/validation API |
| scripts/00_demo.py | 31 | Event-script demo (logs only) |
| scripts/01_critical_alert.py | 28 | Event-script: outbound webhook on CriticalResultFlagged (blank URL) |
| scripts/README.md | 48 | Event-script contract |
| scripts/build_exe.bat | 31 | PyInstaller build (requires `paxs.spec`, missing) |
| scripts/generate_license.py | 247 | Vendor CLI: init-keys, fingerprint, create, verify |
| scripts/install_service.bat | 45 | NSSM Windows service install |
| scripts/reset_admin_password.php | 29 | Resets admin in a sibling `portfolio` DB (hardcoded password) |
| scripts/run_remote_migration.py | 67 | Solves a host's JS challenge, calls a remote `install.php` |
| scripts/sync_agents.py | 48 | Regenerates agent `.md` files from the gateway |
| scripts/unified_gateway_server.py | 444 | Local FastAPI Gemini-to-OpenAI gateway on 127.0.0.1:8899 |
| scripts/use-gateway.ps1 | 60 | Sets persistent Gemini env vars and launches `agy` |
| run_paxs.py | 51 | Server entry point (uvicorn, reload=True) |
| run_all.bat | 73 | Starts Redis (optional), PAXS, Vite dev server, health monitor |
| build_standalone.bat | 120 | Builds `dist\PAXS-Server` (requires `paxs.spec`) |
| health_monitor.bat | 185 | Port status, restart/stop menu |
| run-all-providers.bat | 2 | Wrapper for the .ps1 |
| run-all-providers.ps1 | 68 | Starts gateway and launches `agy` |
| .gitignore | 55 | Ignore rules (secrets, build, `storage/`, `*.spec`) |
| .env.example | 74 | Config template (insecure defaults) |

---

## 2. Deployment topologies

### 2.1 Single-node Windows

- **Entry:** `run_paxs.py:37-51` binds `PAXS_HOST` (default `0.0.0.0`) and `PAXS_PORT` (default `8000`), loads `.env` via dotenv, and enables TLS only if `HTTPS_ENABLED` and cert/key exist. It calls `uvicorn.run("app.main:app", ..., reload=True)` (line 49). Running with `reload=True` in the production entrypoint is a 🟡 issue (see D8).
- **`run_all.bat`:** Starts Redis if `redis-server` is on PATH or in `.tools\redis` (lines 26-42). Starts `run_paxs.py` in a new window (line 47). Starts the Vite dev server on `study-list-ui` if present (line 54). Prints URLs without a port (lines 62-64). Launches `health_monitor.bat` after 3 seconds (line 71).
- **Service install:** `scripts/install_service.bat` creates NSSM service `PAXSServer` for `dist\PAXS-Server\PAXS-Server.exe`, sets AppDirectory and auto-start (lines 23-29). It does not set `ObjectName`, so NSSM's default account applies (typically LocalSystem; not verified here). It prints "Dashboard: http://0.0.0.0" (line 36) and opens no firewall rule. 🟡: `build_standalone.bat:86` copies this script into `dist\PAXS-Server\`, where `set "APP_DIR=%~dp0..\dist\PAXS-Server"` (line 13) resolves to `dist\dist\PAXS-Server`, so the copied installer cannot find the exe.
- **Standalone build:** `build_standalone.bat` builds the Vite UI into `app\static\studies-ui`, runs PyInstaller with `paxs.spec` (line 64), creates `storage/backups/certs`, and writes a `start_paxs.bat`. `paxs.spec` does not exist in the repo and `.gitignore:15` (`*.spec`) excludes it, so the build fails on a clean checkout. `scripts/build_exe.bat:14` has the same dependency. 🟡.
- **Health monitor:** `health_monitor.bat` checks ports 8000, 10400, 6379, 5174 (lines 24-61). The probe at lines 96-97 requests `http://0.0.0.0/health`, which is port 80 and an app route that does not exist (see S6). Its menu also restarts processes via `taskkill /IM` (lines 109-112).

### 2.2 Docker image (`deploy/Dockerfile`)

- Base `python:3.12-slim` (line 3), tag not digest-pinned. Installs `build-essential` via apt-get (lines 8-10) and does not remove it, so it stays in the final image.
- `pip install -r requirements.txt` (line 13). `requirements.txt` includes `pytest` and `httpx` (dev) (lines 35-36); `redis`, `boto3`, and `celery` are commented out (lines 30-32).
- Copies `app`, `migrations`, `alembic.ini`, `run_paxs.py` (lines 15-18). Does not copy `scripts/`.
- ENV (lines 21-26): `PAXS_INSTANCE_ID=node-1`, `DATABASE_URL=mysql+aiomysql://paxs_user:paxs@mysql:3306/paxs_db` (line 22, hardcoded password), `STORAGE_BACKEND=s3`, `S3_ENDPOINT_URL=http://minio:9000`, `DICOM_RECEIVER_ENABLED=true`, `WORKER_ENABLED=true`. `SECRET_KEY` is not set, so it falls back to the public default in `app/core/config.py:136` (see R2).
- `EXPOSE 8000 10400` (line 28). HL7 port 2575 is not exposed by the image, but the compose file sets `HL7_ENABLED=true` on the nodes.
- `HEALTHCHECK` every 15 s calls `/api/v1/health/ready` via urllib (lines 30-31). Correct path.
- `CMD ["python", "run_paxs.py"]` (line 33). **No `USER` directive, so the container runs as root** (D1).
- No `.dockerignore` exists, so the build context (`context: ..`) is the entire repo, including `.env`, `settings.json`, and `deploy/vendor-portal-php/config.php` (contents not copied into the image, but sent to the daemon).

### 2.3 Load-balanced compose (`deploy/docker-compose.lb.yml`)

| Service | Image / build | Published ports (host) | Exposed (internal only) | Volumes | Healthcheck | Notes |
|---|---|---|---|---|---|---|
| haproxy | `haproxy:3.0-alpine` | 8000, 10400, 2575, **8404**, 8405 (lines 16-20) | none | `./haproxy.cfg` ro | `wget http://127.0.0.1:8404/;csv` (lines 25-29) | **8404 is not bound by haproxy.cfg** (D3). depends_on the 3 nodes. |
| paxs-node-1/2/3 | build `..` with `deploy/Dockerfile` (lines 31-34, YAML anchor) | none | 8000, 10400, 2575 (lines 52-55) | none | image HEALTHCHECK | Shared env anchor lines 36-46 with S3 `minioadmin/minioadmin` (41-42). `PAXS_INSTANCE_ID` per node. |
| mysql | `mysql:8.0` | none | 3306 | `mysql-data` | `mysqladmin ping -ppaxs` (lines 81-85) | `MYSQL_ROOT_PASSWORD: root` (75), user `paxs_user/paxs` (77-78) |
| minio | `minio/minio:latest` | **9000 (S3), 9001 (console)** (lines 96-98) | none | `minio-data` | none | `minioadmin/minioadmin` (92-93). Public to host. |

No Redis service (D6). No read replica (SCALE-OUT-GUIDE documents one). No `restart:` policies. Node env `DICOM_RECEIVER_ENABLED=true`, `WORKER_ENABLED=true`, `HL7_ENABLED=true` (lines 44-46).

### 2.4 HAProxy routing (`deploy/haproxy.cfg`)

| Frontend | Bind | Mode | Backend | Behavior |
|---|---|---|---|---|
| `metrics_in` | `*:8405` (line 30) | http | none | `stats enable`, `stats uri /metrics`, `stats prometheus` (lines 32-34). **No auth.** |
| `http_in` | `*:8000` (line 38) | http | `paxs_api` | `option forwardfor`, `X-Forwarded-Proto http` if not SSL (lines 40-42). All paths, including `/metrics`, `/dicom-web`, `/admin`, `/viewer`. |
| `dimse_in` | `*:10400` | tcp | `paxs_dimse` | Timeouts client/server 900 s (lines 66-68). TCP `check` per node. |
| `hl7_in` | `*:2575` | tcp | `paxs_hl7` | Timeouts 120 s. TCP `check` per node. |

- `paxs_api` (lines 45-53): `balance roundrobin`; `option httpchk GET /api/v1/health/ready`; `http-check expect status 200`; `check inter 10s fall 3 rise 2` per node.
- **No sticky sessions.** Acceptable because auth is HS256 JWT (`deps.py`), but the viewer's refresh-token cookie is DB-backed (`app/db/models.py:136`), so this holds only if every node shares MySQL.
- **No `stats` frontend on 8404** (contradicts `LOAD-BALANCING.md:35` and the compose healthcheck).
- No TLS on any frontend (plaintext PHI in transit inside the LB topology).

### 2.5 Monitoring stack

- Prometheus (`docker-compose.monitoring.yml:15-25`, `prom/prometheus:latest`, port 9090 published, 30 d retention). Scrape config (`prometheus.yml`): job `paxs-haproxy` -> `haproxy:8405` (lines 17-21); job `paxs-nodes` -> `paxs-node-1..3:8000`, path `/metrics`, 15 s interval (lines 23-28).
- Grafana (lines 27-39, `grafana/grafana:latest`, port 3000 published, `GF_SECURITY_ADMIN_USER/PASSWORD = admin/admin`, lines 37-38; sign-up disabled line 39). Provisioned from `grafana/provisioning` and `grafana/dashboards` (lines 30-33).
- Datasource: `Prometheus`, `url: http://prometheus:9090`, `isDefault: true`, **no `uid`** (datasources/prometheus.yml:4-9).

**Dashboard "PAXS Archive Overview"** (`uid: paxs-overview`, `refresh: 15s`, default range now-1h). Every panel's datasource is `{"type":"prometheus","uid":"prometheus"}` (JSON lines 30, 62, 107, 142, 193, 227).

| Row / panel (JSON line) | Type | Query(ies) |
|---|---|---|
| Row "Per-node HAProxy traffic" (21) | row | - |
| "HTTP responses/s per node" (29) | timeseries | `sum by (server) (rate(haproxy_server_http_responses_total{server=~"paxs-node-.*"}[5m]))` (line 53), legend `{{server}}` |
| "Total HTTP rate (frontend)" (61) | timeseries | `rate(haproxy_frontend_http_requests_total{frontend="http_in"}[5m])` (85); `rate(haproxy_frontend_http_requests_total{frontend="metrics_in"}[5m])` (90) |
| Row "Worker queue depth" (98) | row | - |
| "Compression jobs by state" (106) | timeseries | `sum by (state) (paxs_worker_queue_depth)` (133) |
| "Queue now (pending / running)" (141) | stat | `sum(paxs_worker_queue_depth{state="pending"})` (171); `sum(paxs_worker_queue_depth{state="running"})` (176) |
| Row "Read replica" (184) | row | - |
| "Replica lag (seconds)" (192) | timeseries | `max by (instance) (paxs_replica_lag_seconds)` (218) |
| "Replica & DB status" (226) | stat | `max(paxs_replica_connected)` (256); `max(paxs_replica_configured)` (261); `min(paxs_db_up)` (266) |

All metric names exist in `app/api/v1/metrics.py`. Probable issue: the panels reference datasource uid `prometheus`, but the provisioned datasource has no `uid`, so Grafana generates a random one. Panels will likely show "datasource not found" (D5; not rendered here).

### 2.6 Vendor licensing portal topology (public web host)

- Plain PHP 7.4+/8.x app. README (lines 19-29) suggests `php -S localhost:8080`. The production path is cPanel/shared hosting under `public_html/paxs-portal/` (lines 41-47), with SQLite at `deploy/vendor-portal-php/portal.db` (`config.php:23` uses `__DIR__ . '/portal.db'`). No `.htaccess` is shipped (R8).
- PAXS server points at it via `settings.LICENSE_PORTAL_URL` (`app/core/config.py:316`, default `http://127.0.0.1:8080`).

---

## 3. Licensing system, end to end

### 3.1 Components and keys

| Artifact | Where | Notes |
|---|---|---|
| Vendor RSA-2048 private key | `deploy/vendor-portal-php/config.php:43-72` (`PAXS_VENDOR_PRIVATE_KEY`); also `scripts/vendor_keys/paxs_vendor_private.pem` if generated (gitignored, `.gitignore:31`) | **Committed in `8af1381`, still tracked (R1).** |
| Vendor RSA-2048 public key | `config.php:75-85`; `app/core/licensing/crypto.py:29-37` (`DEFAULT_PUBLIC_KEY_PEM`) | Same key pair (DER SHA-256 `a999b9de…18bb` in both). |
| AES-256-GCM container key | `config.php:40`: `hash('sha256','PAXS_ARCHIVE_LIC_SEC_2026_AES25', true)`; `crypto.py:39-42` identical | **Public constant** (R15). |
| AAD | `b"PAXS_LIC_v1"` in `crypto.php:101` and `crypto.py:213,241` | Consistent. |
| Container format | `"PAXS" + IV(12) + ciphertext + tag(16)` (`crypto.php:110`; `crypto.py:214-215,240`) | Consistent. |
| Signature | Portal: RSA-SHA256 PKCS#1 v1.5 via `openssl_sign` (`crypto.php:67`). Python sign: PSS (`crypto.py:127-146`); verify tries PSS then PKCS1v15 (`crypto.py:148-197`). | Interop with PKCS1v15 is provided by the verifier fallback. |
| Canonical JSON for signing | Python: `json.dumps(raw_dict, sort_keys=True, separators=(",",":"))` (`crypto.py:103`, `ensure_ascii` default True). PHP: `json_encode(..., JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE)` (`crypto.php:56`). | **Not byte-identical for non-ASCII values (R9).** |
| Edition/feature table | `config.php:88-130` (portal) vs `generate_license.py:107-112` (CLI) vs `crypto.py:55-68` (`LicenseData` defaults) | Three inconsistent feature sets (S10). |

### 3.2 Who issues licenses

1. **CLI:** `scripts/generate_license.py` (`init-keys [--force]` writes `scripts/vendor_keys/*.pem`, `create`, `verify`, `fingerprint`). Uses the app's own `app/core/licensing/crypto.py` (PSS signing, `encrypt_license_container`).
2. **Portal, manual:** `index.php` action `create_license` (lines 27-86): form with customer name/email/org, edition (not whitelisted, line 31), fingerprint (default `*`), validity days, perpetual flag, seats, studies cap, features.
3. **Portal, online:** `api/activate.php` (see 3.4).

### 3.3 Activation API contract (`api/activate.php`, called by `app/core/licensing/manager.py:226-288`)

Request (POST, JSON), built at `manager.py:248-255`:

```
{ "action": "activate", "key": "<PAXS-XXXX-XXXX-XXXX-XXXX>", "email": "<optional>",
  "fingerprint": "<host fingerprint, uppercased>", "hardware_info": {...}, "app_version": "..." }
```

Portal reads `key`, `fingerprint`, `email`, `hardware_info`, `app_version` (`activate.php:27-42`); ignores `action`. Key matched case-insensitively (`UPPER(product_key)=?`, line 47).

Flow in `activate.php`: key lookup (47-58) -> quota check (61-68, not atomic, R10) -> email restriction (71-80) -> payload with `expires_days` and edition features (86-113) -> encrypt + sign (116-126) -> transaction: increment `activations_count`, insert `licenses` row (status ACTIVE, `container_b64`), insert `activations_log` (129-185) -> response (188-196).

Responses:
- Success: `{"success":true,"message":"Successfully activated ...","license_id":"PAXS-<EDITION>-<YEAR>-<8HEX>","edition":"HOSPITAL","license_b64":"<base64 container>","expires_at":"...Z","features":[...]}`
- Failure: HTTP 400 (missing key/fingerprint), 404 (invalid key), 403 (quota or email restriction), 500 (crypto/DB) with `{"success":false,"message":"..."}`.

Client (`manager.py:276-288`): checks `success`, reads `license_b64`, then `activate_from_bytes` verifies signature and expiry. Client does **not** enforce `https://` on the portal URL (`manager.py:238-244`).

### 3.4 Heartbeat API contract (`api/validate.php`, called by `manager.py:290-345`)

Request (POST, JSON), built at `manager.py:309-314`: `{"action":"validate","license_id":"...","fingerprint":"...","app_version":"..."}`.

Portal responses (`validate.php`):
- Unknown `license_id`: `{"success":true,"status":"UNTRACKED","message":"..."}` (lines 48-55).
- Revoked: `{"success":false,"revoked":true,"reason":"..."}` (lines 70-76).
- Active: `{"success":true,"status":"ACTIVE","edition":"...","expires_at":"...","is_perpetual":bool,"message":"...","renewed_license_b64":"<container>"}`. `renewed_license_b64` is set on **every** active response because `container_b64` is always non-empty (lines 90-92).

Client logic (`manager.py:330-345`): checks only `resp_data.get("revoked")` and `resp_data.get("renewed_license_b64")`. It never checks `success` or `status`, so `UNTRACKED` is treated as valid (R7b). Revocation only sets in-memory `self._status = INVALID_SIGNATURE` (line 331); it is not persisted. The heartbeat loop (`app/main.py:396-404`) sleeps first (default `LICENSE_HEARTBEAT_HOURS=24`, `config.py:317`, auto-validate on, `config.py:319`) and ignores the return value.

### 3.5 Portal database schema

Five tables, defined inline in `db.php:39-109` (SQLite) and `db.php:111-183` (MySQL), and duplicated in `schema.sql:1-71` (drift risk):
- `admins(id, username UNIQUE, password_hash, created_at)` - seeded from `ADMIN_DEFAULT_USER/PASS` only if the table is empty (`db.php:103-108`, `177-182`).
- `licenses(id, license_id UNIQUE, customer_name, customer_email, customer_organization, edition, machine_fingerprint, issued_at, expires_at, is_perpetual, max_users, max_studies_per_year, features_json, status ACTIVE|REVOKED, revocation_reason, container_b64, created_at)`.
- `activation_keys(id, product_key UNIQUE, edition, max_activations, activations_count, allowed_email, expires_days, features_json, is_active, created_at)`.
- `activations_log(id, license_id, product_key, machine_fingerprint, ip_address, user_agent, hardware_info_json, created_at)`.
- `heartbeat_log(id, license_id, machine_fingerprint, ip_address, app_version, last_seen_at)`.

### 3.6 Crypto used in the portal

- Passwords: `password_hash` / `password_verify` (bcrypt, cost 10 from the stored hash prefix).
- Product keys: `PAXS-<4-char edition code>-XXXX-XXXX-XXXX` from `random_bytes(2)` x3 (`crypto.php:117-129`), 48 bits of entropy, no rate limit on `activate.php`.
- License IDs: `PAXS-<EDITION>-<YEAR>-<8HEX>` from `random_bytes(4)` (`crypto.php:134-139`).
- Container: AES-256-GCM, random 12-byte IV (`crypto.php:90`), AAD `PAXS_LIC_v1`.
- Signature: RSA-2048 PKCS#1 v1.5 + SHA-256.

### 3.7 PAXS server side

- `app/core/licensing/crypto.py`: loads public key from `PAXS_LICENSE_PUBLIC_KEY` / `PAXS_LICENSE_PUBLIC_KEY_PATH` env, else the embedded default (`:107-125`). AES key from `PAXS_LICENSE_AES_KEY` env, else the public constant (`:39-42`). **No deploy artifact sets these env vars**, so every deployment uses the public keys.
- `manager.py`: `activate_online` -> `activate_from_bytes` (signature + expiry check). `validate_online` -> heartbeat (3.4). Callers: `app/api/v1/license.py:142` (activate), `:163` (validate), `app/main.py:402` (background loop).

**Consequence (R1):** because the private signing key is public, any party with repo access can sign a license that the PAXS server accepts (signature, fingerprint `*`, and expiry all pass). Verified: key pair consistent and public key identical to the one PAXS embeds.

---

## 4. Scripts reference

| Script | Purpose | Inputs | Touches | How to run |
|---|---|---|---|---|
| `scripts/generate_license.py` | Vendor CLI: key generation, issue, verify | Subcommands `init-keys [--force]`, `fingerprint`, `create --customer ... [--edition trial|clinic|hospital|enterprise] [--fingerprint F|*] [--days N|--perpetual] [--max-users] [--max-studies] [--features a,b] [--license-id] [--output paxs.lic]`, `verify <file>` | Writes `scripts/vendor_keys/paxs_vendor_{private,public}.pem` (`:53-55,65-67`); writes the `.lic` file | `python scripts/generate_license.py init-keys` then `create ...` |
| `scripts/unified_gateway_server.py` | Local gateway: serves Gemini-style `/v1beta/models...` and `/v1/models...`, translates to OpenAI `/chat/completions` per provider | `PORT` env (default 8899); reads `settings.json` at repo root `gatewayProviders` (`:26-27,40-50`) | Writes `~/.gemini/config/agents/*.md` (`:55-85`) and **deletes existing `*.md` there** (`:59-64`) | `python scripts/unified_gateway_server.py` (binds 127.0.0.1 only, line 444) |
| `scripts/use-gateway.ps1` | Activates one provider and launches `agy` | `-Provider` (default `agentrouter`), `-Model` | Persists `GOOGLE_GEMINI_BASE_URL` and `GEMINI_API_KEY` in **User env** (`:38-39`); rewrites `~\.gemini\antigravity-cli\settings.json` (`:45-51`) | `powershell .\scripts\use-gateway.ps1 -Provider X` |
| `run-all-providers.ps1` / `.bat` | Starts gateway (broken, see S5), sets dummy env, rewrites global agy settings, launches `agy` | `-Model` (optional) | Same as above | `run-all-providers.bat [model]` |
| `scripts/sync_agents.py` | Regenerates agent `.md` from gateway `/v1beta/models` | none | Deletes `*.md` in `~/.gemini/config/agents` (`:19-24`), writes new ones | `python scripts/sync_agents.py` (gateway must be running) |
| `scripts/run_remote_migration.py` | Solves a third-party host's JS anti-bot challenge and calls its `db/install.php?i=1` | none (hardcoded `http://elsamanii.rf.gd`, line 9) | Remote-only; prints the response | `python scripts/run_remote_migration.py` (needs `cryptography`). **See S7.** |
| `scripts/reset_admin_password.php` | Sets `admin` password in `admin_users` of a sibling project's DB to `Admin1234!` and prints it | none; requires `../portfolio/includes/db.php` (absent) | Writes the sibling DB | `php scripts/reset_admin_password.php` (not applicable to the vendor portal; see S8) |
| `scripts/00_demo.py` | Event-script example: logs StudyReceived / StudyDeleted / CriticalResultFlagged | none | Logs only | Drop into the server's scripts dir (hot-reloaded) |
| `scripts/01_critical_alert.py` | Event-script: POSTs critical-result JSON to `CRITICAL_WEBHOOK_URL` if set | Set URL at line 10 (blank by default) | Outbound HTTP POST (patient_id, description) | Same as above |
| `scripts/build_exe.bat` | PyInstaller build of `paxs.spec` | none | `dist\PAXS-Server` (after `paxs.spec`) | `scripts\build_exe.bat` (requires missing `paxs.spec`) |
| `scripts/install_service.bat` | Installs `PAXSServer` NSSM service | Admin shell; `nssm.exe` on PATH; built `dist\PAXS-Server` | Service registration; starts it | `scripts\install_service.bat` as Administrator |
| `run_paxs.py` | Starts the server (uvicorn, DICOM SCP and worker run in lifespan) | `PAXS_HOST`, `PAXS_PORT`, `HTTPS_ENABLED`, `HTTPS_CERT`, `HTTPS_KEY`, `.env` | Logs only | `python run_paxs.py` |
| `run_all.bat` | Starts Redis (optional), server, optional Vite dev, health monitor | none | Opens windows | `run_all.bat` |
| `build_standalone.bat` | Builds `dist\PAXS-Server` with UI assets, PyInstaller, runtime folders, `.env` | Working `venv` optional | Writes `dist\PAXS-Server`; **copies `.env` into it (S12)** | `build_standalone.bat` (requires `paxs.spec`) |
| `health_monitor.bat` | Port/status menu, restart, stop | Menu input | `taskkill`, starts processes | `health_monitor.bat` |
| `deploy/vendor-portal-php/*` | Vendor admin UI and public API | Browser login; API JSON | `portal.db` (SQLite) or MySQL | `php -S localhost:8080` from that folder (README) |

---

## 5. Ops runbook

**Start (single node, Windows):** `run_all.bat`, or `python run_paxs.py` with `.env` (copy `.env.example`, set `SECRET_KEY`, `DATABASE_URL`, `DICOMWEB_PUBLIC` and `ALLOW_REGISTER` deliberately; see R2-R4).

**Start (LB stack):**
```
docker compose -f deploy/docker-compose.lb.yml up -d --build
docker compose -f deploy/docker-compose.monitoring.yml up -d   # optional
```
Set `SECRET_KEY` first (the compose file currently does not set it; R2).

**Stop:** `docker compose -f deploy/docker-compose.lb.yml down` (add `-v` only to delete MySQL and MinIO data). Windows: `health_monitor.bat` option 6.

**Health:**
- Node readiness: `curl -s localhost:8000/api/v1/health/ready` (200 ready / 503 DB down; `dashboard.py:128-141`).
- Liveness: `/api/v1/health/live` (`dashboard.py:122-125`). Both return `instance` (`PAXS_INSTANCE_ID`).
- HAProxy backend status: the stats page on 8404 does not exist (D3). Use the 8405 `/metrics` endpoint (no auth).
- Round-robin check: `for i in 1 2 3 4 5 6; do curl -s localhost:8000/api/v1/health/live; echo; done`.

**Scale out (API nodes):** add a `paxs-node-4` anchor entry in `docker-compose.lb.yml`, add a `server` line in each of `paxs_api`, `paxs_dimse`, `paxs_hl7` in `haproxy.cfg`, and add the target to `prometheus.yml`. There is no service-discovery; the compose anchor and haproxy must be edited by hand.

**Scale out (compression):** run `python -m app.services.worker --poll 3.0` on dedicated hosts or `paxs-worker@.service` (systemd unit in `SCALE-OUT-GUIDE.md:127-157`). Use `--once` for cron batches. The worker claims rows atomically (`SCALE-OUT-GUIDE.md:63-66`). Redis must be shared for the triage lock (currently absent from the compose stack; D6).

**Database migrations (alembic):** `alembic.ini` sets `script_location = migrations` and `migrations/env.py:31` reads `settings.DATABASE_URL`. Run it in a container:
```
docker compose -f deploy/docker-compose.lb.yml run --rm paxs-node-1 alembic upgrade head
```
Caveat: `SCALE-OUT-GUIDE.md:93` says the worker entrypoint runs `Base.metadata.create_all`. Mixing `create_all` with alembic can drift the schema (not verified in code; see open questions).

**Upgrade (rolling, LB stack):** `git pull`, then rebuild one node at a time so HAProxy keeps serving: `docker compose -f deploy/docker-compose.lb.yml up -d --no-deps --build paxs-node-1`, wait for `/api/v1/health/ready` = 200, then node-2 and node-3. Run alembic once before the first node restarts.

**Reset admin password:**
- Vendor portal: `scripts/reset_admin_password.php` does **not** apply (it updates `admin_users` in `../portfolio`, which is absent). Generate a hash locally with `php -r "echo password_hash('NEW_PASSWORD', PASSWORD_BCRYPT), PHP_EOL;"`, then `UPDATE admins SET password_hash='<hash>' WHERE username='admin';` on `portal.db` or MySQL. Changing `ADMIN_DEFAULT_PASS` in `config.php` has no effect after the first run (seeding only, `db.php:103-108`).
- PAXS app: the first registered user becomes ADMIN (`auth.py:184-185`). No CLI reset script found in scope (open question).

**Backups:** MySQL `mysql-data` and MinIO `minio-data` volumes (not covered by scripts in scope). `portal.db` must be backed up separately and must not be web-accessible (R8).

---

## 6. Code review findings

Ordering is by severity, then by impact. Each entry gives the evidence, the failure scenario, and the fix.

### 🔴 R1. Vendor RSA-2048 **private** signing key committed to the repository

- **Where:** `deploy/vendor-portal-php/config.php:43-72` (`PAXS_VENDOR_PRIVATE_KEY`). Also tracked in commit `8af1381`.
- **Quote (header only; body not reproduced):** `define('PAXS_VENDOR_PRIVATE_KEY', <<<EOD` / `-----BEGIN PRIVATE KEY-----`.
- **What is wrong:** The portal's README (lines 84-85) says this key "signs your licenses. Never commit production private keys". It is committed. Verified: the pair is consistent and its public half equals the key PAXS embeds (`crypto.py:29-37`), so PAXS accepts signatures from this key.
- **Failure scenario:** Anyone with repo read access (or a leaked clone) runs `openssl_sign`-equivalent code, or the portal itself, and produces a license with any edition, features, seats, expiry (or perpetual), and fingerprint `*`. PAXS `verify_license_signature` (`crypto.py:148-197`) accepts it.
- **Fix:** Treat this key as compromised. Generate a new pair, embed the new public key, re-issue licenses, and remove the private key from the working tree and history (`git filter-repo` or equivalent). Load the signing key from an environment variable or secret store on the portal host. Add `deploy/vendor-portal-php/config.php` (or a `config.local.php`) to `.gitignore`. The existing `.gitignore:31` entry for `scripts/vendor_keys/` shows the intent but not the actual path.

### 🔴 R2. Forgeable JWTs: public default `SECRET_KEY`, never overridden by deploy artifacts

- **Where:** `app/core/config.py:136` `SECRET_KEY: str = "paxs-insecure-dev-key-change-me"`; `app/core/security.py:81,141-153` (HS256 verify with `secret`); `app/api/v1/deps.py:51` and `:76` (`decode_token(..., settings.SECRET_KEY)`), then user lookup by `payload["sub"]` (`deps.py:56`, `:81`). `.env.example:39` uses `SECRET_KEY=change-me-to-a-long-random-string` (also public). No startup guard; `grep -i insecure` finds only the default line.
- **Deploy artifacts:** `deploy/Dockerfile:21-26` and `deploy/docker-compose.lb.yml:36-46` never set `SECRET_KEY`.
- **What is wrong:** Both defaults are in the public repo. Token signature verification trusts whatever key the server holds, so anyone can mint `{"sub":"<existing username>","exp":<future>}` signed with the known key. `get_current_user` loads the role from the DB, so forging `sub` = an admin username yields admin access.
- **Failure scenario (not executed):** An attacker reaches `http://<lb-host>:8000`, forges an HS256 token with `sub=admin` and the public default key, and calls admin endpoints (users, storage, settings, license actions).
- **Fix:** Fail startup when `SECRET_KEY` is the default or shorter than 32 random characters. In compose use `SECRET_KEY: ${PAXS_SECRET_KEY:?set PAXS_SECRET_KEY}`. Rotate sessions after the fix (all tokens signed with the old key remain forgeable until rotated).

### 🔴 R3. DICOMweb is open to unauthenticated writes and reads; `DICOMWEB_PUBLIC` is dead config

- **Where:** `app/api/v1/dicomweb.py:806-808` (`POST /studies` -> `stow_studies(..., user: User | None = Depends(get_optional_user))`); no `401` path exists in `dicomweb.py` (grep). Guest access is also used for QIDO and WADO at `dicomweb.py:199, 260, 283, 808`. `app/core/config.py:253` `DICOMWEB_PUBLIC: bool = True`; `app/services/settings_service.py:57-61` exposes it as an admin toggle "DICOMweb public access - Allow QIDO/WADO/STOW without a login token". **No code reads `settings.DICOMWEB_PUBLIC`** (grep, case-insensitive: only config and settings_service). `_apply_to_runtime` (`settings_service.py:427-430`) only `setattr`s it.
- **Gate check:** `ViewerAuthGateMiddleware` (`app/main.py:268-300`) gates only `/`, `/viewer`, `/studies-ui`, `/settings`, and is inert unless `AUTH_GATE_ENABLED`.
- **Deploy/docs:** `.env.example:67-69` sets `DICOMWEB_PUBLIC=true` and `CORS_ORIGINS=*` as recommended values. `paxsviewer-app-config.js:40` sets `supportsStow: true`. `PAXSVIEWER-INTEGRATION.md:22-23` says the endpoints "accept the guest/anonymous scope (see DICOMWEB_PUBLIC)", which is false.
- **Failure scenario:** Any host that can reach `:8000` (HAProxy publishes it; `docker-compose.lb.yml:16`) can POST DICOM instances to `/dicom-web/studies` (archive poisoning, PHI injection) and read all studies and frames via QIDO/WADO, with no login. The admin toggle to "disable public access" has no effect.
- **Fix:** Enforce `DICOMWEB_PUBLIC` in `dicomweb.py` (401 when `user is None` and the flag is off). Make STOW-RS require authentication regardless of the flag. Default `DICOMWEB_PUBLIC=false` and `CORS_ORIGINS` to an explicit list. Add a test that a guest POST to `/dicom-web/studies` returns 401.

### 🔴 R4. Open registration: the first caller of `/api/v1/auth/register` becomes ADMIN

- **Where:** `app/api/v1/auth.py:173` (`if not settings.ALLOW_REGISTER`), `:184-185` (`if total == 0: role = "ADMIN"`). `app/core/config.py:145` `ALLOW_REGISTER: bool = True`; `.env.example:41` `ALLOW_REGISTER=true`; compose does not override.
- **Failure scenario:** A fresh LB deployment (empty MySQL) reachable on `:8000` lets the first HTTP client register `admin` and own the system. The same applies to a re-created DB.
- **Fix:** Default `ALLOW_REGISTER=false`. Create the first admin via a CLI or one-time bootstrap token. Keep the "first user is admin" rule only for that bootstrap path.

### 🔴 R5. Deploy-blocking: `app/services/storage/` is missing from the repo and is gitignored

- **Evidence:** `app/services/storage/` does not exist in the working tree or git history (`git log --all -- app/services/storage` empty). `.gitignore:9` (`storage/`) matches it. Imports that require it: `app/services/storage_service.py:35` (`from app.services.storage import get_storage_backend`), `:38` (`from app.services.storage.backend import StorageError`), `app/api/v1/storage.py:34`, `app/services/replication_service.py:39`. `app/main.py` imports `dicomweb.py`, which imports `storage_service` at module level (`dicomweb.py:32`).
- **What is wrong:** A fresh clone, or a Docker build from this tree (`COPY app ./app`, `Dockerfile:15`), cannot import `app.main`. The backend module is never committed because `storage/` matches it.
- **Failure scenario:** `ModuleNotFoundError: No module named 'app.services.storage'` on container start; restart loop on all three nodes.
- **Fix:** Anchor the ignore rule (`/storage/` for the root data dir) and commit the missing package. Add a CI step `python -c "import app.main"` so this cannot regress. Confirm whether the package exists on the developer machine outside this checkout (open question).

### 🔴 R6. Deploy-blocking (likely): unguarded `import redis`, `boto3` absent

- **Evidence:** `app/services/redis_service.py:26` `import redis` (unguarded, module level). Chain: `app/main.py:491,551` -> `app/api/v1/redis_panel.py:28` -> `redis_service`. `requirements.txt:30-32` comments out `redis` and `boto3`. None of the listed requirements is known to pull in `redis`. `app/core/config.py:59` says "S3 needs boto3 installed", and no `boto` import exists in `app/` (grep).
- **Failure scenario:** The Docker image (`pip install -r requirements.txt`) fails `import app.main` with `ModuleNotFoundError: redis`. `STORAGE_BACKEND=s3` in the image has no client library even if imports were fixed.
- **Fix:** Add `redis>=5.0` (and `boto3` if the S3 backend uses it) to `requirements.txt`, or guard the import and degrade. Build the image in CI (open question: not built here).

### 🔴 R7. License revocation is not enforceable: un-revoke via "+1 Year", and deleted licenses pass validation

- **(a) Re-activation via extend:** `deploy/vendor-portal-php/index.php:113-148` (`extend_license`) runs `UPDATE licenses SET expires_at=?, is_perpetual=0, status='ACTIVE', container_b64=? WHERE id=?` (line 145) with no status check. The "+1Y" form (lines 580-586) appears for every row, including REVOKED ones. One click on a revoked license makes it ACTIVE again and issues a fresh container.
- **(b) Deleted licenses are treated as valid:** `index.php:149-153` hard-deletes a row. `api/validate.php:48-55` then returns `{"success":true,"status":"UNTRACKED"}`. `manager.py:330-345` checks only `revoked` and never `status` or `success`, so the client accepts it.
- **(c) Revocation does not survive restart:** `manager.py:331` sets only in-memory `_status`. The heartbeat loop (`main.py:396-404`) sleeps 24 h before the first check. A restart re-reads the local `.lic` and reports it valid until the next heartbeat.
- **Failure scenario:** Customer's subscription lapses; admin clicks revoke (status REVOKED). Customer restarts PAXS (valid again until the 24 h heartbeat). Or, admin clicks "+1Y" on a REVOKED license and it is reactivated. Or the row is deleted and the server reports UNTRACKED, which the client accepts.
- **Fix:** In `extend_license`, refuse when `status='REVOKED'`, or require explicit un-revoke. In `validate.php`, return a non-success status for unknown IDs and never delete license rows (soft-delete). In `manager.validate_online`, require `success is True` and `status == "ACTIVE"` before accepting, and persist revocation to disk (or treat it as fatal at startup). Run the first heartbeat at startup.

### 🔴 R8. Vendor portal SQLite database inside the web root (documented deploy path)

- **Where:** `config.php:23` `DB_SQLITE_PATH = __DIR__ . '/portal.db'`; README lines 41-47 instruct uploading the whole folder to `public_html/paxs-portal/` and making it writable (`chmod 755`, which allows SQLite creation). No `.htaccess` or `deny` rule is shipped (verified: no `.htaccess` in the folder).
- **Failure scenario:** `GET https://<domain>/paxs-portal/portal.db` downloads the admin bcrypt hash, every product activation key (bearer secrets), every issued license container and customer PII. Shared hosts usually serve unknown extensions as static files.
- **Fix:** Put the DB outside the web root (e.g., `../data/portal.db` with an absolute path in config), and ship a `.htaccess` that denies `*.db`, `*.sqlite`, and `config.php`. Document it as a required step.

### 🟡 R9. PHP and Python canonical JSON differ for non-ASCII values: PHP-issued licenses fail signature checks

- **Where:** PHP `crypto.php:56` (`JSON_UNESCAPED_UNICODE`); Python `crypto.py:103` (`json.dumps(..., sort_keys=True, separators=(",",":"))`, `ensure_ascii=True`).
- **Verified:** For `customer_name = "Dr. Ahmed مستشفى"`, PHP emits raw UTF-8 and Python emits `\u0645\u0633...`. The bytes differ (SHA-256 prefix `32424b87…` vs `9b99fba0…`). Both PSS and PKCS1v15 verification on the PAXS side hash `get_canonical_payload_bytes`, so a PHP signature over raw UTF-8 will not verify.
- **Failure scenario:** A hospital with an Arabic (or any non-ASCII) name, organization, email or feature value is activated online. The portal signs the PHP bytes, `activate_from_bytes` calls `verify_license_signature` (`manager.py:200-212`), and the activation returns "License cryptographic signature is invalid or tampered" without writing the file. The "100% byte-for-byte compatible" claim in `deploy/vendor-portal-php/README.md:14` is false for these inputs. (The byte mismatch is verified; the full activate round-trip was not executed.)
- **Fix:** Use the same encoding on both sides. Either drop `JSON_UNESCAPED_UNICODE` in PHP, or pass `ensure_ascii=False` in Python. Add a cross-language test vector to CI.

### 🟡 R10. Activation race and email restriction bypass

- **Where:** `api/activate.php:61-68` checks `activations_count >= max_activations` (read), and `:134` increments it in a later statement (not conditional). Concurrent requests can both pass the check. `:71` `if (!empty($keyRow['allowed_email']) && !empty($email))` skips the restriction when `email` is empty.
- **Failure scenario:** A single-use key is redeemed twice in parallel. A key restricted to `alice@x.org` is redeemed with no email field. The `email` value is client-supplied anyway (`manager.py:251`), so the restriction is not an authentication control.
- **Fix:** Make the quota check atomic: `UPDATE activation_keys SET activations_count=activations_count+1 WHERE id=? AND activations_count < max_activations` and check `rowCount()` inside the transaction. Reject when `allowed_email` is set and `email` is missing or mismatched.

### 🟡 R11. Vendor portal authentication weaknesses

- No brute-force protection on login (`login.php:17-31`, `auth.php:36-49`).
- No `session_regenerate_id(true)` after login (`auth.php:43-45`): session fixation is possible if an attacker can set the session cookie.
- Session cookie flags are PHP defaults; `config.php:14-16` calls `session_start()` without `session_set_cookie_params` (no `Secure`, `HttpOnly`, `SameSite`).
- Logout is a GET (`logout.php:10`): a cross-site `<img src=logout.php>` logs the admin out.
- Positives verified: every POST in `index.php` and `login.php` checks `verifyCsrfToken` with `hash_equals` (`index.php:24`, `login.php:22`, `auth.php:29-34`); every SQL statement uses PDO prepared statements or constant SQL with `(int)` casts; no SQL injection found.
- **Fix:** Rate-limit login per IP and per username; regenerate session id on login; set cookie params (`secure`, `httponly`, `samesite=Strict`); make logout a POST with CSRF.

### 🟡 R12. Public default admin credentials shown on the login page; documented default does not work

- **Where:** `login.php:204` renders `Default credentials: <code>admin</code> / <code>Admin1234!</code>` on the public page. `config.php:33-35` comment and hash claim "Hash for Admin1234!". **Verified:** `password_verify("Admin1234!", …)` is FALSE for that hash (self-test passes). README lines 31-34 repeat the default.
- **Failure scenario:** On a fresh install the admin account cannot be logged into with the advertised password (lockout; no working reset path; see section 5). Any operator who replaces the hash with one for the advertised password exposes it to the public login page.
- **Fix:** Remove the credentials text from the login page and README. Use a first-run setup page that forces a password on first login, and remove the hardcoded hash.

### 🟡 R13. Unauthenticated `/metrics` and HAProxy stats reachable on public ports

- `app/api/v1/metrics.py:86-88`: `@router.get("/metrics")` with no auth dependency. HAProxy `http_in` forwards every path to `paxs_api` (`haproxy.cfg:37-43`), so `/metrics` is reachable on the public API port `:8000`, not just by Prometheus.
- `haproxy.cfg:29-34` and compose port `8405` (`docker-compose.lb.yml:20`): HAProxy `stats` and `/metrics` published to host with no auth. `MONITORING.md:110-112` says "restrict it to your monitoring subnet", which nothing enforces.
- **Exposed:** node instance IDs, DB up/down, replica state, queue depth, HAProxy server names and traffic.
- **Fix:** Block `/metrics` on the public frontend (`http-request deny if { path /metrics }` in `http_in`), bind `metrics_in` to `127.0.0.1` or a monitoring network, or add a bearer/basic auth check.

### 🟡 R14. Plain HTTP and plain DIMSE for PHI in the LB topology

- `haproxy.cfg:37-43` (no TLS bind on `:8000`); `dimse_in` (`:10400`) and `hl7_in` (`:2575`) are plain TCP; `docker-compose.lb.yml:15-18` publishes all three to the host. `.env.example:22` `DICOM_TLS_ENABLED=false`. `HTTPS_ENABLED` defaults to false (`config.py:51`) and is not used in the compose stack.
- **Fix:** Terminate TLS at HAProxy (`bind *:443 ssl crt ...`) and enable DICOM TLS (`DICOM_TLS_ENABLED=true`, certs from `.env`) before any production use. Document that PHI in plaintext on a LAN is a deployment decision.

### 🟡 R15. Portal storage of customer PII and AES key constant

- `config.php:40` `PAXS_AES_ROOT_KEY = hash('sha256', 'PAXS_ARCHIVE_LIC_SEC_2026_AES25', true)` and `crypto.py:39-42` use the same public string. Anyone can decrypt any `.lic` or `renewed_license_b64` and read `customer_name`, `customer_email`, `customer_organization`, `features`. `api/validate.php:36-37,90-92` returns `container_b64` to any caller who knows a `license_id` (no ownership or fingerprint check; `license_id` is 32 bits random plus edition and year).
- **Fix:** Treat the container as public data (it is only obfuscation). Stop returning `renewed_license_b64` unless it actually changed and the request proves possession (e.g., signed nonce). Keep PII out of the container, or move the key to a per-deployment secret.

### 🟡 R16. Unescaped output and unvalidated edition in the portal admin UI

- `index.php:31` accepts any `edition` string (no whitelist against `$EDITIONS`). `index.php:558` and `:631` echo `strtoupper($l['edition'])` and `strtoupper($k['edition'])` without `htmlspecialchars`.
- `index.php:706` echoes `$_SERVER['HTTP_HOST']` unescaped.
- Impact is limited (admin-only, CSRF-protected create action), but these are stored and reflected XSS-class issues in an admin UI.
- **Fix:** Whitelist edition; escape all output with `htmlspecialchars`.

### 🟡 D1. Container runs as root

- `deploy/Dockerfile` has no `USER` (lines 3-33). Any RCE in the app (e.g., the admin script feature) gives root in the container.
- **Fix:** `RUN useradd -r -u 10001 paxs && chown -R paxs /app` and `USER 10001`. Drop `build-essential` after `pip install` (multi-stage build).

### 🟡 D2. Default and hardcoded credentials in compose and Dockerfile, with MinIO published to host

- `deploy/docker-compose.lb.yml:41-42` and `:92-93` `minioadmin/minioadmin`; console `9001` and S3 `9000` published (lines 96-98) to all host interfaces. The S3 bucket holds every DICOM object (PHI). Anyone who can reach the host can read the archive.
- `deploy/Dockerfile:22` and compose `:38` `DATABASE_URL=...paxs_user:paxs@...`; compose `:75` `MYSQL_ROOT_PASSWORD: root`, `:78` `MYSQL_PASSWORD: paxs`, `:82` healthcheck passes `-ppaxs` on the command line.
- **Fix:** Remove the published MinIO ports (or bind to 127.0.0.1). Use `${VAR:?}` for all secrets, supplied via `.env` (gitignored) or Docker secrets. Rotate the MinIO and MySQL credentials.

### 🟡 D3. HAProxy healthcheck and published port reference a stats frontend that does not exist

- `docker-compose.lb.yml:19` publishes `8404`; `:26` runs `wget http://127.0.0.1:8404/;csv` as the container healthcheck. `haproxy.cfg` has no `bind *:8404` (only 8405 and the three app frontends). The HAProxy container will always report unhealthy.
- `LOAD-BALANCING.md:35` says "HAProxy stats on :8404". That is false for this config.
- **Fix:** Either add `frontend stats` with `bind *:8404` and `stats enable`/`stats auth` (and do not publish it), or change the healthcheck to `haproxy -c -f ...` / `wget -qO- http://127.0.0.1:8405/metrics` and remove the 8404 mapping. Update the doc.

### 🟡 D4. Grafana/Prometheus defaults and host exposure

- `docker-compose.monitoring.yml:37-38` `GF_SECURITY_ADMIN_PASSWORD: admin`; port 3000 published (line 35). `:22` publishes Prometheus 9090 with no auth.
- `MONITORING.md:46-47` documents `admin/admin` as the default.
- **Fix:** Use `${GRAFANA_ADMIN_PASSWORD:?}`, publish only on 127.0.0.1, put Prometheus behind the monitoring network only.

### 🟡 D5. Grafana datasource has no `uid` but every panel references `uid: "prometheus"`

- `grafana/provisioning/datasources/prometheus.yml:4-9` has no `uid`. Dashboard JSON lines 30, 62, 107, 142, 193, 227 reference `"uid": "prometheus"`. Grafana generates a random uid for provisioned datasources without one, so panels will likely show "datasource not found" (not rendered here).
- **Fix:** Add `uid: prometheus` to the datasource file.

### 🟡 D6. Redis is required by the docs but absent from the compose stack

- `SCALE-OUT-GUIDE.md:33-34,287` says Redis is "shared, required for multi-process correctness" (triage lock, rate limit, caches). `REDIS_URL=redis://redis:6379/0` appears in the guide (lines 102, 194, 308). `docker-compose.lb.yml` has no Redis service (grep: none). `app/core/config.py:167` defaults to `redis://127.0.0.1:6379/0` with no auth.
- Without Redis, rate limiting falls back to per-process memory (`app/api/ratelimit.py:49-53`; see D7) and the triage lock is lost (N workers may race).
- **Fix:** Add a `redis` service with `--requirepass ${REDIS_PASSWORD}` and set `REDIS_URL` on the nodes and workers; or update the guide to say Redis is optional for LB mode.

### 🟡 D7. Rate-limit key trusts the client's `X-Forwarded-For`

- `app/api/ratelimit.py:29-33` uses the leftmost `X-Forwarded-For` value as the client key. HAProxy `option forwardfor` (`haproxy.cfg:41`) appends the real client IP to the right, so the leftmost value is client-controlled.
- **Failure scenario:** The `auth` rate limit (`RATE_LIMIT_LOGIN_PER_MINUTE=10`, `.env.example:45`) is bypassed by rotating an arbitrary `X-Forwarded-For` header on every request.
- **Fix:** Use the rightmost value added by the trusted proxy, or use the socket peer address and `uvicorn --proxy-headers --forwarded-allow-ips=<haproxy IP>`.

### 🟡 D8. `reload=True` in the production entrypoint

- `run_paxs.py:49` `reload=True` (also used by the container `CMD`). Uvicorn reload starts a file watcher and a supervisor process; it is not intended for production and runs the app in a child process.
- **Fix:** Make reload opt-in (`PAXS_RELOAD=1`) and default to `False` in the Docker path.

### 🟡 D9. Unpinned images

- `python:3.12-slim` (`Dockerfile:3`), `prom/prometheus:latest`, `grafana/grafana:latest`, `minio/minio:latest` (`docker-compose.monitoring.yml:16,28`, `docker-compose.lb.yml:88`), `mysql:8.0`. Builds are not reproducible and can break silently.
- **Fix:** Pin by version and digest.

### 🟢 D10. Build context and dev dependencies

- No `.dockerignore`; the build context is the whole repo (`docker-compose.lb.yml:33 context: ..`) including `.env`, `settings.json`, `scripts/vendor_keys/` and `deploy/vendor-portal-php/config.php`. They are not copied into the image (the Dockerfile copies explicit paths), but they are sent to the Docker daemon and may be cached in build layers if a future `COPY . .` is added.
- `requirements.txt:35-36` installs `pytest` and `httpx` (dev) into the image.
- **Fix:** Add a `.dockerignore` (`.env`, `settings.json`, `scripts/vendor_keys`, `deploy/vendor-portal-php/config.php`, `storage/`, `.git`, `venv/`). Split dev requirements.

### 🟢 D11. No restart policies; `latest`/unpinned operational images

- No `restart:` on any service in `docker-compose.lb.yml` or `docker-compose.monitoring.yml`. A node that crashes on start (R5/R6) will stay down.
- **Fix:** `restart: unless-stopped`.

### 🟡 S1. Local Gemini gateway: no authentication, wildcard CORS with credentials, spends provider API keys

- `scripts/unified_gateway_server.py:18-24`: `CORSMiddleware(allow_origins=['*'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])`. No route has any auth (`:157-181`, `:292-304`). Bound to `127.0.0.1` (`:444`), which blocks remote access.
- The gateway forwards requests using each provider's `apiKey` (`:95-99`, `:299-304`). Any web page open in the same browser can call `http://127.0.0.1:8899/v1beta/models/...` and receive model output, spending the operator's keys. Whether the browser allows this depends on Local Network Access / Private Network Access rules, which vary by browser version (not tested).
- **Fix:** Require a random bearer token generated at startup and written to a file readable only by the user, and drop `allow_credentials` with `*`. Restrict `allow_origins` to the local agy client origin, if any.

### 🟡 S2. Gateway deletes every `*.md` in `~/.gemini/config/agents` on each model list call

- `unified_gateway_server.py:55-64` (`sync_agents_to_filesystem`) deletes every `*.md` in `AGENTS_DIR`. It runs from `fetch_all_models` (`:87-151`), which is called on startup (`:153-155`), every `GET /v1/models` (`:163-167`), and every `GET /v1beta/models/{name}` (`:169-181`).
- `sync_agents.py:19-24` does the same wipe.
- **Failure scenario:** A user keeps a hand-written agent `~/.gemini/config/agents/reviewer.md`. The next `/v1/models` request (for example, from `run-all-providers.ps1:35`) deletes it. Nothing warns the user, and the directory may be shared with other agent tools.
- **Fix:** Only remove files that carry a generated-by marker (e.g., a first-line comment) and that match the generated set. Write to a dedicated subdirectory.

### 🟡 S3. Path traversal on Windows via upstream model ID

- `unified_gateway_server.py:67-71`: `name = m.get('baseModelId') or ...`, `safe_name = name.replace('/', '_').replace(':', '_')`, `file_path = os.path.join(AGENTS_DIR, f'{safe_name}.md')`. Backslashes are not replaced. The model ID comes from the upstream provider's `/v1/models` response (`:105-113`).
- **Failure scenario:** A compromised or malicious provider returns a model id like `..\..\Startup\x`. On Windows `os.path.join` accepts it and the gateway writes `x.md` outside `AGENTS_DIR`. Impact is limited to `.md` files with attacker-chosen content. Also: `displayName` is written unescaped into YAML frontmatter (`:73-80`), so a newline in it injects keys.
- **Fix:** Whitelist model IDs (`[A-Za-z0-9._-]+`) and resolve the final path against `AGENTS_DIR` before writing.

### 🟡 S4. `use-gateway.ps1` persists the provider API key in plaintext in the user environment

- `scripts/use-gateway.ps1:36` sets `$env:GEMINI_API_KEY = $selected.apiKey`. Line 39 writes it with `SetEnvironmentVariable(..., User)`, which stores it in `HKCU\Environment` in plaintext and leaks it to every future process. `run-all-providers.ps1:44` sets a dummy key instead, so the two scripts disagree.
- Lines 46-51 rewrite `~\.gemini\antigravity-cli\settings.json` with `ConvertTo-Json -Depth 5`, which can truncate deeper nested values.
- **Fix:** Pass the key per process (`$env:` only, no `SetEnvironmentVariable User`). Use `-Depth 100` or edit the JSON as a string.

### 🟡 S5. `run-all-providers.ps1` never starts the gateway (quoting bug)

- `run-all-providers.ps1:27` `Start-Process -FilePath "python" -ArgumentList """"`. Verified in PowerShell: `""""` evaluates to a single `"`, so Python receives `"` as its argument. `$serverScript` (line 8) is computed but never passed.
- The script then queries `/v1beta/models` (line 35), fails silently, prints "Gateway starting...", and launches `agy` pointed at a dead endpoint (lines 43-44).
- **Fix:** Pass the path as one quoted argument, e.g. `-ArgumentList ('"' + $serverScript + '"')`, and verify the process actually started.

### 🟡 S6. `health_monitor.bat` and `run_all.bat` probe the wrong port

- `health_monitor.bat:96-97` probes `http://0.0.0.0/health` (port 80). PAXS listens on 8000 by default (`run_paxs.py:38`). Also, the root `/health` route does not exist: the only health routes are `/api/v1/health*` (`dashboard.py:122,128,143` with the `/api/v1` prefix at line 62). Option [1] therefore always reports failure.
- Printed URLs `http://0.0.0.0/health`, `http://0.0.0.0/viewer/` (`health_monitor.bat:66-68`, `run_all.bat:62-64`) lack `:8000`.
- **Fix:** Use `http://127.0.0.1:%PAXS_PORT%/api/v1/health/ready` and print the real port.

### 🟡 S7. `run_remote_migration.py` solves a third-party bot challenge and runs a remote installer

- `scripts/run_remote_migration.py:9-10` hardcodes `http://elsamanii.rf.gd` (plain HTTP). Lines 23-43 decrypt the host's `__test` JS-challenge cookie (AES-CBC, values taken from the page) and send it back (lines 47-56), then fetch `db/install.php?i=1`, which initializes a remote MySQL schema (docstring line 1). The script defeats the host's anti-bot protection by design.
- The script has nothing to do with the PAXS server and is in the repo. I did not run it.
- **Fix:** Delete it from the repo, or move it out. Only run it against infrastructure you own, and never bypass bot protection on another party's host. Confirm ownership of the host before any use (open question).

### 🟡 S8. `reset_admin_password.php` sets a publicly known password and prints it

- `scripts/reset_admin_password.php:13` `$newPass = 'Admin1234!'`; lines 27-29 print the username and password to stdout; lines 16-25 update `admin_users` in `../portfolio/includes/db.php` (absent in this checkout). The same literal is the vendor portal's advertised default (R12).
- **Failure scenario:** Run against a production sibling site, the admin password becomes a value that is also published in `login.php:204` and the README.
- **Fix:** Generate a random password, print it once, or require it as an argument. Do not hardcode it.

### 🟡 S9. Build scripts depend on files that do not exist or are gitignored

- `scripts/build_exe.bat:14` and `build_standalone.bat:64` run `paxs.spec`, which is absent and gitignored (`.gitignore:15`). `build_standalone.bat:86` copies `install_service.bat` into `dist\PAXS-Server\`, where it cannot find the exe (see section 2.1).
- `CONTROL-PANEL-INTEGRATION.md:81` references `build-win\build.bat` (gitignored `.gitignore:49`, absent). `PAXSVIEWER-INTEGRATION.md:49,178` references `.tools/inject_viewer_theme.sh` (gitignored `.gitignore:18`, absent). `run_all.bat:35-37` uses `.tools\redis` (gitignored, absent).
- **Fix:** Commit `paxs.spec` (or narrow `.gitignore:15`), commit the injection script, and fix the install path (`%~dp0`-relative from the dist folder).

### 🟡 S10. Licensing CLI and portal disagree on edition features and defaults

- `generate_license.py:107-112` hospital = `[recist, lung_rads, agatston, pe_clot, breast_density, hl7_router]`; portal `config.php:108` hospital adds `mpr_3d`, `fhir_gateway`, `storage_tiering`. CLI trial and clinic also differ. CLI defaults `--max-users 100`, `--max-studies 100000`, `--days 365`; portal trial is 5 users / 30 days.
- `generate_license.py:61-67`: `init-keys --force` overwrites the private key with no backup. Every license signed with the old key becomes unverifiable.
- The CLI `private.pem` is written with default permissions (no `chmod 0600`, line 66).
- **Fix:** Single edition table shared by the CLI and portal; back up keys before `--force`; write the key with restrictive permissions.

### 🟡 S11. `install_service.bat`: runs as NSSM default account, no firewall rule, misleading banner

- See section 2.1. The script does not set `ObjectName`, so the service runs under NSSM's default account. Verify the actual account on a test machine. Also no firewall rule for `8000` / `10400`.
- **Fix:** Create a dedicated low-privilege account and set `nssm set PAXSServer ObjectName`. Open only required ports via `netsh advfirewall`.

### 🟡 S12. `build_standalone.bat` ships the live `.env` (SECRET_KEY and DB password) inside the distribution folder

- `build_standalone.bat:75-77`: if `.env` exists it is copied to `dist\PAXS-Server\.env`. Anyone who receives the zip gets the server's `SECRET_KEY` (R2), DB credentials, and any S3 keys.
- **Fix:** Copy only `.env.example` into the distribution. Never copy the live `.env`.

### 🟡 S13. Event script can send patient data to an arbitrary webhook

- `scripts/01_critical_alert.py:19-27`: when `CRITICAL_WEBHOOK_URL` is set, it posts `patient_id`, `study_uid`, `finding_level`, and `description` (patient-linked data) to that URL with `ctx.http_post`. The URL is blank by default (line 10).
- **Fix:** Document that this is PHI egress; require HTTPS and an allowlist of hosts; never default to a public URL.

### 🟢 S14. Minor script issues

- `sync_agents.py:5` `CONFIG_PATH = r'D:\Paxs Archive\settings.json'` is a stale path and is unused.
- `generate_license.py:113` `features = list(set(...))` produces nondeterministic order. The signature covers the list order, so re-issuing gives different bytes for the same license.
- `unified_gateway_server.py:24` `allow_credentials=True` with `*` (also covered by S1).
- `run_all.bat:47` `start "..." %PYTHON_EXE% run_paxs.py` and `build_standalone.bat:56,64` use unquoted `%VAR%`. They work only while the path is relative with no spaces.
- `.env.example:67-69` comments describe `DICOMWEB_PUBLIC=true` and `CORS_ORIGINS=*` as "need open access on a hospital LAN", which conflicts with R3.
- `activate.php:10-13` returns `Access-Control-Allow-Origin: *` on a POST that carries no cookies. Harmless here but unnecessary.
- `db.php:33` `die('Database connection error: ' . htmlspecialchars($e->getMessage()))` leaks DB host/user errors; `activate.php:121-123,180-183` return raw exception text to callers.
- `config.php:29-30` MySQL defaults `DB_USER=root`, `DB_PASS=''`; `config.php:36` `PORTAL_SECRET_KEY` is defined but never used.
- `schema.sql` duplicates the inline schema in `db.php` (drift risk).
- `index.php` `logout` GET (see R11).
- `PERFORMANCE.md:105-108` tells the reader to run `uvicorn ... --http2` or pass `http2=True` to `uvicorn.run`. I could not verify this (uvicorn not installed locally). Treat as unverified.
- `CONTROL-PANEL-INTEGRATION.md:49-51` stores the auth token in `localStorage` (`paxs_token`). Any XSS on `/admin` can exfiltrate it.
- `PAXSVIEWER-INTEGRATION.md` deploy step (`cp deploy/paxsviewer-app-config.js app/static/viewer/app-config.js`, line 175) overwrites the served config. The served copy computes `maxNumberOfWebWorkers` dynamically (`app/static/viewer/app-config.js:20`), while the deploy copy hardcodes `3` (`deploy/paxsviewer-app-config.js:20`). The documented deploy step reverts the served behavior.
- `scripts/00_demo.py` and `01_critical_alert.py` are examples; `scripts/README.md:44-45` says scripts run with server privileges. Any admin-uploaded script is therefore code execution (intended, but document it in the admin UI).

### Verified OK (no issue found)

- No TLS verification disabled anywhere in scope (grep for `verify=False`, `CERT_NONE`, `curl -k`, `_create_unverified`): none.
- No `shell=True`, `os.system`, `eval`, or `exec` in scope scripts.
- Portal CSRF: every POST action checks `verifyCsrfToken` (`index.php:24`, `login.php:22`).
- Portal SQL: prepared statements everywhere; ids cast to int.
- Portal `download` action requires login (`index.php:13` `requireAuth()` runs before it).
- `deploy/Dockerfile` healthcheck path (`/api/v1/health/ready`) matches the route.
- `haproxy.cfg` `paxs_api` health check path and expectation match `dashboard.py:128-141`.
- Prometheus metric names used by Grafana all exist in `app/api/v1/metrics.py`.
- Both vendor public keys are identical; AES constant identical; container format and AAD match.
- MySQL is not published to the host in the LB compose.

---

## 7. Open questions

1. **Is the committed vendor private key the production signing key?** If yes, R1 is an active compromise: rotate and re-issue. Is the repo or its history public anywhere?
2. **Does `app/services/storage/` exist on the developer's machine outside this checkout?** If not, R5 means the app cannot start from this tree at all. Which commit deleted it, if any?
3. **Is `redis` installed transitively in the image?** Build the image to confirm R6 (not built here).
4. **Does current Grafana show "datasource not found" for D5?** Not rendered here.
5. **Does the PAXS admin UI expose any way to reset another user's password?** None found in scope. `users.py` PATCH covers role/active only in the greps.
6. **Is the portal's live admin hash the documented one?** The stored hash does not match `Admin1234!` (verified). What credentials does the deployed portal actually use, and was `portal.db` ever web-accessible (R8)?
7. **Does the admin panel allow arbitrary executable paths to be saved and launched?** `app/services/admin_panel_service.py:167-301` runs `subprocess` with config-supplied paths (`CONTROL-PANEL-INTEGRATION.md:54-58`). Admin-only, but intentional?
8. **Does `SCALE-OUT-GUIDE.md:93`'s claim that the worker runs `Base.metadata.create_all` hold in code?** If yes, alembic and create_all can diverge.
9. **Is `run_remote_migration.py` against a host the user owns?** The script bypasses that host's bot challenge (S7).
10. **Is `~/.gemini/config/agents` shared with other tools?** If yes, S2 is more severe.
11. **Does `validate_online` (and the activation path) have a grace period if the portal is unreachable?** `manager.py:326-328` returns False on network error; the caller ignores it (`main.py:396-404`). Confirm intent.
12. **Is `DICOMWEB_PUBLIC` meant to disable guest access?** The admin label says so, the code does not enforce it (R3). Confirm and fix.
13. **HTTP/2 claim (`PERFORMANCE.md:105-108`):** can uvicorn serve HTTP/2 at all? Not verified (uvicorn not installed locally).
14. **Is Chrome Local Network Access / Private Network Access blocking public pages from reading `127.0.0.1:8899`?** Depends on browser version and policy (S1).
15. **Who owns the `elsamanii.rf.gd` host and is `reset_admin_password.php`'s sibling `portfolio` project in use?** Both are outside this repo.
16. **Is `HL7_ENABLED=true` on all nodes intended?** The `.env.example` default is false; the compose file enables it on every node, so all nodes accept ADT/ORM.
