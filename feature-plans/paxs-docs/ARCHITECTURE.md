# المعمارية البرمجية والهندسية — PAXS Server

> معمارية الطبقات المعزولة (Clean Layered Architecture) لفصل معالجة بروتوكولات الشبكة الطبية عن واجهات الويب وقواعد البيانات.
>
> 📋 **مصفوفة حالة الـ 52 ميزة (41 ✅ · 1 🟡 · 10 ⬜):** راجع `feature-plans/plan.md` و `feature-plans/MASTER-ROADMAP.md`.

---

## 🏗️ المخطط المعماري الكامل للنظام

```
┌─────────────────────────────────────────────────────────────────┐
│                     LAYER 0 — الأجهزة الطبية                    │
│         [ CT ] [ MRI ] [ X-Ray ] [ Ultrasound ] [ CR ]          │
└───────────────────────────┬─────────────────────────────────────┘
                            │ DICOM C-STORE (Port 10400)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                LAYER 1 — DICOM Network Engine                    │
│                                                                  │
│   ┌──────────────────┐     ┌──────────────────────────────┐     │
│   │  C-STORE SCP     │     │  C-FIND SCP (Worklist/Query) │     │
│   │  (receiver.py)   │     │  (finder.py)                 │     │
│   └────────┬─────────┘     └──────────────────────────────┘     │
│            │                                                     │
│            ▼ (Raw .dcm file)                                     │
│   ┌──────────────────────────────────────────────────────┐      │
│   │      DICOM Parser & Metadata Extractor               │      │
│   │      (parser.py) — يستخرج كافة DICOM Tags           │      │
│   └──────────────────────────────────────────────────────┘      │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Async Task (Celery Queue)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│              LAYER 2 — Processing & Storage Pipeline             │
│                                                                  │
│   ┌─────────────────────────┐   ┌─────────────────────────────┐ │
│   │   Compression Worker    │   │   File Storage Service      │ │
│   │   JPEG-LS / HTJ2K       │   │   storage/{uid}/{uid}.dcm   │ │
│   │   (Celery + imagecodecs)│   │   Local FS / MinIO / S3     │ │
│   └─────────┬───────────────┘   └─────────────┬───────────────┘ │
│             │                                  │                 │
│             ▼                                  ▼                 │
│   ┌──────────────────────────────────────────────────────────┐  │
│   │         MySQL 8.0 Database (Async SQLAlchemy)            │  │
│   │   patients → studies → series → instances                │  │
│   └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼ HTTP/REST (Port 8080)
┌─────────────────────────────────────────────────────────────────┐
│              LAYER 3 — DICOMweb API Gateway (FastAPI)            │
│                                                                  │
│   GET  /dicom-web/studies              (QIDO-RS — Search)        │
│   GET  /dicom-web/studies/{uid}/...   (WADO-RS — Retrieve)      │
│   POST /dicom-web/studies             (STOW-RS — Store)         │
│   GET  /api/v1/share/{token}          (Patient Share Link)       │
│   POST /api/v1/auth/login             (JWT Authentication)       │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Nginx Reverse Proxy (SSL/TLS)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│              LAYER 4 — Presentation (Clients & Viewers)          │
│                                                                  │
│   [ OHIF Viewer v3 ]   [ Cornerstone.js ]   [ Mobile PWA ]      │
│   [ Radiology WS ]     [ AI Triage Dashboard ]                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🧩 المكونات الرئيسية — شرح تفصيلي

### 1. DICOM DIMSE Engine (`app/dicom/`)

| الملف | المسؤولية |
|-------|-----------|
| `receiver.py` | C-STORE SCP — يستقبل صور الأشعة من الأجهزة على المنفذ 10400 |
| `sender.py` | C-STORE SCU — يُعيد إرسال الصور لأجهزة أخرى أو PACS خارجي |
| `finder.py` | C-FIND SCP — يرد على استعلامات Worklist وقوائم المرضى |
| `parser.py` | يستخرج DICOM Tags ويحولها لـ Python dict جاهز للحفظ في MySQL |

### 2. Metadata Pipeline & Compression

```
[Raw .dcm arrives] → [parser.py extracts tags] → [DB write: patients/studies/series/instances]
                                                        ↓
                                         [Celery task queued]
                                                        ↓
                                   [compression_worker.py runs JPEG-LS]
                                                        ↓
                                   [DB updated: file_path + file_size_bytes]
```

### 3. DICOMweb REST API Layer

```python
# مثال: مسار QIDO-RS للبحث عن الفحوصات
GET /dicom-web/studies?PatientName=Ahmed&StudyDate=20240101-20241231

# مثال: مسار WADO-RS لجلب صورة مفردة
GET /dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}

# مثال: STOW-RS لرفع صور من عارض الصور
POST /dicom-web/studies
Content-Type: multipart/related; type="application/dicom"
```

### 4. Nginx Configuration (التدفق والأمان)

```nginx
server {
    listen 443 ssl;
    client_max_body_size 2G;          # لدعم رفع فحوصات كبيرة

    location /dicom-web/ {
        proxy_pass http://fastapi:8000;
        add_header Access-Control-Allow-Origin "*";   # CORS للـ OHIF
    }

    location / {
        root /usr/share/nginx/html;   # OHIF Viewer static files
    }
}
```

---

## 🔄 تدفق معالجة الصورة الطبية (End-to-End Flow)

```
1. جهاز CT يرسل فحصاً (200 صورة) عبر C-STORE DIMSE
   ↓
2. receiver.py يستقبل كل صورة .dcm ويحفظها على القرص مؤقتاً
   ↓
3. parser.py يستخرج: PatientID, StudyUID, SeriesUID, SOPInstanceUID, Modality...
   ↓
4. يكتب في MySQL: Patient → Study → Series → Instance (إن لم تكن موجودة)
   ↓
5. يضع مهمة Celery في Redis queue: "اضغط هذا الملف بـ JPEG-LS"
   ↓
6. Worker يضغط الملف، يحدث file_size_bytes وfile_path في DB
   ↓
7. OHIF Viewer يطلب الفحص عبر QIDO-RS → يجلب الصور عبر WADO-RS
   ↓
8. المريض يستلم رابط مشاركة مؤمن بـ OTP صالح 30 يوماً
```

---

## 🏛️ معمارية التخزين الهرمية (Smart Tiered Storage)

```
الفحوصات الواردة
       │
       ▼
┌─────────────────────────────────────────────┐
│   HOT STORAGE (NVMe/SSD)                   │
│   < 30 يوم | أسرع وصول | غير مضغوط         │
└────────────────────┬────────────────────────┘
                     │ بعد 30 يوم (Celery Task)
                     ▼
┌─────────────────────────────────────────────┐
│   WARM STORAGE (MinIO / Local NAS)          │
│   1 شهر – 1 سنة | مضغوط JPEG-LS Lossless   │
└────────────────────┬────────────────────────┘
                     │ بعد سنة (Archive Job)
                     ▼
┌─────────────────────────────────────────────┐
│   COLD STORAGE (S3 / Glacier)               │
│   > 1 سنة | أرخص تخزين | استرجاع عند الطلب │
└─────────────────────────────────────────────┘
```

---

## 🔐 طبقة الأمان (Security Architecture)

```
المستخدم (طبيب / مريض)
        │
        ▼
[JWT Token Auth] ← POST /auth/login
        │
        ▼
[Role-Based Access Control]
   ├── RADIOLOGIST  → قراءة + تقارير
   ├── PHYSICIAN    → قراءة فقط
   ├── ADMIN        → إدارة كاملة
   └── PATIENT      → رابط مؤقت محمي بـ OTP
        │
        ▼
[DICOM TLS] ← اتصال مشفر مع الأجهزة الطبية
[SSL/TLS Nginx] ← كل حركة HTTP مشفرة
[SHA-256 Hash] ← بصمة رقمية لكل صورة (Tamper Detection)
```

---

## ⚙️ متطلبات الخادم الموصى بها

| المورد | الحد الأدنى | الموصى به (إنتاج) |
|--------|-------------|------------------|
| CPU | 4 cores | 16 cores |
| RAM | 8 GB | 32 GB |
| تخزين OS | 50 GB SSD | 100 GB NVMe |
| تخزين DICOM | 1 TB | 10 TB+ (RAID/NAS) |
| شبكة | 1 Gbps | 10 Gbps |
| نظام التشغيل | Ubuntu 22.04 / Windows Server 2019 | Ubuntu 22.04 LTS |
