"""Application configuration (pydantic-settings, .env aware)."""

from functools import lru_cache
from hmac import compare_digest
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, StrictInt, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The origin used in share links / QR codes / FHIR URLs when PUBLIC_BASE_URL is
# left unset. Loopback is reachable at least by the operator running the server,
# which is strictly better than the old ``http://0.0.0.0`` default — a *listen*
# address that no browser can ever resolve (V2-2 in plans/legacy/archive-notes/PAXS_VIEWER_DIAGNOSTIC_AUDIT.md).
DEFAULT_PUBLIC_BASE_URL = "http://localhost:8000"

# Hosts that are bind/listen addresses rather than routable names. A URL built
# from one of these is dead on arrival, so it is treated as "not configured".
_UNROUTABLE_HOSTS = {"", "0.0.0.0", "::", "[::]"}


def is_routable_public_base_url(value: str) -> bool:
    """True when ``value`` is an absolute URL a browser could actually reach."""
    text = (value or "").strip()
    if not text:
        return False
    try:
        parts = urlsplit(text)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").strip().lower()
    return host not in _UNROUTABLE_HOSTS


class Settings(BaseSettings):
    # --- App ---
    APP_NAME: str = "PAXS Archive Server"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    # Load-balanced deployment (roadmap #47): each stateless node reports this
    # id in /health so HAProxy round-robin and the dashboard show which node
    # served a request. Nodes share the DB and object storage — see
    # deploy/LOAD-BALANCING.md.
    PAXS_INSTANCE_ID: str = "node-1"

    # --- Reverse proxy / sub-path deployment ---
    # ROOT_PATH is the URL prefix the app is served under when it is NOT at the
    # domain root, e.g. "/paxs" for https://host/paxs/. Leave empty for a
    # root-mounted deployment (the default). The equivalent runtime header is
    # X-Forwarded-Prefix, which ForwardedPrefixMiddleware honours per request.
    ROOT_PATH: str = ""
    # Which peers may set X-Forwarded-* headers. Accepts "*", a hostname/glob, a
    # comma-separated list, or CIDR. The default trusts only loopback — a proxy
    # in another container has a different IP, so its X-Forwarded-* headers are
    # ignored until it is listed here (or set to "*" behind a trusted network).
    FORWARDED_ALLOW_IPS: str = "127.0.0.1"
    # Hostnames permitted in incoming Host / X-Forwarded-Host values when
    # deriving browser-facing URLs. Supports exact names and *.domain patterns.
    ALLOWED_HOSTS: str = "localhost,127.0.0.1,::1"

    # --- Database ---
    # PostgreSQL is the only supported database (MySQL and SQLite were retired).
    # The API uses asyncpg; the DICOM receiver/workers derive a psycopg (v3)
    # sync URL from this one. Override via .env (see .env.example).
    DATABASE_URL: str = "postgresql+asyncpg://paxs_user:paxs@127.0.0.1:5432/paxs_db"

    # --- DICOM DIMSE receiver ---
    DICOM_AE_TITLE: str = "PAXS_SCP"
    DICOM_HOST: str = "0.0.0.0"
    DICOM_PORT: int = 10400
    DICOM_RECEIVER_ENABLED: bool = True
    DCMTK_BIN_DIR: str = ""
    DCMTK_MOVE_PORT: int = Field(default=0, ge=0, le=65535)
    DCMTK_MOVE_AE_TITLE: str = "PAXS_MOVE"
    DCMTK_MOVE_PROFILE: str = ""
    DICOM_MAX_ASSOCIATIONS: int = 16
    # PDU size (bytes) and DIMSE/ACSE/network timeouts for the pynetdicom SCP.
    # 128 KiB (vs pynetdicom's 16 KiB default) significantly improves gigabit
    # CT/MR C-STORE/C-MOVE throughput; explicit timeouts prevent stalled peers
    # from holding association slots indefinitely.
    DICOM_MAX_PDU_SIZE: int = Field(default=131072, ge=16384, le=1048576)
    DICOM_NETWORK_TIMEOUT_SECONDS: int = Field(default=60, ge=5, le=600)
    DICOM_ACSE_TIMEOUT_SECONDS: int = Field(default=30, ge=5, le=300)
    DICOM_DIMSE_TIMEOUT_SECONDS: int = Field(default=60, ge=5, le=600)
    # Bounds Storage Commitment's in-memory association tracking and dispatch
    # backlog. Zero disables new N-ACTION admission (responds Out of Resources).
    STORAGE_COMMIT_MAX_PENDING: int = Field(default=256, ge=0)
    # Upper bound for cached calling-AE to tenant mappings; zero disables caching.
    DICOM_TENANT_AE_CACHE_MAX_ENTRIES: int = Field(default=1024, ge=0)

    # A-17: enforce PS3.5 UID grammar on the mandatory Type 1 UIDs at ingest.
    #
    # A *missing*, blank or placeholder ("UNKNOWN") UID is rejected either way —
    # that is the defect this guards, and no setting can turn it off. This
    # switch governs only the *format* rule: with it on, a value that is present
    # and unique but not digits-and-dots (some legacy archives hold hex-bearing
    # UIDs) is refused; with it off such a value is accepted with a warning
    # naming it. Turn it off only for an archive that genuinely contains
    # non-conformant identifiers, and fix the source afterwards.
    DICOM_UID_STRICT_FORMAT: bool = True

    # --- DICOM TLS (AES-128/256 over TLS 1.2/1.3, self-signed by default) ---
    # When enabled the receiver wraps its DIMSE socket in TLS (pynetdicom
    # `ssl_context`); SCU helpers (node ping, forwarding) do the same. If the
    # cert/key files don't exist yet, a self-signed certificate is generated
    # into DICOM_TLS_CERT/KEY on first start (requires `openssl` on PATH).
    DICOM_TLS_ENABLED: bool = False
    DICOM_TLS_CERT: str = "./certs/paxs.crt"
    DICOM_TLS_KEY: str = "./certs/paxs.key"
    # Optional CA bundle for mutual TLS: when set, the server requires and
    # verifies client certificates signed by this CA (empty = no client auth).
    #
    # B-16: this is also the SCU's trust anchor. When TLS is enabled and neither
    # this nor a per-node `trust_cert` pin is configured, a production SCU
    # **refuses to connect** rather than encrypting without authenticating the
    # peer (which is indistinguishable from a MITM). Development keeps the old
    # opportunistic behaviour, with a warning.
    DICOM_TLS_CA: str = ""
    # B-8: comma-separated allowlist of calling AE titles permitted to associate.
    # Empty = no allowlist (any AE, subject to the other gates). When set, a
    # caller is accepted only if its title is listed here or registered as a
    # `dicom_nodes` peer — everything else is rejected at association.
    DICOM_ALLOWED_CALLING_AES: str = ""

    # --- Web HTTPS (dashboard / DICOMweb) ---
    # When enabled the uvicorn server listens on HTTPS using the cert/key
    # below. A self-signed pair is auto-generated on first boot (reuses the
    # DICOM TLS cert infrastructure). Disable for plain HTTP behind a
    # reverse proxy that terminates TLS.
    HTTPS_ENABLED: bool = False
    HTTPS_CERT: str = "./certs/https.crt"
    HTTPS_KEY: str = "./certs/https.key"
    HTTPS_PORT: int = 8443

    # --- Optical media ---
    DISC_BURN_ENABLED: bool = False
    DISC_BURN_DRIVE: str = Field(default="", pattern=r"^([A-Za-z]:)?$")
    DISC_BURN_VOLUME_LABEL: str = Field(default="PAXS_DICOM", pattern=r"^[A-Z0-9_]{1,16}$")

    # --- Storage ---
    STORAGE_ROOT: str = "./storage"
    # Maximum cached backend objects across named and environment backends.
    # Zero disables retention while preserving the backend factory contract.
    STORAGE_BACKEND_CACHE_MAX_ENTRIES: int = Field(default=128, ge=0)
    # Sampled viewer availability. Zero sample disables the endpoint; zero cap
    # reports unknown without performing object-store I/O.
    AVAILABILITY_SAMPLE_PER_SERIES: int = Field(default=3, ge=0)
    AVAILABILITY_MAX_CHECKS: int = Field(default=300, ge=0)
    # Object-storage backend: "local" (filesystem under STORAGE_ROOT) or "s3"
    # (S3-compatible — AWS S3 / MinIO). S3 needs boto3 installed.
    STORAGE_BACKEND: str = "local"
    S3_ENDPOINT_URL: str = ""       # e.g. http://localhost:9000 for MinIO (empty = AWS)
    S3_REGION: str = "us-east-1"
    S3_BUCKET: str = "paxs"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_PRESIGN_EXPIRES: int = 900    # seconds
    # Read-only compatibility for the two historical key shapes: absolute
    # paths already inside STORAGE_ROOT, and CWD-relative "storage/..." paths
    # only when that directory is the same canonical STORAGE_ROOT. The flag
    # never permits reads or writes outside the root; writes/deletes always
    # require ordinary relative keys. Keep disabled after migration.
    STORAGE_ALLOW_LEGACY_PATHS: bool = False
    # B-18: the only directories a legacy absolute key may resolve into, beyond
    # STORAGE_ROOT itself. Comma- or os.pathsep-separated absolute paths, read
    # only, checked after canonical resolution (so a symlink inside an allowed
    # root cannot reach out of it). Empty — the default — means "legacy absolute
    # keys are confined to STORAGE_ROOT", which is the pre-B-18 behaviour but
    # stated as a policy instead of implied by the containment check.
    STORAGE_LEGACY_ROOTS: str = ""
    # 08-3: the H09 sweeper that purges abandoned `.tmp_*` staged objects (a
    # crash between stage and promote leaves one behind). Period and age are
    # separate settings because they answer different questions — "how often do
    # we look" and "how old is abandoned" — and a test that wants a 1-second
    # period must not also declare a live upload dead. `0` (or negative)
    # disables the sweep: AGENTS.md N-5 says 0 means off, never "use the
    # default", so the loop returns instead of sweeping on a fallback period.
    STORAGE_SWEEP_INTERVAL_SECONDS: int = 3600
    STORAGE_STALE_THRESHOLD_SECONDS: int = 3600
    # Local filesystem low-water mark guard: reject new ingests cleanly with
    # DICOM 0xA700 / HTTP 507 before the disk reaches 100% and crashes the host
    # or PostgreSQL. Set either threshold to 0 to disable that floor.
    STORAGE_MIN_FREE_GB: float = Field(default=1.0, ge=0.0)
    STORAGE_MIN_FREE_PERCENT: float = Field(default=2.0, ge=0.0, le=50.0)

    # --- Database administration (Settings > Database tab) ---
    # Where a saved PostgreSQL configuration is persisted (rewritten as
    # DATABASE_URL on "Save configuration") and where backup dumps go.
    ENV_FILE_PATH: str = ".env"
    BACKUP_DIR: str = "./backups"

    # --- Connection pool tuning (PostgreSQL) ---
    # Async API requests share the long-lived FastAPI loop. DICOM receiver and
    # workers use the separate bounded sync pool. pool_pre_ping drops stale
    # connections after a server restart, and pool_recycle rotates them before
    # a proxy/firewall idle timeout (e.g. PgBouncer, cloud load balancers) can.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT_SECONDS: float = 5.0
    DB_POOL_RECYCLE_SECONDS: int = 1800
    DB_POOL_PRE_PING: bool = True
    # Enable when placing PgBouncer (transaction pooling) in front of PostgreSQL:
    # disables asyncpg & psycopg3 server-side prepared statement caching so
    # pooled backend connections never collide with DuplicatePreparedStatementError.
    DB_PGBOUNCER_COMPAT: bool = False
    # 0 disables the capacity warning until the DBA supplies the server cap.
    DB_MAX_CONNECTIONS: int = Field(default=0, ge=0)
    # Number of application processes, including separate worker processes.
    APP_WORKERS: int = Field(default=1, ge=1)
    DB_READ_ACQUIRE_RETRIES: int = Field(default=1, ge=0, le=3)
    DB_READ_RETRY_DELAY_SECONDS: float = Field(default=0.1, ge=0, le=2)
    # Optional PostgreSQL read replica (hot standby) (Admin → DB Pool). When set, the panel shows
    # its live status (lag, connected) and an admin toggle can route the two
    # hottest read endpoints (/stats, /studies) through it, falling back to the
    # primary automatically if the replica is unreachable.
    DB_READ_REPLICA_URL: str = ""
    # Slow-query log default threshold (Admin → DB Pool). Statements that ran
    # at or above this many milliseconds land in the in-memory slow-query ring.
    DB_SLOW_QUERY_THRESHOLD_MS: int = 500

    # --- Compression / worker ---
    DEFAULT_COMPRESSION: str = "JPEG-LS"  # AS_SOURCE | UNCOMPRESSED | JPEG-LS | JPEG2000 | HTJ2K | RLE
    COMPRESSION_ENABLED: bool = True
    COMPRESSION_TIMING: Literal["immediate", "scheduled"] = "immediate"
    COMPRESSION_START_TIME: str = Field(default="22:00", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    COMPRESSION_END_TIME: str = Field(default="06:00", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    THUMBNAIL_ENABLED: bool = True
    THUMBNAIL_ON_RECEIPT: bool = False
    PATIENT_NAME_DISPLAY: Literal["default", "source", "structured", "remove_carets"] = "default"
    WORKER_ENABLED: bool = True
    WORKER_POLL_SECONDS: float = Field(default=3.0, ge=0.1, le=300)
    # Lossy re-compression is a destructive, irreversible operation (it discards
    # the original sample values), so it is **fail-closed**: OFF by default.
    # Both entry points in app/services/lossy_compression_service.py return
    # without modifying anything while this is false. Only enable it with an
    # explicit retention policy and a validated backup.
    LOSSY_COMPRESSION_ENABLED: bool = False
    # How many PENDING compression jobs a worker claims per tick. Standalone
    # worker processes (python -m app.services.worker) scale compression
    # ~linearly with cores; this bounds how many each process grabs.
    WORKER_BATCH_SIZE: int = Field(default=15, ge=1, le=1000)
    # Concurrent compression jobs inside one worker process. Keep 1 in embedded
    # API mode to protect request latency; raise to 2-4 in standalone workers.
    WORKER_COMPRESSION_CONCURRENCY: int = Field(default=1, ge=1, le=16)
    WORKER_JOB_LEASE_SECONDS: int = Field(default=300, ge=30)
    WORKER_JOB_HEARTBEAT_SECONDS: int = Field(default=30, ge=5)
    WORKER_JOB_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=20)
    # Maximum queued + running one-shot transfer jobs. Zero disables enqueueing.
    TRANSFER_JOB_QUEUE_CAP: int = 100

    # --- /stats storage-usage cache (scalability) ---
    # storage_usage_bytes() walks the storage tree (O(objects)); the dashboard
    # /stats endpoint uses a TTL cache so a big archive doesn't pay that walk
    # on every page load. 0 disables the cache.
    STATS_USAGE_CACHE_SECONDS: int = 60
    # Maximum process-local backend usage entries; zero disables local retention.
    STATS_USAGE_CACHE_MAX_ENTRIES: int = Field(default=128, ge=0)

    # --- Brute-force account lockout (auth plan §4.1) ---
    # After MAX_FAILED_ATTEMPTS consecutive failed logins the account is
    # locked for LOCKOUT_DURATION_MINUTES. Attempts 1-4 return a generic
    # error; the 5th triggers the lock; a subsequent attempt while locked
    # reports the remaining minutes. The per-IP login rate limit (see
    # RATE_LIMIT_LOGIN_PER_MINUTE) runs in addition.
    MAX_FAILED_ATTEMPTS: int = 5
    LOCKOUT_DURATION_MINUTES: int = 15
    # Global security headers applied to every HTTP response (auth plan §4.2):
    # HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, CSP,
    # Permissions-Policy. Set False behind a proxy that injects its own.
    SECURITY_HEADERS_ENABLED: bool = True

    # --- Security ---
    # H06/B-1: the shipped default is deliberately recognisable so production can
    # refuse to start with it (see validate_production_settings).
    SECRET_KEY: str = "paxs-insecure-dev-key-change-me"
    # B-1: "production" is the DEFAULT so a deployment that forgets to set it
    # fails closed instead of silently skipping every production check below.
    # A local/dev run opts out explicitly with ENVIRONMENT=development (see
    # .env.example). Any unknown or misspelled value is also treated as
    # production, so a typo can never disable the gate.
    ENVIRONMENT: str = "production"
    # B-4: one-time token required to create the FIRST administrator account.
    # Empty (the default) disables API bootstrap entirely, so an empty database
    # can never be claimed by whoever reaches the port first. Generate one with
    #   python -c "import secrets; print(secrets.token_urlsafe(32))"
    # and unset it again once the administrator exists.
    SETUP_TOKEN: str = ""
    # H06: session cookie hardening. Secure requires HTTPS; SameSite=lax keeps
    # the viewer usable while blocking cross-site sends.
    AUTH_COOKIE_SECURE: bool = False
    AUTH_COOKIE_SAMESITE: str = "lax"
    # Short-lived bearer access token (auth plan §2.1). The dashboard/viewer
    # silently refresh it via POST /auth/refresh using the rotating refresh
    # cookie, so a 15-minute lifetime is invisible to the user.
    TOKEN_EXPIRE_MINUTES: int = 15
    # Rotating refresh token (auth plan §2.2/§3.2): opaque value stored
    # SHA-256 hashed in the sessions table, rotated on every use, with
    # whole-family revocation on reuse/theft. Carried in the HttpOnly cookie.
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # H02: self-registration is OFF by default. The very first account (the
    # bootstrap admin) can always be created; after that, enabling this exposes
    # an account-creation endpoint to anyone who can reach the server.
    ALLOW_REGISTER: bool = False
    # Roles a self-registered account may ask for. Never destructive roles:
    # ADMIN and RADIOLOGIST are refused outright by the register endpoint.
    REGISTER_ALLOWED_ROLES: str = "PHYSICIAN"
    # Session housekeeping (auth plan §3.2): the background worker prunes
    # expired / revoked session rows every N polls (60 ticks = once per 5
    # minutes at the default 5s poll) so the sessions table stays bounded
    # with zero cost on the request path.
    SESSION_SWEEP_EVERY_TICKS: int = 60
    # Automated multi-year DB hygiene (bounded batch pruning in the worker):
    # - COMPRESSION_JOBS_RETENTION_DAYS: prune terminal DONE/FAILED rows older
    #   than N days (0 disables).
    # - AUDIT_LOG_RETENTION_DAYS: default 0 (keep forever for compliance). When
    #   > 0, rows are archived to gzipped NDJSON under BACKUP_DIR/audit_archives/
    #   and verified before deletion.
    COMPRESSION_JOBS_RETENTION_DAYS: int = Field(default=30, ge=0, le=3650)
    AUDIT_LOG_RETENTION_DAYS: int = Field(default=0, ge=0, le=36500)

    # --- Unified login / viewer gate (auth plan §2) ---
    # Production always fails startup on a viewer asset inventory mismatch.
    # Development warns by default; strict mode opts local builds into fail-closed behavior.
    VIEWER_MANIFEST_STRICT_DEVELOPMENT: bool = False
    # Optional path to an external PAXs Viewer (OHIF v3) repository or built
    # release directory (e.g. D:\Projects\NEW_Projects\Viewers-master). When
    # empty, PAXS serves the synced OHIF/PAXs Viewer bundle from app/static/viewer.
    PAXS_VIEWER_DIR: str = ""
    # HttpOnly cookie that carries the JWT after login. The /viewer and
    # /studies-ui SPAs are gated on it: unauthenticated requests are
    # 303-redirected to /login (patient share links /viewer?share=… stay
    # public). AUTH_COOKIE_SECURE must be True behind HTTPS.
    AUTH_COOKIE_NAME: str = "paxs_session"
    # The Secure/SameSite policy is declared once, with the other H06 settings
    # above (AUTH_COOKIE_SECURE / AUTH_COOKIE_SAMESITE).
    AUTH_GATE_ENABLED: bool = True
    # B-19/FS-10: CSRF protection for cookie-authenticated state changes. A
    # signed double-submit token bound to the session cookie is required on
    # POST/PUT/PATCH/DELETE (see app/core/csrf.py); bearer-token API clients are
    # exempt. On by default — disabling it re-opens the CSRF hole, so leave it
    # enabled in production.
    CSRF_PROTECTION_ENABLED: bool = True

    # --- Rate limiting (brute-force / abuse protection) ---
    RATE_LIMIT_ENABLED: bool = True
    # Bound distinct clients tracked per in-memory rate-limit window. Zero
    # disables local fallback storage and fails closed if Redis is unavailable.
    RATE_LIMIT_MAX_LOCAL_CLIENT_BUCKETS: int = Field(default=4096, ge=0)
    # H07: only these peers may set X-Forwarded-For for rate-limit identity.
    # Defaults to loopback (a local reverse proxy); anything else has its header
    # ignored, so a client cannot rotate it to bypass the cap.
    TRUSTED_PROXIES: str = "127.0.0.1,::1"
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 10
    RATE_LIMIT_GLOBAL_PER_MINUTE: int = 300

    # --- Redis (caching, distributed lock, rate limiting) ---
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_CACHE_ENABLED: bool = True
    REDIS_CONNECT_TIMEOUT_SECONDS: float = Field(default=0.5, gt=0, le=10)
    REDIS_COMMAND_TIMEOUT_SECONDS: float = Field(default=1.0, gt=0, le=30)
    REDIS_CIRCUIT_FAILURE_THRESHOLD: int = Field(default=5, ge=1, le=100)
    REDIS_CIRCUIT_RESET_SECONDS: float = Field(default=10.0, gt=0, le=3600)
    # Dedicated synchronous I/O pool. Admission covers active + queued work.
    BLOCKING_IO_MAX_THREADS: int = Field(default=16, ge=0, le=64)
    BLOCKING_IO_QUEUE_MAX: int = Field(default=64, ge=0, le=1024)

    # --- Anonymizer (research export) ---
    ANONYMIZER_STRIP_PRIVATE_TAGS: bool = True
    ANONYMIZER_STRIP_DATES: bool = True

    # --- HL7 / FHIR gateway ---
    HL7_ENABLED: bool = False
    # H-06: loopback by default — the listener can merge and rename patients.
    HL7_HOST: str = "127.0.0.1"
    HL7_PORT: int = 2575
    HL7_MAX_CONCURRENT_CONNECTIONS: int = 20
    HL7_MAX_MESSAGE_BYTES: int = 1024 * 1024
    HL7_SOCKET_TIMEOUT_SECONDS: float = 15.0
    # H-06 (fail closed): comma-separated IPs/CIDRs allowed to connect to the
    # MLLP listener. Empty = loopback peers only; anything else is dropped
    # before a byte is read.
    HL7_ALLOWED_SOURCE_IPS: str = ""
    # H-06: comma-separated "APPLICATION|FACILITY" pairs (MSH-3|MSH-4, namespace
    # ID only, case-insensitive). Empty = no sender check; a listed sender
    # list makes every other sender get an AR NAK before anything is processed.
    HL7_ALLOWED_SENDING_FACILITIES: str = ""
    # H-06: MLLP over TLS. A missing or invalid certificate stops the listener
    # (fail closed) instead of falling back to plaintext. HL7_TLS_CA, when set,
    # requires and verifies a client certificate (mutual TLS).
    HL7_TLS_ENABLED: bool = False
    HL7_TLS_CERT: str = ""
    HL7_TLS_KEY: str = ""
    HL7_TLS_CA: str = ""
    # H-06: production refuses a LAN-bound plaintext listener unless this is set
    # explicitly (many HIS interfaces cannot speak MLLP over TLS).
    HL7_PLAINTEXT_ALLOWED: bool = False
    # QBP PIX/PDQ is denied for unmapped senders. Keys are exact
    # peer-IP|MSH-3|MSH-4 tuples and values are active tenant IDs.
    HL7_SENDER_TENANT_MAP: dict[str, StrictInt] = Field(default_factory=dict)
    # HL7 ORU outbound (E2 — report write-back to the EMR). When enabled,
    # finalizing a radiology report sends an ORU^R01 over MLLP to the EMR.
    HL7_EMR_ENABLED: bool = False
    HL7_EMR_HOST: str = "127.0.0.1"
    HL7_EMR_PORT: int = 6661
    HL7_EMR_APPLICATION: str = "EMR"        # MSH-5 receiving application
    HL7_EMR_FACILITY: str = "EMR"           # MSH-6 receiving facility
    HL7_SENDING_APPLICATION: str = "PAXS"   # MSH-3
    HL7_SENDING_FACILITY: str = "PAXS_ARCHIVE"  # MSH-4
    FHIR_ENABLED: bool = True
    # H18: fusion (PET/CT, MR/CT registration) is an independent capability — it
    # used to be registered inside the FHIR branch, so disabling FHIR silently
    # removed fusion too.
    FUSION_ENABLED: bool = True

    # --- LDAP / Active Directory (plans/legacy/archive-notes/plan_9_2026.md C02) ---
    # Directory authentication is OFF until explicitly configured. When enabled,
    # LDAP_SERVER and LDAP_BASE_DN are required and *every* failure mode (bind
    # error, timeout, missing ldap3, unmapped groups) denies the login — there
    # is no built-in fallback identity.
    #
    # Directory users are mapped to PAXS roles through LDAP_GROUP_ROLE_MAP;
    # a user whose groups match nothing is denied unless LDAP_DEFAULT_ROLE names
    # one of app.core.roles.ROLES.
    LDAP_ENABLED: bool = False
    LDAP_SERVER: str = ""                 # e.g. "ldaps://dc01.hospital.org" or "ldap://dc01:389"
    LDAP_BASE_DN: str = ""                # e.g. "DC=hospital,DC=org"
    LDAP_USER_DN_TEMPLATE: str = "uid={username},{base_dn}"
    # Semicolon-separated "group=ROLE" pairs, matched case-insensitively against
    # the user's group DNs (or their CN when the DN is not returned), e.g.
    #   "CN=Radiologists=ADMIN;CN=Doctors=PHYSICIAN"
    LDAP_GROUP_ROLE_MAP: str = ""
    LDAP_DEFAULT_ROLE: str = ""           # "" = deny users with no mapped group
    LDAP_TIMEOUT_SECONDS: float = 5.0
    # LDAP simple binds are never sent over plaintext. ldap:// requires StartTLS;
    # ldaps:// uses implicit TLS regardless of LDAP_USE_TLS.
    LDAP_USE_TLS: bool = True
    # Optional PEM CA bundle for validating the directory server certificate.
    # Empty uses the platform trust store; invalid chains fail closed.
    LDAP_CA_CERT_FILE: str = ""
    # Operator-assigned tenant for this directory. When VNA tenants exist,
    # provisioning fails closed unless this names an active tenant.
    LDAP_TENANT_ID: int | None = None

    # --- Modality Worklist (MWL) ---
    # Station AE title stamped on ADT-created default worklist steps. Empty =
    # the step matches any station (visible to every modality that queries).
    MWL_DEFAULT_STATION_AET: str = ""

    # --- 2D MPR backend prep (roadmap #26, volume streaming) ---
    # Serves volume geometry metadata + raw voxel blocks to the WebGL MPR
    # frontend (GET /api/v1/volume/{series}/info and /data). VOLUME_MAX_SLICES
    # caps the number of slices emitted by one /data request after any explicit
    # Z sampling, so a misbehaving viewer can't force a multi-GB materialization.
    VOLUME_STREAMING_ENABLED: bool = True
    VOLUME_MAX_SLICES: int = 4096
    # B-10: an explicit *byte* ceiling for one /data response. VOLUME_MAX_SLICES
    # alone bounded the slice count, not the size — 4096 slices of a 512x512
    # 16-bit volume is ~2 GiB, which the handler used to join into one bytes
    # object before sending. Enforced before any buffer is allocated.
    VOLUME_MAX_BYTES: int = 256 * 1024 * 1024
    # Process-local aggregate reservation for response bytes queued or in flight.
    # A zero value disables this admission cap; per-response limits still apply.
    RESPONSE_BYTES_IN_FLIGHT_BUDGET_BYTES: int = Field(
        default=512 * 1024 * 1024, ge=0
    )
    # B-10: independent cap on one /tiles batch (PNG frames rendered and
    # concatenated in a single response). VOLUME_MAX_SLICES was the only bound,
    # and it is a raw-data budget, not a render budget.
    VOLUME_MAX_TILES: int = 64
    # Server-side MPR slice reslicing (thin-client fallback): caps the output
    # pixels of one oblique slice so a huge request can't force a big
    # in-memory resample.
    VOLUME_MPR_MAX_PIXELS: int = 4_000_000
    # Browser-side MPR guardrail. Large overview volumes are sampled before
    # allocation and marked non-primary; full-resolution Server planes remain
    # served on demand. A zero byte budget disables client overview loading.
    VOLUME_MPR_CLIENT_MAX_SLICES: int = Field(default=1500, ge=1, le=1_000_000)
    VOLUME_MPR_CLIENT_MAX_VOLUME_MB: int = Field(default=1, ge=0, le=65536)
    VOLUME_MPR_CLIENT_OVERVIEW_TARGET_SLICES: int = Field(default=86, ge=1, le=1_000_000)
    # Server MPR uses a labelled sampled plane for initial orientation when a
    # volume exceeds the client overview threshold; exact stack viewing remains
    # available through the primary DICOM stack viewer.
    VOLUME_MPR_OVERVIEW_DOWNSAMPLE: int = Field(default=32, ge=2, le=32)
    # Bounds parallel DICOM header reads while assembling a cold volume bundle.
    VOLUME_HEADER_READ_CONCURRENCY: int = Field(default=32, ge=1, le=64)
    # Concurrent cache-miss assemblies reserve an estimated peak working set.
    # Zero disables new assemblies (cache hits continue to work); excess waiters
    # or requests whose estimate cannot fit receive HTTP 503.
    VOLUME_ASSEMBLY_BUDGET_MB: int = Field(default=2048, ge=0, le=65536)
    # Caps distinct active volume builds, including builds waiting for byte budget.
    # Zero disables uncached builds while cached L1/L2 hits remain available.
    VOLUME_ASSEMBLY_MAX_INFLIGHT: int = Field(default=16, ge=0, le=1024)
    VOLUME_ASSEMBLY_MAX_WAITERS: int = Field(default=16, ge=0, le=1024)
    VOLUME_ASSEMBLY_WAIT_SECONDS: float = Field(default=30.0, ge=0, le=3600)
    # Single-flight waiters may take over only after this bounded wait count.
    VOLUME_SINGLEFLIGHT_WAIT_SECONDS: float = Field(default=180.0, ge=0, le=3600)
    VOLUME_SINGLEFLIGHT_MAX_WAITS: int = Field(default=3, ge=0, le=16)
    # Strict geometric tolerances for the standalone volume geometry validator.
    # The validator is not yet wired into volume assembly or request handlers.
    VOLUME_GEOMETRY_IOP_TOLERANCE: float = Field(default=1e-4, gt=0, le=0.1)
    VOLUME_GEOMETRY_POSITION_TOLERANCE_MM: float = Field(
        default=1e-3, gt=0, le=1.0
    )
    VOLUME_GEOMETRY_SPACING_ABS_TOLERANCE_MM: float = Field(
        default=1e-3, gt=0, le=1.0
    )
    VOLUME_GEOMETRY_SPACING_REL_TOLERANCE: float = Field(
        default=1e-3, gt=0, le=0.1
    )
    # Pure-NumPy gantry-tilt deskewing onto an orthogonal regular patient-space grid.
    # Linearly sheared gantry-tilted stacks are resampled and marked derived/resampled;
    # non-linear or erratic in-plane displacement remains fail-closed (HTTP 422).
    VOLUME_GANTRY_TILT_RESAMPLE_ENABLED: bool = True
    VOLUME_GANTRY_TILT_MAX_EXPANSION_FACTOR: float = Field(
        default=3.0, ge=1.0, le=8.0
    )

    # --- Viewer processing distribution (roadmap #51) ---
    # Controls how DICOM viewing, MPR, and 3D rendering are distributed
    # between the server and the client browser.
    #   "hybrid"  (default) — auto-detect; strong clients get WebGL, weak
    #             clients get server-rendered frames.
    #   "server"  — all decoding, reslicing and rendering on the server;
    #             only rendered PNG/JPEG frames stream to the browser
    #             (thin client — works on tablets and legacy browsers).
    #   "client"  — raw voxel data streams to the browser and the
    #             diagnostic workstation does all GPU rendering locally
    #             (thick client — requires WebGL2, sufficient VRAM).
    VIEWER_PROCESSING_MODE: str = "hybrid"
    # Hybrid-mode thresholds (plans/legacy/archive-notes/plan_viewer_process.md §7) ----------
    # Min WebGL score (0-100) for the client to qualify for client-side
    # processing.  Below this the hybrid engine routes to server mode.
    HYBRID_CLIENT_SCORE_THRESHOLD: int = Field(default=40, ge=0, le=100)
    # Max server-side volume cache budget (MB).  When the in-process
    # volume cache exceeds this, the hybrid engine shifts new series to
    # client-side processing to shed server RAM.
    HYBRID_SERVER_VOL_BUDGET_MB: int = Field(default=768, ge=0, le=65536)

    # --- Cache memory budgets (V1-2 / V3-6) ------------------------------
    # Hard byte ceilings for the in-process volume/frame caches. These are
    # deliberately INDEPENDENT of HYBRID_SERVER_VOL_BUDGET_MB: that setting is a
    # *policy* input to mode negotiation ("how much volume work does this server
    # intend to do"), whereas these are *hard memory caps* enforced by the caches
    # themselves.  Keeping them separate means a negotiation change can never
    # silently raise the process's worst-case RSS.
    #
    # VOLUME_CACHE_BUDGET_MB caps the L1 materialised-volume cache
    # (`_VOL_CACHE`, float32 HU arrays, ~500 MB for a 512x512x500 CT).  It
    # replaces the previous 8-entry cap, which was smaller than
    # HYBRID_MAX_SERVER_MPR_SESSIONS and therefore guaranteed to thrash under the
    # load it was built for.  Eviction is least-recently-used.
    #
    # Server shedding uses the lower enabled value of this hard cap and the
    # hybrid policy budget; a higher policy value is clamped to this ceiling.
    VOLUME_CACHE_BUDGET_MB: int = 1024
    # Metadata/slice-row bundle LRU budget; zero disables bundle caching.
    VOLUME_BUNDLE_CACHE_BUDGET_MB: int = Field(default=128, ge=0, le=8192)

    # Caps the decoded-frame LRU in the WADO-RS bulk service
    # (`_FRAME_CACHE`, raw decoded pixel bytes).  It replaces the previous
    # 16-entry cap: at ~0.5 MB per 512x512 CT frame that was ~8 MB, so a
    # 200-image study scrubbed across three planes re-decoded on almost every
    # revisit (a 3072x2560 J2K frame costs ~2.3 s).  Bound by bytes so it scales
    # with image size instead of assuming small frames.
    DICOM_FRAME_CACHE_MB: int = 256
    # WADO raw-frame range path (S2, plans/09: on by default). Multi-frame objects
    # with a provable layout are served by reading only the requested frame's bytes
    # instead of parsing the whole file per frame; unsupported layouts/backends keep
    # the legacy pydicom extraction path; 0 disables retained byte-offset indexes.
    DICOM_FRAME_RANGE_READ_ENABLED: bool = True
    DICOM_FRAME_OFFSET_CACHE_MB: int = Field(default=64, ge=0, le=2048)
    DICOM_FRAME_OFFSET_MAX_FRAGMENTS: int = Field(default=65536, ge=1, le=1000000)

    # Reading claim (plans/09 C1): seconds without a viewer heartbeat after which
    # a study claim is stale and another reader may take it without a takeover.
    # The viewer heartbeats every 60 s; 0 = claims never expire.
    READING_CLAIM_TTL_SECONDS: int = Field(default=300, ge=0, le=86400)

    # Hard ceiling for retained experimental AI result masks. A single mask
    # larger than this budget is returned to its caller but is never cached.
    AI_MASK_CACHE_BUDGET_MB: int = Field(default=256, ge=0, le=65536)

    # --- Prometheus /metrics access ---
    # Prometheus scrapes without credentials, but /metrics must not be public:
    # the payload reveals version, topology, queue depths and cache pressure.
    # Requests from these peers skip authentication; every other caller must
    # authenticate as ADMIN. The list accepts IPs, CIDRs, globs or explicit "*".
    #
    # Only local scrapers are trusted by default. Add an explicit address or
    # CIDR for a remote Prometheus peer; authenticated access requires ADMIN.
    METRICS_ALLOW_IPS: str = "127.0.0.1,::1"
    # Event-loop scheduling delay sample interval; zero disables the sampler.
    METRICS_EVENT_LOOP_SAMPLE_SECONDS: float = Field(default=1.0, ge=0, le=60)

    # Network RTT threshold (ms).  If the client's measured round-trip
    # time exceeds this, hybrid prefers client-side processing (even on
    # modest hardware) to avoid per-slice latency penalties.
    HYBRID_RTT_THRESHOLD_MS: int = Field(default=150, ge=0, le=600000)
    # Concurrency cap: max simultaneous server-side MPR sessions before
    # the hybrid engine forces new sessions to client mode.
    HYBRID_MAX_SERVER_MPR_SESSIONS: int = Field(default=20, ge=0, le=4096)
    # Per-user role override (JSON map).  Allows pinning specific roles
    # to a specific processing mode regardless of the global setting.
    # Example: '{"RADIOLOGIST":"client","PHYSICIAN":"server"}'
    # Empty string = no role overrides (global mode applies to all).
    VIEWER_PROCESSING_ROLE_MAP: str = ""

    # --- AI emergency triage ---
    TRIAGE_ENABLED: bool = True
    TRIAGE_RUN_IN_WORKER: bool = True
    TRIAGE_MAX_SLICES_PER_SERIES: int = 8

    # --- Study Lifecycle Management (item 14) ---
    # Auto-archive/restore/purge rule sweep interval in worker polls (every
    # N worker ticks — one sweep per minute at the default 5s poll).
    LIFECYCLE_RUN_IN_WORKER: bool = True
    LIFECYCLE_SWEEP_EVERY_TICKS: int = 12
    # When True, studies pulled from remote nodes over WADO-RS (Web Retrieve)
    # are automatically tiered to the cold `.archive/` prefix right after
    # ingest — the operator can also opt in per pull from the dashboard
    # (auto_archive checkbox) without flipping this global switch.
    LIFECYCLE_AUTO_ARCHIVE_WADO_PULL: bool = False

    # --- Storage quota reconciliation (C-17 / FD-7) ---
    # The denormalised `storage_quotas.used_bytes` counters are maintained
    # incrementally on write and on delete. A path that ever failed to
    # decrement (a crashed purge, a manual filesystem removal) would leave a
    # tenant looking fuller than it is and eventually refusing writes it should
    # accept. This sweep recomputes each tenant's total from the indexed
    # instance sizes and rewrites the counters — idempotent, so running it more
    # often than necessary costs one aggregate query, never correctness.
    QUOTA_RECONCILE_RUN_IN_WORKER: bool = True
    QUOTA_RECONCILE_SWEEP_EVERY_TICKS: int = 60
    # H-07: index/object reconciliation. Every N worker ticks, check up to
    # STORAGE_RECONCILE_BATCH_SIZE instances (keyset, resumes, wraps) and audit
    # each newly missing object as OBJECT_MISSING. Rows are never deleted.
    # 0 for either value disables the sweep.
    STORAGE_RECONCILE_SWEEP_EVERY_TICKS: int = Field(default=360, ge=0)
    STORAGE_RECONCILE_BATCH_SIZE: int = Field(default=200, ge=0)

    # --- Smart Router send queue (Phase C1) ---
    # Durable dispatch queue: matched studies are enqueued and the worker
    # C-STORE's them to the destination node with exponential backoff.
    ROUTING_QUEUE_ENABLED: bool = True
    ROUTING_RUN_IN_WORKER: bool = True
    ROUTING_SCU_AE_TITLE: str = Field(
        default="PAXS_SCU", pattern=r"^[A-Za-z0-9_.-](?:[A-Za-z0-9_. -]{0,14}[A-Za-z0-9_.-])?$",
    )
    ROUTING_TIMING: Literal["immediate", "scheduled"] = "immediate"
    ROUTING_START_TIME: str = Field(default="22:00", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    ROUTING_END_TIME: str = Field(default="06:00", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    ROUTING_MAX_ATTEMPTS: int = 5
    ROUTING_RETRY_BACKOFF_SECONDS: int = 60   # base; grows ~attempts^2
    ROUTING_SWEEP_EVERY_TICKS: int = 2

    # --- Webhooks (comma-separated URLs notified on archive events) ---
    WEBHOOK_URLS: str = ""

    # --- Lua / Python scripting (custom server scripts, roadmap #43) ---
    # Directory of .py scripts loaded at runtime (importlib). Each script may
    # define EVENTS, handle(event, ctx) and/or on_<event>(ctx); it receives a
    # ScriptContext with a logger, the event payload, and read-only PAXS
    # helpers. Scripts are trusted admin code — they run in-process.
    SCRIPTS_DIR: str = "scripts"
    SCRIPTS_ENABLED: bool = True
    # Hard bounds for the trusted admin script registry. Zero disables scripts.
    SCRIPTS_MAX_COUNT: int = 256
    SCRIPTS_MAX_SOURCE_BYTES: int = 262144
    # Chained-event budget (C-5). A script may re-dispatch an event with
    # `ctx.fire(...)`, which lets scripts chain; without a cap a chain (or a
    # loop that fires inside a handler) keeps the shared background pool
    # permanently saturated, starving warm-on-open and every other background
    # task. `SCRIPTS_MAX_CHAIN_DEPTH` bounds the chain length (0 = chaining
    # disabled: a script's `ctx.fire` is dropped), and
    # `SCRIPTS_MAX_CHAIN_PER_SECOND` bounds how many chained dispatches may be
    # accepted process-wide in any one-second window (0 disables chaining).
    SCRIPTS_MAX_CHAIN_DEPTH: int = 5
    SCRIPTS_MAX_CHAIN_PER_SECOND: int = 50

    # --- Background task pool (C-4 / C-5) ---
    # One shared, bounded ThreadPoolExecutor carries every background task
    # (warm-on-open, script event dispatch). Before this existed, each study
    # opened and each script event spawned a raw daemon thread, so a burst could
    # exhaust OS threads (AGENTS.md §1 N-5).
    #   BACKGROUND_MAX_THREADS — worker count; 0 disables background dispatch
    #     entirely (honoured literally, never replaced by a default).
    #   BACKGROUND_QUEUE_MAX   — backlog cap (queued, not yet running); past it a
    #     new task is dropped/debounced rather than queued without bound.
    BACKGROUND_MAX_THREADS: int = 8
    BACKGROUND_QUEUE_MAX: int = 64

    # --- Disaster Recovery replication (roadmap #49) ---
    # Duplicates study objects to an off-site S3/Azure-Blob-compatible target.
    # DR_REPLICATION_TARGET names a registered storage backend (Admin →
    # Storage, kind=s3); when empty the env S3 config (S3_* settings) is used.
    DR_REPLICATION_ENABLED: bool = False
    DR_REPLICATION_TARGET: str = ""
    DR_REPLICATION_PREFIX: str = "replica/"
    DR_REPLICATION_MAX_ATTEMPTS: int = 5
    DR_REPLICATION_SWEEP_EVERY_TICKS: int = 4

    # --- DICOMweb / API ---
    # Anonymous reads (QIDO/WADO) and writes (STOW) are separate capabilities.
    # Public reads remain opt-in; public writes are independently opt-in and
    # default deny so enabling viewer access never opens archive ingestion.
    DICOMWEB_PUBLIC_READ: bool = False
    DICOMWEB_PUBLIC_WRITE: bool = False
    # P2.0: expose PHI-free per-phase WADO-RS frame timings for diagnostics.
    DICOMWEB_SERVER_TIMING_ENABLED: bool = False
    CORS_ORIGINS: str = "*"

    # --- Remote-node DICOMweb client (QIDO-RS / WADO-RS) ---
    # SSRF guard: the client refuses to connect to loopback / link-local /
    # private ranges / cloud-metadata IPs by default. Flip this to True only
    # for trusted local test peers (integration tests connect to 127.0.0.1).
    DICOMWEB_CLIENT_ALLOW_PRIVATE: bool = False
    DICOMWEB_CLIENT_TIMEOUT: float = 15.0
    DICOMWEB_QIDO_MAX_BYTES: int = 16 * 1024 * 1024  # 16 MiB response ceiling
    DICOMWEB_STOW_MAX_RESPONSE_BYTES: int = 1024 * 1024  # 1 MiB response ceiling
    DICOMWEB_CLIENT_MAX_BYTES: int = 4 * 1024 * 1024 * 1024  # 4 GiB per retrieve
    # B-14/FP-11: a WADO-RS retrieve is streamed to a temp file and the byte
    # count is enforced *while the bytes arrive* — the response body is never
    # materialised in RAM. MAX_WADO_INSTANCE_BYTES is the per-object ceiling: a
    # peer whose Content-Length already exceeds it is refused with HTTP 413
    # before a single body byte is read, and one that omits Content-Length and
    # streams past it is cut off mid-transfer. WADO_STREAM_CHUNK_BYTES is the
    # read granularity (the most that is ever resident in memory).
    MAX_WADO_INSTANCE_BYTES: int = 512 * 1024 * 1024        # 512 MiB per instance
    WADO_STREAM_CHUNK_BYTES: int = 64 * 1024                # 64 KiB read chunks

    # --- Incoming STOW-RS upload caps ---
    # The STOW handler streams the multipart body part-by-part, spooling each
    # part to a temp file on disk (never buffering it in RAM, C-10). These caps
    # bound the worst case: a single part larger than STOW_MAX_PART_BYTES, or a
    # total body larger than STOW_MAX_TOTAL_BYTES (0 = unlimited), is rejected
    # with 413. MAX_INSTANCE_UPLOAD_BYTES is the hard per-instance ceiling the
    # streaming ingest enforces *as the bytes arrive*, aborting an oversized part
    # before it completes.
    MAX_INSTANCE_UPLOAD_BYTES: int = 512 * 1024 * 1024      # 512 MiB per instance
    STOW_MAX_PART_BYTES: int = 2 * 1024 * 1024 * 1024   # 2 GiB per part
    STOW_MAX_TOTAL_BYTES: int = 4 * 1024 * 1024 * 1024  # 4 GiB total; 0 disables cap
    # H-01 / N-5: STOW promotions (an S3 upload of up to STOW_MAX_PART_BYTES)
    # run in worker threads; at most this many at once per process. 0 disables.
    STOW_MAX_CONCURRENT_PROMOTIONS: int = 4

    # XDS registration is JSON (base64 or text), so cap both decoded documents
    # and the serialized HTTP body before FastAPI parses it.
    XDS_MAX_DOCUMENT_BYTES: int = 16 * 1024 * 1024  # 16 MiB; 0 disables decoded cap
    XDS_MAX_REQUEST_BYTES: int = 22 * 1024 * 1024  # 22 MiB; 0 disables body cap

    # --- Transfer Accelerator (item 19) ---
    # HTTP-based fast transfer bypassing DIMSE C-STORE: a study's instances
    # are split into batches and pushed to a peer's DICOMweb service over N
    # parallel STOW-RS connections with chunked transfer encoding. Only used
    # when the caller opts in (?accelerated=1 on the web upload endpoint).
    TRANSFER_ACCELERATOR_ENABLED: bool = True
    TRANSFER_ACCELERATOR_WORKERS: int = 4      # concurrent HTTP connections
    TRANSFER_ACCELERATOR_BATCH_SIZE: int = 16  # instances per multipart batch
    # 0 disables remote STOW uploads; otherwise bounds active upload workflows.
    NODE_TRANSFER_STOW_MAX_CONCURRENT: int = Field(default=4, ge=0, le=64)
    # Public base URL used in patient share links/QR codes (behind reverse proxy).
    # Leave EMPTY to derive it from an approved request host, or set it explicitly
    # to the origin a browser can reach, e.g. https://pacs.example.org.
    # A bind address (0.0.0.0 / ::) is rejected and normalised to the safe host.
    PUBLIC_BASE_URL: str = ""
    # Host authority (name or name:port, no scheme) used when request headers
    # are absent or fail ALLOWED_HOSTS validation.
    SERVER_HOST: str = "localhost:8000"

    # --- Patient sharing ---
    SHARE_EXPIRY_DAYS: int = 30
    # Accept legacy /viewer?share= links by redirecting to the exchange route.
    SHARE_LEGACY_VIEWER_LINKS_ENABLED: bool = True

    # --- PaxsViewer Control Panel web bridge (/admin) ---
    # The control-panel UI (served at /admin) manages the Viewers/Orthanc
    # stack. Its per-service ports/paths/interval are persisted here (JSON);
    # empty = a file named admin_panel.json beside STORAGE_ROOT.
    ADMIN_PANEL_ENABLED: bool = True
    ADMIN_PANEL_CONFIG_FILE: str = ""

    # --- I/O thread pool (WADO-RS frame/rendered/thumbnail retrieval) ---
    # Disk reads + pydicom decode for the DICOMweb hot paths run via
    # asyncio.to_thread(), which uses this executor. asyncio's own default
    # (min(32, cpu+4)) is too small for a CT/MRI viewer that opens dozens of
    # concurrent frame requests per series; size it well above core count so
    # I/O-bound reads aren't serialized behind a handful of worker threads.
    IO_THREAD_POOL_WORKERS: int = 64

    # --- Warm-on-open (viewer opens a study -> background cache warm) ---
    # When a study is opened in the viewer (QIDO with a specific
    # StudyInstanceUID, or a WADO-RS study-metadata fetch), a background
    # thread pre-generates any missing 192px thumbnails, warms the Redis
    # thumbnail keys, and pre-decodes the first frames into the in-process
    # decoded-frame cache — so scrolling the study and re-opening it later
    # hit warm caches instead of cold disk reads + decodes. WARM_MAX_INSTANCES
    # bounds how many instances one warm pass touches (thumbnails);
    # WARM_MAX_FRAMES bounds the number of frames pre-decoded per warm pass.
    WARM_ON_OPEN_ENABLED: bool = True
    WARM_MAX_INSTANCES: int = 16
    WARM_MAX_FRAMES: int = 4

    # --- Licensing & Online Activation (Phase 3 & 4) ---
    LICENSE_FILE_PATH: str = "./paxs.lic"
    LICENSE_PORTAL_URL: str = "http://127.0.0.1:8080"
    LICENSE_HEARTBEAT_HOURS: int = 24
    LICENSE_GRACE_PERIOD_DAYS: int = 7
    LICENSE_AUTO_VALIDATE_ONLINE: bool = True
    # B-2 / FS-7: the AES-256-GCM key that wraps .lic containers. There is NO
    # default: the previous hardcoded salt ("PAXS_ARCHIVE_LIC_SEC_2026_AES25") is
    # public in this repository's history and mirrored in the PHP portal, so a
    # container wrapped with it is effectively plaintext. Supply 32 bytes (or any
    # length — it is SHA-256-derived) from the environment or a secrets manager.
    # An empty value is a hard error the moment a container is encrypted or
    # decrypted (app/core/licensing/crypto.py, LicenseConfigurationError).
    PAXS_LICENSE_AES_KEY: str = ""
    # The vendor verification key. Leave empty to use the key embedded in
    # app/core/licensing/crypto.py; set it (PEM, or a path via
    # PAXS_LICENSE_PUBLIC_KEY_PATH) to rotate without a redeploy. A *revoked* key
    # is refused even when configured explicitly.
    PAXS_LICENSE_PUBLIC_KEY: str = ""
    PAXS_LICENSE_PUBLIC_KEY_PATH: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_assignment=True,
    )

    @field_validator("DATABASE_URL", "DB_READ_REPLICA_URL")
    @classmethod
    def _require_postgresql(cls, value: str) -> str:
        """PostgreSQL only: refuse retired MySQL/SQLite URLs instead of half-working."""
        if not value:
            return value
        if value.startswith(("postgresql+asyncpg://", "postgresql://", "postgres://")):
            for prefix in ("postgresql://", "postgres://"):
                if value.startswith(prefix):
                    return "postgresql+asyncpg://" + value[len(prefix):]
            return value
        scheme = value.split("://", 1)[0]
        raise ValueError(
            f"unsupported database URL scheme {scheme!r}: PAXS Archive runs on PostgreSQL only "
            "(postgresql+asyncpg://user:password@host:5432/dbname)"
        )

    @field_validator("PUBLIC_BASE_URL")
    @classmethod
    def _normalise_public_base_url(cls, value: str) -> str:
        """Guarantee PUBLIC_BASE_URL is either usable or empty (V2-2).

        Two failure modes are neutralised here:

        * a **bind address** (``http://0.0.0.0``, ``http://[::]:8000``) — the old
          shipped default — which silently produced share links and QR codes no
          browser could open;
        * a trailing slash, which would yield ``//viewer`` when concatenated.

        An unroutable value is downgraded to ``""`` (= "not configured") rather
        than guessed at, so :func:`resolve_public_base_url` can prefer the
        request's own Host and only then fall back to loopback.
        """
        text = (value or "").strip().rstrip("/")
        if not is_routable_public_base_url(text):
            return ""
        return text

    @field_validator("HL7_ALLOWED_SOURCE_IPS")
    @classmethod
    def _validate_hl7_allowed_source_ips(cls, value: str) -> str:
        """Refuse to start with an unparseable allowlist entry (N-8)."""
        import ipaddress

        for entry in (part.strip() for part in (value or "").split(",")):
            if not entry:
                continue
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"HL7_ALLOWED_SOURCE_IPS entry {entry!r} is not an IP or CIDR"
                ) from exc
        return value

    @field_validator("HL7_ALLOWED_SENDING_FACILITIES")
    @classmethod
    def _validate_hl7_allowed_sending_facilities(cls, value: str) -> str:
        """Each entry must name both MSH-3 and MSH-4 ("APP|FACILITY") (N-8)."""
        for entry in (part.strip() for part in (value or "").split(",")):
            if not entry:
                continue
            app, sep, facility = entry.partition("|")
            if not sep or not app.strip() or not facility.strip():
                raise ValueError(
                    f"HL7_ALLOWED_SENDING_FACILITIES entry {entry!r} must be APPLICATION|FACILITY"
                )
        return value

    @field_validator("HL7_SENDER_TENANT_MAP")
    @classmethod
    def _validate_hl7_sender_tenant_map(
        cls, value: dict[str, StrictInt]
    ) -> dict[str, StrictInt]:
        """Require exact peer-IP/MSH sender tuples and positive tenant IDs."""
        for sender, tenant_id in value.items():
            parts = sender.split("|", 2)
            if (
                len(parts) != 3
                or not all(part and part == part.strip() for part in parts)
                or any(char in sender for char in "\r\n")
            ):
                raise ValueError("HL7 sender keys must be peer-IP|MSH-3|MSH-4 tuples")
            try:
                ip_address(parts[0])
            except ValueError as exc:
                raise ValueError("HL7 sender key must begin with an exact IP address") from exc
            if tenant_id <= 0:
                raise ValueError("HL7 sender tenant IDs must be positive")
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def resolve_public_base_url(
    request=None, settings_obj: "Settings | None" = None
) -> str:
    """The absolute app base URL to publish in share links, QR codes and FHIR URLs.

    Resolution order (V2-2):

    1. an explicitly configured ``PUBLIC_BASE_URL`` — always wins, so a
       deployment behind a proxy keeps full control;
    2. ``X-Forwarded-Host`` from a peer in ``FORWARDED_ALLOW_IPS``, if the host
       is also approved by ``ALLOWED_HOSTS``;
    3. the request's own ``Host`` header, if approved by ``ALLOWED_HOSTS``;
    4. the configured ``SERVER_HOST``; then the loopback constant as a last resort.

    Request headers never supply an unchecked URL authority, and bind addresses
    are rejected so the generated links remain usable.

    The **path prefix is always appended** (V2-4): when the app is mounted under
    a sub-path — either statically via ``ROOT_PATH`` or per-request via
    ``X-Forwarded-Prefix``, which the middleware publishes as
    ``scope["root_path"]`` — links must include it or they point outside the app.
    """
    cfg = settings_obj if settings_obj is not None else get_settings()
    configured = (getattr(cfg, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    suffix = _request_prefix(request, cfg)

    if is_routable_public_base_url(configured):
        # An explicit PUBLIC_BASE_URL may already carry the prefix; do not
        # duplicate it (https://host/paxs + /paxs would be wrong).
        if suffix and not configured.endswith(suffix):
            return configured + suffix
        return configured

    from app.core.proxy_prefix import TrustedHosts, validated_host_authority

    allowed_hosts = getattr(cfg, "ALLOWED_HOSTS", "") or ""
    authority = None
    trusted_forwarded_host = False
    headers = getattr(request, "headers", None) or {} if request is not None else {}
    if request is not None:
        scope = getattr(request, "scope", None) or {}
        client = scope.get("client")
        peer = client[0] if client else None
        forwarded_is_trusted = bool(
            peer and peer in TrustedHosts(getattr(cfg, "FORWARDED_ALLOW_IPS", ""))
        )
        if forwarded_is_trusted:
            authority = validated_host_authority(
                headers.get("x-forwarded-host"), allowed_hosts
            )
            trusted_forwarded_host = authority is not None
        if authority is None:
            authority = validated_host_authority(headers.get("host"), allowed_hosts)

    if authority is None:
        authority = validated_host_authority(
            getattr(cfg, "SERVER_HOST", "") or "", None
        )
    if authority is None:
        return DEFAULT_PUBLIC_BASE_URL + suffix

    proto = "http"
    if request is not None:
        if trusted_forwarded_host:
            proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto not in ("http", "https"):
            proto = getattr(getattr(request, "url", None), "scheme", "") or "http"
        if proto not in ("http", "https"):
            proto = "http"
    return f"{proto}://{authority}{suffix}"


def _request_prefix(request, settings_obj: "Settings") -> str:
    """The sub-path the app is mounted under: request scope first, then ROOT_PATH."""
    from app.core.proxy_prefix import normalise_prefix

    if request is not None:
        scope = getattr(request, "scope", None) or {}
        prefix = normalise_prefix(scope.get("root_path") or "")
        if prefix:
            return prefix
    return normalise_prefix(getattr(settings_obj, "ROOT_PATH", "") or "")


INSECURE_SECRET_KEYS = {
    "paxs-insecure-dev-key-change-me",
    "change-me-to-a-long-random-string",
    "change-me",
    "secret",
}

#: Weak values that must never become an administrator password. Bootstrap
#: (db/init seeding and the API setup path) refuses these outright — the old
#: shipped default ``admin123`` is the first entry, kept here so it can never
#: come back.
INSECURE_PASSWORDS = {
    "admin123", "admin", "password", "passw0rd", "changeme", "change-me",
    "paxs", "paxsadmin", "secret", "123456", "12345678", "qwerty",
}

#: Minimum length for an administrator password created by automation.
MIN_ADMIN_PASSWORD_LENGTH = 12

#: Minimum length for SETUP_TOKEN when one is configured.
MIN_SETUP_TOKEN_LENGTH = 16


class InsecureConfigurationError(RuntimeError):
    """The configuration is not safe for production (B-1).

    Raised at startup so the process refuses to serve traffic rather than
    running with a known JWT secret, an open CORS policy or an unsecured
    session cookie. Subclasses :class:`RuntimeError` so existing handlers and
    tests that expect ``RuntimeError`` keep working; ``run_paxs.py`` converts it
    into a clean ``SystemExit`` for the CLI.
    """


# H06: environments that are explicitly NOT production. Anything else - empty,
# unknown or misspelled - is treated as production so a typo fails closed
# instead of silently switching every check below off.
NON_PRODUCTION_ENVIRONMENTS = {"development", "dev", "local", "test", "testing"}


def is_production_environment(settings_obj: "Settings") -> bool:
    """True when the configuration must satisfy the production gates (H06).

    Production is signalled by ``ENVIRONMENT=production`` **or** by a deployment
    that switched ``DEBUG`` off explicitly. The shipped defaults leave both
    values alone (``ENVIRONMENT=development``), so a plain local run and
    ``run_all.bat`` keep working, while a staging/production deployment that
    only pins ``DEBUG=False`` is still held to the production rules.

    Single source of truth: ``app/main/startup.py`` (startup gate) and ``run_paxs.py``
    (TLS + auto-reload) both ask this function.
    """
    value = (getattr(settings_obj, "ENVIRONMENT", "") or "").strip().lower()
    if value in NON_PRODUCTION_ENVIRONMENTS:
        pinned_off = "DEBUG" in getattr(settings_obj, "model_fields_set", frozenset())
        return bool(pinned_off and not getattr(settings_obj, "DEBUG", False))
    return True


def _is_loopback_host(host: str) -> bool:
    """True for a bind address only the local machine can reach."""
    import ipaddress

    text = str(host or "").strip().lower()
    if text in ("localhost", ""):
        return text == "localhost"
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _hl7_production_problems(settings_obj: "Settings") -> list[str]:
    """H-06: a LAN-reachable HL7 listener can merge and rename patients."""
    if not getattr(settings_obj, "HL7_ENABLED", False):
        return []
    if _is_loopback_host(getattr(settings_obj, "HL7_HOST", "")):
        return []
    problems = []
    if not str(getattr(settings_obj, "HL7_ALLOWED_SOURCE_IPS", "") or "").strip():
        problems.append(
            "HL7_ALLOWED_SOURCE_IPS must list the HIS/RIS peers when HL7_HOST is not loopback"
        )
    if not getattr(settings_obj, "HL7_TLS_ENABLED", False) and not getattr(
        settings_obj, "HL7_PLAINTEXT_ALLOWED", False
    ):
        problems.append(
            "HL7_TLS_ENABLED must be true for a LAN-bound HL7 listener "
            "(or set HL7_PLAINTEXT_ALLOWED=true to accept plaintext MLLP explicitly)"
        )
    return problems


def validate_production_settings(settings_obj: "Settings") -> None:
    """Refuse to run an insecure configuration in production (H06 / B-1).

    Called at startup so a production deployment fails closed rather than
    serving traffic with a known JWT secret, an insecure admin cookie, or a
    wildcard CORS policy. Raises :class:`InsecureConfigurationError`.
    """
    if not is_production_environment(settings_obj):
        return

    problems: list[str] = []
    secret = getattr(settings_obj, "SECRET_KEY", "") or ""
    if secret in INSECURE_SECRET_KEYS or len(secret) < 32:
        problems.append(
            "SECRET_KEY is the shipped default or shorter than 32 characters - "
            "set a long random value"
        )
    if not getattr(settings_obj, "AUTH_COOKIE_SECURE", False):
        problems.append("AUTH_COOKIE_SECURE must be true in production")
    # H06: the SameSite attribute is part of the session-cookie policy, so an
    # empty or misspelled value (which browsers silently drop) is a startup
    # error rather than a silent downgrade.
    samesite = str(getattr(settings_obj, "AUTH_COOKIE_SAMESITE", "") or "").strip().lower()
    if samesite not in ("lax", "strict", "none"):
        problems.append(
            "AUTH_COOKIE_SAMESITE must be one of 'lax', 'strict' or 'none' "
            f"(got {samesite or 'empty'!r})"
        )
    elif samesite == "none" and not getattr(settings_obj, "AUTH_COOKIE_SECURE", False):
        # Browsers reject SameSite=None without Secure, so the pair must match.
        problems.append("AUTH_COOKIE_SAMESITE='none' requires AUTH_COOKIE_SECURE=true")
    if getattr(settings_obj, "DEBUG", False):
        problems.append("DEBUG must be false in production")
    if not getattr(settings_obj, "RATE_LIMIT_ENABLED", True):
        problems.append("RATE_LIMIT_ENABLED must stay true in production")

    # B-1: a wildcard CORS policy lets any origin drive the API with the
    # browser's credentials; production must name its origins explicitly.
    try:
        origins = list(getattr(settings_obj, "cors_origin_list", []) or [])
    except (ValueError, TypeError, AttributeError):  # a malformed value must not mask the gate
        origins = []
    if not origins:
        problems.append(
            "CORS_ORIGINS must list the browser origins allowed to call the API "
            "(it is empty)"
        )
    elif "*" in origins:
        problems.append(
            "CORS_ORIGINS must not be '*' in production - list the allowed origins"
        )

    problems.extend(_hl7_production_problems(settings_obj))

    # B-4: a configured setup token is a credential, so a short one is refused.
    token = str(getattr(settings_obj, "SETUP_TOKEN", "") or "")
    if token and len(token) < MIN_SETUP_TOKEN_LENGTH:
        problems.append(
            f"SETUP_TOKEN must be at least {MIN_SETUP_TOKEN_LENGTH} characters "
            "(or unset, which disables API bootstrap)"
        )

    if problems:
        raise InsecureConfigurationError(
            "Refusing to start in production with an insecure configuration:\n  - "
            + "\n  - ".join(problems)
        )


def bootstrap_enabled(settings_obj: "Settings | None" = None) -> bool:
    """Whether the first administrator may be created over the API (B-4).

    Bootstrap requires an explicitly configured ``SETUP_TOKEN``. With the token
    unset — the shipped default — the answer is **no**, so an empty database
    cannot be claimed by the first caller to reach the port.
    """
    obj = settings_obj if settings_obj is not None else settings
    return bool(str(getattr(obj, "SETUP_TOKEN", "") or "").strip())


def setup_token_matches(candidate: str | None, settings_obj: "Settings | None" = None) -> bool:
    """Constant-time comparison of a caller-supplied setup token (B-4).

    Always returns ``False`` when no token is configured, so a missing
    configuration denies rather than accidentally accepting an empty string.
    """
    obj = settings_obj if settings_obj is not None else settings
    expected = str(getattr(obj, "SETUP_TOKEN", "") or "")
    if not expected:
        return False
    return compare_digest(str(candidate or ""), expected)


def is_weak_admin_password(password: str | None) -> bool:
    """Whether ``password`` is unacceptable for an administrator (B-4).

    Rejects the known-weak list (including the retired ``admin123`` default),
    anything shorter than :data:`MIN_ADMIN_PASSWORD_LENGTH`, and single-character
    repetitions — the values automation tends to reach for.
    """
    value = str(password or "")
    if len(value) < MIN_ADMIN_PASSWORD_LENGTH:
        return True
    if value.strip().lower() in INSECURE_PASSWORDS:
        return True
    return len(set(value)) == 1


def validate_memory_budgets(settings_obj: "Settings | None" = None) -> list[str]:
    """Coherence warnings for the cache memory budgets (V1-2 / V3-6).

    Returns a list of human-readable warnings — empty means coherent.  These are
    **warnings, not startup failures**: an operator may deliberately run the L1
    cache above the negotiation budget, and refusing to boot over a tuning choice
    would be worse than telling them about it.

    Only *unambiguous* misconfigurations are reported.  An earlier revision also
    warned when the byte budget could not hold one reference volume (~500 MB) per
    admitted server session — but that fires on the shipped defaults
    (1024 MB vs HYBRID_MAX_SERVER_MPR_SESSIONS=20) for what is a legitimate
    design: the byte budget is the authoritative memory constraint, and the
    session cap is an admission limit the cache need not fully serve.  A warning
    that is wrong for most real workloads trains people to ignore it, so the
    volume-size heuristic was removed.  Whether the budget is actually under
    pressure is now *measured*: watch ``paxs_cache_utilisation_ratio`` and the
    miss rate on ``/metrics``.
    """
    s = settings_obj if settings_obj is not None else get_settings()
    vol = int(getattr(s, "VOLUME_CACHE_BUDGET_MB", 0) or 0)
    hybrid = int(getattr(s, "HYBRID_SERVER_VOL_BUDGET_MB", 0) or 0)
    frame = int(getattr(s, "DICOM_FRAME_CACHE_MB", 0) or 0)

    warnings: list[str] = []
    if vol < 0:
        warnings.append("VOLUME_CACHE_BUDGET_MB must be >= 0 (0 disables the volume cache).")
    if frame < 0:
        warnings.append("DICOM_FRAME_CACHE_MB must be >= 0 (0 disables the frame cache).")
    if vol and hybrid and hybrid > vol:
        warnings.append(
            f"HYBRID_SERVER_VOL_BUDGET_MB ({hybrid}) exceeds VOLUME_CACHE_BUDGET_MB "
            f"({vol}); the effective server-shedding threshold is clamped to {vol} MB."
        )
    return warnings


settings = get_settings()
