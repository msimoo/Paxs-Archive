# PAXS Archive - دليل الموجهات الشامل والخطة التنفيذية الكاملة (Master Prompts & Execution Plan)

## مقدمة
هذا الملف يحتوي على الخطة التنفيذية الكاملة ودليل الموجهات (Prompts) البرمجية والهندسية لمشروع **PAXS Archive & paxs viewer**، مصممة خصيصاً للعمل مع نماذج الذكاء الاصطناعي (Gemini 2.6 Flash و DeepSeek V4).

---

## 1. الموجهات المعمارية وتنظيم المشروع (Architecture & Setup Prompts)

### Prompt 1.1: ترتيب وهيكلة الكود (Clean Architecture)
```text
Role: Senior Python Software Architect specializing in Medical Imaging (PACS/DICOM Systems).

Context:
I am re-structuring my medical image archiving system named "paxs archive".
Core Tech Stack:
- Backend: Python (pydicom, pynetdicom, FastAPI/Tornado)
- Database: MySQL
- Frontend/Viewer: Custom modified OHIF viewer ("paxs viewer") integrating via DICOMweb (WADO-RS, QIDO-RS).

Task:
Refactor and reorganize the project directory structure to follow Domain-Driven Design (DDD) and Clean Architecture principles.

Requirements:
1. Provide a comprehensive directory tree layout showing clear separation between DICOM SCP/SCU services, REST API endpoints (DICOMweb), MySQL ORM/Database access layers, File Storage Management, and the Viewer build output.
2. Draft a `config.py` module design that cleanly manages environment variables (DICOM AE Title, Port, MySQL credentials, Storage Paths, WADO URIs).
3. Provide Python code for a clean Dependency Injection pattern separating the C-STORE DIMSE listener from the MySQL metadata indexer.

Ensure the code adheres to PEP 8 standards, handles DICOM Transfer Syntaxes safely, and avoids tight coupling.
```

### Prompt 1.2: توثيق النظام ووثيقة المطابقة (DICOM Conformance Statement)
```text
Role: Technical Writer and DICOM Integration Specialist.

Context:
Project: "paxs archive" (Python + MySQL + paxs viewer).
Need to build formal technical documentation and an industry-standard DICOM Conformance Statement.

Task:
1. Generate a structured template for a formal DICOM Conformance Statement tailored for "paxs archive" acting as a Storage SCP, Query/Retrieve SCP, and DICOMweb Provider.
2. Write a comprehensive `README.md` containing:
   - System Overview & Architecture diagram (Mermaid.js format).
   - MySQL Database Schema Overview (Tables: Patients, Studies, Series, Instances).
   - DICOM Network Configuration (AE Title, Port 104/11112, Supported Transfer Syntaxes).
   - Step-by-step setup guide for developers.
3. Write API documentation for the custom DICOMweb integration endpoints used by `paxs viewer` (WADO-RS retrieve study/metadata, QIDO-RS search studies).

Output must be in crisp Markdown format ready for production deployment repository.
```

---

## 2. موجهات الأداء والتوسع (Scalability & Non-Blocking Architecture)

### Prompt 2.1: إعداد طابور المهام غير المتزامنة (Celery + Redis Async Architecture)
```text
Role: High-Performance Distributed Systems Architect.

Context:
System: "paxs archive" (FastAPI + pynetdicom + MySQL).
Goal: Decouple DICOM C-STORE network ingestion from heavy AI and indexing operations to achieve zero-latency response.

Task:
Write complete, production-ready Python code implementing an Event-Driven Microservices Architecture:
1. Configure FastAPI with asynchronous background worker queues using Celery and Redis.
2. Create an ingress route `/api/dicom/cstore-ingest` that:
   - Saves incoming DICOM payloads to NVMe disk instantly (<20ms).
   - Writes minimal metadata to MySQL.
   - Emits a non-blocking `STUDY_STORED` event.
3. Define Celery worker tasks for background execution:
   - Vector Embedding generation for pathology search.
   - Post-Quantum Merkle-DAG cryptographic hashing.
   - Self-Healing DICOM Metadata validation.
```

### Prompt 2.2: تحسين قاعدة البيانات وسجلات التخزين (MySQL Partitioning & Storage Tiering)
```text
Role: Lead Database Administrator & Storage Engineer.

Task:
Provide optimized MySQL Schema and Lifecycle Management scripts for "paxs archive":
1. Write DDL for `studies`, `series`, and `instances` tables with Composite Indexing on (`PatientID`, `StudyInstanceUID`, `AccessionNumber`) and Range Partitioning by `StudyDate`.
2. Write a Python SQLAlchemy Connection Pool setup optimized for high-concurrency DICOM writes.
3. Write an automated Storage Lifecycle Script (`storage_tiering.py`) that migrates studies older than 30 days from high-speed NVMe to compressed Cold Storage (S3/NAS) while preserving WADO-RS retrieval pointers.
```

---

## 3. موجهات المزايا التقنية العميقة (Deep Technical Innovation Prompts)

### Prompt 3.1: محرك العرض 3D العميل برسم كارت الشاشة (WebGPU Zero-Server-Load Engine)
```text
Role: Advanced Graphics & WebGPU Engineer for Medical Imaging.

Context:
Project: "paxs archive" & "paxs viewer".

Task:
Provide technical architecture and JavaScript/WebGPU pipeline code for `paxs viewer` that:
1. Receives raw 3D DICOM pixel arrays from FastAPI/WADO-RS.
2. Utilizes WebGPU Compute Shaders to perform real-time Window/Leveling, MPR slicing across Axial, Sagittal, and Coronal planes.
3. Generates a Cinematic Volume Rendering pipeline with transfer function controls (Opacity/Color mapping for Bone, Soft Tissue, Vascular).
4. Delivers 120 FPS performance while removing dynamic rendering load from the Python backend server.
```

### Prompt 3.2: الضغط العصبي للشرائح (Implicit Neural Representation - SIREN Compression)
```text
Role: Deep Learning Architect (Python + PyTorch / JAX).

Task:
Write a Python module (`inr_compression.py`) that:
1. Ingests a 3D DICOM Pixel Array (`pydicom`), normalizes Hounsfield Units (HU), and trains a lightweight SIREN MLP network mapping (x, y, z) coordinates to density values.
2. Exports network weights into a compressed binary payload (<5MB) indexed in MySQL.
3. Provides an inference script capable of decoding arbitrary 2D slice planes (Axial, Coronal, Sagittal, Oblique) at dynamic sub-millimeter resolutions directly from weight parameters.
```

### Prompt 3.3: التوزيع الداخلي ند-للند (Intra-Hospital Local P2P Mesh Caching)
```text
Role: Distributed Systems & Network Protocol Architect.

Task:
Design an in-browser WebRTC Peer-to-Peer DICOM Slice Swarm engine for `paxs viewer`:
1. Create a Python signaling server (FastAPI + WebSockets) tracking active viewer sessions in the hospital local subnet.
2. Write JS client logic where `paxs viewer` checks if neighboring clients hold DICOM frame buffers in IndexedDB/Memory.
3. Fetch missing DICOM slices directly from local peer browsers using WebRTC Data Channels before falling back to downloading from the main Python PACS server.
```

### Prompt 3.4: التوقيع الرقمي ومنع التزوير الجنائي (Cryptographic Pixel Forensics)
```text
Role: Cybersecurity & Medical Data Integrity Engineer.

Task:
Create a Python Cryptographic Verification Module (`dicom_forensics.py`) for "paxs archive":
1. Calculate a SHA-256 HMAC signature of raw pixel arrays (`dataset.pixel_array.tobytes()`) and metadata fields upon C-STORE ingress.
2. Store the cryptographic hash and timestamp in a tamper-resistant MySQL table `image_signatures`.
3. Provide a Python validation function triggered prior to DICOMweb/WADO delivery that re-computes the hash and flags any file integrity violations or metadata tampering.
```

### Prompt 3.5: البحث المتجهي التشريحي (Vector Latent Pathology Search)
```text
Role: Machine Learning Engineer specializing in Medical Vector Search.

Task:
Build a Semantic Vector Search Engine for DICOM volumes (`vector_search.py`):
1. Write a Python worker service that extracts 3D ROI feature vectors using BiomedCLIP/MedSAM upon C-STORE completion.
2. Store feature vectors in MySQL under `study_embeddings` with an HNSW spatial index.
3. Build a REST endpoint `/api/search/similar-pathology` taking a 3D ROI bounding box array from `paxs viewer` and returning top-K similar historical studies ranked by Cosine Similarity.
```

---

## 4. موجهات المزايا السريرية الـ 15 (15 Clinical Feature Prompts)

```text
Role: Senior Medical Imaging & AI Algorithm Engineer.

Task:
Write modular Python scripts (`clinical_modules/`) for "paxs archive" implementing the 15 master clinical features:

1. RECIST 1.1 / iRECIST: Automated target lesion measurement and response classification (CR, PR, SD, PD).
2. Opportunistic BMD & FRAX: Automated L1-L4 HU measurement on abdominal CTs for osteoporosis screening and FRAX fracture risk.
3. PE Clot Burden & RV Strain: Qanadli obstruction index calculation and RV/LV diameter ratio measurement on CTPA.
4. Automated ASPECTS & Penumbra: 10-point ASPECTS scoring and core vs. penumbra volumetric mapping on stroke CTs.
5. Agatston Calcium Score: Coronary artery calcium scoring and soft vs. calcified plaque risk stratification.
6. MRI-PDFF Liver Fat & Fibrosis: Proton density fat fraction calculation and T1/T2* mapping for non-invasive fibrosis staging (F0-F4).
7. Lung-RADS & Volumetric Doubling Time: 3D nodule volume tracking, Volume Doubling Time (VDT) calculation, and Lung-RADS classification.
8. Surgical Margin Clearance: 3D spatial safety margin clearance calculation (5mm/10mm) around tumors relative to major vessels.
9. BBB Permeability (Ktrans): Dynamic contrast MRI permeability mapping to distinguish radiation necrosis from true tumor recurrence.
10. MS Lesion Load & Brain Volumetry: FLAIR white matter hyperintensity segmentation, lesion volume tracking, and brain atrophy rate.
11. Volumetric Breast Density: Fibroglandular tissue volume percentage calculation and BI-RADS A-D density classification.
12. 3D Joint Space & Cartilage Mapping: 3D joint space width measurement and cartilage thickness heatmap generation for osteoarthritis.
13. Fetal MRI Biometry & Placental Perfusion: Automated fetal organ biometric measurements and placental perfusion analysis.
14. Radiomics Biomarker Extraction: Extraction of 100+ radiomic features (texture, wavelet) predicting genomic mutations (EGFR, IDH1).
15. Cumulative Isodose & OAR Safety: Isodose contour mapping (Gy) and cumulative dose tracking for organs-at-risk safety alerts.
```

---

## 5. موجهات التجهيز والتوزيع والتطبيق المباشر (Packaging & Deployment)

### Prompt 5.1: أتمتة الإصدار النظيف (Build Release Automation)
```text
Role: DevOps & Release Engineer.

Task:
Write a Python Release Automation Script (`build_release.py`) that prepares "paxs archive" for distribution in a clean directory structure.

The script must:
1. Copy only source code files, excluding development artifacts (`.git`, `__pycache__`, `.venv`, `.env`, temporary DICOM test files).
2. Validate that all required dependencies are explicitly pinned in a production `requirements.txt`.
3. Create a standardized folder hierarchy (`bin/`, `config/`, `web/`, `logs/`, `docs/`).
4. Verify code integrity by running a syntax sanity check across all `.py` files before finalizing the package.
```

### Prompt 5.2: التحويل إلى تطبيقي ويندوز (PyInstaller & Inno Setup)
```text
Role: Windows Packaging Specialist.

Task:
Provide complete configuration scripts to convert "paxs archive" into a standalone Windows `.exe` with an Inno Setup installer:

1. PyInstaller `.spec` File (`paxs_archive.spec`):
   - Include hidden imports for `pynetdicom`, `pydicom`, `numpy`, `mysql.connector`.
   - Bundle static assets for `paxs viewer` (`web/`).
   - Package as a multi-file directory (`onedir`).

2. Inno Setup Script (`installer_setup.iss`):
   - Configure installation path (`C:\Program Files\PAXS Archive`).
   - Register the executable as a Windows Service using NSSM so DICOM C-STORE SCP runs automatically on system boot.
   - Configure Windows Firewall rules for DICOM Port (104/11112) and Web Port (8080/443).
   - Create Desktop and Start Menu shortcuts.
```

---

## 6. الخطة التنفيذية الكامله وخارطة الطريق (Full Execution Roadmap)

```
[المرحلة 1: المعمارية والنواة]
  ├── إعداد المجلد المضيف والهيكلية النظيفة (FastAPI + pynetdicom + MySQL)
  ├── ضبط الموصلات غير المتزامن (Celery + Redis Event Queue)
  └── كتابة محولات DICOMweb (QIDO-RS, WADO-RS, STOW-RS)

[المرحلة 2: تطوير واجهة paxs viewer]
  ├── إدماج حزم Cornerstone3D 5.0 + HTJ2K Streaming
  ├── بناء محرك WebGPU 3D / MPR Renderer
  └── دمج نماذج MedSAM 3D Auto-Segmentation داخل المتصفح

[المرحلة 3: المزايا التقنية والسريرية]
  ├── تنفيذ طابور المعالجة غير المتزامن للـ 15 ميزة سريرية
  ├── تفعيل محرك البحث المتجهي التشريحي (Vector Search)
  └── دمج التوقيع الرقمي وحماية البيانات (Merkle-DAG & Forensics)

[المرحلة 4: التعبئة والتصقيل]
  ├── صياغة وثيقة DICOM Conformance Statement الرسمية
  ├── بناء ملفات الحزم (build_release.py, PyInstaller .spec, Inno Setup)
  └── إجراء اختبارات الضغط والمطابقة الشاملة (IHE Plugfest Readiness)
```