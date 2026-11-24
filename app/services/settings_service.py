"""Runtime-editable server configuration (admin Settings UI).

The dashboard's Settings view is a form over a curated registry of editable
keys (EDITABLE_KEYS below). Each key's *live* value is

    DB override (server_settings table)  ??  env default (app.core.config.settings)

so nothing is hard-coded in the UI: the form always shows what the server is
actually using. PATCHes are validated against the registry, persisted, and
immediately applied to the in-process ``settings`` object after Pydantic
assignment validation and production-security checks. At boot,
``apply_db_overrides()``
replays every stored override so persisted changes survive restarts.

Keys whose consumers bind at import/startup time (DICOM receiver, HL7
listener, router inclusion) carry ``restart=True`` in the registry: the UI
shows an explicit "restart required" hint, and the value still lands in the
DB + settings object so the *next* boot honors it.
"""

from __future__ import annotations

import json
import logging
import re
from threading import RLock
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import settings, validate_production_settings
from app.db.models import AuditLog, ServerSetting
from app.db.session import SyncSessionLocal

log = logging.getLogger("paxs.settings")

# Serializes DB persistence and in-process mutation/reload as one operation.
_SETTINGS_LOCK = RLock()

IMMUTABLE_SECURITY_KEYS = {"ENVIRONMENT", "SECRET_KEY", "SETUP_TOKEN", "STORAGE_ROOT"}

# ---------------------------------------------------------------- registry --
# key -> {label, group, type, options?, description, restart?}
# group values match the dashboard's Settings sub-tabs.
EDITABLE_KEYS: dict[str, dict[str, Any]] = {
    "DISC_BURN_ENABLED": {
        "label": "Enable CD/DVD burning",
        "group": "optical",
        "type": "bool",
        "description": "Allow signed-in users to burn studies they can access on the Windows PAXS server.",
    },
    "DISC_BURN_DRIVE": {
        "label": "Recorder drive letter",
        "group": "optical",
        "type": "str",
        "description": "Enter the server drive letter, for example D:. Leave blank to use the only detected recorder.",
    },
    "DISC_BURN_VOLUME_LABEL": {
        "label": "Disc label",
        "group": "optical",
        "type": "str",
        "description": "Name displayed when the disc is opened. Use 1–16 uppercase letters, digits or underscores. Keep patient details out of the label.",
    },
    # ---- System ----------------------------------------------------------
    "APP_NAME": {
        "label": "Application name",
        "group": "sys",
        "type": "str",
        "description": "Server display name shown in the header and login panel.",
    },
    "PUBLIC_BASE_URL": {
        "label": "Public base URL",
        "group": "sys",
        "type": "str",
        "description": "External URL used in patient share links and QR codes (behind a reverse proxy).",
    },
    "CORS_ORIGINS": {
        "label": "CORS allowed origins",
        "group": "security",
        "type": "str",
        "description": "Comma-separated browser origins; production rejects empty or wildcard origins.",
    },
    "SHARE_EXPIRY_DAYS": {
        "label": "Share link expiry (days)",
        "group": "sys",
        "type": "int",
        "description": "Default lifetime of patient share links.",
    },
    "DICOMWEB_PUBLIC_READ": {
        "label": "Public DICOMweb reads",
        "group": "sys",
        "type": "bool",
        "description": "Allow anonymous QIDO/WADO reads. Patient share links remain limited to their own study when public reads are disabled.",
    },
    "DICOMWEB_PUBLIC_WRITE": {
        "label": "Public DICOMweb writes",
        "group": "security",
        "type": "bool",
        "description": "Allow anonymous STOW-RS uploads. Keep disabled unless unauthenticated archive ingestion is explicitly required.",
    },
    # ---- LDAP / Active Directory (plans/legacy/archive-notes/plan_9_2026.md C02) ---------------------
    "LDAP_ENABLED": {
        "label": "LDAP / AD authentication",
        "group": "security",
        "type": "bool",
        "description": "Enable directory login. Off by default; when off, /api/v1/ldap/login refuses every request.",
    },
    "LDAP_SERVER": {
        "label": "LDAP server URL",
        "group": "security",
        "type": "str",
        "description": "Directory endpoint, e.g. ldaps://dc01.hospital.org or ldap://dc01.hospital.org:389.",
    },
    "LDAP_BASE_DN": {
        "label": "LDAP base DN",
        "group": "security",
        "type": "str",
        "description": "Search base for user binds, e.g. DC=hospital,DC=org.",
    },
    "LDAP_USER_DN_TEMPLATE": {
        "label": "LDAP user DN template",
        "group": "security",
        "type": "str",
        "description": "DN template used to bind a user; {username} and {base_dn} are substituted.",
    },
    "LDAP_GROUP_ROLE_MAP": {
        "label": "LDAP group → role map",
        "group": "security",
        "type": "str",
        "description": 'Semicolon-separated "group=ROLE" pairs, e.g. CN=Radiologists=ADMIN;CN=Doctors=PHYSICIAN. Roles must be ADMIN, RADIOLOGIST, PHYSICIAN or TECHNOLOGIST.',
    },
    "LDAP_DEFAULT_ROLE": {
        "label": "LDAP default role",
        "group": "security",
        "type": "str",
        "description": "Role granted when no group matches. Leave empty to deny such users (fail closed).",
    },
    "STORAGE_ALLOW_LEGACY_PATHS": {
        "label": "Allow legacy storage paths",
        "group": "storage",
        "type": "bool",
        "description": "Read-only support for legacy paths already inside STORAGE_ROOT; never allows access outside the root.",
    },
    # ---- DICOM receiver ----------------------------------------------------
    "DICOM_AE_TITLE": {
        "label": "AE title",
        "group": "dicom",
        "type": "str",
        "restart": True,
        "description": "Application Entity title the DIMSE receiver advertises.",
    },
    "DICOM_PORT": {
        "label": "DIMSE port",
        "group": "dicom",
        "type": "int",
        "restart": True,
        "description": "TCP port for C-STORE / C-FIND / C-GET / C-MOVE / MPPS / Storage Commitment.",
    },
    "DICOM_RECEIVER_ENABLED": {
        "label": "DICOM receiver",
        "group": "dicom",
        "type": "bool",
        "restart": True,
        "description": "Start the DIMSE listener on boot.",
    },
    "DICOM_MAX_ASSOCIATIONS": {
        "label": "Max associations",
        "group": "dicom",
        "type": "int",
        "restart": True,
        "description": "Concurrent DIMSE association cap.",
    },
    "DICOM_TLS_ENABLED": {
        "label": "DICOM TLS (AES)",
        "group": "dicom",
        "type": "bool",
        "restart": True,
        "description": "Wrap DIMSE in TLS 1.2/1.3 with a self-signed certificate.",
    },
    "MWL_DEFAULT_STATION_AET": {
        "label": "MWL default station AE",
        "group": "dicom",
        "type": "str",
        "description": "Station AE stamped on ADT-created worklist steps (empty = all stations).",
    },
    "DICOM_ALLOWED_CALLING_AES": {
        "label": "Allowed calling AE titles",
        "group": "dicom",
        "type": "str",
        "description": "Comma-separated calling AE allowlist (empty = allow any peer; registered nodes are always permitted).",
    },
    "DCMTK_BIN_DIR": {
        "label": "DCMTK bin directory",
        "group": "dicom",
        "type": "str",
        "description": "Absolute path to OFFIS DCMTK binaries (echoscu, findscu, dcmsend, movescu, dcmrecv). Empty disables DCMTK.",
    },
    "DCMTK_MOVE_PORT": {
        "label": "DCMTK C-MOVE receiver port",
        "group": "dicom",
        "type": "int",
        "min": 0,
        "max": 65535,
        "description": "Dedicated TCP port for temporary dcmrecv C-MOVE storage (must differ from DICOM_PORT; 0 disables).",
    },
    "DCMTK_MOVE_AE_TITLE": {
        "label": "DCMTK C-MOVE AE title",
        "group": "dicom",
        "type": "str",
        "description": "AE title advertised by the temporary dcmrecv receiver during C-MOVE pulls.",
    },
    "DCMTK_MOVE_PROFILE": {
        "label": "DCMTK storescp.cfg path",
        "group": "dicom",
        "type": "str",
        "description": "Absolute path to DCMTK storescp.cfg (optional — auto-discovered beside DCMTK_BIN_DIR when blank).",
    },
    # ---- Interop (HL7 / FHIR) ---------------------------------------------
    "HL7_ENABLED": {
        "label": "HL7 MLLP listener",
        "group": "interop",
        "type": "bool",
        "restart": True,
        "description": "Receive ADT admissions and ORM imaging orders from the HIS.",
    },
    "HL7_HOST": {
        "label": "HL7 bind host",
        "group": "interop",
        "type": "str",
        "restart": True,
    },
    "HL7_PORT": {
        "label": "HL7 MLLP port",
        "group": "interop",
        "type": "int",
        "restart": True,
    },
    "HL7_EMR_ENABLED": {
        "label": "ORU report write-back",
        "group": "interop",
        "type": "bool",
        "description": "Send ORU^R01 to the EMR when a report is finalized.",
    },
    "HL7_EMR_HOST": {
        "label": "EMR host",
        "group": "interop",
        "type": "str",
    },
    "HL7_EMR_PORT": {
        "label": "EMR MLLP port",
        "group": "interop",
        "type": "int",
    },
    "HL7_EMR_APPLICATION": {
        "label": "EMR application (MSH-5)",
        "group": "interop",
        "type": "str",
    },
    "HL7_EMR_FACILITY": {
        "label": "EMR facility (MSH-6)",
        "group": "interop",
        "type": "str",
    },
    "HL7_SENDING_APPLICATION": {
        "label": "Sending application (MSH-3)",
        "group": "interop",
        "type": "str",
    },
    "HL7_SENDING_FACILITY": {
        "label": "Sending facility (MSH-4)",
        "group": "interop",
        "type": "str",
    },
    "FHIR_ENABLED": {
        "label": "FHIR R4 endpoint",
        "group": "interop",
        "type": "bool",
        "restart": True,
        "description": "Serve the /fhir REST API.",
    },
    "FUSION_ENABLED": {
        "label": "Image fusion (PET/CT, MR/CT)",
        "group": "interop",
        "type": "bool",
        "restart": True,
        "description": "Serve the fusion endpoints. Independent of FHIR (H18).",
    },
    "HL7_TLS_ENABLED": {
        "label": "HL7 MLLP TLS",
        "group": "interop",
        "type": "bool",
        "restart": True,
        "description": "Require TLS 1.2/1.3 on the inbound HL7 MLLP listener.",
    },
    "HL7_ALLOWED_SOURCE_IPS": {
        "label": "HL7 allowed source IPs/CIDRs",
        "group": "interop",
        "type": "str",
        "description": "Comma-separated HIS/RIS IPs or CIDRs permitted to connect (empty = loopback only).",
    },
    "HL7_ALLOWED_SENDING_FACILITIES": {
        "label": "HL7 allowed senders (APP|FAC)",
        "group": "interop",
        "type": "str",
        "description": "Comma-separated MSH-3|MSH-4 pairs permitted to send HL7 messages (empty = any).",
    },
    "DICOMWEB_CLIENT_ALLOW_PRIVATE": {
        "label": "DICOMweb client allow private IPs",
        "group": "interop",
        "type": "bool",
        "description": "Allow outbound QIDO/WADO/STOW web-peer requests to private/LAN IP addresses.",
    },
    "WEBHOOK_URLS": {
        "label": "Webhook URLs",
        "group": "interop",
        "type": "str",
        "description": "Comma-separated HTTP/HTTPS URLs notified on StudyReceived and archive events.",
    },
    # ---- Storage -----------------------------------------------------------
    "STORAGE_BACKEND": {
        "label": "Storage backend",
        "group": "storage",
        "type": "select",
        "options": ["local", "s3"],
        "restart": True,
        "description": "Filesystem under STORAGE_ROOT, or an S3-compatible object store.",
    },
    "STORAGE_MIN_FREE_GB": {
        "label": "Minimum free disk space (GB)",
        "group": "storage",
        "type": "float",
        "description": "Reject new local ingests cleanly (DICOM 0xA700 / HTTP 507) if free disk falls below this GB floor. 0 disables.",
    },
    "STORAGE_MIN_FREE_PERCENT": {
        "label": "Minimum free disk space (%)",
        "group": "storage",
        "type": "float",
        "description": "Reject new local ingests cleanly if free disk percentage falls below this threshold. 0 disables.",
    },
    "DICOM_MAX_PDU_SIZE": {
        "label": "DICOM max PDU size (bytes)",
        "group": "dicom",
        "type": "int",
        "restart": True,
        "description": "Maximum DIMSE PDU payload size (16384–1048576). 131072 (128 KiB) boosts gigabit CT/MR transfer throughput.",
    },
    "DICOM_NETWORK_TIMEOUT_SECONDS": {
        "label": "DICOM network timeout (s)",
        "group": "dicom",
        "type": "int",
        "restart": True,
        "description": "Drop stalled modality sockets after this many seconds so association slots stay free.",
    },
    "DICOM_DIMSE_TIMEOUT_SECONDS": {
        "label": "DICOM DIMSE timeout (s)",
        "group": "dicom",
        "type": "int",
        "restart": True,
        "description": "Timeout waiting for a DIMSE response on an active association.",
    },
    "WORKER_COMPRESSION_CONCURRENCY": {
        "label": "Compression worker concurrency",
        "group": "compression",
        "type": "int",
        "description": "Concurrent compression jobs per worker process (keep 1 in embedded API mode; 2–4 in standalone PAXS-Worker.exe).",
    },
    "COMPRESSION_JOBS_RETENTION_DAYS": {
        "label": "Compression job history retention (days)",
        "group": "compression",
        "type": "int",
        "description": "Prune completed DONE/FAILED compression_jobs older than N days in bounded batches (0 = keep forever).",
    },
    "AUDIT_LOG_RETENTION_DAYS": {
        "label": "Audit log archive & prune retention (days)",
        "group": "security",
        "type": "int",
        "description": "0 keeps all audit logs in PostgreSQL forever. When >0, older rows are archived to gzipped NDJSON under BACKUP_DIR/audit_archives/ and verified before deletion.",
    },
    "S3_ENDPOINT_URL": {
        "label": "S3 endpoint URL",
        "group": "storage",
        "type": "str",
        "description": "MinIO / S3-compatible endpoint (empty = AWS).",
    },
    "S3_REGION": {
        "label": "S3 region",
        "group": "storage",
        "type": "str",
    },
    "S3_BUCKET": {
        "label": "S3 bucket",
        "group": "storage",
        "type": "str",
        "restart": True,
    },
    "S3_PRESIGN_EXPIRES": {
        "label": "S3 presign expiry (s)",
        "group": "storage",
        "type": "int",
    },
    "DEFAULT_COMPRESSION": {
        "label": "Default image format",
        "group": "compression",
        "type": "select",
        "options": ["AS_SOURCE", "UNCOMPRESSED", "RLE", "JPEG-LS", "JPEG2000", "HTJ2K"],
        "option_labels": {
            "AS_SOURCE": "As source image",
            "UNCOMPRESSED": "Uncompressed",
            "RLE": "Run Length Encoding (RLE)",
            "JPEG-LS": "JPEG-LS Lossless",
            "JPEG2000": "JPEG 2000 Lossless",
            "HTJ2K": "HTJ2K Lossless",
        },
        "description": "Applied to newly received images. Uncompressed can use substantially more disk space.",
    },
    "COMPRESSION_ENABLED": {
        "label": "Convert images in background",
        "group": "compression",
        "type": "bool",
        "description": "The archive stores incoming files first. The worker converts eligible images after storage.",
    },
    "COMPRESSION_TIMING": {
        "label": "Default store compression time",
        "group": "compression",
        "type": "select",
        "options": ["immediate", "scheduled"],
        "option_labels": {"immediate": "After receipt", "scheduled": "Only in the time window"},
        "description": "A running job can finish outside the window. The window uses the server's local time.",
    },
    "COMPRESSION_START_TIME": {
        "label": "Start time",
        "group": "compression",
        "type": "str",
        "input": "time",
    },
    "COMPRESSION_END_TIME": {
        "label": "End time",
        "group": "compression",
        "type": "str",
        "input": "time",
        "description": "An earlier end time crosses midnight. Equal times allow processing all day.",
    },
    "THUMBNAIL_ENABLED": {
        "label": "Generate thumbnails",
        "group": "compression",
        "type": "bool",
    },
    "THUMBNAIL_ON_RECEIPT": {
        "label": "Queue thumbnails on receipt",
        "group": "compression",
        "type": "bool",
        "description": "When thumbnails are enabled, generate them through a separate job that can run outside the compression window.",
    },
    "PATIENT_NAME_DISPLAY": {
        "label": "Patient name display",
        "group": "compression",
        "type": "select",
        "options": ["default", "source", "structured", "remove_carets"],
        "option_labels": {
            "default": "Application default",
            "source": "As stored",
            "structured": "Family, given, middle, prefix and suffix",
            "remove_carets": "Remove ^ separators",
        },
        "description": "Display only in the archive study lists. DICOM patient names and client viewer overlays keep their own formatting.",
    },
    "WORKER_ENABLED": {
        "label": "Enable background worker",
        "group": "compression",
        "type": "bool",
        "restart": True,
        "description": "Also runs other background archive services, including transfers and routing.",
    },
    "WORKER_BATCH_SIZE": {
        "label": "Jobs per worker cycle",
        "group": "compression",
        "type": "int",
        "min": 1,
        "max": 1000,
        "description": "Each worker processes these jobs sequentially before its next poll.",
    },
    "WORKER_POLL_SECONDS": {
        "label": "Worker poll interval (seconds)",
        "group": "compression",
        "type": "float",
        "min": 0.1,
        "max": 300,
        "restart": True,
    },
    "LOSSY_COMPRESSION_ENABLED": {
        "label": "Allow lossy compression runs",
        "group": "compression",
        "type": "bool",
        "description": "Permits manual rules to replace original pixel values permanently. Review the retention policy and backup before enabling.",
    },
    "WARM_ON_OPEN_ENABLED": {
        "label": "Warm on viewer open",
        "group": "storage",
        "type": "bool",
        "description": "Pre-generate thumbnails + warm frame caches for a study the moment it is opened in the viewer.",
    },
    # ---- Security ----------------------------------------------------------
    "TOKEN_EXPIRE_MINUTES": {
        "label": "Access token lifetime (min)",
        "group": "security",
        "type": "int",
        "description": "Short-lived bearer JWT; silently refreshed via the session cookie.",
    },
    "REFRESH_TOKEN_EXPIRE_DAYS": {
        "label": "Session lifetime (days)",
        "group": "security",
        "type": "int",
        "description": "Rotating refresh-token expiry — how long a login stays alive.",
    },
    "ALLOW_REGISTER": {
        "label": "Allow self-registration",
        "group": "security",
        "type": "bool",
        "description": "Let new users create accounts from the login panel.",
    },
    "TRUSTED_PROXIES": {
        "label": "Trusted proxies",
        "group": "security",
        "type": "str",
        "restart": False,
        "description": "Comma-separated peers whose X-Forwarded-For is honoured for rate limiting (H07).",
    },
    "RATE_LIMIT_ENABLED": {
        "label": "Rate limiting",
        "group": "security",
        "type": "bool",
        "description": "Sliding-window per-IP abuse protection.",
    },
    "RATE_LIMIT_LOGIN_PER_MINUTE": {
        "label": "Login attempts / min",
        "group": "security",
        "type": "int",
    },
    "RATE_LIMIT_GLOBAL_PER_MINUTE": {
        "label": "Global requests / min",
        "group": "security",
        "type": "int",
    },
    "DICOMWEB_CLIENT_ALLOW_PRIVATE": {
        "label": "Allow private web peers",
        "group": "security",
        "type": "bool",
        "description": "Permit node QIDO/WADO/STOW connections to loopback/private IPs (SSRF guard off).",
    },
    "ANONYMIZER_STRIP_PRIVATE_TAGS": {
        "label": "Anonymizer: strip private tags",
        "group": "security",
        "type": "bool",
    },
    "ANONYMIZER_STRIP_DATES": {
        "label": "Anonymizer: strip dates",
        "group": "security",
        "type": "bool",
    },
    # ---- Workflow ----------------------------------------------------------
    "TRIAGE_ENABLED": {
        "label": "AI emergency triage",
        "group": "workflow",
        "type": "bool",
        "description": "Run the critical-finding detector pipeline in the worker.",
    },
    "TRIAGE_MAX_SLICES_PER_SERIES": {
        "label": "Triage max slices / series",
        "group": "workflow",
        "type": "int",
    },
    "LIFECYCLE_RUN_IN_WORKER": {
        "label": "Lifecycle auto-archive sweep",
        "group": "compression",
        "type": "bool",
        "description": "Worker evaluates archive/restore/purge rules on schedule.",
    },
    "LIFECYCLE_AUTO_ARCHIVE_WADO_PULL": {
        "label": "Auto-tier WADO pulls to cold",
        "group": "workflow",
        "type": "bool",
        "description": "Tier studies pulled via Web Retrieve straight to the cold tier.",
    },
    "ROUTING_QUEUE_ENABLED": {
        "label": "Smart Router send queue",
        "group": "compression",
        "type": "bool",
    },
    "ROUTING_SCU_AE_TITLE": {
        "label": "Auto-route calling AE title",
        "group": "compression",
        "type": "str",
        "description": "The remote destination must accept this calling AE. Applies to the Smart Router's outbound C-STORE associations.",
    },
    "ROUTING_TIMING": {
        "label": "Auto-route dispatch time",
        "group": "compression",
        "type": "select",
        "options": ["immediate", "scheduled"],
        "option_labels": {"immediate": "Dispatch when due", "scheduled": "Only in the routing window"},
        "description": "Applies to the durable send queue, using the server's local time.",
    },
    "ROUTING_START_TIME": {
        "label": "Routing window start",
        "group": "compression",
        "type": "str",
        "input": "time",
    },
    "ROUTING_END_TIME": {
        "label": "Routing window end",
        "group": "compression",
        "type": "str",
        "input": "time",
    },
    "ROUTING_MAX_ATTEMPTS": {
        "label": "Router max attempts",
        "group": "workflow",
        "type": "int",
    },
    "ROUTING_RETRY_BACKOFF_SECONDS": {
        "label": "Router retry backoff (s)",
        "group": "workflow",
        "type": "int",
    },
    "TRANSFER_ACCELERATOR_ENABLED": {
        "label": "Transfer Accelerator",
        "group": "workflow",
        "type": "bool",
        "description": "Parallel chunked STOW-RS fast uploads to web peers.",
    },
    "TRANSFER_ACCELERATOR_WORKERS": {
        "label": "Accelerator workers",
        "group": "workflow",
        "type": "int",
    },
    "TRANSFER_ACCELERATOR_BATCH_SIZE": {
        "label": "Accelerator batch size",
        "group": "workflow",
        "type": "int",
    },
    # ---- Viewer Processing (roadmap #51) ---------------------------------
    "VIEWER_PROCESSING_MODE": {
        "label": "Viewer processing mode",
        "group": "viewer",
        "type": "select",
        "options": ["hybrid", "server", "client"],
        "description": (
            "How DICOM decoding, MPR and 3D rendering are distributed. "
            "'hybrid' auto-detects client capabilities; 'server' renders "
            "all frames server-side (thin client); 'client' streams raw "
            "voxels to the browser GPU (diagnostic workstation)."
        ),
    },
    "HYBRID_CLIENT_SCORE_THRESHOLD": {
        "label": "Hybrid: min client score (0-100)",
        "group": "viewer",
        "type": "int",
        "min": 0,
        "max": 100,
        "description": "WebGL capability score below which the hybrid engine falls back to server mode.",
    },
    "HYBRID_SERVER_VOL_BUDGET_MB": {
        "label": "Hybrid: server volume budget (MB)",
        "group": "viewer",
        "type": "int",
        "min": 0,
        "max": 65536,
        "description": "Policy threshold for shifting new MPR viewers to client mode; zero disables this signal, and the effective threshold is clamped to the L1 volume-cache budget.",
    },
    "HYBRID_RTT_THRESHOLD_MS": {
        "label": "Hybrid: network RTT threshold (ms)",
        "group": "viewer",
        "type": "int",
        "min": 0,
        "max": 600000,
        "description": "Round-trip time above which client-side processing is preferred over per-slice server calls.",
    },
    "HYBRID_MAX_SERVER_MPR_SESSIONS": {
        "label": "Hybrid: max server MPR sessions",
        "group": "viewer",
        "type": "int",
        "min": 0,
        "max": 4096,
        "description": "Recent server-side MPR viewer scopes before new viewers are sent to client mode; zero disables this signal, and idle scopes expire after five minutes.",
    },
    # ---- Cache memory budgets (V1-2 / V3-6) ------------------------------
    # Hard byte ceilings for the in-process decode caches. Independent of
    # HYBRID_SERVER_VOL_BUDGET_MB (a negotiation input) so a tuning change there
    # cannot silently raise the process's worst-case RSS.
    "VOLUME_CACHE_BUDGET_MB": {
        "label": "Volume cache budget (MB)",
        "group": "viewer",
        "type": "int",
        "min": 0,
        "max": 65536,
        "description": (
            "Hard ceiling for the L1 materialised-volume cache (float32 HU arrays; "
            "~500 MB per 512x512x500 CT). LRU-evicted; zero disables this cache. "
            "Server mode shedding uses the lower enabled value of this hard cap "
            "and HYBRID_SERVER_VOL_BUDGET_MB."
        ),
    },
    "DICOM_FRAME_CACHE_MB": {
        "label": "Decoded frame cache (MB)",
        "group": "viewer",
        "type": "int",
        "min": 0,
        "max": 16384,
        "description": (
            "Hard ceiling for the decoded-frame LRU in the WADO-RS bulk service "
            "(raw pixel bytes). Bound by bytes rather than entry count so it scales "
            "with image size; a 512x512 CT frame is ~0.5 MB, a 3072x2560 J2K frame "
            "takes ~2.3 s to decode; zero disables this cache."
        ),
    },
    "VIEWER_PROCESSING_ROLE_MAP": {
        "label": "Per-role mode overrides (JSON)",
        "group": "viewer",
        "type": "str",
        "description": "JSON map of role\u2192mode overrides, e.g. {\"RADIOLOGIST\":\"client\"}. Empty = global mode for all.",
    },
}

SECURITY_SENSITIVE_KEYS = {
    key for key, spec in EDITABLE_KEYS.items() if spec["group"] == "security"
} | {"DICOMWEB_PUBLIC_READ"}

#: The audit action recorded for every override mutation. One name, greppable,
#: so an operator can answer "who changed the security posture, and when" with a
#: single query. (`CONFIG_UPDATED` was the previous spelling; rows written
#: before this rename still carry it.)
AUDIT_ACTION = "SETTING_OVERRIDE"

#: Placeholder written instead of a secret's value.
AUDIT_REDACTED = "<redacted>"

#: Explicit opt-in for keys whose value must never reach the audit table. Empty
#: today — the name heuristic below is what actually catches a credential.
SECRET_AUDIT_KEYS: frozenset[str] = frozenset()

_SECRET_NAME_RE = re.compile(
    r"PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|CREDENTIAL|PRIVATE_?KEY", re.IGNORECASE
)


def is_secret_key(key: str) -> bool:
    """True when ``key``'s value is a credential and must not be audited.

    Name-based on purpose: the failure mode of forgetting to register a new
    secret is a plaintext credential in an exported table, so the default for a
    credential-shaped name has to be "redact".
    """
    return key in SECRET_AUDIT_KEYS or bool(_SECRET_NAME_RE.search(key))


GROUPS: dict[str, dict[str, str]] = {
    "compression": {"label": "Compression", "description": "Store format, processing window, thumbnails, and background archive controls."},
    "optical": {"label": "CD/DVD Burning", "description": "Use a blank writable disc in a recorder attached to the Windows server. PAXS verifies every written file."},
    "sys": {"label": "System", "description": "Identity, URLs and sharing defaults."},
    "dicom": {"label": "DICOM", "description": "DIMSE receiver, AE title, TLS and worklist defaults."},
    "interop": {"label": "Interop", "description": "HL7 MLLP (ADT · ORM · ORU) and FHIR R4 gateways."},
    "storage": {"label": "Storage", "description": "Backend (local / S3) and compression policy."},
    "security": {"label": "Security", "description": "Sessions, registration, rate limits and anonymizer."},
    "workflow": {"label": "Workflow", "description": "Triage, lifecycle, routing and transfer acceleration."},
    "viewer": {"label": "Viewer", "description": "Processing distribution, rendering modes and hybrid auto-detection thresholds."},
}

# ------------------------------------------------------------- coercion ----
def _parse(key: str, raw: str) -> Any:
    """Convert a stored string back to the key's native Python type."""
    t = EDITABLE_KEYS[key]["type"]
    if t == "bool":
        normalized = str(raw).strip().lower()
        if normalized not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
            raise ValueError(f"invalid stored boolean for {key}")
        return normalized in {"1", "true", "yes", "on"}
    if t == "int":
        return int(raw)
    if t == "float":
        return float(raw)
    return raw


def _coerce(key: str, value: Any) -> str:
    """Validate + canonicalize an incoming value for storage (raises ValueError)."""
    spec = EDITABLE_KEYS[key]
    t = spec["type"]
    if value is None:
        raise ValueError("value is required")

    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError("expected a boolean")
        return "1" if value else "0"
    if t == "int":
        if isinstance(value, bool) or (
            isinstance(value, float) and not value.is_integer()
        ):
            raise ValueError("expected an integer")
        try:
            n = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected an integer") from exc
        # Optional per-key range (used by the cache memory budgets, where a 0 or
        # a negative value would silently disable a cache).
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and n < lo:
            raise ValueError(f"must be >= {lo}")
        if hi is not None and n > hi:
            raise ValueError(f"must be <= {hi}")
        return str(n)
    if t == "float":
        try:
            return str(float(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("expected a number") from exc
    if t == "select":
        if value not in spec.get("options", []):
            raise ValueError(f"must be one of {', '.join(spec['options'])}")
        return str(value)
    return str(value)


def _row(session, key: str) -> ServerSetting | None:
    """Fetch a setting row by its key column (NOT the surrogate PK)."""
    return session.execute(
        select(ServerSetting).where(ServerSetting.key == key)
    ).scalar_one_or_none()


def current_value(key: str) -> Any:
    """Live value: DB override (if any) ?? env default."""
    if key not in EDITABLE_KEYS or key in IMMUTABLE_SECURITY_KEYS:
        raise KeyError(key)
    with _SETTINGS_LOCK:
        with SyncSessionLocal() as session:
            row = _row(session, key)
        if row is not None:
            return _parse(key, row.value)
        return getattr(settings, key)


def is_overridden(key: str) -> bool:
    if key in IMMUTABLE_SECURITY_KEYS:
        return False
    with _SETTINGS_LOCK, SyncSessionLocal() as session:
        return _row(session, key) is not None


def _candidate_with_assignment(key: str, value: Any):
    """Validate a proposed value on an isolated settings copy."""
    candidate = settings.model_copy(deep=True)
    try:
        setattr(candidate, key, value)
    except ValidationError as exc:
        raise ValueError(f"invalid value for {key}: {exc}") from exc
    return candidate, getattr(candidate, key)


def _audit_value(key: str, value: Any) -> str:
    """Serialize a typed setting value for the dedicated audit columns.

    Secrets are redacted (B-22). None of the keys currently in the registry is a
    credential, but the audit table is exported and shipped off-box, so the
    mechanism has to exist *before* one is added: an SMTP password or an LDAP
    bind password landing there in plaintext would be exactly the kind of leak
    the audit trail is supposed to detect. Redaction is driven by the key name
    as well as an explicit set, so a new credential-shaped key is covered
    without anyone remembering to register it.
    """
    if is_secret_key(key):
        return json.dumps(AUDIT_REDACTED)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


# ------------------------------------------------------------ mutations ----
def set_override(
    key: str,
    value: Any,
    *,
    actor_user_id: int,
    performed_by: str,
) -> Any:
    """Persist and apply one validated, audited runtime setting update.

    The re-entrant lock covers candidate validation, the setting + audit DB
    transaction, and the in-process mutation so competing request threads cannot
    interleave database and runtime state. Returns the typed live value.
    """
    if key in IMMUTABLE_SECURITY_KEYS:
        raise ValueError(f"{key} is immutable at runtime")
    if key not in EDITABLE_KEYS:
        raise KeyError(f"unknown setting {key!r}")

    with _SETTINGS_LOCK:
        stored = _coerce(key, value)
        candidate, parsed = _candidate_with_assignment(key, _parse(key, stored))
        if key in SECURITY_SENSITIVE_KEYS:
            validate_production_settings(candidate)
        stored = _coerce(key, parsed)

        with SyncSessionLocal() as session:
            _persist_override(session, key, stored, parsed, actor_user_id, performed_by)

        _apply_to_runtime(key, parsed)
        return parsed


def _persist_override(
    session: Session,
    key: str,
    stored: str,
    parsed: Any,
    actor_user_id: int,
    performed_by: str,
) -> None:
    """Atomically persist the setting and its redacted audit record."""
    row = _row(session, key)
    old_value = _parse(key, row.value) if row is not None else getattr(settings, key)
    if row is None:
        session.add(ServerSetting(key=key, value=stored))
    else:
        row.value = stored
    session.add(AuditLog(
        user_id=actor_user_id,
        action=AUDIT_ACTION,
        performed_by=performed_by,
        config_key=key,
        old_value=_audit_value(key, old_value),
        new_value=_audit_value(key, parsed),
    ))
    session.commit()


def _apply_to_runtime(key: str, parsed: Any) -> None:
    """Push a prevalidated value onto the in-process settings object."""
    with _SETTINGS_LOCK:
        setattr(settings, key, parsed)


def overrides_map() -> dict[str, Any]:
    """All stored editable overrides as {key: parsed value} (single query).

    Immutable security keys and keys stored by other subsystems (e.g. the DB
    panel's own knobs) are intentionally omitted from the Settings UI.
    """
    with _SETTINGS_LOCK:
        with SyncSessionLocal() as session:
            rows = session.execute(
                select(ServerSetting.key, ServerSetting.value)
            ).all()
        out: dict[str, Any] = {}
        for row in rows:
            if row.key not in EDITABLE_KEYS or row.key in IMMUTABLE_SECURITY_KEYS:
                continue
            out[row.key] = _parse(row.key, row.value)
        return out


def apply_db_overrides() -> int:
    """Validate, then atomically replay stored overrides onto runtime settings.

    Immutable keys are ignored even if manually inserted into ``server_settings``.
    The complete candidate configuration passes the production gate before any
    runtime values are changed, so unsafe persisted state prevents startup.
    """
    with _SETTINGS_LOCK:
        with SyncSessionLocal() as session:
            rows = session.execute(
                ServerSetting.__table__.select().order_by(ServerSetting.key)
            ).all()

        candidate = settings.model_copy(deep=True)
        applied_keys: list[str] = []
        for row in rows:
            key, value = row.key, row.value
            if key in IMMUTABLE_SECURITY_KEYS:
                log.error("Ignoring persisted override for immutable security key %s", key)
                continue
            if key not in EDITABLE_KEYS:
                continue
            try:
                setattr(candidate, key, _parse(key, value))
            except ValidationError as exc:
                raise ValueError(f"invalid persisted setting {key}: {exc}") from exc
            applied_keys.append(key)

        validate_production_settings(candidate)
        for key in applied_keys:
            _apply_to_runtime(key, getattr(candidate, key))

        if applied_keys:
            log.info("Applied %d server-settings override(s) from the database", len(applied_keys))
        return len(applied_keys)
