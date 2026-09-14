# PAXS Archive — Settings & Admin Console Guide
# دليل الإعدادات ولوحة التحكم — أرشيف PAXS

> **Applies to / يشمل:** `http://localhost:8000/#settings` and / و `http://localhost:8000/#admin`
>
> These views are **admin-only** — sign in with an `ADMIN` account to use them. Changes made here persist to the database and apply live (a few require a server restart, marked *restart required* below).
>
> هذان العرضان **للإدارة فقط** — سجّل الدخول بحساب `ADMIN` لاستخدامهما. التغييرات تُحفظ في قاعدة البيانات وتُطبَّق فورًا (بعضها يتطلب إعادة تشغيل الخادم، ومُعلَّم بـ *مطلوب إعادة تشغيل* أدناه).

---

## Part 1 — Settings · `#settings` / الجزء الأول — الإعدادات

The Settings view groups every runtime-editable server setting into 8 tabs. The form shows the **live** value the server is actually using (database override if present, otherwise the `.env` default). Any change you make appears in a bottom **save bar** — *Apply changes* writes them all, *Discard* cancels.

تعرض صفحة الإعدادات كل إعدادات الخادم القابلة للتعديل في 8 تبويبات. يعرض النموذج القيمة **الحية** الفعلية التي يستخدمها الخادم (قيمة قاعدة البيانات إن وُجدت، وإلا الافتراضي من `.env`). أي تعديل يظهر في **شريط الحفظ** السفلي — زر *تطبيق التغييرات* يحفظ الكل، وزر *تجاهل* يلغي.

### 1. System / النظام
Server identity and public-facing defaults. / هوية الخادم والإعدادات العامة الظاهرة للمستخدمين.

| Field / الحقل | Purpose / الوظيفة |
|---|---|
| Application name / اسم التطبيق | Display name in the header & login panel / الاسم الظاهر في الترويسة وصفحة الدخول |
| Public base URL / الرابط العام الأساسي | External URL used in patient share links & QR codes (behind a reverse proxy) / الرابط الخارجي المستخدم في روابط المشاركة وأكواد QR (خلف بروكسي عكسي) |
| Share link expiry (days) / صلاحية رابط المشاركة (أيام) | Default lifetime of patient share links / العمر الافتراضي لروابط مشاركة المريض |
| DICOMweb public access / الوصول العام لـ DICOMweb | Allow QIDO/WADO/STOW without a login token / السماح بالاستعلام والسحب والرفع بدون رمز دخول |

### 2. DICOM / الإعدادات
The DIMSE receiver — how modalities talk to the archive over the DICOM protocol. / مستقبل DIMSE — كيف تتواصل أجهزة التصوير مع الأرشيف عبر بروتوكول DICOM.

| Field / الحقل | Purpose / الوظيفة |
|---|---|
| AE title / اسم الكيان | Application Entity title the receiver advertises (*restart*) / اسم الكيان الذي يعلنه المستقبل (*إعادة تشغيل*) |
| DIMSE port / منفذ DIMSE | TCP port for C-STORE/C-FIND/C-GET/C-MOVE/MPPS/Storage Commitment (*restart*) / منفذ TCP للعمليات (*إعادة تشغيل*) |
| DICOM receiver / مستقبل DICOM | Start the DIMSE listener on boot (*restart*) / تشغيل مستقبل DIMSE عند الإقلاع (*إعادة تشغيل*) |
| Max associations / أقصى عدد اتصالات | Concurrent DIMSE association cap (*restart*) / الحد الأقصى للاتصالات المتزامنة (*إعادة تشغيل*) |
| DICOM TLS (AES) / تشفير DICOM | Wrap DIMSE in TLS 1.2/1.3 with a self-signed cert (*restart*) / تشفير DIMSE عبر TLS بشهادة ذاتية التوقيع (*إعادة تشغيل*) |
| MWL default station AE / محطة MWL الافتراضية | Station AE stamped on ADT-created worklist steps / محطة العمل المثبتة على خطوات قائمة العمل المنشأة من ADT |

### 3. Modality / الأجهزة
A **live registry** of every modality currently present in the archive (sourced from real study data), with series counts. It drives the modality filter, worklist and routing rules. No editing needed — it is a read-only dashboard.

سجل **حي** بكل أنواع الأجهزة الموجودة فعليًا في الأرشيف (مستمد من بيانات الفحوصات الحقيقية) مع عدد السلاسل. يغذّي فلتر الأجهزة وقائمة العمل وقواعد التوجيه. للعرض فقط — لا يتطلب تعديلًا.

### 4. Interop / التكامل
HL7 + FHIR gateways to the hospital HIS/EMR. / بوابات HL7 و FHIR مع نظام المستشفى.

| Field / الحقل | Purpose / الوظيفة |
|---|---|
| HL7 MLLP listener / مستقبل HL7 | Receive ADT admissions & ORM imaging orders from the HIS (*restart*) / استقبال إدخالات ADT وأوامر التصوير ORM من نظام المستشفى (*إعادة تشغيل*) |
| HL7 bind host / عنوان الربط | MLLP listen address (*restart*) / عنوان استماع MLLP (*إعادة تشغيل*) |
| HL7 MLLP port / منفذ MLLP | MLLP listen port (*restart*) / منفذ استماع MLLP (*إعادة تشغيل*) |
| ORU report write-back / إرسال التقارير | Send ORU^R01 to the EMR when a report is finalized / إرسال ORU^R01 إلى السجل الطبي الإلكتروني عند اعتماد تقرير |
| EMR host · port · application (MSH-5) · facility (MSH-6) | Destination EMR details for report delivery / بيانات السجل الطبي الإلكتروني الهدف لتسليم التقارير |
| Sending application (MSH-3) · facility (MSH-4) | Who the HL7 messages appear to come from / هوية مرسل رسائل HL7 الظاهرة |
| FHIR R4 endpoint / نقطة FHIR | Serve the `/fhir` REST API (*restart*) / تفعيل واجهة `/fhir` (*إعادة تشغيل*) |

### 5. Storage / التخزين
Where DICOM objects live and how they are compressed. / أين تُخزَّن ملفات DICOM وكيف تُضغط.

| Field / الحقل | Purpose / الوظيفة |
|---|---|
| Storage backend / نظام التخزين | Filesystem under `STORAGE_ROOT`, or an S3-compatible object store (*restart*) / نظام ملفات أو مخزن S3 متوافق (*إعادة تشغيل*) |
| S3 endpoint · region · bucket · presign expiry | S3/MinIO connection details & signed-URL lifetime / بيانات اتصال S3/MinIO وعمر الروابط الموقعة |
| Default compression / الضغط الافتراضي | Transfer syntax used by the worker: `RLE` · `JPEG-LS` · `JPEG2000` · `HTJ2K` / صيغة النقل المستخدمة في العامل الخلفي |
| Background compression / الضغط الخلفي | Enable the async compression worker / تفعيل عامل الضغط غير المتزامن |
| Thumbnails / الصور المصغرة | Generate thumbnails for the study list / توليد الصور المصغرة لقائمة الفحوصات |
| Warm on viewer open / التحضير عند الفتح | Pre-generate thumbnails + warm frame caches the moment a study opens / توليد الصور المصغرة وتجهيز ذاكرة الإطارات فور فتح الفحص |

### 6. Security / الأمان
Sessions, registration, rate limits and anonymization. / الجلسات والتسجيل وحدود المعدل وإخفاء الهوية.

| Field / الحقل | Purpose / الوظيفة |
|---|---|
| Session lifetime (min) / عمر الجلسة (دقيقة) | JWT expiry for dashboard logins / صلاحية رمز JWT لتسجيل دخول لوحة التحكم |
| Allow self-registration / السماح بالتسجيل الذاتي | Let new users create accounts from the login panel / السماح بإنشاء حسابات من لوحة الدخول |
| Rate limiting / تحديد المعدل | Sliding-window per-IP abuse protection / حماية من الإساءة عبر نافذة منزلقة لكل عنوان IP |
| Login attempts / min · Global requests / min | Per-IP throttles / حدود المعدل لكل عنوان |
| Allow private web peers / السماح بالأقران الخاصة | Permit node QIDO/WADO/STOW to loopback/private IPs — **SSRF guard off** / السماح باتصالات الأقران بعناوين خاصة — **تعطيل حماية SSRF** |
| Anonymizer: strip private tags · strip dates | Remove vendor tags / dates when de-identifying / إزالة الوسوم الخاصة والتواريخ عند إخفاء الهوية |

### 7. Workflow / سير العمل
Background automation policies. / سياسات الأتمتة الخلفية.

| Field / الحقل | Purpose / الوظيفة |
|---|---|
| AI emergency triage / الفرز الطارئ بالذكاء الاصطناعي | Run the critical-finding detector pipeline in the worker / تشغيل خط كاشف النتائج الحرجة في العامل الخلفي |
| Triage max slices / series | Cap detector work per series / سقف الشرائح المعالجة لكل سلسلة |
| Lifecycle auto-archive sweep / الأرشفة التلقائية | Worker evaluates archive/restore/purge rules on schedule / تقييم قواعد الأرشفة والاستعادة والحذف دوريًا |
| Auto-tier WADO pulls to cold / تحويل السحوبات للبارد | Tier Web-retrieved studies straight to cold storage / نقل الفحوصات المسحوبة عبر الويب مباشرة للتخزين البارد |
| Smart Router send queue / طابور التوجيه | Enable the durable dispatch queue / تفعيل طابور الإرسال الدائم |
| Router max attempts · retry backoff (s) | Dispatch resilience / مرونة إعادة المحاولة |
| Transfer Accelerator / مسرّع النقل | Parallel chunked STOW-RS fast uploads — workers & batch size / رفع سريع متوازي عبر STOW-RS — عدد العمال وحجم الدفعة |

### 8. Database / قاعدة البيانات
Three tools for the database underneath the archive. / ثلاث أدوات لقاعدة البيانات الأساسية.

| Card / البطاقة | Purpose / الوظيفة |
|---|---|
| **Database engine / محرك قاعدة البيانات** | Switch between MySQL 8.0 and SQLite with host/port/user/password/name. *Test connection* probes without writing; *Save configuration* rewrites `DATABASE_URL` in `.env` (restart needed). / التبديل بين MySQL 8.0 و SQLite مع بيانات الاتصال. زر *اختبار الاتصال* يفحص دون كتابة؛ *حفظ الإعدادات* يعيد كتابة `DATABASE_URL` في `.env` (يتطلب إعادة تشغيل). |
| **Initialize database / تهيئة قاعدة البيانات** | Run every Alembic migration to head on a new (or existing) install; auto-creates the MySQL database if missing; optional *seed an admin account* if none exists. / تشغيل كل ترحيلات Alembic حتى الأحدث؛ إنشاء قاعدة MySQL تلقائيًا إن لم توجد؛ خيار *إنشاء حساب إداري* إن لم يوجد. |
| **Backup / النسخ الاحتياطي** | Snapshot to a downloadable dump in `./backups` — SQLite uses the hot-backup engine, MySQL uses `mysqldump` (or built-in SQL export). / لقطة قابلة للتنزيل في `./backups` — SQLite عبر محرك النسخ السريع، MySQL عبر `mysqldump`. |

---

## Part 2 — Admin Console · `#admin` / الجزء الثاني — لوحة التحكم

The Admin Console is a live matrix of every completed PAXS module — all read from the **running server**, not static files. It has 13 tabs.

لوحة التحكم هي مصفوفة حيّة لكل وحدات PAXS المكتملة — تُقرأ جميعها من **الخادم الجاري** وليست ملفات ثابتة. تضم 13 تبويبًا.

### 1. Overview / نظرة عامة
Live health of the whole server: app name & version, node instance, database status, DICOM receiver (AE title:port), DICOM TLS, HL7 and FHIR status, storage backend, compression method, rate limit, plus counters: users, DICOM nodes, active shares, key-image notes, audit events, VNA tenants, pending jobs.

صحة الخادم كاملًا: اسم التطبيق والإصدار، المثيل، حالة قاعدة البيانات، مستقبل DICOM، تشفير DICOM، حالة HL7 و FHIR، نظام التخزين، طريقة الضغط، حد المعدل، مع عدّادات: المستخدمون، عقد DICOM، المشاركات النشطة، ملاحظات الصور المفتاحية، أحداث التدقيق، مستأجرو VNA، المهام المعلقة.

### 2. DICOM Services / خدمات DICOM
Live DIMSE capability matrix — the SOP classes the receiver advertises and the SCU helpers PAXS ships: **C-STORE, C-FIND, C-GET, C-MOVE, C-ECHO, MWL, MPPS, Storage Commitment**, plus a second grid for **Web & integration layers**.

مصفوفة قدرات DIMSE الحية — فئات SOP التي يعلنها المستقبل وأدوات SCU المرفقة: **C-STORE, C-FIND, C-GET, C-MOVE, C-ECHO, MWL, MPPS, Storage Commitment**، مع شبكة ثانية لطبقات **الويب والتكامل**.

### 3. Smart Router / الموجّه الذكي
**Auto-routing rules engine:** when a new study matches a rule (modality · body part · destination node · optional description pattern), PAXS enqueues it and the worker C-STORE's it with automatic retry & backoff. Create rules (+ Add rule), toggle enable/disable, delete. Below: the **send queue (C1)** — durable dispatches filterable by status (Pending / Sending / Completed / Failed) with attempt counts, retry times and errors, plus a **Sweep now ▶** button to dispatch due items immediately.

**محرك قواعد التوجيه التلقائي:** عند مطابقة فحص جديد لقاعدة (الجهاز · منطقة الجسم · العقدة الهدف · نمط الوصف)، يضيفه PAXS إلى الطابور ويرسله العامل عبر C-STORE مع إعادة محاولة تلقائية. أنشئ القواعد وأوقف/فعّل واحذف. أدناه: **طابور الإرسال (C1)** — إرسالات دائمة يمكن فلترتها بالحالة مع عدد المحاولات وأوقات إعادة المحاولة والأخطاء، وزر **مسح الآن ▶**.

### 4. Critical Results / النتائج الحرجة
Track critical findings per study. Flag a study with a level — **CRITICAL_RED · URGENT_ORANGE · ACTIONABLE_YELLOW** — and a description (e.g. *"Pneumothorax — call referring physician"*), then review the list. (Integrates with the AI triage in Settings → Workflow.)

تتبع النتائج الحرجة لكل فحص. علّم فحصًا بمستوى — **أحمر حرج · برتقالي عاجل · أصفر قابل للتصرف** — مع وصف، ثم راجع القائمة. (متكامل مع الفرز الذكي في الإعدادات ← سير العمل.)

### 5. Storage / التخزين
Three sections:
- **Storage info** — live backend status grid.
- **Storage backends (MULTI):** named backends (local folders + S3/MinIO buckets). First row is the default; each instance records its own backend, so studies can move between backends without re-ingesting. Register a backend (name, kind local/s3, root/endpoint, bucket, region, keys) and **Move study →** between backends.
- **Lossy compression rules (B4):** modality + age in days + quality → transcodes matching studies.
- **Study lifecycle (14):** tier studies hot ↔ cold with **ARCHIVE / RESTORE / PURGE** rules (name, action, modality, age, body part); the worker sweeps automatically, **Run now ▶** evaluates on demand; archived (cold) studies listed below.

ثلاثة أقسام:
- **معلومات التخزين** — شبكة حالة الخلفية الحية.
- **خلفيات التخزين (متعدد):** خلفيات مسماة (مجلدات محلية + دلاء S3/MinIO). الصف الأول هو الافتراضي؛ يسجّل كل مثيل خلفيته الخاصة، فيمكن نقل الفحوصات بين الخلفيات دون إعادة إدخال. سجّل خلفية و**انقل الفحص ←** بين الخلفيات.
- **قواعد الضغط الفاقد (B4):** جهاز + عمر بالأيام + جودة → إعادة ترميز الفحوصات المطابقة.
- **دورة حياة الفحص (14):** تقسيم الفحوصات بين الساخن والبارد بقواعد **أرشفة / استعادة / حذف**؛ العامل يمسح تلقائيًا وزر **تشغيل الآن ▶** للتقييم الفوري؛ الفحوصات المؤرشفة (الباردة) تظهر أدناه.

### 6. Migration / الترحيل
**Folder import:** point at a local folder (e.g. `C:\dicom-inbox`) and *Start folder import* — the server scans, ingests and indexes every DICOM file found, showing progress in the list.

**استيراد مجلد:** حدد مجلدًا محليًا واضغط *بدء استيراد المجلد* — يفحص الخادم كل ملف DICOM ويستورده ويفهرسه مع عرض التقدم في القائمة.

### 7. Audit / التدقيق
Tamper-evident **audit trail**: every meaningful operation is logged with a SHA-256 fingerprint, so the record can be verified against tampering. Read-only review list.

**سجل تدقيق** مضاد للتلاعب: كل عملية مهمة تُسجَّل ببصمة SHA-256 يمكن التحقق منها. قائمة مراجعة للقراءة فقط.

### 8. Scripts / السكربتات
Custom **server scripts (roadmap #43)** — drop `.py` files in the `scripts/` folder; they load at runtime with **hot reload** (edits take effect on the next event, no restart). Trusted admin code, Orthanc-Lua style. Buttons: **↻ Reload all**, **+ New script** (from a template), then view/edit the source of any script.

**سكربتات الخادم المخصصة (خارطة الطريق #43)** — ضع ملفات `.py` في مجلد `scripts/`؛ تُحمَّل وقت التشغيل مع **إعادة تحميل ساخنة** (تسري التعديلات عند الحدث التالي دون إعادة تشغيل). كود إداري موثوق بأسلوب Orthanc-Lua. أزرار: **إعادة تحميل الكل**، **سكربت جديد** (من قالب)، ثم عرض/تحرير مصدر أي سكربت.

### 9. Replication / النسخ المتماثل
Disaster-recovery replication: copies every study's objects to an off-site S3/Azure-Blob target (a registered `s3` backend via `DR_REPLICATION_TARGET`, or env S3 config). **⟳ Sweep now** runs the worker pass on demand; **Replicate study →** copies one study immediately; the records list shows what was replicated and when.

النسخ المتماثل للتعافي من الكوارث: ينسخ كل كائنات الفحوصات إلى هدف S3/Azure-Blob خارجي (خلفية `s3` مسجلة عبر `DR_REPLICATION_TARGET` أو إعدادات S3). زر **⟳ مسح الآن** يشغّل جولة العامل فورًا؛ **نسخ الفحص ←** ينسخ فحصًا واحدًا؛ قائمة السجلات تعرض ما نُسخ ومتى.

### 10. Protocols / البروتوكولات
**Clinical Protocols Library (#31):** map study descriptions / modalities / body parts to a standardized protocol with a local RIS code. The archive auto-suggests the best protocol for incoming studies. Add a protocol (name, standardized name, RIS code, modality, body part, priority, description pattern — comma-separated case-insensitive alternatives), then use the **Match tester** to see which protocol a given description/modality/body part resolves to.

**مكتبة البروتوكولات السريرية (#31):** اربط أوصاف الفحوصات والأجهزة وأجزاء الجسم ببروتوكول موحّد برمز RIS محلي. يقترح الأرشيف أفضل بروتوكول تلقائيًا للفحوصات الواردة. أضف بروتوكولًا ثم استخدم **مُختبِر المطابقة** لمعرفة البروتوكول الذي يطابق وصفًا معينًا.

### 11. Tenants / المستأجرون
**VNA (vendor-neutral archive) tenancy:** studies ingested from a tenant-mapped AE title — or via STOW by a tenant user — are isolated to that tenant. Queries from those AEs/users only see their own data; unmapped traffic stays in the legacy scope. Add tenants (name, code, description).

**استئجار VNA (أرشيف محايد للبائعين):** الفحوصات الواردة من كيان AE مرتبط بمستأجر — أو عبر STOW من مستخدم مستأجر — تُعزل لذلك المستأجر. استعلامات هذه الكيانات لا ترى إلا بياناتها؛ حركة المرور غير المرتبطة تبقى في النطاق القديم. أضف مستأجرًا (الاسم، الرمز، الوصف).

### 12. Users / المستخدمون
Every account on the server with roles: **ADMIN · RADIOLOGIST · PHYSICIAN · TECHNOLOGIST**. Roles gate what each user can do (destructive actions, settings, tenants). Create accounts (+ Add user), reset passwords, enable/disable, delete — but you can **never delete, disable or demote your own account**, and the archive always keeps at least one active ADMIN.

كل الحسابات على الخادم بأدوار: **مدير · أخصائي أشعة · طبيب · تقني**. الأدوار تحدّد صلاحيات كل مستخدم (الإجراءات التدميرية والإعدادات والمستأجرون). أنشئ الحسابات وأعد تعيين كلمات المرور وفعّل/عطّل واحذف — لكن لا يمكنك **أبدًا حذف أو تعطيل أو خفض حسابك الخاص**، ويحافظ الأرشيف دائمًا على مدير نشط واحد على الأقل.

### 13. DB Pool / تجمع قاعدة البيانات
Read-replica / connection-pool performance dashboard:
- **Pool metrics** — live pool stats + MySQL counters.
- **Read replica (RO):** route the two hottest read endpoints (`/stats`, `/studies`) through a MySQL read replica — set `DB_READ_REPLICA_URL` in `.env` and restart; the toggle persists, and reads fall back to the primary automatically if the replica is unreachable.
- **Slow-query log:** statements at/above a threshold (ms) land in an in-memory ring (last 200); toggle on/off, set the threshold, refresh.
- **EXPLAIN analyzer (PLAN):** run the query planner on the hottest archive queries (MySQL `EXPLAIN` / SQLite `EXPLAIN QUERY PLAN`) — pick a hot query, or type custom read-only SQL — to spot missing indexes and full scans.

لوحة أداء القراءة الموزعة وتجمع الاتصالات:
- **مقاييس التجمع** — إحصائيات حية + عدّادات MySQL.
- **قراءة النسخة (RO):** توجيه أكثر نقطتين قراءةً حرارة (`/stats`, `/studies`) عبر نسخة قراءة MySQL — اضبط `DB_READ_REPLICA_URL` في `.env` وأعد التشغيل؛ يستمر التبديل ويعود القراءة للرئيسي تلقائيًا عند تعذر النسخة.
- **سجل الاستعلامات البطيئة:** العبارات التي تصل أو تتجاوز حدًا (مللي ثانية) تُسجَّل في حلقة ذاكرة (آخر 200)؛ فعّل/عطّل وحدد الحد وحدّث.
- **محلّل EXPLAIN:** شغّل مخطط الاستعلام على أكثر استعلامات الأرشيف حرارة (MySQL `EXPLAIN` / SQLite `EXPLAIN QUERY PLAN`) — اختر استعلامًا ساخنًا أو اكتب SQL مخصصًا للقراءة فقط — لاكتشاف الفهارس المفقودة والفحص الكامل.

---

## Access & Safety Notes / ملاحظات الوصول والأمان
- Both views require an **ADMIN** login; non-admin users see a sign-in prompt or a denied state.
- كلاهما يتطلب تسجيل دخول **ADMIN**؛ غير المديرين يرون طلب تسجيل الدخول أو حالة مرفوضة.
- Settings marked *restart required* apply at next boot — the UI shows the hint before you save.
- الإعدادات المعلّمة *مطلوب إعادة تشغيل* تُطبَّق عند الإقلاع التالي — تعرض الواجهة التنبيه قبل الحفظ.
- Destructive actions (delete study, purge, user delete) go through confirmation modals.
- الإجراءات التدميرية (حذف فحص، حذف بيانات، حذف مستخدم) تمر عبر نوافذ تأكيد.
- The **save bar** shows how many unsaved settings you have; *Apply changes* validates every value against its type before persisting.
- **شريط الحفظ** يعرض عدد الإعدادات غير المحفوظة؛ زر *تطبيق التغييرات* يتحقق من كل قيمة قبل الحفظ.
