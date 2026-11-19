"""Application configuration (pydantic-settings, .env aware)."""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The origin used in share links / QR codes / FHIR URLs when PUBLIC_BASE_URL is
# left unset. Loopback is reachable at least by the operator running the server,
# which is strictly better than the old ``http://0.0.0.0`` default — a *listen*
# address that no browser can ever resolve (V2-2 in PAXS_VIEWER_DIAGNOSTIC_AUDIT.md).
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

    # --- Database ---
    # MySQL 8 is the default (full performance indexing, utf8mb4, foreign-key
    # enforcement — built for large archives). Adjust user/password/database
    # to your MySQL, or override via .env (see .env.example). SQLite remains
    # selectable for zero-setup local/dev runs:
    #   DATABASE_URL="sqlite+aiosqlite:///./paxs.db"
    DATABASE_URL: str = "mysql+aiomysql://paxs_user:paxs@127.0.0.1:3306/paxs_db?charset=utf8mb4"

    # --- DICOM DIMSE receiver ---
    DICOM_AE_TITLE: str = "PAXS_SCP"
    DICOM_HOST: str = "0.0.0.0"
    DICOM_PORT: int = 10400
    DICOM_RECEIVER_ENABLED: bool = True
    DICOM_MAX_ASSOCIATIONS: int = 16

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
    DICOM_TLS_CA: str = ""

    # --- Web HTTPS (dashboard / DICOMweb) ---
    # When enabled the uvicorn server listens on HTTPS using the cert/key
    # below. A self-signed pair is auto-generated on first boot (reuses the
    # DICOM TLS cert infrastructure). Disable for plain HTTP behind a
    # reverse proxy that terminates TLS.
    HTTPS_ENABLED: bool = False
    HTTPS_CERT: str = "./certs/https.crt"
    HTTPS_KEY: str = "./certs/https.key"
    HTTPS_PORT: int = 8443

    # --- Storage ---
    STORAGE_ROOT: str = "./storage"
    # Object-storage backend: "local" (filesystem under STORAGE_ROOT) or "s3"
    # (S3-compatible — AWS S3 / MinIO). S3 needs boto3 installed.
    STORAGE_BACKEND: str = "local"
    S3_ENDPOINT_URL: str = ""       # e.g. http://localhost:9000 for MinIO (empty = AWS)
    S3_REGION: str = "us-east-1"
    S3_BUCKET: str = "paxs"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_PRESIGN_EXPIRES: int = 900    # seconds
    # Legacy storage resolution (plan_9_2026.md C05). OFF by default: storage
    # keys must be relative and must stay inside STORAGE_ROOT. When ON, pre-B1
    # rows holding absolute paths, or paths relative to the process CWD
    # ("storage/1.2.3/..."), still resolve — a compatibility escape hatch for
    # unmigrated archives only, never for ingest. Enable, migrate, turn back off.
    STORAGE_ALLOW_LEGACY_PATHS: bool = False
    # Explicit read-only roots for legacy objects outside the configured storage root.
    STORAGE_LEGACY_ROOTS: str = ""

    # --- Database administration (Settings > Database tab) ---
    # Where a saved MySQL/SQLite configuration is persisted (rewritten as
    # DATABASE_URL on "Save configuration") and where backup dumps go.
    ENV_FILE_PATH: str = ".env"
    BACKUP_DIR: str = "./backups"

    # --- Connection pool tuning (MySQL) ---
    # The async engine deliberately uses NullPool (one connection per request
    # — never reuse a connection across event loops); the sync engine (DICOM
    # receiver / worker threads) uses a bounded QueuePool sized by these.
    # pool_pre_ping drops stale connections so long-lived threads never fail
    # against a restarted MySQL, and pool_recycle rotates them before MySQL's
    # wait_timeout can kill them.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE_SECONDS: int = 1800
    DB_POOL_PRE_PING: bool = True
    # Optional MySQL read replica (Admin → DB Pool). When set, the panel shows
    # its live status (lag, connected) and an admin toggle can route the two
    # hottest read endpoints (/stats, /studies) through it, falling back to the
    # primary automatically if the replica is unreachable.
    DB_READ_REPLICA_URL: str = ""
    # Slow-query log default threshold (Admin → DB Pool). Statements that ran
    # at or above this many milliseconds land in the in-memory slow-query ring.
    DB_SLOW_QUERY_THRESHOLD_MS: int = 500

    # --- SQLite tuning ---
    # "delete" (default, classic rollback journal) or "wal".
    #
    # Default is the rollback journal: on Windows + aiosqlite, WAL readers can
    # miss a just-committed write (stale snapshots across pooled connections),
    # breaking read-your-writes consistency for the API. The rollback journal
    # is serialized by busy_timeout and is always consistent. WAL remains
    # selectable for Linux deployments that want concurrent-reader throughput
    # (or for network filesystems, use "delete" — WAL misbehaves there too).
    SQLITE_JOURNAL_MODE: str = "delete"

    # --- Optical media ---
    DISC_BURN_ENABLED: bool = False
    DISC_BURN_DRIVE: str = Field(default="", pattern=r"^([A-Za-z]:)?$")
    DISC_BURN_VOLUME_LABEL: str = Field(default="PAXS_DICOM", pattern=r"^[A-Z0-9_]{1,16}$")

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

    # --- /stats storage-usage cache (scalability) ---
    # storage_usage_bytes() walks the storage tree (O(objects)); the dashboard
    # /stats endpoint uses a TTL cache so a big archive doesn't pay that walk
    # on every page load. 0 disables the cache.
    STATS_USAGE_CACHE_SECONDS: int = 60

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
    # H06: the shipped default is deliberately recognisable so production can
    # refuse to start with it (see validate_production_settings).
    SECRET_KEY: str = "paxs-insecure-dev-key-change-me"
    # "development" | "production". Production turns on fail-closed checks.
    ENVIRONMENT: str = "development"
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

    # --- Unified login / viewer gate (auth plan §2) ---
    # HttpOnly cookie that carries the JWT after login. The /viewer and
    # /studies-ui SPAs are gated on it: unauthenticated requests are
    # 303-redirected to /login (patient share links /viewer?share=… stay
    # public). AUTH_COOKIE_SECURE must be True behind HTTPS.
    AUTH_COOKIE_NAME: str = "paxs_session"
    # The Secure/SameSite policy is declared once, with the other H06 settings
    # above (AUTH_COOKIE_SECURE / AUTH_COOKIE_SAMESITE).
    AUTH_GATE_ENABLED: bool = True

    # --- Rate limiting (brute-force / abuse protection) ---
    RATE_LIMIT_ENABLED: bool = True
    # H07: only these peers may set X-Forwarded-For for rate-limit identity.
    # Defaults to loopback (a local reverse proxy); anything else has its header
    # ignored, so a client cannot rotate it to bypass the cap.
    TRUSTED_PROXIES: str = "127.0.0.1,::1"
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 10
    RATE_LIMIT_GLOBAL_PER_MINUTE: int = 300

    # --- Redis (caching, distributed lock, rate limiting) ---
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_CACHE_ENABLED: bool = True

    # --- Anonymizer (research export) ---
    ANONYMIZER_STRIP_PRIVATE_TAGS: bool = True
    ANONYMIZER_STRIP_DATES: bool = True

    # --- HL7 / FHIR gateway ---
    HL7_ENABLED: bool = False
    HL7_HOST: str = "0.0.0.0"
    HL7_PORT: int = 2575
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

    # --- LDAP / Active Directory (plan_9_2026.md C02) ---
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
    LDAP_USE_TLS: bool = False            # startTLS on ldap:// (ldaps:// implies TLS)

    # --- Modality Worklist (MWL) ---
    # Station AE title stamped on ADT-created default worklist steps. Empty =
    # the step matches any station (visible to every modality that queries).
    MWL_DEFAULT_STATION_AET: str = ""

    # --- 2D MPR backend prep (roadmap #26, volume streaming) ---
    # Serves volume geometry metadata + raw voxel blocks to the WebGL MPR
    # frontend (GET /api/v1/volume/{series}/info and /data). VOLUME_MAX_SLICES
    # caps a single /data request so a misbehaving viewer can't force a
    # multi-GB materialization.
    VOLUME_STREAMING_ENABLED: bool = True
    VOLUME_MAX_SLICES: int = 4096
    # Server-side MPR slice reslicing (thin-client fallback): caps the output
    # pixels of one oblique slice so a huge request can't force a big
    # in-memory resample.
    VOLUME_MPR_MAX_PIXELS: int = 4_000_000

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
    # Hybrid-mode thresholds (plan_viewer_process.md §7) ----------
    # Min WebGL score (0-100) for the client to qualify for client-side
    # processing.  Below this the hybrid engine routes to server mode.
    HYBRID_CLIENT_SCORE_THRESHOLD: int = 40
    # Max server-side volume cache budget (MB).  When the in-process
    # volume cache exceeds this, the hybrid engine shifts new series to
    # client-side processing to shed server RAM.
    HYBRID_SERVER_VOL_BUDGET_MB: int = 4096

    # --- Cache memory budgets (V1-2 / V3-6) ------------------------------
    # Hard byte ceilings for the two in-process decode caches.  These are
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
    # Keep this BELOW HYBRID_SERVER_VOL_BUDGET_MB unless you have raised that
    # too — see validate_memory_budgets() for the coherence check.
    VOLUME_CACHE_BUDGET_MB: int = 1024

    # Caps the decoded-frame LRU in the WADO-RS bulk service
    # (`_FRAME_CACHE`, raw decoded pixel bytes).  It replaces the previous
    # 16-entry cap: at ~0.5 MB per 512x512 CT frame that was ~8 MB, so a
    # 200-image study scrubbed across three planes re-decoded on almost every
    # revisit (a 3072x2560 J2K frame costs ~2.3 s).  Bound by bytes so it scales
    # with image size instead of assuming small frames.
    DICOM_FRAME_CACHE_MB: int = 256

    # --- Prometheus /metrics access ---
    # Prometheus scrapes without credentials, but /metrics must not be public:
    # the payload reveals version, topology, queue depths and cache pressure.
    # Requests from these peers skip authentication; every other caller must
    # authenticate exactly as before. Accepts "*", a host/IP, a glob, a
    # comma-separated list, or CIDR.
    #
    # The default is loopback plus the RFC1918 ranges, which is exactly what the
    # shipped Docker/HAProxy topology needs (Prometheus scrapes the nodes over a
    # private network) while keeping the endpoint off the public internet.
    # Set "*" only on a genuinely trusted network.
    METRICS_ALLOW_IPS: str = (
        "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    )

    # Network RTT threshold (ms).  If the client's measured round-trip
    # time exceeds this, hybrid prefers client-side processing (even on
    # modest hardware) to avoid per-slice latency penalties.
    HYBRID_RTT_THRESHOLD_MS: int = 150
    # Concurrency cap: max simultaneous server-side MPR sessions before
    # the hybrid engine forces new sessions to client mode.
    HYBRID_MAX_SERVER_MPR_SESSIONS: int = 20
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
    # C03: private mode is the baseline. Set this to true only for a legacy
    # LAN deployment where anonymous DICOMweb clients must keep working;
    # anonymous access is then archive-wide (tenant scoping still applies to
    # authenticated callers, and patient share links stay study-scoped).
    DICOMWEB_PUBLIC: bool = False
    CORS_ORIGINS: str = "*"

    # --- Remote-node DICOMweb client (QIDO-RS / WADO-RS) ---
    # SSRF guard: the client refuses to connect to loopback / link-local /
    # private ranges / cloud-metadata IPs by default. Flip this to True only
    # for trusted local test peers (integration tests connect to 127.0.0.1).
    DICOMWEB_CLIENT_ALLOW_PRIVATE: bool = False
    DICOMWEB_CLIENT_TIMEOUT: float = 15.0
    DICOMWEB_CLIENT_MAX_BYTES: int = 4 * 1024 * 1024 * 1024  # 4 GiB per retrieve

    # --- Incoming STOW-RS upload caps ---
    # The STOW handler streams the multipart body part-by-part (one part in
    # memory at a time, never the whole request). These caps bound the worst
    # case: a single part larger than STOW_MAX_PART_BYTES, or a total body
    # larger than STOW_MAX_TOTAL_BYTES (0 = unlimited), is rejected with 413.
    STOW_MAX_PART_BYTES: int = 2 * 1024 * 1024 * 1024   # 2 GiB per part
    STOW_MAX_TOTAL_BYTES: int = 0                        # 0 = unlimited total

    # --- Transfer Accelerator (item 19) ---
    # HTTP-based fast transfer bypassing DIMSE C-STORE: a study's instances
    # are split into batches and pushed to a peer's DICOMweb service over N
    # parallel STOW-RS connections with chunked transfer encoding. Only used
    # when the caller opts in (?accelerated=1 on the web upload endpoint).
    TRANSFER_ACCELERATOR_ENABLED: bool = True
    TRANSFER_ACCELERATOR_WORKERS: int = 4      # concurrent HTTP connections
    TRANSFER_ACCELERATOR_BATCH_SIZE: int = 16  # instances per multipart batch
    # Public base URL used in patient share links/QR codes (behind reverse proxy).
    # Leave EMPTY to derive it from the incoming request (X-Forwarded-Host/Host),
    # or set it explicitly to the origin a browser can reach, e.g.
    #   PUBLIC_BASE_URL=https://pacs.example.org
    # A bind address (0.0.0.0 / ::) is rejected and normalised to the loopback
    # default, because a link or QR code built from one is dead on arrival.
    PUBLIC_BASE_URL: str = ""

    # --- Patient sharing ---
    SHARE_EXPIRY_DAYS: int = 30

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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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
    2. the reverse proxy's ``X-Forwarded-Host`` / ``X-Forwarded-Proto``;
    3. the request's own ``Host`` header;
    4. :data:`DEFAULT_PUBLIC_BASE_URL` (loopback) as a last resort.

    Never returns a bind address: steps 2-3 skip unroutable hosts, so a request
    that arrived on ``0.0.0.0`` still yields a usable origin.

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

    origin = ""
    if request is not None:
        headers = getattr(request, "headers", None) or {}
        host = (headers.get("x-forwarded-host") or headers.get("host") or "")
        host = host.split(",")[0].strip()
        # Strip a default port so the URL stays canonical.
        hostname = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
        if host and hostname.strip("[]").lower() not in _UNROUTABLE_HOSTS:
            proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
            if proto not in ("http", "https"):
                proto = getattr(getattr(request, "url", None), "scheme", "") or "http"
            origin = f"{proto}://{host}"

    if not origin:
        return DEFAULT_PUBLIC_BASE_URL + suffix
    return origin.rstrip("/") + suffix


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

    Single source of truth: ``app/main.py`` (startup gate) and ``run_paxs.py``
    (TLS + auto-reload) both ask this function.
    """
    value = (getattr(settings_obj, "ENVIRONMENT", "") or "").strip().lower()
    if value in NON_PRODUCTION_ENVIRONMENTS:
        pinned_off = "DEBUG" in getattr(settings_obj, "model_fields_set", frozenset())
        return bool(pinned_off and not getattr(settings_obj, "DEBUG", False))
    return True


def validate_production_settings(settings_obj: "Settings") -> None:
    """Refuse to run an insecure configuration in production (H06).

    Called at startup so a production deployment fails closed rather than
    serving traffic with a known JWT secret or an insecure admin cookie.
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

    if problems:
        raise RuntimeError(
            "Refusing to start in production with an insecure configuration:\n  - "
            + "\n  - ".join(problems)
        )


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
    if vol and hybrid and vol > hybrid:
        warnings.append(
            f"VOLUME_CACHE_BUDGET_MB ({vol}) exceeds HYBRID_SERVER_VOL_BUDGET_MB "
            f"({hybrid}). The L1 cache may hold more than mode negotiation assumes, "
            "so the byte half of server_overloaded() can never fire before the "
            "memory is already spent."
        )
    return warnings


settings = get_settings()
