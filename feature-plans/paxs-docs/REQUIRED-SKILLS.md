# قائمة المهارات المطلوبة — PAXS Server

> مرجع شامل للمهارات التقنية المطلوبة من المطور أو الـ AI Model لبناء هذا النظام بكفاءة عالية.
>
> 📋 **مصفوفة حالة الـ 52 ميزة (41 ✅ · 1 🟡 · 10 ⬜):** راجع `feature-plans/plan.md` و `feature-plans/MASTER-ROADMAP.md`.

---

## 🧠 مستوى الإتقان المطلوب

| الرمز | المستوى |
|-------|---------|
| 🔴 | ضروري جداً — لا يمكن بناء المشروع بدونه |
| 🟡 | مهم — يؤثر على الجودة والأداء |
| 🟢 | مفيد — يضيف قيمة تنافسية |

---

## 1. 🐍 مهارات Python الأساسية

| المهارة | الأدوات | المستوى | السبب |
|---------|---------|---------|-------|
| **Async/Await & AsyncIO** | `asyncio`, `anyio` | 🔴 | كل عمليات DB والشبكة أسنكرونية |
| **Multi-threading** | `threading`, `concurrent.futures` | 🔴 | تشغيل DICOM Receiver وFastAPI معاً |
| **FastAPI Framework** | `fastapi`, `uvicorn`, `pydantic` | 🔴 | بناء كامل DICOMweb API Layer |
| **Pydantic v2 Models** | `pydantic`, `pydantic-settings` | 🟡 | التحقق من البيانات وإدارة الإعدادات |
| **Type Hints & Generics** | نظام الأنواع في Python | 🟡 | كود قابل للصيانة والاختبار |
| **Context Managers** | `async with`, `contextlib` | 🟡 | إدارة موارد DB وملفات DICOM |
| **Streaming Responses** | `StreamingResponse`, `FileResponse` | 🔴 | إرجاع ملفات DICOM كبيرة عبر HTTP |
| **Generators** | `yield`, `async generator` | 🟡 | توليد Multipart responses تدريجياً |

---

## 2. 🏥 مهارات الصور الطبية ومعايير DICOM

| المهارة | الأدوات | المستوى | السبب |
|---------|---------|---------|-------|
| **DICOM Standard PS3** | المواصفة الرسمية | 🔴 | فهم الهيكل Patient→Study→Series→Instance |
| **DICOM Tags & VRs** | DICOM Dictionary | 🔴 | استخراج وتفسير كل وسم من الصور |
| **Transfer Syntax UIDs** | DICOM PS3.5 | 🔴 | فهم أنواع الضغط (JPEG-LS, J2K, HTJ2K) |
| **pydicom Library** | `pydicom` | 🔴 | قراءة/كتابة/تعديل ملفات .dcm |
| **pynetdicom Framework** | `pynetdicom` | 🔴 | بناء C-STORE SCP/SCU, C-FIND SCP |
| **DIMSE Services** | C-STORE, C-FIND, C-GET, C-MOVE | 🔴 | التواصل الشبكي مع أجهزة الأشعة |
| **DICOMweb Standards** | QIDO-RS, WADO-RS, STOW-RS | 🔴 | بناء REST API متوافق مع OHIF |
| **SOP Classes** | CT Image, MR Image, CR Image... | 🟡 | دعم كافة أنواع أجهزة الأشعة |
| **Lossless Compression** | `imagecodecs`, `openjph`, GDCM | 🟡 | ضغط الصور بدون فقدان جودة |
| **Pixel Data Processing** | NumPy, 16-bit pixel arrays | 🟡 | معالجة بيانات الصورة الخام |

---

## 3. 🗄️ قواعد البيانات والتخزين

| المهارة | الأدوات | المستوى | السبب |
|---------|---------|---------|-------|
| **MySQL 8.0** | DDL, Indexing, Foreign Keys | 🔴 | تصميم الجداول وتحسين الأداء |
| **SQLAlchemy 2.0 Async** | `sqlalchemy[asyncio]`, `aiomysql` | 🔴 | ORM كامل للكتابة والقراءة الأسنكرونية |
| **Alembic Migrations** | `alembic` | 🔴 | إدارة تطور مخطط قاعدة البيانات |
| **Connection Pooling** | `asyncpg`, `aiomysql` pool | 🟡 | منع تسريب الاتصالات تحت الحمل العالي |
| **Query Optimization** | EXPLAIN, Indexes | 🟡 | تسريع البحث في ملايين الصور |
| **File System Management** | `pathlib`, `os`, `shutil` | 🔴 | تنظيم ملفات .dcm بهيكل UID hierarchical |
| **Object Storage (S3/MinIO)** | `boto3`, `minio` | 🟡 | التخزين السحابي للفحوصات القديمة |
| **Redis** | `redis-py`, Celery | 🟡 | قوائم انتظار مهام الضغط |

---

## 4. ⚙️ مهارات الـ DevOps والشبكات

| المهارة | الأدوات | المستوى | السبب |
|---------|---------|---------|-------|
| **Docker & Compose** | `docker-compose.yml` | 🔴 | تحزيم MySQL, Redis, Nginx, API |
| **Nginx Configuration** | `nginx.conf`, proxy_pass | 🔴 | Reverse Proxy, CORS, SSL, streaming |
| **SSL/TLS** | Let's Encrypt, OpenSSL | 🟡 | تشفير HTTP وDICOM اتصالات |
| **DICOM Network Debugging** | `dcmtk`, `storescu`, `findscu` | 🔴 | اختبار استقبال/إرسال صور الأشعة |
| **Celery Task Queue** | `celery`, Redis broker | 🟡 | معالجة مهام الضغط في الخلفية |
| **PyInstaller** | `pyinstaller` | 🟡 | تحزيم كـ EXE لنشر على Windows |
| **NSSM / Windows Services** | `nssm` | 🟡 | تشغيل EXE كخدمة ويندوز تلقائياً |
| **Helm Charts** | Kubernetes, k8s | 🟢 | نشر في بنية تحتية سحابية |

---

## 5. 🖥️ واجهات المستخدم والتكامل

| المهارة | الأدوات | المستوى | السبب |
|---------|---------|---------|-------|
| **OHIF Viewer v3** | إعداد `app-config.js` | 🔴 | توصيل عارض الصور بـ DICOMweb API |
| **CORS Configuration** | Nginx headers | 🔴 | السماح لـ OHIF بالوصول للـ API |
| **WebSockets** | `websockets`, FastAPI WS | 🟡 | العرض التشاوري المتزامن |
| **Cornerstone.js** | فهم آلية عرض الصور الطبية | 🟡 | فهم كيف يعرض OHIF الصور 16-bit |
| **QR Code Generation** | `qrcode` | 🟡 | روابط مشاركة المريض |
| **PWA Concepts** | Manifest, Service Worker | 🟢 | عارض خفيف لجوال المريض |

---

## 6. 🔐 الأمان والامتثال

| المهارة | الأدوات | المستوى | السبب |
|---------|---------|---------|-------|
| **JWT Authentication** | `python-jose`, `passlib` | 🔴 | تأمين جميع مسارات API |
| **RBAC** | Role-Based Access Control | 🟡 | صلاحيات مختلفة للأطباء والمرضى |
| **SHA-256 Hashing** | `hashlib` | 🟡 | كشف التلاعب في الصور الطبية |
| **HIPAA / GDPR Basics** | PHI anonymization | 🟢 | متطلبات البحث العلمي والقانون |
| **Rate Limiting** | `slowapi` | 🟡 | حماية API من إرهاق الخادم |
| **Secrets Management** | `.env`, `python-dotenv` | 🔴 | عدم تضمين كلمات المرور في الكود |

---

## 7. 🤖 الذكاء الاصطناعي (اختياري - مستقبلي)

| المهارة | الأدوات | المستوى | السبب |
|---------|---------|---------|-------|
| **MONAI Framework** | PyTorch + MONAI | 🟢 | اكتشاف الحالات الحرجة (AI Triage) |
| **OpenAI Whisper** | `openai-whisper` | 🟢 | تحويل الصوت إلى تقرير طبي |
| **TorchServe** | Model serving | 🟢 | تشغيل نماذج AI في الخلفية |
| **NumPy & PIL** | معالجة مصفوفات الصور | 🟡 | تجهيز صور DICOM لنماذج AI |

---

## 8. 🧪 الاختبار والجودة

| المهارة | الأدوات | المستوى | السبب |
|---------|---------|---------|-------|
| **pytest** | `pytest`, `pytest-asyncio` | 🔴 | اختبارات وحدة وتكامل |
| **HTTP Testing** | `httpx`, FastAPI TestClient | 🟡 | اختبار مسارات DICOMweb |
| **Load Testing** | `locust`, threading | 🟡 | اختبار CP-05 (500 صورة متزامنة) |
| **DICOM Test Files** | `pydicom.data` | 🟡 | ملفات DICOM تجريبية للاختبار |

---

## 📊 ملخص الأولويات للبدء الفوري

```
لبدء المشروع اليوم، أتقن هذه المهارات بالترتيب:

المرحلة 1 — الأساس (أسبوع 1):
  🔴 FastAPI + SQLAlchemy 2.0 Async + Alembic
  🔴 pydicom (قراءة/كتابة ملفات .dcm)
  🔴 MySQL 8.0 + Docker Compose

المرحلة 2 — القلب (أسبوع 2):
  🔴 pynetdicom (C-STORE SCP)
  🔴 DICOM Tags: PatientID, StudyUID, SeriesUID, SOPUID
  🔴 AsyncIO + Threading

المرحلة 3 — الواجهة (أسبوع 3):
  🔴 DICOMweb (QIDO-RS, WADO-RS)
  🔴 OHIF Viewer v3 Configuration
  🔴 Nginx CORS + Proxy

المرحلة 4 — الإنتاج (أسبوع 4+):
  🟡 JWT + Security
  🟡 Celery + JPEG-LS Compression
  🟡 PyInstaller EXE Build
```
