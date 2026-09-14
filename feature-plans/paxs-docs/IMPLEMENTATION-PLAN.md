# خطة التنفيذ التفصيلية — PAXS Server

> خطة هندسية تفصيلية لكل مرحلة بناء على طبقات واضحة ومعزولة.

---

## 📋 مصفوفة حالة الـ 52 ميزة (canonical — تحقّق 2026-08-06)

> ✅ Done · 🟡 Partial · ⬜ Planned — **41 ✅ · 1 🟡 · 10 ⬜**. التفاصيل في `plan.md` و `MASTER-ROADMAP.md`.

| # | الميزة | الحالة |
| :-: | :--- | :--- |
| 1 | C-STORE (SCP/SCU) | ✅ |
| 2 | C-FIND (SCP/SCU) | ✅ |
| 3 | C-MOVE (SCP/SCU) | ✅ |
| 4 | C-GET (SCP/SCU) | ✅ |
| 5 | C-ECHO (SCP/SCU) | ✅ |
| 6 | Storage Commitment | ✅ |
| 7 | Modality Worklist (MWL) | ✅ |
| 8 | MPPS | ✅ |
| 9 | IAN | ✅ |
| 10 | DICOM TLS | ✅ |
| 11 | Lossless Compression | ✅ |
| 12 | Lossy Compression | ✅ |
| 13 | VNA | ✅ |
| 14 | Study Lifecycle Management | ✅ |
| 15 | Auto-routing Rules Engine | ✅ |
| 16 | Prefetching | ✅ |
| 17 | DICOMDIR | ✅ |
| 18 | Migration Tools | ✅ |
| 19 | Transfer Accelerator | ✅ |
| 20 | WADO-URI | ✅ |
| 21 | WADO-RS / QIDO-RS / STOW-RS | ✅ |
| 22 | Hanging Protocols | ✅ |
| 23 | Prior Study Linking | ✅ |
| 24 | GSPS | ✅ |
| 25 | KIN | ✅ |
| 26 | 2D MPR | ✅ |
| 27 | 3D VRT / MIP | ✅ |
| 28 | Dose Monitoring (RDSR) | ✅ |
| 29 | Fusion | ✅ |
| 30 | Worklist Management UI | ⬜ |
| 31 | Clinical Protocols Library | ⬜ |
| 32 | Peer Review / QA | ✅ |
| 33 | Critical Results Tracking | ✅ |
| 34 | Reporting / Dictation Bridge | 🟡 (reporting + ORU done) |
| 35 | Chat & Collaboration | ⬜ |
| 36 | Teaching Files | ⬜ |
| 37 | Study Split/Merge | ✅ |
| 38 | HL7 ORU/ORM/ADT | ✅ |
| 39 | LDAP / Active Directory | ✅ |
| 40 | XDS-I | ✅ |
| 41 | REST API | ✅ |
| 42 | Webhooks | ✅ |
| 43 | Lua / Python Scripting | ⬜ |
| 44 | FHIR ImagingStudy | ✅ |
| 45 | Audit Logging (ATNA) | ✅ (SHA-256; ATNA XML ⬜) |
| 46 | RBAC | ✅ |
| 47 | Load Balancing | ⬜ |
| 48 | High Availability | ⬜ |
| 49 | DR Replication | ⬜ |
| 50 | Dashboard & Analytics | ✅ |
| 51 | Anonymization Engine | ✅ |
| 52 | Storage Tiering (HSM) | ✅ (via Lifecycle cold tier) |

---

## 📦 المرحلة الأولى: البيئة والنواة (Phase 1 — Foundation)
**المدة: الأسبوعان 1-2**

### 1.1 إعداد بيئة التطوير

```bash
# إنشاء مجلد المشروع والبيئة الافتراضية
mkdir paxs-python-server && cd paxs-python-server
python -m venv venv
source venv/bin/activate

# تثبيت المكتبات الأساسية
pip install fastapi uvicorn[standard] pydantic-settings \
            sqlalchemy[asyncio] alembic aiomysql \
            pydicom pynetdicom \
            celery redis \
            python-jose[cryptography] passlib[bcrypt] \
            imagecodecs openjph

pip freeze > requirements.txt
```

### 1.2 هيكل المجلدات الكامل

```
paxs-python-server/
├── app/
│   ├── __init__.py
│   ├── main.py                       # نقطة الدخول + startup events
│   ├── core/
│   │   ├── config.py                 # البيئة والإعدادات (pydantic-settings)
│   │   └── security.py               # JWT tokens, password hashing
│   ├── db/
│   │   ├── base.py                   # Base class للـ ORM
│   │   ├── session.py                # AsyncEngine + AsyncSession factory
│   │   └── models.py                 # ORM: Patient, Study, Series, Instance
│   ├── dicom/
│   │   ├── __init__.py
│   │   ├── receiver.py               # C-STORE SCP Server
│   │   ├── sender.py                 # C-STORE SCU Client
│   │   ├── finder.py                 # C-FIND SCP
│   │   └── parser.py                 # DICOM Tags → Python dict
│   ├── api/
│   │   ├── __init__.py
│   │   └── v1/
│   │       ├── dicomweb.py           # QIDO-RS, WADO-RS, STOW-RS
│   │       ├── auth.py               # Login / Token endpoints
│   │       └── share.py              # Patient share link endpoints
│   ├── services/
│   │   ├── storage_service.py        # حفظ .dcm على القرص
│   │   ├── compression_service.py    # JPEG-LS / HTJ2K worker
│   │   ├── sharing_service.py        # توليد QR + token
│   │   └── audit_service.py          # SHA-256 + audit log
│   └── workers/
│       └── celery_app.py             # Celery configuration
├── migrations/
│   └── env.py                        # Alembic env
├── tests/
│   ├── test_receiver.py
│   ├── test_dicomweb.py
│   └── test_compression.py
├── docker/
│   ├── Dockerfile
│   └── nginx.conf
├── docker-compose.yml
├── run_paxs.py                       # Entry point للـ EXE
├── alembic.ini
└── requirements.txt
```

### 1.3 الإعدادات (config.py)

```python
# app/core/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "mysql+aiomysql://paxs_user:password@localhost/paxs_db"

    # DICOM Server
    DICOM_AE_TITLE: str = "PAXS_SCP"
    DICOM_HOST: str = "0.0.0.0"
    DICOM_PORT: int = 10400

    # Storage
    STORAGE_ROOT: str = "./storage"
    STORAGE_BACKEND: str = "local"  # local | s3 | minio

    # Redis / Celery
    REDIS_URL: str = "redis://localhost:6379/0"

    # Security
    SECRET_KEY: str = "change-me-in-production"
    TOKEN_EXPIRE_MINUTES: int = 60

    # Compression
    DEFAULT_COMPRESSION: str = "JPEG-LS"  # JPEG-LS | JPEG2000 | HTJ2K

    class Config:
        env_file = ".env"

settings = Settings()
```

### 1.4 تصميم قاعدة البيانات وهجرات Alembic

```bash
# تهيئة Alembic
alembic init migrations

# إنشاء أول هجرة (migration)
alembic revision --autogenerate -m "create_paxs_core_tables"

# تطبيق على قاعدة البيانات
alembic upgrade head
```

---

## 📡 المرحلة الثانية: محرك DICOM الشبكي (Phase 2 — DICOM Engine)
**المدة: الأسبوعان 3-4**

### 2.1 خادم الاستقبال C-STORE SCP

```python
# app/dicom/receiver.py
from pynetdicom import AE, debug_logger
from pynetdicom.sop_class import StoragePresentationContexts
from app.dicom.parser import parse_dicom_file
from app.services.storage_service import save_dicom_file
from app.workers.celery_app import queue_compression_job
import asyncio

def handle_store(event):
    """معالج استقبال كل صورة DICOM واردة"""
    ds = event.dataset
    ds.file_meta = event.file_meta

    # 1. حفظ الملف على القرص
    file_path = save_dicom_file(ds)

    # 2. استخراج الميتاداتا
    metadata = parse_dicom_file(ds)

    # 3. حفظ في قاعدة البيانات (thread-safe call إلى async)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(save_metadata_to_db(metadata, file_path))
    loop.close()

    # 4. جدولة مهمة الضغط
    queue_compression_job.delay(metadata["sop_instance_uid"], file_path)

    return 0x0000  # Success status

def start_dicom_receiver(host: str = "0.0.0.0", port: int = 10400):
    ae = AE(ae_title="PAXS_SCP")
    ae.supported_contexts = StoragePresentationContexts

    handlers = [(evt.EVT_C_STORE, handle_store)]
    ae.start_server((host, port), block=True, evt_handlers=handlers)
```

### 2.2 محلل الميتاداتا (parser.py)

```python
# app/dicom/parser.py
import pydicom
from pydicom.dataset import Dataset

DICOM_TAG_MAP = {
    "PatientID":          (0x0010, 0x0020),
    "PatientName":        (0x0010, 0x0010),
    "PatientBirthDate":   (0x0010, 0x0030),
    "PatientSex":         (0x0010, 0x0040),
    "StudyInstanceUID":   (0x0020, 0x000D),
    "StudyDate":          (0x0008, 0x0020),
    "StudyTime":          (0x0008, 0x0030),
    "AccessionNumber":    (0x0008, 0x0050),
    "StudyDescription":   (0x0008, 0x1030),
    "SeriesInstanceUID":  (0x0020, 0x000E),
    "Modality":           (0x0008, 0x0060),
    "SeriesNumber":       (0x0020, 0x0011),
    "SeriesDescription":  (0x0008, 0x103E),
    "SOPInstanceUID":     (0x0008, 0x0018),
    "SOPClassUID":        (0x0008, 0x0016),
    "InstanceNumber":     (0x0020, 0x0013),
}

def parse_dicom_file(ds: Dataset) -> dict:
    result = {}
    for key, tag in DICOM_TAG_MAP.items():
        elem = ds.get(tag)
        result[key] = str(elem.value) if elem else None
    return result
```

### 2.3 خدمة التخزين على القرص

```python
# app/services/storage_service.py
import os
from pathlib import Path
from app.core.config import settings

def save_dicom_file(ds) -> str:
    """
    يحفظ ملف DICOM بمسار منظم:
    storage/{StudyUID}/{SeriesUID}/{SOPUID}.dcm
    """
    study_uid  = str(ds.StudyInstanceUID)
    series_uid = str(ds.SeriesInstanceUID)
    sop_uid    = str(ds.SOPInstanceUID)

    dir_path = Path(settings.STORAGE_ROOT) / study_uid / series_uid
    dir_path.mkdir(parents=True, exist_ok=True)

    file_path = dir_path / f"{sop_uid}.dcm"
    ds.save_as(str(file_path), write_like_original=False)

    return str(file_path)
```

---

## 🌐 المرحلة الثالثة: DICOMweb API + OHIF (Phase 3 — Web Layer)
**المدة: الأسبوعان 5-6**

### 3.1 مسارات QIDO-RS (البحث)

```python
# app/api/v1/dicomweb.py
from fastapi import APIRouter, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.db import models

router = APIRouter(prefix="/dicom-web")

@router.get("/studies", tags=["QIDO-RS"])
async def search_studies(
    PatientName: str | None = Query(None),
    StudyDate:   str | None = Query(None),
    Modality:    str | None = Query(None),
    limit: int  = Query(100, le=1000),
    db: AsyncSession = Depends(get_db)
):
    """QIDO-RS: بحث عن الفحوصات وإرجاع DICOM JSON"""
    # ... query DB + format as DICOM PS3.18 JSON
    pass

@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}",
            tags=["WADO-RS"])
async def retrieve_instance(study_uid: str, series_uid: str, sop_uid: str):
    """WADO-RS: جلب صورة DICOM لعارض OHIF"""
    # ... locate file + stream as multipart/related
    pass

@router.post("/studies", tags=["STOW-RS"])
async def store_instances():
    """STOW-RS: قبول رفع صور من عارضات الصور"""
    pass
```

### 3.2 ضغط الصور الطبية في الخلفية (Celery Worker)

```python
# app/services/compression_service.py
import pydicom
from pydicom.uid import JPEGLSLossless, JPEG2000Lossless, HTJ2KLossless
import os

COMPRESSION_MAP = {
    "JPEG-LS":    JPEGLSLossless,
    "JPEG2000":   JPEG2000Lossless,
    "HTJ2K":      HTJ2KLossless,
}

def compress_dicom_lossless(file_path: str, method: str = "JPEG-LS") -> dict:
    ds = pydicom.dcmread(file_path)
    original_size = os.path.getsize(file_path)

    target_uid = COMPRESSION_MAP.get(method, JPEGLSLossless)
    ds.compress(target_uid)
    ds.save_as(file_path)   # استبدال الملف في نفس المسار

    compressed_size = os.path.getsize(file_path)
    ratio = ((original_size - compressed_size) / original_size) * 100

    return {
        "original_bytes":   original_size,
        "compressed_bytes": compressed_size,
        "saved_percent":    round(ratio, 1),
    }
```

### 3.3 نظام المشاركة الذكي

```python
# app/services/sharing_service.py
import secrets
import qrcode
from datetime import datetime, timedelta
from io import BytesIO
import base64

def generate_patient_share_link(study_uid: str, expiry_days: int = 30) -> dict:
    token      = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(days=expiry_days)
    share_url  = f"https://paxs.hospital.com/view?token={token}"

    # توليد QR Code
    qr = qrcode.make(share_url)
    buf = BytesIO()
    qr.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return {
        "token":      token,
        "url":        share_url,
        "expires_at": expires_at.isoformat(),
        "qr_base64":  qr_b64,
    }
```

---

## 🔐 المرحلة الرابعة: الأمان والإنتاج (Phase 4 — Production Ready)
**المدة: الأسبوعان 7-8**

### 4.1 قائمة مهام هذه المرحلة

```
[ ] JWT Authentication — تسجيل دخول + صلاحيات
[ ] DICOM TLS — تشفير اتصالات C-STORE
[ ] CORS configuration في Nginx لـ OHIF
[ ] Rate Limiting (slowapi)
[ ] SHA-256 Tamper Detection لكل صورة
[ ] MinIO / S3 كـ Storage Backend بديل
[ ] AI Triage — تكامل MONAI (اختياري)
[ ] Docker Compose FullStack
[ ] PyInstaller Windows EXE Build
[ ] NSSM Windows Service Installer
[ ] Integration Tests (pytest-asyncio)
[ ] Load Test: 500 صورة متزامنة
```

### 4.2 Docker Compose Setup

```yaml
# docker-compose.yml
version: "3.9"
services:

  paxs-db:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: root_secret
      MYSQL_DATABASE: paxs_db
      MYSQL_USER: paxs_user
      MYSQL_PASSWORD: PaxsUserPassword2026!
    ports: ["3306:3306"]
    volumes: ["paxs-mysql-data:/var/lib/mysql"]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  paxs-api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]
    volumes: ["./storage:/app/storage"]
    depends_on: [paxs-db, redis]

  paxs-dicom:
    build: .
    command: python -m app.dicom.receiver
    ports: ["10400:10400"]
    depends_on: [paxs-db, redis]

  paxs-worker:
    build: .
    command: celery -A app.workers.celery_app worker --loglevel=info
    depends_on: [paxs-db, redis]

  nginx:
    image: nginx:alpine
    ports: ["80:80", "443:443"]
    volumes: ["./docker/nginx.conf:/etc/nginx/nginx.conf"]
    depends_on: [paxs-api]

volumes:
  paxs-mysql-data:
```

---

## 🪟 المرحلة الخامسة: التوزيع (Phase 5 — Distribution)
**خاصة بنشر Windows EXE في المستشفيات**

### 5.1 نقطة الدخول الموحدة

```python
# run_paxs.py
import sys, os, threading, uvicorn
from app.main import app
from app.dicom.receiver import start_dicom_receiver

def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

if __name__ == "__main__":
    BASE_DIR = get_base_dir()
    print(f"🚀 PAXS Server starting from: {BASE_DIR}")

    # تشغيل مستقبل DICOM في Thread منفصل
    t = threading.Thread(
        target=start_dicom_receiver,
        kwargs={"host": "0.0.0.0", "port": 10400},
        daemon=True
    )
    t.start()
    print("📡 DICOM SCP running on port 10400")

    # تشغيل FastAPI
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

### 5.2 أمر البناء

```cmd
pyinstaller --noconfirm --onedir --console --name "PAXS-Server" ^
  --collect-all imagecodecs ^
  --collect-all pydicom ^
  --collect-all pynetdicom ^
  --collect-all openjph ^
  run_paxs.py
```

### 5.3 تثبيت Windows Service

```cmd
nssm install PAXSServer "C:\dist\PAXS-Server\PAXS-Server.exe"
nssm set PAXSServer Start SERVICE_AUTO_START
nssm set PAXSServer AppDirectory "C:\dist\PAXS-Server"
net start PAXSServer
```
