# دليل عمل Redis في خادم PAXS Archive وعارض الدراسات DICOM PaxsViewer
# Complete Guide: Redis Architecture, DICOM PaxsViewer Acceleration & Administration

> **الهدف من هذه الوثيقة**: شرح هندسة عمل خادم **Redis** داخل نظام **PAXS Archive**، وكيفية تسريع عارض الأشعة **DICOM PaxsViewer** وتوفير استجابة فورية (Sub-5ms Latency)، بالإضافة إلى دليل استخدام **لوحة التحكم الشاملة** في صفحة الإعدادات عبر الرابط: `http://localhost:8000/#settings`.

---

## ١) نظرة عامة على هندسة التخزين المؤقت (Architecture Overview)

يعمل Redis في PAXS Archive كـ **طبقة تخزين مؤقت موزعة فائقة السرعة (Tier-2 Distributed Cache)** وتنسيق متعدد العقد (Multi-node Coordination)، مدمجة بالتوازي مع **ذاكرة RAM سريعة داخلية (Tier-1 In-Memory LRU Cache)**.

```
                           ┌────────────────────────────────────────────────────────┐
                           │               PAXS Ingest / Viewer Request             │
                           └───────────┬────────────────────────────────┬───────────┘
                                       │                                │
                             1. فحص ذاكرة RAM L1               2. فحص خادم Redis L2
                         [زمن وصول < 0.1 مللي ثانية]         [زمن وصول < 1.5 مللي ثانية]
                                       │                                │
                             (Hit) ────┴──── (Miss) ─────────── (Hit) ──┴── (Miss)
                                                                 │            │
                                                      ترقية تلقائية للـ RAM  قراءة من القرص / S3
                                                       (Promote to L1)     وفك ضغط الـ DICOM
```

### مستويات التخزين المؤقت (Two-Tier Caching Pipeline):

1. **الطبقة الأولى — Tier-1 In-Memory RAM Cache**:
   - ذاكرة وصول عشوائي محلية داخل معالج التطبيق (`_MEM_CACHE` في [`redis_service.py`](file:///D:/Paxs%20Archive/app/services/redis_service.py)).
   - السعة القصوى: **256 ميجابايت** أو **4,000 عنصر**.
   - خوارزمية الإخلاء: **LRU (Least Recently Used)** مع قفل أمان متوازي (`threading.Lock`).
   - زمن الاستجابة: **أقل من 0.1 مللي ثانية** بدون أي استدعاءات شبكية.

2. **الطبقة الثانية — Tier-2 Redis Distributed Cache**:
   - خادم Redis مركزي (افتراضي `redis://127.0.0.1:6379/0`).
   - يشارك البيانات بين جميع خيوط المعالجة وعقد الخادم المتعددة (API Nodes & Background Workers).
   - **الترقية التلقائية (Promotion)**: عند جلب أي عنصر من Redis، يُنسخ تلقائيًا إلى ذاكرة RAM L1 لمدة 300 ثانية لتسريع الطلبات اللاحقة إلى الصفر تقريبًا.

3. **آلية الفشل المرن التلقائي (Graceful Failover)**:
   - إذا توقف خادم Redis أو تعذر الوصول إليه أو تم تعطيله (`REDIS_CACHE_ENABLED=False`)، يتحول النظام تلقائيًا وفورًا وبدون أي انقطاع إلى استخدام ذاكرة RAM L1 الحصرية، دون ظهور أي أخطاء للمستخدمين أو تعطل لخدمات الـ DICOM.

---

## ٢) كيف يعمل Redis مع عارض الأشعة DICOM PaxsViewer؟ 🩻⚡

عارض الأشعة **PaxsViewer** (المتاح عبر `/viewer` و `/studies-ui`) يعتمد على Redis في أربعة محاور حاسمة لضمان تجربة فحص سريرية سلسة وسريعة للأطباء وأخصائيي الأشعة:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                DICOM PaxsViewer Actions                                │
└──────┬───────────────────────┬─────────────────────────┬────────────────────────┬──────┘
       │                       │                         │                        │
  فتح دراسة جديدة         تشغيل Cine Loop             عرض 3D MPR              تصفح السلاسل
 (Study Open Event)     (60 FPS CT/MR Scroll)     (Volume Reslicing)       (Series Thumbnails)
       │                       │                         │                        │
       ▼                       ▼                         ▼                        ▼
  Warm-on-Open            Decoded Frame             Voxel Block Array        Instant Render
  Pre-warming             Cache Hit                 Cache Hit                Cache Hit
  `paxs:lock:warm:*`      `paxs:frame:*`            `paxs:vol:*`             `paxs:thumb:*`
```

### أ) التسخين التلقائي المسبق عند فتح الدراسة (Warm-on-Open)
- **الملف المسؤول**: [`app/services/warm_service.py`](file:///D:/Paxs%20Archive/app/services/warm_service.py)
- **كيف يعمل؟**: في اللحظة التي يفتح فيها الطبيب دراسة عبر QIDO-RS أو WADO-RS:
  1. يقوم الخادم بحجز **قفل موزع في Redis** باسم `paxs:lock:warm:{study_uid}` (لمدة 120 ثانية) لمنع تكرار المعالجة إذا فتح عدة أطباء نفس الدراسة في نفس اللحظة.
  2. يُطلق خيط خلفي (Daemon Thread) يقوم فورًا بـ:
     - إنشاء الصور المصغرة المفقودة 192px وحفظها.
     - تسخين مفاتيح الكاش للصور المصغرة في Redis باسم `paxs:thumb:{sop_instance_uid}:auto:256:png`.
     - فك ضغط الإطارات الأولى من السلسلة (`WARM_MAX_FRAMES`) وتخزينها في الذاكرة لتكون جاهزة قبل أن يبدأ المستخدم بالتمرير.

### ب) تسريع التمرير السينمائي السريع (Cine Loop & 60 FPS Frame Scrolling)
- **الملف المسؤول**: [`app/services/wado_rs_bulk_service.py`](file:///D:/Paxs%20Archive/app/services/wado_rs_bulk_service.py)
- **المشكلة**: فحوصات الأشعة المقطعية (CT) والرنين المغناطيسي (MRI) تحتوي مئات أو آلاف الإطارات المضغوطة بصيغ مثل `JPEG-LS` أو `JPEG2000` أو `HTJ2K`. فك ضغط كل شريحة من القرص عند كل تمريرة يسبب بطئاً وتقطيعاً ملحوظاً.
- **حل Redis**: يتم تخزين الإطارات المفكوكة كـ Raw Bytes في Redis تحت مفاتيح:
  `paxs:frame:{sop_uid}:{frame_idx}:{transfer_syntax}:{options}`
- **النتيجة**: استرجاع الإطار خلال **أقل من 3 مللي ثانية**، مما يتيح تمرير بمعدل 60 إطار في الثانية (60 FPS Cine Loop) بسلاسة تامة.

### ج) إعادة البناء ثلاثي الأبعاد والشرائح المتعددة (3D Multi-Planar Reconstruction - MPR)
- **الملف المسؤول**: [`app/services/volume_service.py`](file:///D:/Paxs%20Archive/app/services/volume_service.py)
- **المشكلة**: عرض الحجم ثلاثي الأبعاد والشرائح المحورية (Axial) والتاجية (Coronal) والجانبية (Sagittal) والمائلة (Oblique) يتطلب تجميع مصفوفة ثلاثية الأبعاد كاملة من قيم Hounsfield Units (Float32 Array) من جميع ملفات DICOM، وهو استهلاك مكثف للقرص والمعالج (قد يستغرق 3 إلى 8 ثوانٍ لكل استدعاء).
- **حل Redis**:
  - يتم تخزين مصفوفة الـ Voxel المكتملة في Redis تحت المفتاح `paxs:vol:{cache_key}` لمدة ساعة كاملة (`_VOL_REDIS_TTL = 3600`).
  - يتضمن المفتاح ترويسة أبعاد 12 بايت `(nz, ny, nx)` متبوعة بمصفوفة `float32` المضغوطة ثنائياً.
  - عند تغيير زاوية الرؤية أو تحريك شريحة MPR، يقرأ المعالج الحجم من Redis مباشرة دون لمس القرص الصلب، مما يسمح بحساب الشرائح المائلة في أجزاء من الثانية.

### د) سرعة تصفح السلاسل والصور المصغرة (Series Thumbnails)
- **المفتاح**: `paxs:thumb:{sop_instance_uid}:auto:256:png`
- يتم تحميل مصغرات السلاسل في اللوحة اليسرى لعارض PaxsViewer (Study Browser) فورياً من Redis أو الـ RAM دون الحاجة لقراءة ملف DICOM الأصلي من مسار التخزين.

---

## ٣) تصنيف وهيكلة المفاتيح في Redis (Taxonomy of Redis Keys)

| نمط المفتاح (Key Pattern) | نوع البيانات | مدة البقاء (TTL) | الغرض والوظيفة |
| :--- | :--- | :--- | :--- |
| `paxs:frame:{sop}:{frame}:*` | String (Binary) | 600s – 3600s | إطارات الصور المفكوكة للعرض الفوري السريع في PaxsViewer |
| `paxs:vol:{sop_uid}:{slices}` | String (Binary) | 3600s (1 hour) | كتل الـ Voxel ثلاثية الأبعاد لـ 3D MPR ومساحة العمل السريرية |
| `paxs:thumb:{sop}:*` | String (Binary PNG) | 600s (10 min) | الصور المصغرة للسلاسل في شريط العارض وقوائم الفحص |
| `paxs:lock:warm:{study_uid}` | Lock (String) | 120s | قفل موزع لمنع تكرار تسخين نفس الدراسة بين المتصفحات |
| `paxs:ratelimit:{window}:{ip}`| Sorted Set (ZSET) | Window + 5s | سجل تقييد معدل الطلبات وحماية واجهات تسجيل الدخول والـ API |
| `paxs:storage:usage` | String (Integer) | 60s | الحجم المحسوب للملفات في التخزين لمنع الفحص المتكرر للقرص |

---

## ٤) لوحة التحكم الكاملة بـ Redis في صفحة الإعدادات (`#settings`)

تمت إضافة تبويب مخصص ومستقل لـ **Redis** مباشرة بعد تبويب **Database** في واجهة الإعدادات عبر الرابط:
👉 **`http://localhost:8000/#settings`** (أو النقر على تبويب **Settings** ثم اختيار **Redis**).

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│ System │ DICOM │ Modality │ Interop │ Storage │ Security │ Workflow │ Database │ Redis ⚡ │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### الأقسام والوظائف المتاحة في اللوحة:

### ١. بطاقة الحالة والمقاييس الحية (Live Status & Metrics)
- **مؤشر الاتصال وسرعة الاستجابة**: عرض الشارة الحية (`Redis 7.2.5` أو `Offline`) مع وقت الـ PING بالمللي ثانية (مثلاً `1.42 ms`).
- **معلومات الخادم**: إصدار Redis، نمط التشغيل (Standalone / Cluster)، نظام التشغيل، ومدة التشغيل (Uptime).
- **استهلاك الذاكرة (Memory Usage)**: حجم الذاكرة المستخدمة حالياً وذروة الاستهلاك (Peak Memory).
- **المفاتيح ومعدل الإصابة (Hit Rate %)**: إجمالي المفاتيح المخزنة، ونسبة الاستجابة الناجحة من الكاش مقابل الإخفاق (Hits vs. Misses).
- **الطبقة الأولى (Tier-1 RAM)**: عدد العناصر المحفوظة في الذاكرة الحية داخل المعالج وحجمها من السقف المحدد (256 MB).
- **زر التحديث السريع (⟳ Refresh)**: تحديث فوري لجميع المقاييس بدون إعادة تحميل الصفحة.

### ٢. التحكم في تشغيل وإيقاف وتثبيت خادم Redis (Process & Service Control - SERVICE)
- **الكشف التلقائي عن التثبيت (Auto-Detection)**: فحص مسارات البرامج التنفيذية على النظام (`Memurai`, `Redis for Windows`, `.tools/redis/`, `PATH`) ومراقبة خدمات ويندوز (`sc query Memurai / Redis`).
- **زر Start Redis (▶)**: تشغيل خدمة ويندوز أو بدء العملية في الخلفية تلقائياً وإعادة ربط الاتصال.
- **زر Stop Redis (⏹)**: إيقاف آمن لخادم Redis وإرسال أمر `SHUTDOWN nosave` وإيقاف الخدمة.
- **زر Restart Redis (🔄)**: إعادة تشغيل الخدمة بنقرة واحدة.
- **زر Install / Setup Portable Redis (📥)**: تنزيل وتثبيت النسخة المحمولة الخفيفة من Redis تلقائياً داخل مجلد `.tools/redis/` وتشغيلها فورياً عند عدم وجود خادم مثبت.

### ٣. تهيئة وإعدادات الاتصال (Redis Configuration - CONFIG)
- **Host**: عنوان خادم Redis (افتراضي `127.0.0.1`).
- **Port**: منفذ الاتصال (افتراضي `6379`).
- **Database Index**: رقم قاعدة البيانات (0 إلى 15).
- **Password**: كلمة المرور (مشفرة ومحمية في الخادم).
- **SSL/TLS**: دعم الاتصال المشفر عبر `rediss://`.
- **Enable Redis Caching**: مفتاح تفعيل/تعطيل التخزين المؤقت عالمياً.
- **زر Test Connection**: فحص الاتصال وقراءة معلومات الخادم والزمن بدون أي تعديل.
- **زر Save Configuration**: حفظ الإعدادات في قاعدة البيانات (`server_settings`) وتحديث ملف [`.env`](file:///D:/Paxs%20Archive/.env)، وإعادة ربط حوض الاتصال فوراً دون الحاجة لإعادة تشغيل الخادم.

### ٤. متصفح المفاتيح والمعاينة المباشرة (Key Browser & Inspector)
- **البحث والتصفية بالأقنعة (Wildcard Pattern)**: كتابة أي نمط مثل `paxs:*` أو `*`.
- **أزرار التصفية السريعة (Quick Filter Chips)**:
  - `PAXS (paxs:*)`: كل مفاتيح النظام.
  - `Frames (paxs:frame:*)`: إطارات الصور المحفوظة لـ PaxsViewer.
  - `3D Volumes (paxs:vol:*)`: أحجام الـ 3D MPR.
  - `Thumbnails (paxs:thumb:*)`: الصور المصغرة.
  - `Rate Limits (paxs:ratelimit:*)`: قيود الطلبات.
  - `Warm Locks (paxs:warm:*)`: أقفال المعالجة الموزعة.
  - `All (*)`: استعراض جميع المفاتيح.
- **جدول المفاتيح التفاعلي**:
  - عرض اسم المفتاح.
  - شارة مكان التخزين (`L1 RAM` أو `Redis L2`).
  - نوع البيانات (`string`, `zset`, `hash`, `list`).
  - وقت البقاء المتبقي (`TTL`).
  - حجم البيانات بالبايت.
- **زر View**: فتح نافذة فحص واستعراض محتوى المفتاح بالتفصيل (JSON / نص / معاينة ثنائية).
- **زر Delete**: حذف المفتاح المختار بشكل فردي وفوري.

### ٤. عمليات المسح والتفريغ والصيانة (Cache Purge & Operations)
- **🧹 Flush PAXS Cache (`paxs:*`)**: مسح كل عناصر الذاكرة المؤقتة الخاصة بـ PAXS بأمان (الإطارات، الأحجام، المصغرات) وتتجدد تلقائياً عند الطلب دون المساس ببيانات أخرى.
- **⚡ Flush L1 RAM Cache**: تفريغ ذاكرة المعالج الداخلية RAM LRU فقط.
- **🎯 Flush by Pattern**: إدخال نمط مخصص لحذف مجموعة محددة من المفاتيح.
- **⏱️ Measure Ping**: قياس مباشر لزمن الاستجابة الحية بالمللي ثانية.
- **📋 Raw INFO**: استعراض التقرير التشخيصي الكامل من خادم Redis.
- **⚠️ Flush Entire DB (`FLUSHDB`)**: مسح قاعدة بيانات Redis المختارة بالكامل (مع رسالة تأكيد تحذيرية).

---

## ٥) التوثيق البرمجي للواجهات الخلفية (Backend REST API Reference)

جميع الواجهات البرمجية مدمجة ومحمية ضمن المسار الأساسي `/api/v1/redis/*`:

| Endpoint | Method | الصلاحية | الوظيفة |
| :--- | :--- | :--- | :--- |
| `/api/v1/redis/status` | `GET` | Authenticated | جلب المقاييس الحية، الذاكرة، معدل الإصابة، وحالة الاتصال |
| `/api/v1/redis/test` | `POST` | ADMIN | اختبار الاتصال بخادم Redis عبر المعاملات المرسلة |
| `/api/v1/redis/configure` | `POST` | ADMIN | حفظ الإعدادات وتحديث `.env` وإعادة الاتصال الحي |
| `/api/v1/redis/keys` | `GET` | Authenticated | البحث وتصفح المفاتيح وفق Pattern محدد |
| `/api/v1/redis/key` | `GET` | Authenticated | معاينة تفاصيل ومحتوى مفتاح محدد |
| `/api/v1/redis/key` | `DELETE`| ADMIN | حذف مفتاح معين من Redis و RAM |
| `/api/v1/redis/flush` | `POST` | ADMIN | مسح الكاش بحسب الهدف (`paxs`, `l1`, `pattern`, `all`) |
| `/api/v1/redis/ping` | `POST` | Authenticated | قياس زمن الذهاب والإياب لـ PING |
| `/api/v1/redis/info` | `GET` | ADMIN | قراءة مخرجات أمر Redis INFO الكاملة |

---

## ٦) الملفات المسؤولة في المشروع (Source Code Map)

- [`app/services/redis_service.py`](file:///D:/Paxs%20Archive/app/services/redis_service.py): محرك إدارة Redis والتخزين ذي المستويين ومقاييس الأداء.
- [`app/api/v1/redis_panel.py`](file:///D:/Paxs%20Archive/app/api/v1/redis_panel.py): نقاط النهاية لخدمات الـ API الخاصة باللوحة.
- [`app/services/warm_service.py`](file:///D:/Paxs%20Archive/app/services/warm_service.py): نظام التسخين المسبق للدراسات عند الفتح في عارض الأشعة.
- [`app/services/volume_service.py`](file:///D:/Paxs%20Archive/app/services/volume_service.py): نظام تخزين مصفوفات الأحجام ثلاثية الأبعاد 3D MPR في Redis.
- [`app/services/wado_rs_bulk_service.py`](file:///D:/Paxs%20Archive/app/services/wado_rs_bulk_service.py): استرجاع الإطارات السريع لعارض DICOMweb.
- [`app/static/index.html`](file:///D:/Paxs%20Archive/app/static/index.html): واجهة تبويب Redis (`#spanel-redis`) وبطاقات التحكم.
- [`app/static/app.js`](file:///D:/Paxs%20Archive/app/static/app.js): منطق الواجهة التفاعلي والتحديثات الحية والمسح والمعاينة.
- [`app/static/styles.css`](file:///D:/Paxs%20Archive/app/static/styles.css): تصميم الشارات والمقاييس لتبويب Redis.
- [`settings_explain.md`](file:///D:/Paxs%20Archive/settings_explain.md): التوثيق المرجعي الشامل لتبويبات الإعدادات.
