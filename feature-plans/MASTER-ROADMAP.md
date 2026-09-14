# 🗺️ PAXS Master Roadmap — خارطة الطريق الرئيسية

> **Single source of truth** — this document replaces the outdated
> `IMPLEMENTATION_CHECKPOINT.md` (which still shows C-MOVE at 50% and Webhooks
> at 0%, both shipped). Last updated: 2026-08-06.
>
> Evidence: **305 integration tests passing on SQLite** (after HL7 ORU — E2),
> HL7/worklist/dashboard subsets green on **MySQL 8.0**, 26 Alembic migrations on one
> linear head (downgrade→upgrade cycle verified), live server verified
> end-to-end (DICOM :10400 optional TLS, API :8000, HL7 :2575).

---

## 📋 52-Feature Status Matrix (canonical — verified 2026-08-06)

> Legend: ✅ Done · 🟡 Partial · ⬜ Planned (not yet) — **41 ✅ · 1 🟡 · 10 ⬜**.
> Details per feature below and in `plan.md`.

| # | Feature | Status |
| :-: | :--- | :--- |
| 1 | C-STORE (SCP/SCU) | ✅ |
| 2 | C-FIND (SCP/SCU) | ✅ |
| 3 | C-MOVE (SCP/SCU) | ✅ |
| 4 | C-GET (SCP/SCU) | ✅ |
| 5 | C-ECHO (SCP/SCU) | ✅ |
| 6 | Storage Commitment (N-ACTION/N-EVENT) | ✅ |
| 7 | Modality Worklist (MWL) | ✅ |
| 8 | MPPS | ✅ |
| 9 | IAN | ✅ |
| 10 | DICOM TLS | ✅ |
| 11 | Lossless Compression | ✅ |
| 12 | Lossy Compression | ✅ |
| 13 | VNA (Vendor Neutral Archive) | ✅ |
| 14 | Study Lifecycle Management | ✅ |
| 15 | Auto-routing Rules Engine | ✅ |
| 16 | Prefetching | ✅ |
| 17 | DICOMDIR Creation | ✅ |
| 18 | Migration Tools | ✅ |
| 19 | Transfer Accelerator | ✅ |
| 20 | WADO-URI | ✅ |
| 21 | WADO-RS / QIDO-RS / STOW-RS | ✅ |
| 22 | Hanging Protocols | ✅ |
| 23 | Prior Study Linking | ✅ |
| 24 | GSPS (Presentation State) | ✅ |
| 25 | Key Image Notes (KIN) | ✅ |
| 26 | 2D MPR Backend Prep | ✅ |
| 27 | 3D VRT / MIP Support | ✅ |
| 28 | Dose Monitoring (RDSR) | ✅ |
| 29 | Fusion (PET/CT, MRI/US) | ✅ |
| 30 | Worklist Management (Unread/Stat/Dictated) | ⬜ |
| 31 | Clinical Protocols Library | ⬜ |
| 32 | Peer Review / QA | ✅ |
| 33 | Critical Results Tracking | ✅ |
| 34 | Reporting / Dictation Bridge | 🟡 (reporting + ORU done; dictation ⬜) |
| 35 | Chat & Collaboration | ⬜ |
| 36 | Teaching Files | ⬜ |
| 37 | Study Split/Merge | ✅ |
| 38 | HL7 v2.x ORU/ORM/ADT | ✅ |
| 39 | LDAP / Active Directory | ✅ |
| 40 | XDS-I | ✅ |
| 41 | REST API (General) | ✅ |
| 42 | Webhooks | ✅ |
| 43 | Lua / Python Scripting | ✅ |
| 44 | FHIR ImagingStudy mapping | ✅ |
| 45 | Audit Logging (ATNA) | ✅ (SHA-256; ATNA XML ⬜) |
| 46 | Role-Based Access Control | ✅ |
| 47 | Load Balancing | ✅ (HAProxy + stateless nodes + liveness/readiness probes) |
| 48 | High Availability / Clustering | ⬜ |
| 49 | Disaster Recovery Replication | ✅ (background sweep to S3/named backend + replication_records) |
| 50 | Dashboard & Analytics | ✅ |
| 51 | Anonymization Engine | ✅ |
| 52 | Storage Tiering (HSM) | ✅ (via Study Lifecycle cold tier + S3) |

---

## ✅ 1. ما تم إنجازه (Done — verified)

### Core DICOM (Phase 1–2)
| Feature | Files | Status |
| :--- | :--- | :--- |
| **C-STORE SCP/SCU** (استقبال + إرسال/تحويل الصور) | `receiver.py` (SCP) + `sender.py` (`send_to_node_sync`) + `POST /api/v1/nodes/{ae}/send` | ✅ |
| C-FIND SCP/SCU (بحث المرضى/الدراسات) | `receiver.py` (SCP، 4 مستويات + wildcards + عزل VNA) + `sender.py` (`send_find`/`query_remote_node_sync`) + `POST /api/v1/nodes/{ae}/find` | ✅ |
| **C-GET SCP/SCU** (سحب دراسات من أرشيف بعيد بدون AE وجهة) | `receiver.py` (SCP) + `sender.py` (`send_get`/`pull_from_node_sync`) + `POST /api/v1/nodes/{ae}/pull` | ✅ |
| C-ECHO SCP/SCU | `app/dicom/receiver.py`, `sender.py` | ✅ |
| **C-MOVE SCP/SCU** + سجل العقد DICOM | `receiver.py`, `sender.py`, `node_service.py` | ✅ |
| DICOMweb: QIDO-RS / WADO-RS / STOW-RS / **WADO-URI** | `app/api/v1/dicomweb.py` | ✅ |
| **DICOMweb للعقد البعيدة** (QIDO-RS Query / WADO-RS Retrieve / STOW-RS Upload — بديل C-FIND/C-GET/C-STORE عبر REST) | `dicomweb_client.py` (SSRF guard: يرفض loopback/خاص/رابط-محلي/سحابة افتراضياً + `DICOMWEB_CLIENT_ALLOW_PRIVATE`) + `POST /api/v1/nodes/{ae}/web/query` و `/web/retrieve` و `/web/upload` (STOW-RS — multipart/related، FailedSOPSequence، مادرة ملف مؤقت للذاكرة) + حقول `web_url`/`web_username`/`web_password` في سجل العقد (Alembic u1234567v8w9) + زرا Web Query / Web Retrieve / Web Upload في لوحة Nodes | ✅ |
| **Storage Commitment SCP** (Push Model) | `commitment_statuses` + `handle_n_action` + `commit_sender` | ✅ |
| **Modality Worklist (MWL SCP)** | C-FIND على نفس المنفذ + auto-scheduling من HL7 ADT + إغلاق تلقائي عند وصول الصور + لوحة Schedule + REST | ✅ |
| **MPPS SCP/SCU** (N-CREATE/N-SET) | `mpps_service` + ربط حلقة MWL + SCU في `sender.py` + REST `/api/v1/mpps` + قسم Performed | ✅ |
| **IAN (Instance Availability Notification)** | N-CREATE DIMSE (SOP 1.2.840.10008.5.1.4.33) + `ian_service` + REST `/api/v1/ian` | ✅ |
| **DICOM TLS** (AES عبر TLS 1.2/1.3) | `app/dicom/tls.py` — شهادة ذاتية مولّدة تلقائياً + mTLS عبر `DICOM_TLS_CA` + SCU واعٍ بـ TLS | ✅ |

### Storage & Archive
| Feature | Status |
| :--- | :--- |
| SQLite + **MySQL 8.0** (Alembic, 9 migrations, دورات downgrade/upgrade) | ✅ |
| تخزين محلي بهيكل UIDs | ✅ |
| ضغط بلا فقدان (RLE مدمج + JPEG-LS/JPEG2000/HTJ2K اختياري) | ✅ |
| **Anonymizer** — تصدير ZIP مجهول حسب PS3.15 | ✅ (`/api/v1/export/{study}/anonymized`) |
| **MinIO / S3 (StorageBackend)** | ✅ واجهة موحدة + S3 (boto3، presigned) + محلي افتراضي + توافق عكسي |
| ضغط مع فقدان (Lossy) | ✅ قواعد تلقائية (modality + عمر ≥ أيام + جودة) + تشغيل يدوي + لوحة Storage |
| **VNA (Vendor Neutral Archive)** — عزلة متعددة المؤسسات/الأقسام | ✅ `tenants` + `tenant_ae_titles` + `studies.tenant_id`/`users.tenant_id`، عزل الاستعلامات حسب AE title، `/api/v1/tenants` + تبويب Tenants |
| **Study Lifecycle Management (14)** — auto-archive / restore / purge | ✅ `study_lifecycle_rules` + أعمدة `studies.lifecycle_state`/`archived_at` (Alembic t0123456q6r7) + `lifecycle_service` (قواعد ARCHIVE/RESTORE/PURGE بعمر/نمط/جزء، نقل فعلي للكائنات عبر `StorageBackend.move` — لا نسخ بايتات عبر العملية) + مسح worker تلقائي + REST `/api/v1/lifecycle` + قسم Lifecycle في تبويب Storage + **auto-archive للدراسات المسحوبة عبر WADO-RS** (خانة "Tier to cold storage" في نافذة Web Retrieve + إعداد `LIFECYCLE_AUTO_ARCHIVE_WADO_PULL`) |
| **Transfer Accelerator (19)** — نقل HTTP سريع يتجاوز C-STORE | ✅ `accelerator_service.py` — تقسيم الدراسة إلى دفعات (افتراضي 16) تُرفع عبر اتصالات STOW-RS متوازية (افتراضي 4، `TRANSFER_ACCELERATOR_WORKERS`) بتشفير **chunked transfer encoding** (`stow_upload(stream=True)` يبث multipart مباشرة من المخزن) + إعادة محاولة للدفعات الفاشلة عابراً (شبكة / HTTP ≥ 500) + إعدادات `TRANSFER_ACCELERATOR_*` + `POST /api/v1/nodes/{ae}/web/upload?accelerated=1&workers=` + زر **⚡ Fast Upload** وخانة "⚡ Accelerate" في لوحة Nodes + إحصائيات النقل (batches/workers/elapsed/MB/s) — tests/test_accelerator.py (10 اختبارات) |

### Viewer & Dashboard
| Feature | Status |
| :--- | :--- |
| لوحة تحكم ويب + عارض قراءة داكن (تبديل السلاسل، نوافذ WL/WW، تكبير، صور مصغرة) | ✅ |
| لوحة Nodes (إدارة عقد DICOM) + شارة HIS للمرضى المسجلين | ✅ |

### Clinical Workflow
| Feature | Status |
| :--- | :--- |
| Worker خلفي (ضغط + صور مصغرة + AI triage) | ✅ |
| **AI Emergency Triage** — خط أنابيب كاشفات قابل للتوصيل + جدول `triage_flags` + بانر | ✅ (كاشف استدلالي للاسترواح الصدري) |
| مشاركة المريض عبر QR | ✅ |
| **Modality Worklist (MWL SCP)** | ✅ تنفيذ كامل — C-FIND على نفس المنفذ، auto-scheduling من HL7 ADT، إغلاق تلقائي عند وصول الصور، لوحة Schedule + REST |
| **MPPS SCP** (N-CREATE/N-SET) | ✅ تنفيذ كامل — الجهاز يبلّغ عن بداية/نهاية الفحص، يربط خطوة MWL ويغلق الحلقة بالدراسة، REST `/api/v1/mpps` + قسم Performed |
| **Storage Commitment SCP** (Push Model) | ✅ تنفيذ كامل — N-ACTION → تحقق من ملفات backend (S3/محلي) → N-EVENT-REPORT تلقائياً (فوراً أو بعد C-STORE)، استرجاع عبر dicom_nodes، REST `/api/v1/commitments` + قسم Commitments في لوحة Schedule |

### Integration & Interop
| Feature | Status |
| :--- | :--- |
| **HL7 v2 ADT listener** (MLLP :2575) + auto-attach للدراسات + شارة HIS | ✅ |
| **HL7 v2 ORM (أوامر الفحوصات → MWL)** — أوامر RIS تصبح خطوات worklist تلقائياً (NW/XO/CA/OC/DC/CR) | ✅ (E1) |
| **FHIR R4 gateway** — Patient + ImagingStudy + CapabilityStatement | ✅ |
| **Webhooks** — `StudyReceived` عند وصول دراسة جديدة | ✅ |
| REST API إدارية كاملة (Swagger مولد تلقائياً) | ✅ |
| **كتابة التقارير إلى EMR (ORU^R01)** | ✅ (E2) — نموذج `reports` + `report_service` (بناء ORU يدوي MSH/PID/PV1/ORC/OBR/OBX مع LOINC 50005-5/50122-5 وهروب HL7) + إرسال MLLP مع ACK + حالة تسليم (SENT/FAILED/PENDING) + إعادة إرسال + REST `/api/v1/reports` |

### Security & Ops
| Feature | Status |
| :--- | :--- |
| JWT auth + RBAC (أول مستخدم يصبح ADMIN) | ✅ |
| Rate limiting (نافذة منزلقة، 429 + Retry-After) | ✅ |
| سجل تدقيق SHA-256 | ✅ |
| Docker Compose (MySQL 8.0 + خادم + ترحيل تلقائي) | ✅ |
| سكربتات EXE لنظام Windows (PyInstaller + NSSM) | ✅ (المثبّت NSIS لا) |
| **DICOM TLS** (AES عبر TLS 1.2/1.3) | ✅ تشفير DIMSE بشهادة ذاتية مولّدة تلقائياً + mTLS اختياري عبر `DICOM_TLS_CA` + SCU واعٍ بـ TLS (Ping/إعادة التوجيه) |
| هيدرات أمان (CSP/HSTS) | ⬜ لا |
| OpenTelemetry / مقاييس | ⬜ لا |

---

## ⬜ 2. ما لم يتم (Not done — grouped)

| المجموعة | البنود |
| :--- | :--- |
| **Clinical SCPs** | المرحلة A مكتملة (MWL ✅ + MPPS ✅ + Storage Commitment ✅) |
| **Enterprise storage** | الضغط المفقود، إدارة الحصة (S3 مكتمل ✅) |
| **Security** | هيدرات الأمان، CORS صارم، تشفير عند الراحة |
| **Automation** | Smart Router ✅ (C1) — التالي: B3 هيدرات الأمان + CORS صارم |
| **AI** | نماذج MONAI حقيقية، تنبيهات (Telegram/Slack)، حلقة تغذية راجعة |
| **Interop write-path** | **HL7 ORM → MWL ✅ (E1)** · تقارير → EMR ✅ (E2) — الحلقة مغلقة بالكامل |
| **Observability** | OpenTelemetry، نسخ احتياطي تلقائي، /metrics |
| **Distribution** | مثبّت NSIS، تحديث ذاتي |
| **Advanced** | PWA + OTP للمشاركة، عارض تعاوني، صوت→تقرير، Edge PACS، HA، IHE، إضافات |

---

## 🚦 3. المراحل القادمة — واحدة تلو الأخرى (Suggested execution order)

> كل مرحلة = وحدة عمل متكاملة تُنفَّذ وتُختبر (SQLite + MySQL) وتُوثَّق قبل الانتقال للتالية.

### المرحلة A — Clinical Core (أعلى قيمة)
| # | البند | ملاحظات |
| :-: | :--- | :--- |
| A1 | **Modality Worklist (MWL SCP)** | ✅ مكتمل (2026-08-05) — 13 اختباراً جديداً، DIMSE e2e حقيقي |
| A2 | **MPPS SCP** (N-CREATE/N-SET) | ✅ مكتمل (2026-08-05) — 9 اختبارات DIMSE e2e جديدة، حلقة MWL مغلقة بالجهاز كمصدر حقيقة |
| A3 | **Storage Commitment** | ✅ مكتمل (2026-08-05) — 7 اختبارات DIMSE e2e جديدة (N-ACTION → N-EVENT-REPORT، 0x0112/0x0110، تلقائي بعد C-STORE، استرجاع عبر dicom_nodes)، جدول `commitment_statuses` + REST + لوحة Commitments |

### المرحلة B — Enterprise Storage & Security
| # | البند | ملاحظات |
| :-: | :--- | :--- |
| B1 | **MinIO/S3 backend** (واجهة StorageBackend موحدة) | ✅ مكتمل (2026-08-05) — `STORAGE_BACKEND=s3`، مفاتيح نسبية، presigned URLs، توافق عكسي، اختبارات S3 بـ fake client |
| B2 | **DICOM TLS** | ✅ مكتمل (2026-08-05) — 5 اختبارات اتصال مشفرة حقيقية (C-ECHO/C-STORE عبر TLS، رفض الجلسات النصية، رفض العميل غير الموثوق)، شهادة ذاتية مولّدة تلقائياً بـ openssl، mTLS عبر `DICOM_TLS_CA` |
| B3 | هيدرات الأمان + CORS صارم |
| B4 | ضغط Lossy قابل للتهيئة |
| B5 | **VNA (Vendor Neutral Archive)** | ✅ مكتمل (2026-08-06) — 6 اختبارات تكامل جديدة، عزل ingest/query حسب AE title والمستخدم، لوحة Tenants، توافق عكسي كامل |
| B6 | **C-STORE SCU** (إرسال/تحويل إلى عقد DICOM) | ✅ مكتمل (2026-08-06) — 11 اختبار DIMSE جديد (round-trip حقيقي، إرسال دراسة كاملة لعقد مسجّل، trigger REST `POST /api/v1/nodes/{ae}/send` بمدى دراسة/سلسلة/صورة، عزل VNA، materialization لـ S3/MinIO قبل الإرسال، audit FORWARDED، أزرار Send في اللوحة) |
| B7 | **C-FIND SCU** (استعلام بحثي على عقد DICOM بعيدة) | ✅ مكتمل (2026-08-06) — 11 اختبار DIMSE جديد (`send_find` SCU عام بكل المستويات PATIENT/STUDY/SERIES/INSTANCE، استعلام على العقدة عبر REST `POST /api/v1/nodes/{ae}/find`، wildcards، prefetch يعيد استخدام SCU الموحد، إصلاح خلل SCP قائم: PatientID كان يُملأ بمفتاح FK الرقمي فيفشل ترميز DICOM) |
| B8 | **C-GET SCU** (سحب وترحيل دراسات من عقدة بعيدة) | ✅ مكتمل (2026-08-06) — 12 اختبار DIMSE جديد (`send_get` SCU — context تخزين + تفاوض SCP-role + جمع الحمولات عبر EVT_C_STORE، `pull_from_node_sync` يستوعب المثيلات عبر مسار C-STORE الكامل مع وسم VNA، REST `POST /api/v1/nodes/{ae}/pull` + زر Pull في اللوحة، إصلاح خلل SCP قائم: handle_get لم يكن يُصدِر عدد العمليات الفرعية أولاً فكان C-GET ينهار؛ ودعم storage contexts بأدوار صريحة في receiver) |
| B9 | **C-ECHO** (التحقق من الاتصال — تغطية اختبارات مخصصة) | ✅ مكتمل (2026-08-06) — 9 اختبارات جديدة في `tests/test_echo.py` (`send_echo` SCU ضد مستقبلنا، SCP تسجيل صدى مستقل، نظير غير متاح/غير DICOM، REST `POST /api/v1/nodes/{ae}/ping` بمسارات الخطأ 404/409، AE مخصص) — يغلق **بند 5 Core DICOM** وبهذا اكتملت Core DICOM (1-5) بالكامل |
| B10 | **MPPS SCU** (N-CREATE/N-SET إرسال) | ✅ مكتمل (2026-08-06) — `send_mpps_create`/`send_mpps_set` في sender.py (TLS-aware، فشل رشيق 0x0112/رفض الاتصال، AE مخصص)، 5 اختبارات SCU جديدة في test_mpps.py (دورة حياة كاملة N-CREATE→N-SET عبر SCU ضد SCP خاصتنا) — يغلق **بند 8 Core DICOM** بالكامل (SCP + SCU) |

### المرحلة C — Automation & Routing
| # | البند | ملاحظات |
| :-: | :--- | :--- |
| C1 | **Smart Router** — محرك قواعد (modality/body part → عقدة) + طابور إرسال مع إعادة محاولة | ✅ مكتمل (2026-08-06) — الطابور المتبقي أُكمل بالكامل: جدول `routing_queue` (Alembic v1234567w8x9) + `enqueue_dispatch` (idempotent لكل دراسة+عقدة) + `process_queue_batch` (سحب + C-STORE + إعادة محاولة بفاصل تصاعدي `retry_backoff × attempts²` حتى `max_attempts` ثم FAILED) + `retry_queue_item` يدوي + استرداد العناصر العالقة SENDING عند الإقلاع + مسح worker دوري (`ROUTING_SWEEP_EVERY_TICKS`) + REST `/api/v1/routing/queue` (قائمة/إعادة محاولة/تشغيل فوري) + قسم Send Queue في لوحة Smart Router + عدادات الطابور في إحصائيات البطاقة — 8 اختبارات جديدة في tests/test_routing_queue.py (idempotency، إرسال DIMSE حقيقي ناجح، retry ثم FAILED، إعادة محاولة يدوية، استرداد العالق، API بكل الأدوار) |

### المرحلة D — AI Upgrade
| # | البند |
| :-: | :--- |
| D1 | استبدال الكاشف الاستدلالي بنموذج MONAI حقيقي |
| D2 | تنبيهات (Telegram/Slack/PagerDuty) + حلقة قبول/رفض للطبيب |

### المرحلة E — Interop Write-Path
| # | البند | ملاحظات |
| :-: | :--- | :--- |
| E1 | HL7 **ORM** (أوامر الفحوصات) → MWL | ✅ مكتمل (2026-08-06) — 13 اختباراً جديداً في tests/test_orm.py (parser ORM^O01 + NW/XO/CA/OC/DC/CR + idempotency + DIMSE MWL e2e + MLLP listener)، `app/dicom/hl7.py` — لا جدول/هجرة جديدة (جدول A1 جاهز) |
| E2 | تقارير → EMR (ORU) | ✅ مكتمل (2026-08-06) — 9 اختبارات جديدة في tests/test_oru.py (بنية ORU^R01 + هروب HL7، CRUD عبر REST، بوابة دور 403، بدون EMR → PENDING، round-trip MLLP حقيقي مع ACK، EMR معطّل → FAILED ثم إعادة إرسال ناجحة، رفض إعادة إرسال المسودة 409، فلترة القائمة، finalize مزدوج idempotent)، جدول `reports` (Alembic w1234567x8y9) + `report_service` + REST `/api/v1/reports` |

### المرحلة F — Observability & Distribution
| # | البند |
| :-: | :--- |
| F1 | OpenTelemetry (مقاييس/تتبع/سجلات) + /metrics + نسخ احتياطي |
| F2 | مثبّت NSIS + تحديث ذاتي |

### المرحلة G — Advanced (Backlog)
| # | البند |
| :-: | :--- |
| G1 | PWA + OTP للمشاركة، عارض تعاوني (WebSocket)، صوت→تقرير، Edge PACS، HA، IHE، بنية إضافات |

---

## 📊 ملخص الأرقام

| المقياس | القيمة |
| :--- | :--- |
| اختبارات التكامل | **305** SQLite ✅ (بعد HL7 ORU — E2: 9 اختبارات جديدة) · MySQL subsets ✅ (Interop 43/43) |
| Migrations | 26 (رأس خطي واحد — دورة downgrade→upgrade موثّقة ✅) |
| خدمة DICOM على المنفذ | 10400 (C-STORE SCP/SCU + FIND/GET/MOVE/MPPS/Storage-Commit/ECHO) — TLS اختياري |
| API | :8000 — HL7 MLLP :2575 |
| Base DICOM | ✅ **100% — Core DICOM 1–10 مكتملة بالكامل** (C-STORE/FIND/MOVE/GET/ECHO + MWL + MPPS + Storage Commitment + IAN + TLS) |
