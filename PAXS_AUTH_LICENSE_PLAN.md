# PAXS — Comprehensive Authentication & Licensing System Plan

> **Scope:** Unified authentication for PAXS Archive (FastAPI/Python) and paxs viewer (OHIF v3/React), role-based access control, hardware-bound licensing with online activation portal.

---

## Table of Contents

1. [User Roles & Permissions](#1-user-roles--permissions)
2. [Authentication Architecture](#2-authentication-architecture)
3. [Database Schema](#3-database-schema)
4. [Security & Anti-Tamper Measures](#4-security--anti-tamper-measures)
5. [Licensing System](#5-licensing-system)
6. [Machine Fingerprinting](#6-machine-fingerprinting)
7. [License File Structure](#7-license-file-structure)
8. [Online Activation Portal](#8-online-activation-portal)
9. [End-User Flow](#9-end-user-flow)
10. [Implementation Roadmap](#10-implementation-roadmap)

---

## 1. User Roles & Permissions

### Role Hierarchy

```
Super Admin (Developer — you)
    └── Admin (Hospital IT / Manager)
            ├── Radiologist
            ├── Technician
            └── Referring Physician
```

### Permission Matrix

| Feature | Super Admin | Admin | Radiologist | Technician | Referring Physician |
|---------|:-----------:|:-----:|:-----------:|:----------:|:-------------------:|
| PAXS Archive Dashboard | ✅ | ✅ | ❌ | ❌ | ❌ |
| User Management | ✅ | ✅ | ❌ | ❌ | ❌ |
| Full Study List | ✅ | ✅ | ✅ | ✅ | Own patients only |
| paxs viewer | ✅ | ✅ | ✅ | View only | View only |
| License Management | ✅ | ❌ | ❌ | ❌ | ❌ |
| System Reports | ✅ | ✅ | ❌ | ❌ | ❌ |
| Storage Settings | ✅ | ✅ | ❌ | ❌ | ❌ |
| DICOM Configuration | ✅ | ✅ | ❌ | ❌ | ❌ |
| Export / Download | ✅ | ✅ | ✅ | ❌ | ❌ |
| Create Worklist Items | ✅ | ✅ | ❌ | ✅ | ❌ |

### Post-Login Redirect Rules

```
Super Admin        →  /archive/dashboard
Admin              →  /archive/dashboard
Radiologist        →  /viewer/study-list
Technician         →  /viewer/worklist
Referring Physician →  /viewer/my-patients
```

Any attempt by a Radiologist, Technician, or Referring Physician to access
`/archive/*` triggers an automatic HTTP 303 redirect to their designated landing page.

---

## 2. Authentication Architecture

### 2.1 Login Flow

```
User opens browser
        │
        ▼
GET /login  ←─── FastAPI serves unified login page (all roles)
        │
        ▼
POST /auth/login  { username, password }
        │
        ├── Validate credentials (bcrypt, cost=12)
        ├── Check license validity
        ├── Check account active & not locked
        ├── Check failed attempt count
        │
        ▼
    ┌───────────────────────────┐
    │   Issue Token Pair        │
    │                           │
    │  Access Token  (15 min)   │  ← JWT in Authorization header
    │  Refresh Token (7 days)   │  ← HttpOnly Secure cookie
    └───────────────────────────┘
        │
        ▼
Redirect based on role (see §1 table)
```

### 2.2 Token Architecture

#### Access Token (JWT)

```json
{
  "sub": "42",
  "role": "radiologist",
  "tenant_id": 3,
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "iat": 1720000000,
  "exp": 1720000900,
  "jti": "unique-token-id-prevents-replay"
}
```

#### Refresh Token

- Stored as `HttpOnly; Secure; SameSite=Strict` cookie
- Rotated on every use — old token immediately invalidated
- If two requests arrive with the same refresh token (theft detection),
  the entire session family is revoked immediately

### 2.3 OHIF Integration

OHIF is a React SPA — it has no knowledge of Python auth.
FastAPI acts as the gatekeeper before OHIF ever loads:

```
Browser requests /viewer/*
        │
        ▼
FastAPI middleware checks HttpOnly cookie
        │
   ┌────┴────────┐
   │             │
Authenticated  Not authenticated
   │             │
   ▼             ▼
Serve OHIF    Redirect to /login
index.html    (with ?next=/viewer/study-list)
```

```python
@app.get("/viewer/{path:path}")
async def serve_viewer(
    path: str,
    current_user: User = Depends(get_current_user_from_cookie)
):
    if current_user.role in ["radiologist", "technician", "referring"]:
        # Prevent any access to archive routes
        if path.startswith("archive"):
            return RedirectResponse("/viewer/study-list", status_code=303)
    return FileResponse("static/viewer/index.html")
```

### 2.4 FastAPI Route Protection

```python
# Decorator-based role enforcement
@router.get("/archive/dashboard")
@require_roles(["admin", "super_admin"])
@require_valid_license
async def dashboard(current_user: User = Depends(get_current_user)):
    ...

# Referring physician — own patients only
@router.get("/viewer/studies")
async def get_studies(
    current_user: User = Depends(get_current_user)
) -> list[StudyOut]:
    if current_user.role == "referring":
        return await study_service.get_by_referring_physician(
            current_user.physician_id
        )
    return await study_service.get_all()
```

---

## 3. Database Schema

### 3.1 Users Table

```sql
CREATE TABLE users (
    id                  INT PRIMARY KEY AUTO_INCREMENT,
    username            VARCHAR(50)  UNIQUE NOT NULL,
    email               VARCHAR(100) UNIQUE NOT NULL,
    password_hash       VARCHAR(255) NOT NULL,          -- bcrypt cost=12
    role                ENUM(
                            'super_admin',
                            'admin',
                            'radiologist',
                            'technician',
                            'referring'
                        ) NOT NULL,
    full_name           VARCHAR(200),
    physician_id        VARCHAR(50),                    -- for referring physicians
    is_active           BOOLEAN      DEFAULT TRUE,
    is_locked           BOOLEAN      DEFAULT FALSE,
    failed_attempts     INT          DEFAULT 0,
    locked_until        DATETIME     NULL,
    last_login          DATETIME     NULL,
    last_login_ip       VARCHAR(45)  NULL,
    password_changed_at DATETIME     DEFAULT NOW(),
    created_at          DATETIME     DEFAULT NOW(),
    created_by          INT          REFERENCES users(id),
    tenant_id           INT          NOT NULL           -- hospital this user belongs to
);
```

### 3.2 Sessions Table

```sql
CREATE TABLE sessions (
    id                  VARCHAR(64)  PRIMARY KEY,       -- UUIDv4
    user_id             INT          NOT NULL REFERENCES users(id),
    refresh_token_hash  VARCHAR(255) NOT NULL,          -- SHA-256 of the actual token
    family_id           VARCHAR(64)  NOT NULL,          -- for theft detection
    ip_address          VARCHAR(45),
    user_agent          TEXT,
    created_at          DATETIME     DEFAULT NOW(),
    last_used_at        DATETIME     DEFAULT NOW(),
    expires_at          DATETIME     NOT NULL,
    is_revoked          BOOLEAN      DEFAULT FALSE,
    revoke_reason       VARCHAR(100) NULL
);
```

### 3.3 Auth Audit Log Table

```sql
CREATE TABLE auth_audit_log (
    id              BIGINT   PRIMARY KEY AUTO_INCREMENT,
    user_id         INT      NULL REFERENCES users(id),
    action          ENUM(
                        'login_success',
                        'login_failed',
                        'logout',
                        'token_refresh',
                        'token_theft_detected',
                        'password_change',
                        'account_locked',
                        'account_unlocked',
                        'role_changed'
                    ) NOT NULL,
    ip_address      VARCHAR(45),
    user_agent      TEXT,
    timestamp       DATETIME DEFAULT NOW(),
    details         JSON     NULL                       -- extra context, never PHI
);
```

### 3.4 Tenants Table (Multi-Hospital)

```sql
CREATE TABLE tenants (
    id              INT          PRIMARY KEY AUTO_INCREMENT,
    name            VARCHAR(200) NOT NULL,              -- hospital name
    license_key     VARCHAR(30)  UNIQUE NOT NULL,
    is_active       BOOLEAN      DEFAULT TRUE,
    created_at      DATETIME     DEFAULT NOW()
);
```

---

## 4. Security & Anti-Tamper Measures

### 4.1 Brute Force & Account Protection

```python
MAX_FAILED_ATTEMPTS = 5          # lock after 5 failures
LOCKOUT_DURATION_MINUTES = 15    # locked for 15 minutes
RATE_LIMIT_LOGIN = "5/minute"    # per IP address
RATE_LIMIT_REFRESH = "10/minute" # per IP address
```

**Progressive lockout logic:**
- Attempts 1–4: return generic error (do not reveal which field is wrong)
- Attempt 5: lock account for 15 minutes, log `account_locked`, notify admin
- IP rate limit: 5 login requests/minute → 429 Too Many Requests

### 4.2 Security Headers (applied globally)

```python
headers = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "  # OHIF requires this
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self';"
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()"
}
```

### 4.3 Refresh Token Theft Detection

```python
async def rotate_refresh_token(
    old_token: str,
    db: AsyncSession
) -> TokenPair:
    session = await db.get_session_by_token_hash(sha256(old_token))

    if session is None:
        # Token does not exist — possible theft of already-used token
        # Revoke the entire family
        await db.revoke_session_family(family_id_from_token(old_token))
        raise TokenTheftDetectedException()

    if session.is_revoked:
        # Token was already used — definite theft
        await db.revoke_session_family(session.family_id)
        await audit_log.write("token_theft_detected", session.user_id)
        raise TokenTheftDetectedException()

    # Valid use — rotate
    await db.revoke_session(session.id, reason="rotated")
    return await issue_new_token_pair(session.user_id, session.family_id)
```

### 4.4 Password Policy

```
Minimum length:        12 characters
Required:              uppercase + lowercase + digit + special character
Prohibited:            username, email, common passwords (top-10000 list)
History:               cannot reuse last 5 passwords
Expiry:                180 days (configurable per tenant)
Bcrypt cost:           12 (recalibrate if hardware changes)
```

---

## 5. Licensing System

### 5.1 License Models

| Model | Description | Hardware Binding | Validity |
|-------|-------------|-----------------|---------|
| **Per Site** | One license for the entire facility | MAC + Domain + IP | Perpetual or dated |
| **Per Server** | One license per physical server | MAC + CPU + Motherboard | Perpetual or dated |
| **Annual Subscription** | Renewed every 12 months | MAC + CPU | Date-bound |
| **Trial** | Evaluation period | MAC | 30 days, non-renewable |

### 5.2 License Editions & Feature Flags

| Edition | Max Users | Max Studies/Year | Clinical Modules |
|---------|:---------:|:----------------:|:-----------------|
| **Clinic** | 5 | 10,000 | Basic viewer only |
| **Hospital** | 50 | 100,000 | RECIST, Lung-RADS, Agatston, PE Clot, Breast Density |
| **Enterprise** | Unlimited | Unlimited | All modules including TMTV, Brain Seg, advanced AI |

### 5.3 Cryptographic Stack

```
License File Encryption:   AES-256-GCM
License File Signing:      RSA-2048 with PSS padding + SHA-256
Key Storage:
    Private key  →  your server only (never leaves)
    Public key   →  embedded inside PAXS binary (obfuscated)
```

### 5.4 License Validation Sequence (on every startup)

```
Application starts
        │
        ▼
1. Load paxs.lic from LICENSE_FILE_PATH
        │
        ▼
2. Decrypt with embedded AES-256 key
        │
        ▼
3. Verify RSA-2048 signature with embedded public key
   → Any signature mismatch = INVALID (file tampered)
        │
        ▼
4. Verify machine fingerprint matches
   → Mismatch = HARDWARE_MISMATCH (license used on wrong machine)
        │
        ▼
5. Check expiry date (if applicable)
   → Expired = EXPIRED
        │
        ▼
6. Check usage quotas (users, studies)
   → Over quota = QUOTA_EXCEEDED
        │
        ▼
7. Online validation (async, non-blocking)
   → If offline: check grace period (7 days max)
        │
        ▼
License Status: VALID ✅ / INVALID ❌
```

### 5.5 Periodic Online Validation

```python
async def periodic_license_check() -> None:
    """Runs every 24 hours in a background task."""
    try:
        response = await httpx.AsyncClient().post(
            "https://elsamanii.rf.gd/api/license/validate",
            json={
                "license_id":   current_license.license_id,
                "fingerprint":  get_machine_fingerprint(),
                "paxs_version": APP_VERSION,
                "timestamp":    datetime.utcnow().isoformat(),
            },
            timeout=10.0,
            headers={"X-PAXS-Activation-Token": ACTIVATION_SECRET}
        )

        if response.status_code == 200:
            await save_last_valid_check(datetime.utcnow())

        elif response.status_code == 403:
            # License revoked by vendor
            await trigger_license_revoked()

        elif response.status_code == 402:
            # License expired — notify admin
            await notify_admin_license_expiring()

    except httpx.ConnectError:
        # No internet — apply grace period
        last_check = await get_last_valid_check()
        days_offline = (datetime.utcnow() - last_check).days

        if days_offline > settings.LICENSE_GRACE_PERIOD_DAYS:
            await trigger_grace_period_exceeded()
        else:
            logger.warning("LICENSE_OFFLINE_MODE", extra={
                "days_offline": days_offline,
                "grace_days_remaining": settings.LICENSE_GRACE_PERIOD_DAYS - days_offline
            })
```

---

## 6. Machine Fingerprinting

### 6.1 Data Collected

```python
import hashlib
import platform
import subprocess
import netifaces

def get_machine_fingerprint() -> str:
    """
    Builds a stable hardware fingerprint from multiple sources.
    Resistant to: MAC spoofing (uses all NICs), VM cloning,
    OS reinstall (uses motherboard serial).
    """
    components: list[str] = []

    # 1. All non-loopback MAC addresses (sorted for stability)
    for iface in sorted(netifaces.interfaces()):
        addrs = netifaces.ifaddresses(iface)
        if netifaces.AF_LINK in addrs:
            mac = addrs[netifaces.AF_LINK][0].get("addr", "")
            if mac and mac != "00:00:00:00:00:00":
                components.append(f"MAC:{mac.upper()}")

    if platform.system() == "Windows":
        # 2. CPU Processor ID
        try:
            cpu = subprocess.check_output(
                "wmic cpu get ProcessorId /value",
                shell=True, stderr=subprocess.DEVNULL
            ).decode().strip().split("=")[-1].strip()
            components.append(f"CPU:{cpu}")
        except Exception:
            pass

        # 3. Motherboard Serial Number
        try:
            mb = subprocess.check_output(
                "wmic baseboard get SerialNumber /value",
                shell=True, stderr=subprocess.DEVNULL
            ).decode().strip().split("=")[-1].strip()
            if mb and mb.lower() not in ("to be filled by o.e.m.", ""):
                components.append(f"MB:{mb}")
        except Exception:
            pass

        # 4. OS Installation Serial
        try:
            os_serial = subprocess.check_output(
                "wmic os get SerialNumber /value",
                shell=True, stderr=subprocess.DEVNULL
            ).decode().strip().split("=")[-1].strip()
            components.append(f"OS:{os_serial}")
        except Exception:
            pass

    elif platform.system() == "Linux":
        # 2. Machine ID (stable across reboots)
        try:
            with open("/etc/machine-id") as f:
                components.append(f"MACHINE_ID:{f.read().strip()}")
        except FileNotFoundError:
            pass

        # 3. DMI system UUID
        try:
            uuid = subprocess.check_output(
                ["cat", "/sys/class/dmi/id/product_uuid"],
                stderr=subprocess.DEVNULL
            ).decode().strip()
            components.append(f"DMI_UUID:{uuid}")
        except Exception:
            pass

    if not components:
        raise RuntimeError("Unable to collect any hardware identifiers")

    raw = "|".join(sorted(components))
    return hashlib.sha256(raw.encode()).hexdigest()[:32].upper()
```

### 6.2 Fingerprint Stability

| Event | Fingerprint Impact |
|-------|-------------------|
| Reboot | ✅ Stable |
| Network card change | ⚠️ Minor — other components compensate |
| RAM upgrade | ✅ Stable |
| OS reinstall | ❌ Changes — requires reactivation |
| VM clone | ❌ Changes — UUID differs |
| Hardware migration | ❌ Changes — requires reactivation |

---

## 7. License File Structure

### 7.1 Contents (before encryption)

```json
{
  "license_id":   "PAXS-HOSP-2026-A1B2-C3D4",
  "version":      "1.0",
  "customer": {
    "name":       "Khartoum Specialty Hospital",
    "email":      "it@hospital.sd",
    "country":    "SD"
  },
  "product":      "PAXS Archive & Viewer",
  "edition":      "hospital",
  "machine_fingerprint": "3A7F9C2E...",
  "issued_at":    "2026-01-01T00:00:00Z",
  "expires_at":   "2027-01-01T00:00:00Z",
  "max_users":    50,
  "max_studies_per_year": 100000,
  "features": [
    "recist",
    "lung_rads",
    "agatston",
    "pe_clot",
    "breast_density"
  ],
  "signature":    "<RSA-2048-PSS-SHA256-BASE64>"
}
```

### 7.2 File Generation (on your server)

```python
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import json, base64, os

def generate_license_file(
    license_data: dict,
    private_key_pem: bytes,
    aes_key: bytes          # 32 bytes, same key embedded in PAXS binary
) -> bytes:
    # Step 1: Sign the payload
    payload = json.dumps(license_data, sort_keys=True).encode()
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    signature = private_key.sign(
        payload,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )

    # Step 2: Add signature to payload
    signed = {**license_data, "signature": base64.b64encode(signature).decode()}
    signed_bytes = json.dumps(signed, sort_keys=True).encode()

    # Step 3: Encrypt with AES-256-GCM
    aesgcm = AESGCM(aes_key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, signed_bytes, None)

    # Step 4: Write: nonce (12 bytes) + ciphertext
    return nonce + ciphertext
```

### 7.3 File Validation (inside PAXS)

```python
def verify_license_file(
    license_path: str,
    aes_key: bytes,
    public_key_pem: bytes
) -> LicenseData:
    with open(license_path, "rb") as f:
        raw = f.read()

    # Decrypt
    nonce, ciphertext = raw[:12], raw[12:]
    aesgcm = AESGCM(aes_key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    except Exception:
        raise LicenseDecryptionError("License file is corrupted or tampered")

    signed = json.loads(plaintext)
    signature = base64.b64decode(signed.pop("signature"))
    payload   = json.dumps(signed, sort_keys=True).encode()

    # Verify RSA signature
    public_key = serialization.load_pem_public_key(public_key_pem)
    try:
        public_key.verify(
            signature, payload,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
    except Exception:
        raise LicenseSignatureError("License signature is invalid")

    return LicenseData(**signed)
```

---

## 8. Online Activation Portal

### 8.1 Activation Flow

```
Customer purchases license
        │
        ▼
Receives License Key by email:  PAXS-HOSP-XXXX-XXXX
        │
        ▼
Opens PAXS → Activation screen appears
        │
        ▼
Enters License Key
PAXS collects machine fingerprint automatically
        │
        ▼
POST https://elsamanii.rf.gd/api/activate
{
  "license_key":   "PAXS-HOSP-XXXX-XXXX",
  "fingerprint":   "3A7F9C2E...",
  "machine_info": {
    "hostname":    "HOSPITAL-SRV-01",
    "os":          "Windows Server 2022",
    "country":     "SD",
    "paxs_version": "2.0.0"
  }
}
        │
        ▼
Your server validates:
  ✅ Key exists in database
  ✅ Key not already activated on different machine
  ✅ Key not revoked
  ✅ Edition matches requested features
        │
        ▼
Generates signed + encrypted license file
        │
        ▼
Response: { "license_file": "<base64>" }
        │
        ▼
PAXS saves as paxs.lic → System unlocked ✅
```

### 8.2 Activation Portal Database (on your server)

```sql
CREATE TABLE license_keys (
    id                  INT          PRIMARY KEY AUTO_INCREMENT,
    license_key         VARCHAR(30)  UNIQUE NOT NULL, -- PAXS-HOSP-XXXX-XXXX
    edition             ENUM('clinic', 'hospital', 'enterprise', 'trial'),
    customer_name       VARCHAR(200),
    customer_email      VARCHAR(100),
    country             VARCHAR(5),
    status              ENUM('unused', 'active', 'revoked', 'expired')
                        DEFAULT 'unused',
    machine_fingerprint VARCHAR(64)  NULL,            -- set on first activation
    activated_at        DATETIME     NULL,
    expires_at          DATETIME     NULL,            -- NULL = perpetual
    max_users           INT          NOT NULL,
    max_studies_year    INT          NOT NULL,
    features            JSON         NOT NULL,
    allow_reactivation  BOOLEAN      DEFAULT FALSE,   -- set true when customer changes hardware
    notes               TEXT         NULL,
    created_at          DATETIME     DEFAULT NOW()
);

CREATE TABLE activation_log (
    id              BIGINT      PRIMARY KEY AUTO_INCREMENT,
    license_key     VARCHAR(30) NOT NULL,
    fingerprint     VARCHAR(64),
    ip_address      VARCHAR(45),
    machine_info    JSON,
    action          ENUM('activate', 'validate', 'revoke', 'reactivate'),
    result          VARCHAR(50),
    timestamp       DATETIME    DEFAULT NOW()
);
```

### 8.3 Activation API Endpoints (on your server)

```
POST   /api/activate              →  Activate a license key
POST   /api/license/validate      →  Periodic 24-hour check from PAXS
POST   /api/license/revoke        →  Immediate remote revocation (admin only)
GET    /api/license/{key}/status  →  Check license status (admin only)
```

### 8.4 Admin Control Panel (elsamanii.rf.gd/paxs-admin)

```
/paxs-admin/
    ├── /licenses              →  All licenses list with status
    ├── /licenses/new          →  Create new license key
    ├── /licenses/{id}         →  License details + activation history
    ├── /licenses/{id}/revoke  →  Immediate remote revocation
    ├── /licenses/{id}/reactivate → Allow hardware change reactivation
    ├── /activations           →  Full activation log (all customers)
    └── /stats                 →  Sales overview, active installs, expiring soon
```

---

## 9. End-User Flow

### 9.1 First-Time Setup

```
Install PAXS Archive
        │
        ▼
First launch → License screen
  ┌─────────────────────────────────┐
  │  PAXS Activation                │
  │                                 │
  │  License Key: [_____________]   │
  │                                 │
  │  [Activate Online]              │
  └─────────────────────────────────┘
        │
        ▼
Connects to elsamanii.rf.gd/api/activate
        │
        ▼
Downloads paxs.lic → Saves locally
        │
        ▼
Login screen appears
```

### 9.2 Daily Operation

```
User opens browser → /login
        │
        ▼
Enters credentials
        │
        ├── Success → Redirect by role
        └── Failure → Increment counter
                          │
                     5 failures → Lock + notify admin
        │
        ▼
Session active (Access Token 15min + Refresh 7 days)
        │
        ▼
Every 15 min: silent token refresh (invisible to user)
        │
        ▼
Every 24 hours: background license check
  ├── ✅ Valid        → Continue
  ├── ⚠️  No internet → Grace period warning (days remaining shown)
  ├── ❌ Revoked      → Session terminated, message shown
  └── ❌ Expired      → Read-only mode + renewal prompt
```

### 9.3 License States & System Behavior

| License State | System Behavior |
|---------------|----------------|
| `VALID` | Full operation |
| `EXPIRED` | Read-only: view existing studies, no new uploads |
| `GRACE_PERIOD` | Full operation with banner showing days remaining |
| `REVOKED` | Immediate shutdown, contact vendor message |
| `HARDWARE_MISMATCH` | Blocked, contact vendor for reactivation |
| `QUOTA_EXCEEDED` | New uploads blocked, existing data accessible |
| `NOT_FOUND` | Activation screen |

---

## 10. Implementation Roadmap

### Phase 1 — Authentication Core (Weeks 1–2)

```
Week 1:
  ✦ Database tables: users, sessions, auth_audit_log, tenants
  ✦ Password hashing with bcrypt (cost=12)
  ✦ JWT access token issuance and validation
  ✦ Refresh token with rotation
  ✦ POST /auth/login endpoint
  ✦ POST /auth/logout endpoint
  ✦ POST /auth/refresh endpoint
  ✦ GET  /auth/me endpoint

Week 2:
  ✦ Unified login page (HTML served by FastAPI)
  ✦ HttpOnly cookie for refresh token
  ✦ OHIF integration (cookie-gated /viewer/* routes)
  ✦ Role-based redirect after login
```

### Phase 2 — Access Control (Week 3)

```
  ✦ require_roles() dependency decorator
  ✦ require_valid_license() dependency decorator
  ✦ Archive route protection (redirect non-admins)
  ✦ Referring physician study filter
  ✦ Rate limiting on /auth/login (fastapi-limiter + Redis)
  ✦ Account lockout after 5 failed attempts
  ✦ Security headers middleware
```

### Phase 3 — Licensing (Weeks 4–5)

```
Week 4:
  ✦ Machine fingerprint collector (Windows + Linux)
  ✦ RSA-2048 key pair generation (your server)
  ✦ AES-256-GCM license file encryption/decryption
  ✦ License file signature generation and verification
  ✦ License validator with all status checks

Week 5:
  ✦ License validation middleware (runs on startup + every request)
  ✦ 24-hour periodic online check background task
  ✦ Grace period enforcement
  ✦ License status → system behavior mapping
  ✦ Activation screen UI
```

### Phase 4 — Activation Portal (Week 6)

```
  ✦ Activation portal database schema (on your server)
  ✦ POST /api/activate endpoint
  ✦ POST /api/license/validate endpoint
  ✦ POST /api/license/revoke endpoint
  ✦ Admin control panel (basic CRUD for license keys)
  ✦ Activation log viewer
  ✦ Email notification on activation / expiry warning
```

---

## Anti-Tamper Summary

| Attack Vector | Defense |
|---------------|---------|
| Copy `paxs.lic` to another machine | Machine fingerprint (MAC + CPU + Motherboard) |
| Change system clock to extend license | Server-side timestamp signing + online validation |
| Modify `paxs.lic` contents | RSA-2048 signature — any modification invalidates it |
| Disconnect from internet permanently | Grace period: 7 days max, then restricted mode |
| VM cloning | VM UUID changes → fingerprint changes → reactivation needed |
| OS reinstall | OS serial changes → fingerprint changes → reactivation needed |
| Reverse-engineer the binary | Public key only embedded, private key never leaves your server |
| Reuse activation token | `license_key` is single-use; second activation on different hardware requires your manual approval |
| Parallel installations | `license_key` locked to one fingerprint on first activation |
| Replay attack on validation API | Timestamp in every request + HTTPS + `X-PAXS-Activation-Token` header |

---

## Technology Stack Summary

```
Language:           Python 3.11+
Framework:          FastAPI (async)
Auth Libraries:     python-jose[cryptography], passlib[bcrypt]
Crypto:             cryptography (RSA-2048, AES-256-GCM)
Rate Limiting:      fastapi-limiter + Redis
Session Store:      Redis (refresh token lookup, blacklist)
Database:           MySQL (SQLAlchemy async)
Hardware Info:      netifaces, wmi (Windows), subprocess
HTTP Client:        httpx (async, for online validation)
Frontend Gate:      FastAPI serves OHIF static files after auth check
```

---

*PAXS Archive & Viewer — Authentication & Licensing Plan v1.0*
*Python / FastAPI / OHIF v3 — Medical PACS System*
