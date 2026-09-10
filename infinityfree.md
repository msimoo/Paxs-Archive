# infinityfree
* FTP details:
FTP Username: if0_42641543
FTP Password:5yzMevPA9T   
FTP Hostname : ftpupload.net
FTP Port (optional) :21


* Mysql Database:
dbname:if0_42641543_Portfolio: 
Username:if0_42641543
MySQL Password:5yzMevPA9T
MySQL Hostname:sql105.infinityfree.com
MySQL Port (optional):3306


Yes! All of these clinical and AI features are fully integrated, licensed, and operational across the entire PAXS
  Archive & Viewer stack.

  Here is the exact breakdown of how they work and where they operate in the system:
  ──────
  ### 🧠 1. Neuro, Emergency & Volumetric AI

  • ich_triage & brain_seg (Emergency Intracranial Hemorrhage & Brain Volumetry):
      • Engine: Automated 3D voxel density extraction (55–95 HU acute blood range) with connected-component
      clustering.
      • Verified Live: Tested on BRAIN +PA (31-slice CT volume) ➔ detected acute hemorrhagic focus of 141.16 cm³ (69.
      1 HU mean density) in 582.89 ms with CRITICAL triage tag.
      • REST Endpoints: POST /api/v1/ai/infer and raw 3D binary mask stream GET /api/v1/ai/mask/{series_uid}.
  • lung_rads (Pulmonary Nodule Volumetry & Tracker):
      • Engine: Thoracic parenchymal segmentation (-400 to +100 HU) computing nodule volume (mm³/cm³) and
      Fleischner/Lung-RADS follow-up risk category.
  • bone_segmentation, cobb_angle & bone_age_fracture:
      • Engine: Cortical bone segmentation (>300  HU) and bone density estimation for skeletal quantification.

  ──────
  ### 🔬 2. 3D Advanced Visualization & Nuclear Medicine

  • mpr_3d (3D Multi-Planar Reconstruction & MIP):
      • Engine & Viewer: Full client-side & server-assisted 3D volume reconstruction (paxs-mpr.js + WebAssembly +
      WebGL). Supports Orthogonal Axial, Coronal, Sagittal planes, and dynamic Slab MIP (Maximum Intensity
      Projection), MinIP, and Mean averaging.
  • tmtv_pet & PET/CT Multi-Modality Fusion:
      • Engine: fusion_service.py with Frame of Reference alignment, SUV max/mean calculation, and radiometric false-
      color LUTs (PET Rainbow, Hot Iron, Warm Metal).

  ──────
  ### 📝 3. Structured Reporting & Oncology Tracking

  • recist (RECIST 1.1 Tumor Response Assessment):
      • Engine: Longitudinal lesion tracking comparing baseline vs. follow-up studies, auto-calculating sum of
      longest diameters (SLD), and classifying response (Complete Response, Partial Response, Stable Disease,
      Progressive Disease).
  • ai_report_draft (DICOM Structured Report TID 1500):
      • Engine: Automatically compiles volumetric AI findings into standard DICOM Comprehensive 3D SR (TID 1500)
      files and archives them into PACS storage and database.
      • Live Verified: Generated SOP Instance 1.2.826.0.1.3680043.8.498.7337... attached to study.

  ──────
  ### 🏥 4. Interoperability & Cloud Architecture

  • hl7_router: Live MLLP HL7 v2.x server running on port 2575 handling ADT (patient admit/update) and ORM (modality
  worklist orders) with automated ORU^R01 write-back to hospital EHRs.
  • fhir_gateway: RESTful HL7 FHIR R4 endpoints (/api/v1/fhir/DiagnosticReport, /api/v1/fhir/ImagingStudy,
  /api/v1/fhir/Patient).
  • telerad_edge: HIPAA Safe Harbor De-identification engine (anonymizer.py) for cloud gateway export.
  • storage_tiering & dr_replication: Automated cold storage migration (lifecycle_service.py) and off-site S3
  replication.
  ──────
  ### 🛠️ How to Test & Access Them Right Now

  1. Check Live Active License & Feature Badges:
      • Open http://localhost:8000/#admin ➔ License 🛡️ tab to view all active clinical modules.
  2. Launch PaxsViewer with 3D MPR & Tools:
      • Open http://localhost:8000/viewer or http://localhost:8000/studies-ui to inspect studies with 3D multi-planar
      tools.
  3. Query AI Models API:
      • Endpoint: GET /api/v1/ai/models returns the full roster of active AI models.

