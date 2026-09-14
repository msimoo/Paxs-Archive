# نموذج البيانات الهيكلي — PAXS Server

> يعكس هذا النموذج الهيكل الرسمي لـ DICOM Information Object Definition (IOD) المكون من 4 مستويات هرمية.
>
> 📋 **مصفوفة حالة الـ 52 ميزة (41 ✅ · 1 🟡 · 10 ⬜):** راجع `feature-plans/plan.md` و `feature-plans/MASTER-ROADMAP.md`.

---

## 📊 مخطط العلاقات (Entity Relationship)

```
┌──────────────────────────────────────────────────────────────────────┐
│                         DICOM Hierarchy                              │
│                                                                      │
│  patients (1) ──< studies (N) ──< series (N) ──< instances (N)      │
│                                                                      │
│  مريض واحد → فحوصات متعددة → سلاسل متعددة → صور متعددة             │
└──────────────────────────────────────────────────────────────────────┘

patients
  │ id (PK)
  │ patient_id (DICOM Tag 0010,0020) ← unique
  │ patient_name
  │ patient_birth_date
  └── patient_sex
       │
       ▼ (1..N)
  studies
  │ id (PK)
  │ patient_id (FK → patients.id)
  │ study_instance_uid ← unique (DICOM Tag 0020,000D)
  │ study_date / study_time
  │ accession_number
  └── study_description
       │
       ▼ (1..N)
  series
  │ id (PK)
  │ study_id (FK → studies.id)
  │ series_instance_uid ← unique (DICOM Tag 0020,000E)
  │ modality (CT / MRI / CR / US / DX)
  │ series_number
  └── series_description
       │
       ▼ (1..N)
  instances
    id (PK)
    series_id (FK → series.id)
    sop_instance_uid ← unique (DICOM Tag 0008,0018)
    sop_class_uid
    instance_number
    file_path
    file_size_bytes
    transfer_syntax_uid   ← نوع الضغط
    compressed_at         ← وقت الضغط

  ── جداول إضافية ──
  patient_shares          ← روابط مشاركة المريض
  compression_jobs        ← مهام الضغط في الخلفية
  audit_log               ← سجل التدقيق (بصمة SHA-256)
```

---

## 💾 DDL Schema — MySQL 8.0

```sql
-- ============================================================
-- 1. جدول المرضى
-- ============================================================
CREATE TABLE patients (
    id                 BIGINT       AUTO_INCREMENT PRIMARY KEY,
    patient_id         VARCHAR(64)  NOT NULL,
    patient_name       VARCHAR(192) NULL,
    patient_birth_date DATE         NULL,
    patient_sex        VARCHAR(16)  NULL,        -- M / F / O
    created_at         TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX uq_patient_id   (patient_id),
    INDEX        idx_patient_name (patient_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 2. جدول الفحوصات
-- ============================================================
CREATE TABLE studies (
    id                    BIGINT       AUTO_INCREMENT PRIMARY KEY,
    patient_id            BIGINT       NOT NULL,
    study_instance_uid    VARCHAR(128) NOT NULL,
    study_date            DATE         NULL,
    study_time            TIME         NULL,
    accession_number      VARCHAR(64)  NULL,
    study_description     VARCHAR(255) NULL,
    referring_physician   VARCHAR(192) NULL,
    num_series            INT          DEFAULT 0,
    num_instances         INT          DEFAULT 0,
    created_at            TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX uq_study_uid      (study_instance_uid),
    INDEX        idx_accession     (accession_number),
    INDEX        idx_study_date    (study_date),
    CONSTRAINT fk_study_patient
        FOREIGN KEY (patient_id) REFERENCES patients(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 3. جدول السلاسل (Series)
-- ============================================================
CREATE TABLE series (
    id                   BIGINT       AUTO_INCREMENT PRIMARY KEY,
    study_id             BIGINT       NOT NULL,
    series_instance_uid  VARCHAR(128) NOT NULL,
    modality             VARCHAR(16)  NOT NULL,   -- CT, MRI, CR, DX, US, NM...
    series_number        INT          NULL,
    series_description   VARCHAR(255) NULL,
    body_part_examined   VARCHAR(64)  NULL,
    num_instances        INT          DEFAULT 0,
    created_at           TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    UNIQUE INDEX uq_series_uid  (series_instance_uid),
    INDEX        idx_modality   (modality),
    CONSTRAINT fk_series_study
        FOREIGN KEY (study_id) REFERENCES studies(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 4. جدول الصور المفردة (Instances / SOPs)
-- ============================================================
CREATE TABLE instances (
    id                    BIGINT        AUTO_INCREMENT PRIMARY KEY,
    series_id             BIGINT        NOT NULL,
    sop_instance_uid      VARCHAR(128)  NOT NULL,
    sop_class_uid         VARCHAR(128)  NOT NULL,
    instance_number       INT           NULL,
    file_path             VARCHAR(512)  NOT NULL,
    file_size_bytes       BIGINT        NOT NULL,
    transfer_syntax_uid   VARCHAR(128)  DEFAULT '1.2.840.10008.1.2.1',  -- Explicit Little Endian
    compressed_at         TIMESTAMP     NULL,
    created_at            TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,

    UNIQUE INDEX uq_sop_uid       (sop_instance_uid),
    INDEX        idx_file_path    (file_path(255)),
    CONSTRAINT fk_instance_series
        FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 5. جدول روابط مشاركة المريض (Patient Share Links)
-- ============================================================
CREATE TABLE patient_shares (
    id                  BIGINT       AUTO_INCREMENT PRIMARY KEY,
    study_instance_uid  VARCHAR(128) NOT NULL,
    share_token         VARCHAR(64)  NOT NULL,
    otp_phone           VARCHAR(20)  NULL,       -- رقم هاتف التحقق
    expires_at          DATETIME     NOT NULL,
    accessed_count      INT          DEFAULT 0,
    created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    UNIQUE INDEX uq_share_token (share_token),
    INDEX        idx_expires_at (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 6. جدول سجل التدقيق (Audit Log — Tamper Detection)
-- ============================================================
CREATE TABLE audit_log (
    id                 BIGINT        AUTO_INCREMENT PRIMARY KEY,
    sop_instance_uid   VARCHAR(128)  NOT NULL,
    sha256_hash        CHAR(64)      NOT NULL,    -- بصمة SHA-256 للملف
    action             VARCHAR(32)   NOT NULL,    -- STORED / RETRIEVED / COMPRESSED / DELETED
    performed_by       VARCHAR(128)  NULL,        -- مستخدم أو نظام
    performed_at       TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_sop_audit (sop_instance_uid),
    INDEX idx_action    (action)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 7. جدول مهام الضغط (Compression Queue)
-- ============================================================
CREATE TABLE compression_jobs (
    id              BIGINT       AUTO_INCREMENT PRIMARY KEY,
    instance_id     BIGINT       NOT NULL,
    method          VARCHAR(16)  DEFAULT 'JPEG-LS',   -- JPEG-LS / JPEG2000 / HTJ2K
    status          VARCHAR(16)  DEFAULT 'PENDING',   -- PENDING / RUNNING / DONE / FAILED
    original_size   BIGINT       NULL,
    compressed_size BIGINT       NULL,
    ratio_pct       DECIMAL(5,2) NULL,
    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    finished_at     TIMESTAMP    NULL,

    INDEX idx_job_status (status),
    CONSTRAINT fk_job_instance
        FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

---

## 📐 SQLAlchemy ORM Models (Python)

```python
# app/db/models.py
from sqlalchemy import BigInteger, String, Date, Time, ForeignKey, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from datetime import datetime, date, time

class Base(DeclarativeBase):
    pass

class Patient(Base):
    __tablename__ = "patients"
    id                 : Mapped[int]          = mapped_column(BigInteger, primary_key=True)
    patient_id         : Mapped[str]          = mapped_column(String(64), unique=True, nullable=False)
    patient_name       : Mapped[str | None]   = mapped_column(String(192))
    patient_birth_date : Mapped[date | None]  = mapped_column(Date)
    patient_sex        : Mapped[str | None]   = mapped_column(String(16))
    created_at         : Mapped[datetime]     = mapped_column(default=datetime.utcnow)
    studies            : Mapped[list["Study"]] = relationship(back_populates="patient", cascade="all, delete")

class Study(Base):
    __tablename__ = "studies"
    id                  : Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    patient_id          : Mapped[int]           = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    study_instance_uid  : Mapped[str]           = mapped_column(String(128), unique=True)
    study_date          : Mapped[date | None]   = mapped_column(Date)
    study_description   : Mapped[str | None]    = mapped_column(String(255))
    accession_number    : Mapped[str | None]    = mapped_column(String(64))
    patient             : Mapped["Patient"]     = relationship(back_populates="studies")
    series_list         : Mapped[list["Series"]] = relationship(back_populates="study", cascade="all, delete")

class Series(Base):
    __tablename__ = "series"
    id                  : Mapped[int]              = mapped_column(BigInteger, primary_key=True)
    study_id            : Mapped[int]              = mapped_column(ForeignKey("studies.id", ondelete="CASCADE"))
    series_instance_uid : Mapped[str]              = mapped_column(String(128), unique=True)
    modality            : Mapped[str]              = mapped_column(String(16))
    series_description  : Mapped[str | None]       = mapped_column(String(255))
    study               : Mapped["Study"]          = relationship(back_populates="series_list")
    instances           : Mapped[list["Instance"]] = relationship(back_populates="series", cascade="all, delete")

class Instance(Base):
    __tablename__ = "instances"
    id               : Mapped[int]        = mapped_column(BigInteger, primary_key=True)
    series_id        : Mapped[int]        = mapped_column(ForeignKey("series.id", ondelete="CASCADE"))
    sop_instance_uid : Mapped[str]        = mapped_column(String(128), unique=True)
    sop_class_uid    : Mapped[str]        = mapped_column(String(128))
    file_path        : Mapped[str]        = mapped_column(String(512))
    file_size_bytes  : Mapped[int]        = mapped_column(BigInteger)
    series           : Mapped["Series"]   = relationship(back_populates="instances")
```

---

## 🗜️ معايير الضغط المدعومة

| الخوارزمية | Transfer Syntax UID | نسبة الضغط | سرعة فك الضغط | الاستخدام الأمثل |
|-----------|---------------------|------------|----------------|-----------------|
| **JPEG-LS Lossless** | 1.2.840.10008.1.2.4.80 | حتى 3.5:1 | فائقة | CT / MRI |
| **JPEG 2000 Lossless** | 1.2.840.10008.1.2.4.90 | حتى 3:1 | متوسطة | CR / DX |
| **HTJ2K Lossless** | 1.2.840.10008.1.2.4.201 | حتى 3:1 | أسرع × 10 | OHIF Viewer Web |
| **RLE Lossless** | 1.2.840.10008.1.2.5 | 1.5:1 | سريعة | Ultrasound |

---

## 📂 هيكل تخزين الملفات على القرص

```
storage/
└── {StudyInstanceUID}/
    └── {SeriesInstanceUID}/
        ├── {SOPInstanceUID_1}.dcm
        ├── {SOPInstanceUID_2}.dcm
        └── {SOPInstanceUID_N}.dcm

مثال:
storage/
└── 1.2.840.113619.2.408.5282380.7369954/
    └── 1.2.840.113619.2.408.5282380.7369955/
        ├── 1.2.840.113619.2.408.5282380.7369956.dcm   (2.4 MB → 0.8 MB بعد JPEG-LS)
        ├── 1.2.840.113619.2.408.5282380.7369957.dcm
        └── ...
```
