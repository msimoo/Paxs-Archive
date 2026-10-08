# 🚀 PAXS Archive Server — Run & Build Guide
# دليل التشغيل والبناء الشامل لنظام PAXS Archive Server

> **الإصدار**: 0.1.0 &nbsp;|&nbsp; **تاريخ التحديث**: سبتمبر 2026
>
> يقدم هذا الدليل خطوات تفصيلية وقابلة للتنفيذ المباشر لإعداد وتشغيل وبناء نظام **PAXS Archive Server** — من بيئة التطوير المحلية وحتى نشر الإنتاج.
> الشروحات باللغة العربية مع الحفاظ على المصطلحات التقنية والأوامر الإنجليزية الأصلية.

---

## 1. المتطلبات الأساسية (System Prerequisites)

### 1.1 البرمجيات المطلوبة

| المكوّن | الإصدار المطلوب | الوظيفة | ملاحظات |
|---------|-----------------|---------|---------|
| **Python** | 3.11+ (يُنصح بـ 3.12) | الخادم الخلفي + DICOM receiver + worker | تأكد من وجوده في `PATH`. لا تستخدم إصدار Microsoft Store |
| **Node.js** | 18+ (يُنصح بـ 20+) | بناء واجهة Study List UI | مطلوب فقط للتطوير أو إعادة بناء الواجهة |
| **npm** | 9+ | مدير حزم Node.js | يأتي مع Node.js |
| **Redis** | 6.x+ | ذاكرة تخزين مؤقت L2 | **اختياري** — النظام يعمل بدونه بوضع standalone |
| **MySQL** | 8.0+ | قاعدة بيانات الإنتاج | **اختياري** — يمكن استخدام SQLite للتطوير |

### 1.2 مكتبات الترميز الطبي (Medical Imaging Codecs)

هذه المكتبات مطلوبة لفك ترميز وضغط صور DICOM — تُثبّت تلقائياً من `requirements.txt`:

| المكتبة | الإصدار | الوظيفة |
|---------|---------|---------|
| `pydicom` | ≥ 3.0 | قراءة/كتابة ملفات DICOM Part-10 |
| `pynetdicom` | ≥ 2.1 | بروتوكول DIMSE (C-STORE, C-FIND, C-GET, ...) |
| `pylibjpeg` | ≥ 2.0 | إطار عمل فك ترميز بكسل JPEG |
| `pylibjpeg-libjpeg` | ≥ 2.1 | ترميز JPEG baseline و lossless |
| `pylibjpeg-openjpeg` | ≥ 2.0 | ترميز JPEG 2000 (OpenJPEG) |
| `pylibjpeg-rle` | ≥ 2.0 | ترميز RLE lossless |
| `pyjpegls` | ≥ 1.9 | ترميز JPEG-LS (CharLS) — **الافتراضي** |
| `imagecodecs` | ≥ 2024.1 | ترميز HTJ2K (High Throughput JPEG 2000) عبر openjph |
| `numpy` | ≥ 1.26 | معالجة المصفوفات الرقمية (حجم ثلاثي الأبعاد) |
| `pillow` | ≥ 10.3 | معالجة الصور (thumbnails, rendered frames) |

### 1.3 تبعيات إضافية اختيارية

| المكتبة | الوظيفة | متى تحتاجها |
|---------|---------|-------------|
| `boto3` + `botocore` (≥ 1.34) | تخزين S3/MinIO | عند استخدام `STORAGE_BACKEND=s3` |
| `ldap3` (≥ 2.9) | مصادقة LDAP/Active Directory | عند تفعيل `LDAP_ENABLED=true` |
| `cryptography` (≥ 42.0) | تراخيص RSA-2048 + AES-256-GCM | **مطلوب** — مضمن في requirements.txt |

---

## 2. الإعداد والتثبيت لأول مرة (Initial Setup & Environment Configuration)

### 2.1 إنشاء بيئة Python الافتراضية (Virtual Environment)

```powershell
# الانتقال إلى مجلد المشروع
cd "D:\Paxs Archive"

# إنشاء بيئة افتراضية
python -m venv venv

# تفعيل البيئة الافتراضية (PowerShell)
.\venv\Scripts\Activate.ps1

# أو في Command Prompt (CMD)
venv\Scripts\activate.bat
```

> [!IMPORTANT]
> يجب دائماً استخدام البيئة الافتراضية (`venv`) — ملفات التشغيل الآلي (`run_all.bat`) تشير صراحة إلى `venv\Scripts\python.exe` ولن تعمل بدون البيئة الافتراضية.

### 2.2 تثبيت تبعيات Python

```powershell
# تأكد من تفعيل البيئة الافتراضية أولاً
pip install -r requirements.txt
```

يشمل هذا الأمر تثبيت:
- **إطار العمل**: FastAPI, Uvicorn, Pydantic, SQLAlchemy, Alembic
- **DICOM**: pydicom, pynetdicom
- **مكتبات الترميز**: pylibjpeg, pylibjpeg-libjpeg, pylibjpeg-openjpeg, pylibjpeg-rle, pyjpegls, imagecodecs
- **التخزين**: boto3, botocore (لـ S3)
- **الأمان**: cryptography, python-multipart
- **الاختبار**: pytest, httpx

### 2.3 تثبيت تبعيات الواجهة الأمامية (Frontend)

```powershell
# فقط إذا كنت تحتاج تعديل أو إعادة بناء واجهة Study List
cd "D:\Paxs Archive\study-list-ui"
npm install
cd ..
```

### 2.4 إعداد ملف التكوين البيئي (`.env`)

```powershell
# نسخ القالب
copy .env.example .env
```

ثم عدّل `.env` حسب بيئتك. فيما يلي شرح أهم المتغيرات:

#### متغيرات التكوين الحرجة

| المتغير | القيمة الافتراضية | الشرح |
|---------|-------------------|-------|
| `DATABASE_URL` | `mysql+aiomysql://paxs_user:paxs@127.0.0.1:3306/paxs_db?charset=utf8mb4` | سلسلة اتصال قاعدة البيانات. لاستخدام SQLite بدون إعداد: `sqlite+aiosqlite:///./paxs.db` |
| `SECRET_KEY` | `paxs-insecure-dev-key-change-me` | **مفتاح JWT** — يجب تغييره في الإنتاج إلى سلسلة عشوائية طويلة (32+ حرفاً). ولّد واحداً بـ: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ENVIRONMENT` | `development` | `development` للتطوير، `production` للإنتاج — يُفعّل بوابات الأمان |
| `PUBLIC_BASE_URL` | *(فارغ)* | العنوان الذي يصل إليه المتصفح (مثلاً `https://pacs.example.org`). اتركه فارغاً للاستنتاج التلقائي من الطلب |
| `VIEWER_PROCESSING_MODE` | `hybrid` | وضع المعالجة: `server` (خادم فقط) \| `client` (عميل فقط) \| `hybrid` (تلقائي) |
| `REDIS_CACHE_ENABLED` | `true` | تفعيل ذاكرة Redis L2. عند `false` أو عدم وجود Redis — يعمل بوضع standalone |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | عنوان خادم Redis |
| `STORAGE_ROOT` | `./storage` | مجلد تخزين ملفات DICOM على القرص |
| `STORAGE_BACKEND` | `local` | `local` للقرص المحلي، `s3` لـ Amazon S3 أو MinIO |
| `DICOM_AE_TITLE` | `PAXS_SCP` | اسم AE Title لمستقبل DICOM |
| `DICOM_PORT` | `10400` | منفذ TCP لمستقبل DICOM DIMSE |
| `DICOM_RECEIVER_ENABLED` | `true` | تفعيل مستقبل DICOM |
| `DEFAULT_COMPRESSION` | `JPEG-LS` | طريقة الضغط الافتراضية: `JPEG-LS` \| `JPEG2000` \| `HTJ2K` \| `RLE` |
| `HTTPS_ENABLED` | `false` | تفعيل HTTPS المباشر (بدون reverse proxy) |
| `AUTH_GATE_ENABLED` | `true` | تفعيل بوابة المصادقة على `/viewer` و `/studies-ui` |
| `ALLOW_REGISTER` | `false` | السماح بتسجيل الذات — أول حساب يُنشأ دائماً كـ `ADMIN` |
| `AUTH_COOKIE_SECURE` | `false` | يجب أن يكون `true` في الإنتاج (مع HTTPS) |
| `HL7_ENABLED` | `false` | تفعيل بوابة HL7 v2 MLLP |
| `FHIR_ENABLED` | `true` | تفعيل FHIR R4 gateway |

#### مثال تكوين للتطوير المحلي (أبسط إعداد):

```dotenv
# ═══ أبسط تكوين للتطوير المحلي ═══
DATABASE_URL=sqlite+aiosqlite:///./paxs.db
ENVIRONMENT=development
SECRET_KEY=paxs-insecure-dev-key-change-me
REDIS_CACHE_ENABLED=false
STORAGE_ROOT=./storage
DICOM_RECEIVER_ENABLED=true
DICOM_PORT=10400
```

#### مثال تكوين للإنتاج:

```dotenv
# ═══ تكوين الإنتاج ═══
DATABASE_URL=mysql+aiomysql://paxs_user:StrongP@ss!@db.hospital.org:3306/paxs_db?charset=utf8mb4
ENVIRONMENT=production
SECRET_KEY=a7b3c9d2e1f0...your-48-char-random-token...
DEBUG=false
AUTH_COOKIE_SECURE=true
AUTH_COOKIE_SAMESITE=lax
REDIS_CACHE_ENABLED=true
REDIS_URL=redis://redis.hospital.org:6379/0
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=http://minio.hospital.org:9000
S3_ACCESS_KEY=your-access-key
S3_SECRET_KEY=your-secret-key
S3_BUCKET=paxs
PUBLIC_BASE_URL=https://pacs.hospital.org
RATE_LIMIT_ENABLED=true
HTTPS_ENABLED=false
# ← أوقف HTTPS المباشر إذا كان reverse proxy يُنهي TLS
```

### 2.5 إعداد قاعدة البيانات

#### الخيار أ: SQLite (بدون إعداد مسبق)

```dotenv
DATABASE_URL=sqlite+aiosqlite:///./paxs.db
```

لا حاجة لأي إعداد إضافي — يُنشئ الملف تلقائياً عند أول تشغيل.

#### الخيار ب: MySQL 8

```powershell
# 1. إنشاء قاعدة البيانات والمستخدم في MySQL
mysql -u root -e "
  CREATE DATABASE IF NOT EXISTS paxs_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
  CREATE USER IF NOT EXISTS 'paxs_user'@'%' IDENTIFIED BY 'paxs';
  GRANT ALL PRIVILEGES ON paxs_db.* TO 'paxs_user'@'%';
  FLUSH PRIVILEGES;
"

# 2. تشغيل عمليات الترحيل
venv\Scripts\python.exe -m alembic upgrade head
```

#### استخدام MySQL المحمول المُضمّن (للتطوير على Windows):

```powershell
# تهيئة MySQL المحمول (مرة واحدة فقط)
build-win\portable-mysql\init-mysql.bat

# تشغيل MySQL المحمول (المنفذ 3307)
build-win\portable-mysql\start-mysql.bat

# ثم عدّل .env:
# DATABASE_URL=mysql+aiomysql://paxs_user:paxs@127.0.0.1:3307/paxs_db?charset=utf8mb4
```

### 2.6 إعداد Redis (اختياري — مُنصح به)

#### التثبيت من نسخة مُضمّنة (Windows):

```powershell
# Redis مُضمّن في المشروع:
build-win\third-party\redis\start-redis.bat
```

يستخدم تكوين `paxs-redis.conf`:
- `bind 127.0.0.1` — مقيّد على المضيف المحلي
- `maxmemory 256mb` — حد أقصى للذاكرة
- `maxmemory-policy allkeys-lru` — إخلاء LRU عند الامتلاء
- `save 300 100` — لقطة كل 5 دقائق

#### أو التثبيت من مدير حزم:

```powershell
# على Linux/macOS:
sudo apt install redis-server    # Ubuntu/Debian
brew install redis                # macOS

# التحقق من التشغيل:
redis-cli ping
# → PONG
```

---

## 3. التشغيل في بيئة التطوير (Running in Development Mode)

### 3.1 التشغيل الآلي عبر Batch Script (الطريقة الموصى بها على Windows)

```powershell
# من مجلد المشروع:
.\run_all.bat
```

**ماذا يفعل `run_all.bat`:**

1. **التحقق من البيئة الافتراضية**: يبحث عن `venv\Scripts\python.exe` — يتوقف إذا لم يجدها.
2. **فحص ما قبل التشغيل (Pre-flight)**:
   - `python -c "import app.main"` — يتحقق من صحة الاستيراد.
   - `asyncio.run(init_db())` — يتحقق من اتصال قاعدة البيانات والترحيلات.
3. **تشغيل Redis**: يتحقق مما إذا كان يعمل، وإلا يُشغّله من `PATH` أو `.tools\redis\`.
4. **تشغيل الخادم الخلفي**: يفتح نافذة جديدة:
   ```
   start "PAXS Archive Server" cmd /k venv\Scripts\python.exe run_paxs.py
   ```
   (`cmd /k` يبقي النافذة مفتوحة عند الخطأ لعرض التتبع)
5. **تشغيل واجهة التطوير**: إذا وُجد `study-list-ui\package.json`:
   ```
   start "PAXS Viewer UI" cmd /k "cd /d study-list-ui && npm run dev"
   ```
6. **تشغيل مراقب الصحة**: بعد 3 ثوانٍ يُشغّل `health_monitor.bat`.

### 3.2 التشغيل اليدوي (Manual Command-Line)

#### تشغيل الخادم الخلفي (Uvicorn):

```powershell
# تأكد من تفعيل البيئة الافتراضية
.\venv\Scripts\Activate.ps1

# تشغيل الخادم
python run_paxs.py

# أو مباشرة عبر Uvicorn مع auto-reload:
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

#### تشغيل واجهة التطوير (Vite Dev Server):

```powershell
cd study-list-ui
npm run dev
# → يعمل على http://localhost:5174/studies-ui/
```

#### تشغيل Redis يدوياً:

```powershell
# من Redis المُضمّن:
build-win\third-party\redis\redis-server.exe build-win\third-party\redis\paxs-redis.conf

# أو من المسار:
redis-server
```

### 3.3 التحقق من صحة التشغيل (Service Health Verification)

بعد التشغيل، تحقق من الخدمات التالية:

| الخدمة | العنوان | الحالة المتوقعة |
|--------|---------|----------------|
| **Backend API** | `http://localhost:8000/api/v1/health/ready` | `{"status": "ready"}` |
| **API Docs (Swagger)** | `http://localhost:8000/api/docs` | صفحة OpenAPI تفاعلية |
| **Dashboard/Study List** | `http://localhost:8000/` | لوحة التحكم الرئيسية |
| **PaxsViewer** | `http://localhost:8000/viewer/` | عارض DICOM (OHIF) |
| **Study List (Dev)** | `http://localhost:5174/studies-ui/` | واجهة Study List (Vite dev) |
| **DICOM SCP** | المنفذ `10400` (TCP) | يستقبل C-STORE/C-FIND |
| **Redis** | المنفذ `6379` (TCP) | `redis-cli ping` → `PONG` |
| **Login Page** | `http://localhost:8000/login` | صفحة تسجيل الدخول |

#### اختبار سريع عبر سطر الأوامر:

```powershell
# التحقق من صحة API
curl http://localhost:8000/api/v1/health/ready
# → {"status":"ready","instance":"node-1"}

# التحقق من المنافذ
netstat -ano | findstr ":8000 "
netstat -ano | findstr ":10400 "
netstat -ano | findstr ":6379 "

# أو استخدم مراقب الصحة التفاعلي:
.\health_monitor.bat
```

### 3.4 إنشاء أول حساب مسؤول (Bootstrap Admin)

عند التشغيل لأول مرة، سجّل أول حساب — سيُمنح صلاحيات `ADMIN` تلقائياً:

```powershell
curl -X POST http://localhost:8000/api/v1/auth/register ^
  -H "Content-Type: application/json" ^
  -d "{\"username\":\"admin\",\"password\":\"YourStrongPassword123!\",\"full_name\":\"System Admin\"}"
```

أو استخدم صفحة `/login` في المتصفح ← رابط التسجيل (متاح فقط عندما لا يوجد حسابات).

---

## 4. تشغيل الاختبارات وضمان الجودة (Running Automated Test Batteries)

### 4.1 اختبارات Python الخلفية (Pytest)

```powershell
# تأكد من تفعيل البيئة الافتراضية
.\venv\Scripts\Activate.ps1

# تشغيل جميع الاختبارات
pytest tests/ -v

# تشغيل اختبارات محددة:
pytest tests/test_volume.py -v                    # Volume streaming
pytest tests/test_hybrid_engine.py -v             # Hybrid mode negotiation
pytest tests/test_auth.py -v                      # Authentication
pytest tests/test_dicomweb.py -v                  # DICOMweb QIDO/WADO/STOW
pytest tests/test_tenant_isolation.py -v          # VNA tenant isolation
pytest tests/test_route_auth_matrix.py -v         # Security auth matrix
pytest tests/test_no_embedded_secrets.py -v       # Source code secret scan
pytest tests/test_storage_containment.py -v       # Path traversal prevention

# تشغيل فئة معينة:
pytest tests/ -k "volume" -v                      # كل اختبارات الحجم
pytest tests/ -k "auth" -v                        # كل اختبارات المصادقة
pytest tests/ -k "hybrid" -v                      # كل اختبارات الوضع المختلط

# مع تقرير تغطية الكود:
pytest tests/ --cov=app --cov-report=html -v
```

#### ما تغطيه مجموعة الاختبارات (100+ ملف اختبار):

| الفئة | أمثلة | الوصف |
|-------|------|-------|
| **Volume/MPR** | `test_volume.py`, `test_volume_mpr_slice.py`, `test_volume_geometry.py`, `test_mip.py` | تدفق البيانات الحجمية، تقطيع MPR، إسقاطات MIP |
| **Hybrid Engine** | `test_hybrid_engine.py`, `test_hybrid_integration.py`, `test_negotiate.py` | مفاوضة الأوضاع + viewer role overrides |
| **DICOMweb** | `test_dicomweb.py`, `test_wado_bulk.py`, `test_stow_upload.py` | QIDO/WADO/STOW-RS |
| **DIMSE** | `test_cstore.py`, `test_cfind.py`, `test_cget.py`, `test_receiver.py` | C-STORE/C-FIND/C-GET/C-ECHO |
| **Auth/Security** | `test_auth.py`, `test_auth_defenses.py`, `test_sessions.py`, `test_login_gate.py` | JWT, lockout, token rotation, theft detection |
| **Routing** | `test_routing.py`, `test_routing_queue.py` | Auto-routing + durable queue |
| **Storage** | `test_storage.py`, `test_storage_containment.py`, `test_multi_storage.py` | Path traversal, S3, atomic ingest |
| **Clinical** | `test_worklist.py`, `test_mpps.py`, `test_reports.py`, `test_critical_results.py` | MWL, MPPS, reports, critical results |
| **Hardening** | `test_p1_hardening.py`, `test_no_embedded_secrets.py`, `test_config.py` | Production security gates |

> [!TIP]
> حارس قاعدة البيانات (`tests/_db_guard.py`) يمنع الاختبارات من لمس قاعدة بيانات الإنتاج — يتحقق من أن اسم قاعدة البيانات يحتوي على `'test'` أو أنها SQLite في الذاكرة.

### 4.2 اختبارات JavaScript (Node.js Test Runner)

```powershell
# تشغيل جميع اختبارات JS
node --test tests/js/

# تشغيل اختبار محدد:
node --test tests/js/paxs-mpr-geometry.test.js
node --test tests/js/paxs-mpr-client-mode.test.js
node --test tests/js/paxs-mpr-server-mode.test.js
node --test tests/js/paxs-mpr-disposal.test.js
node --test tests/js/paxs-study-list-refresh.test.js
```

#### ما تغطيه اختبارات JavaScript:

| الملف | الوصف |
|-------|-------|
| `paxs-mpr-geometry.test.js` | حسابات هندسة MPR: تحويلات الإحداثيات، القياسات الفيزيائية، Cobb angle, CTR |
| `paxs-mpr-client-mode.test.js` | وضع العميل WebGL2 |
| `paxs-mpr-server-mode.test.js` | وضع الخادم (rendered tiles) |
| `paxs-mpr-plane-extraction.test.js` | استخراج مستويات التقطيع |
| `paxs-mpr-wiring.test.js` | ربط الأحداث |
| `paxs-mpr-disposal.test.js` | تحرير الموارد WebGL |
| `paxs-mpr-worker-lifecycle.test.js` | دورة حياة Web Worker |
| `paxs-study-list-refresh.test.js` | تحديث قائمة الدراسات |
| `test_viewer_fallback.test.js` | سلوك الرجوع الاحتياطي للعارض |

### 4.3 تفسير نتائج الاختبار ومعايير عدم التراجع (Zero-Regression Criteria)

```
# النتيجة المتوقعة:
============================================ PASSED ============================================
# أي فشل يعني تراجعاً ويجب إصلاحه قبل الدمج

# قاعدة Zero-Regression:
# ✅ جميع اختبارات pytest يجب أن تنجح (0 فشل)
# ✅ جميع اختبارات node --test يجب أن تنجح (0 فشل)
# ✅ لا يُسمح بـ @pytest.mark.skip على اختبارات أمنية
# ✅ test_no_embedded_secrets.py يجب أن ينجح دائماً
```

---

## 5. البناء والتشغيل للإنتاج (Production Build & Packaging)

### 5.1 بناء أصول الواجهة الأمامية (Frontend Build)

```powershell
# بناء واجهة Study List
cd study-list-ui
npm install
npm run build
cd ..

# النتائج في study-list-ui/dist/
# يُنسخ تلقائياً إلى app/static/studies-ui/ عبر build_standalone.bat
```

### 5.2 تشغيل الإنتاج عبر Docker & Docker Compose

#### بناء وتشغيل عقدة واحدة:

```powershell
# من مجلد deploy/
cd deploy

# بناء الصورة
docker build -t paxs-server -f Dockerfile ..

# تشغيل حاوية واحدة
docker run -d \
  --name paxs \
  -p 8000:8000 -p 10400:10400 \
  -e DATABASE_URL="mysql+aiomysql://paxs_user:paxs@host.docker.internal:3306/paxs_db" \
  -e SECRET_KEY="your-long-random-secret" \
  -e ENVIRONMENT=production \
  -e AUTH_COOKIE_SECURE=true \
  paxs-server
```

#### نشر متوسع (3 عقد + HAProxy + MySQL + MinIO):

```powershell
cd deploy

# تشغيل المكدس الكامل
docker compose -f docker-compose.lb.yml up -d

# المكونات:
# ├── paxs-haproxy   → :8000 (HTTP), :10400 (DIMSE), :2575 (HL7), :8404 (Stats)
# ├── paxs-node-1    → عقدة PAXS #1
# ├── paxs-node-2    → عقدة PAXS #2
# ├── paxs-node-3    → عقدة PAXS #3
# ├── paxs-mysql     → MySQL 8 مشترك
# └── paxs-minio     → MinIO S3 مشترك (:9000 API, :9001 Console)
```

#### إضافة المراقبة (Prometheus + Grafana):

```powershell
docker compose -f docker-compose.monitoring.yml up -d

# Prometheus: http://localhost:9090
# Grafana:    http://localhost:3000 (admin/admin)
```

#### تكوين Reverse Proxy:

راجع الملف [`deploy/REVERSE-PROXY.md`](deploy/REVERSE-PROXY.md) للحصول على أمثلة كاملة لـ:

**nginx (sub-path stripping):**
```nginx
location /paxs/ {
    proxy_pass http://paxs-app:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /paxs;
}
```

**Traefik (Docker labels):**
```yaml
labels:
  - "traefik.http.routers.paxs.rule=PathPrefix(`/paxs`)"
  - "traefik.http.middlewares.paxs-strip.stripprefix.prefixes=/paxs"
  - "traefik.http.middlewares.paxs-prefix.headers.customrequestheaders.X-Forwarded-Prefix=/paxs"
```

**Caddy:**
```caddyfile
handle_path /paxs/* {
    reverse_proxy paxs-app:8000
    header_up X-Forwarded-Prefix /paxs
}
```

> [!IMPORTANT]
> عند استخدام reverse proxy، يجب ضبط `PAXS_FORWARDED_ALLOW_IPS` في `.env` على عنوان IP الـ proxy أو `*` — وإلا ستُتجاهل ترويسات `X-Forwarded-*` بصمت.

### 5.3 بناء حزمة Windows مستقلة (Standalone `PAXS-Server.exe`)

#### الطريقة الشاملة (موصى بها):

```powershell
.\build_standalone.bat
```

**ماذا يفعل:**
1. **بناء الواجهة**: يُشغّل `npm install` + `npm run build` في `study-list-ui/` وينسخ النتائج إلى `app\static\studies-ui\`.
2. **تثبيت PyInstaller**: `pip install pyinstaller`.
3. **بناء التطبيق**: `pyinstaller --noconfirm paxs.spec`.
4. **تجميع الحزمة**: ينسخ `.env.example`، `health_monitor.bat`، `install_service.bat` إلى `dist\PAXS-Server\`.
5. **إنشاء مجلدات**: `storage\`، `backups\`، `certs\` في الحزمة.
6. **مُشغّل**: ينشئ `start_paxs.bat` داخل الحزمة.

#### النتيجة:
```
dist\PAXS-Server\
├── PAXS-Server.exe          # التطبيق الرئيسي
├── start_paxs.bat           # مُشغّل سريع
├── health_monitor.bat       # مراقب الصحة
├── install_service.bat      # تسجيل كخدمة Windows
├── .env                     # التكوين
├── storage\                 # مخزن DICOM
├── backups\                 # نسخ احتياطية
└── certs\                   # شهادات TLS
```

> يمكن نقل المجلد `dist\PAXS-Server\` بالكامل إلى أي جهاز Windows بدون الحاجة لتثبيت Python أو Node.js أو Git!

#### الطريقة اليدوية:

```powershell
# 1. تثبيت PyInstaller
pip install pyinstaller

# 2. البناء
pyinstaller --noconfirm paxs.spec

# 3. نسخ ملفات الدعم
copy .env.example dist\PAXS-Server\.env
```

#### تسجيل كخدمة Windows (Windows Service):

```powershell
# يتطلب nssm.exe (Non-Sucking Service Manager) في PATH
cd dist\PAXS-Server
install_service.bat

# هذا يُنشئ خدمة Windows باسم PAXSServer
# التشغيل التلقائي عند بدء Windows
# للإزالة: nssm remove PAXSServer confirm
```

---

## 6. استكشاف الأخطاء وحلها (Troubleshooting & Diagnostics)

### 6.1 مشاكل مسارات Windows مع المسافات (Windows Path & Space Issues)

> [!WARNING]
> مسار المشروع `D:\Paxs Archive` يحتوي على مسافة — هذا يسبب مشاكل شائعة مع بعض الأدوات.

#### الأعراض والحلول:

| المشكلة | السبب | الحل |
|---------|------|------|
| `'D:\Paxs' is not recognized` | مسافة في المسار بدون اقتباس | ضع المسار بين علامتي اقتباس: `cd "D:\Paxs Archive"` |
| `ModuleNotFoundError: No module named 'app'` | Python يعمل من مجلد خاطئ | تأكد من `cd /d "%~dp0"` في الـ batch أو `cd "D:\Paxs Archive"` |
| `venv\Scripts\python.exe` لا يعمل | مسار مع مسافات | `run_all.bat` يستخدم `%~dp0venv\Scripts\python.exe` بدون اقتباس إضافي — هذا صحيح |
| `npm ERR! path "D:\Paxs Archive\study-list-ui"` | مسافة في مسار npm | استخدم `cd /d "D:\Paxs Archive\study-list-ui" && npm install` |

#### ممارسة آمنة:

```powershell
# دائماً اقتبس المسارات:
set "PROJECT_DIR=D:\Paxs Archive"
cd /d "%PROJECT_DIR%"
"%PROJECT_DIR%\venv\Scripts\python.exe" run_paxs.py
```

### 6.2 تعارضات المنافذ (Port Conflicts & Address Binding Failures)

#### الأعراض:
```
[PAXS] Starting on http://0.0.0.0:8000
ERROR: [Errno 10048] Only one usage of each socket address is normally permitted
```

#### التشخيص:
```powershell
# البحث عن العملية التي تستخدم المنفذ
netstat -ano | findstr ":8000 "
# → TCP    0.0.0.0:8000    0.0.0.0:0    LISTENING    12345

# معرفة اسم العملية
tasklist /FI "PID eq 12345"

# إيقاف العملية
taskkill /PID 12345 /F
```

#### المنافذ المستخدمة بالنظام:

| المنفذ | الخدمة | ملاحظات |
|--------|--------|---------|
| `8000` | FastAPI Backend + DICOMweb + Dashboard | قابل للتغيير عبر `PAXS_PORT` |
| `10400` | DICOM DIMSE SCP | قابل للتغيير عبر `DICOM_PORT` |
| `5174` | Vite Dev Server (Study List UI) | فقط في وضع التطوير |
| `6379` | Redis | قابل للتغيير عبر `REDIS_URL` |
| `3306` / `3307` | MySQL | `3307` للـ MySQL المحمول |
| `2575` | HL7 MLLP | فقط عند `HL7_ENABLED=true` |
| `8443` | HTTPS (مباشر) | فقط عند `HTTPS_ENABLED=true` |

### 6.3 مشاكل WebGL2 وفقدان السياق (WebGL2 Context Loss & Silent Fallback)

#### الأعراض:
- الشاشة سوداء في عارض MPR
- رسالة `WebGL context lost` في Console المتصفح
- لا يعمل على الأجهزة الافتراضية (VMs) أو Remote Desktop

#### التشخيص:
```javascript
// في Console المتصفح:
const gl = document.createElement('canvas').getContext('webgl2');
console.log(gl ? 'WebGL2 متاح' : 'WebGL2 غير متاح');
if (gl) {
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    console.log('GPU:', gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL));
    console.log('Max 3D Texture:', gl.getParameter(gl.MAX_3D_TEXTURE_SIZE));
}
```

#### الحلول:

| السيناريو | الحل |
|-----------|------|
| لا يوجد WebGL2 | النظام يتراجع تلقائياً إلى وضع `server` (rendered PNG tiles) |
| VM / Remote Desktop | ضبط `VIEWER_PROCESSING_MODE=server` في `.env` |
| فقدان سياق متكرر | عارض PAXS يحتوي على `WebGL Guard` (`__paxsVtkGuardInstalled`) — يلتقط الأخطاء بصمت |
| ذاكرة GPU غير كافية | وضع `hybrid` ينتقل تلقائياً إلى `server` عندما يكون `webgl_score < 40` |

#### التحقق من تفعيل الرجوع الاحتياطي:

```powershell
# التحقق من الوضع المفاوَض لسلسلة معينة:
curl "http://localhost:8000/api/v1/volume/{series_uid}/negotiate?webgl_score=0&rtt_ms=50" ^
  -H "Authorization: Bearer YOUR_TOKEN"

# يجب أن يعيد: "effective_mode": "server"
```

### 6.4 مشاكل Redis L2 Cache (Redis Connectivity & Memory Alerts)

#### الأعراض:
- `WARNING paxs.services.redis: Redis unreachable — falling back to L1 only`
- تباطؤ ملحوظ عند تصفح الصور (re-decode من القرص)

#### التشخيص:
```powershell
# التحقق من اتصال Redis
redis-cli -h 127.0.0.1 -p 6379 ping
# → PONG

# التحقق من استخدام الذاكرة
redis-cli info memory
# → used_memory_human:45.23M
# → maxmemory_human:256.00M

# التحقق من مقاييس PAXS Redis
curl http://localhost:8000/metrics -H "Authorization: Bearer TOKEN" | findstr "paxs_cache"
```

#### الحلول:

| المشكلة | الحل |
|---------|------|
| Redis غير قابل للوصول | تحقق من `REDIS_URL` في `.env` والمنفذ `6379` |
| `maxmemory` ممتلئة | ارفع `maxmemory` في `paxs-redis.conf` أو Redis يخلي تلقائياً عبر `allkeys-lru` |
| النظام يعمل بدون Redis | هذا طبيعي — `REDIS_CACHE_ENABLED=false` أو Redis غير متاح → يعمل بـ L1 فقط |
| أداء ضعيف بعد إعادة تشغيل Redis | الذاكرة المؤقتة فارغة — ستمتلئ تدريجياً مع الاستخدام |

### 6.5 مشاكل قاعدة البيانات (Database Issues)

#### خطأ الترحيل (Migration Error):
```powershell
# إصلاح ترحيل Alembic معلق:
venv\Scripts\python.exe -m alembic stamp base
venv\Scripts\python.exe -m alembic upgrade head
```

#### خطأ اتصال MySQL:
```powershell
# التحقق من تشغيل MySQL
mysql -u root -e "SELECT 1"

# التحقق من قاعدة البيانات
mysql -u paxs_user -ppaxs -e "USE paxs_db; SHOW TABLES;"
```

#### التبديل إلى SQLite (للتطوير):
```dotenv
DATABASE_URL=sqlite+aiosqlite:///./paxs.db
```

### 6.6 مشاكل DICOM Receiver

#### الأعراض:
- لا يستقبل صور C-STORE
- `Connection refused` من الجهاز (modality)

#### التشخيص:
```powershell
# التحقق من أن المنفذ يستمع
netstat -ano | findstr ":10400 "

# اختبار C-ECHO
# (يتطلب pynetdicom)
python -c "
from pynetdicom import AE
ae = AE()
ae.add_requested_context('1.2.840.10008.1.1')
assoc = ae.associate('127.0.0.1', 10400, ae_title='PAXS_SCP')
if assoc.is_established:
    status = assoc.send_c_echo()
    print('C-ECHO:', 'Success' if status.Status == 0 else 'Failed')
    assoc.release()
else:
    print('Association failed')
"
```

#### إعدادات جهاز التصوير (Modality Configuration):
```
Remote AE Title:  PAXS_SCP
Remote Host:      <عنوان IP الخادم>
Remote Port:      10400
```

### 6.7 مشاكل بوابات الأمان في الإنتاج (Production Security Gates)

عند تشغيل `ENVIRONMENT=production`، يرفض الخادم البدء إذا:

| الفحص | رسالة الخطأ | الحل |
|-------|------------|------|
| `SECRET_KEY` افتراضي | `SECRET_KEY is the shipped default or shorter than 32 characters` | ولّد مفتاحاً: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `AUTH_COOKIE_SECURE=false` | `AUTH_COOKIE_SECURE must be true in production` | ضبط `AUTH_COOKIE_SECURE=true` (يتطلب HTTPS) |
| `DEBUG=true` | `DEBUG must be false in production` | ضبط `DEBUG=false` |
| `RATE_LIMIT_ENABLED=false` | `RATE_LIMIT_ENABLED must stay true in production` | ضبط `RATE_LIMIT_ENABLED=true` |
| `AUTH_COOKIE_SAMESITE` غير صالح | `AUTH_COOKIE_SAMESITE must be one of 'lax', 'strict' or 'none'` | ضبط قيمة صحيحة |

---

## 7. مرجع سريع للأوامر (Quick Reference)

```powershell
# ════════════════════════════════════════════════
#  PAXS Archive Server — Quick Command Reference
# ════════════════════════════════════════════════

# --- الإعداد ---
python -m venv venv                              # إنشاء بيئة افتراضية
.\venv\Scripts\Activate.ps1                       # تفعيل (PowerShell)
pip install -r requirements.txt                   # تثبيت التبعيات
copy .env.example .env                            # إنشاء ملف التكوين
venv\Scripts\python.exe -m alembic upgrade head    # ترحيل قاعدة البيانات

# --- التشغيل ---
.\run_all.bat                                     # تشغيل كل الخدمات
python run_paxs.py                                # تشغيل الخادم فقط
.\health_monitor.bat                              # مراقبة الصحة

# --- الاختبار ---
pytest tests/ -v                                  # اختبارات Python
node --test tests/js/                              # اختبارات JavaScript

# --- البناء ---
.\build_standalone.bat                             # بناء حزمة Windows
cd study-list-ui && npm run build                  # بناء الواجهة

# --- Docker ---
cd deploy
docker compose -f docker-compose.lb.yml up -d      # نشر متوسع
docker compose -f docker-compose.monitoring.yml up -d  # مراقبة

# --- الصيانة ---
venv\Scripts\python.exe -m alembic upgrade head    # تحديث المخطط
redis-cli flushdb                                   # مسح ذاكرة Redis المؤقتة
```

---

> **ملاحظة**: هذا الدليل يعكس الحالة الفعلية لأدوات البناء والتشغيل في المستودع. تأكد من مراجعة `.env.example` للحصول على قائمة كاملة ومحدّثة بجميع متغيرات التكوين المتاحة.
