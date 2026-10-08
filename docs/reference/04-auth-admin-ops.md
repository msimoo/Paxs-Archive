# Authentication, users, dashboard, settings, DB / Redis / admin consoles, metrics, LDAP

> **Area deep-dive** — part of the [PAXS Archive documentation](../README.md). Produced by static analysis of commit `e90d32c` (2026-10-08); line numbers refer to that commit. Findings here are consolidated, de-duplicated and prioritized in [CODE_REVIEW.md](../CODE_REVIEW.md) — where the two differ, CODE_REVIEW.md takes precedence. Labels such as `[read]`, `[verified]`, `[V]`, `[H]` or `[inferred]` describe how each claim was checked (read in code vs. inferred).

> ⚠️ **Baseline: this deep-dive describes commit `e90d32c`.** What changed in `5a32aa4` (fixes, regressions, new code and endpoints) is in [5a32aa4/01-core-config-auth.md](5a32aa4/01-core-config-auth.md), [5a32aa4/05-clinical-ldap-frontend.md](5a32aa4/05-clinical-ldap-frontend.md). The current status of every finding is in [CODE_REVIEW.md](../CODE_REVIEW.md).

Snapshot date: 2026-10-08. All `file:line` references are to the files as read.

**Method.** Every in-scope file was read in full. `app/api/v1/deps.py`, `app/core/security.py` and `app/core/config.py` were read in full (name resolution). `app/db/models.py` was consulted by grep only (FK `ondelete`, table names). Items marked **cross-area** were verified only at the cited lines (outside area D). Severity: 🔴 blocking, 🟡 important, 🟢 nit.

**Top-line summary**

- Anonymous self-registration can pick `role=RADIOLOGIST`, a destructive role, and `ALLOW_REGISTER` defaults to `true` (F2).
- `ldap_service.py` ships a hard-coded login bypass (`ldap_*` / `domainpass`) that mints a RADIOLOGIST JWT for anyone (F1). LDAP-provisioned accounts also get a publicly known password (F3).
- Several write endpoints (org settings, logo upload, report templates) declare `get_optional_user` but never enforce it, so they are unauthenticated (F4). Their HTML is later rendered unescaped (stored XSS).
- `GET /api/v1/redis/status` is anonymous and returns the saved Redis password in plaintext (F5).
- Dashboard detail, series and thumbnail routes return PHI (including patient phone) with no auth. Tenant isolation is missing on object-level endpoints (F7, F8).
- No arbitrary SQL console and no arbitrary Redis command endpoint exist. The only custom-SQL path is a prefix-guarded `EXPLAIN` (verified, see section 5 "verified good").

---

## 1. Files covered

| Path | Lines | One-line purpose |
|---|---|---|
| app/api/v1/auth.py | 354 | Register, login (lockout), refresh (rotation + theft detection), logout, me; session cookie |
| app/api/v1/users.py | 366 | Admin user CRUD, role/active/tenant changes, session revoke, lockout reset, auth audit list |
| app/api/v1/dashboard.py | 864 | Health probes, /stats aggregation, /audit, studies list/detail, triage, thumbnails, presign, study delete |
| app/api/v1/settings.py | 206 | Organization settings + logo upload; report-template CRUD (auth not enforced) |
| app/api/v1/config.py | 103 | Runtime settings registry: GET (any user) and PATCH (admin) |
| app/api/v1/database.py | 240 | DB admin: status, test, configure (.env), init/migrate/seed, backup and download |
| app/api/v1/db_panel.py | 68 | DB Pool panel routes (perf, slow-query, EXPLAIN, replica toggle); DESTR role |
| app/api/v1/redis_panel.py | 185 | Redis panel: status, test/configure, keys/key, flush, ping, info, server lifecycle |
| app/api/v1/admin_panel.py | 70 | Control-panel bridge: status/logs/config and start/stop/restart/launch |
| app/api/v1/license.py | 169 | License status, fingerprint, reload, activate (base64/upload/online), validate |
| app/api/v1/metrics.py | 159 | Prometheus text /metrics and in-process HTTP request counter middleware |
| app/api/v1/ldap.py | 56 | LDAP/AD login route issuing PAXS JWT |
| app/services/settings_service.py | 472 | 49-key editable registry; DB overrides; runtime apply; boot replay |
| app/services/db_admin.py | 510 | DB URL parse/mask/build, .env write, alembic, mysqldump/SQLite backup, seed admin |
| app/services/db_panel.py | 321 | Pool/MySQL counters, 8 hot queries, EXPLAIN analyzer, slow-query + replica toggles |
| app/services/redis_service.py | 1096 | Redis client, L1 RAM LRU, admin ops, Windows server lifecycle, cache API |
| app/services/admin_panel_service.py | 354 | Control-panel config (JSON), port/service health, spawn/stop of local processes |
| app/services/ldap_service.py | 95 | LDAP authenticate (dead ldap3 branch + mock) and DB user sync |
| app/services/audit_service.py | 38 | `add_audit()` helper for the DICOM audit table (SHA-256 of file) |
| app/services/webhook_service.py | 66 | Fire-and-forget POST of events to env-configured URLs |
| app/api/v1/deps.py | 120 | Peeked (full): auth dependencies, role guards |
| app/core/security.py | 153 | Peeked (full): PBKDF2, HS256 JWT, refresh-token helpers, cookie helpers |
| app/core/config.py | 335 | Peeked (full): all settings and defaults |
| app/db/models.py | 1581 | Peeked (grep): User/Session/AuthAuditLog/ServerSetting FKs and table names |
| app/main.py | (grep/partial) | Cross-area: middleware order, router includes, static mounts, lifespan call to `apply_db_overrides` |
| app/api/ratelimit.py | (30-32) | Cross-area: `x-forwarded-for` trust |
| app/api/v1/reports.py | (220-260, grep) | Cross-area: public `/print/{uid}` report HTML |
| app/services/tenant_service.py | (83-95) | Cross-area: `scope_from_user` |
| app/static/login.html | (200-250) | Cross-area: login page JS (token storage, redirect) |
| app/static/settings/index.html | (grep 720-800) | Cross-area: settings UI `innerHTML` sinks |

---

## 2. API endpoint reference

**Auth column legend.** `NONE` = no auth dependency. `ANY` = `get_current_user_any` (bearer or session cookie, any role). `BEARER` = `get_current_user` (bearer header only). `OPT` = `get_optional_user` declared but route does not enforce it, so anonymous callers succeed. `ADMIN` = `require_admin` (deps.py:117-120). `DESTR` = `require_destructive_role` (ADMIN or RADIOLOGIST, deps.py:105-114).

`main.py:507-556` includes every router with no `dependencies=` argument, and `ViewerAuthGateMiddleware` covers only `/`, `/viewer`, `/studies-ui`, `/settings` (main.py:294-300). Per-route dependencies are therefore the only API auth.

### 2.1 auth (prefix `/api/v1/auth`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 1 | POST | /api/v1/auth/register | NONE (rate_limit "auth") | Body `RegisterBody{username 3-64, password 6-128, full_name?, role?}`; query `next` | 201 `{access_token, token_type, user, redirect_to}` + Set-Cookie `paxs_session` | auth.py:170-214 (def :172) |
| 2 | POST | /api/v1/auth/login | NONE (rate_limit "auth") | Body `LoginBody{username, password}`; query `next` | 200 same shape + cookie; 401 / 423 | auth.py:217-274 (def :219) |
| 3 | POST | /api/v1/auth/refresh | NONE (rate_limit; needs cookie) | query `next`; cookie | 200 token + rotated cookie; 401 | auth.py:277-331 (def :279) |
| 4 | POST | /api/v1/auth/logout | NONE (cookie optional) | cookie | 200 `{ok:true}`, cookie cleared | auth.py:334-349 |
| 5 | GET | /api/v1/auth/me | ANY | - | `{username, full_name, role, created_at}` | auth.py:352-354 |

### 2.2 users (prefix `/api/v1/users`), all ADMIN

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 6 | GET | /api/v1/users | ADMIN | - | list of user dicts (id, username, role, is_active, tenant_id) | users.py:85-90 |
| 7 | POST | /api/v1/users (201) | ADMIN | `CreateUserBody{username 3-64, password 6-128, full_name?, role="PHYSICIAN", tenant_id?}` | user dict | users.py:93-115 |
| 8 | GET | /api/v1/users/sessions | ADMIN | - | active sessions with ip, user agent, timestamps | users.py:118-150 |
| 9 | POST | /api/v1/users/sessions/{session_id}/revoke | ADMIN | path `session_id` | `{ok, username}`; 404 / 409 | users.py:153-178 |
| 10 | POST | /api/v1/users/sessions/revoke-user/{user_id} | ADMIN | path `user_id` | `{ok, username, revoked}` | users.py:181-213 |
| 11 | GET | /api/v1/users/auth-audit | ADMIN | query `action?`, `user_id?`, `limit` (1-500) | auth audit rows | users.py:216-250 |
| 12 | GET | /api/v1/users/locked | ADMIN | - | locked accounts with `remaining_minutes` | users.py:253-280 |
| 13 | POST | /api/v1/users/{user_id}/reset-lockout | ADMIN | path | `{ok, username}` | users.py:283-297 |
| 14 | PATCH | /api/v1/users/{user_id} | ADMIN | `PatchUserBody{role?, full_name?, is_active?, tenant_id?}` | user dict; 422 on guard hit | users.py:309-342 |
| 15 | POST | /api/v1/users/{user_id}/reset-password | ADMIN | `ResetPasswordBody{password 6-128}` | `{ok, username}` | users.py:345-353 |
| 16 | DELETE | /api/v1/users/{user_id} | ADMIN | path | `{ok, username}` | users.py:356-366 |

### 2.3 dashboard (prefix `/api/v1`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 17 | GET | /api/v1/health/live | NONE | - | `{status, instance}` | dashboard.py:122-125 |
| 18 | GET | /api/v1/health/ready | NONE | - | `{status, instance}` or 503 | dashboard.py:128-140 |
| 19 | GET | /api/v1/health | NONE | - | large infrastructure dump (see F33) | dashboard.py:143-220 |
| 20 | GET | /api/v1/stats | NONE (`get_read_db`) | - | counters incl. `users`, audit events, storage bytes, feature blocks | dashboard.py:223-383 |
| 21 | GET | /api/v1/audit | DESTR | query `limit` (1-200) | audit rows (sop/study uid, sha256, performed_by) | dashboard.py:386-412 |
| 22 | GET | /api/v1/studies | OPT (guest = unscoped data only; ADMIN all; others own tenant) | query `page`, `limit` (1-100), `search`, `modality`, `from`, `to`, `sort` (date/patient/accession/series/instances/modality/created), `order`, `state`, `critical`, `reported`, `oru`, `his`, `tenant` | `{items, total, page, limit, sort, order, counts}` | dashboard.py:415-640 |
| 23 | GET | /api/v1/triage | ANY (no tenant scope) | - | up to 50 unreviewed findings incl. patient_name, patient_id | dashboard.py:643-681 |
| 24 | POST | /api/v1/triage/{flag_id}/review | ANY | path | `{ok, id}` | dashboard.py:684-694 |
| 25 | GET | /api/v1/studies/{study_uid} | NONE | path | `{patient{name,id,birth_date,sex,phone,source}, study{...}, series[...]}` | dashboard.py:697-748 |
| 26 | GET | /api/v1/studies/{study_uid}/series/{series_uid}/instances | NONE | path | instance metadata (sop uid, rows, cols, transfer syntax, size) | dashboard.py:751-770 |
| 27 | GET | /api/v1/studies/{study_uid}/series/{series_uid}/thumbnail.png | NONE (generates + stores thumbnail on cache miss) | path | `image/png` | dashboard.py:773-801 |
| 28 | GET | /api/v1/storage/presign/{study_uid}/{series_uid}/{sop_uid} | BEARER | path | `{sop_instance_uid, backend, url, expires_in}` | dashboard.py:804-831 |
| 29 | DELETE | /api/v1/studies/{study_uid} (204) | DESTR | path | 204; deletes files + DB rows | dashboard.py:834-864 |

### 2.4 settings and templates (no prefix; explicit paths)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 30 | GET | /api/v1/settings/organization | NONE | - | org settings dict (names, header/footer HTML, phone, email, address, logo_path) | settings.py:49-70 |
| 31 | POST | /api/v1/settings/organization | OPT (not enforced) | form: org_name_ar/en, header_html_text, footer_html_text, phone, email, address; file `logo_file` | `{ok, message, logo_path}` | settings.py:73-112 (dep :84) |
| 32 | GET | /api/v1/templates | NONE | query `modality?` | list of report templates | settings.py:118-134 |
| 33 | POST | /api/v1/templates (201) | OPT (not enforced) | `TemplateIn{title 1-128, modality?, body_findings_template?, default_impression_template?, is_default}` | template | settings.py:137-154 (dep :141) |
| 34 | GET | /api/v1/templates/{template_id} | NONE | path | template | settings.py:157-169 |
| 35 | PATCH | /api/v1/templates/{template_id} | OPT (not enforced) | `TemplatePatch` | template | settings.py:172-193 (dep :177) |
| 36 | DELETE | /api/v1/templates/{template_id} | OPT (not enforced) | path | `{ok, message}` | settings.py:196-206 (dep :200) |

### 2.5 config (prefix `/api/v1/config`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 37 | GET | /api/v1/config | ANY | - | `{admin, groups[fields], readonly: <health payload>}` | config.py:46-73 (dep :47) |
| 38 | PATCH | /api/v1/config | ADMIN | `ConfigPatch{updates: {KEY: value}}` | `{ok, applied[], errors[], partial?}`; 422 if nothing applied | config.py:76-103 (dep :77) |

### 2.6 database (prefix `/api/v1/db`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 39 | GET | /api/v1/db/status | ANY | - | backend, url_masked, host, port, database, revisions, saved (public subset), backup_count, backup_dir, `admin` | database.py:77-81 |
| 40 | POST | /api/v1/db/test | ADMIN | `TestBody{backend, host, port, user, password, name}` | `{ok, latency_ms, version, database_exists}` | database.py:84-92 |
| 41 | POST | /api/v1/db/configure | ADMIN | `ConfigureBody{...mysql fields, persist_env=true}` | `{ok, url_masked, restart, env_path, note}` | database.py:95-119 |
| 42 | POST | /api/v1/db/init | ADMIN | `InitBody{url?, backend?, host?, port?, user?, password?, name?, use_saved=true, seed_admin=true, admin_username="admin", admin_password="admin123"}` | `{ok, ..., log[], seeded}`; 500 with log | database.py:122-208 |
| 43 | POST | /api/v1/db/backup | ADMIN | - | `{ok, file, size, backend, created_at}` | database.py:211-214 |
| 44 | GET | /api/v1/db/backups | ADMIN | - | `[{file,size,mtime}]` | database.py:217-219 |
| 45 | GET | /api/v1/db/backups/{name} | ADMIN | path | file download (full DB dump) | database.py:222-230 |
| 46 | DELETE | /api/v1/db/backups/{name} | ADMIN | path | `{ok, file}` | database.py:233-240 |

### 2.7 DB pool panel (prefix `/api/v1/db/perf`), all DESTR

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 47 | GET | /api/v1/db/perf | DESTR | - | pool stats, MySQL counters, replica status, slow-query state | db_panel.py:36-39 |
| 48 | POST | /api/v1/db/perf/slow-query | DESTR | `SlowQueryBody{enabled?, threshold_ms?}` | `{enabled, threshold_ms}` | db_panel.py:42-45 |
| 49 | GET | /api/v1/db/perf/slow-queries | DESTR | - | ring-buffer entries | db_panel.py:48-50 |
| 50 | GET | /api/v1/db/perf/explain | DESTR | query `name?`, `sql?` (GET, so SQL sits in the URL) | `{backend, name, sql, columns, rows}`; 422 on guard | db_panel.py:53-59 |
| 51 | POST | /api/v1/db/perf/replica | DESTR | `ReplicaBody{enabled}` | `{configured, enabled}` | db_panel.py:62-68 |

### 2.8 Redis panel (prefix `/api/v1/redis`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 52 | GET | /api/v1/redis/status | OPT (anonymous allowed; response includes `admin` flag only) | - | full status incl. **`saved` (plaintext password, F5)** | redis_panel.py:63-69 (dep :64) |
| 53 | POST | /api/v1/redis/test | ADMIN | `TestBody{host, port, db, password, ssl}`; blank password falls back to saved | probe result | redis_panel.py:72-78 |
| 54 | POST | /api/v1/redis/configure | ADMIN | `ConfigureBody{host, port, db, password, enabled, ssl, persist_env}` | `{ok, url_masked, enabled, env_path, note}` | redis_panel.py:81-92 |
| 55 | GET | /api/v1/redis/keys | OPT (anonymous allowed) | query `pattern` (default `*`), `limit` (1-500) | `{ok, pattern, count, limit, keys[{key,type,ttl,size_bytes,tier}]}` | redis_panel.py:95-102 (dep :99) |
| 56 | GET | /api/v1/redis/key | OPT (anonymous allowed) | query `key` (required) | `{ok, key, tier, type, ttl, size_bytes, preview}`; 404 | redis_panel.py:105-114 (dep :108) |
| 57 | DELETE | /api/v1/redis/key | ADMIN | `DeleteKeyBody{key}` | `{ok, key, deleted}` | redis_panel.py:117-121 |
| 58 | POST | /api/v1/redis/flush | ADMIN | `FlushBody{target: paxs\|pattern\|l1\|all, pattern="paxs:*"}` | result; 500 on error | redis_panel.py:124-132 |
| 59 | POST | /api/v1/redis/ping | OPT (anonymous allowed) | - | `{ok, ping, latency_ms}` | redis_panel.py:135-138 |
| 60 | GET | /api/v1/redis/info | ADMIN | query `section?` | `{ok, info}`; 502 | redis_panel.py:141-150 |
| 61 | POST | /api/v1/redis/server/start | ADMIN | - | result; 500 | redis_panel.py:154-160 |
| 62 | POST | /api/v1/redis/server/stop | ADMIN | - | `{ok, message}` | redis_panel.py:163-167 |
| 63 | POST | /api/v1/redis/server/restart | ADMIN | - | result; 500 | redis_panel.py:170-176 |
| 64 | POST | /api/v1/redis/server/install | ADMIN | - | result; downloads and starts a binary (F35); 500 | redis_panel.py:179-185 |

### 2.9 Admin / control panel (prefix `/api/v1/admin-panel`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 65 | GET | /api/v1/admin-panel/status | ANY | - | `{orthanc, proxy, api, adminFront, mysql, lastChecked}` (port checks + `sc query`) | admin_panel.py:27-30 |
| 66 | GET | /api/v1/admin-panel/logs | ANY | - | list of up to 500 log strings | admin_panel.py:33-35 |
| 67 | GET | /api/v1/admin-panel/config | ANY | - | config (ports, executable/dir paths) | admin_panel.py:38-40 |
| 68 | POST | /api/v1/admin-panel/config | ADMIN | body dict (free-form) | `{ok, config}` or `{ok:false, error}` | admin_panel.py:43-49 |
| 69 | POST | /api/v1/admin-panel/actions/{action} | ADMIN | action in `start-all`, `stop-all`, `restart` (body.service whitelisted), `launch-viewer`, `launch-admin`, `open-viewer`, `open-admin` | action result | admin_panel.py:52-70 |

### 2.10 license (prefix `/api/v1/license`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 70 | GET | /api/v1/license | NONE | - | `LicenseStatusResponse` (incl. customer_name, customer_organization, customer_email, machine_fingerprint) | license.py:48-54 |
| 71 | GET | /api/v1/license/status | NONE | - | same | license.py:49-54 |
| 72 | GET | /api/v1/license/fingerprint | NONE | - | `{fingerprint, hardware_info}` | license.py:57-63 |
| 73 | POST | /api/v1/license/reload | ADMIN | - | `{success, status, message, is_active}` | license.py:66-78 |
| 74 | POST | /api/v1/license/activate | ADMIN | `LicenseActivateRequest{license_b64}` | `{success, message, summary}`; 400 | license.py:81-104 |
| 75 | POST | /api/v1/license/upload | ADMIN | multipart `file` (no size cap in route) | `{success, message, summary}`; 400 | license.py:107-126 |
| 76 | POST | /api/v1/license/activate-online | ADMIN | `{activation_key, customer_email?, portal_url?}` (portal_url is caller-supplied) | `{success, message, summary}`; 400 | license.py:135-154 |
| 77 | POST | /api/v1/license/validate-online | ADMIN | - | `{success, message, summary}` | license.py:157-168 |

### 2.11 metrics and LDAP

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 78 | GET | /metrics | NONE | - | `text/plain; version=0.0.4` (instance, version, app name, request count, queue depths, replica lag) | metrics.py:87-159 (route :87) |
| 79 | POST | /api/v1/ldap/login | NONE (no rate limit, no audit) | `LDAPLoginRequest{username, password}` | `{access_token, token_type, username, role, full_name, auth_provider}`; 401 | ldap.py:29-56 (decorator :29) |

Total: 79 routes (plus `/api/v1/license` alias of `/status`). Also mounted, outside area D: `/api/docs` and `/api/openapi.json` (main.py:426-427).

---

## 3. Service reference

### 3.1 settings_service.py (runtime-editable configuration)
- **Purpose.** Curated registry of editable settings. Live value = DB override in `server_settings` (key = setting name) falling back to the env default in `app.core.config.settings`. Overrides are applied to the in-process settings object.
- **Registry.** `EDITABLE_KEYS: dict` at :37-328, **49 keys** in 6 groups (sys 4, dicom 6, interop 11, storage 9, security 9, workflow 10). Groups at :330-337. Secrets (`SECRET_KEY`, `DATABASE_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `REDIS_URL`) are **not** in the registry.
- **Key functions.**
  - `_parse(key: str, raw: str) -> Any` (:340-349).
  - `_coerce(key: str, value: Any) -> str` (:352-378). Type checks only; no range limits for `int`.
  - `current_value(key: str) -> Any` (:388-396); `is_overridden(key) -> bool` (:399-401).
  - `set_override(key: str, value: Any) -> Any` (:405-424): coerce, parse, upsert `ServerSetting`, `_apply_to_runtime`.
  - `_apply_to_runtime(key, parsed) -> None` (:427-432): `setattr(settings, key, parsed)`.
  - `overrides_map() -> dict[str, Any]` (:435-451): one query.
  - `apply_db_overrides() -> int` (:454-472): boot replay, called from `main.py:386`.
- **DB models.** `ServerSetting(key, value)`.
- **Redis keys.** None.
- **External.** None.

### 3.2 db_admin.py (database administration engine, sync functions run via `asyncio.to_thread`)
- **Purpose.** URL parse/mask/build, `.env` persistence of `DATABASE_URL`, alembic state, migrations to arbitrary target URLs, MySQL probe and `CREATE DATABASE`, backups, seed first admin.
- **Key functions.**
  - `parse_db_url(url) -> dict` (:51-75); `build_mysql_url(cfg: dict) -> str` (:78-86); `masked_url(url) -> str` (:89-95).
  - `saved_config() -> dict|None` (:99-110); `save_config(cfg) -> None` (:113-132; stores non-secret fields and `has_password` only); `saved_password() -> str` (:135-143, reads the password back from `.env`).
  - `read_env() -> dict` (:171-182); `write_env_url(url) -> Path` (:185-200).
  - `head_revision() -> str|None` (:204-213); `current_revision(url) -> str|None` (:216-227); `run_migrations(url, stamp_on_exists=True) -> (list[str], bool)` (:237-274, mutates `settings.DATABASE_URL` temporarily).
  - `ensure_database(url) -> bool` (:277-299; name regex `_NAME_RE` at :46).
  - `test_mysql_connection(cfg) -> dict` (:303-342).
  - `create_backup() -> dict` (:420-438); `_dump_mysql_mysqldump` (:367-382, password via `MYSQL_PWD` env); `_dump_mysql_sqlalchemy` (:385-417); `_backup_sqlite` (:352-364); `backup_path(name) -> Path` (:441-448); `list_backups()` (:451-458).
  - `db_status() -> dict` (:462-484); `seed_admin(url, username, password, full_name=None) -> dict` (:488-510, creates ADMIN only if `users` is empty on the target).
- **DB models.** `ServerSetting` (key `db_config`), `User` (seed).
- **External.** MySQL (pymysql, SQLAlchemy NullPool engines), `mysqldump` subprocess, alembic, local filesystem `.env` and `BACKUP_DIR`.

### 3.3 db_panel.py (DB Pool panel)
- **Purpose.** Pool metrics, MySQL global counters, read-replica status and toggle, slow-query ring buffer, and an EXPLAIN analyzer.
- **Key functions.**
  - `_get_setting(key) -> str|None` (:42-49); `_set_setting(key, value)` (:52-63).
  - `_mysql_counters() -> dict|None` (:70-105, sync pymysql, 3 s connect timeout).
  - `HOT_QUERIES` (:116-183), 8 fixed statements.
  - `explain_query(name: str = "", custom_sql: str = "") -> dict` (:190-234).
  - `async perf_panel() -> dict` (:241-293). Calls sync DB and pymysql inside an `async def` (see F29).
  - `set_slow_query(enabled, threshold_ms) -> dict` (:296-305); `set_replica(enabled) -> dict` (:308-313); `slow_queries()` (:316-317); `has_replica()` (:320-321).
- **DB models.** `ServerSetting` keys `dbp_read_replica`, `dbp_slow_query`, `dbp_slow_threshold_ms`.
- **External.** MySQL (`SHOW GLOBAL STATUS`, `SHOW GLOBAL VARIABLES`), read replica via `replica_probe()` (session.py, not read).

### 3.4 redis_service.py (Redis client, L1 cache, admin ops)
- **Purpose.** Shared redis-py client with fallback to a process-local LRU (`_MEM_CACHE`, max 4000 items / 256 MB, :43-48). Admin helpers for keys, flush, Windows service/process lifecycle and portable install. Cache API used by the DICOMweb and WADO code paths.
- **Key functions.**
  - URL helpers: `parse_redis_url(url) -> dict` (:52-90), `build_redis_url(cfg) -> str` (:93-109, host not quoted), `masked_redis_url(url) -> str` (:112-127).
  - Persistence: `saved_redis_password() -> str` (:149-163), `saved_redis_config() -> dict|None` (:166-177), `write_env_redis(url, enabled) -> Path` (:180-204), `save_redis_config(cfg, persist_env=True) -> dict` (:207-271).
  - Client pool: `reset_redis_client(new_url=None, enabled=None)` (:275-292, setattr on settings), `get_redis_client() -> redis.Redis|None` (:295-320, socket timeout 1.5 s).
  - Lifecycle: `get_redis_server_installation() -> dict` (:344-412; runs `sc query` x2 and `tasklist`), `start_redis_server() -> dict` (:415-510), `stop_redis_server() -> dict` (:513-552), `restart_redis_server()` (:555-559), `install_redis_portable() -> dict` (:562-596).
  - Probes/status: `test_redis_connection(cfg: dict|str|None=None) -> dict` (:600-643), `ping_redis() -> dict` (:646-657), `get_redis_status() -> dict` (:660-756), `get_redis_raw_info(section=None) -> dict` (:759-774).
  - Keys: `list_redis_keys(pattern, limit) -> dict` (:778-853, SCAN), `get_key_detail(key) -> dict` (:856-923), `delete_redis_key(key) -> bool` (:926-942), `flush_redis_cache(target, pattern) -> dict` (:945-995: `all` = FLUSHDB, `pattern`/`paxs` = KEYS + DEL).
  - L1: `get_l1_cache_stats()` (:999-1012), `flush_l1_cache()` (:1015-1022).
  - Cache API: `cache_get(key) -> bytes|None` (:1026-1051), `cache_set_mem(key, value, expire_seconds)` (:1054-1075, skips values over 16 MB), `cache_set(key, value, expire_seconds) -> bool` (:1078-1091), `cache_delete_pattern(pattern) -> int` (:1094-1096).
- **DB models.** `ServerSetting` keys `redis_config` (JSON including **password**), `REDIS_URL` (URL including **password**), `REDIS_CACHE_ENABLED`.
- **Redis keys used by the cache API.** `paxs:frame:{sop_uid}:{n}` (wado_rs_bulk_service.py:162,196; dicomweb.py:730; warm_service.py:172), `paxs:usage:{backend}` (storage_service.py:215-241), plus DICOMweb rendered/frame cache keys (dicomweb.py:447, 500, 567, 586, 690, 729; key format not read). Key names therefore contain SOP instance UIDs.
- **External.** Redis server; Windows `sc`, `net`, `tasklist`, `taskkill`; GitHub zip download (`https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip`, :577); subprocess spawn of `redis-server.exe`.

### 3.5 admin_panel_service.py (control-panel process and health service)
- **Purpose.** Health checks (TCP connects, `sc query` / `systemctl`), config JSON (`admin_panel.json`), start/stop of locally configured Orthanc, viewer proxy, admin API, and admin frontend.
- **Key functions.** `get_admin_panel_service() -> AdminPanelService` (:349-354, singleton). Methods: `config_path` (:85-89), `_load_config`/`_validate` (:91-116), `save_config(cfg: dict) -> dict` (:123-135), `_check_port(port, host, timeout)` (:153-161), `_check_mysql(service_name)` (:163-186), `snapshot()` (:188-197), `_resolve(path)` (:202-217, includes hard-coded `D:/Github`), `_spawn(service, argv, cwd)` (:219-233), `start_service(service)` (:243-280), `stop_service(service)` (:282-306, `taskkill` on tracked PIDs only), `restart_service`, `start_all`, `stop_all`, `launch(target)` (:330-342, `webbrowser.open` on the server).
- **DB models.** None.
- **Files.** `admin_panel.json` at `ADMIN_PANEL_CONFIG_FILE` or `Path(STORAGE_ROOT).parent / "admin_panel.json"` (:85-89).
- **External.** TCP to localhost ports, `sc`, `systemctl`, `pgrep`, `taskkill`, `python`, `node`, `npm` subprocesses.

### 3.6 ldap_service.py
- **Purpose.** Authenticate directory users and provision/sync a local `User` row.
- **Key functions.** `authenticate_ldap_user(username, password) -> LDAPUserIdentity|None` (:32-69): ldap3 bind branch guarded by `getattr(settings, "LDAP_SERVER", None)` (:39-40, never set, see F1); then a mock branch at :59-67. `async sync_ldap_user_to_db(session, identity) -> User` (:72-95): overwrites `full_name`/`role` for existing rows (:79-83); creates rows with `hash_password("ldap_managed_password")` (:86-93).
- **DB models.** `User`.
- **External.** LDAP/AD via ldap3 (dead branch).

### 3.7 audit_service.py
- **Purpose.** Append `AuditLog` rows (DICOM audit trail) with SHA-256 of the file.
- **Key functions.** `add_audit(session, sop_instance_uid, action, performed_by=None, study_instance_uid=None, file_path=None, sha256=None) -> None` (:16-38). Reads the whole file synchronously when `file_path` is given (:25-29, `storage_service.sha256_of`).
- **DB models.** `AuditLog`.
- **External.** Filesystem read.

### 3.8 webhook_service.py
- **Purpose.** Fire-and-forget event POST to env-configured URLs.
- **Key functions.** `configured_urls() -> list[str]` (:26-28, `settings.WEBHOOK_URLS`, env only, not in the registry); `_deliver(url, payload) -> None` (:31-50, `urllib` POST, 8 s timeout, 2 attempts, follows redirects); `fire(event, **data) -> None` (:53-66, one daemon thread per URL per event).
- **Callers.** `critical_results_service.py:72` (`CriticalResultFlagged` with patient_id and description), `indexing_service.py:128` (`StudyReceived` with patient_name), `dashboard.py` uses `script_fire` (not webhook).
- **External.** Outbound HTTP to configured URLs.

### 3.9 Auth-related helpers (for context; in deps.py and security.py)
- `deps.py`: `get_current_user_from_cookie(request) -> User|None` (:17-43), `get_current_user(credentials) -> User` (:46-63, bearer only), `get_optional_user(request, credentials) -> User|None` (:66-86), `get_current_user_any(request, credentials) -> User` (:89-101), `require_destructive_role` (:108-114), `require_admin` (:117-120).
- `security.py`: `set_session_cookie(response, token)` (:29-39), `clear_session_cookie` (:42-44), `generate_refresh_token(family_id)` (:50-58), `hash_refresh_token` (:61-63), `refresh_token_family` (:66-72), `session_token_from_cookie` (:75-79, **unused**), `hash_password` (:88-96), `verify_password` (:99-109), `create_access_token(subject, role, secret, expires_minutes)` (:123-138), `decode_token(token, secret) -> dict|None` (:141-153).

---

## 4. Workflows

### (a) Login (password check, lockout, token and cookie issue)
1. `POST /api/v1/auth/login` runs rate_limit "auth" (auth.py:218; `RATE_LIMIT_LOGIN_PER_MINUTE` default 10, config.py:163). Client IP is taken from `x-forwarded-for` when present (ratelimit.py:30-32, cross-area), so the limit is spoofable.
2. User lookup by username (auth.py:220-222).
3. **Unknown username**: immediate `401 "Invalid username or password"` (auth.py:224-226). No PBKDF2 work is done, which is a timing oracle (F11).
4. **Locked account**: if `locked_until > now`, write `login_failed {reason: locked}`, commit, return `423` with remaining minutes (auth.py:228-236). The password is not checked while locked.
5. `verify_password` (auth.py:238 -> security.py:99-109): PBKDF2-HMAC-SHA256, 200,000 iterations, per-user 16-byte salt, `hmac.compare_digest`.
6. **Wrong password**: `failed_attempts += 1` (auth.py:240). If `>= MAX_FAILED_ATTEMPTS` (default 5, config.py:128) set `locked_until = now + 15 min` (auth.py:242), audit `account_locked`, commit, `423` (auth.py:243-251). Otherwise audit `login_failed`, commit, `401` (auth.py:252-255). The counter is never reset when a lock expires, so the next failure re-locks immediately (F14).
7. **Disabled account**: checked only after the password matches, giving a password oracle for disabled users (auth.py:257-260).
8. **Success**: reset `failed_attempts` and `locked_until`, set `last_login` and `last_login_ip` (auth.py:263-266), audit `login_success` (auth.py:267).
9. Access token: HS256 JWT `{sub: username, role, iat, exp = now + TOKEN_EXPIRE_MINUTES (15)}` (auth.py:269-270; security.py:123-138). The `role` claim is not used for authorization; `deps.py` reloads the user row each request.
10. Refresh session: `family_id = uuid4().hex`; refresh token `"<family>.<token_urlsafe(32)>"`; only its SHA-256 is stored in `sessions` with ip, user agent (truncated to 500) and `expires_at = now + 7 days` (auth.py:144-158; security.py:50-63).
11. Response: JSON `{access_token, token_type, user, redirect_to}` (auth.py:161-167). `redirect_to = role_landing(role, ?next)` (auth.py:66-72). The check is `startswith("/") and not startswith("//")` plus newline rejection, which misses `/\host` (F12).
12. Cookie via `set_session_cookie` (security.py:29-39): `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age=7d`, `Secure=AUTH_COOKIE_SECURE` (default **False**, config.py:158).
13. Client (login.html:219-242): posts to `/api/v1/auth/login?next=...`, stores the access token in `localStorage["paxs_token"]` (login.html:241), then `window.location.href = data.redirect_to || next || "/"` (login.html:242).

### (b) Logout and session revocation
1. `POST /api/v1/auth/logout` (auth.py:334-349): hash the cookie, look up the session, set `is_revoked=True`, `revoke_reason="logout"`, audit `logout`, then clear the cookie (`delete_cookie`, security.py:42-44). The endpoint takes no auth, so cross-site POSTs carry no cookie (Lax).
2. The **access token is not revoked**. `get_current_user` (deps.py:46-63) checks signature, expiry and `is_active` only, so a bearer token remains usable until `exp` (up to 15 min).
3. Cookie-based checks do see revocation: `get_current_user_from_cookie` returns None for revoked sessions (deps.py:36-37). `ViewerAuthGateMiddleware` validates entry points against the DB (main.py:321-330).
4. **Reuse / theft**: in `refresh`, a missing or revoked session row means a rotated token was replayed. The handler calls `_revoke_family(family)` (auth.py:129-141), audits `token_theft_detected`, commits, and returns 401 (auth.py:294-306).
5. Expired session: marked `revoke_reason="expired"` and 401 (auth.py:308-312).
6. **Rotation**: new refresh token, same family; `expires_at` is re-extended on every refresh (sliding 7-day window, auth.py:320-324). A stolen cookie stays valid as long as it is used within each 7-day window.
7. **Admin per-session revoke** (users.py:153-178): sets `revoke_reason="admin"`, audits `session_revoked`.
8. **Admin bulk revoke for a user** (users.py:181-213): skips the admin's own sessions when targeting self, audits `sessions_revoked_all`.
9. **Disable user** (users.py:336-337): no session rows are touched. Cookies and bearer tokens are rejected by the `is_active` checks (deps.py:41-42, 60-62; auth.py:317-318).
10. **Admin password reset** (users.py:345-353): does not revoke sessions (F16).
11. **Concurrency gap**: `refresh` reads the session without `SELECT ... FOR UPDATE` (auth.py:289-292). Two concurrent refreshes with the same cookie both rotate, and the second commit overwrites the hash. The client keeps one token; the next refresh with the other token triggers family revocation (F15).

### (c) User CRUD and role change
1. Every route is `require_admin` (deps.py:117-120). The dependency resolves the user from bearer or cookie (`get_optional_user`, deps.py:66-86) and then checks role from the DB row, not the JWT claim.
2. **Create** (users.py:93-115): role must be in `ROLES` (users.py:96-97, auth.py:51); username uniqueness is checked with a read (users.py:98-102, race possible); `hash_password`; `flush`; `refresh`. **No audit row** (F17).
3. **Patch** (users.py:309-342): role validated (315-316); self-disable forbidden (319-320); self-demotion forbidden (321-322); last-ADMIN guard for another user's disable or demote (325-328) uses `_active_admin_count` with `FOR UPDATE` (52-62). Role change writes `role_changed {old, new}` with **no actor** (330-333). `is_active` changes are **not audited** (336-337). `tenant_id` is applied only when present in the body (339-340).
4. **Delete** (users.py:356-366): self-delete forbidden (360-361). Last-ADMIN guard (362-363) counts active admins, so deleting an inactive ADMIN is refused when only one active admin exists (F42). FK cascade: `sessions.user_id ON DELETE CASCADE` (models.py:150); `auth_audit_log.user_id ON DELETE SET NULL` (models.py:183). **No audit row** (F17).
5. **Reset password** (users.py:345-353): no current-password requirement; `hash_password`; audit `password_change` with **no actor** and **no session revocation** (F16).
6. **Self-registration role choice** (auth.py:183-191): any role except ADMIN (including RADIOLOGIST) is accepted when the DB already has users (F2).

### (d) Changing server settings and how they apply at runtime
1. `GET /api/v1/config` (config.py:46-73; any authenticated user) builds groups from the registry, using `overrides_map()` (one query) and `getattr(settings, key)` for defaults. It also returns the `health()` payload as `readonly`.
2. `PATCH /api/v1/config` (config.py:76-103; ADMIN): for each key, reject unknown keys (84-86), then `set_override` (settings_service.py:405-424).
3. `_coerce` (settings_service.py:352-378) type-checks only. `int` has no bounds (363-368), and `str` has no format check.
4. Persist: upsert `ServerSetting(key, value)` and commit, synchronously (settings_service.py:415-421).
5. Runtime apply: `setattr(settings, key, parsed)` (settings_service.py:427-432). This affects the **current process only**.
6. Boot replay: `apply_db_overrides()` runs from `main.py:386` (settings_service.py:454-472).
7. Read timing:
   - Read live at call time: `ALLOW_REGISTER` (auth.py:173), `TOKEN_EXPIRE_MINUTES` (auth.py:208, 269, 327), `REFRESH_TOKEN_EXPIRE_DAYS` for session expiry (auth.py:115), `AUTH_COOKIE_SECURE` (security.py:37), `AUTH_GATE_ENABLED` (main.py:291), `PUBLIC_BASE_URL` (dashboard.py:824, admin_panel_service.py:332).
   - **Captured at import, so runtime edits do not take effect**: login and register and refresh rate-limit arguments (auth.py:171, 218, 278), global rate limit (main.py:442), and cookie `Max-Age` (security.py:26 `COOKIE_MAX_AGE`).
8. Keys flagged `restart=True` (for example DICOM receiver, HL7 listener, STORAGE_BACKEND, S3_BUCKET) are also written to the live object, so the UI's "restart required" hint is the only signal they bind at startup.
9. **Multi-node**: there is no cross-node notification. The config.py:13-16 comment describes a load-balanced deployment. Each node picks up the new value only on restart or when it replays overrides at boot (F23).

### (e) DB admin panel and "query execution"
- There is **no general SQL console** in area D. The only user-supplied SQL path is `GET /api/v1/db/perf/explain` (db_panel.py:53-59, DESTR), backed by `explain_query` (services/db_panel.py:190-234).
1. Input: either `name` (must match one of 8 `HOT_QUERIES`, services/db_panel.py:198-201) or `sql` (non-empty, :202-205). `name` takes precedence.
2. Normalize: `rstrip`; remove one trailing `;`; reject any remaining `;` (:207-211).
3. Read-only guard: `stripped = sql.lstrip().upper()` must start with `SELECT` or `EXPLAIN` (:214-216).
4. Execute: a fresh NullPool engine on `to_sync_url(settings.DATABASE_URL)` (:218-219). It runs `text(f"EXPLAIN {sql}")` on MySQL (:222-223) or `text(f"EXPLAIN QUERY PLAN {sql}")` on SQLite (:225). Plain EXPLAIN does not execute the statement. `EXPLAIN ANALYZE` is not reachable, because `ANALYZE` cannot be the first token.
5. Result: `columns` and `rows` of the plan (:226-232). Engine disposed in `finally` (:233-234).
- Gaps (F37): the guard is prefix-based. The connection uses the **primary application credentials**, not a read-only account. The SQL travels in the query string (`db_panel.py:54`), so it lands in access logs.
- Separately, `/api/v1/db/status` (database.py:77-81, ANY) returns host, port, DB user, database name and `backup_dir` to any authenticated role (F33).
- DB init and configure (database.py:95-208): ADMIN only. They accept an arbitrary target URL, run `CREATE DATABASE`, run alembic migrations and optionally seed `admin/admin123` (F28, F24).

### (f) Redis panel operations
1. **Status** (redis_panel.py:63-69, OPT): `get_redis_status` (redis_service.py:660-756) calls `saved_redis_config()` (:668), `get_redis_server_installation()` (:669, spawns `sc query` x2 and `tasklist`), a probe (:671), and returns `saved` (:748). It is reachable anonymously (F5, F30).
2. **Keys** (redis_panel.py:95-102, OPT): `list_redis_keys` scans L1 then Redis with `SCAN ... MATCH pattern COUNT 100` (redis_service.py:812). It loops until `limit` or cursor 0 (:811-842), so a pattern that matches nothing scans the whole keyspace in one request (F6).
3. **Key detail** (redis_panel.py:105-114, OPT): `get_key_detail` returns TTL, type and a preview (redis_service.py:856-923): first 256 bytes for strings of 512 bytes or more, first 10 members for zset/hash/list (`HGETALL` loads the whole hash, :904).
4. **Delete** (redis_panel.py:117-121, ADMIN): `delete_redis_key` from L1 and Redis (redis_service.py:926-942).
5. **Flush** (redis_panel.py:124-132, ADMIN): `target=l1` clears the RAM LRU. `target=all` clears L1 and runs `FLUSHDB` on the configured DB index (redis_service.py:954-963). `target=paxs|pattern` deletes L1 matches and runs `KEYS pat` then `DEL` (redis_service.py:966-983). `KEYS` is O(N) and blocks Redis (F34).
6. **Ping** (redis_panel.py:135-138, OPT): `PING` with latency.
7. **Info** (redis_panel.py:141-150, ADMIN): `info(section)` (redis_service.py:759-774). `section` is passed as the INFO argument, not as a raw command.
8. **Configure** (redis_panel.py:81-92, ADMIN): `save_redis_config` (redis_service.py:207-271) stores `redis_config` JSON **including the password** (:219-226, 231-238) and `REDIS_URL` **including the password** (:241-247). It writes `.env` (:259-261) and resets the client (:264).
9. **Test** (redis_panel.py:72-78, ADMIN): a blank password is replaced with `saved_redis_password()` (:76-77). The probe then connects to the **submitted host** with the saved password (F25).
10. **Server lifecycle** (redis_panel.py:154-185, ADMIN): `start` probes, then `net start` (redis_service.py:435-442), then direct `Popen` of a found exe (:462-482), then waits up to 4 s. `stop` sends `SHUTDOWN NOSAVE`, `net stop`, and `taskkill /F /IM` by image name (:515-549). `install` downloads a zip from GitHub without a checksum (:576-596) and starts the exe (F35).
11. No raw command endpoint exists. No `CONFIG SET`, `FLUSHALL`, `EVAL` or `DEBUG` path was found in area D.

### (g) License activation and validation (router only; core/licensing.py not read)
1. Status and fingerprint are anonymous (license.py:48-63). The response model exposes `customer_name`, `customer_organization`, `customer_email` and `machine_fingerprint` (license.py:33-35, 43) when populated (F33).
2. Mutations are ADMIN: `reload` (66-78) calls `lic.load_license()`; `activate` (81-104) base64-decodes and calls `lic.activate_from_bytes`; `upload` (107-126) reads the whole file with no size cap; `activate-online` (135-154) passes `portal_url` straight through to `lic.activate_online` (caller-supplied URL, see open questions); `validate-online` (157-168) triggers a heartbeat.
3. Bytes are parsed and verified in `app/core/licensing.py`, which was not read. Signature verification and `portal_url` validation therefore could not be confirmed.

### (h) Prometheus metrics exposure
1. `GET /metrics` (metrics.py:87-159) is anonymous.
2. `MetricsMiddleware` (metrics.py:42-56) increments a module-level counter for every `http` scope. It is registered at main.py:449, inside the viewer gate and outside gzip.
3. The body is built as text (metrics.py:95-99): `paxs_up 1`, `paxs_info{instance,version,app}` (labels escaped by `_esc`, :59-61), `paxs_http_requests_total`.
4. DB gauges (metrics.py:105-126): `COUNT(*)` of `CompressionJob` PENDING and RUNNING and `RoutingQueueItem` by status, on every scrape (default 15 s). On DB error, zeros are emitted and `paxs_db_up 0`.
5. Replica (metrics.py:139-149): `replica_probe()` lag and connected flags.
6. Response `text/plain; version=0.0.4`, `Cache-Control: no-store` (metrics.py:154-158).

### (i) Dashboard data aggregation
1. **`/api/v1/stats`** (dashboard.py:223-383, anonymous, read replica):
   - About 45 sequential awaited `COUNT(*)` queries: Patient, Study, Series, Instance, User, DicomNode, PatientShare (active), CompressionJob, worklist, MPPS, commitments, critical results, routing (rules and queue), lossy, lifecycle, migration, KIN, triage, protocols, AuditLog, reports, tenants, replication (dashboard.py:231-366).
   - Reading worklist via `reader_counts` in a try block (:286-298).
   - Storage bytes via `asyncio.to_thread(storage_service.cached_storage_usage_bytes)` with a TTL cache (:314-316, `STATS_USAGE_CACHE_SECONDS` default 60).
   - `codec_status()` and `_script_count()` (`scripting_service.list_scripts()`, synchronous, :103-107, 357-360).
2. **`/api/v1/studies`** (dashboard.py:415-640):
   - Scope: `scope_from_user` (dashboard.py:439) gives ADMIN all tenants, other users their tenant, and anonymous only unscoped rows (tenant_service.py:83-95).
   - Search: parameterized `LIKE '%term%'` on four columns (:454-463), so it cannot be indexed.
   - Counters: 7 filtered `COUNT`s (:486-511), plus `total` executed again after the filters (:533). This duplicates the query at :487.
   - Sort: whitelisted branches (:536-569). `series`, `instances` and `modality` build `GROUP BY` subqueries over the **whole** Series or Instance table on each request (:543-561).
   - Page: `LIMIT/OFFSET` (:571-575). Then `study_aggregates` batch (:578), `get_tenant_names` (:579), one batch query for first series (:581-590), one batch query for report rows (:593-600). No N+1 in this path.
3. **`/api/v1/triage`** (dashboard.py:643-681): one join query, `LIMIT 50`, cross-tenant.
4. **`/api/v1/studies/{uid}`** (dashboard.py:697-748): one study fetch with patient joinedload, one series list, one batched instance count. Anonymous (F7).

---

## 5. Code review findings

Severity: 🔴 blocking, 🟡 important, 🟢 nit. Every item below was verified by the quoted code.

### 5.1 Blocking (🔴)

**F1. 🔴 Hard-coded LDAP credential bypass mints a RADIOLOGIST JWT for anyone**

> **Reviewer correction (verified):** the mock assigns the *lowercase* role `"radiologist"`/`"physician"` (`ldap_service.py:60`). `require_destructive_role` compares against uppercase `"RADIOLOGIST"` (`deps.py:105-110`), so the minted account is **not** able to delete studies. It is still a valid logged-in identity for every *Any user* route — including lifecycle PURGE rules and folder migration with delete (see CODE_REVIEW.md) — so the finding stays blocking.
- `app/services/ldap_service.py:59-60`: `if username.startswith("ldap_") and password == "domainpass":` and `role = "radiologist" if "rad" in username else "physician"`.
- `app/api/v1/ldap.py:29-56` (no rate limit, no audit, no refresh cookie). `app/services/ldap_service.py:86-93` creates the user row.
- `LDAP_SERVER` is not declared in `app/core/config.py` (all 335 lines read), so `getattr(settings, "LDAP_SERVER", None)` (ldap_service.py:39) is always `None`. The ldap3 branch never runs, and the mock is the only path.
- Failure: `POST /api/v1/ldap/login {"username":"ldap_rad_x","password":"domainpass"}` returns a valid JWT with `role: radiologist`. That role passes `require_destructive_role` (deps.py:105, 108-114), so the attacker can `DELETE /api/v1/studies/{uid}` (dashboard.py:834-836) and read `/api/v1/audit` (dashboard.py:388). The same credential also overwrites `role` and `full_name` of an existing local account whose username starts with `ldap_` (ldap_service.py:79-83). `tests/test_ldap.py:20` uses the same credential, so the test mock shipped into production code.
- Fix: delete the mock. Declare `LDAP_*` settings explicitly. Never derive role from a substring of the username. Do not let directory sync overwrite a local account's role without an explicit admin mapping. Rate-limit and audit this route.

**F2. 🔴 Anonymous self-registration can self-assign RADIOLOGIST, a destructive role**
- `app/api/v1/auth.py:183`: `role = body.role or "PHYSICIAN"`. `auth.py:186`: `elif role not in ROLES:`. Only `ADMIN` is refused (auth.py:188-191), so `RADIOLOGIST` is accepted.
- `app/core/config.py:145`: `ALLOW_REGISTER: bool = True`. `README.md:740` says "ALLOW_REGISTER=true by default; disable it after setup", and `.env.example:41` ships `ALLOW_REGISTER=true`.
- `app/api/v1/deps.py:105`: `DESTRUCTIVE_ROLES = ("ADMIN", "RADIOLOGIST")`.
- Failure: any network client can `POST /api/v1/auth/register {"username":"zz9x","password":"secret1","role":"RADIOLOGIST"}` (201, session cookie set). It can then `DELETE /api/v1/studies/{uid}` for any tenant (F8) and read audit and DB perf (db_panel routes are DESTR).
- Related: when the DB has no users, the first registration receives ADMIN (auth.py:184-185). The check-then-insert at auth.py:182-185 has no lock, so two concurrent first registrations both become ADMIN.
- Fix: default `ALLOW_REGISTER=false`. Self-registration should only ever create PHYSICIAN or TECHNOLOGIST accounts, or create a pending state that an admin approves. Never allow destructive roles through registration. Serialize the first-user bootstrap.

**F3. 🔴 LDAP-provisioned accounts use a publicly known password**
- `app/services/ldap_service.py:88`: `password_hash=hash_password("ldap_managed_password"),`.
- `app/api/v1/auth.py:238`: `ok = verify_password(body.password, user.password_hash)` runs on the normal `/api/v1/auth/login` path.
- Failure: anyone who has read the repo can `POST /api/v1/auth/login` as any LDAP-provisioned username with password `ldap_managed_password` and receive a full session. The account is created by the LDAP sync path (ldap_service.py:86-93), which F1 can trigger for any `ldap_*` name, but the password itself is fixed by the code. This works on the normal login route and skips LDAP entirely.
- Fix: store an unusable sentinel hash (for example `!` plus random bytes) that `verify_password` always rejects, and authenticate LDAP users only through LDAP.

**F4. 🔴 Unauthenticated writes to organization settings, logo upload and report templates; stored XSS chain**
- `app/api/v1/settings.py:84`: `user: User | None = Depends(get_optional_user),` in `POST /api/v1/settings/organization`. `user` is never read, so there is no check. The same pattern repeats at settings.py:141 (create template), :177 (patch), and :200 (delete). All four routes are anonymous.
- Payload fields `header_html_text` and `footer_html_text` are stored as raw HTML (settings.py:99-100 into report_service).
- Logo upload (settings.py:88-94): `filename = f"logo_{Path(logo_file.filename).name}"`, no extension or content-type check, no size limit, written under `app/static/uploads/`. `main.py:560` mounts `/static` (CachedStaticFiles), so the file is served same-origin at `/static/uploads/logo_<name>`.
- Rendering sinks:
  - `app/api/v1/reports.py:374` `{header_html}`, `:427` `{footer_html}`, and `:371` `<img src="{logo_src}">` (`logo_src` from `org.logo_path`, reports.py:240). These are unescaped interpolations into the public print HTML (see F10).
  - `app/static/settings/index.html:738-739`: `document.getElementById('live_preview_header').innerHTML = document.getElementById('header_html_text').value;` (loaded at :727-728). Template titles are injected via `innerHTML` at `index.html:795-797` (`<td><b>${t.title}</b></td>`). Those titles are anonymously writable through `POST /api/v1/templates`.
- Failure chain: anonymous `POST /api/v1/settings/organization` with `header_html_text=<img src=x onerror="...">`. When an admin opens `/settings` (gated only by a cookie check, main.py:299), the script runs in the admin's origin. It reads `localStorage["paxs_token"]` (set in login.html:241) or calls `POST /api/v1/auth/refresh` (the HttpOnly cookie is sent same-origin). Admin takeover follows.
- Fix: `Depends(require_admin)` on all four routes. Sanitize or escape HTML on write and on render (escape everywhere the value is interpolated). Restrict uploads to an image MIME allowlist, a size cap and generated filenames. Serve uploads with `Content-Disposition: attachment` or from a non-origin path.

**F5. 🔴 `GET /api/v1/redis/status` is anonymous and returns the saved Redis password**
- `app/api/v1/redis_panel.py:63-64`: `@router.get("/status")` / `async def get_status(user: User | None = Depends(get_optional_user)):`. The handler returns the service dict with no admin check (redis_panel.py:66-69).
- `app/services/redis_service.py:668`: `saved = saved_redis_config()`, returned at `:748`: `"saved": saved,`.
- The stored JSON includes the password: `redis_service.py:219-226` `clean_cfg = {... "password": password, ...}`, serialized at `:231` (`payload = json.dumps(clean_cfg)`) and written to `server_settings.redis_config` at :236-238.
- Failure: `curl http://<host>/api/v1/redis/status` returns `"saved": {"host": ..., "password": "<plaintext>"}` for any caller, once the Redis configuration has been saved through the UI.
- Fix: require ADMIN on this route, or strip `password` from `saved` before returning it. Do not store the password in `server_settings` (see F27).

**F6. 🔴 Anonymous Redis key enumeration and value previews**
- `app/api/v1/redis_panel.py:95-102` (`GET /keys`, `Depends(get_optional_user)` at :99) and `:105-114` (`GET /key`, :108). Neither enforces auth. `get_key_detail` returns a preview (redis_service.py:892-898, first 256 bytes of string values of 512 bytes or more).
- Cache key names embed SOP instance UIDs (`paxs:frame:{sop_uid}:{n}`, dicomweb.py:730, wado_rs_bulk_service.py:162). An anonymous caller can enumerate cached instances and read leading bytes of cached frames or thumbnails.
- Unbounded scan: `redis_service.py:811-842`: `while len(results) < limit:` ... `if cursor == 0: break`. A pattern with no matches walks the entire keyspace in one request. Each request also opens a new Redis connection (:805-809).
- Hash preview runs `HGETALL` on the whole hash (:904), which loads it into memory.
- Fix: require ADMIN on `/keys` and `/key`. Bound the scan. Use a server-side key prefix allowlist for previews. Reuse one pooled client.

**F7. 🔴 PHI returned by anonymous dashboard endpoints**
- `app/api/v1/dashboard.py:697-698`: `async def study_detail(study_uid: str, db: AsyncSession = Depends(get_db)):` (no user dependency). Response includes `"birth_date"`, `"sex"`, and `"phone": study.patient.patient_phone,` (dashboard.py:721-723).
- `dashboard.py:751-752` `series_instances` (no user dependency). `dashboard.py:773-775` `series_thumbnail` (no user dependency). On a cache miss it **generates and stores** a thumbnail (dashboard.py:786-796). Thumbnails are image pixels derived from patient data.
- Study UIDs are not secret: they appear in DICOM headers and in every DICOMweb query. `deploy/PAXSVIEWER-INTEGRATION.md:22` and the `DICOMWEB_PUBLIC` description (settings_service.py:57-62) scope anonymous access to DICOMweb only. Nothing in dashboard.py references `DICOMWEB_PUBLIC`.
- Fix: add `Depends(get_current_user_any)` and tenant checks (see F8). If anonymous access is intended for some share flow, route it through share tokens. Do not generate thumbnails for anonymous GETs. If the intent is confirmed as viewer-only, this is a blocking finding.

**F8. 🔴 Tenant (VNA) isolation missing on object-level endpoints**
- The list endpoint applies scope (`tenant_service.scope_from_user`, dashboard.py:439-452). The object endpoints do not:
  - `delete_study` (dashboard.py:834-836 `user=Depends(require_destructive_role)`; no tenant check in 837-864). A RADIOLOGIST of tenant A can delete tenant B's study, files included (857, 859).
  - `presign_instance` (dashboard.py:804-807, `user: User = Depends(get_current_user)`). Any authenticated role can get a download URL for any instance (822-831).
  - `study_detail`, `series_instances`, `series_thumbnail` (see F7).
  - `list_triage` (dashboard.py:643-665): any authenticated role sees patient_name and patient_id for every tenant's unreviewed findings.
  - `review_triage` (dashboard.py:684-694): any authenticated role can mark any tenant's finding reviewed, with no record of who did it (F44).
- Fix: centralize `get_study_for_user(db, user, study_uid)` that applies `scope_from_user` and 404s out-of-scope studies. Use it on every object route, including triage and presign.

**F9. 🔴 Default JWT signing key is accepted without a startup guard**
- `app/core/config.py:136`: `SECRET_KEY: str = "paxs-insecure-dev-key-change-me"`. No check was found in `app/main.py` or elsewhere (grep of `insecure-dev-key` and `SECRET_KEY` shows only definitions and uses).
- The default is public in the repo. HS256 tokens are verified only against this value (`deps.py:51`, `deps.py:76`, `auth.py:208/269/327`, `ldap.py:45`). `decode_token` checks only the HMAC and `exp` (security.py:141-153).
- Failure: if the deployment does not override `SECRET_KEY`, anyone can sign `{"sub":"admin","role":"ADMIN","exp":...}`. `get_current_user` loads the user by `sub` (deps.py:56) and uses the DB role, so the forged token authenticates as the existing `admin` account (also seeded by default, F28).
- Doc conflict: `README.md:532` lists `SECRET_KEY` as "(none — required)", `.env.example:39` ships the placeholder `change-me-to-a-long-random-string`, and `feature-plans/plan/plan_post.md:58` plans "Error on startup if SECRET_KEY equals the dev default", which is not implemented.
- Fix: refuse to start (outside DEBUG) when `SECRET_KEY` equals the default, is shorter than 32 bytes, or equals the `.env.example` placeholder.

**F10. 🔴 (cross-area) Public print report renders org header/footer and DICOM PatientName unescaped**
- `app/api/v1/reports.py:227-228`: `@router.get("/print/{study_instance_uid}", response_class=HTMLResponse)` / `async def print_report(study_instance_uid: str, db: AsyncSession = Depends(get_db)):` (no auth). Router is included at `main.py:541`.
- Unescaped sinks: `reports.py:374` `{header_html}`, `:427` `{footer_html}`, `:383` `<td><b>{data['patient_name']}</b></td>`, `:251` `<title>... {data['patient_name']} ...</title>`.
- `data['patient_name']` comes from DICOM `PatientName`, which an external modality or PACS can set through C-STORE or STOW. Combined with F4, the header value is attacker-writable too.
- Failure: a DICOM instance with `PatientName` containing `<script>` is stored, and any user who opens `/api/v1/reports/print/<uid>` (no login) runs it in the app origin.
- Fix: HTML-escape every interpolated value (`html.escape`), and require auth on the print route.
- Verified only at the cited lines; reports.py was not read in full.

### 5.2 Important (🟡)

**F11. 🟡 Account lockout enables targeted DoS and username enumeration; timing oracle**
- `app/api/v1/auth.py:224-226`: unknown user returns 401 immediately; `auth.py:238` runs PBKDF2 (200k iterations) only for known users. Measurable latency difference enumerates usernames.
- `auth.py:228-236`: a locked account returns `423` with remaining minutes **without checking the password**. This also reveals that the account exists.
- Anyone can lock any known account: 5 wrong passwords, then the account is unusable for 15 minutes (`LOCKOUT_DURATION_MINUTES`, config.py:129). The per-IP rate limit is spoofable (F18), and this applies to ADMIN accounts too.
- Fix: throttle per (IP, username) with exponential backoff rather than locking the account. Run a dummy PBKDF2 for unknown usernames. Return the same error and status for locked and wrong-password cases (or 429 only).

**F12. 🟡 Open redirect via `/\host` and tab-character bypass in `role_landing`**
- `app/api/v1/auth.py:68`: `if next_url and next_url.startswith("/") and not next_url.startswith("//"):`. Lines 70-71 reject only `\n` and `\r`.
- `app/static/login.html:242`: `window.location.href = data.redirect_to || next || "/";`. `redirect_to` is server-computed from `?next=` (auth.py:166).
- Failure: `/login?next=%2F%5Cevil.example` results in `redirect_to = "/\evil.example"`. Browsers normalize `\` to `/` for http(s), so the navigation goes to `//evil.example`. A tab (`%09`) passes the same way (it is not in the rejected set and browsers strip it). This is a post-login phishing redirect.
- Fix: parse with `urllib.parse.urlsplit`, require empty scheme and netloc, and reject any `\` or ASCII control character.

**F14. 🟡 Failure counter is not reset when a lock expires, so one wrong password re-locks**
- `app/api/v1/auth.py:240-242`: `user.failed_attempts = (user.failed_attempts or 0) + 1` / `if user.failed_attempts >= settings.MAX_FAILED_ATTEMPTS:`. The counter resets only on success (auth.py:263) and admin reset (users.py:292).
- Failure: after the 15-minute lock lapses, the next wrong password (`failed_attempts` 6 >= 5) re-locks the account for another 15 minutes. This contradicts the documented "attempts 1-4 return a generic error" (config.py:122-127).
- Fix: when `locked_until` is in the past, reset `failed_attempts` to 0 before incrementing.

**F15. 🟡 Concurrent refresh with the same cookie causes self-inflicted family revocation**
- `app/api/v1/auth.py:289-292`: `select(Session).where(Session.refresh_token_hash == token_hash)` with no `with_for_update()` (contrast `users.py:58` which uses it). Both requests read the same row, both rotate (auth.py:321-324), and the second commit overwrites the first.
- The browser keeps one of the two new tokens. Its next refresh finds no row (`sess is None`), so auth.py:294-306 revokes the whole family and logs the user out on all devices.
- Fix: `SELECT ... FOR UPDATE` on the session row, or keep the previous hash valid for a short grace window (30-60 s).

**F16. 🟡 Admin password reset does not revoke sessions and does not record the actor**
- `app/api/v1/users.py:350-351`: `user.password_hash = hash_password(body.password)` / `await write_audit(db, user.id, "password_change")`. No session revocation (compare `revoke_user_sessions`, users.py:181-213), and no `by` in details.
- Failure: a compromised account's existing refresh cookie keeps working (rotation extends it, auth.py:320-324), and bearer tokens stay valid until `exp`.
- Fix: revoke all sessions for the user on reset and record `{by: admin.username}`.

**F17. 🟡 Audit trail has gaps for privileged user operations**
- Role change audit has no actor: `users.py:330-333` `write_audit(db, user.id, "role_changed", details={"old": ..., "new": ...})`.
- No audit for `is_active` changes (`users.py:336-337`), user create (`users.py:93-115`), or user delete (`users.py:356-366`).
- Audit rows use the target user's id (`user_id=user.id`), so the actor appears only in some `details` dicts.
- Fix: add `by` to every admin audit event and log create, delete and disable.

**F18. 🟡 (cross-area) Rate limiter trusts spoofable `x-forwarded-for`**
- `app/api/ratelimit.py:30-32`: `forwarded = request.headers.get("x-forwarded-for")` / `if forwarded: return forwarded.split(",")[0].strip()`. The header is trusted unconditionally.
- Failure: any client sends a random `X-Forwarded-For` per request, which defeats the per-IP login limit (`RATE_LIMIT_LOGIN_PER_MINUTE`, auth.py:171/218/278) and the global limit (main.py:440-443). This matters most for F11 and F3-style brute force.
- Fix: honor `X-Forwarded-For` only from configured trusted proxies. Otherwise use the socket peer address.
- Verified only at the cited lines; ratelimit.py was not read in full.

**F19. 🟡 LDAP login route has no rate limit, no audit, and a latent DN injection**
- `app/api/v1/ldap.py:29-33`: `@router.post("/login", response_class=JSONResponse)` has no `dependencies=[Depends(rate_limit(...))]`, unlike auth.py:217-218.
- No `write_audit`, no `last_login`, and no refresh session (ldap.py:39-56). LDAP logins leave no trace in `auth_audit_log`.
- Latent: `ldap_service.py:44`: `user_dn = f"uid={username},{getattr(settings, 'LDAP_BASE_DN', 'dc=hospital,dc=org')}"`. `username` is not escaped (`,`, `=`, `\`, `*`, `(`, `)` and NUL are all unescaped). This is unreachable today (F1), but would become a DN injection as soon as `LDAP_SERVER` is declared.
- Fix: rate-limit, audit, and `ldap3.utils.dn.escape_rdn` the username before building the DN.

**F20. 🟡 Viewer auth gate can be bypassed with `share=` anywhere in the query string, and `/admin` is not gated**
- `app/main.py:308`: `if "share=" in query:  # externally-delivered patient share link`. This is a substring test. `/settings/?x=share=1`, `/viewer?myshare=1` and `/?share=1` all skip the cookie check.
- `main.py:294-300` gates only `/`, `/viewer*`, `/studies-ui*`, `/settings*`. The `/admin` control-panel SPA is mounted at `main.py:661` without a gate.
- Impact is limited to HTML shells, because the APIs enforce their own auth. Anonymous-OPT routes (F4, F5) remain reachable either way.
- Fix: match the exact share route and parameter name (for example `path.startswith("/viewer/share/")` or a parsed query key `share`), and apply the gate to `/admin`.

**F21. 🟡 Session cookie `Secure` defaults to False; access token kept in localStorage**
- `app/core/config.py:158`: `AUTH_COOKIE_SECURE: bool = False`. `security.py:37`: `secure=settings.AUTH_COOKIE_SECURE,`. The comment at config.py:156 says it must be True behind HTTPS, but nothing enforces that.
- `app/static/login.html:241`: `try { localStorage.setItem("paxs_token", data.access_token); } catch (e) {}`. Any XSS (see F4, F10) can read the access token. The refresh cookie is HttpOnly, but the XSS can call `/api/v1/auth/refresh` same-origin.
- Fix: default `AUTH_COOKIE_SECURE` to True (or derive it from the scheme). Keep the access token in memory, not localStorage.

**F22. 🟡 Runtime settings registry has no bounds and exposes security toggles**
- `app/services/settings_service.py:363-368`: `n = int(value)` / `return str(n)`. No min or max for `RATE_LIMIT_LOGIN_PER_MINUTE`, `TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`, `ROUTING_MAX_ATTEMPTS`, `TRANSFER_ACCELERATOR_WORKERS`. Setting `TOKEN_EXPIRE_MINUTES` to a huge value makes access tokens effectively permanent. Setting `RATE_LIMIT_LOGIN_PER_MINUTE` to 0 or a huge value disables or breaks the login limit.
- Security toggles are runtime-editable: `DICOMWEB_CLIENT_ALLOW_PRIVATE` (settings_service.py:257-262, "SSRF guard off"), `RATE_LIMIT_ENABLED` (241-246), `ALLOW_REGISTER` (235-240), `S3_ENDPOINT_URL` (177-182, redirects signed S3 traffic), `HL7_EMR_HOST` and `HL7_EMR_PORT` (131-140).
- Fix: add min and max to `int` keys. Make SSRF-guard, registration and S3-endpoint keys restart-only, or require a second confirmation. Audit every PATCH.

**F23. 🟡 Runtime setting changes are inconsistent across the app and across nodes**
- Captured at import (edits silently ignored until restart): `auth.py:171`, `auth.py:218`, `auth.py:278` (`rate_limit("auth", settings.RATE_LIMIT_LOGIN_PER_MINUTE, 60)`), `main.py:442` (global limit), and `security.py:26` (`COOKIE_MAX_AGE`, so the cookie lifetime does not follow `REFRESH_TOKEN_EXPIRE_DAYS` while the DB session does, auth.py:115).
- No cross-node propagation: the load-balanced deployment described in config.py:13-16 keeps stale values on every other node until restart. `apply_db_overrides` runs only at boot (main.py:386).
- `_apply_to_runtime` (settings_service.py:427-432) swallows exceptions silently (`log.warning`), so a failed apply still reports success.
- Fix: read settings at call time, or publish changes through a DB row or Redis pub/sub that every node polls.

**F24. 🟡 `.env` injection via unvalidated host and database name**
- `app/services/db_admin.py:86`: `return f"mysql+aiomysql://{user}:{password}@{cfg['host']}:{port}/{name}?charset=utf8mb4"`. `user` and `password` go through `quote_plus`, but `host` and `name` do not.
- `app/services/db_admin.py:189`: `new_line = f'DATABASE_URL="{url}"'`, written verbatim into `.env` (:199).
- `app/api/v1/database.py:64-74` `_validate_mysql` checks only that `host`, `user` and `name` are non-empty. `database.py:33-39` `MysqlCfg` has no pattern. `ensure_database` (db_admin.py:282) does enforce `_NAME_RE`, but `configure` does not call it.
- `app/services/redis_service.py:109` and `:184` (`REDIS_URL="{url}"`); `redis_panel.py:33-35` `RedisCfg.host: str` is unvalidated.
- Failure: an ADMIN sends `host = '127.0.0.1"\nSECRET_KEY=attacker'`. The newline ends the line, so `.env` gains a `SECRET_KEY` line that overrides the real key on the next boot.
- Fix: validate `host` (hostname/IP regex) and `name` (`_NAME_RE`), reject control characters, and write `.env` through a proper serializer.

**F25. 🟡 Blank-password probes send the stored DB and Redis passwords to an admin-chosen host**
- `app/api/v1/database.py:90-91`: `if not cfg.get("password"): cfg["password"] = db_admin.saved_password()`, then `test_mysql_connection(cfg)` with the submitted `host`. Same pattern at `database.py:152-153` (`/init`, via `ensure_database` and migrations).
- `app/api/v1/redis_panel.py:76-77`: `cfg["password"] = redis_service.saved_redis_password()`, probing the submitted host.
- Failure: an ADMIN points `host` at a host it controls, leaves the password blank, and receives the saved credential in the MySQL or Redis handshake.
- Fix: never substitute a saved secret for a host other than the saved host. Require the password explicitly for new hosts.

**F26. 🟡 Control-panel config can point at arbitrary executables (admin-level code execution primitive)**
- `app/services/admin_panel_service.py:123-125`: `validated = self._validate({**self._config, **(cfg or {})})`. `_validate` (:102-116) accepts any string for `orthancPath`, `apiDir`, `proxyDir` and `adminFrontDir`, and keeps unknown keys (:103, `out = dict(cfg)`).
- `start_service` runs `Popen` on those paths: `exe = self._resolve(cfg["orthancPath"])` (:255) with `_spawn` (:257), `["python", str(script)]` (:262), `[node, target]` (:270), and `["npm", "run", "dev"]` (:274).
- Failure: an ADMIN can set `orthancPath` to any `.exe` and start it on the server. ADMIN-only, but there is no allowlist, and the same admin can already change settings that affect the whole server.
- Fix: restrict paths to a configured root, drop unknown keys, and log every config change with the actor.

**F27. 🟡 Secrets and PHI at rest: Redis password in DB, unencrypted full backups**
- `app/services/redis_service.py:241-247`: the `REDIS_URL` row is written with the password embedded (`session.add(ServerSetting(key="REDIS_URL", value=url))`). `:231-238` also stores `redis_config` with `"password"`.
- This contradicts `app/services/db_admin.py:113-121`, whose docstring says the DB password is kept out of the archive DB "so credentials never end up in the archive's own database or its backups".
- `app/api/v1/database.py:211-230` and `app/services/db_admin.py:367-417`: backups are full dumps (users table with PBKDF2 hashes, sessions with refresh-token hashes, every PHI row). They are unencrypted, and any ADMIN can download them (`FileResponse`, database.py:228-230).
- Fix: keep secrets out of `server_settings` (use `.env` or a secret store). Encrypt backups at rest and make downloads an explicit, audited action.

**F28. 🟡 Weak default admin credential is seeded**
- `app/api/v1/database.py:59-61`: `seed_admin: bool = True`, `admin_username: str = Field(default="admin", ...)`, `admin_password: str = Field(default="admin123", min_length=6, max_length=128)`.
- `app/services/db_admin.py:488-510`: `seed_admin` creates the ADMIN with the supplied password when `users` is empty on the target. Combined with F9 this yields a known admin login on fresh installs.
- Fix: no default password. Require the operator to supply it, or print a random one once. Default `seed_admin` to False.

**F29. 🟡 Synchronous I/O and DB calls on the event loop**
- Sync DB inside async handlers:
  - `dashboard.py:183` `"backends": _named_backend_count(),` inside `async def health` (`_named_backend_count` at :110-118 uses `SyncSessionLocal`). `/api/v1/health` is probed by HAProxy.
  - `config.py:50` `overrides_map()` in `async def get_config`, and `config.py:88` `set_override(...)` in `async def patch_config`.
  - `services/db_panel.py:249-284`: `_get_setting`, `set_slow_query` and `_mysql_counters()` (pymysql, up to 3 s) all run inside `async def perf_panel`, called directly from `db_panel.py:39`.
- Sync file and storage I/O in async handlers:
  - `dashboard.py:819` `storage_service.exists(inst.file_path)` (`presign_instance`, not wrapped in `to_thread`, unlike :787-789).
  - `dashboard.py:849-851` `add_audit(...)` computes SHA-256 of each DICOM file (`audit_service.py:25-29`), and `:857-858` `delete_study_files` and `invalidate_usage_cache` run synchronously. Deleting a large study blocks the whole server.
- Sync network in async handlers: `ldap.py:35` `authenticate_ldap_user` (ldap3 network bind when enabled), `license.py:142` `lic.activate_online` (outbound HTTP), `settings.py:92-93` `open(...).write` for logo upload.
- Fix: wrap each call in `asyncio.to_thread` (the pattern already used in redis_panel.py and database.py).

**F30. 🟡 Anonymous `/api/v1/redis/status` spawns subprocesses on every request**
- `redis_service.py:669`: `server_install = get_redis_server_installation()`, which runs `sc query` for `Memurai` and `Redis` (`_get_service_status`, :324-341, 371-372), `tasklist` (:385-390) and filesystem `exists` checks. Each request also opens 2 Redis connections (`test_redis_connection` at :611 and `get_redis_status` at :681).
- Callable anonymously (F5), and the Settings UI polls it.
- Fix: ADMIN-only, cache the installation result, and move it off the request path.

**F31. 🟡 Anonymous `/api/v1/stats` runs about 45 sequential COUNT(*) per call and leaks counters**
- `dashboard.py:231-310` and `:363-365`: sequential awaited `COUNT(*)` on Instance, Series, Study and others, per request, with no caching other than storage bytes. Dashboards poll this endpoint.
- Anonymous callers receive `users` (dashboard.py:235, 324), `audit.events` (:284), tenant counts, shares, and the feature block.
- Fix: cache the whole payload for a short TTL, compute counts in one grouped query or use approximate counts, and require auth for the operational block.

**F32. 🟡 Heavy dashboard queries: whole-table GROUP BY sorts and leading-wildcard search**
- `dashboard.py:543-561`: `sort=series|instances|modality` aggregate `Series` or `Instance` across the whole table on every page request. The `instances` variant (:548-554) joins `Instance` (the largest table). This is not limited to the filtered studies.
- `dashboard.py:454-463`: `LIKE '%term%'` on patient name, patient id, description and accession. It cannot use an index, and `%` and `_` in user input are not escaped (LIKE wildcards).
- Fix: precompute per-study aggregates (or denormalize series/instance counts onto `studies`), add an index strategy for search, and escape LIKE metacharacters.

**F33. 🟡 Anonymous infrastructure and license disclosure**
- `dashboard.py:143-220` `GET /api/v1/health` (anonymous) returns `dicom.host`, `dicom.port`, `tls.cert` path, `hl7.host`, `hl7.port`, `storage.endpoint` (`S3_ENDPOINT_URL`), `storage.bucket`, `public_base_url`, `services` matrix, and `rate_limit_per_minute`.
- `license.py:48-63`: anonymous `customer_name`, `customer_organization`, `customer_email` and `machine_fingerprint` (license.py:33-35, 43) when populated, plus hardware fingerprint and info.
- `database.py:77-81` `GET /api/v1/db/status` returns host, port, DB user, database name and `backup_dir` to any authenticated role (`db_admin.py:462-484`).
- Fix: keep only status and version on `/health`, move detail to an admin route, restrict license PII to ADMIN, and limit `db/status` to ADMIN.

**F34. 🟡 Redis flush: one-click FLUSHDB and blocking KEYS, no confirmation**
- `redis_service.py:954-963`: `target == "all"` runs `client.flushdb()`, which wipes the entire configured Redis DB. The DB may be shared with other applications.
- `redis_service.py:981`: `keys = r_client.keys(pat)` runs O(N) and blocks Redis for the duration. `pat` is user-controlled through `pattern` (redis_panel.py:124-132).
- Fix: use `SCAN` plus batched `UNLINK` for pattern deletes, require a typed confirmation for `all`, and do not expose `all` as a single-click option.

**F35. 🟡 Downloads and executes an unverified Redis binary**
- `redis_service.py:576-596`: `urllib.request.urlretrieve(download_url, zip_path)` from `github.com/tporadowski/redis` (a third-party Windows port) with no checksum or signature check, then `zipfile.ZipFile(...).extractall(TOOLS_REDIS_DIR)`, then `start_redis_server()` runs `Popen` on the extracted exe (:462-482). `urlretrieve` has no timeout (a thread can hang indefinitely).
- Fix: pin the expected SHA-256 and verify before extracting. Use an HTTP client with a timeout and a size cap. Consider a vetted installer.

**F36. 🟡 PHI in outbound webhook payloads; no HTTPS enforcement; unbounded threads**
- `app/services/indexing_service.py:128-135`: `webhook_fire("StudyReceived", ..., patient_id=..., patient_name=patient.patient_name, ...)`.
- `app/services/critical_results_service.py:72-79`: `CriticalResultFlagged` with `patient_id` and clinical `description`.
- `webhook_service.py:26-28` accepts any URL scheme (urllib also supports `file:` and `ftp:`), `_deliver` (:31-50) sends PHI over plain `http`, and `fire` (:53-66) starts one thread per URL per event with no pool or queue.
- Fix: send minimal identifiers (study UID, event type), require `https` in production, and use a bounded worker pool with a queue.

**F37. 🟡 DB Pool EXPLAIN guard is prefix-only and runs with the primary credentials**
- `app/services/db_panel.py:214-216` checks only that the statement starts with `SELECT` or `EXPLAIN`, and `:223` runs `text(f"EXPLAIN {sql}")`. `:218` uses `settings.DATABASE_URL`, i.e. the app's read-write account.
- Plain `EXPLAIN` does not execute DML, and `ANALYZE` cannot be reached. The guard is correct today, but it is a single point of failure and depends on EXPLAIN semantics staying the same.
- The custom SQL is a GET query parameter (db_panel.py:53-54), so it is logged by proxies and access logs. It may contain PHI in LIKE literals (see the `studies_search` hot query, services/db_panel.py:130).
- Fix: connect the panel through a read-only MySQL account, parse with sqlglot or equivalent and reject anything other than a single SELECT, and send the SQL in a POST body.

**F38. 🟡 CSRF protection relies entirely on SameSite=Lax**
- No CSRF token exists in scope. Cookie-accepting mutating routes are many: all users routes (`get_current_user_any` via `require_admin`), `DELETE /api/v1/studies/{uid}`, `POST /api/v1/triage/{id}/review`, `POST /api/v1/auth/logout`, the config and DB and Redis admin routes.
- `security.py:35-36` sets `SameSite=Lax`. That blocks cross-site POSTs, but not requests from a sibling subdomain or any same-site origin.
- Fix: add a double-submit or header-based CSRF check for cookie-authenticated mutating requests, or require an `Authorization` header for mutations.

**F39. 🟡 Admin resets and disables do not invalidate issued access tokens**
- `deps.py:46-63` (`get_current_user`) checks `is_active`, which is good, but a revoked session still yields a valid bearer for up to 15 minutes (`TOKEN_EXPIRE_MINUTES`, config.py:140). Logout (auth.py:334-349) has the same gap.
- Acceptable with a short lifetime, but worth noting for incident response. Fix: check a per-user `token_version` claim, or keep lifetimes short.

**F40. 🟡 `/api/v1/users/sessions` and similar admin lists are unpaginated**
- `users.py:85-90` (`list_users`), `users.py:118-150` (`list_sessions`, one row per device per user), `users.py:253-280` (`list_locked`) return all rows. Grows with user and device count. Fix: add `limit`/`offset`.

### 5.3 Nits (🟢)

**F41. 🟢 Anonymous `/metrics` discloses instance identity and traffic counters.** `metrics.py:87-88`. Restrict by network ACL or add a bearer token, since Prometheus can send one.

**F42. 🟢 Delete-user last-admin guard refuses deleting an inactive ADMIN.** `users.py:362-363`: `if user.role == "ADMIN" and await _active_admin_count(db) <= 1:` counts only active admins, so it blocks deleting an inactive admin while the requester is the sole active admin.

**F43. 🟢 Login reveals a password oracle for disabled accounts.** `auth.py:257-260`: `if not user.is_active:` runs after the password check, so `"Account disabled"` appears only when the password is correct.

**F44. 🟢 Triage review records no reviewer.** `dashboard.py:684-694` sets `flag.reviewed = True` without audit or `reviewed_by`. Clinical review needs attribution.

**F45. 🟢 Redundant and unvalidated query parameters.** `dashboard.py:419-420` `int(params.get("page", 1))` / `int(params.get("limit", 20))`: a non-integer returns 500 (`ValueError`). `dashboard.py:487` and `:533` run the same count twice. `dashboard.py:571-575` uses deep OFFSET pagination.

**F46. 🟢 `set_slow_query` NaN or negative threshold.** `services/db_panel.py:302` `_set_setting(_SLOW_THRESHOLD_KEY, str(int(threshold)))`: `int(float("nan"))` raises `ValueError` and returns 500. Negative thresholds log every statement.

**F47. 🟢 `masked_url` and `masked_redis_url` reveal password length.** `db_admin.py:94` and `redis_service.py:122`: `'*' * max(4, len(password))` reveals the length for passwords over 4 characters.

**F48. 🟢 Redis stop kills all processes with the image name.** `redis_service.py:544-549` `taskkill /F /IM redis-server.exe` terminates any instance, not only PAXS-owned ones.

**F49. 🟢 Error classification heuristic.** `redis_service.py:499`: `if service_err and "5" in service_err:` maps any message containing the digit 5 to "requires Administrator rights".

**F50. 🟢 Hard-coded dev path.** `admin_panel_service.py:211`: `github = Path("D:/Github")` is searched on production hosts too.

**F51. 🟢 Import-time env default and dead code.** `security.py:75-79` `session_token_from_cookie` is unused. `metrics.py:36-39` `_INFO_KEYS` is unused.

**F52. 🟢 Non-atomic `.env` write and global mutation during migration.** `db_admin.py:199` writes `.env` in place with default permissions. `db_admin.py:254-274` `run_migrations` temporarily sets `settings.DATABASE_URL` process-wide. Concurrent `create_backup` (:422) or DB-panel reads can see the migration target during that window.

**F53. 🟢 Logout does not revoke the bearer token** (F39 covers the security impact). `auth.py:334-349`.

**F54. 🟢 Password policy.** `auth.py:77` and `users.py:67` allow 6-character passwords with no complexity or breach check. `CreateUserBody`, `ResetPasswordBody` and `RegisterBody` share this minimum.

**F55. 🟢 CORS `*` default and public docs.** `config.py:254` `CORS_ORIGINS: str = "*"` (main.py:433 keeps `allow_credentials=False`, so credentialed cross-origin reads are blocked). `main.py:426-427` publishes `/api/docs` and `/api/openapi.json` to anonymous callers.

**F56. 🟢 Cross-area secrets in the repo.** `deploy/vendor-portal-php/config.php:36` defines `PORTAL_SECRET_KEY` as a literal; `deploy/docker-compose.lb.yml:42` sets `S3_SECRET_KEY: minioadmin`. Verify whether the portal secret signs license material (see open questions).

**F57. 🟢 Silent exception swallow in org settings read.** `app/api/v1/settings.py:53-58`: `try: if org.updated_at: updated_at_str = org.updated_at.isoformat()` / `except Exception: pass` hides real errors. Narrow the exception or drop the guard.

### 5.4 Verified good (no issue found)

- Refresh tokens: 32-byte `token_urlsafe`, stored only as SHA-256 (security.py:50-63), rotated per use with whole-family revocation on reuse (auth.py:294-324).
- Password hashing: PBKDF2-HMAC-SHA256, 200k iterations, per-user salt, `hmac.compare_digest` (security.py:82, 88-109).
- JWT: HMAC verified with `compare_digest`, `exp` required and enforced, algorithm fixed (security.py:141-153). No `alg` confusion.
- Role source of truth: `get_current_user` reloads `User` each request (deps.py:46-63), so JWT role claims are not trusted.
- Self-privilege changes blocked: no self-demotion or self-disable (users.py:318-322), and last-active-ADMIN guard uses `FOR UPDATE` (users.py:52-62, 325-328, 362-363).
- Self-registration cannot create ADMIN once users exist (auth.py:188-191), though see F2 for RADIOLOGIST.
- Open-redirect partly mitigated (auth.py:66-72), though bypassable (F12).
- Session cookie: `HttpOnly`, `SameSite=Lax`, `Path=/` (security.py:31-39). Logout clears it (security.py:42-44).
- Admin per-session and per-user revokes are audited (users.py:172-176, 209-211). Lockout reset is audited (users.py:294-295).
- Admin revoke of self-targeted user sessions preserves the admin's own session (users.py:198-202).
- DB admin backups: filename allowlist and traversal check (db_admin.py:441-448). `mysqldump` gets the password via `MYSQL_PWD`, not argv (db_admin.py:371). `CREATE DATABASE` name is validated (db_admin.py:282, 293). URL-embedded credentials are `quote_plus`-encoded (db_admin.py:80-81).
- DB admin `configure` is ADMIN-only; `db/status` returns no password (database.py:77-81, db_admin.py:462-484; `saved` contains no password, db_admin.py:99-110, 470).
- Redis: no arbitrary command endpoint. No `CONFIG SET`, `FLUSHALL`, `EVAL`, `DEBUG`, or raw command path in area D. Listing uses `SCAN` (redis_service.py:812), not `KEYS`. Redis `info(section)` is an argument to the INFO command (redis_service.py:770), not a free-form command.
- Admin panel: service-name whitelist (admin_panel.py:22, 64); only PIDs the process spawned are killed (admin_panel_service.py:283-306); subprocess calls use argv lists, not shell strings.
- DB Pool EXPLAIN: hot-query allowlist, single statement, read-only prefix, no ANALYZE (services/db_panel.py:190-216). Admin/RADIOLOGIST only.
- Dashboard list: sort columns whitelisted (dashboard.py:536-569); search values are bound parameters (dashboard.py:456-461); scope applied to list (dashboard.py:439-452); no N+1 (joinedload dashboard.py:433, batched aggregates dashboard.py:577-600).
- Triage ordering uses `case` on severity (dashboard.py:649-654).
- Metrics: label values escaped (metrics.py:59-61). DB failures degrade to zeros with `db_up 0` (metrics.py:124-126).
- Webhooks are env-only and not runtime-editable (settings_service.py registry has no WEBHOOK_URLS). This removes the runtime SSRF surface, though no private-IP guard exists (contrast DICOMWEB_CLIENT_ALLOW_PRIVATE, config.py:257-260).
- Settings registry excludes secrets (`SECRET_KEY`, `DATABASE_URL`, `S3_*_KEY`, `REDIS_URL` are absent from EDITABLE_KEYS).

### 5.5 Specific checklist answers (requested focus areas)

- **Endpoints missing auth or admin checks**: F4 (settings/templates writes), F5, F6 (Redis status/keys/key), F7 (study detail, instances, thumbnail), F33 (health, stats, license, db/status info), F1/F2 (auth bypass and registration), F41 (metrics), F55 (docs). Also `/admin` shell (F20).
- **Privilege escalation (user changes own role)**: not possible through `PATCH /api/v1/users/{id}` (users.py:318-322). It is possible through self-registration as RADIOLOGIST (F2), and through the LDAP mock (F1). Via the admin-controlled settings there is no role path.
- **Arbitrary SQL in DB panels**: no general console. The only path is guarded EXPLAIN (F37). `db/init` and `db/configure` take an arbitrary target URL (admin only).
- **Arbitrary Redis commands**: none found (see section 5.4).
- **LDAP injection**: current path is mock-only (F1). Latent DN injection at ldap_service.py:44 (F19).
- **SSRF in webhooks**: env-configured only (webhook_service.py:26-28). No private-IP guard. PHI sent over plain HTTP (F36).
- **Other SSRF-like sinks** (admin-chosen host with the saved secret attached): F25; `db/init` `url` (database.py:128-130); license `portal_url` (license.py:132, 146); `S3_ENDPOINT_URL` runtime-editable (F22).
- **Secrets returned in API responses**: F5 (Redis password). `GET /api/v1/redis/status` is the only confirmed case. `db/status` masks properly (F47 nit). Backup download returns hashes and PHI (F27).
- **Timing-unsafe comparisons**: none for secrets. `verify_password` and `decode_token` use `hmac.compare_digest` (security.py:107, 146). Username timing oracle is F11 (not a comparison issue). LDAP mock uses `==` (ldap_service.py:59), moot after F1.
- **Cookie flags**: HttpOnly yes, SameSite=Lax yes, Path=/ yes, Max-Age yes, **Secure default off** (F21).
- **CSRF**: no token; relies on SameSite=Lax (F38).
- **N+1 and heavy dashboard queries**: no N+1 in `/studies` (section 4i). Heavy: F31 (`/stats`), F32 (sort and search), F40 (unpaginated admin lists).

---

## 6. Open questions

1. **Anonymous PHI routes (F7).** Is `GET /api/v1/studies/{uid}`, `/series/{s}/instances` and `/thumbnail.png` intended to be public? Nothing in dashboard.py references `DICOMWEB_PUBLIC`, and `deploy/PAXSVIEWER-INTEGRATION.md:22` scopes anonymous access to DICOMweb only. Confirm before treating F7 as a bug or as a design choice.
2. **LDAP mock (F1, F3).** `tests/test_ldap.py:20` is the only caller of the `ldap_*` / `domainpass` credential. Confirm that no packaged build relies on it. Also confirm whether `LDAP_SERVER` is supposed to be declared in `Settings` (it is not today).
3. **Deployed `SECRET_KEY` and `AUTH_COOKIE_SECURE`.** `.env.example:39` ships a placeholder; the README calls it required. Check the production `.env` (not in repo) for an override, and whether HTTPS termination makes `Secure` safe to enable.
4. **Rate limiter (F18).** `app/api/ratelimit.py` was read only at lines 30-32. Confirm its keying, whether `RATE_LIMIT_ENABLED` is honored per request, and whether a trusted-proxy list exists elsewhere.
5. **Licensing (section 4g).** `app/core/licensing.py` was not read. Need: contents of `get_summary()` (is `customer_email` populated?), whether `activate_online` validates `portal_url` (license.py:142-146), whether `activate_from_bytes` verifies a signature, and whether `deploy/vendor-portal-php/config.php:36` `PORTAL_SECRET_KEY` signs licenses.
6. **Reports (F10).** `app/api/v1/reports.py` was read only at 220-260 and by grep. Confirm whether `print_report` is intended to be public, and whether DICOM `PatientName` is escaped anywhere upstream (for example in ingest).
7. **Tenant service and share tokens.** `tenant_service.scope_from_user` (tenant_service.py:83-95) was read. Confirm whether share tokens (`/viewer?share=`) grant access to any dashboard detail routes, and whether dicomweb.py uses the same scope.
8. **`delete_study` ordering (dashboard.py:857-859).** `storage_service.delete_study_files` runs before `db.delete` and commit. If the DB commit fails after files are deleted, the archive is inconsistent. Confirm behavior of `delete_study_files` on partial failure (storage_service.py not read).
9. **Audit immutability.** No delete route exists for `AuditLog` in area D, and the `add_audit` rows carry only a per-file SHA-256, not a hash chain. Confirm whether DB privileges make `audit_logs` append-only in production.
10. **Stats cost.** Confirm the cost of `reading_worklist_service.reader_counts` and `scripting_service.list_scripts()` (both run per `/stats` call; not read). Confirm whether `/stats` is served from the read replica only when `DB_READ_REPLICA_URL` is configured.
11. **`study_aggregates` batching (dashboard.py:578).** `query_service.study_aggregates` was assumed batched from its call pattern (one call per page). Confirm in query_service.py.
12. **EXPLAIN edge cases (F37).** Not verified: whether MySQL `EXPLAIN` evaluates constant subqueries (a timing side channel), or whether `SELECT ... INTO OUTFILE` is accepted inside EXPLAIN. The prefix guard does not exclude `INTO`.
13. **Boot order.** `apply_db_overrides()` runs at main.py:386. Confirm whether it runs before the DICOM receiver and HL7 listener bind, since those keys are `restart=True` and the UI claims a restart is required.
14. **`/admin` control panel exposure.** `main.py:661` mounts the admin SPA without the viewer gate. Confirm whether this is intended, and whether `ADMIN_PANEL_ENABLED` (config.py:290) is checked before mounting (not verified in main.py).
15. **First-install bootstrap (F2).** If `/api/v1/db/init` requires ADMIN (database.py:122-123) on a DB whose `users` table does not exist yet, the call cannot succeed. Confirm the intended first-run path.
16. **`DICOMWEB_CLIENT_ALLOW_PRIVATE` (F22).** Confirm whether runtime toggling of the SSRF guard is intended, given config.py:257-260 documents it as a test-only switch.
17. **Thumbnail generation on GET (F7).** Confirm whether `thumbnail_service.generate_thumbnail` writes into the archive's storage backend on anonymous requests, and whether that is acceptable.
18. **Admin password reset policy (F16).** Confirm whether admins should be able to reset other ADMIN passwords without a second approver.
