# PAXS Server — Python Enterprise DICOM & PACS Platform

> نظام إدارة وأرشفة وعرض صور الأشعة الطبية (PACS) مبني كلياً بـ Python، مفتوح المصدر، مصمم للأداء العالي والتكامل مع المستشفيات.

---

## 🎯 ما هو PAXS Server؟

PAXS Server هو منصة PACS (Picture Archiving and Communication System) من الجيل الجديد مكتوبة بالكامل بلغة Python. يستقبل صور الأشعة الطبية من أجهزة CT وMRI والأشعة التقليدية، يفهرسها في قاعدة بيانات MySQL، ويخدمها عبر واجهات DICOMweb القياسية لعارض الصور المدمج PaxsViewer (فرع OHIF v3) عند /viewer.

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
| 26 | 2D MPR | ⬜ |
| 27 | 3D VRT / MIP | ⬜ |
| 28 | Dose Monitoring (RDSR) | ✅ |
| 29 | Fusion | ⬜ |
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

## 🚀 المميزات الرئيسية

| الميزة | التفاصيل |
|--------|----------|
| **خادم DICOM DIMSE** | يدعم C-STORE, C-FIND, C-GET, C-MOVE عبر `pynetdicom` على المنفذ 10400 |
| **معالج ميتاداتا متقدم** | استخراج وتحليل DICOM Tags وفهرستها في MySQL 8.0 |
| **DICOMweb REST API** | تغطية كاملة لـ QIDO-RS, WADO-RS, STOW-RS عبر FastAPI |
| **توافق عارضات الصور** | OHIF Viewer v3, Cornerstone.js |
| **ضغط Lossless** | JPEG-LS, JPEG 2000, HTJ2K في الخلفية عبر Celery |
| **توزيع Windows EXE** | تحزيم PyInstaller للنشر في المستشفيات |
| **نظام مشاركة ذكي** | QR Code ورابط مؤقت للمريض بدلاً من CD |
| **AI Triage** | اكتشاف الحالات الحرجة تلقائياً عبر MONAI |

---

## 🛠️ مكدس التقنيات (Tech Stack)

```
Backend:       Python 3.11+ | FastAPI | Uvicorn | AsyncIO
DICOM Engine:  pydicom | pynetdicom | imagecodecs (HTJ2K)
Database:      MySQL 8.0 | SQLAlchemy 2.0 Async | Alembic
Task Queue:    Celery + Redis
Proxy:         Nginx (SSL/TLS + CORS)
Viewer:        OHIF Viewer v3
AI:            MONAI | Whisper (Voice-to-Report)
DevOps:        Docker Compose | Helm Charts
Distribution:  PyInstaller (Windows EXE) | NSSM Service
```

---

## 📁 هيكل المجلدات

```
paxs-python-server/
├── app/
│   ├── main.py                    # نقطة الدخول الرئيسية
│   ├── core/
│   │   ├── config.py              # إعدادات البيئة
│   │   └── security.py            # JWT + TLS
│   ├── db/
│   │   ├── session.py             # Async DB session
│   │   └── models.py              # SQLAlchemy ORM models
│   ├── dicom/
│   │   ├── receiver.py            # C-STORE SCP Engine
│   │   ├── sender.py              # C-STORE SCU Engine
│   │   └── parser.py              # DICOM Tags extractor
│   ├── api/
│   │   └── v1/
│   │       ├── dicomweb.py        # WADO-RS & QIDO-RS endpoints
│   │       └── auth.py            # Authentication routes
│   └── services/
│       ├── storage_service.py     # ملفات DICOM على القرص
│       ├── compression_service.py # Lossless compression worker
│       └── sharing_service.py     # روابط مشاركة المريض
├── migrations/                    # Alembic migrations
├── tests/
├── docker/
├── docker-compose.yml
├── run_paxs.py                    # Entry point for EXE
└── requirements.txt
```

---

## ⚡ التشغيل السريع

```bash
# 1. استنساخ المستودع
git clone https://github.com/your-org/paxs-python-server.git
cd paxs-python-server

# 2. إنشاء البيئة الافتراضية
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. تثبيت الاعتمادات
pip install -r requirements.txt

# 4. تشغيل قواعد البيانات والـ Cache
docker-compose up -d paxs-db redis

# 5. تطبيق مخططات قاعدة البيانات
alembic upgrade head

# 6. تشغيل الخادم
python -m app.main
```

بعد التشغيل:
- **DICOM Receiver:** `0.0.0.0:10400` (DIMSE)
- **REST API & DICOMweb:** `http://localhost:8000`
- **API Docs:** `http://localhost:8000/docs`

---

## 🏥 النشر في بيئة Windows (EXE)

```cmd
pip install pyinstaller
pyinstaller --onedir --name "PAXS-Server" ^
  --collect-all imagecodecs ^
  --collect-all pydicom ^
  --collect-all pynetdicom ^
  run_paxs.py
```

لتثبيت كخدمة ويندوز تعمل تلقائياً عند الإقلاع:
```cmd
nssm install PAXSServer "C:\dist\PAXS-Server\PAXS-Server.exe"
nssm set PAXSServer Start SERVICE_AUTO_START
net start PAXSServer
```

---

## 📄 الوثائق

| الملف | المحتوى |
|-------|---------|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | المعمارية البرمجية والمخطط المعماري |
| [SCALABILITY.md](./SCALABILITY.md) | تحليل قابلية التوسع الرأسي والأفقي مع التوصيات |
| [STABILITY.md](./STABILITY.md) | تحليل الاستقرار والموثوقية ومصفوفة الفشل |
| [DATA-MODEL.md](./DATA-MODEL.md) | نموذج البيانات وجداول MySQL |
| [IMPLEMENTATION-PLAN.md](./IMPLEMENTATION-PLAN.md) | خطة التنفيذ التفصيلية لكل مرحلة |
| [ROADMAP.md](./ROADMAP.md) | خريطة الطريق والجدول الزمني |
| [CHECKPOINTS.md](./CHECKPOINTS.md) | نقاط التحقق ومعايير القبول |
| [REQUIRED-SKILLS.md](./REQUIRED-SKILLS.md) | قائمة المهارات المطلوبة |

---

## 📜 الترخيص

MIT License — مفتوح المصدر للاستخدام في المستشفيات والمراكز الطبية.
