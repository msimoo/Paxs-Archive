إليك **خطة العمل التنفيذية الكاملة والمعدلة تجارياً** لبناء نظام **PAXS Archive**، مقسمة إلى 7 مراحل متتالية ومجهزة بـ **أوامر برومبتات دقيقة وقابلة للنسخ واللصق مباشرة إلى DeepSeek V4**.

تم تصميم هذه الخطة لتبدأ ببناء **النواة التجارية عالية الربحية والسرعة (MVP Core)**، ثم معالجة الأداء والأمان والأدوات السريرية الحرجة (Clinical Pack 2.5)، وصولاً إلى التعبئة والتصقيل.

---

## 🗺️ خارطة الطريق التنفيذية (Execution Roadmap)

```
[المرحلة 1: النواة واستقبال DICOM اللحظي] ──> [المرحلة 2: قاعدة البيانات وسجل الامتثال Audit Trail]
                                                                  │
                                                                  ▼
[المرحلة 4: الحزمة السريرية الخاطفة 2.5]  <── [المرحلة 3: قائمة العمل الذكية والتقارير المنهجية]
         │
         ▼
[المرحلة 5: ترقية paxs viewer والنمط الموفق Offline-First]
         │
         ▼
[المرحلة 6: التعبئة، التعديل والتجميع (.exe + Installer)]

```

---

## 1️⃣ المرحلة الأولى: الهيكلة والنواة عالية الأداء (Zero-Lag Core)

### 🎯 الهدف:

فصل استقبال شبكة DICOM (`pynetdicom`) عن معالجة البيانات، باستخدام **FastAPI + Celery + Redis** لتنفيذ الاستقبال خلال أقل من 20ms.

#### 📋 البرومبت الأول لـ DeepSeek V4:

```text
Role: Senior Python Systems Architect specializing in High-Throughput DICOM Systems.

Context:
I am building "paxs archive" using Python (FastAPI, pynetdicom, pydicom), Celery, and Redis.
Architecture Principle: Zero-Lag Core Ingestion (Core Service Isolation).
C-STORE network reception must NOT block or perform heavy processing. It must save the file to NVMe disk, index minimal metadata, emit a non-blocking Celery event, and close the socket in <20ms.

Task:
Write production-ready Python code for Phase 1:
1. `config.py`: Central settings using pydantic-settings managing AE_TITLE, DICOM_PORT (104/11112), STORAGE_PATH, REDIS_URL, and DB_URL.
2. `dicom_server/store_scp.py`: A non-blocking `pynetdicom` C-STORE SCP listener. Upon receiving a dataset:
   - Generate a deterministic file path: `STORAGE_PATH/{StudyInstanceUID}/{SeriesInstanceUID}/{SOPInstanceUID}.dcm`.
   - Write file to disk using `pydicom`.
   - Push a background task `process_dicom_ingress.delay(file_path, study_uid)` to Celery via Redis.
   - Return C-STORE Status `0x0000` (Success) immediately.
3. `celery_app/celery.py` & `celery_app/tasks.py`: Celery instance configuration and the base worker task handling initial file verification.

Ensure full type hints, explicit error handling, and thread safety.

```

---

## 2️⃣ المرحلة الثانية: قاعدة البيانات السريعة وسجل الامتثال (Database & Audit Trail)

### 🎯 الهدف:

بناء قاعدة البيانات (MySQL 8.4 / PostgreSQL) مع دعم تقسيم البيانات (Partitioning) وبناء محرك **HIPAA/GDPR Audit Trail Engine** لتسجيل جميع الأنشطة.

#### 📋 البرومبت الثاني لـ DeepSeek V4:

```text
Role: Senior Database Administrator & Healthcare Security Specialist.

Context:
Project: "paxs archive" backend.
Database Choice: MySQL 8.4 or PostgreSQL. Need efficient schema for Study/Series/Instance metadata alongside an immutable HIPAA/GDPR Audit Trail.

Task:
Provide complete Python (SQLAlchemy ORM + Alembic) and SQL DDL code:
1. ORM Models:
   - `Patient`: PatientID (Primary Key), PatientName, BirthDate, Sex.
   - `Study`: StudyInstanceUID (PK), AccessionNumber, StudyDate, StudyDescription, Modality, PatientID (FK). Partitioned by `StudyDate` range or indexed heavily.
   - `Series`: SeriesInstanceUID (PK), SeriesNumber, Modality, SeriesDescription, StudyInstanceUID (FK).
   - `Instance`: SOPInstanceUID (PK), InstanceNumber, StoragePath, TransferSyntaxUID, SeriesInstanceUID (FK).
   - `AuditLog`: id (PK), timestamp, user_id, user_ip, patient_id, study_uid, action_type (VIEW, EXPORT, DELETE, MODIFY, C-STORE), status, details_json.
2. `database/audit_logger.py`: Asynchronous non-blocking Audit Trail logger that writes security logs directly to the `AuditLog` table without slowing down API endpoints.
3. Database connection pool setup optimized for high-concurrency writes.

```

---

## 3️⃣ المرحلة الثالثة: قائمة العمل الذكية والتقارير المنهجية (Commercial Workflow Core)

### 🎯 Objetivo:

بناء **Smart Priority Worklist** ترتب الفحوصات بحسب الأولوية ووقت الانتظار، ونظام **Multilingual Structured Reporting Engine**، ونافذة **Critical Value Alert Gateway**.

#### 📋 البرومبت الثالث لـ DeepSeek V4:

```text
Role: Medical Workflow & DICOMweb API Integration Engineer.

Context:
Project: "paxs archive" API layer using FastAPI.

Task:
Provide complete FastAPI route modules for:

Module 1: Smart Priority Worklist (`/api/v1/worklist`)
- Endpoint fetching active studies.
- Dynamically calculate a `PriorityScore`:
  Score = (UrgencyWeight [Emergency=100, Stat=50, Routine=10]) + (WaitTimeInMinutes * 1.5)
- Support QIDO-RS parameters (`PatientID`, `PatientName`, `Modality`, `StudyDate`).

Module 2: Multilingual Structured Reporting Engine (`/api/v1/reports`)
- CRUD endpoints for diagnostic reports.
- Support JSON templates for Arabic and English reports.
- Auto-populate measurements (e.g., tumor diameter, HU values) directly from `paxs viewer` payloads into the report fields.

Module 3: Critical Value Alert Gateway (`/api/v1/alerts`)
- Trigger instant alert events (SMS / Push / WhatsApp API webhook) when an AI or doctor flags a critical finding (e.g., Acute PE, Brain Hemorrhage, Pneumothorax).
- Log the notification event in the Audit Trail.

```

---

## 4️⃣ المرحلة 2.5: الحزمة السريرية الخاطفة (Commercial Clinical Pack)

### 🎯 الهدف:

تنفيذ الخمس موديولات السريرية الأكثر طلباً في السوق كمهام خلفية عبر Celery دون إبطاء السيرفر.

#### 📋 البرومبت الرابع لـ DeepSeek V4:

```text
Role: Senior Medical Image Analytics & AI Engineer.

Context:
System: "paxs archive" background workers (Celery + PyTorch/SimpleITK/pydicom).
Goal: Implement the "Phase 2.5 Commercial Clinical Pack" running asynchronously upon study arrival.

Task:
Write modular Python code (`clinical_pack/`) for the following 5 clinical engines:

1. `aspects_stroke.py`: Analyze non-contrast head CT series -> compute 10-point ASPECTS score for early ischemic stroke detection.
2. `pe_clot_burden.py`: Analyze CT Pulmonary Angiography (CTPA) -> calculate Qanadli Obstruction Index and RV/LV diameter ratio.
3. `agatston_calcium.py`: Analyze non-gated cardiac/chest CT -> detect coronary calcifications and output Agatston Score.
4. `lung_rads.py`: Track 3D pulmonary nodule volume across prior studies, calculate Volume Doubling Time (VDT) in days, and output Lung-RADS category.
5. `recist_tracker.py`: Compare target tumor lesion diameters across time series and classify response (Complete Response, Partial Response, Stable Disease, Progressive Disease).

Ensure all scripts output structured JSON results saved to MySQL and formatted for rendering inside `paxs viewer`.

```

---

## 5️⃣ المرحلة الخامسة: ترقية paxs viewer وتكنولوجيا Offline-First

### 🎯 الهدف:

ترقية الواجهة لتشغيل **WebGPU 3D Rendering** ودعم نمط **Offline-First PWA** باستغلال `IndexedDB`.

#### 📋 البرومبت الخامس لـ DeepSeek V4:

```text
Role: Lead Frontend & Medical Imaging Engineer (OHIF v3 / Cornerstone3D / TypeScript).

Context:
Application: "paxs viewer" (Customized OHIF v3 build).

Task:
Write JavaScript/TypeScript code for two core frontend extensions:

Module 1: Offline-First PWA Vault (`src/offline/pwaVault.ts`)
- Use `IndexedDB` and Service Workers to cache recent DICOM series and metadata locally.
- Allow the radiologist to open, view, and draft reports for cached studies when internet or LAN connection is offline.
- Auto-sync draft reports and audit logs back to the Python backend upon reconnection.

Module 2: Client-Side WebGPU Render Bridge (`src/rendering/webgpuBridge.ts`)
- Offload 3D Volume Rendering and Multi-Planar Reconstruction (MPR) to the client browser's GPU via WebGPU.
- Receive raw pixel array buffers via WADO-RS, feed into WebGPU compute shaders, and render at 120 FPS without consuming server GPU.

```

---

## 6️⃣ المرحلة السادسة: أتمتة البناء والتجميع (Executable & Installer)

### 🎯 الهدف:

أتمتة تنظيف الكود، حزمه عبر **PyInstaller**، وإنشاء برنامج تثبيت **Inno Setup** يُصغى كـ Windows Service.

#### 📋 البرومبت السادس لـ DeepSeek V4:

```text
Role: Windows Desktop Packaging & DevOps Engineer.

Context:
Application: "paxs archive" (Python FastAPI + pynetdicom + Celery + embedded paxs viewer static files).

Task:
Provide complete build automation scripts:

1. `build_release.py`:
   - Clean temporary development files (`__pycache__`, `.pytest_cache`, `.env`, temporary DICOMs).
   - Verify production `requirements.txt`.
   - Copy clean code into `dist_release/` folder structure (`bin/`, `config/`, `web/`, `logs/`, `docs/`).

2. `paxs_archive.spec` (PyInstaller):
   - Package the FastAPI application, pynetdicom listeners, and C-extensions (`pydicom`, `numpy`, `mysql.connector`/`psycopg2`).
   - Bundle static production build files of `paxs viewer`.
   - Output as `onedir` for optimal execution speed.

3. `installer_setup.iss` (Inno Setup Script):
   - Create Windows installer for `C:\Program Files\PAXS Archive`.
   - Automatically register `paxs_archive.exe` as a Windows Service (using NSSM or native wrapper) running on system startup.
   - Configure Windows Firewall rules for DICOM Port (104) and Web Port (8080).

```

---

## 💡 نصائح تنفيدية إستراتيجية للتطبيق مع DeepSeek V4
  