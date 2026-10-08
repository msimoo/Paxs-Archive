# Core: startup, configuration, security primitives, licensing, database & migrations

> **Area deep-dive** — part of the [PAXS Archive documentation](../README.md). Produced by static analysis of commit `e90d32c` (2026-10-08); line numbers refer to that commit. Findings here are consolidated, de-duplicated and prioritized in [CODE_REVIEW.md](../CODE_REVIEW.md) — where the two differ, CODE_REVIEW.md takes precedence. Labels such as `[read]`, `[verified]`, `[V]`, `[H]` or `[inferred]` describe how each claim was checked (read in code vs. inferred).

> ⚠️ **Baseline: this deep-dive describes commit `e90d32c`.** What changed in `5a32aa4` (fixes, regressions, new code and endpoints) is in [5a32aa4/01-core-config-auth.md](5a32aa4/01-core-config-auth.md), [5a32aa4/04-ingest-storage-schema.md](5a32aa4/04-ingest-storage-schema.md). The current status of every finding is in [CODE_REVIEW.md](../CODE_REVIEW.md).

Every in-scope file was read in full with the Read tool (migrations included). Route-level auth was checked with a signature-level scan of every `@router.*` in `app/api/v1/` plus targeted reads of the handlers that matter.

Verification tags used in section 8:
- [read] verified by reading the cited lines
- [grep] repo-wide search showed no other callers or definitions
- [scan] signature-level scan of all routes in `app/api/v1/` (handler bodies were not all read)
- [exp] reproduced with a standalone Python script (local verification script)
- [digest] key pair compared with openssl public-key digests; no key material is reproduced here

---

## 1. Files covered

| Path | Lines | Purpose |
|---|---|---|
| run_paxs.py | 51 | Process entry point: loads .env, resolves TLS, runs uvicorn with `reload=True` |
| app/__init__.py | 3 | Package marker, `__version__ = "0.1.0"` |
| app/main.py | 750 | FastAPI app: lifespan, middleware stack, router includes, static/SPA mounts, login/logout/terms routes |
| app/core/config.py | 335 | `Settings` (pydantic-settings), `cors_origin_list`, `get_settings()` singleton |
| app/core/security.py | 153 | PBKDF2 password hashing, stdlib HS256 JWT, opaque refresh-token helpers, session cookie helpers |
| app/core/licensing/__init__.py | 33 | Re-exports licensing API |
| app/core/licensing/crypto.py | 263 | LicenseData model, RSA-PSS sign/verify (PKCS1v15 fallback), AES-256-GCM container |
| app/core/licensing/deps.py | 46 | FastAPI gates `require_valid_license`, `require_license_feature` (no route uses them) |
| app/core/licensing/fingerprint.py | 257 | Machine fingerprint from MAC/CPU/board/OS identifiers (Windows and Linux paths) |
| app/core/licensing/manager.py | 434 | LicenseManager state machine: load, activate, online validate, feature/seat/quota checks |
| app/db/models.py | 1581 | 43 SQLAlchemy ORM models (DeclarativeBase `Base`) |
| app/db/session.py | 249 | Async (NullPool) and sync (QueuePool) engines, read-replica routing, `init_db`/`close_db` |
| app/db/perf.py | 121 | Slow-query ring buffer, engine timing hooks, pool snapshot |
| app/api/ratelimit.py | 110 | Sliding-window rate limiter (Redis if available, else in-process deque) |
| app/api/v1/deps.py | 120 | Auth dependencies: cookie/bearer resolution, `require_admin`, `require_destructive_role` |
| migrations/env.py | 73 | Alembic env; URL taken from `settings.DATABASE_URL`, mapped to sync driver |
| alembic.ini | 45 | Alembic config (script_location `migrations`, logger levels) |
| migrations/versions/*.py | 1,880 total (38 files) | Schema migrations. Per-file list in section 7 |
| requirements.txt | 36 | Python dependencies (no pins/upper bounds; dev deps mixed in) |
| .env.example | 74 | Sample environment file |
| settings.example.json | 10 | Unrelated gateway/API-key example; not referenced anywhere |

Total in scope: 6,624 lines (4,744 non-migration + 1,880 migration).

---

## 2. Module reference

### run_paxs.py (51)
- Purpose: single-process entry point. Starts uvicorn on `app.main:app`.
- `_load_ssl_context() -> dict | None` (L24-34): returns `{"ssl_certfile","ssl_keyfile"}` only if `HTTPS_ENABLED` and both files exist. Otherwise prints a warning and falls back to plain HTTP. It does NOT generate certificates, despite the comment in config.py:47-49.
- Main block (L36-51): `host = os.getenv("PAXS_HOST","0.0.0.0")`, `port = int(os.getenv("PAXS_PORT","8000"))`, then `uvicorn.run("app.main:app", host=..., port=..., log_level="info", reload=True, **ssl_kw)`. `reload=True` is hard-coded (L49).
- Config read: `HTTPS_ENABLED`, `HTTPS_CERT`, `HTTPS_KEY` (via `settings`). `PAXS_HOST` and `PAXS_PORT` are read straight from `os.getenv` and are not Settings fields.
- Import-time side effects: `load_dotenv()` (L13-17), which runs before `from app.main import app` (L21). `from app.main import app` triggers the full app import (see section 3).

### app/main.py (750)
- Constants: `STATIC_DIR` (L43), `_COMPRESSIBLE_TYPE` regex (L62-65), `_GZIP_MIN_BYTES=1024` (L66), `_GZIP_MAX_BYTES=32 MiB` (L67), `_BINARY_PATH_PREFIXES` (L68), `IMMUTABLE_CACHE`/`NO_CACHE`/`DEFAULT_CACHE` (L158-160), `_NO_CACHE_NAMES` (L163-168), `_HASH_STEM` (L169), `_SPA_ENTRY_PATHS` (L204-206), `_SPA_ASSET_EXT` (L210-211), `_SECURITY_HEADERS` dict (L223-240), `VIEWER_DIR`/`STUDIES_UI_DIR`/`ADMIN_DIR`/`SETTINGS_UI_DIR` (L594-595, 653, 678), `LOGIN_PAGE` (L692), `TERMS_PATH` (L724), `PAXS_VIEWER_GUIDE_PATH` (L739), `app` (L421).
- Classes:
  - `GzipCompressionMiddleware(app, min_bytes=1024)` (L71-146): ASGI wrapper. Buffers and gzips text responses with a known content-length between 1 KiB and 32 MiB.
  - `CachedStaticFiles(StaticFiles).get_response(path, scope)` (L194-201): sets Cache-Control via `_cache_control_for`.
  - `SecurityHeadersMiddleware(app)` (L247-265): adds the `_SECURITY_HEADERS` dict to every HTTP response when `SECURITY_HEADERS_ENABLED`.
  - `ViewerAuthGateMiddleware(app)` (L268-331): redirects unauthenticated requests for `/`, `/viewer*`, `/studies-ui*`, `/settings*` to `/login`.
  - `RateLimitMiddleware(BaseHTTPMiddleware)(app, rate_limiter)` (L347-370): calls the global limiter dependency, returns a JSON 429 on `HTTPException`.
  - `SpaStaticFiles(CachedStaticFiles).get_response` (L574-586): maps 404 to `index.html`.
- Functions: `_cache_control_for(path)` (L172), `_looks_like_asset(path)` (L214), `_cookie_value(cookie_header, name)` (L334), `lifespan(_app)` (L373, asynccontextmanager), nested `_heartbeat_loop()` (L398), routes `index()` (L564), `viewer_root_study_list(request)` (L604), `viewer_splash_logo()` (L633), `viewer_fallback()` (L644), `admin_panel_page()` (L658), `studies_ui_page()` (L671), `settings_ui_page()` (L682), `login_page(request)` (L697), `logout_page()` (L714), `terms()` (L728), `paxs_viewer_guide()` (L743).
- Config read: almost all of the app-level toggles, including APP_NAME/APP_VERSION, DEBUG, IO_THREAD_POOL_WORKERS, WORKER_ENABLED, DICOM_RECEIVER_ENABLED, HL7_ENABLED, LICENSE_HEARTBEAT_HOURS, LICENSE_AUTO_VALIDATE_ONLINE, CORS_ORIGINS, RATE_LIMIT_GLOBAL_PER_MINUTE, FHIR_ENABLED, ADMIN_PANEL_ENABLED, AUTH_GATE_ENABLED, AUTH_COOKIE_NAME, SECURITY_HEADERS_ENABLED, HTTPS_ENABLED, DICOM_HOST/PORT, HL7_HOST/PORT.
- Import-time side effects:
  - `logging.basicConfig(...)` (L37-40), which configures the root logger for the whole process.
  - `_SECURITY_HEADERS["Strict-Transport-Security"]` added when `HTTPS_ENABLED` is true (L243-244). This is a snapshot taken before `apply_db_overrides` runs.
  - Builds the FastAPI app with `docs_url="/api/docs"` and `openapi_url="/api/openapi.json"` (L421-428).
  - Six `app.add_middleware` calls (L430-454).
  - Imports about 47 router modules (L457-505, L552-556). Each module's import-time code runs (services, models, etc.).
  - Reads `viewer-version.txt` (L621-623) and `admin-version.txt` (L662-664) at import.
  - Mounts `/static`, `/viewer`, `/admin`, `/studies-ui`, `/settings` conditionally on directory existence.

### app/__init__.py (3)
- `__version__ = "0.1.0"`. Duplicates `APP_VERSION` default in config.py:11.

### app/core/config.py (335)
- `class Settings(BaseSettings)`: about 115 typed fields (L10-320). Full table in section 4.
- `model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")` (L321). The `.env` path is relative to the current working directory.
- `cors_origin_list` property (L323-327): returns `["*"]` if `CORS_ORIGINS` is `*`, else splits on commas.
- `get_settings()` decorated with `@lru_cache` (L330-332). Module-level `settings = get_settings()` (L335).
- Import-time side effects: reads environment and `.env` immediately. Any invalid env value raises during import.

### app/core/security.py (153)
- Constants: `COOKIE_MAX_AGE = REFRESH_TOKEN_EXPIRE_DAYS*86400` (L26, computed at import), `ALGORITHM="HS256"` (L81), `_PBKDF2_ITERATIONS=200_000` (L82).
- `set_session_cookie(response, token)` (L29-39): `HttpOnly`, `SameSite=Lax`, `Secure=AUTH_COOKIE_SECURE`, `path="/"`, `max_age=COOKIE_MAX_AGE`.
- `clear_session_cookie(response)` (L42-44).
- `generate_refresh_token(family_id) -> str` (L50-58): `f"{family_id}.{secrets.token_urlsafe(32)}"`.
- `hash_refresh_token(token) -> str` (L61-63): SHA-256 hex.
- `refresh_token_family(token) -> str|None` (L66-72).
- `session_token_from_cookie(value) -> str|None` (L75-79): accepts length 16-256.
- `hash_password(password) -> str` (L88-96): PBKDF2-HMAC-SHA256, 16-byte salt, 200k iterations. Output format `pbkdf2_sha256$<iter>$<salt_b64>$<dk_b64>`.
- `verify_password(password, stored) -> bool` (L99-109): splits on `$`, ignores the algorithm field, constant-time compare, returns False on any exception.
- `_b64url`, `_b64url_decode` (L115-120).
- `create_access_token(subject, role, secret, expires_minutes) -> str` (L123-138): JWT with `sub`, `role`, `iat`, `exp`.
- `decode_token(token, secret) -> dict|None` (L141-153): checks signature (constant-time) and `exp`. Does not check `alg`, `iat`, `aud`, or `iss`.
- Imports `uuid` (L15), which is never used (verified: only occurrence is the import).

### app/core/licensing/__init__.py (33)
- Re-exports `LicenseData`, `decrypt_license_container`, `encrypt_license_container`, `generate_rsa_key_pair`, `sign_license`, `verify_license_signature`, `require_license_feature`, `require_valid_license`, `get_hardware_info`, `get_machine_fingerprint`, `LicenseManager`, `LicenseStatus`, `get_license_manager`.

### app/core/licensing/crypto.py (263)
- Constants: `DEFAULT_PUBLIC_KEY_PEM` RSA-2048 public key (L29-37). `_DEFAULT_AES_KEY` = SHA-256 of a hardcoded string literal (L40-42, literal at L41).
- `class LicenseData(BaseModel)` (L45-84): fields `license_id`, `version`, `customer_name`, `customer_email`, `customer_organization`, `customer_country`, `product`, `edition`, `machine_fingerprint` (default `"*"`), `issued_at`, `expires_at`, `max_users`, `max_studies_per_year`, `features` (default list), `signature`.
- `get_canonical_payload_bytes(data) -> bytes` (L87-104): `model_dump(mode="json")`, drops `signature`, `json.dumps(sort_keys=True, separators=(",",":"))`.
- `get_public_key() -> RSAPublicKey` (L107-124): env `PAXS_LICENSE_PUBLIC_KEY` or `PAXS_LICENSE_PUBLIC_KEY_PATH`, else the embedded key.
- `sign_license(data, private_key_pem, password=None) -> LicenseData` (L127-145): RSA-PSS, MGF1-SHA256, max salt, SHA-256.
- `verify_license_signature(data, public_key=None) -> bool` (L148-196): tries PSS, then PKCS1v15, returns False on any error.
- `encrypt_license_container(data, aes_key=None) -> bytes` (L199-214): `b"PAXS" + iv(12) + AESGCM(...)`, AAD `b"PAXS_LIC_v1"`. Key: `aes_key`, or `PAXS_LICENSE_AES_KEY` env, or `_DEFAULT_AES_KEY`. Keys that are not 32 bytes are SHA-256'd.
- `decrypt_license_container(container_bytes, aes_key=None) -> LicenseData` (L217-244): accepts plain JSON (L221-225) without decryption. Otherwise checks magic `PAXS`, decrypts, and maps `InvalidTag` to ValueError.
- `generate_rsa_key_pair() -> (private_pem, public_pem)` (L247-263).
- Import-time side effects: computes the default AES key (L40-42).

### app/core/licensing/deps.py (46)
- `require_valid_license(lic=Depends(get_license_manager))` (L12-26): 403 `LICENSE_REQUIRED` unless `lic.is_active`.
- `require_license_feature(feature_name)` (L29-46): returns a dependency that checks `lic.has_feature`. 403 `FEATURE_NOT_LICENSED`.
- Route usage: none. Grep for both names and for `check_user_seat` / `check_study_quota` finds only definitions and re-exports. See finding F-11.

### app/core/licensing/fingerprint.py (257)
- `_CACHED_FINGERPRINT` process-global (L23).
- `_get_mac_addresses() -> list[str]` (L26-59): `uuid.getnode()` (filtered for multicast bit), plus every non-`lo` interface under `/sys/class/net` on Linux.
- `_get_windows_identifiers() -> (cpu_id, mb_serial, os_serial)` (L62-144): PowerShell `Get-CimInstance` (4 s timeout), then `wmic` fallbacks (3 s timeouts, `shell=True` with fixed strings).
- `_get_linux_identifiers() -> (machine_id, dmi_uuid, product_serial)` (L147-185): `/etc/machine-id` (or `/var/lib/dbus/machine-id`), `/sys/class/dmi/id/product_uuid`, `/sys/class/dmi/id/product_serial`.
- `get_machine_fingerprint(force_refresh=False) -> str` (L188-233): joins sorted components, SHA-256, first 32 hex chars, uppercased. Cached per process.
- `get_hardware_info() -> dict` (L236-257): not cached. Calls `_get_mac_addresses()` and `_get_windows_identifiers()` on every call, so it spawns PowerShell/WMIC each time on Windows.
- Import-time side effects: none.

### app/core/licensing/manager.py (434)
- `class LicenseStatus(str, Enum)` (L31-39): VALID, GRACE_PERIOD, EXPIRED, HARDWARE_MISMATCH, QUOTA_EXCEEDED, REVOKED, INVALID_SIGNATURE, NOT_FOUND.
- `class LicenseManager` (L42-423):
  - `__init__(license_path=None)` (L45-52): calls `load_license()` immediately. `_grace_period_days = 7` is hard-coded (L51).
  - Properties: `license_file_path` (L55-68: explicit path, else `PAXS_LICENSE_PATH`, else `./paxs.lic`, else `./config/paxs.lic`), `status`, `status_message`, `is_active` (VALID or GRACE_PERIOD, L79-81), `license_data`.
  - `load_license(file_path=None) -> LicenseStatus` (L87-198): if the file is missing and `PAXS_DEV_LICENSE` is `1|true|yes`, grants an unlimited enterprise license (L97-136). Otherwise: decrypt, verify signature, check machine fingerprint (unless `"*"`), check expiry with a 7-day grace.
  - `activate_from_bytes(content_bytes, destination_path=None) -> (bool,str)` (L200-224): decrypt, verify, fingerprint, expiry, write file, reload.
  - `activate_online(activation_key, portal_url=None, customer_email=None) -> (bool,str)` (L226-288): urllib POST to `<portal>/api/activate.php` (timeout 15 s), payload includes `hardware_info`.
  - `validate_online(portal_url=None) -> (bool,str)` (L290-345): urllib POST to `<portal>/api/validate.php` (timeout 12 s). If `revoked` is truthy, sets `INVALID_SIGNATURE` (L330-333). If `renewed_license_b64` is present, applies it through `activate_from_bytes`.
  - `has_feature(name)` (L347-351), `check_user_seat(n)` (L353-357), `check_study_quota(n)` (L359-363). Only `has_feature` has callers.
  - `get_summary() -> dict` (L365-423): includes `server_fingerprint`, `hardware_info` (MACs, CPU ID, board/OS serials), `customer_email`, `license_file_path`.
- `_LICENSE_MANAGER_INSTANCE` and `get_license_manager()` (L426-434): unlocked singleton.
- Config read: `LICENSE_PORTAL_URL`, `APP_VERSION`. Not read: `LICENSE_FILE_PATH`, `LICENSE_GRACE_PERIOD_DAYS` (both unused, see F-24).
- Import-time side effects: none.

### app/db/models.py (1581)
- Constants: `BigIntPK = BigInteger().with_variant(Integer, "sqlite")` (L36). `pk()` (L39-40).
- `class Base(DeclarativeBase)` (L30-31).
- `class TimestampMixin` (L43-49): defined but no model inherits it (verified by reading all 43 classes).
- 43 model classes, listed in section 5.
- Import-time side effects: none.

### app/db/session.py (249)
- `to_sync_url(url)` (L43-49): `sqlite+aiosqlite://` to `sqlite://`, `mysql+aiomysql://` to `mysql+pymysql://`.
- `_set_sqlite_pragma(dbapi_connection, _record)` (L52-59): `PRAGMA journal_mode=<SQLITE_JOURNAL_MODE>` (f-string, L55), `busy_timeout=30000`, `foreign_keys=ON`.
- `_ensure_charset(url)` (L66-76): appends `charset=utf8mb4` for MySQL URLs that lack one.
- `async_engine` (L84-86): `create_async_engine(..., poolclass=NullPool)`. Connection-per-request by design (comment L79-82).
- `AsyncSessionLocal` (L91-93): `async_sessionmaker(expire_on_commit=False)`.
- `sync_engine` (L98-112): QueuePool with `pool_size`, `max_overflow`, `pool_pre_ping`, `pool_recycle` on MySQL only.
- `SyncSessionLocal` (L117).
- Read replica (L125-140): `read_async_engine` (NullPool) and `ReadAsyncSessionLocal`, both only if `DB_READ_REPLICA_URL` is set.
- `replica_configured()` (L143-144), `_replica_toggle_enabled()` (L147-162): SYNCHRONOUS query on `SyncSessionLocal` for `ServerSetting` key `dbp_read_replica`. Called on every `get_read_db()` invocation.
- `replica_probe()` (L165-192), `get_read_db()` (L195-220), `get_db()` (L223-231).
- `init_db()` (L234-240): `Base.metadata.create_all` on the async engine. Creates missing tables only. Never alters existing tables.
- `close_db()` (L243-249): disposes async, replica, and sync engines.
- Import-time side effects:
  - On Windows with Python < 3.14, sets `WindowsSelectorEventLoopPolicy` process-wide (L31-40).
  - Creates the async and sync engines, plus a replica engine when `DB_READ_REPLICA_URL` is set. No connection is opened until first use.
  - Attaches SQLite PRAGMA listeners (L88, L114) and slow-query hooks (L89, L115).

### app/db/perf.py (121)
- `SLOW_QUERIES: deque(maxlen=200)` (L33). `_slow_enabled`, `_slow_threshold_ms=500.0` (L34-35).
- `set_slow_query(enabled, threshold_ms)` (L38-42), `slow_query_state()` (L45-46), `slow_query_entries()` (L49-51, newest first).
- `install_slow_query_hooks(engine, label)` (L54-84): `before_cursor_execute` and `after_cursor_execute` listeners. Stores `sql[:600]` and `repr(params)[:200]` (L76-84). Parameters can contain PHI.
- `pool_snapshot(pool) -> dict` (L91-121).
- Module docstring (L13) says `explain_query` lives here. It does not; it lives in `app/services/db_panel.py:190`.
- Import-time side effects: none.

### app/api/ratelimit.py (110)
- `_WINDOWS`, `_LOCK = threading.Lock()`, `_CLEANUP_TICK` (L24-26).
- `_client_key(request) -> str` (L29-33): leftmost `X-Forwarded-For` entry if present, else `request.client.host`. See F-09.
- `rate_limit(window_key, limit, window_seconds)` (L36-102): returns an async dependency. Gated on `RATE_LIMIT_ENABLED`. Tries Redis first (L51-74, pipeline with `zremrangebyscore`, `zcard`, `zadd`, `expire`). Falls back to an in-process deque (L76-100). Raises 429 with `Retry-After`.
- `reset_rate_limits()` (L105-110).
- Import-time side effects: none.

### app/api/v1/deps.py (120)
- `bearer_scheme = HTTPBearer(auto_error=False)` (L14).
- `get_current_user_from_cookie(request) -> User|None` (L17-43): reads cookie `settings.AUTH_COOKIE_NAME`, looks up `Session` by SHA-256 hash. Rejects if `is_revoked`. Does NOT check `expires_at`. Rejects if the user is inactive.
- `get_current_user(credentials) -> User` (L46-63): bearer only. `decode_token`, then loads user by `sub` (username). 401 if the user is missing or inactive. Does not check sessions.
- `get_optional_user(request, credentials) -> User|None` (L66-86): bearer first, then cookie fallback. Never raises.
- `get_current_user_any(request, credentials) -> User` (L89-101): `get_optional_user`, raises 401 if None.
- `DESTRUCTIVE_ROLES = ("ADMIN","RADIOLOGIST")` (L105).
- `require_destructive_role(user) -> User` (L108-114).
- `require_admin(user) -> User` (L117-120).
- Import-time side effects: none.

### migrations/env.py (73)
- `to_sync_url` duplicate of session.py's (L19-27).
- `config.set_main_option("sqlalchemy.url", to_sync_url(settings.DATABASE_URL))` (L31). Passing a URL containing `%` through ConfigParser can fail, because `set_main_option` does not escape `%`.
- `run_migrations_offline()` (L40-50), `run_migrations_online()` (L53-67) with NullPool. `compare_type=True`. No `render_as_batch`.
- Import-time side effects: `fileConfig(...)` (L34) and `Base.metadata` import (L16).

### alembic.ini (45)
- `script_location = migrations`, `prepend_sys_path = .`. URL is provided by env.py. Logger levels: root WARNING, alembic INFO, `paxs` DEBUG.

### requirements.txt (36)
- Core runtime: fastapi>=0.115, uvicorn[standard]>=0.30, pydantic>=2.7, pydantic-settings>=2.3, sqlalchemy[asyncio]>=2.0.30, aiosqlite>=0.20, aiomysql>=0.2, pymysql>=1.1, alembic>=1.13, pydicom>=3.0, pynetdicom>=2.1, numpy>=1.26, pillow>=10.3, python-multipart>=0.0.9, qrcode>=7.4, cryptography>=42.0.0, pylibjpeg (+ libjpeg, openjpeg, rle), pyjpegls>=1.9, imagecodecs>=2024.1.
- Commented out: boto3, celery, redis (line 32, `# redis>=5.0`).
- Dev/test mixed in: pytest>=8.2, httpx>=0.27 (lines 35-36). httpx is also a runtime import (see F-38).
- Not listed but imported: `ldap3` (lazy, guarded), `redis` (unguarded, see F-08).

### .env.example (74)
- Sample values: `DATABASE_URL` (MySQL with `paxs:paxs`, L7), `SECRET_KEY=change-me-to-a-long-random-string` (L39), `TOKEN_EXPIRE_MINUTES=480` (L40), `DEFAULT_COMPRESSION=RLE` (L32), `WORKER_POLL_SECONDS=5` (L36), `DICOMWEB_PUBLIC=true` with comment "PAXs-style viewers need open access on a hospital LAN" (L67-68), `CORS_ORIGINS=*` (L69), `HL7_ENABLED=false`, `FHIR_ENABLED=true`.

### settings.example.json (10)
- Contains `allowNonWorkspaceAccess`, `enableTelemetry`, `gatewayProviders` with a placeholder API key. Not referenced by any file in the repo (grep). Appears to belong to a different tool. See F-37.

---

## 3. Startup sequence

### 3.1 Process start (`python run_paxs.py`)
1. `load_dotenv()` (run_paxs.py:13-17) loads `.env` into `os.environ`.
2. `from app.main import app` (run_paxs.py:21) executes app/main.py at module level:
   - `app.core.config` builds `settings` (config.py:335), reading env vars and `./.env`.
   - `app.db.session` builds the async, sync, and optional replica engines. No connection is opened. On Windows with Python < 3.14 it sets the selector event-loop policy (session.py:31-40).
   - `logging.basicConfig` (main.py:37-40).
   - `_SECURITY_HEADERS` is built, with HSTS only if `HTTPS_ENABLED` (main.py:223-244).
   - `app = FastAPI(lifespan=lifespan, ...)` (main.py:421-428).
   - Middleware is registered (main.py:430-454; order in 3.2).
   - Router modules are imported and included (main.py:457-556).
   - Static mounts and direct routes are registered (main.py:558-749).
3. `if __name__ == "__main__"` (run_paxs.py:36-51): resolves `_load_ssl_context()`, then calls `uvicorn.run("app.main:app", reload=True, ...)`. With `reload=True`, uvicorn runs the server in a child process that re-imports `app.main`. Lifespan runs in that child.

### 3.2 Middleware order (outermost to innermost)
Starlette builds the stack by reversing `user_middleware`, which is in add order reversed. The outermost is `ServerErrorMiddleware`, then:

| # | Middleware | Registered at | Notes |
|---|---|---|---|
| 1 | SecurityHeadersMiddleware | main.py:454 (last added) | Adds CSP, X-Frame-Options, Referrer-Policy, Permissions-Policy, and HSTS if HTTPS_ENABLED |
| 2 | ViewerAuthGateMiddleware | main.py:452 | Redirects `/`, `/viewer*`, `/studies-ui*`, `/settings*` without a session cookie |
| 3 | MetricsMiddleware | metrics.py:42; added main.py:449 | Increments a counter only |
| 4 | GzipCompressionMiddleware | main.py:71; added main.py:447 | Buffers and gzips eligible text responses |
| 5 | RateLimitMiddleware (BaseHTTPMiddleware) | main.py:347; added main.py:440 | Global per-IP limit; skips `/static` and `/dicom-web` |
| 6 | CORSMiddleware | main.py:430 (first added) | `allow_origins=settings.cors_origin_list`, `allow_credentials=False` |
| 7 | ExceptionMiddleware, then router | Starlette | |

Consequence: a 429 from RateLimit is returned without CORS headers, since CORS sits inside it.

### 3.3 Lifespan startup (main.py:373-412), in order
1. `asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(IO_THREAD_POOL_WORKERS, "paxs-io"))` (main.py:380-382). Default 64 workers.
2. `await init_db()` (session.py:234-240): `create_all` on the async engine.
3. `apply_db_overrides()` (settings_service.py:454-472): synchronous query of `server_settings`. Applies overrides to the in-process `settings` object. Runs in the event loop.
4. If `WORKER_ENABLED`: `start_worker()` (worker.py:354-364):
   - `_recover_stuck_jobs()` (worker.py:367-386): sets all `CompressionJob.status='RUNNING'` to PENDING and all `Series.triage_status='RUNNING'` to PENDING. Global, not per node.
   - `recover_stuck_queue_items()` (routing_service, not read).
   - Starts `CompressionWorker` (daemon thread `paxs-compression-worker`, worker.py:26-39), which polls every `WORKER_POLL_SECONDS`.
5. If `DICOM_RECEIVER_ENABLED`: `start_receiver_in_thread()` (receiver.py:641-658). Daemon thread `paxs-dicom-scp`. If `DICOM_TLS_ENABLED`, calls `ensure_cert_paths_exist()` and `server_context()` (app/dicom/tls.py, not read), then `start_receiver(tls_context)`.
6. If `HL7_ENABLED`: `start_hl7_listener()` (hl7.py:764-772). HL7Listener accept-loop thread, plus one thread per connection (hl7.py:731).
7. License: `get_license_manager()` (manager.py:429-434) runs `load_license()`. It reads the file, decrypts, verifies the signature, and checks the fingerprint. The fingerprint may spawn PowerShell or WMIC. main.py:393-396 then logs `get_summary()`, which calls `get_hardware_info()` and spawns subprocesses again.
8. If `LICENSE_AUTO_VALIDATE_ONLINE`: `asyncio.create_task(_heartbeat_loop())` (main.py:398-406). The loop sleeps first, `max(3600, LICENSE_HEARTBEAT_HOURS*3600)` seconds, then calls `validate_online` via `asyncio.to_thread`.
9. Logs "ready" (main.py:408-411) and yields.

### 3.4 Shutdown (main.py:413-418)
1. `_hb_task.cancel()` (not awaited).
2. `shutdown_receiver()` (receiver.py:631-638): `_ae.shutdown()`.
3. `stop_worker()` (worker.py:389-396): sets the stop event, joins up to 5 s.
4. `shutdown_hl7()` (hl7.py:775-780): stop, join up to 3 s.
5. `close_db()` (session.py:243-249): disposes async, replica, and sync engines.
The default executor set in step 3.3.1 is never shut down.

### 3.5 Routers mounted (`app.include_router`, main.py:507-556)

| Line | Router module | Prefix (from APIRouter, file:line) |
|---|---|---|
| 507 | dicomweb | `/dicom-web` (dicomweb.py:48) |
| 508 | dashboard | `/api/v1` (dashboard.py:62) |
| 509 | metrics | none; route `/metrics` (metrics.py:87) |
| 510 | ai | `/api/v1/ai` |
| 511 | config | `/api/v1/config` |
| 512 | database | `/api/v1/db` |
| 513 | license | `/api/v1/license` (license.py:20) |
| 514 | auth | `/api/v1/auth` (auth.py:49) |
| 515 | admin_panel | `/api/v1/admin-panel` |
| 516 | share | `/api/v1/share` |
| 517 | export | `/api/v1` |
| 518 | nodes | `/api/v1/nodes` |
| 519 | worklist | `/api/v1/worklist` |
| 520 | worklist_tasks | `/api/v1/worklist-tasks` |
| 521 | mpps | `/api/v1/mpps` |
| 522 | commitments | `/api/v1/commitments` |
| 523 | gsps | `/api/v1/gsps` |
| 524 | migration | `/api/v1/migration` |
| 525 | split_merge | `/api/v1/studies` |
| 526 | clinical_protocols | `/api/v1/clinical-protocols` |
| 527 | critical_results | `/api/v1/critical-results` |
| 528 | routing | `/api/v1/routing` |
| 529 | kin | `/api/v1/kin` |
| 530 | lossy_compression | `/api/v1/compression/lossy` |
| 531 | lifecycle | `/api/v1/lifecycle` |
| 532 | ian | `/api/v1/ian` |
| 533 | dicomdir | `/api/v1/export/dicomdir` |
| 534 | prefetch | `/api/v1/prefetch` |
| 535 | dose | `/api/v1/dose` |
| 536 | xdsi | `/api/v1/xdsi` |
| 537 | priors | `/api/v1/priors` |
| 538 | hanging_protocols | `/api/v1/hanging-protocols` |
| 539 | ldap | `/api/v1/ldap` |
| 540 | peer_review | `/api/v1/peer-review` |
| 541 | reports | `/api/v1/reports` |
| 542 | tenants | `/api/v1/tenants` |
| 543 | users | `/api/v1/users` |
| 544 | storage | `/api/v1/storage` |
| 545 | quota | `/api/v1/quotas` |
| 546 | pix_pdq | `/api/v1/pix` |
| 547 | volume | `/api/v1/volume` |
| 548 | scripts | `/api/v1/scripts` |
| 549 | replication | `/api/v1/replication` |
| 550 | db_panel | `/api/v1/db/perf` |
| 551 | redis_panel | `/api/v1/redis` |
| 552-553 | settings (imported as settings_v1_router) | none; routes are absolute, e.g. `/api/v1/settings/organization`, `/api/v1/templates` (settings.py:24, 49) |
| 555 (if FHIR_ENABLED, default true) | fhir | `/fhir` (fhir.py:24) |
| 556 (if FHIR_ENABLED) | fusion | `/api/v1/fusion` (fusion.py:26) |

Every `app/api/v1/*.py` file that defines an `APIRouter` is included. Verified by comparing the file list against main.py.

### 3.6 Direct routes and static mounts (main.py)
- `GET /` serves index.html, `Cache-Control: no-cache` (L563-567). Registered only if `static/` exists.
- `StaticFiles` mount `/static` (CachedStaticFiles, L560). Public and not gated.
- `GET /viewer`, `/viewer/`, `/viewer/index.html`, `/viewer/viewer` (L599-618). Mount `/viewer` as SpaStaticFiles(html=True) (L620). Fallback `viewer.html` if the dist is absent (L641-649).
- `GET /PaxsViewer-logo.png` (L632-639).
- `GET /admin`, `/admin/`, mount `/admin` (L656-664). Only if `ADMIN_PANEL_ENABLED` and `static/admin` exists. Not in the ViewerAuthGate list.
- `GET /studies-ui`, `/studies-ui/`, mount `/studies-ui` (L669-675).
- `GET /settings`, `/settings/`, mount `/settings` (L680-686).
- `GET /login`, `/login/` (L695-710): redirects an authenticated visitor via `role_landing`, else serves login.html.
- `GET /logout` (L713-719): clears the cookie only. Does not revoke the session row.
- `GET /terms` (L727-734, reads feature-plans/terms.md) and `GET /paxs-viewer-guide` (L742-749, reads paxs_viewer_guide.md). Both public.
- FastAPI defaults: `/api/docs`, `/api/openapi.json` (public).

### 3.7 Background tasks and threads

| Thread or task | Started by | Kind | Interval or trigger |
|---|---|---|---|
| paxs-io pool | lifespan (main.py:380-382) | ThreadPoolExecutor(64) for `asyncio.to_thread` | on demand |
| paxs-compression-worker | start_worker (worker.py:354-364) | daemon thread (CompressionWorker.run) | every WORKER_POLL_SECONDS (3.0 default) |
| worker tick sub-steps | `_process_batch` (worker.py:~99-132) | in-thread | compression and triage each tick; lifecycle every 12 ticks; routing every 2 ticks; DR replication every 4 ticks if enabled; session sweep every 60 ticks |
| paxs-dicom-scp | start_receiver_in_thread (receiver.py:641-658) | daemon thread | runs `start_receiver` |
| HL7Listener + per-conn threads | start_hl7_listener (hl7.py:764-772, thread at 731) | daemon threads | if HL7_ENABLED |
| license heartbeat | lifespan (main.py:398-406) | asyncio task | first run after max(1 h, LICENSE_HEARTBEAT_HOURS) |
| commit-sender | commit_sender.py:88 | daemon thread | created lazily, not in lifespan |
| routing dispatch | routing.py:208 | thread per dispatch call | per request |
| script runner | scripting_service.py:319 | thread | trigger not traced |
| warm-on-open | warm_service.py:65 | thread | per study open in viewer |
| webhooks | webhook_service.py:64 | thread | per archive event |

---

## 4. Configuration reference

Field names are the env var names (pydantic-settings, case-insensitive). The `.env` file is read from the current working directory (config.py:321). All lines are in config.py.

| Setting | Type | Default | Line | Meaning |
|---|---|---|---|---|
| APP_NAME | str | "PAXS Archive Server" | 10 | Display name, OpenAPI title |
| APP_VERSION | str | "0.1.0" | 11 | Version string; sent in licensing User-Agent |
| DEBUG | bool | False | 12 | Sets root log level to DEBUG |
| PAXS_INSTANCE_ID | str | "node-1" | 17 | Node id reported by /health (load balancing) |
| DATABASE_URL | str | "mysql+aiomysql://paxs_user:paxs@127.0.0.1:3306/paxs_db?charset=utf8mb4" | 25 | Primary DB. Default embeds a password (F-20) |
| DICOM_AE_TITLE | str | "PAXS_SCP" | 28 | DIMSE AE title |
| DICOM_HOST | str | "0.0.0.0" | 29 | DIMSE bind address |
| DICOM_PORT | int | 10400 | 30 | DIMSE port |
| DICOM_RECEIVER_ENABLED | bool | True | 31 | Start the SCP at boot |
| DICOM_MAX_ASSOCIATIONS | int | 16 | 32 | Max concurrent associations |
| DICOM_TLS_ENABLED | bool | False | 39 | Wrap DIMSE in TLS |
| DICOM_TLS_CERT | str | "./certs/paxs.crt" | 40 | DIMSE certificate path (auto-generated if missing, per comment) |
| DICOM_TLS_KEY | str | "./certs/paxs.key" | 41 | DIMSE key path |
| DICOM_TLS_CA | str | "" | 44 | CA bundle for client certs (mTLS) |
| HTTPS_ENABLED | bool | False | 51 | Serve uvicorn over TLS; also gates HSTS header |
| HTTPS_CERT | str | "./certs/https.crt" | 52 | Web TLS cert (no auto-generation exists; F-18) |
| HTTPS_KEY | str | "./certs/https.key" | 53 | Web TLS key |
| HTTPS_PORT | int | 8443 | 54 | Unused (grep: no reader) |
| STORAGE_ROOT | str | "./storage" | 57 | Local object store root |
| STORAGE_BACKEND | str | "local" | 60 | "local" or "s3" |
| S3_ENDPOINT_URL | str | "" | 61 | S3-compatible endpoint (empty = AWS) |
| S3_REGION | str | "us-east-1" | 62 | S3 region |
| S3_BUCKET | str | "paxs" | 63 | S3 bucket |
| S3_ACCESS_KEY | str | "" | 64 | S3 access key (plaintext env) |
| S3_SECRET_KEY | str | "" | 65 | S3 secret key (plaintext env) |
| S3_PRESIGN_EXPIRES | int | 900 | 66 | Presigned URL lifetime, seconds |
| ENV_FILE_PATH | str | ".env" | 71 | File rewritten by "Save configuration" in DB admin |
| BACKUP_DIR | str | "./backups" | 72 | DB backup dump directory |
| DB_POOL_SIZE | int | 5 | 81 | Sync QueuePool size (MySQL) |
| DB_MAX_OVERFLOW | int | 10 | 82 | Sync pool overflow |
| DB_POOL_RECYCLE_SECONDS | int | 1800 | 83 | Sync pool recycle |
| DB_POOL_PRE_PING | bool | True | 84 | Sync pool pre-ping |
| DB_READ_REPLICA_URL | str | "" | 89 | Optional read replica; routing toggled by ServerSetting dbp_read_replica |
| DB_SLOW_QUERY_THRESHOLD_MS | int | 500 | 92 | Slow-query threshold |
| SQLITE_JOURNAL_MODE | str | "delete" | 103 | SQLite journal mode (interpolated into PRAGMA, session.py:55) |
| DEFAULT_COMPRESSION | str | "JPEG-LS" | 106 | Default transfer syntax for compression jobs |
| COMPRESSION_ENABLED | bool | True | 107 | Enable compression |
| THUMBNAIL_ENABLED | bool | True | 108 | Enable thumbnails |
| WORKER_ENABLED | bool | True | 109 | Start background worker thread at boot |
| WORKER_POLL_SECONDS | float | 3.0 | 110 | Worker poll interval (.env.example uses 5) |
| WORKER_BATCH_SIZE | int | 15 | 114 | Jobs claimed per tick |
| STATS_USAGE_CACHE_SECONDS | int | 60 | 120 | TTL cache for /stats storage walk; 0 disables |
| MAX_FAILED_ATTEMPTS | int | 5 | 128 | Failed logins before account lock |
| LOCKOUT_DURATION_MINUTES | int | 15 | 129 | Lock duration |
| SECURITY_HEADERS_ENABLED | bool | True | 133 | Global security headers |
| SECRET_KEY | str | "paxs-insecure-dev-key-change-me" | 136 | HS256 key for access tokens. No startup check (F-01) |
| TOKEN_EXPIRE_MINUTES | int | 15 | 140 | Access-token lifetime (.env.example says 480) |
| REFRESH_TOKEN_EXPIRE_DAYS | int | 7 | 144 | Refresh/session lifetime; also cookie max-age (at import) |
| ALLOW_REGISTER | bool | True | 145 | Enables self-registration (F-07) |
| SESSION_SWEEP_EVERY_TICKS | int | 60 | 150 | Worker ticks between expired-session sweeps |
| AUTH_COOKIE_NAME | str | "paxs_session" | 157 | Refresh-token cookie name |
| AUTH_COOKIE_SECURE | bool | False | 158 | Cookie Secure flag (should be True behind HTTPS) |
| AUTH_GATE_ENABLED | bool | True | 159 | ViewerAuthGate on/off |
| RATE_LIMIT_ENABLED | bool | True | 162 | Rate limiter on/off |
| RATE_LIMIT_LOGIN_PER_MINUTE | int | 10 | 163 | Per-IP window for register, login, refresh |
| RATE_LIMIT_GLOBAL_PER_MINUTE | int | 300 | 164 | Per-IP global window |
| REDIS_URL | str | "redis://127.0.0.1:6379/0" | 167 | Redis endpoint for cache and rate limit |
| REDIS_CACHE_ENABLED | bool | True | 168 | Redis caching on/off |
| ANONYMIZER_STRIP_PRIVATE_TAGS | bool | True | 171 | Research export anonymizer |
| ANONYMIZER_STRIP_DATES | bool | True | 172 | Research export anonymizer |
| HL7_ENABLED | bool | False | 175 | Start HL7 ADT listener |
| HL7_HOST | str | "0.0.0.0" | 176 | HL7 bind address |
| HL7_PORT | int | 2575 | 177 | HL7 port |
| HL7_EMR_ENABLED | bool | False | 180 | Send ORU to EMR |
| HL7_EMR_HOST | str | "127.0.0.1" | 181 | EMR host |
| HL7_EMR_PORT | int | 6661 | 182 | EMR port |
| HL7_EMR_APPLICATION | str | "EMR" | 183 | MSH-5 |
| HL7_EMR_FACILITY | str | "EMR" | 184 | MSH-6 |
| HL7_SENDING_APPLICATION | str | "PAXS" | 185 | MSH-3 |
| HL7_SENDING_FACILITY | str | "PAXS_ARCHIVE" | 186 | MSH-4 |
| FHIR_ENABLED | bool | True | 187 | Mount FHIR and fusion routers (on by default) |
| MWL_DEFAULT_STATION_AET | str | "" | 192 | Station AET for ADT-created worklist steps |
| VOLUME_STREAMING_ENABLED | bool | True | 199 | Volume endpoints |
| VOLUME_MAX_SLICES | int | 4096 | 200 | Cap per /data request |
| VOLUME_MPR_MAX_PIXELS | int | 4000000 | 204 | Cap per MPR slice |
| TRIAGE_ENABLED | bool | True | 207 | AI triage on/off |
| TRIAGE_RUN_IN_WORKER | bool | True | 208 | Run triage in worker |
| TRIAGE_MAX_SLICES_PER_SERIES | int | 8 | 209 | Triage slice budget |
| LIFECYCLE_RUN_IN_WORKER | bool | True | 214 | Lifecycle sweep in worker |
| LIFECYCLE_SWEEP_EVERY_TICKS | int | 12 | 215 | Ticks between lifecycle sweeps |
| LIFECYCLE_AUTO_ARCHIVE_WADO_PULL | bool | False | 220 | Auto-archive WADO pulls |
| ROUTING_QUEUE_ENABLED | bool | True | 225 | Durable routing queue |
| ROUTING_RUN_IN_WORKER | bool | True | 226 | Routing sweep in worker |
| ROUTING_MAX_ATTEMPTS | int | 5 | 227 | Retries before FAILED |
| ROUTING_RETRY_BACKOFF_SECONDS | int | 60 | 228 | Base backoff |
| ROUTING_SWEEP_EVERY_TICKS | int | 2 | 229 | Ticks between routing sweeps |
| WEBHOOK_URLS | str | "" | 232 | Comma-separated webhook targets |
| SCRIPTS_DIR | str | "scripts" | 239 | Directory of trusted .py scripts loaded in-process |
| SCRIPTS_ENABLED | bool | True | 240 | Scripting on/off |
| DR_REPLICATION_ENABLED | bool | False | 246 | DR replication worker sweep |
| DR_REPLICATION_TARGET | str | "" | 247 | Named storage backend target |
| DR_REPLICATION_PREFIX | str | "replica/" | 248 | Key prefix on target |
| DR_REPLICATION_MAX_ATTEMPTS | int | 5 | 249 | Retries |
| DR_REPLICATION_SWEEP_EVERY_TICKS | int | 4 | 250 | Ticks between sweeps |
| DICOMWEB_PUBLIC | bool | True | 253 | Documented as "open read access"; NO CODE READS IT (F-10) |
| CORS_ORIGINS | str | "*" | 254 | Comma-separated origins, or "*" |
| DICOMWEB_CLIENT_ALLOW_PRIVATE | bool | False | 260 | SSRF guard bypass for remote-node client |
| DICOMWEB_CLIENT_TIMEOUT | float | 15.0 | 261 | Remote DICOMweb timeout, seconds |
| DICOMWEB_CLIENT_MAX_BYTES | int | 4 GiB | 262 | Cap per remote retrieve |
| STOW_MAX_PART_BYTES | int | 2 GiB | 269 | Max part size, 413 above |
| STOW_MAX_TOTAL_BYTES | int | 0 | 270 | Max total body (0 = unlimited) |
| TRANSFER_ACCELERATOR_ENABLED | bool | True | 277 | Accelerated STOW push |
| TRANSFER_ACCELERATOR_WORKERS | int | 4 | 278 | Parallel connections |
| TRANSFER_ACCELERATOR_BATCH_SIZE | int | 16 | 279 | Instances per batch |
| PUBLIC_BASE_URL | str | "http://0.0.0.0" | 281 | Base for share links (default is not useful) |
| SHARE_EXPIRY_DAYS | int | 30 | 284 | Patient share lifetime |
| ADMIN_PANEL_ENABLED | bool | True | 290 | Mount /admin control panel |
| ADMIN_PANEL_CONFIG_FILE | str | "" | 291 | Path for admin panel JSON |
| IO_THREAD_POOL_WORKERS | int | 64 | 299 | Default executor size |
| WARM_ON_OPEN_ENABLED | bool | True | 310 | Warm cache when a study is opened |
| WARM_MAX_INSTANCES | int | 16 | 311 | Thumbnail budget per warm pass |
| WARM_MAX_FRAMES | int | 4 | 312 | Frame pre-decode budget per warm pass |
| LICENSE_FILE_PATH | str | "./paxs.lic" | 315 | UNUSED (grep: no reader; manager uses PAXS_LICENSE_PATH and cwd) |
| LICENSE_PORTAL_URL | str | "http://127.0.0.1:8080" | 316 | Vendor portal base URL (plain HTTP default) |
| LICENSE_HEARTBEAT_HOURS | int | 24 | 317 | Heartbeat interval (floor 1 h in main.py:400) |
| LICENSE_GRACE_PERIOD_DAYS | int | 7 | 318 | UNUSED (manager hard-codes 7, manager.py:51) |
| LICENSE_AUTO_VALIDATE_ONLINE | bool | True | 319 | Start heartbeat task |

Non-Settings environment variables read directly:
- `PAXS_HOST` (default 0.0.0.0) and `PAXS_PORT` (default 8000): run_paxs.py:37-38.
- `PAXS_LICENSE_PUBLIC_KEY`, `PAXS_LICENSE_PUBLIC_KEY_PATH`: crypto.py:109-110.
- `PAXS_LICENSE_AES_KEY`: crypto.py:204, 233.
- `PAXS_LICENSE_PATH`: manager.py:58.
- `PAXS_DEV_LICENSE`: manager.py:97.
- `PAXS_SQLITE_JOURNAL` is named in the session.py docstring (L10-11) but is not read. The real field is `SQLITE_JOURNAL_MODE`.

---

## 5. Data model

Base: `DeclarativeBase` (models.py:30). PK helper `pk()` uses BIGINT on MySQL and INTEGER on SQLite. Datetimes are naive UTC. All 43 classes below are verified against the migrations (section 7, last paragraph).

| # | Class (models.py:line) | Table | Key columns | ORM relationships | Indexes |
|---|---|---|---|---|---|
| 1 | Tenant (52) | tenants | id; name UNIQUE; code UNIQUE; is_active | none | unique name, code |
| 2 | TenantAeTitle (84) | tenant_ae_titles | id; tenant_id FK tenants CASCADE; ae_title UNIQUE | none | tenant_id (index=True) |
| 3 | User (104) | users | id; username UNIQUE; password_hash String(256); role default PHYSICIAN; is_active; failed_attempts; locked_until; last_login; last_login_ip; tenant_id FK tenants SET NULL | none | username unique; tenant_id |
| 4 | Session (136) | sessions | id String(64) PK; user_id FK users CASCADE; refresh_token_hash UNIQUE (SHA-256); family_id; expires_at; is_revoked | none | user_id; refresh_token_hash (unique+index); family_id |
| 5 | AuthAuditLog (171) | auth_audit_log | id; user_id FK users SET NULL; action; timestamp; details (JSON text) | none | user_id; action; timestamp |
| 6 | Patient (194) | patients | id; patient_id UNIQUE; patient_name; patient_birth_date String(8); patient_phone; source | studies (1:N cascade); identities (1:N cascade) | patient_name; source; composite ix_patients_name_id (patient_name, patient_id) |
| 7 | AssigningAuthority (232) | assigning_authorities | id; oid UNIQUE; name; is_authoritative | identities (1:N cascade) | ix_assigning_authorities_oid |
| 8 | PatientIdentity (272) | patient_identities | id; patient_id FK patients CASCADE; assigning_authority_id FK CASCADE; identifier; id_type; is_primary | patient (N:1); assigning_authority (N:1) | index patient_id; index assigning_authority_id; ix_patient_identities_patient_aa; UNIQUE ix_patient_identities_aa_identifier |
| 9 | Study (317) | studies | id; patient_id FK patients CASCADE; tenant_id FK tenants SET NULL; study_instance_uid UNIQUE; study_date; accession_number; lifecycle_state default ONLINE; storage_backend (string, no FK); stat_flag; assigned_radiologist; read_at; read_by | patient (N:1); series_list (1:N cascade) | patient_id; tenant_id; study_date; accession_number; lifecycle_state; created_at; assigned_radiologist; ix_studies_tenant_created; ix_studies_tenant_date; ix_studies_state_created; ix_studies_assigned_rad; ix_studies_stat_flag |
| 10 | Series (384) | series | id; study_id FK studies CASCADE; series_instance_uid UNIQUE; modality; series_number; frame_of_reference_uid; triage_status | study (N:1); instances (1:N cascade) | study_id; modality; frame_of_reference_uid; triage_status; ix_series_study_number; ix_series_study_modality |
| 11 | Instance (431) | instances | id; series_id FK series CASCADE; sop_instance_uid UNIQUE; file_path String(768); storage_backend (string); file_size_bytes; compressed_at | series (N:1) | series_id; storage_backend; ix_instances_series_number; ix_instances_backend_created |
| 12 | PresentationState (473) | presentation_states | id; sop_instance_uid UNIQUE; study_instance_uid; series_instance_uid; window_center/width; rotation; file_path | none (UIDs are strings) | sop_instance_uid unique; study_instance_uid; series_instance_uid |
| 13 | PatientShare (528) | patient_shares | id; study_instance_uid; share_token UNIQUE (stored raw); phone; expires_at; accessed_count | none | study_instance_uid; share_token unique |
| 14 | AuditLog (542) | audit_log | id; sop_instance_uid; study_instance_uid; sha256_hash; action; performed_by; performed_at | none | sop_instance_uid; study_instance_uid; action; performed_at |
| 15 | TriageFlag (556) | triage_flags | id; study_instance_uid; finding; severity; confidence; reviewed; detector | none | study_instance_uid; reviewed; ix_triage_reviewed_severity |
| 16 | ScheduledProcedureStep (587) | scheduled_procedure_steps | id; patient_id FK patients CASCADE; sps_id UNIQUE; accession_number; modality; scheduled_station_aet; start_date; status; study_instance_uid | patient (N:1) | patient_id; accession_number; modality; scheduled_station_aet; start_date; study_instance_uid; ix_sps_status_station; ix_sps_status_modality |
| 17 | PerformedProcedureStep (636) | performed_procedure_steps | id; sop_instance_uid UNIQUE; patient_id FK patients SET NULL; scheduled_step_id FK scheduled_procedure_steps SET NULL; status; study_instance_uid | patient (N:1); scheduled_step (N:1) | patient_id; scheduled_step_id; status; accession_number; study_instance_uid |
| 18 | StorageCommitRequest (688) | commitment_statuses | id; transaction_uid UNIQUE; ae_title; status; payload_json | none | ae_title; status |
| 19 | DicomNode (726) | dicom_nodes | id; ae_title UNIQUE; host; port; enabled; web_url; web_username; web_password (plaintext, String 256) | none | none beyond unique ae_title |
| 20 | CompressionJob (762) | compression_jobs | id; instance_id FK instances CASCADE; method; status; original_size; compressed_size; ratio_pct | instance (N:1) | status (FK index auto on MySQL) |
| 21 | MigrationJob (785) | migration_jobs | id; job_type; status; source_info; counters | none | status |
| 22 | CriticalResult (811) | critical_results | id; study_instance_uid; patient_id; finding_level; status; reported_by; acknowledged_* | none | study_instance_uid; patient_id; finding_level; status; ix_critical_status_study; ix_critical_created |
| 23 | RoutingRule (856) | routing_rules | id; name UNIQUE; destination_node_id FK dicom_nodes CASCADE; modality; source_aet; study_description_pattern | destination_node (N:1) | none beyond unique name |
| 24 | RoutingQueueItem (889) | routing_queue | id; study_instance_uid; rule_id FK routing_rules SET NULL; node_id FK dicom_nodes CASCADE; status; attempts; next_retry_at | rule (N:1); node (N:1) | study_instance_uid; node_id; status; next_retry_at |
| 25 | KeyImageNote (935) | key_image_notes | id; sop_instance_uid UNIQUE; study_instance_uid; series_instance_uid; file_path | none | sop unique; study_instance_uid; series_instance_uid |
| 26 | StudyLifecycleRule (966) | study_lifecycle_rules | id; name UNIQUE; action; min_age_days; modality | none | unique name |
| 27 | LossyCompressionRule (999) | lossy_compression_rules | id; name UNIQUE; transfer_syntax_uid; quality_factor | none | unique name |
| 28 | LosslessCompressionRule (1028) | lossless_compression_rules | id; name UNIQUE; method | none | unique name. NO MIGRATION (F-16) |
| 29 | StorageBackendConfig (1057) | storage_backends | id; name UNIQUE; kind; is_default; root_path; endpoint_url; bucket; access_key; secret_key (plaintext) | none (string links from studies/instances) | unique name |
| 30 | StorageQuota (1095) | storage_quotas | id; tenant_id FK tenants CASCADE; storage_backend_name; max_bytes; warning_threshold_pct | tenant (N:1) | tenant_id; storage_backend_name; ix_storage_quotas_tenant; ix_storage_quotas_backend |
| 31 | InstanceAvailabilityNotification (1142) | ian_notifications | id; study_instance_uid; target_ae_title; instance_availability; status; payload_json | none | study_instance_uid; target_ae_title; status |
| 32 | PrefetchRule (1173) | prefetch_rules | id; name UNIQUE; source_node_id FK dicom_nodes CASCADE; max_priors_count | source_node (N:1) | unique name |
| 33 | PrefetchJob (1205) | prefetch_jobs | id; patient_id; worklist_step_id FK scheduled_procedure_steps SET NULL; source_node_id FK dicom_nodes CASCADE; status | source_node (N:1); worklist_step (N:1) | patient_id; status |
| 34 | RadiationDoseReport (1235) | radiation_dose_reports | id; sop_instance_uid UNIQUE; study_instance_uid; series_instance_uid; patient_id; total_dlp_mgy_cm | none | sop unique; study_instance_uid; series_instance_uid; patient_id |
| 35 | XDSDocument (1269) | xds_documents | id; entry_uuid UNIQUE; unique_id UNIQUE; patient_id; study_instance_uid; availability_status | none | entry_uuid; unique_id; patient_id; study_instance_uid; availability_status |
| 36 | HangingProtocol (1309) | hanging_protocols | id; name UNIQUE; modality; body_part_examined; user_id FK users CASCADE; layout_rows; layout_columns | none | modality; body_part_examined; user_id |
| 37 | ClinicalProtocol (1338) | clinical_protocols | id; name UNIQUE; standardized_name; ris_code; modality; body_part_examined; study_description_pattern; enabled | none | standardized_name; ris_code; modality; body_part_examined; ix_clinical_protocols_modality_bodypart; ix_clinical_protocols_enabled_priority |
| 38 | RadiologyReport (1384) | reports | id; study_id FK studies CASCADE UNIQUE (1:1); study_instance_uid; patient_id; status; findings; impression; oru_status; oru_error | study (1:1 via unique FK) | study_instance_uid; patient_id; status; oru_status; ix_reports_status_oru |
| 39 | PeerReviewCase (1437) | peer_review_cases | id; study_instance_uid; patient_id; modality; original_reader; reviewer; status; score | none | study_instance_uid; patient_id; modality; reviewer; status |
| 40 | ReplicationRecord (1481) | replication_records | id; study_instance_uid UNIQUE; target; status; attempts | none | study_instance_uid unique; ix_replication_status_attempts |
| 41 | ServerSetting (1521) | server_settings | id; key UNIQUE; value (Text) | none | key unique |
| 42 | OrganizationSettings (1543) | organization_settings | id; org_name_ar/en; logo_path; header_html_text; footer_html_text; phone; email; address | none | none. NO MIGRATION (F-16) |
| 43 | ReportTemplate (1562) | report_templates | id; title; modality; body_findings_template; default_impression_template; is_default | none | modality. NO MIGRATION (F-16) |

Also: PatientIdentity (8) and AssigningAuthority (7) have no migration (F-16).

### Relationships for an ER diagram

ORM-declared (`relationship()`):
- Patient 1 --- N Study (cascade delete-orphan)
- Study 1 --- N Series (cascade delete-orphan)
- Series 1 --- N Instance (cascade delete-orphan)
- Patient 1 --- N PatientIdentity (cascade delete-orphan)
- AssigningAuthority 1 --- N PatientIdentity (cascade delete-orphan)
- Patient 1 --- N ScheduledProcedureStep (one-way)
- Patient 0..1 --- N PerformedProcedureStep (one-way)
- ScheduledProcedureStep 0..1 --- N PerformedProcedureStep (one-way)
- Instance 1 --- N CompressionJob (one-way)
- DicomNode 1 --- N RoutingRule (one-way, destination_node)
- RoutingRule 0..1 --- N RoutingQueueItem (one-way, rule)
- DicomNode 1 --- N RoutingQueueItem (one-way, node)
- DicomNode 1 --- N PrefetchRule (source_node)
- DicomNode 1 --- N PrefetchJob (source_node)
- ScheduledProcedureStep 0..1 --- N PrefetchJob (worklist_step)
- Study 1 --- 1 RadiologyReport (unique study_id FK)
- Tenant 1 --- N StorageQuota (one-way)

FK-only (no ORM relationship declared):
- Tenant 1 --- N TenantAeTitle (CASCADE)
- Tenant 0..1 --- N User (SET NULL)
- Tenant 0..1 --- N Study (SET NULL)
- User 1 --- N Session (CASCADE)
- User 0..1 --- N AuthAuditLog (SET NULL)
- User 0..1 --- N HangingProtocol (CASCADE)

String-keyed links (no FK; by UID or name):
- Study.study_instance_uid is referenced by PresentationState, KeyImageNote, PatientShare, AuditLog, TriageFlag, CriticalResult, RadiationDoseReport, PeerReviewCase, ReplicationRecord, XDSDocument, ScheduledProcedureStep, PerformedProcedureStep, InstanceAvailabilityNotification, RoutingQueueItem, RadiologyReport (denormalized).
- Series UID: PresentationState, KeyImageNote, RadiationDoseReport.
- StorageBackendConfig.name is referenced by Study.storage_backend, Instance.storage_backend, StorageQuota.storage_backend_name, ReplicationRecord.target.

---

## 6. Security and auth model

### 6.1 Passwords
- `hash_password`: PBKDF2-HMAC-SHA256, 200,000 iterations, 16-byte random salt, stored as `pbkdf2_sha256$iter$salt$dk` (security.py:82, 88-96). `verify_password` uses `hmac.compare_digest` (L107) and ignores the algorithm id (L101).
- No pepper. No rehash-on-login. 200k is below current PBKDF2-HMAC-SHA256 guidance (F-34).
- Login (auth.py:217-274): unknown usernames return 401 before any hashing (L224-226). That gives a timing difference (F-34). Lockout counters apply only to existing accounts.

### 6.2 Access tokens (stdlib HS256 JWT)
- `create_access_token` sets `sub` (username), `role`, `iat`, `exp` (security.py:123-138). Signed with `SECRET_KEY`.
- `decode_token` checks signature (constant time) and `exp` only (security.py:141-153). It does not check `alg`, `aud`, `iss`, or `iat`. Because `alg` is never read, there is no alg-confusion risk.
- Identity is re-resolved from the DB on every request: role and active flag come from the `users` row, not the token (deps.py:46-63). The token's `role` claim is never used for authorization.
- Lifetime: 15 min by default (config.py:140). `.env.example:40` sets 480 min. Logout does not revoke access tokens (F-21).
- Default `SECRET_KEY` is public (config.py:136). No startup guard (F-01).

### 6.3 Refresh tokens and sessions
- Opaque token `"<family_id>.<token_urlsafe(32)>"` (security.py:50-58). Only SHA-256 hex is stored, in `sessions.refresh_token_hash` (models.py:152-154).
- Rotated on refresh (auth.py:277-332 per model docstring; refresh handler partly read). Expiry checked on refresh (auth.py:308-309) and extended on use (auth.py:324). Logout (POST /auth/logout) sets `is_revoked` (auth.py:344).
- Cookie `paxs_session`: HttpOnly, SameSite=Lax, path=/, Secure only if `AUTH_COOKIE_SECURE` (default False, config.py:158, security.py:29-39).

### 6.4 Who can call what (deps.py)
- Bearer access token is preferred. If absent or invalid, `get_optional_user` falls back to the refresh cookie (deps.py:66-86). The cookie resolver accepts the refresh token as an API credential.
- Cookie resolution checks `is_revoked` and `user.is_active`, but NOT `sessions.expires_at` (deps.py:36-42). Expired rows are removed only by the worker sweep (worker.py:184-206, every 60 ticks). See F-13.
- `get_current_user` (bearer only, deps.py:46-63) is used by a few routes, e.g. reports list/get/patch (reports.py:83, 92, 102). Checks `is_active`.
- `get_current_user_any` (deps.py:89-101) is the cookie-or-bearer 401 gate used by `/auth/me` and `require_*`.
- `require_admin` (deps.py:117-120): role == ADMIN.
- `require_destructive_role` (deps.py:108-114): role in (ADMIN, RADIOLOGIST). Used by study DELETE (dashboard.py:834-836), audit trail (dashboard.py:388), DB perf panel (db_panel.py:37-60), report finalize/resend (reports.py:117-135), clinical protocol writes, triage review.
- `get_optional_user` used as "soft auth" on 23 routes that never reject anonymous callers (8 DICOMweb, 4 settings, 4 volume, 4 redis_panel, 1 dashboard, 1 fusion, 1 reports; route scan plus grep for user checks). A further 42 routes have no auth dependency at all (route scan; intended public routes such as login, health, share, and metrics are included in that count). Verified list in F-03 through F-06. This is the main authorization gap.

Roles: ADMIN, RADIOLOGIST, PHYSICIAN, TECHNOLOGIST (auth.py:51). Landing per role (auth.py:58-63). Destructive: ADMIN, RADIOLOGIST.

### 6.5 Registration and bootstrap
- `ALLOW_REGISTER` defaults to True (config.py:145). `/api/v1/auth/register` (auth.py:170-214) rejects only role "ADMIN" (auth.py:188-191). Any other role in ROLES is accepted, including RADIOLOGIST (F-07).
- First user is forced to ADMIN when the table is empty (auth.py:184-185). On a fresh DB, the first network client to register becomes admin (F-30).

### 6.6 Brute-force protection
- Per-account lockout: `MAX_FAILED_ATTEMPTS=5`, `LOCKOUT_DURATION_MINUTES=15` (config.py:128-129). Implemented at auth.py:228-255. Lock is checked before the password, so a correct password is refused during the lock (by design). Anyone can lock a known username such as `admin` (F-29).
- Per-IP sliding window: 10 req/min for register, login, and refresh, sharing one bucket key "auth" (auth.py:171, 218, 278). Global 300 req/min per IP (main.py:442), exempt for `/static` and `/dicom-web` (main.py:358-359).
- Keying uses the leftmost X-Forwarded-For value if present (ratelimit.py:29-33). The header is client-controlled. See F-09.
- Backend: Redis sorted-set window when available (ratelimit.py:51-74), else an in-process deque per worker (ratelimit.py:76-100). Multi-worker deployments get per-process limits.

### 6.7 Web security headers and gates
- `_SECURITY_HEADERS` (main.py:223-240): `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, CSP, `Permissions-Policy`. HSTS only if `HTTPS_ENABLED` (main.py:243-244, F-18).
- CSP `script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval'` (main.py:229). Weakens XSS protection (F-19).
- ViewerAuthGate (main.py:268-331): cookie presence for deep links and assets, DB session validation for the entry pages. Bypassable with `share=` anywhere in the query string (main.py:307-310, F-31).

### 6.8 Licensing (licensing/)
- Load path (manager.py:87-198): read file, decrypt container (AES-256-GCM, key from hardcoded SHA-256 at crypto.py:40-42 or `PAXS_LICENSE_AES_KEY`; or accept plain JSON, crypto.py:221-225), verify RSA signature over canonical JSON (RSA-PSS SHA-256 MGF1 max salt, PKCS1v15 fallback, crypto.py:148-196), check machine fingerprint (unless `"*"`), check expiry with 7-day grace (manager.py:174-191).
- Status gate: `is_active` = VALID or GRACE_PERIOD (manager.py:79-81).
- Fingerprint (fingerprint.py:188-233): SHA-256 over sorted components, truncated to 32 hex chars. Windows components: MAC, CPU ID, board serial, OS serial. Linux: MAC (all non-lo interfaces), machine-id, DMI UUID, product serial.
- Heartbeat (manager.py:290-345): HTTP POST to `LICENSE_PORTAL_URL` (default `http://127.0.0.1:8080`). Honors an unsigned `revoked` flag (manager.py:330-333). Renewals must carry a valid signature (activate_from_bytes → verify), so renewals cannot be forged by the portal alone.
- Enforcement: no route calls `require_valid_license` or `require_license_feature`. Only `ai.py:98,146` checks `lic.is_active` manually. Feature flags are shown in the AI endpoints (ai.py:46-85). Core PACS/DICOMweb/worker run regardless of license status (F-11).
- Dev bypass: `PAXS_DEV_LICENSE` grants an unlimited enterprise license when the license file is missing (manager.py:97-136).
- Key material: the public key is embedded (crypto.py:29-37). The vendor PRIVATE key is committed (F-02). Signing with it produces licenses that the embedded key accepts.

### 6.9 Positive observations
- Refresh tokens are stored as SHA-256 hashes and rotated (models.py:136-168; auth.py).
- Constant-time comparisons for passwords and JWT signatures (security.py:107, 146).
- Cookie flags HttpOnly and SameSite=Lax (security.py:31-38).
- Secrets not in the settings editor: SECRET_KEY, DATABASE_URL, S3 keys are not in EDITABLE_KEYS (grep of settings_service.py registry).
- Remote-node SSRF guard default-on (config.py:260).
- `.gitignore` excludes `.env`, `*.lic`, `certs/*.key`, `paxs.db`.

---

## 7. Migration chain

### 7.1 Graph summary (computed by a local analysis script)
- 38 revisions. Root: `56d9f882c2e4`. Single head: `c0d1e2f3a4b5`. No dangling down_revisions.
- Forks (more than one child): `g7081011b2c3` (children `6070910a1b2c`, `h8091112c3d4`) and `z2345678a9b0` (children `a12345678901`, `a1234567b8c1`).
- Merges (more than one parent): `s9013243n4o5` (parents `6070910a1b2c`, `r8902132m3n4`) and `t9013243n4o5` (parents `a12345678901`, `d5e6f70819a2`).
- Both merges are intentional and documented in their docstrings. Each branch touches disjoint tables.
- Dependency check (manual): every migration's referenced tables and columns come from an ancestor revision. Verified for `c4d5e6f70819` (needs tenant_id, lifecycle_state, storage_backend, reports, critical_results, triage_status), `b3456789c0d1` (needs stat_flag, assigned_radiologist), and `a12345678901` (needs tenants). No ordering hazard was found.

### 7.2 Ordered list (topological, head at bottom)

| # | Revision | File (lines) | down_revision | Summary |
|---|---|---|---|---|
| 1 | 56d9f882c2e4 | 56d9f882c2e4_create_paxs_core_tables.py (160) | none (root) | audit_log, patient_shares, patients, users, studies, series, instances, compression_jobs |
| 2 | 87b28a09bb37 | 87b28a09bb37_add_triage_flags.py (50) | 56d9f882c2e4 | triage_flags |
| 3 | a1b2c3d4e5f6 | a1b2c3d4e5f6_add_series_triage_status.py (32) | 87b28a09bb37 | series.triage_status |
| 4 | b2c3d4e5f607 | b2c3d4e5f607_add_patient_source_and_phone.py (37) | a1b2c3d4e5f6 | patients.patient_phone, patients.source |
| 5 | c3d4e5f60709 | c3d4e5f60709_add_dicom_nodes.py (38) | b2c3d4e5f607 | dicom_nodes |
| 6 | d4e5f6070910 | d4e5f6070910_add_scheduled_procedure_steps.py (59) | c3d4e5f60709 | scheduled_procedure_steps |
| 7 | e5f6070910a1 | e5f6070910a1_add_performed_procedure_steps.py (60) | d4e5f6070910 | performed_procedure_steps |
| 8 | f6070910a1b2 | f6070910a1b2_add_presentation_states.py (41) | e5f6070910a1 | presentation_states |
| 9 | g7081011b2c3 | g7081011b2c3_add_migration_jobs.py (35) | f6070910a1b2 | migration_jobs. FORK POINT |
| 10 | 6070910a1b2c | 6070910a1b2c_add_commitment_statuses.py (47) | g7081011b2c3 | commitment_statuses (branch A1). Docstring says "Revises: f6070910a1b2" (L4) but down_revision is g7081011b2c3 (L21) |
| 11 | h8091112c3d4 | h8091112c3d4_add_critical_results.py (41) | g7081011b2c3 | critical_results (branch A2) |
| 12 | i9011213d4e5 | i9011213d4e5_add_routing_rules.py (38) | h8091112c3d4 | routing_rules (needs dicom_nodes) |
| 13 | j0121314e5f6 | j0121314e5f6_add_key_image_notes.py (36) | i9011213d4e5 | key_image_notes |
| 14 | k1231415f6a7 | k1231415f6a7_add_lossy_compression_rules.py (37) | j0121314e5f6 | lossy_compression_rules |
| 15 | l2341516g7h8 | l2341516g7h8_add_ian_notifications.py (35) | k1231415f6a7 | ian_notifications |
| 16 | m3451617h8i9 | m3451617h8i9_add_prefetch_tables.py (52) | l2341516g7h8 | prefetch_rules, prefetch_jobs |
| 17 | n4561718i9j0 | n4561718i9j0_add_radiation_dose_reports.py (40) | m3451617h8i9 | radiation_dose_reports |
| 18 | o5671819j0k1 | o5671819j0k1_add_xds_documents.py (42) | n4561718i9j0 | xds_documents |
| 19 | p6781920k1l2 | p6781920k1l2_add_hanging_protocols.py (37) | o5671819j0k1 | hanging_protocols |
| 20 | q7892021l2m3 | q7892021l2m3_add_peer_review_cases.py (38) | p6781920k1l2 | peer_review_cases |
| 21 | r8902132m3n4 | r8902132m3n4_add_vna_tenants.py (85) | q7892021l2m3 | tenants, tenant_ae_titles; studies.tenant_id; users.tenant_id (batch mode) |
| 22 | s9013243n4o5 | s9013243n4o5_merge_commitment_and_vna_heads.py (29) | (6070910a1b2c, r8902132m3n4) | MERGE. No-op |
| 23 | t0123456q6r7 | t0123456q6r7_add_study_lifecycle.py (42) | s9013243n4o5 | study_lifecycle_rules; studies.lifecycle_state, archived_at |
| 24 | u1234567v8w9 | u1234567v8w9_add_dicomweb_node_fields.py (26) | t0123456q6r7 | dicom_nodes.web_url, web_username, web_password |
| 25 | v1234567w8x9 | v1234567w8x9_add_routing_queue.py (86) | u1234567v8w9 | routing_queue |
| 26 | w1234567x8y9 | w1234567x8y9_add_radiology_reports.py (82) | v1234567w8x9 | reports |
| 27 | x1234567y8z9 | x1234567y8z9_add_server_settings.py (47) | w1234567x8y9 | server_settings |
| 28 | y1234567z8a0 | y1234567z8a0_add_users_active.py (29) | x1234567y8z9 | users.is_active |
| 29 | z2345678a9b0 | z2345678a9b0_add_storage_backends.py (55) | y1234567z8a0 | storage_backends; studies.storage_backend; instances.storage_backend. FORK POINT |
| 30 | a12345678901 | a12345678901_add_storage_quotas.py (42) | z2345678a9b0 | storage_quotas (branch B1). id is BigInteger without SQLite variant (F-17) |
| 31 | a1234567b8c1 | a1234567b8c1_add_replication_records.py (46) | z2345678a9b0 | replication_records (branch B2) |
| 32 | b3c4d5e6f708 | b3c4d5e6f708_add_reading_worklist.py (45) | a1234567b8c1 | studies.stat_flag, assigned_radiologist, read_at, read_by |
| 33 | c4d5e6f70819 | c4d5e6f70819_add_performance_indexes.py (67) | b3c4d5e6f708 | 25 performance indexes across studies, series, instances, audit_log, sps, triage, critical, reports, replication |
| 34 | d5e6f70819a2 | d5e6f70819a2_add_clinical_protocols.py (59) | c4d5e6f70819 | clinical_protocols (branch B2 end) |
| 35 | t9013243n4o5 | t9013243n4o5_merge_storage_quotas_and_clinical_protocols.py (30) | (a12345678901, d5e6f70819a2) | MERGE. No-op |
| 36 | a2345678b9c0 | a2345678b9c0_add_series_frame_of_reference.py (31) | t9013243n4o5 | series.frame_of_reference_uid + index |
| 37 | b3456789c0d1 | b3456789c0d1_add_patient_search_and_worklist_indexes.py (30) | a2345678b9c0 | 4 indexes incl. patients.patient_birth_date (no model counterpart) |
| 38 | c0d1e2f3a4b5 | c0d1e2f3a4b5_add_auth_sessions_and_lockout.py (94) | b3456789c0d1 | HEAD. users lockout columns; sessions; auth_audit_log |

Note on index names: many migration index names differ from SQLAlchemy default names for the same index (e.g., `ix_sps_accession` vs model default `ix_scheduled_procedure_steps_accession_number`). Harmless at runtime; alembic autogenerate will report drift.

### 7.3 Model vs migration coverage
- Every table created by a migration exists in models.py (no extra tables).
- Five model tables have NO migration: `assigning_authorities`, `patient_identities`, `organization_settings`, `report_templates`, `lossless_compression_rules` (F-16).
- Model index not in migration: `sessions.refresh_token_hash` unique index (migration only has UniqueConstraint, c0d1e2f3a4b5 L49).

### 7.4 Interaction with runtime bootstrap
- `init_db()` runs `create_all` on every boot (session.py:234-240) and only creates missing tables. It never adds columns.
- `db_admin.run_migrations` (services/db_admin.py:237-274) runs `alembic upgrade head`. On any "already exists" or "duplicate ..." error it runs `command.stamp(cfg, "head")` (L265-271). That marks the DB as current without running the failed migration or any later migration (F-15).

---

## 8. Code review findings

Severity: 🔴 blocking (fix before deployment), 🟡 important, 🟢 nit.

### 🔴 Blocking

**F-01 Default SECRET_KEY is public and not checked at startup [read]**
- config.py:136 `SECRET_KEY: str = "paxs-insecure-dev-key-change-me"`.
- Used to sign and verify every access token: deps.py:51, 76; auth.py:208, 269, 327; ldap.py:45.
- Scenario: any attacker who reads the source can sign `{"sub":"admin","exp":<future>}` with HS256 and this key. decode_token (security.py:141-153) accepts it. get_current_user (deps.py:46-63) loads the `admin` row from the DB and grants ADMIN. Works on any install where SECRET_KEY was not overridden. `.env.example:39` is a second guessable placeholder.
- Fix: refuse to start when SECRET_KEY equals the default or is shorter than 32 bytes. Generate a random key on first run and store it outside the repo. Remove the placeholder from the example and validate it in the lifespan.

**F-02 Vendor RSA private signing key is committed in the repository [digest]**
- deploy/vendor-portal-php/config.php L36: `define('PORTAL_SECRET_KEY', '<hardcoded literal>');` (value not reproduced).
- L40: `PAXS_AES_ROOT_KEY` derived from the same string literal as crypto.py:41.
- L43-71: `define('PAXS_VENDOR_PRIVATE_KEY', <<<EOD` heredoc containing a 2048-bit PKCS#8 private key (`-----BEGIN PRIVATE KEY-----` at L44, `-----END PRIVATE KEY-----` at L71). Comment at L42 says "for signing issued licenses".
- The file is tracked by git (introduced in commit 8af1381) and is in history.
- Digest check: the public key derived from this private key has SHA-256 `a999b9de96a7d17e6c4118948adcba7ce347961a650aca2f3ea5ae4927ce18bb`. The public key embedded in app/core/licensing/crypto.py:29-37 gives the same digest. So the pair matches the key the application trusts.
- Scenario: anyone with repo access can sign a license with any edition, features, seats, expiry, or fingerprint `*`. The app accepts it (manager.py:155, verify_license_signature). The licensing control is fully defeated.
- .gitignore:30-31 excludes `scripts/vendor_keys/` (directory does not exist). The key was evidently placed in config.php instead.
- Fix: treat the key as compromised. Generate a new pair, embed the new public key, remove config.php's private key from the working tree and history (git filter-repo), and reissue or revoke existing licenses. Move signing to an HSM or a secrets store. Remove PORTAL_SECRET_KEY and the derived AES key literal from source.

**F-03 Anonymous DICOM ingest via STOW-RS [read]**
- dicomweb.py:804-808: `@router.post("/studies")` and `@router.post("/studies/{study_uid}")` with `user: User | None = Depends(get_optional_user)`.
- The handler body (L806-829 read; grep of the whole file finds no `user is None` / 401 check; the only use of `user` is the tenant_id expression at L884) never rejects a missing user.
- Scenario: any host that can reach port 8000 can POST multipart DICOM and store instances, create studies and series, and fill the disk. RateLimit exempts `/dicom-web` (main.py:358-359).
- Fix: `Depends(get_current_user_any)` plus a role check. Rate-limit or size-limit as needed. Remove the `/dicom-web` exemption for POST.

**F-04 Anonymous report create, update, and finalize; anonymous report read [read]**
- reports.py:173-187: `POST /api/v1/reports/save`, `user: User | None = Depends(get_optional_user)`. The handler creates or updates the report (L186-187 branch) and the docstring says it also finalizes. The radiologist name falls back to "Dr. Radiologist" when there is no user (L184).
- reports.py:160-167: `GET /api/v1/reports/study/{uid}`, no auth dependency. Returns study metadata and report content.
- reports.py:227: `GET /api/v1/reports/print/{uid}` returns HTML, no auth dependency [scan].
- Scenario: anonymous clients can overwrite findings and impressions of finalized clinical reports, and read them.
- Fix: `require_destructive_role` (or PHYSICIAN-level) on all writes, and `get_current_user_any` on reads.

**F-05 Anonymous organization settings write, HTML injection, and unrestricted static upload [read]**
- settings.py:73-85: `POST /api/v1/settings/organization` with `user: User | None = Depends(get_optional_user)`. No check on `user`.
- settings.py:88-94: uploaded `logo_file` is read whole into memory (`await logo_file.read()`, no size cap) and written to `UPLOADS_DIR` with any extension. No content-type or extension check.
- settings.py:26: `UPLOADS_DIR = app/static/uploads`. main.py:560 serves `/static`, so uploaded files are served from the application origin.
- Header and footer HTML (L77-78) are passed straight to `update_org_settings`, and rendered into reports (see F-04 report print path).
- Combined with CSP `unsafe-inline` (main.py:229, F-19), an uploaded `.html` file runs script on the app origin.
- Scenario: an anonymous attacker uploads an HTML file to `/static/uploads/logo_x.html`, sends a link to an administrator, and runs script in the admin's session. Or fills the disk with 2 GiB uploads.
- Fix: require ADMIN on this route. Allow only image MIME types and extensions, with a size cap. Store uploads outside the static root, or serve them with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`.

**F-06 Patient data readable without authentication [scan + read]**
- Routes without any auth dependency in their signature, from the route scan of app/api/v1/:
  - WADO-RS and WADO-URI: dicomweb.py:136 `/wado`, :327, :339, :359, :376, :403, :414, :425, :512, :593, :735. These return DICOM instances, metadata, rendered frames, and bulk data.
  - dashboard.py: `/studies/{uid}` (study detail), `/studies/{uid}/series/{s}/instances`, `/studies/{uid}/series/{s}/thumbnail.png`, `/stats`, `/health*`.
  - fhir.py:24 onwards: `/fhir/metadata`, `/fhir/Patient` (search and read), `/fhir/ImagingStudy` (search and read). FHIR is enabled by default (config.py:187).
  - gsps.py: `/api/v1/gsps/{sop_uid}`, `/study/{uid}`, `/instance/{sop_uid}/presentations`.
  - license.py: `GET /api/v1/license`, `/status`, `/fingerprint` (see F-12).
  - metrics.py:87 `/metrics`. share.py `/{token}` and `/{token}/qr.png` (token by design).
- Optional-only (anonymous allowed, user not checked) [scan + grep]: QIDO-RS dicomweb.py:197, 256-257, 277-279; dashboard.py `/studies` (L445 uses user only to filter tenant); volume.py:65-344 (4 routes: `/info`, `/data`, `/mip`, `/mpr/slice`); fusion.py `/{study_uid}`; redis_panel.py `/status`, `/keys`, `/key`, `/ping`.
- The `.env.example:67-68` comment says viewers need open read access on a LAN, and `DICOMWEB_PUBLIC=true` is the documented switch. The switch is never read (F-10). So "open read" is hard-wired, not configurable.
- Scenario: any host on the network can list all studies, download all DICOM instances and volume voxel data, and read FHIR patient records, without logging in.
- Fix: default-deny. Decide explicitly which read routes are public and gate the rest with `get_current_user_any`. Wire `DICOMWEB_PUBLIC` into a dependency or delete it. Add router-level `dependencies=[...]`.

**F-07 Self-registration can create RADIOLOGIST accounts, which can delete studies [read]**
- config.py:145: `ALLOW_REGISTER: bool = True`.
- auth.py:170-214: `/api/v1/auth/register` (rate-limited 10/min per IP only). The role comes from the request body (`RegisterBody.role`, auth.py:79). The only check is `elif role == "ADMIN": raise 403` (auth.py:188-191). RADIOLOGIST passes (ROLES at auth.py:51).
- deps.py:105-114 `require_destructive_role` admits RADIOLOGIST. dashboard.py:834-836 `@router.delete("/studies/{study_uid}")` uses that gate. Also clinical protocol writes, audit trail (dashboard.py:388), DB perf panel and custom SQL (db_panel.py:37-60), report finalize (reports.py:117-135).
- Scenario: on a default install, `POST /api/v1/auth/register {"username":"x","password":"123456","role":"RADIOLOGIST"}` returns 201. The attacker then calls `DELETE /api/v1/studies/<uid>` with the bearer token and deletes archive data.
- Fix: ignore the requested role for self-registration and always assign PHYSICIAN. Default `ALLOW_REGISTER=False`. Restrict role changes to ADMIN (users.py). Make the destructive list ADMIN-only, or add a per-action permission.

**F-08 `redis` is not installed by requirements.txt, but the app imports it at startup [read]**
- requirements.txt:32 `# redis>=5.0` (commented out).
- app/services/redis_service.py:26 `import redis` at module level, with no try/except.
- app/api/v1/redis_panel.py:28 `from app.services import redis_service`. redis_panel is included at main.py:551 (and imported at main.py:491).
- Scenario: a clean install from requirements.txt, then `python run_paxs.py`, fails at import with `ModuleNotFoundError: No module named 'redis'` before uvicorn starts. Even if worked around, rate-limit imports redis_service lazily at ratelimit.py:52, before the try block that starts at L55, so an ImportError there is not caught and every request except `/static` and `/dicom-web` would 500.
- Fix: add `redis>=5.0` to requirements.txt (or guard the import and make the module degrade gracefully). Move the import inside the try in ratelimit.py.

### 🟡 Important

**F-09 X-Forwarded-For is trusted unconditionally [read]**
- ratelimit.py:29-33: `forwarded = request.headers.get("x-forwarded-for"); if forwarded: return forwarded.split(",")[0].strip()`.
- Scenario: send `X-Forwarded-For: <random>` on each request. Every request gets a new bucket, which defeats the 10/min login and register limit (auth.py:171, 218, 278) and the 300/min global limit (main.py:442). Per-account lockout (F-29) still applies, but credential stuffing across accounts is unthrottled.
- Note: deploy/haproxy.cfg:41 enables `option forwardfor`. Whatever HAProxy adds, the app reads the first entry of the header, and that entry can be set by the client. The exact HAProxy append behavior was not verified. Either way, the leftmost value is not trustworthy. Use the rightmost untrusted hop, or uvicorn `--proxy-headers` with a trusted proxy list.
- Fix: parse XFF from the right, trusting only known proxies. Or use uvicorn ProxyHeadersMiddleware with `forwarded_allow_ips` set to the proxy address.

**F-10 `DICOMWEB_PUBLIC` is a dead switch [grep]**
- config.py:253 and settings_service.py:57 (admin-editable registry). No other reader in the repo. Changing it does nothing. Documented as the gate for "open access" (.env.example:67-68).
- Fix: wire it into the auth dependency for DICOMweb and the anonymous browse routes, or delete it from config, registry, and docs.

**F-11 License gate is not enforced anywhere [grep]**
- `require_valid_license`, `require_license_feature`, `check_user_seat`, `check_study_quota` have no route callers (licensing/deps.py:12-46, manager.py:353-363). Only `ai.py:98,146` checks `lic.is_active` manually. `has_feature` is used only for display in ai.py:46-85.
- Consequence: an unlicensed or expired server serves DICOM, DICOMweb, uploads, and worker jobs normally. User seats and study quotas are never checked.
- Dev bypass: `PAXS_DEV_LICENSE` grants an unlimited enterprise license when the file is missing (manager.py:97-136). No warning beyond an INFO log.
- Fix: apply `require_valid_license` at the router or middleware level, or drop the licensing claims. Remove or gate the dev bypass behind a non-production flag.

**F-12 Blocking subprocess in an async route; unauthenticated license PII and hardware IDs [read]**
- license.py:57-63 `async def get_server_fingerprint()` calls `get_machine_fingerprint()` and `get_hardware_info()`. The latter (fingerprint.py:236-257) is not cached and runs PowerShell (4 s timeout) and up to three WMIC calls (3 s each) on Windows (fingerprint.py:70-142). These run synchronously in the event loop.
- Scenario: repeated unauthenticated `GET /api/v1/license/fingerprint` stalls the whole server for seconds per request. The 300/min global limit still allows about 5 requests per second per IP, and XFF (F-09) removes that cap.
- license.py:48-54 `GET /api/v1/license` and `/status` are unauthenticated. The response model (license.py:27-45) includes `customer_name`, `customer_organization`, `customer_email`, `features`, and expiry data. `/fingerprint` (license.py:57-63) returns MAC addresses, CPU ID, motherboard and OS serials.
- Fix: require ADMIN on all license routes. Run `get_hardware_info` in a thread (`asyncio.to_thread`) and cache its result.

**F-13 Refresh-cookie identity does not check session expiry [read]**
- deps.py:36-37 checks `sess.is_revoked` only. There is no `sess.expires_at` comparison.
- Expired rows are deleted only by `CompressionWorker._sweep_sessions` (worker.py:127-131, 184-206), which runs inside the worker tick every 60 ticks. If `WORKER_ENABLED=false`, or the worker dies, or the process runs without a worker, expired refresh cookies remain valid as API credentials indefinitely (`get_optional_user` falls back to them, deps.py:86).
- Also: `sessions.expires_at` is extended on every refresh (auth.py:324), so an active browser never expires.
- Fix: check `sess.expires_at > now` in deps.py:36 and in auth refresh. Run the sweep in lifespan as well, independent of the worker.

**F-14 Read-replica dependency: exceptions are swallowed and a second yield causes a 500 [exp + read]**
- session.py:202-213: the replica branch yields inside `try: ... except Exception:`. If the endpoint raises (for example HTTPException 404 or 409), the exception is thrown into the generator, caught, logged as "Read replica unavailable - falling back to primary", and the generator yields again on the primary branch (L214-220). contextlib then raises `RuntimeError: generator didn't stop after athrow()`. Reproduced with a local verification script: the endpoint's exception is replaced by RuntimeError and the log line is printed.
- Scope: only when the admin enables the replica toggle (ServerSetting `dbp_read_replica`, session.py:147-162). Affects every endpoint that uses `get_read_db` (dashboard.py per [grep]).
- Also: `_replica_toggle_enabled()` runs a synchronous query on `SyncSessionLocal` (session.py:156-159) on every `get_read_db()` call (L202). That blocks the event loop per request.
- Fix: yield outside the try. Wrap only the probe in try/except, then yield once. Cache the toggle value or read it asynchronously.

**F-15 Bootstrap: `create_all` on boot plus stamp-on-exists skips migrations [read]**
- session.py:234-240 runs `Base.metadata.create_all` on every boot. It creates missing tables (for example sessions, auth_audit_log, tenants, reports) but does not add columns to existing tables.
- db_admin.py:230-271 `run_migrations`: on any error containing "already exists" or starting with "duplicate " it calls `command.stamp(cfg, "head")` (L265-271).
- Scenario: a DB at an old revision, booted once with the new code. `create_all` creates `sessions` and `tenants`. Running "migrate to head" then hits `CREATE TABLE tenants` (or `audit_log`, which already exists) and fails with "already exists". The code stamps head, skipping `ALTER TABLE users ADD failed_attempts`, `studies ADD tenant_id`, `studies ADD lifecycle_state`, `users ADD is_active`, and others. The app then fails with unknown-column errors on those columns.
- Fix: do not stamp on "already exists". Diff the live schema against the expected one and fail loudly. Make `init_db` not call `create_all` on a DB that has an alembic version, or run migrations only.

**F-16 Five model tables have no migration [grep + script]**
- `assigning_authorities` (models.py:232), `patient_identities` (272), `organization_settings` (1543), `report_templates` (1562), `lossless_compression_rules` (1028). Verified against all 38 migrations with a script.
- Effect: DBs built only from Alembic lack these tables until `init_db` runs at boot. Features (PIX/PDQ, organization settings, report templates, lossless rules) break on those DBs.
- Fix: add a migration that creates the five tables. Or remove the models if unused.

**F-17 SQLite: BIGINT primary keys without the SQLite variant [read]**
- a12345678901_add_storage_quotas.py:26 `sa.Column("id", sa.BigInteger(), primary_key=True)`.
- z2345678a9b0_add_storage_backends.py:25 `sa.Column("id", sa.BigInteger(), primary_key=True)`.
- On SQLite, only the exact type INTEGER is a rowid alias. BIGINT PRIMARY KEY does not auto-increment, so inserts without an explicit id store NULL. The models use `BigIntPK` with the SQLite variant (models.py:36), so ORM inserts also omit the id. Storage backend CRUD on SQLite will break.
- Fix: use `sa.BigInteger().with_variant(sa.Integer(), "sqlite")` in both migrations, as other files do.

**F-18 HSTS is sent over plain HTTP when TLS certificates are missing [read]**
- main.py:243-244 adds `Strict-Transport-Security` when `HTTPS_ENABLED`, which is a settings flag, not "TLS is active".
- run_paxs.py:24-34 falls back to HTTP silently (warning print only) when the cert or key files are missing. config.py:47-49 claims a self-signed cert is generated on first boot. No such code exists for HTTPS (only for DICOM, dicom/tls.py).
- Consequence: with `HTTPS_ENABLED=true` and no certs, browsers receive HSTS over HTTP, which RFC 6797 forbids (comment main.py:241-242). LAN access can break after a later protocol switch.
- Fix: derive the HSTS condition from the actual TLS state. Fail startup if HTTPS is required and certs are missing. Implement the generation the comment describes, or correct the comment.

**F-19 CSP allows `unsafe-inline` and `unsafe-eval` for scripts [read]**
- main.py:229 `script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval'`. This removes most of CSP's XSS protection. Combined with F-05 (uploads served from the origin) it makes stored XSS exploitable.
- Fix: move to nonces or hashes. Remove `unsafe-eval` if the bundle allows it.

**F-20 Insecure defaults in config and examples [read]**
- config.py:25 `DATABASE_URL` default embeds `paxs_user:paxs` at 127.0.0.1.
- .env.example:39 `SECRET_KEY=change-me-to-a-long-random-string`.
- .env.example:40 `TOKEN_EXPIRE_MINUTES=480` (8 hours) versus config.py:140 default 15.
- config.py:158 `AUTH_COOKIE_SECURE: bool = False` by default.
- Fix: no default credentials in config (require DATABASE_URL). Generate SECRET_KEY on first run. Align the example and default lifetimes. Default AUTH_COOKIE_SECURE to True when HTTPS is enabled.

**F-21 Access tokens and GET /logout do not revoke sessions [read]**
- Access tokens are stateless and valid until `exp` (security.py:123-153). Logout does not invalidate them (auth.py:334-344 revokes only the refresh session). A stolen 480-minute token (per .env.example) remains valid after logout.
- `GET /logout` (main.py:713-719) clears the cookie but does not revoke the server-side session. A copied refresh token stays valid for 7 days.
- Fix: revoke sessions in GET /logout too (or make it POST). Keep access-token lifetimes short. Consider a token version check on the user row.

**F-22 Secrets stored in plaintext in the database [read]**
- models.py:753 `DicomNode.web_password` (String 256), models.py:1087-1088 `StorageBackendConfig.access_key` and `secret_key` (String 255). Stored raw.
- models.py:533 `PatientShare.share_token` stored raw (not hashed). A DB read gives working share links.
- Fix: encrypt these at rest with a key from the environment (or KMS), and hash the share token (store a digest, return the token once).

**F-23 PHI in the slow-query log, and arbitrary SELECT via custom SQL [read]**
- perf.py:76-84 stores `repr(params)[:200]` and `sql[:600]` in the in-memory ring buffer. Query parameters include patient IDs, names, and accession numbers.
- db_panel.py:49-50 exposes the buffer to ADMIN and RADIOLOGIST (`require_destructive_role`).
- services/db_panel.py:202-216 allows any statement starting with SELECT or EXPLAIN, with no table restriction and no check for SLEEP/BENCHMARK or INTO OUTFILE. db_panel.py:55-60 runs it. Reachable from F-07.
- Fix: keep PHI out of logged params (log statement shape only). Restrict custom SQL to ADMIN with an allow-list of tables, or remove it.

**F-24 License heartbeat and expiry handling [read]**
- manager.py:330-333: an unsigned `revoked` field in the portal response disables the license. Default portal is plain HTTP (config.py:316). A network attacker on the path can revoke the license and stop the server (F-02 is the other side).
- manager.py:174-191: expiry is evaluated only inside `load_license`. The heartbeat (manager.py:290-345) does not re-evaluate it. A license that expires while the process runs stays VALID until restart.
- manager.py:51 hard-codes `_grace_period_days = 7`. config.py:318 `LICENSE_GRACE_PERIOD_DAYS` is ignored (grep: no reader). config.py:315 `LICENSE_FILE_PATH` is ignored (grep: no reader).
- Fix: require HTTPS for the portal. Verify a signature on heartbeat responses. Re-check expiry on each heartbeat and on a timer. Read the grace period from settings.

**F-25 Linux fingerprint includes virtual interfaces [read]**
- fingerprint.py:42-57 includes every non-`lo` interface under `/sys/class/net`. Docker bridges, veth pairs, and libvirt bridges come and go, so the fingerprint changes when containers start or stop. A machine-bound license then reports HARDWARE_MISMATCH.
- Windows path uses only `uuid.getnode()` (fingerprint.py:31), one NIC chosen by the stdlib. Changing the primary adapter changes the fingerprint.
- Fix: include only physical interfaces (skip docker*, veth*, br-*, virbr*, and interfaces without a backing device). Prefer stable identifiers.

**F-26 Gzip middleware: synchronous compression on the event loop; Range responses compressed [read]**
- main.py:124 `gzip.compress(data, compresslevel=6)` runs on the event loop for responses up to 32 MiB (`_GZIP_MAX_BYTES`, L67). Blocks all requests during compression.
- main.py:102-109 treats any status >= 200 (except 204 and 304) as eligible, including 206 Partial Content. Compressing a 206 breaks Range semantics (Content-Range refers to uncompressed bytes).
- Fix: compress in a thread (`asyncio.to_thread`) or cap at a much smaller size. Exclude 206.

**F-27 `reload=True` is hard-coded for every run [read]**
- run_paxs.py:49 `reload=True`. The reloader spawns a child process and restarts it on any `.py` change. The DICOM SCP, worker, and HL7 threads restart with it, dropping live DIMSE associations.
- PAXS_HOST defaults to 0.0.0.0 (run_paxs.py:37), so the server listens on all interfaces by default.
- Fix: make reload opt-in (for example `PAXS_RELOAD=1`). Default the host to 127.0.0.1 or make it explicit.

**F-28 Startup recovery resets other nodes' in-flight jobs [read]**
- worker.py:367-386 `_recover_stuck_jobs` sets every `CompressionJob` with status RUNNING to PENDING, and every `Series.triage_status` RUNNING to PENDING. It has no node filter.
- config.py:13-16 says nodes share the DB (load-balanced deployment, LOAD-BALANCING.md). Restarting node B while node A is compressing re-queues A's jobs. A and B then process the same job.
- Same pattern expected in `recover_stuck_queue_items()` (routing_service, not read).
- Fix: record `claimed_by` (instance id) on claim and recover only that node's rows, or use a lease with an expiry.

**F-29 Account lockout can be used for denial of service [read]**
- auth.py:228-255: five wrong passwords lock the account for 15 minutes, and the lock applies even to the correct password. Anyone can lock `admin` (a predictable username).
- Fix: keep the lockout, but also throttle by IP and use a larger threshold for ADMIN, or offer an out-of-band unlock.

**F-30 First registrant on an empty database becomes ADMIN [read]**
- auth.py:182-185: `if total == 0: role = "ADMIN"`. With `ALLOW_REGISTER=True` (default), the first network client to reach `/register` on a fresh install owns the system.
- Fix: create the first admin from the CLI or the setup wizard, and disable self-registration until one exists.

**F-31 ViewerAuthGate bypass and cookie-presence-only checks [read]**
- main.py:307-310 `if "share=" in query: bypass`. Any query string containing the substring `share=` skips the gate (for example `/settings/?x=share=1`). Returns the SPA shell to unauthenticated users.
- main.py:321-331: for non-entry deep links, only cookie presence is checked, not validity.
- The shell is static, so the impact is limited to the HTML, but it defeats the gate's stated purpose. Data APIs still depend on F-04 through F-06.
- Fix: match the parameter name exactly (`share` key, not a substring). Validate the session for deep links too.

### 🟢 Nits

**F-32 PBKDF2 iteration count and no rehash path [read]**
- security.py:82 200,000 iterations. Current guidance for PBKDF2-HMAC-SHA256 is roughly 600,000. No upgrade on login. security.py:101 ignores the algorithm id.
- Fix: raise iterations, store the iteration count per hash (already done), and rehash on successful login when below target.

**F-33 Timing and existence oracles [read]**
- auth.py:224-226 unknown username returns before PBKDF2 (measurable). auth.py:179-180 register returns 409 "Username already taken" (existence oracle).
- Fix: run a dummy hash for unknown users. Return a generic response on register.

**F-34 Unused import and unused mixin [read]**
- security.py:15 `import uuid` never used. models.py:43-49 `TimestampMixin` is defined but no model inherits it.

**F-35 Documentation and config drift [read]**
- config.py:315 `LICENSE_FILE_PATH` unused. config.py:54 `HTTPS_PORT` unused. config.py:47-49 claims certificate auto-generation for HTTPS that does not exist.
- perf.py:13 docstring says `explain_query` is in this module; it is in app/services/db_panel.py:190.
- session.py:10-11 docstring names `PAXS_SQLITE_JOURNAL` and says WAL is default. The field is `SQLITE_JOURNAL_MODE` (config.py:103) with default "delete".
- migrations/versions/6070910a1b2c_add_commitment_statuses.py:4 docstring "Revises: f6070910a1b2" versus L21 `down_revision = 'g7081011b2c3'`.
- .env.example:32 `DEFAULT_COMPRESSION=RLE` (default is JPEG-LS, config.py:106). .env.example:36 `WORKER_POLL_SECONDS=5` (default 3.0).
- app/__init__.py:3 and config.py:11 both hard-code 0.1.0.

**F-36 Logger and default-executor hygiene [read]**
- main.py:380-382 default executor is never shut down. main.py:413-414 cancels the heartbeat task without awaiting it. main.py:37-40 calls `basicConfig` at import, which overrides any logging set up by the host process.

**F-37 settings.example.json is unrelated and unreferenced [grep]**
- Contains `gatewayProviders` with `"apiKey": "sk-your-key-here"` and `baseUrl`. No PAXS code reads it. Appears to be a copy from another project.
- Fix: remove, or document what reads it.

**F-38 Requirements hygiene [read]**
- No upper bounds, no lock file, no hashes.
- Dev-only packages mixed into the runtime list: pytest, httpx (requirements.txt:35-36). httpx is also a runtime import (app/services/dicomweb_client.py:30), so it must stay.
- ldap3 used lazily (services/ldap_service.py:41-42, inside try) but not listed. boto3 listed as optional but not imported anywhere (S3 backend does not use it; the comment at config.py:59 is stale).
- Fix: split dev requirements, add ldap3 as optional, pin with a lock file.

**F-39 Import-time snapshots that ignore DB overrides [read]**
- security.py:26 `COOKIE_MAX_AGE` and main.py:243 HSTS are computed at import, before `apply_db_overrides` (main.py:386). Admin changes to REFRESH_TOKEN_EXPIRE_DAYS do not change the cookie max-age, while the server-side session expiry does change (auth.py:114-115 reads settings at call time).

**F-40 Model vs migration index-name drift [read]**
- Example: models.py:199 `patient_name index=True` creates `ix_patients_patient_name` (migration 56d9 L59 agrees). But ScheduledProcedureStep indexes in the migration (`ix_sps_*`) differ from the model defaults (`ix_scheduled_procedure_steps_*`). `ix_patients_birth_date` (b3456789c0d1 L17) has no model counterpart. `sessions.refresh_token_hash` unique index not in migration (c0d1e2f3a4b5 L49).
- Effect: alembic autogenerate noise. No runtime impact.

**F-41 Fragile header pre-check in gzip middleware [read]**
- main.py:79 `"gzip" not in scope.get("headers", []).__str__()` relies on the repr of a list of bytes tuples. It works, but it is brittle. The real check is at L85.

**F-42 Public documentation and metrics endpoints [read]**
- main.py:426-427 `/api/docs` and `/api/openapi.json` are public. metrics.py:87 `/metrics` is public and exposes instance id, queue depths, replica lag.
- Fix: gate in production, or restrict to internal networks.

**F-43 Permissive CORS default [read]**
- config.py:254 `CORS_ORIGINS="*"`, main.py:432. `allow_credentials=False` (main.py:433) prevents cookie reads cross-origin, so impact is limited. CORS also sits inside RateLimit, so 429 responses lack CORS headers (see 3.2).

**F-44 Read-only SQL guard is prefix-only [read]**
- services/db_panel.py:214-216 checks only that the statement starts with SELECT or EXPLAIN. Covered in F-23.

**F-45 Per-request NullPool connection cost [read]**
- session.py:84-86 deliberately uses NullPool for the async engine. Every request opens a new MySQL connection and authenticates. Justified by the comment (L79-82), but measurable under load. Consider a loop-bound pool.

**F-46 Single-process rate limiter fallback [read]**
- ratelimit.py:76-100 keeps counters per process. Without Redis, multiple uvicorn workers each allow the full limit. Documented in the module docstring (L3-4).

---

## 9. Open questions

1. Was the vendor private key in deploy/vendor-portal-php/config.php used to sign production licenses? If yes, every issued license must be reissued after rotation (F-02).
2. Is anonymous read of DICOMweb, FHIR, and dashboard study data intended for the LAN viewer (.env.example:67-68)? If so, the intent should be enforced by `DICOMWEB_PUBLIC` (F-10). If not, F-06 is a blocker.
3. Anonymous QIDO-RS scope: `tenant_id=None` for anonymous users (dicomweb.py:884). Does `None` mean "no tenant filter" (all studies visible) or "legacy scope only"? The service code was not read. This determines whether tenant isolation is bypassed for anonymous users.
4. Was the reversed role check in register (F-07) meant to block all non-PHYSICIAN roles, or only ADMIN? The comment at auth.py:189-190 suggests ADMIN only.
5. Does the PHP-side canonical JSON used by `scripts/generate_license.py` and the vendor portal match `get_canonical_payload_bytes` (crypto.py:87-104)? Datetime formatting ("+00:00" vs "Z"), null fields, and key ordering all matter for signature verification. Not tested.
6. Does `recover_stuck_queue_items()` (routing_service) also reset RUNNING rows globally (F-28)?
7. Do the worker sub-steps (`_process_triage`, `_sweep_lifecycle`, `_sweep_routing`) catch their own exceptions? If not, one failing step skips the later steps in the same tick, including the session sweep (worker.py:99-132).
8. FastAPI exception-into-yield-dependency behavior: F-14 was reproduced with `contextlib` semantics, not with a running FastAPI app. A runtime test with the installed FastAPI version is needed.
9. `alembic upgrade head` on a fresh MySQL and on a fresh SQLite database was not executed. The DAG and table/column dependencies were checked by reading and by script only.
10. The gitignore references `scripts/generate_license.py` and `scripts/vendor_keys/`. The generator exists (scripts/generate_license.py, 9.7 KB, not read). The key directory does not exist.
11. Is `FHIR_ENABLED=true` (default) intended for production? It exposes Patient and ImagingStudy without auth (F-06).
12. `scripts/reset_admin_password.php` is in the repo (outside this scope, not read). If it is reachable from the web, it is an admin-takeover risk.
13. The HL7 listener (app/dicom/hl7.py) allows one thread per TCP connection with no cap (hl7.py:731). Not audited in depth. Only relevant when HL7_ENABLED is true.
14. Starlette `RedirectResponse` quoting of the Location header (used by `role_landing`, auth.py:66-72, and `/login?next=`) was not verified against the installed version. The current code only accepts same-origin paths starting with "/" and not "//"; the backslash variant `/\\host` was not tested.
15. Whether `apply_db_overrides` can change `DATABASE_URL` or other security-relevant keys at boot. The EDITABLE_KEYS registry was grepped; it does not list SECRET_KEY or DATABASE_URL, but the full registry (settings_service.py) was not read.
