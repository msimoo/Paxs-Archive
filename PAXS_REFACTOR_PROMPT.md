# PAXS Archive — Master Refactor Prompt for DeepSeek

> **Purpose:** Complete, production-grade refactor of the PAXS Archive project (Python/FastAPI DICOM medical archiving system) without breaking any existing functionality.

---

## Role & Objective

You are a senior Python engineer specializing in FastAPI and medical PACS systems.

Your mission is to perform a thorough, precise refactor of the PAXS Archive codebase. You must improve code quality, structure, error handling, and security — without breaking any existing feature, endpoint, or DICOM capability.

---

## Phase 1: Discovery & Analysis

Before touching any code, perform a full audit of the project.

### 1.1 Build a Project Map

Read every file and produce:

- A complete file tree with paths
- A list of every API endpoint (method + path + handler)
- A list of every database model and its relationships
- A list of every external dependency in `requirements.txt`
- A list of every DICOM SOP class and service (C-STORE, C-FIND, C-MOVE, WADO-RS, STOW-RS, QIDO-RS, MWL)

### 1.2 Bug Detection Checklist

Search for and log every instance of the following before making any fix:

**Runtime Bugs**
- [ ] Unhandled exceptions without try/except
- [ ] Unguarded `None` values before attribute access
- [ ] Race conditions in async operations
- [ ] Unclosed file handles or database connections
- [ ] Memory leaks in long-running DICOM handlers

**DICOM-Specific Bugs**
- [ ] Incorrect Transfer Syntax handling
- [ ] `pydicom` datasets not closed after reading
- [ ] Missing validation of required DICOM tags before processing
- [ ] Incorrect DIMSE status codes returned to calling AE

**Security Issues**
- [ ] Hardcoded passwords, secrets, or API keys
- [ ] SQL injection risk in raw queries
- [ ] Debug mode or verbose error details exposed in responses
- [ ] Sensitive data (passwords, tokens, PHI) written to logs

**Code Quality Issues**
- [ ] Duplicate logic repeated across multiple files
- [ ] Functions exceeding 50 lines (candidates for decomposition)
- [ ] Magic numbers or magic strings without named constants
- [ ] Circular imports
- [ ] Missing or incomplete type hints
- [ ] Inconsistent naming conventions (mixed `snake_case` / `camelCase`)

### 1.3 Pre-Refactor Analysis Report

Before writing any code, output a report in this format:

```
ANALYSIS REPORT
===============
Critical Issues  (block operation):     X issues
Moderate Issues  (degrade performance): X issues
Minor Issues     (code quality):        X issues
Proposed Improvements:                  X items

Issue List:
[CRITICAL-01] <file>:<line> — <description>
[MODERATE-01] <file>:<line> — <description>
[MINOR-01]    <file>:<line> — <description>
```

Do not proceed to Phase 2 until this report is complete.

---

## Phase 2: Target Project Structure

Reorganize the project into this exact layout:

```
paxs_archive/
│
├── app/
│   ├── __init__.py
│   ├── main.py                        # FastAPI app, lifespan, global middleware
│   ├── config.py                      # All settings loaded from .env via pydantic
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── router.py              # Aggregates all v1 routers
│   │       ├── dependencies.py        # Shared FastAPI dependencies
│   │       └── endpoints/
│   │           ├── studies.py         # Study-level DICOM endpoints
│   │           ├── series.py          # Series endpoints
│   │           ├── instances.py       # Instance / image endpoints
│   │           ├── wado.py            # WADO-RS retrieve endpoints
│   │           ├── stow.py            # STOW-RS store endpoints
│   │           ├── qido.py            # QIDO-RS query endpoints
│   │           ├── worklist.py        # Modality Worklist (MWL) endpoints
│   │           ├── auth.py            # Login / logout / refresh / me
│   │           └── health.py          # Health check endpoint
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── security.py                # JWT creation/validation, password hashing
│   │   ├── exceptions.py              # Custom exception classes + FastAPI handlers
│   │   ├── constants.py               # DICOM tags, enums, all magic values
│   │   └── logging.py                 # Structured JSON logging configuration
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── session.py                 # SQLAlchemy async engine + session factory
│   │   ├── base.py                    # Declarative base with common columns
│   │   └── migrations/                # Alembic migration scripts
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   ├── user.py                    # User, Session, AuditLog
│   │   ├── study.py                   # Study, Series, Instance
│   │   ├── worklist.py                # MWL items
│   │   └── license.py                 # License record
│   │
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── auth.py                    # LoginRequest, TokenResponse, UserOut
│   │   ├── study.py                   # DICOM metadata Pydantic schemas
│   │   └── common.py                  # Shared pagination / error response schemas
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── dicom/
│   │   │   ├── __init__.py
│   │   │   ├── store.py               # C-STORE / STOW-RS business logic
│   │   │   ├── query.py               # C-FIND / QIDO-RS business logic
│   │   │   ├── retrieve.py            # WADO-RS / C-MOVE / C-GET business logic
│   │   │   └── parser.py              # DICOM file parsing and tag extraction
│   │   ├── storage/
│   │   │   ├── __init__.py
│   │   │   ├── manager.py             # Storage tiering (NVMe → HDD → Cold)
│   │   │   └── cache.py               # Redis cache service wrapper
│   │   ├── auth_service.py            # Authentication and session business logic
│   │   └── license_service.py         # License validation and enforcement
│   │
│   └── utils/
│       ├── __init__.py
│       ├── dicom_utils.py             # DICOM helper functions
│       └── file_utils.py              # File I/O helpers
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                    # Fixtures, test DB, test client
│   ├── test_auth.py
│   ├── test_stow.py
│   ├── test_wado.py
│   └── test_qido.py
│
├── .env.example                       # Template — never include real secrets
├── .env                               # Local secrets — never commit to git
├── .gitignore
├── requirements.txt
├── requirements-dev.txt
├── alembic.ini
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Phase 3: Refactoring Rules

Apply every rule below without exception.

---

### Rule 1 — Never Break Existing Functionality

- Every existing endpoint must remain accessible at the same URL path.
- Every supported DICOM SOP class must remain supported.
- Every DIMSE status code must remain correct.
- Response formats must not change unless the current format is a confirmed bug.
- If you are unsure whether a change is safe, add a `# TODO: verify` comment and leave it unchanged.

---

### Rule 2 — Comprehensive Error Handling

Every service function must follow this pattern:

```python
async def process_dicom_store(dataset: Dataset) -> StoreResult:
    """
    Stores a DICOM dataset in the archive.

    Args:
        dataset: A pydicom Dataset object received from C-STORE or STOW-RS.

    Returns:
        StoreResult containing status and SOPInstanceUID.

    Raises:
        DICOMValidationError: If required tags are missing or invalid.
        StorageError: If the file cannot be written to disk.
        DatabaseError: If metadata cannot be saved to the database.
    """
    if dataset is None:
        raise DICOMValidationError("Dataset cannot be None")

    try:
        _validate_required_tags(dataset)
        file_path = await _save_to_storage(dataset)
        await _save_metadata_to_db(dataset, file_path)

        return StoreResult(
            status=StoreStatus.SUCCESS,
            sop_instance_uid=str(dataset.SOPInstanceUID)
        )

    except DICOMValidationError:
        raise  # Re-raise validation errors unchanged
    except OSError as e:
        logger.error("DICOM_STORE_STORAGE_FAILED", extra={
            "sop_instance_uid": str(dataset.SOPInstanceUID),
            "error": str(e)
        })
        raise StorageError(f"Failed to save DICOM file: {e}") from e
    except SQLAlchemyError as e:
        logger.error("DICOM_STORE_DB_FAILED", extra={
            "sop_instance_uid": str(dataset.SOPInstanceUID),
            "error": str(e)
        })
        raise DatabaseError(f"Failed to save metadata: {e}") from e
```

---

### Rule 3 — All Configuration from Environment Variables

No hardcoded values anywhere in the codebase. Every setting lives in `config.py`:

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Database
    DATABASE_URL: str
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20

    # DICOM Network
    DICOM_STORAGE_PATH: str = "/data/dicom"
    DICOM_AE_TITLE: str = "PAXS_ARCHIVE"
    DICOM_PORT: int = 11112

    # Security
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Redis
    REDIS_URL: str = "redis://localhost:6379"
    REDIS_TTL_SECONDS: int = 28800  # 8 hours (full radiologist shift)

    # Storage Tiering
    STORAGE_HOT_PATH: str = "/data/hot"
    STORAGE_WARM_PATH: str = "/data/warm"
    STORAGE_COLD_PATH: str = "/data/cold"
    STORAGE_HOT_MAX_GB: int = 500

    # License
    LICENSE_FILE_PATH: str = "./paxs.lic"
    LICENSE_CHECK_INTERVAL_HOURS: int = 24
    LICENSE_GRACE_PERIOD_DAYS: int = 7

    # Threading
    IO_THREAD_POOL_WORKERS: int = -1  # -1 = auto: min(64, cpu_count * 4)

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
```

---

### Rule 4 — Structured Logging

Every significant operation must be logged with structured fields, not plain strings:

```python
# Operation start
logger.info("DICOM_STORE_START", extra={
    "sop_class_uid": str(dataset.SOPClassUID),
    "modality": getattr(dataset, "Modality", "UNKNOWN"),
    "patient_id": getattr(dataset, "PatientID", "UNKNOWN"),
    "calling_ae": calling_ae_title,
})

# Operation success
logger.info("DICOM_STORE_SUCCESS", extra={
    "sop_instance_uid": str(dataset.SOPInstanceUID),
    "file_size_bytes": file_size,
    "storage_tier": "hot",
    "duration_ms": round(elapsed * 1000),
})

# Operation failure
logger.error("DICOM_STORE_FAILED", extra={
    "error_type": type(e).__name__,
    "error_detail": str(e),
    "sop_instance_uid": str(dataset.SOPInstanceUID),
})
```

Never log: passwords, JWT tokens, refresh tokens, or patient PHI beyond PatientID.

---

### Rule 5 — Full Type Hints

Every function must have complete type annotations:

```python
# ❌ Wrong
async def get_study(study_uid, db):
    ...

# ✅ Correct
async def get_study(
    study_uid: str,
    db: AsyncSession
) -> Study | None:
    ...
```

---

### Rule 6 — Named Constants, No Magic Values

```python
# ❌ Wrong
if dataset[0x0008, 0x0060].value == "CT":
    ...

# ✅ Correct — in constants.py
class DICOMTag:
    MODALITY            = (0x0008, 0x0060)
    PATIENT_ID          = (0x0010, 0x0020)
    STUDY_INSTANCE_UID  = (0x0020, 0x000D)
    SERIES_INSTANCE_UID = (0x0020, 0x000E)
    SOP_INSTANCE_UID    = (0x0008, 0x0018)
    SOP_CLASS_UID       = (0x0008, 0x0016)

class Modality:
    CT  = "CT"
    MR  = "MR"
    US  = "US"
    XA  = "XA"
    PT  = "PT"
    DX  = "DX"
    MG  = "MG"
    NM  = "NM"

class DIMSEStatus:
    SUCCESS              = 0x0000
    PENDING              = 0xFF00
    OUT_OF_RESOURCES     = 0xA700
    DATASET_DOES_NOT_MATCH_SOP_CLASS = 0xA900
    CANNOT_UNDERSTAND    = 0xC000

# ✅ In service code
if dataset[DICOMTag.MODALITY].value == Modality.CT:
    ...
```

---

### Rule 7 — File Size Limit

Any file exceeding 300 lines must be split into focused modules. Each module has one clear responsibility.

---

### Rule 8 — DICOM Handler Safety

`pynetdicom` handlers are the most sensitive part of the codebase. Apply these rules:

- Always return a valid DIMSE status code — never let a handler raise an unhandled exception to the network layer.
- Always close datasets after reading: use `dcmread()` inside a `with` block or call `dataset.clear()` after processing.
- Validate `SOPClassUID` and `SOPInstanceUID` exist before any processing.
- Log the calling AE title and IP on every received association.

---

## Phase 4: Deliverables

Deliver in this exact order:

### 4.1 `ANALYSIS_REPORT.md`
- Numbered list of all issues found
- For each: file, line number, severity, description, fix applied

### 4.2 Refactored Source Files
For each modified file:
1. State the filename
2. State what changed and why
3. Provide the complete new file content

End each file with:
```
✅ DONE | File: <name> | Changes: <count> | Tests: PASS / NEEDS_REVIEW
```

### 4.3 `MIGRATION_NOTES.md`
- Any new or renamed environment variables
- Any database schema changes with the corresponding Alembic migration script
- Step-by-step upgrade instructions from the old version

### 4.4 `tests/` — Critical Path Tests
Cover at minimum:
- Authentication: login, logout, token refresh, role enforcement
- STOW-RS: valid DICOM upload, missing tags, oversized payload
- WADO-RS: retrieve by study/series/instance UID
- QIDO-RS: patient search, date range filter
- License: valid license, expired license, hardware mismatch

---

## Additional Instructions

**Before starting:** Take a full backup.
```bash
cp -r paxs_archive paxs_archive_backup_$(date +%Y%m%d_%H%M%S)
```

**If you encounter code whose purpose is unclear:** Do not delete it.  
Add `# TODO: clarify purpose — left unchanged during refactor` and move on.

**If DeepSeek stops mid-task:** Resume with:
> "Continue the refactor from where you stopped. The last completed file was: `<filename>`"

**After the refactor, verify with:**
```bash
# Syntax check
python -m py_compile app/main.py

# Import check
python -c "from app.main import app; print('OK')"

# Run tests
pytest tests/ -v --tb=short

# Start server
uvicorn app.main:app --reload --port 8000
```

**Threading — auto-configure the thread pool:**
```python
import os
IO_THREAD_POOL_WORKERS = min(64, (os.cpu_count() or 4) * 4)
```

**Redis TTL — use shift-aware caching:**
```python
# Default: 8 hours (covers a full radiologist shift)
# Not 1 hour — radiologists reopen the same CT many times per shift
REDIS_TTL_SECONDS = 8 * 60 * 60
```

---

## Scope Boundaries

| In Scope | Out of Scope |
|----------|-------------|
| Refactoring all Python files | Changing DICOM protocol behavior |
| Fixing bugs and security issues | Modifying the database schema without a migration |
| Adding type hints and docstrings | Adding new clinical features |
| Splitting oversized files | Changing existing API response formats |
| Improving error handling | Upgrading major dependency versions |
| Structured logging | Rewriting the frontend / OHIF viewer |

---

*PAXS Archive — Master Refactor Prompt v1.0*  
*Generated for DeepSeek V3 / R1 — Python/FastAPI DICOM System*
