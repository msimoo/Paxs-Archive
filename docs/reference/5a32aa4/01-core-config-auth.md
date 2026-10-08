# Core, configuration, auth, sessions and admin ops — changes in 5a32aa4

> **Re-review of commit `5a32aa4`** (baseline `e90d32c`), part of the [PAXS Archive documentation](../../README.md). Static analysis, 2026-10-08; line numbers refer to `5a32aa4`. Baseline area reference: [01-core-config-db.md](../01-core-config-db.md), [04-auth-admin-ops.md](../04-auth-admin-ops.md). Findings are consolidated and prioritized in [CODE_REVIEW.md](../../CODE_REVIEW.md), which takes precedence where the two differ. Finding IDs here (N1, N-01, V4-01, …) are local to this file; CODE_REVIEW.md maps them to NEW-xx IDs.

Range: `e90d32c` (old review) to `5a32aa4` (HEAD). The working tree was clean for tracked files at start, so the files read are HEAD. Static reading only: no code was run and uvicorn is not installed in this environment, so claims about uvicorn internals are marked ❓ and need a runtime test.

Legend: 🔴 blocking · 🟡 important · 🟢 nit / defence-in-depth. Status: FIXED / PARTIAL / OPEN / REGRESSED. ❓ = depends on runtime or third-party behaviour not verifiable here.

---

## 0. Bottom line

- The big fixes landed: registration is closed by default and self-registration is limited to PHYSICIAN; DICOMWEB_PUBLIC is now read and makes DICOMweb private by default; the LDAP mock is gone; `/api/v1/stats`, `/api/v1/health`, study detail, `/license`, and `/redis/*` are no longer anonymous; the session cookie checks expiry; migrations no longer auto-stamp; the five unmigrated tables now have migrations.
- The stop-ship items that remain: default `SECRET_KEY` is still accepted in the shipped Docker/LB configuration (the guard is opt-in), `B-02` (unguarded `import redis` with redis commented out), the open redirect, the `share=` gate bypass, `GET /logout` not revoking, the license gate never applied, and the read-replica double-yield.
- New in this commit and worth a fix before release: the new trusted-proxy model breaks the shipped HAProxy topology (one rate-limit bucket for all users, `/metrics` reachable through the LB), the sub-path prefix header is not trusted under the documented config, the migration advisory lock is ineffective, and the production gate runs before DB overrides.

---

## 1. Change summary (per file, HEAD line numbers)

Note: the "+N" figures after each file name are total changed lines from `git diff --stat e90d32c HEAD`, not pure additions.

**app/api/v1/deps.py** (476 lines; +366)
- `get_current_user_from_cookie(request)` (27-58): now rejects expired sessions (`sess.expires_at <= _utcnow()`, lines 50-52). Revoked check at 48.
- `get_current_user(credentials)` (61-78): unchanged, bearer only, checks `is_active`, does not check any session row.
- `get_optional_user(request, credentials)` (81-101): unchanged; bearer then cookie fallback; never raises.
- `get_current_user_any(request, credentials)` (104-116): raises 401 if neither authenticates.
- `require_destructive_role` (123-129) now uses `DESTRUCTIVE_ROLES` from `app.core.roles`; `require_admin` (132-135): `user.role != "ADMIN"` → 403.
- New C03 share block (148-225): `SHARE_TOKEN_QUERY_PARAM="share"`, `SHARE_TOKEN_HEADER`, `SHARE_COOKIE_NAME="paxs_share"`, `share_cookie_max_age`, `get_share_scope(request)` (token from query, header or cookie), `share_cookie_ttl`, `_study_uid_for_series`, `_share_grants`.
- New DICOMweb guards: `require_dicomweb_read(request, study_uid=None, credentials)` (226-270), `require_dicomweb_write(request, credentials)` (273-292), `require_series_read(request, series_uid=None, ...)` (295-313), `require_share_or_user(request)` (316-326), `require_dicomweb_access(request, study_uid=None, credentials)` (329-344, the router-level guard).
- New C04 tenant helpers: `tenant_scope(user)` (353), `read_scope(user, *, share_scope)` (358-370), ContextVar `_request_scope` (378), `current_scope()` (383), `require_study_access` (388), `require_series_access` (412), `_series_belongs_to` (440), `require_instance_access` (455).
- `DICOMWEB_PUBLIC` is read at 251, 264, 282, 368.

**app/core/config.py** (710 lines; +383)
- New settings with defaults: `ROOT_PATH=""` (51), `FORWARDED_ALLOW_IPS="127.0.0.1"` (56), `ENVIRONMENT="development"` (191), `AUTH_COOKIE_SAMESITE="lax"` (195), `REGISTER_ALLOWED_ROLES="PHYSICIAN"` (210), `TRUSTED_PROXIES="127.0.0.1,::1"` (232), `METRICS_ALLOW_IPS` (359-361, loopback plus 10/8, 172.16/12, 192.168/16), `STORAGE_ALLOW_LEGACY_PATHS=False` (111), `LOSSY_COMPRESSION_ENABLED=False` (161), `FUSION_ENABLED=True` (261), `LDAP_ENABLED=False` and `LDAP_SERVER/BASE_DN/USER_DN_TEMPLATE/GROUP_ROLE_MAP/DEFAULT_ROLE/TIMEOUT_SECONDS/USE_TLS` (272-282), `VOLUME_CACHE_BUDGET_MB=1024` (338), `DICOM_FRAME_CACHE_MB=256` (346), `VIEWER_PROCESSING_*` and `HYBRID_*` (312-374), `DEFAULT_PUBLIC_BASE_URL` (13).
- Changed defaults: `ALLOW_REGISTER` True to **False** (207); `DICOMWEB_PUBLIC` True to **False** (427); `DICOMWEB_CLIENT_ALLOW_PRIVATE` still False (434).
- Unchanged insecure defaults: `SECRET_KEY` (189), `AUTH_COOKIE_SECURE=False` (194), `TOKEN_EXPIRE_MINUTES=15` (199), `CORS_ORIGINS="*"` (428), `SCRIPTS_DIR="scripts"` (409).
- `PUBLIC_BASE_URL` now has a field validator (502-521).
- New functions: `is_routable_public_base_url` (20), `resolve_public_base_url(request=None, settings_obj=None)` (535-582), `_request_prefix` (585-594), `INSECURE_SECRET_KEYS` (597-602), `NON_PRODUCTION_ENVIRONMENTS` (607), `is_production_environment` (610-626), `validate_production_settings` (629-668), `validate_memory_budgets` (671-707).

**app/core/proxy_prefix.py** (new, 311 lines)
- `APP_MOUNT_ROOTS` (49-61), `normalise_prefix` (64-74), `TrustedHosts` (77-108; `*`, glob, exact, CIDR), `ForwardedPrefixMiddleware` (115-175; pure ASGI, trusts `X-Forwarded-Prefix/Host/Proto` only when `scope["client"]` is trusted), `prefix_path` (178-193), `prefix_markup` (199-216), boot shim template (219-281), `build_boot_script` (286-294), `inject_boot` (297-311).

**app/core/roles.py** (new, 37 lines): `ROLES`, `DESTRUCTIVE_ROLES`, `WRITE_ROLES`, `ADMIN_ROLE` (unused), `normalize_role`, `is_known_role`. See section 4.5.

**app/core/cache_stats.py** (new, 87 lines): thread-safe hit/miss/insert/evict counters and bytes/entries gauges (`incr`, `set_gauges`, `snapshot`, `reset`). No security impact. Used by `metrics.py:152`, `redis_service.py`, `volume_service.py`, `wado_rs_bulk_service.py`.

**app/core/dicom_diagnostics.py** (new, 145 lines): `transfer_syntax_label` (56), `decode_context(ds, *, frame, series_uid)` (91). Log helper for decode failures, used by `triage_service.py:68`, `volume_service.py:593`, `wado_rs_bulk_service.py:158`.

**app/core/security.py** (155 lines; +4): `set_session_cookie` samesite now `settings.AUTH_COOKIE_SAMESITE` (38). `COOKIE_MAX_AGE` still computed at import (26).

**app/main.py** (946 lines; +274)
- Lifespan (476-535): `validate_production_settings(settings)` (490), `validate_memory_budgets` warnings (495-496), `init_db()` (497), `apply_db_overrides()` (500), then worker, DICOM, HL7, license, heartbeat (512-520), new storage sweep task `_storage_sweep_loop` (452-473, started at 522).
- Middleware order (add order, last = outermost): CORS (547), RateLimit (557-560), Gzip (564), Metrics (565-566), ViewerAuthGate (569), ForwardedPrefix (574-578, `trusted_hosts=settings.FORWARDED_ALLOW_IPS`), SecurityHeaders (580, outermost of the app's own middleware). uvicorn's ProxyHeadersMiddleware wraps all of these (run_paxs.py:90-91).
- SPA shells are served through `_spa_shell` (232-245), which injects the boot shim when a sub-path is active (242). New `/viewer/app-config.js` (793-804). `viewer_root_study_list` now sets the `paxs_share` cookie from `?share=` (773-790).
- `/login` and `/logout` now prefix-aware (885-915).
- Unchanged: CSP `unsafe-inline`/`unsafe-eval` (298); `/admin` not in the gate list (364-369); `share=` substring bypass (377); RateLimit exempts `/static` and `/dicom-web` (437-438).

**app/api/ratelimit.py** (127 lines; +23): `_trusted_proxies()` (29-39, exact string match from `TRUSTED_PROXIES`). `_client_key` (42-50): XFF honoured only when `peer in _trusted_proxies()` (45); leftmost XFF entry used (47). `RATE_LIMIT_ENABLED` checked per request (64). The Redis import is at line 69, outside the `try` that starts at 72.

**app/api/v1/auth.py** (383 lines; +71)
- `role_landing` (68-81) now calls `prefix_path(root_path, ...)`; open-redirect check unchanged (77).
- `register` (180-241): bootstrap admin when `total == 0` (193-194); otherwise `ALLOW_REGISTER` gate (196-197), requested role must be in `REGISTER_ALLOWED_ROLES` minus destructive roles (202-216), default role PHYSICIAN (217).
- `login` (244-302), `refresh` (305-360), `logout` (363-378): logic unchanged except `root_path` passing; `logout` revokes the session row.

**app/db/session.py** (286 lines; +49): `init_db()` (234-~278) rewritten. Empty DB (only `alembic_version` or nothing) runs `db_admin.run_migrations`; a non-empty DB only runs `schema_drift` and logs. `create_all` removed from app boot. `get_read_db` (195-220), `_replica_toggle_enabled` (147-162) unchanged.

**app/services/settings_service.py** (610 lines; +140)
- Registry adds: LDAP keys (64-100), `STORAGE_ALLOW_LEGACY_PATHS` (group storage), `FUSION_ENABLED` (restart), `TRUSTED_PROXIES` (291-297, restart False), VIEWER_PROCESSING_* and HYBRID_* (group `viewer`), `VOLUME_CACHE_BUDGET_MB` (min 64, max 65536, lines 430-431), `DICOM_FRAME_CACHE_MB` (min 16, max 16384, lines 443-444).
- `DICOMWEB_PUBLIC` description changed (61): "Allow QIDO/WADO/STOW without a login token..."
- `_coerce` int range check (499-506) applies only to the two cache keys.
- No change to `set_override` (543-562), `_apply_to_runtime` (565-570, swallows exceptions), `apply_db_overrides` (592-610).

**app/services/db_admin.py** (607 lines; +137)
- `schema_drift(url)` (237-262); `_migration_config(url)` (265-272, no global settings mutation); `_advisory_lock` (275-291); `_release_advisory_lock` (294-301).
- `run_migrations(url, stamp_on_exists=False)` (304-349): no auto-stamp; "already exists" now raises `RuntimeError` (338-344).
- New `stamp_head(url)` (352-371): refuses if drift; **no caller** in the app.

**app/api/v1/config.py** (118 lines; +27): `overrides_map` and `set_override` now run via `asyncio.to_thread` (60, 103); `_field` adds min/max; `get_config` (56) still bearer-only (`get_current_user`); readonly section now `health_payload()` (79-81).

**app/api/v1/database.py** (240 lines; +12): migration step now `steps, _stamped = ...run_migrations(url)` (174); response `stamped` is always False in practice (200). Seed defaults unchanged (`seed_admin=True` line 59, `admin_password` default `"admin123"` line 61). `/status` (77-78) is `get_current_user`.

**app/api/v1/license.py** (170 lines; +5): `GET ""` and `GET /status` (48-55) now `get_current_user_any`; `GET /fingerprint` (58-64) now `require_admin` (59).

**app/api/v1/metrics.py** (256 lines; +103): `require_metrics_access` (37-61): authenticated users pass; otherwise `TrustedHosts(METRICS_ALLOW_IPS)` on `scope["client"]` (57-59) or 401. `/metrics` (181-182) uses it. Cache counters and budgets added (130-178).

**app/api/v1/redis_panel.py** (185 lines; +10): `/status` (63-64), `/keys` (95-102, dep 99), `/key` (105-114, dep 108), `/ping` (135-138) switched from `get_optional_user` to `get_current_user_any`. `/test` still falls back to the saved password (77).

**app/api/v1/users.py** (375 lines; +13): `reset_password` (346-363) revokes all non-revoked sessions of the target user (353-361) and returns `sessions_revoked`. No actor recorded. Access tokens are not revoked.

**app/services/admin_panel_service.py** (357 lines; +7): `launch()` uses `resolve_public_base_url()` (329-334). `_validate` unchanged (F26).

**app/services/redis_service.py** (1096 lines; unchanged): `import redis` still unconditional at line 26; `get_redis_status` still returns `"saved": saved` (748).

**run_paxs.py** (94 lines; +53): `_is_production()` (24-30) delegates to `is_production_environment`. `_load_ssl_context` (33-61) now raises `SystemExit` in production when cert/key is missing or unreadable (54-58), otherwise falls back to HTTP with a warning. uvicorn call (84-94): `reload=not _is_production()` (89), `proxy_headers=True` (90), `forwarded_allow_ips=os.getenv("PAXS_FORWARDED_ALLOW_IPS", "127.0.0.1")` (78), `root_path=os.getenv("PAXS_ROOT_PATH", "")` (79). `PAXS_HOST` default still `0.0.0.0` (64).

**requirements.txt** (39 lines): `boto3>=1.34` and `botocore>=1.34` now uncommented (30-31). `# redis>=5.0` still commented (34). `# ldap3>=2.9` added as a comment (35). No pins. `pytest` and `httpx` still in the runtime list (38-39).

Also in scope: `app/api/v1/deps.py` router-level guard is used by `dicomweb.py:64` (`dependencies=[Depends(require_dicomweb_access)]`).

**Method for auth coverage:** an AST scan of `app/api/v1/*.py` (scratch script, not in the repo) classified every route decorator by the auth-named dependencies in its signature. Result: 149 routes carry an explicit ANY/ADMIN/DESTR/share guard, 98 use `get_current_user` (any authenticated role, bearer only), 4 use `get_optional_user` (all four are DICOMweb routes guarded at router level), and 22 have no auth-named dependency. Of the 22, 11 are DICOMweb routes covered by the router-level guard, and the rest are register/login/refresh/logout, `/health/live`, `/health/ready`, `/fhir/metadata`, `/metrics` (guarded by a non-auth-named dependency), `/share/{token}` and `/share/{token}/qr.png` (token-based, public by design), and `/ldap/login` (rate-limit dependency only). The scan does not check semantics, so the 98 bearer-only routes are reported as "any role", not as verified-insufficient.

---

## 2. Prior findings: status

| ID / short name | Status | Evidence (HEAD) | Notes |
|---|---|---|---|
| B-02 `import redis` unguarded, redis commented | **OPEN** | `app/services/redis_service.py:26` `import redis` (module level, no try); `requirements.txt:34` `# redis>=5.0`; `app/api/ratelimit.py:69` imports `redis_service` outside the `try` at :72; `app/main.py:617, 677` wire `redis_panel` | `boto3` and `botocore` were uncommented (`requirements.txt:30-31`), but redis was not. Fresh `pip install -r requirements.txt` still cannot import the app. |
| S-01 default `SECRET_KEY` accepted | **PARTIAL** | Default `config.py:189` `SECRET_KEY: str = "paxs-insecure-dev-key-change-me"`; guard exists at `config.py:597-602` (INSECURE set), `:629-668` (validator), called at `main.py:490`; but `is_production_environment` (`config.py:610-626`) returns False for `ENVIRONMENT=development`, which is the default (`config.py:191`) | The guard now exists and fails closed in production. The shipped Dockerfile (`deploy/Dockerfile:33` `CMD ["python", "run_paxs.py"]`) and `deploy/docker-compose.lb.yml` set neither `SECRET_KEY` nor `ENVIRONMENT`, so those nodes run with the public key. The script-upload half of S-01 is in the scripts area (not re-verified). |
| S-04 core: DICOMWEB_PUBLIC dead; anonymous reads; default-deny | **PARTIAL** | `DICOMWEB_PUBLIC` read at `deps.py:251, 264, 282, 368` and `dicomweb.py:201-206, 244`; router guard `dicomweb.py:64`; default False `config.py:427`; `dashboard.py:769-770` and `:824-825`, `:852-854` now `Depends(get_current_user_any)` | DICOMweb is private by default and the toggle is now effective. Still no default-deny: `main.py:633-684` includes routers with no `dependencies=`. The toggle also opens anonymous STOW writes (N-06). |
| S-05 `/redis/status` anonymous, returns saved password | **PARTIAL** | Anonymous closed: `redis_panel.py:64` `user: User | None = Depends(get_current_user_any)`. Password still returned: `redis_service.py:668` `saved = saved_redis_config()`, `:748` `"saved": saved`; password stored in `redis_service.py:219-226` (`clean_cfg`) | Any authenticated role (PHYSICIAN, TECHNOLOGIST, RADIOLOGIST) now receives the plaintext Redis password. `/keys` `/key` `/ping` also any-role (`redis_panel.py:99, 108, 136`), so any user can enumerate keys and read cached frame previews. Should be ADMIN-only plus `has_password`. |
| S-07 open self-registration, destructive role, first-admin, seeded admin | **PARTIAL** | Default closed `config.py:207` `ALLOW_REGISTER: bool = False`; role allow-list `auth.py:202-217` (destructive removed, default PHYSICIAN); `REGISTER_ALLOWED_ROLES` not runtime-editable (`settings_service` registry has no entry); bootstrap `auth.py:193-194` `if total == 0: role = "ADMIN"` (no lock); seed default `database.py:59` `seed_admin: bool = True`, `:61` `admin_password ... default="admin123"` | Role matrix fixed. First-registrant race and seeded `admin/admin123` unchanged. |
| S-10 `SCRIPTS_DIR` default; anonymous `/stats` runs scripts | **PARTIAL** | `config.py:409` `SCRIPTS_DIR: str = "scripts"` (unchanged); `dashboard.py:242-244` `stats(... user: User = Depends(get_current_user_any))` (anonymous removed); `dashboard.py:406` `"count": _script_count()` (inside `stats`, range 242 to next def) | Anonymous trigger removed. Any authenticated role still makes the server `exec_module` every `scripts/*.py` on each `/stats` call. |
| F-09 / XFF trusted unconditionally | **PARTIAL (regression in shipped LB)** | Spoofing fixed: `ratelimit.py:45` `if forwarded and peer in _trusted_proxies():`; default `config.py:232` `TRUSTED_PROXIES: str = "127.0.0.1,::1"` | Behind HAProxy (`deploy/docker-compose.lb.yml:16`) every client shares one bucket per node (N-02). The leftmost-entry rule is retained at `ratelimit.py:47` (N-04). |
| Cookie session ignores `expires_at` (F-13) | **FIXED** | `deps.py:50-52` `if sess.expires_at is not None and sess.expires_at <= _utcnow(): return None` | Refresh path already checked expiry (`auth.py:336`). |
| `GET /logout` clears cookie only (F-21) | **OPEN** | `main.py:906-915` only calls `clear_session_cookie(response)` | `POST /api/v1/auth/logout` does revoke (`auth.py:372-375`). Access tokens are not revoked (F-39). |
| Open redirect `/\host` (F-12) | **OPEN** | `auth.py:77` `if next_url and next_url.startswith("/") and not next_url.startswith("//"):`; `proxy_prefix.py:187-188` `if not prefix or not isinstance(path, str): return path` | With the default root mount `prefix_path` returns the path unchanged, so `/login?next=/\evil.example` still redirects off-site. |
| `AUTH_COOKIE_SECURE` default False; CSP unsafe; CORS `*` (F-19, F-21, F-43) | **OPEN** | `config.py:194` `AUTH_COOKIE_SECURE: bool = False`; `main.py:298` `script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval'`; `config.py:428` `CORS_ORIGINS: str = "*"` | `validate_production_settings` forces Secure in production only (`config.py:645-646`). |
| License routes anonymous; fingerprint PowerShell on the event loop (F-12) | **PARTIAL** | Anonymous closed: `license.py:52` `_user=Depends(get_current_user_any)`, `:59` `_admin=Depends(require_admin)` | Any authenticated role gets customer name, organisation and email (`license.py:33-35`). `lic.get_summary()` (`license.py:55`) and `get_hardware_info()` (`license.py:63`) still run synchronously inside `async def` handlers. |
| License gate never applied (F-07 / F-11) | **OPEN** | `grep` for `require_valid_license` and `require_license_feature` in `app/` finds only `app/core/licensing/deps.py` and `__init__.py` | Revocation still in-memory. |
| ViewerAuthGate `share=` substring bypass; `/admin` not gated (F-31, F-20) | **OPEN** | `main.py:377` `if "share=" in query:  # externally-delivered patient share link`; gated list `main.py:364-369` has no `/admin` | Impact limited to static HTML shells, since APIs enforce their own auth. |
| `run_paxs.py` hard-coded `reload=True` (F-27) | **PARTIAL** | `run_paxs.py:89` `reload=not _is_production(),`; `deploy/Dockerfile:33` starts `run_paxs.py` | Reload is off only in production; the container runs with reload on by default. `PAXS_HOST` default `0.0.0.0` (`run_paxs.py:64`) unchanged. |
| Anonymous `/stats` (45 COUNTs) and unauthenticated `/metrics` (F-31, F-41) | **PARTIAL** | `/stats` now auth (`dashboard.py:242-244`); `/metrics` gated (`metrics.py:181-182`, `:59`) but allow-list defaults to RFC1918 (`config.py:359-361`) | `/stats` query count unchanged (not re-counted). `/metrics` reachable through the LB (N-01). |
| Read-replica dependency double-yield (F-14) | **OPEN** | `session.py:202-216`: `yield session` at 210 inside `try`, `except Exception:` at 212 falls through to a second `yield session` at 216 | A 404/409 raised by the endpoint while the replica toggle is on becomes RuntimeError (500). |
| Sync toggle query per request (F-09 perf) | **OPEN** | `session.py:202` `if ReadAsyncSessionLocal is not None and _replica_toggle_enabled():`; `_replica_toggle_enabled` (147-162) is a sync `SyncSessionLocal` query | Blocks the event loop per request when a replica is configured. |
| `init_db` `create_all` on boot; migrations stamp on "already exists" (F-10, F-15) | **FIXED, with residuals** | `session.py:234-~278` no `create_all` for existing DBs; `db_admin.py:336-344` raises `RuntimeError` instead of stamping; `db_admin.py:352-371` `stamp_head` exists | Residuals: `worker.py:420` still runs `Base.metadata.create_all(sync_engine)` in `run_standalone` (399); `stamp_head` has no route and no CLI caller, so the "explicit stamp procedure" the error message promises is unreachable (N-08). |
| Five model tables without a migration (F-16) | **FIXED** | `migrations/versions/b4567890c1d2_c10_tenant_scoped_patient_identity.py:75-124` (`op.create_table`, `assigning_authorities`, `patient_identities`); `c5678901d2e3_sync_chain_with_models.py:38` (`organization_settings`, `report_templates`, `lossless_compression_rules`) | Head is now `d6789012e3f4`. |
| HSTS sent over HTTP (F-18) | **PARTIAL** | `main.py:312-313` adds HSTS on `settings.HTTPS_ENABLED` alone; `run_paxs.py:54-58` refuses start only in production | Dev with `HTTPS_ENABLED=true` and no cert still gets HTTP plus HSTS. |
| Session revocation on admin password reset (F-16) | **PARTIAL** | `users.py:353-361` revokes cookie sessions | No `by` actor in audit (`users.py:362`); bearer access tokens remain valid until expiry (`deps.py:61-78` checks no session). |
| Settings registry bounds (F-22) | **PARTIAL** | Bounds only on `VOLUME_CACHE_BUDGET_MB` and `DICOM_FRAME_CACHE_MB` (`settings_service.py:430-444`); `TOKEN_EXPIRE_MINUTES` (273-278), `REFRESH_TOKEN_EXPIRE_DAYS` (279-284) and `RATE_LIMIT_LOGIN_PER_MINUTE` (304-308) have no bounds | |
| Runtime-editable security toggles (F-22) | **OPEN** | Editable: `ALLOW_REGISTER` (285-290), `RATE_LIMIT_ENABLED` (298-303), `DICOMWEB_PUBLIC` (57-62), `TRUSTED_PROXIES` (291-297), `DICOMWEB_CLIENT_ALLOW_PRIVATE` (314), `STORAGE_ALLOW_LEGACY_PATHS`, `LDAP_*`, `S3_ENDPOINT_URL` (227), `PUBLIC_BASE_URL` (45) | Not editable: `SECRET_KEY`, `DEBUG`, `ENVIRONMENT`, `AUTH_COOKIE_*`, `METRICS_ALLOW_IPS`, `FORWARDED_ALLOW_IPS`, `ROOT_PATH`, `REGISTER_ALLOWED_ROLES`, `MAX_FAILED_ATTEMPTS`, `CORS_ORIGINS` (verified by grep). |
| Config PATCH audited (F-17 / F-23) | **OPEN** | `api/v1/config.py` and `settings_service.py` contain no audit call (grep) | No actor is recorded for runtime setting changes. |
| Lockout counter not reset on expiry (F-14 in auth doc) | **OPEN** | `auth.py:267-269` `user.failed_attempts = (user.failed_attempts or 0) + 1` / `if user.failed_attempts >= settings.MAX_FAILED_ATTEMPTS:`; success-only reset at `:290` | One wrong password after expiry re-locks. |
| Login timing oracle, disabled-account oracle (F-11, F-43) | **OPEN** | Unknown user returns before `verify_password`: `auth.py:251-253`; disabled check after password: `auth.py:284-287` | |
| Refresh race (F-15) | **OPEN** | `auth.py:318-320` `select(Session)...scalar_one_or_none()` with no `FOR UPDATE` | |
| Bearer token survives logout/disable/reset (F-39, F-53) | **OPEN** | `deps.py:61-78` checks only signature, `sub` and `is_active` | Expiry is `TOKEN_EXPIRE_MINUTES` (15 default; 480 in `.env.example:122`). |
| `COOKIE_MAX_AGE` computed at import (F-39) | **OPEN** | `security.py:26` | |
| DB/Redis saved-password fallback to submitted host (F-25) | **OPEN** | `database.py:91` `cfg["password"] = db_admin.saved_password()`; `redis_panel.py:77` `cfg["password"] = redis_service.saved_redis_password()` | Admin-only. |
| `.env` injection via host/name (F-24) | **OPEN** | `db_admin.py:189` `new_line = f'DATABASE_URL="{url}"'` (unchanged) | |
| Control-panel arbitrary executables (F-26) | **OPEN** | `admin_panel_service.py` diff touches only lines 35 and 329-334 | Not re-read in full (outside V1 file list beyond the diff). |
| STOW-RS anonymous (F-03, S-03) | **FIXED (private mode), OPEN (public mode)** | `dicomweb.py:64` router guard; `deps.py:282-283` public mode returns anonymous for writes; `dicomweb.py:941-943` `user: User | None = Depends(get_optional_user)` | See N-06. |
| LDAP mock login (S-06, F-1) | **FIXED** (cross-area) | `grep domainpass app/` finds only the `ldap_service.py:15` docstring; `ldap_service.py:38` `NO_LOCAL_PASSWORD_HASH`; real bind at `ldap_service.py:197-208` | `ldap.py` route not re-read. Note N-10 about TLS. |
| Destructive/egress operations any-role (S-08) | **PARTIAL** (cross-area, grep/AST only) | Fixed: lifecycle mutations `require_admin` (`lifecycle.py:84-186`), routing mutations `require_admin`/`require_destructive_role` (`routing.py:88-201`), lossy `require_destructive_role` (`lossy_compression.py:88-172`), split/merge `require_destructive_role` (`split_merge.py:48, 84`). Still any-role: `migration.py:56-61` POST `/folder` (`get_current_user`), `nodes.py:121-123` PUT, `:147-149` DELETE, `:161-163` POST ping (`get_current_user`), `reports.py:81-82` POST create, `reports.py:118-121` PATCH | Outside V1 file scope; reported for the lead. |
| Requirements hygiene (F-38) | **OPEN** | `requirements.txt` has no pins; `pytest`/`httpx` in runtime list (38-39) | |

---

## 3. New findings

Ordered by severity. Items marked ❓ depend on uvicorn's `ProxyHeadersMiddleware`, which is not installed here and needs a runtime check.

### N-01 🟡 `/metrics` is reachable from anywhere that can reach the LB, because the allow-list checks the proxy's private address
- Code: `app/api/v1/metrics.py:57-59`
  ```python
  client = request.scope.get("client")
  client_ip = client[0] if client else None
  if client_ip and client_ip in TrustedHosts(settings.METRICS_ALLOW_IPS):
  ```
  Default: `app/core/config.py:359-361` `"127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"`.
- Topology: `deploy/docker-compose.lb.yml:16` publishes `"8000:8000"` on the host; `deploy/haproxy.cfg:38-43` forwards `http_in` to `paxs_api` with no path restriction. Nodes get no `FORWARDED_ALLOW_IPS` / `PAXS_FORWARDED_ALLOW_IPS` (uvicorn trusts only 127.0.0.1, `run_paxs.py:78`), so `scope["client"]` is the HAProxy container address, which is in 172.16.0.0/12.
- Failure: `curl http://<lb-host>:8000/metrics` from the internet returns instance name, version, request counter, queue depths, replica lag, cache stats and DB status with no credentials.
- Fix: default the allow-list to loopback only; resolve the client address only through an explicit trusted-proxy list; or block `/metrics` at HAProxy with a path ACL and scrape nodes on the private network directly.

### N-02 🟡 The new trusted-proxy default collapses all clients into one rate-limit bucket behind the shipped LB (regression from H07)
- Code: `app/api/ratelimit.py:43-50` keys on `request.client.host` (the HAProxy address) and only honours XFF for an exact match in `TRUSTED_PROXIES` (`config.py:232`, default loopback). The deploy docs and compose never set `TRUSTED_PROXIES` (grep finds no reference outside `config.py`, `settings_service.py:291`, `ratelimit.py`).
- Limits in effect: login/register/refresh share the `"auth"` window at `RATE_LIMIT_LOGIN_PER_MINUTE=10` per minute (`config.py:233`; `auth.py:181, 245, 306`); every request counts against `RATE_LIMIT_GLOBAL_PER_MINUTE=300` per minute (`config.py:234`; `main.py:557-560`).
- Failure: in `docker-compose.lb.yml` all users of a node share one bucket. Silent token refresh (`POST /auth/refresh`, same bucket as login) across the ward returns 429 once 10 refreshes land in a minute. The viewer's API traffic for about 30 concurrent users exceeds 300/min, and everyone gets 429.
- Fix: make `TRUSTED_PROXIES` CIDR-aware (reuse `TrustedHosts` from `proxy_prefix.py:77-108`), set it in the LB compose to the Docker subnet, and document it. Consider taking the rightmost untrusted XFF entry (see N-04).

### N-03 🟡 Four trusted-proxy settings, one of them documented wrongly; the prefix header is ignored under the documented configuration
- Four independent knobs: `FORWARDED_ALLOW_IPS` (`config.py:56`, used by `main.py:577` for `ForwardedPrefixMiddleware`); `PAXS_FORWARDED_ALLOW_IPS` (read directly by `run_paxs.py:78` for uvicorn's `forwarded_allow_ips`); `TRUSTED_PROXIES` (`config.py:232`, rate limiter only); `METRICS_ALLOW_IPS` (`config.py:359`, metrics only). `PAXS_ROOT_PATH` (`run_paxs.py:79`) duplicates `ROOT_PATH` (`config.py:51`).
- Documentation: `deploy/REVERSE-PROXY.md:144-145`: "Add `/paxs` to `PAXS_FORWARDED_ALLOW_IPS` (or the Docker subnet) or the prefix header is ignored." Per code, the prefix header is governed by `FORWARDED_ALLOW_IPS` (`main.py:577`), not `PAXS_FORWARDED_ALLOW_IPS`. The compose example at `REVERSE-PROXY.md:170` therefore leaves the header ignored.
- Second issue ❓: `ForwardedPrefixMiddleware` decides trust from `scope["client"]` (`proxy_prefix.py:130-134`). If uvicorn's ProxyHeadersMiddleware rewrites `scope["client"]` to the end-user address for a trusted peer (its documented behaviour; not verifiable here), the check compares the end user, not the proxy, and succeeds only for `FORWARDED_ALLOW_IPS="*"` (`proxy_prefix.py:100`).
- Failure: an operator follows `REVERSE-PROXY.md`, sets `PAXS_ROOT_PATH=/paxs` and `PAXS_FORWARDED_ALLOW_IPS=172.18.0.0/16`, and gets links and SPA assets without the prefix. The "fix" that works is `FORWARDED_ALLOW_IPS=*`, which leads to N-04.
- Fix: one trusted-proxy list used by all consumers, matched against the socket peer (not the rewritten client); correct `REVERSE-PROXY.md:144-145` and `.env.example:192` (which documents only `PAXS_FORWARDED_ALLOW_IPS`).

### N-04 🟡 The recommended wildcard makes client identity spoofable (rate limit and metrics ACL); leftmost-XFF rule is latent
- Code: `run_paxs.py:75` comment: "set PAXS_FORWARDED_ALLOW_IPS to the proxy address, subnet, or `"*"`". `_client_key` uses `request.client.host` (`ratelimit.py:43`) and `require_metrics_access` uses `scope["client"]` (`metrics.py:57-59`). ❓ With a wildcard trusted peer, uvicorn's ProxyHeadersMiddleware takes the leftmost `X-Forwarded-For` entry as the client (its `always_trust` branch); this must be confirmed on the installed version.
- Failure: `X-Forwarded-For: 10.0.0.1` on `/metrics` passes the RFC1918 allow-list (`config.py:360`). `X-Forwarded-For: <random>` on each login gives a fresh bucket. Both defeat the controls this review was asked to check.
- Latent sub-issue 🟢: `ratelimit.py:46-47` takes the leftmost XFF entry when the peer is trusted. The documented nginx config appends (`REVERSE-PROXY.md:99, 125`, `$proxy_add_x_forwarded_for`), so a client-supplied leftmost value is kept. Masked today because uvicorn already rewrote `client` for loopback peers (`forwarded_allow_ips` default 127.0.0.1); it becomes exploitable if an operator lists the proxy in `TRUSTED_PROXIES` but not in `PAXS_FORWARDED_ALLOW_IPS`.
- Fix: stop recommending `"*"`; use an explicit CIDR list; take the rightmost untrusted entry in both places.

### N-05 🟡 The production gate runs once, before DB overrides, and a runtime edit can disable a gated invariant
- Code: `main.py:490` `validate_production_settings(settings)` runs before `main.py:500` `apply_db_overrides()`. `config.py:661-662` requires `RATE_LIMIT_ENABLED` true in production. `RATE_LIMIT_ENABLED` is runtime-editable (`settings_service.py:298-303`) and `PATCH /api/v1/config` is admin-only (`config.py:89-90`).
- Failure: an admin PATCHes `RATE_LIMIT_ENABLED=false`. The limiter turns off immediately (`ratelimit.py:64`) and is not re-validated. On restart the validator passes (override not yet applied) and the override is replayed afterwards (`main.py:500`), so the production invariant is silently broken until someone reads the registry.
- Fix: run the production validator again after `apply_db_overrides()` and on every `PATCH`; or make production-invariant keys non-editable.

### N-06 🟡 "DICOMweb public access" also opens anonymous STOW-RS writes
- Code: `deps.py:282-283`:
  ```python
  if settings.DICOMWEB_PUBLIC:
      return await get_optional_user(request, credentials)
  ```
  The router guard (`dicomweb.py:64`) passes anonymous writes through, and `stow_studies` (`dicomweb.py:941-943`) only uses `user` for the tenant check. The admin description (`settings_service.py:61`) says "Allow QIDO/WADO/STOW without a login token".
- The `.env.example` (`DICOMWEB_PUBLIC=false`, line 158) and the older docs describe the switch as "open read access". A reader who flips it for a LAN viewer also opens writes.
- Failure: an admin enables public DICOMweb for the viewer. Any host POSTs multipart DICOM to `/dicom-web/studies` (tenant `None`). Reused UIDs overwrite and repoint stored objects (the D-01 / D-02 scenario).
- Fix: split the flag into read and write switches, keep writes private unless explicitly enabled, and correct the registry text and `.env.example` comment.

### N-07 🟡 The migration advisory lock is released before the upgrade runs
- Code: `app/services/db_admin.py:275-291`:
  ```python
  with engine.connect() as conn:
      got = conn.exec_driver_sql(f"SELECT GET_LOCK('{name}', 30)").scalar()
  return bool(got)
  ```
  The `with` block closes the connection on return. The engine is `NullPool` (`db_admin.py:327`), so the DBAPI connection is closed and MySQL releases the GET_LOCK (its locks end with the session). `run_migrations` then calls `command.upgrade(cfg, "head")` (`db_admin.py:331`) with no lock held. `_release_advisory_lock` (`:294-301`) runs on a new connection.
- The H11 docstring ("Serialises migrations across processes on MySQL", `db_admin.py:277-279`) is therefore not true.
- Failure: `docker-compose.lb.yml` starts three nodes with the same env (`:36-46`), and each runs `init_db` (`session.py:234`). On an empty MySQL, two nodes can run `alembic upgrade head` concurrently. The loser hits "already exists" and raises `RuntimeError` (`db_admin.py:338-344`), so that node fails startup, or DDL interleaves.
- Confidence: high from code plus documented MySQL semantics; a two-process test would confirm.
- Fix: keep the `GET_LOCK` connection open for the whole upgrade and release it in `finally`; or use a lock row / leader election.

### N-08 🟡 After H11 there is no reachable path to reconcile a create_all-bootstrapped database, and the standalone worker still calls create_all
- Code: `app/services/worker.py:420` `Base.metadata.create_all(sync_engine)` inside `run_standalone` (399). `session.py:240-243` docstring says `create_all` "is now used only by the test fixtures", which is wrong. `db_admin.py:352` `stamp_head` has no caller (grep). `db_admin.py:339-343` tells the operator to "run the explicit stamp procedure", which does not exist as a route or CLI.
- Failure: a database first created by `python -m app.services.worker` (no `alembic_version`) is treated as non-empty by `init_db`, which only logs drift (`session.py` non-empty branch). The next `alembic upgrade head` raises `RuntimeError`, and no endpoint can stamp it. Missing columns persist and produce runtime 500s until someone runs SQL by hand. The previous code stamped automatically, so this is a behavioural regression.
- Fix: remove `create_all` from `run_standalone`; expose `stamp_head` through an ADMIN route or CLI with the parity check; correct the docstring.

### N-09 🟡 `GET /api/v1/config` returns the whole registry, including LDAP, S3 and rate-limit settings, to every authenticated role
- Code: `app/api/v1/config.py:55-56` `@router.get("")` / `async def get_config(user: User = Depends(get_current_user))`; the response includes every `EDITABLE_KEYS` field (`config.py:62-73`, `_field` at `:31-49`) and the health payload (`:79-81`). Registry includes `S3_ENDPOINT_URL` (`settings_service.py:227`), `LDAP_SERVER`/`LDAP_BASE_DN`, `TRUSTED_PROXIES`, `ALLOW_REGISTER`, `RATE_LIMIT_*`.
- Failure: any PHYSICIAN or TECHNOLOGIST account reads the S3 endpoint, LDAP host and base DN, and the trusted-proxy and rate-limit values, which is useful recon for the other findings here.
- Fix: restrict `GET /api/v1/config` to ADMIN, or split the non-sensitive fields out for non-admins.

### N-10 🟡 (cross-area) LDAP_USE_TLS is declared but never read; `ldap://` binds send the password in clear
- Code: `app/core/config.py:282` `LDAP_USE_TLS: bool = False            # startTLS on ldap:// (ldaps:// implies TLS)`. `grep LDAP_USE_TLS app` finds only that line. `app/services/ldap_service.py:197-208` builds `ldap3.Server(..., use_ssl=...startswith("ldaps://"))` and `ldap3.Connection(...)` with no `start_tls` call.
- Failure: an operator follows the comment `LDAP_SERVER=ldap://dc01:389` and sends user passwords in clear over the LAN. The setting promises a protection that does not exist.
- Fix: call `conn.start_tls()` when `LDAP_USE_TLS` is set, or refuse `ldap://` unless the setting is explicitly off with a warning; remove the dead setting if unused.

### N-11 🟢 `resolve_public_base_url` trusts `X-Forwarded-Host` / `X-Forwarded-Proto` from any peer, and the boot shim injects the result unescaped into a script block
- Code: `config.py:570` `host = (headers.get("x-forwarded-host") or headers.get("host") or "")`; `config.py:575` `proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()`. Callers: share URL and QR (`sharing_service.py:72-82`; `share.py:50`; anonymous `share.py:127-132` `GET /api/v1/share/{token}/qr.png`); admin launch (`admin_panel_service.py:333-334`); boot shim (`main.py:239-242` → `proxy_prefix.py:286-294`, where `json.dumps(public_base)` is placed inside a `<script>` block without escaping `<`, `>` or `/`).
- Impact: a client that controls the forwarded host can make the QR code for a token it already holds point elsewhere; a Host or X-Forwarded-Host containing `</script>` could break out of the boot shim in sub-path deployments. Browsers cannot set Host or X-Forwarded-Host cross-origin, and the breakout depends on whether uvicorn accepts such header bytes (❓), so the rating is 🟢.
- Fix: derive the public origin only from `PUBLIC_BASE_URL` or from headers of trusted peers; escape JSON for script context (`<`, `>`, `&`, U+2028/2029).

### N-12 🟢 `normalise_prefix` accepts quotes and angle brackets, and the value is inserted into HTML and JS
- Code: `proxy_prefix.py:64-74` rejects only `://` and `..`. The prefix is inserted by `prefix_markup` (`:199-216`, string replacement into markup) and `build_boot_script` (`:286-294`).
- Only a trusted peer's `X-Forwarded-Prefix` or `ROOT_PATH` reaches this, so impact is low. Fix: restrict the prefix to `[A-Za-z0-9._~/-]`.

### N-13 🟢 LDAP role mapping is runtime-editable by admin, with no audit
- `settings_service.py:94` (`LDAP_DEFAULT_ROLE`) and the `LDAP_GROUP_ROLE_MAP` entry are editable. Setting `LDAP_DEFAULT_ROLE=ADMIN` makes every unmapped directory user an admin on next login. Admin-only, no audit (see N-14). Fix: make the mapping restart-only or require a confirmation; audit the change.

### N-14 🟢 Runtime configuration changes are not audited and not attributed
- `api/v1/config.py:89-118` and `settings_service.py:543-562` have no audit or log-with-actor call (grep). The only log is `_apply_to_runtime` on failure (`settings_service.py:570`). Fix: write an audit row with actor, key, old and new values for every PATCH.

### N-15 🟢 Decode-failure log helper emits file-derived strings unsanitised
- `app/core/dicom_diagnostics.py:77-88` (`_attr`) and `:114-143` (`decode_context`) put `Modality`, `SeriesInstanceUID`, `SOPInstanceUID`, photometric and rows into log lines without stripping control characters. A DICOM value containing CR/LF could forge log lines. Not verified against pydicom's value handling. Fix: `repr`/escape values.

### N-16 🟢 `POST /api/v1/db/init` response field `stamped` is now always False
- `database.py:174` `steps, _stamped = ...` and `:200` `"stamped": bool(_stamped)`. Misleading field; drop it or keep it as an explicit `False`.

### N-17 🟢 Settings writes skip validation for string keys and bypass the PUBLIC_BASE_URL validator
- `settings_service.py:483-516` `_coerce` returns `str(value)` for `str` types, with no check for `TRUSTED_PROXIES`, `LDAP_*`, `S3_ENDPOINT_URL`. `set_override` uses `setattr` (`settings_service.py:568`), which bypasses the pydantic `field_validator` (`config.py:502-521`). `resolve_public_base_url` re-checks (`config.py:560`), so the impact is limited to the stored value.

### N-18 🟢 Possible ValueError on login when `AUTH_COOKIE_SAMESITE` is invalid outside production
- `security.py:38` passes `settings.AUTH_COOKIE_SAMESITE` through unchanged; only `validate_production_settings` checks it (`config.py:650-658`). ❓ Starlette's `set_cookie` may assert on an invalid value, turning login into a 500 in dev. Not verified (Starlette source not in the repo).

---

## 4. Documentation deltas

### 4.1 Settings (new or changed; name, default, meaning)
| Setting | Default (HEAD) | Meaning / note |
|---|---|---|
| `ENVIRONMENT` | `development` (`config.py:191`) | `production` or any unknown/empty value or `DEBUG` pinned false turns on the H06 gates (SECRET_KEY, AUTH_COOKIE_SECURE, SAMESITE, DEBUG, RATE_LIMIT_ENABLED). Checked at startup only (N-05). |
| `ALLOW_REGISTER` | `False` (was True) | Self-registration off. Bootstrap admin still possible when `users` is empty (`auth.py:193-194`). Runtime-editable. |
| `REGISTER_ALLOWED_ROLES` | `PHYSICIAN` | Roles self-registration may request. Destructive roles removed (`auth.py:206`). Not runtime-editable. |
| `DICOMWEB_PUBLIC` | `False` (was True in docs) | Now effective. Also opens anonymous STOW writes (N-06). Runtime-editable. |
| `TRUSTED_PROXIES` | `127.0.0.1,::1` | Peers whose XFF the rate limiter honours. Exact match only. Runtime-editable. (N-02) |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Peers trusted for `X-Forwarded-Prefix/Host/Proto` (ForwardedPrefixMiddleware only). Not the uvicorn list. (N-03) |
| `PAXS_FORWARDED_ALLOW_IPS` (env, `run_paxs.py:78`) | `127.0.0.1` | uvicorn `forwarded_allow_ips`. Not a Settings field. |
| `ROOT_PATH` / `PAXS_ROOT_PATH` | `""` | Sub-path mount. Two names for the same thing. |
| `METRICS_ALLOW_IPS` | loopback + 10/8, 172.16/12, 192.168/16 | Unauthenticated `/metrics` allow-list (N-01). |
| `AUTH_COOKIE_SAMESITE` | `lax` | Validated in production (`config.py:650-658`). Not runtime-editable. |
| `AUTH_COOKIE_SECURE` | `False` | Must be true in production (`config.py:645-646`). Not editable. |
| `SECRET_KEY` | `paxs-insecure-dev-key-change-me` | Unchanged default. Rejected only in production (`config.py:597-602, 639-644`). |
| `FUSION_ENABLED` | `True` | Independent of `FHIR_ENABLED` (`main.py:683-684`). |
| `STORAGE_ALLOW_LEGACY_PATHS` | `False` | Legacy path resolution escape hatch. Runtime-editable. |
| `LOSSY_COMPRESSION_ENABLED` | `False` | Fail-closed. |
| `LDAP_ENABLED`, `LDAP_SERVER`, `LDAP_BASE_DN`, `LDAP_USER_DN_TEMPLATE`, `LDAP_GROUP_ROLE_MAP`, `LDAP_DEFAULT_ROLE`, `LDAP_TIMEOUT_SECONDS`, `LDAP_USE_TLS` | off / empty / 5.0 / False | `LDAP_USE_TLS` is unused (N-10). |
| `VOLUME_CACHE_BUDGET_MB` | `1024` (min 64, max 65536) | Hard cache ceiling. |
| `DICOM_FRAME_CACHE_MB` | `256` (min 16, max 16384) | Hard cache ceiling. |
| `VIEWER_PROCESSING_MODE`, `VIEWER_PROCESSING_ROLE_MAP`, `HYBRID_*` | `hybrid`, `""`, see `config.py:312-374` | Viewer processing distribution. |
| `TOKEN_EXPIRE_MINUTES` | `15` (`.env.example:122` ships `480`) | Unbounded in registry. |
| `SCRIPTS_DIR` | `scripts` (unchanged) | Still the repo tooling folder (S-10). |
| `DICOMWEB_CLIENT_ALLOW_PRIVATE` | `False` | SSRF guard. Runtime-editable (F-22). |

### 4.2 Endpoints with changed auth (from the diff)
| Endpoint | Before | After | Evidence |
|---|---|---|---|
| `GET /api/v1/license` and `/status` | anonymous | any authenticated role | `license.py:48-55` |
| `GET /api/v1/license/fingerprint` | anonymous | ADMIN | `license.py:58-59` |
| `GET /api/v1/health` | anonymous | ADMIN | `dashboard.py:235-236` |
| `GET /api/v1/stats` | anonymous | any authenticated role | `dashboard.py:242-244` |
| `GET /api/v1/studies/{uid}`, `.../instances`, `.../thumbnail.png` | anonymous | any authenticated role | `dashboard.py:769-770, 824-825, 852-854` |
| `GET /api/v1/redis/status`, `/keys`, `/key`, `POST /ping` | optional | any authenticated role | `redis_panel.py:63-64, 95-108, 135-136` |
| `GET /metrics` | anonymous | authenticated, or peer in `METRICS_ALLOW_IPS` | `metrics.py:37-61, 181-182` |
| All `/dicom-web/*` | per-route optional/none | router-level `require_dicomweb_access` | `dicomweb.py:64`, `deps.py:329-344` |
| `POST /api/v1/auth/register` | open for any role except ADMIN | closed by default; PHYSICIAN only when open | `config.py:207, 210`; `auth.py:196-217` |
| `POST /api/v1/users/{id}/reset-password` | sessions kept | cookie sessions revoked | `users.py:353-361` |
| `POST /api/v1/db/init` | auto-stamp | no auto-stamp; `stamped` always false | `database.py:174, 200`; `db_admin.py:336-344` |
| `PATCH /api/v1/config` | ADMIN | ADMIN (unchanged); now offloaded | `config.py:89-103` |
| Lifecycle mutations | any role | ADMIN | `lifecycle.py:84-186` |

### 4.3 Middleware stack (outermost first, verified in code)
1. uvicorn ProxyHeadersMiddleware (`run_paxs.py:90-91`; ❓ internals)
2. `SecurityHeadersMiddleware` (`main.py:580`)
3. `ForwardedPrefixMiddleware` (`main.py:574-578`; trusts `FORWARDED_ALLOW_IPS`)
4. `ViewerAuthGateMiddleware` (`main.py:569`)
5. `MetricsMiddleware` (`main.py:565-566`)
6. `GzipCompressionMiddleware` (`main.py:564`)
7. `RateLimitMiddleware` (`main.py:557-560`; skips `/static` and `/dicom-web`)
8. `CORSMiddleware` (`main.py:547-553`)
9. Router

### 4.4 Startup sequence changes (lifespan, `main.py:476-535`)
1. Default executor sized to `IO_THREAD_POOL_WORKERS` (482-485).
2. `validate_production_settings(settings)` (490). Fails closed in production.
3. `validate_memory_budgets` warnings (495-496).
4. `init_db()` (497): empty DB runs alembic `upgrade head`; non-empty DB only logs drift; no `create_all`.
5. `apply_db_overrides()` (500). Runs after validation (N-05).
6. Worker, DICOM receiver, HL7, license manager, heartbeat, storage sweep (501-522).
Shutdown: cancels heartbeat and sweep (not awaited), then receiver, worker, HL7, DB close (528-535).

`run_paxs.py`: `load_dotenv`, then `_load_ssl_context` (fail-closed in production, 33-61), then `uvicorn.run` with `reload=not production`, `proxy_headers=True`, `forwarded_allow_ips` and `root_path` from environment variables.

### 4.5 Roles and permission model (`app/core/roles.py`)
- `ROLES = ("ADMIN", "RADIOLOGIST", "PHYSICIAN", "TECHNOLOGIST")` (15). All uppercase.
- `DESTRUCTIVE_ROLES = ("ADMIN", "RADIOLOGIST")` (18). Used by `deps.require_destructive_role` (123-129), `reports.py:26`, and removed from self-registration (`auth.py:206`).
- `WRITE_ROLES = ("ADMIN", "TECHNOLOGIST")` (21). Used by the worklist write guard (`worklist.py:81-82`). RADIOLOGIST and PHYSICIAN cannot write worklist entries.
- `ADMIN_ROLE = "ADMIN"` (24). Defined, no callers.
- `normalize_role(value)` (27-32): strips, uppercases, returns None for unknown. Used by `ldap_service.py:77, 99, 277` to map directory groups; unknown groups fall through to `LDAP_DEFAULT_ROLE` or are denied.
- `is_known_role(value)` (35-37).
- Checks in code: `require_admin` (`deps.py:132-135`) requires `role == "ADMIN"`; `require_destructive_role` requires membership in `DESTRUCTIVE_ROLES`; `get_current_user*` only check authentication, so any role passes.
- Landing per role (`auth.py:60-65`): ADMIN to `/`, others to `/viewer`.
- Bootstrap: the first registered account becomes ADMIN (`auth.py:193-194`).

### 4.6 Other documentation drift to fix
- `deploy/REVERSE-PROXY.md:144-145` and `:170`: wrong variable for the prefix trust (N-03).
- `.env.example:122`: `TOKEN_EXPIRE_MINUTES=480` versus code default 15.
- `.env.example:97` lists `METRICS_ALLOW_IPS` with RFC1918 ranges (N-01).
- `session.py:240-243` docstring says `create_all` is used only by tests (N-08).
- `run_paxs.py:75` recommends `"*"` (N-04).
- `config.py:282` comment promises startTLS that is not implemented (N-10).

---

## 5. Open questions

1. **uvicorn internals (❓):** which uvicorn version is pinned, and does `ProxyHeadersMiddleware` rewrite `scope["client"]` to the leftmost or rightmost untrusted XFF entry when `trusted_hosts` is `*` or a loopback peer? This decides whether N-03 and N-04 hold as written. A runtime test: run uvicorn with `proxy_headers=True`, send a request through a loopback and a non-loopback peer with `X-Forwarded-For` and `X-Forwarded-Prefix`, and print `scope["client"]`.
2. **Intended trust model:** should `TRUSTED_PROXIES`, `FORWARDED_ALLOW_IPS`, `PAXS_FORWARDED_ALLOW_IPS` and `METRICS_ALLOW_IPS` be one list? Is the LB intended to be on the Docker subnet (N-02)?
3. **DICOMWEB_PUBLIC scope:** is anonymous STOW-RS intended when the switch is on? The registry text says it is (N-06).
4. **Migration concurrency:** confirm with two processes that the lock is not held during upgrade (N-07). Is the LB topology expected to start three nodes against an empty DB?
5. **Standalone worker and schema:** is `python -m app.services.worker` run against a database before the API ever starts? If yes, N-08 applies.
6. **Production gate:** will `ENVIRONMENT=production` be set in the Docker images and LB compose? Today neither sets it, so the default key is accepted (S-01).
7. **Bootstrap admin:** is anonymous first-registration on an empty database intended? It is now the only unauthenticated write path that creates an ADMIN.
8. **LDAP TLS:** is `ldap://` meant to be supported without startTLS (N-10)? `ldap_service.py` was not re-read beyond the bind block.
9. **Share-token scope on collection routes:** `read_scope` gives share-token holders archive-wide scope (`deps.py:368`). QIDO `/studies`, `/series` and `/instances` force the share study (`dicomweb.py:301-303, 365-369, 395-397`), and WADO instance/series filters apply study and series filters even when `include_all` is set (`query_service.py:182-195`, `apply_instance_filters` at 95-110). No bypass was found, but every new collection route should be checked the same way.
10. **Reports and nodes (cross-area):** S-08 remains for `migration.py:56-61`, `nodes.py:121-163` and `reports.py:81-121`, which use `get_current_user`. Confirm in the relevant area reviews.
