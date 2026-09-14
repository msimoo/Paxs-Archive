"""Application configuration (pydantic-settings, .env aware)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- Compression / worker ---
    DEFAULT_COMPRESSION: str = "JPEG-LS"  # JPEG-LS (lossless fast) | JPEG2000 | HTJ2K | RLE
    COMPRESSION_ENABLED: bool = True
    THUMBNAIL_ENABLED: bool = True
    WORKER_ENABLED: bool = True
    WORKER_POLL_SECONDS: float = 3.0
    # How many PENDING compression jobs a worker claims per tick. Standalone
    # worker processes (python -m app.services.worker) scale compression
    # ~linearly with cores; this bounds how many each process grabs.
    WORKER_BATCH_SIZE: int = 15

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
    SECRET_KEY: str = "paxs-insecure-dev-key-change-me"
    # Short-lived bearer access token (auth plan §2.1). The dashboard/viewer
    # silently refresh it via POST /auth/refresh using the rotating refresh
    # cookie, so a 15-minute lifetime is invisible to the user.
    TOKEN_EXPIRE_MINUTES: int = 15
    # Rotating refresh token (auth plan §2.2/§3.2): opaque value stored
    # SHA-256 hashed in the sessions table, rotated on every use, with
    # whole-family revocation on reuse/theft. Carried in the HttpOnly cookie.
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ALLOW_REGISTER: bool = True
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
    AUTH_COOKIE_SECURE: bool = False
    AUTH_GATE_ENABLED: bool = True

    # --- Rate limiting (brute-force / abuse protection) ---
    RATE_LIMIT_ENABLED: bool = True
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
    DICOMWEB_PUBLIC: bool = True
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
    PUBLIC_BASE_URL: str = "http://0.0.0.0"

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

    @property
    def cors_origin_list(self) -> list[str]:
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
