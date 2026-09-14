# نقاط التحقق ومعايير القبول — PAXS Server Checkpoints

> كل Checkpoint يمثل بوابة إجبارية لا يمكن تجاوزها قبل الانتقال للمرحلة التالية.

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

## 📋 جدول نقاط التحقق الإجمالي

| النقطة | الاسم | المرحلة المرتبطة | الحالة |
|--------|-------|-----------------|--------|
| CP-01 | DB & Pipeline | Phase 1 | 🔲 لم يبدأ |
| CP-02 | C-STORE Network Test | Phase 2 | 🔲 لم يبدأ |
| CP-03 | QIDO-RS JSON Response | Phase 3 | 🔲 لم يبدأ |
| CP-04 | OHIF Full Integration | Phase 3 | ✅ مكتمل (OHIF v3.12.11 at /ohif — DICOMweb same-origin) |
| CP-05 | High Load Stress Test | Phase 4 | 🔲 لم يبدأ |
| CP-06 | Security Hardening | Phase 4 | 🟡 جزئياً (JWT/RBAC/rate-limit تم) |
| CP-07 | Windows EXE Validation | Phase 5 | 🔲 لم يبدأ |

---

## ✅ CP-01 — Database & Storage Pipeline

**المرحلة:** Phase 1 | **الأسبوع:** 2

### هدف الاختبار
تأكيد أن ملف DICOM تجريبي يُعالَج بالكامل من القراءة حتى الحفظ في DB والقرص.

### خطوات التنفيذ
```python
# test_cp01.py
import pydicom
import asyncio
from app.dicom.parser import parse_dicom_file
from app.services.storage_service import save_dicom_file
from app.db.session import get_db

async def test_cp01():
    # 1. تحميل ملف DICOM تجريبي
    from pydicom.data import get_testfiles_name
    ds = pydicom.dcmread(get_testfiles_name("CT_small.dcm"))

    # 2. حفظ على القرص
    file_path = save_dicom_file(ds)
    assert os.path.exists(file_path), "❌ الملف لم يُحفظ على القرص"
    print(f"✅ الملف حُفظ في: {file_path}")

    # 3. استخراج الميتاداتا
    meta = parse_dicom_file(ds)
    assert meta["StudyInstanceUID"] is not None, "❌ فشل استخراج StudyUID"
    print(f"✅ StudyUID: {meta['StudyInstanceUID']}")

    # 4. التحقق من قاعدة البيانات
    async with get_db() as db:
        from sqlalchemy import text
        result = await db.execute(text("SELECT COUNT(*) FROM instances"))
        count = result.scalar()
        assert count > 0, "❌ لا يوجد سجلات في جدول instances"
        print(f"✅ عدد الصور في DB: {count}")

asyncio.run(test_cp01())
```

### معايير النجاح
- [x] الملف محفوظ في `storage/{StudyUID}/{SeriesUID}/{SOPUID}.dcm`
- [x] سجل `patients` يحتوي على المريض
- [x] سجل `studies` يحتوي على الفحص
- [x] سجل `instances` يحتوي على الصورة مع `file_path` و`file_size_bytes`
- [x] لا يوجد أي exception أو traceback

**📊 حالة النجاح:** جميع Assertions تمر دون أخطاء

---

## ✅ CP-02 — C-STORE Network Test

**المرحلة:** Phase 2 | **الأسبوع:** 4

### هدف الاختبار
إرسال فحص حقيقي من أداة خارجية إلى خادم PAXS والتأكد من الاستقبال الكامل.

### خطوات التنفيذ

```bash
# 1. تشغيل خادم PAXS في Terminal أول
python -m app.dicom.receiver

# 2. إرسال ملف DICOM من DCMTK في Terminal ثانٍ
storescu -v --aetitle TEST_SCU \
         --call PAXS_SCP \
         127.0.0.1 10400 \
         test_files/CT_small.dcm

# 3. مراقبة السجلات
# يجب أن تظهر:
# 📡 Received C-STORE request for: 1.2.840.xxx
# ✅ Stored to: storage/1.2.840.xxx/.../xxx.dcm
# ✅ DB write complete
# 📦 Compression job queued
```

### معايير النجاح
- [x] `storescu` يُرجع Status: `0x0000` (Success)
- [x] لا يوجد `Network timeout` أو `Connection refused`
- [x] الملف موجود على القرص
- [x] سجل موجود في `instances` في MySQL
- [x] مهمة Celery في Redis queue (يمكن فحصها عبر `celery inspect active`)

**📊 الأداء المتوقع:** معالجة كل صورة في أقل من 500ms

---

## ✅ CP-03 — QIDO-RS JSON Response

**المرحلة:** Phase 3 | **الأسبوع:** 5

### هدف الاختبار
التأكد أن مسار البحث يرجع JSON مطابقاً لمواصفة DICOM PS3.18.

### خطوات التنفيذ

```bash
# طلب بحث QIDO-RS
curl -s -H "Accept: application/dicom+json" \
     "http://localhost:8000/dicom-web/studies" | python -m json.tool

# يجب أن يرجع مصفوفة JSON مثل:
# [
#   {
#     "00100020": { "vr": "LO", "Value": ["PATIENT_001"] },
#     "00200010": { "vr": "SH", "Value": ["1"] },
#     "0020000D": { "vr": "UI", "Value": ["1.2.840.113619..."] }
#   }
# ]
```

### معايير النجاح
- [x] Status HTTP: `200 OK`
- [x] `Content-Type: application/dicom+json`
- [x] كل حقل له شكل `{"XXXXXXXX": {"vr": "XX", "Value": [...]}}` 
- [x] الحقول الإلزامية موجودة: `0020000D` (StudyUID), `00100020` (PatientID)
- [x] البحث يعمل بـ `?PatientName=...` و`?StudyDate=...`

**📊 الأداء المتوقع:** وقت الاستجابة < 200ms لـ 1000 فحص في DB

---

## ✅ CP-04 — OHIF Full Integration

**المرحلة:** Phase 3 | **الأسبوع:** 6

### هدف الاختبار
فتح عارض OHIF Viewer v3 في المتصفح واستعراض فحص كامل بسلاسة.

### خطوات التنفيذ

```
1. فتح المتصفح على: http://localhost
2. يجب أن تظهر قائمة الفحوصات من QIDO-RS
3. النقر على فحص CT
4. يجب أن تُحمّل الصور من WADO-RS وتُعرض في العارض
5. تجربة:
   - Zoom / Pan
   - Window/Level (WW/WL)
   - التنقل بين الشرائح (Slice scroll)
   - القياسات (Ruler / Angle)
```

### معايير النجاح
- [x] لا يوجد CORS errors في console المتصفح
- [x] الصور تُحمَّل بدون `404 Not Found` أو `500 Internal Server Error`
- [x] فحص CT من 200 صورة يُعرض بالكامل في < 10 ثوانٍ (اتصال محلي)
- [x] Windowing يعمل بشكل صحيح (الصورة بـ 16-bit grayscale)
- [x] تحكم Zoom / Pan سلس بدون تقطيع

**📊 الأداء المتوقع:** تحميل الصورة الأولى < 1 ثانية

---

## ✅ CP-05 — High Load Stress Test

**المرحلة:** Phase 4 | **الأسبوع:** 7

### هدف الاختبار
إرسال 500 صورة متزامنة والتأكد من عدم فقدان أي منها.

### خطوات التنفيذ

```python
# test_stress.py
import threading
import subprocess
import time

def send_file(file_path: str, thread_id: int):
    result = subprocess.run([
        "storescu", "-v", "127.0.0.1", "10400", file_path
    ], capture_output=True)
    if b"0x0000" in result.stdout:
        print(f"✅ Thread {thread_id}: Success")
    else:
        print(f"❌ Thread {thread_id}: FAILED — {result.stderr}")

# إرسال 500 صورة بـ 50 thread متوازي
threads = []
for i in range(500):
    t = threading.Thread(
        target=send_file,
        args=(f"test_files/slice_{i % 20}.dcm", i)
    )
    threads.append(t)

start = time.time()
[t.start() for t in threads]
[t.join() for t in threads]
elapsed = time.time() - start

print(f"\n⏱️ إجمالي الوقت: {elapsed:.1f}s")
print(f"📊 متوسط: {elapsed/500*1000:.0f}ms / صورة")
```

### معايير النجاح
- [x] 500/500 صورة تُستقبل بدون فقدان (0% loss)
- [x] لا يوجد خطأ `ConnectionRefused` أو `Timeout`
- [x] الخادم لا يتعطل ويبقى يعمل بعد الاختبار
- [x] كل 500 صورة موجودة في جدول `instances` في DB
- [x] مهام Celery للضغط تُنفَّذ بنجاح

**📊 الأداء المتوقع:** استقبال 500 صورة في < 60 ثانية (8+ صور/ثانية)

---

## ✅ CP-06 — Security Hardening

**المرحلة:** Phase 4 | **الأسبوع:** 8

### معايير النجاح

```bash
# 1. اختبار JWT Authentication
curl -s -X POST "http://localhost:8000/auth/login" \
     -H "Content-Type: application/json" \
     -d '{"username":"admin","password":"wrong"}'
# يجب أن يرجع: 401 Unauthorized

# 2. اختبار الوصول بدون Token
curl -s "http://localhost:8000/dicom-web/studies"
# يجب أن يرجع: 401 Unauthorized

# 3. اختبار HTTPS
curl -k -s "https://localhost/dicom-web/studies"
# يجب أن يعمل بدون أخطاء SSL

# 4. اختبار SHA-256 Tamper Detection
# تعديل ملف .dcm يدوياً → النظام يرصد التلاعب عند الاسترجاع
```

- [x] مسارات API تطلب JWT token
- [x] HTTPS يعمل مع شهادة SSL
- [x] Rate Limiting يمنع الطلبات المتكررة (> 100/min)
- [x] SHA-256 tamper detection يعمل

---

## ✅ CP-07 — Windows EXE Validation

**المرحلة:** Phase 5 | **الأسبوع:** 10

### معايير النجاح

```
اختبار على جهاز Windows نظيف (بدون Python مثبت):

1. نسخ مجلد dist/PAXS-Server/ إلى الجهاز الجديد
2. تشغيل PAXS-Server.exe
3. يجب أن تظهر:
   🚀 Starting PAXS Server Engine from: C:\dist\PAXS-Server
   📡 DICOM C-STORE Receiver is running on port 10400...
   INFO: Started server process
   INFO: Uvicorn running on http://127.0.0.1:8000

4. فتح المتصفح على http://127.0.0.1:8000/docs
   يجب أن تظهر واجهة Swagger UI

5. إرسال ملف DICOM من storescu
   يجب أن يُستقبل وتُفهرس في MySQL
```

- [x] EXE يعمل على Windows 10/11 بدون Python
- [x] المنفذان 10400 و8000 يعملان
- [x] OHIF Viewer يعمل من المتصفح
- [x] NSSM يشغل EXE عند إقلاع الويندوز تلقائياً
- [x] السجلات (logs) تظهر في نافذة الـ Console

---

## 🏁 قائمة التحقق النهائية قبل الإنتاج

```
الأداء والاستقرار:
[ ] اختبار تشغيل مستمر لـ 24 ساعة بدون crashes
[ ] مراقبة استهلاك RAM والـ CPU أثناء الحمل العالي
[ ] اختبار استرداد الخادم بعد إعادة التشغيل

قاعدة البيانات:
[ ] النسخ الاحتياطي التلقائي مفعّل (mysqldump cron)
[ ] Connection Pooling مضبوط لمنع تسريب الاتصالات
[ ] الفهارس (Indexes) تعمل وتُسرّع الاستعلامات

الأمان:
[ ] كل كلمات المرور في .env ولا توجد في الكود
[ ] firewall يسمح فقط بالمنافذ: 10400, 443, 80
[ ] DICOM TLS مفعّل للاتصالات مع الأجهزة الطبية

التوثيق:
[ ] دليل التثبيت مكتمل للمستشفيات
[ ] دليل استكشاف الأخطاء وإصلاحها
[ ] API documentation (Swagger) محدّثة
```
