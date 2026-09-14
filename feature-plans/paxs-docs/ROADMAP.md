# خريطة الطريق — PAXS Server Roadmap

> جدول زمني واضح من بناء النواة حتى الإنتاج الكامل في المستشفيات.

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

## 🗺️ نظرة عامة على المسار

```
Phase 1          Phase 2          Phase 3          Phase 4          Phase 5
[Foundation] ──> [DICOM Engine] ──> [DICOMweb API] ──> [Production] ──> [Distribution]
  أسبوع 1-2       أسبوع 3-4         أسبوع 5-6         أسبوع 7-8       أسبوع 9-10
```

---

## 🔵 Phase 1 — البيئة وتصميم النواة (الأسابيع 1-2)

**الهدف:** بيئة عمل مستقرة + قاعدة بيانات جاهزة

| # | المهمة | الأولوية | الحالة |
|---|--------|----------|--------|
| 1.1 | إعداد المستودع Git وهيكل المجلدات | 🔴 ضروري | 🔲 |
| 1.2 | إنشاء بيئة Python وتثبيت جميع الاعتمادات | 🔴 ضروري | 🔲 |
| 1.3 | كتابة نماذج ORM (Patient/Study/Series/Instance) | 🔴 ضروري | 🔲 |
| 1.4 | إعداد Alembic وتطبيق الهجرات على MySQL | 🔴 ضروري | 🔲 |
| 1.5 | تطوير `parser.py` لاستخراج DICOM Tags | 🔴 ضروري | 🔲 |
| 1.6 | تطوير `storage_service.py` بهيكل `study/series/sop.dcm` | 🔴 ضروري | 🔲 |
| 1.7 | Docker Compose للـ MySQL و Redis | 🟡 مهم | 🔲 |
| 1.8 | اختبار حفظ ملف DICOM تجريبي في DB والقرص | 🔴 ضروري | 🔲 |

**✅ معيار الاكتمال:** الـ Checkpoint CP-01 يمر بنجاح

---

## 🟡 Phase 2 — محرك شبكات DICOM (الأسابيع 3-4)

**الهدف:** استقبال وإرسال صور الأشعة عبر بروتوكول DIMSE

| # | المهمة | الأولوية | الحالة |
|---|--------|----------|--------|
| 2.1 | بناء `receiver.py` — C-STORE SCP على المنفذ 10400 | 🔴 ضروري | 🔲 |
| 2.2 | دعم كافة `Presentation Contexts` لأنواع الأجهزة | 🔴 ضروري | 🔲 |
| 2.3 | ربط `EVT_C_STORE` handler لاستدعاء parser + storage | 🔴 ضروري | 🔲 |
| 2.4 | الكتابة الأسنكرونية في DB (Thread-safe AsyncIO) | 🔴 ضروري | 🔲 |
| 2.5 | بناء `finder.py` — C-FIND SCP للـ Worklist | 🟡 مهم | 🔲 |
| 2.6 | بناء `sender.py` — C-STORE SCU لإعادة الإرسال | 🟡 مهم | 🔲 |
| 2.7 | اختبار باستخدام `storescu` (dcmtk) من محطة خارجية | 🔴 ضروري | 🔲 |
| 2.8 | إعداد Celery + Redis لمهام الضغط الخلفية | 🟡 مهم | 🔲 |
| 2.9 | بناء `compression_service.py` (JPEG-LS Lossless) | 🟡 مهم | 🔲 |

**✅ معيار الاكتمال:** الـ Checkpoint CP-02 يمر بنجاح

---

## 🟢 Phase 3 — DICOMweb API + OHIF Viewer (الأسابيع 5-6)

**الهدف:** عارض صور طبي يعمل بالكامل في المتصفح

| # | المهمة | الأولوية | الحالة |
|---|--------|----------|--------|
| 3.1 | إنشاء مسارات QIDO-RS `GET /dicom-web/studies` | 🔴 ضروري | 🔲 |
| 3.2 | تنسيق الردود بـ `application/dicom+json` (PS3.18) | 🔴 ضروري | 🔲 |
| 3.3 | إنشاء مسارات WADO-RS لجلب الصور والـ frames | 🔴 ضروري | 🔲 |
| 3.4 | دعم Multipart Streaming Responses للصور الكبيرة | 🔴 ضروري | 🔲 |
| 3.5 | إنشاء مسارات STOW-RS لقبول الرفع من المتصفح | 🟡 مهم | 🔲 |
| 3.6 | ضبط إعدادات CORS في Nginx خصيصاً لـ OHIF | 🔴 ضروري | 🔲 |
| 3.7 | تكوين OHIF Viewer v3 وتوجيهه للـ DICOMweb API | 🔴 ضروري | 🔲 |
| 3.8 | اختبار فتح فحص CT كامل (200+ صورة) في OHIF | 🔴 ضروري | 🔲 |
| 3.9 | بناء `sharing_service.py` — رابط + QR للمريض | 🟡 مهم | 🔲 |

**✅ معيار الاكتمال:** الـ Checkpoints CP-03 + CP-04 تمران بنجاح

---

## 🔴 Phase 4 — الأمان والتوسعة والإنتاجية (الأسابيع 7-8)

**الهدف:** نظام جاهز للنشر في مستشفى حقيقي

| # | المهمة | الأولوية | الحالة |
|---|--------|----------|--------|
| 4.1 | JWT Authentication — تسجيل دخول وصلاحيات | 🔴 ضروري | 🔲 |
| 4.2 | Role-Based Access (RADIOLOGIST / PHYSICIAN / PATIENT) | 🔴 ضروري | 🔲 |
| 4.3 | DICOM TLS لتشفير اتصالات C-STORE مع الأجهزة | 🟡 مهم | 🔲 |
| 4.4 | SSL/TLS على Nginx (شهادات Let's Encrypt أو CA) | 🔴 ضروري | 🔲 |
| 4.5 | SHA-256 Tamper Detection لكل صورة (Audit Log) | 🟡 مهم | ✅ |
| 4.6 | Rate Limiting لحماية API من الإرهاق | 🟡 مهم | ✅ |
| 4.7 | Smart Tiered Storage (Hot → Warm → Cold) | 🟢 ميزة | 🔲 |
| 4.8 | MinIO / S3 كـ Storage Backend بديل للتخزين المحلي | 🟢 ميزة | 🔲 |
| 4.9 | اختبار الحمل العالي (500 صورة متزامنة) | 🔴 ضروري | 🔲 |
| 4.10 | تحزيم Docker Compose + Helm Charts | 🟡 مهم | 🔲 |

**✅ معيار الاكتمال:** الـ Checkpoint CP-05 يمر بنجاح

---

## 🪟 Phase 5 — التوزيع (الأسابيع 9-10)

**الهدف:** ملف EXE جاهز للنشر في بيئات Windows بدون تثبيت Python

| # | المهمة | الأولوية | الحالة |
|---|--------|----------|--------|
| 5.1 | كتابة `run_paxs.py` — نقطة الدخول الموحدة | 🔴 ضروري | ✅ |
| 5.2 | اختبار تشغيل DICOM Receiver + FastAPI في Threads | 🔴 ضروري | ✅ |
| 5.3 | بناء EXE بـ PyInstaller `--onedir` | 🔴 ضروري | 🔲 (spec + scripts جاهزة) |
| 5.4 | اختبار التشغيل على جهاز Windows نظيف | 🔴 ضروري | 🔲 |
| 5.5 | تثبيت كـ Windows Service باستخدام NSSM | 🟡 مهم | 🔲 |
| 5.6 | إنشاء مثبت GUI (NSIS Installer) | 🟢 ميزة | 🔲 |
| 5.7 | توثيق دليل التثبيت للمستشفيات | 🟡 مهم | 🔲 |

---

## 💡 مميزات مستقبلية (Future Features Backlog)

مرتبة حسب الأولوية والقيمة التجارية:

| الأولوية | الميزة | الوصف |
|----------|--------|-------|
| ⭐⭐⭐ | **AI Emergency Triage** | MONAI لاكتشاف Stroke/Pneumothorax تلقائياً + إشعار فوري |

> **تم التنفيذ (Phase 4.6):** AI Emergency Triage pipeline ✅ (worker detector
> pipeline + `triage_flags` table + dashboard banner — بفاحص Pneumothorax تجريبي،
> واجهة pluggable لنماذج MONAI لاحقاً)
| ⭐⭐⭐ | **Voice-to-Report** | Whisper لتحويل صوت الطبيب إلى تقرير طبي مكتوب |
| ⭐⭐⭐ | **Zero-CD Patient Sharing** | QR Code + PWA للمريض بدلاً من أقراص CD |
| ⭐⭐ | **Real-Time Collaborative Viewer** | WebSockets لمزامنة عارض الصور بين أطباء في مدن مختلفة |
| ⭐⭐ | **HL7 / FHIR Gateway** | ربط PAXS بأنظمة EMR/HIS الخاصة بالمستشفى |
| ⭐⭐ | **Smart DICOM Router** | توزيع الفحوصات على أجهزة مختلفة حسب قواعد مخصصة |
| ⭐ | **Automated Anonymizer** | إزالة PHI للبحث العلمي + تدريب الذكاء الاصطناعي |

> **تم التنفيذ (Phase 4.5):** Rate Limiting ✅ · Automated Anonymizer ✅ ·
> HL7/FHIR Gateway ✅ · Windows EXE packaging scripts (PyInstaller + NSSM) ✅
| ⭐ | **Edge PACS** | عمل Offline-First في مراكز ذات اتصال ضعيف |
| ⭐ | **Blockchain Audit** | سجل تدقيق لا يمكن التلاعب به على Immutable Ledger |

---

## 📅 الجدول الزمني المقترح

```
الأسبوع 1-2  │▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░│ Phase 1: Foundation
الأسبوع 3-4  │░░░░░░░░░░▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░│ Phase 2: DICOM Engine
الأسبوع 5-6  │░░░░░░░░░░░░░░░░░░░░▓▓▓▓▓▓▓▓░░│ Phase 3: DICOMweb + OHIF
الأسبوع 7-8  │░░░░░░░░░░░░░░░░░░░░░░░░░░▓▓▓▓│ Phase 4: Production
الأسبوع 9-10 │ (Windows EXE Distribution)    │ Phase 5: Distribution
```
