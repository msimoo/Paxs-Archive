# PaxsViewer PHP — Comprehensive Features Catalog

> A consolidated features catalog from **MedDream PaxsViewer 5.5** (PHP-based) and **OHIF Viewer v3** (architecture reference), adapted for a **modern PHP stack (Laravel/Symfony)**.

---

## Legend

| Icon | Meaning                                              |
| ---- | ---------------------------------------------------- |
| 🅿️   | Feature **ported/refactored** from MedDream PHP code |
| 🅾️   | Feature **inspired by** OHIF Viewer architecture     |
| ➕   | **New** feature (not in either source)               |
| ⭐   | **Priority** (Phase 1-2)                             |

---

## 1. Study Search & Management

| #   | Feature                       | Source | Phase | Description                                                  |
| --- | ----------------------------- | ------ | ----- | ------------------------------------------------------------ |
| 1   | ⭐ Patient/Study search table | 🅿️     | P1    | DataTable with patient name, ID, description, modality, date |
| 2   | ⭐ Date range filter          | 🅿️     | P1    | 1d/3d/1w/1m/1y/Any quick-select + custom date picker         |
| 3   | ⭐ Modality filter            | 🅿️     | P1    | Checkbox filter for all DICOM modalities                     |
| 4   | ⭐ Text search                | 🅿️     | P1    | Patient ID, Name, Accession Number, Description, Source AE   |
| 5   | Study thumbnails              | 🅿️     | P2    | Inline thumbnail generation via DCMTK bridge                 |
| 6   | Modality icons                | 🅿️     | P1    | Visual modality indicators                                   |
| 7   | Pagination                    | 🅿️     | P1    | Server-side paginated results                                |
| 8   | Multi-study select            | 🅿️     | P2    | Checkbox selection for batch operations                      |
| 9   | Study counts endpoint         | 🅿️     | P1    | GET /api/counts                                              |

### Search Actions

| #   | Feature                   | Source | Phase | Description                   |
| --- | ------------------------- | ------ | ----- | ----------------------------- |
| 10  | Open in viewer            | 🅿️     | P1    | Eye icon links to viewer page |
| 11  | Export study (DICOM/ZIP)  | 🅿️     | P5    | Laravel Queue-based export    |
| 12  | Forward to PACS (C-STORE) | 🅿️     | P5    | DCMTK dcmsnd wrapper          |
| 13  | Share to DICOM Library    | 🅿️     | P5    | Email-share integration       |
| 14  | Delete study              | 🅿️     | P5    | From PACS (permissions-based) |
| 15  | Add study (upload)        | 🅿️     | P5    | STOW-RS upload endpoint       |
| 16  | DICOMDIR generation       | 🅿️     | P5    | Via DCMTK dcmdir              |

---

## 2. Viewer Core

### 2.1 Image Display

| #   | Feature                       | Source | Phase | Description                                     |
| --- | ----------------------------- | ------ | ----- | ----------------------------------------------- |
| 17  | ⭐ Pixi.js canvas rendering   | 🅿️     | P1    | 2D canvas rendering (ported from MedDream)      |
| 18  | ⭐ DICOM to JPEG pipeline     | 🅿️     | P1    | Via DCMTK dcm2jpg + optional meddream_thumbnail |
| 19  | ⭐ Window/Level (WW/WC)       | 🅿️     | P1    | Interactive brightness/contrast                 |
| 20  | ⭐ Zoom (scroll/pinch)        | 🅿️     | P1    | Canvas-based zoom                               |
| 21  | ⭐ Pan (drag)                 | 🅿️     | P1    | Canvas-based pan                                |
| 22  | ⭐ Stack scroll (mouse wheel) | 🅿️     | P1    | Navigate DICOM slices                           |
| 23  | ⭐ Fit to window              | 🅿️     | P1    | Auto-fit image                                  |
| 24  | Reset view                    | 🅿️     | P1    | Reset WW/WC, zoom, pan                          |
| 25  | Image rotation (90°)          | 🅿️     | P2    | Canvas rotation                                 |
| 26  | Image flip (H/V)              | 🅿️     | P2    | Canvas mirror                                   |
| 27  | Image inversion               | 🅿️     | P2    | Grayscale inversion                             |
| 28  | True size display             | 🅿️     | P3    | Actual size from DICOM tags                     |
| 29  | Magnifying glass              | 🅿️     | P3    | Local zoom window                               |

### 2.2 Viewport Management

| #   | Feature                   | Source | Phase | Description                          |
| --- | ------------------------- | ------ | ----- | ------------------------------------ |
| 30  | ⭐ Multi-viewport grid    | 🅿️     | P1    | 1x1, 1x2, 2x1, 2x2, 1x3, 3x3 layouts |
| 31  | ⭐ Draggable splitters    | 🅿️     | P1    | Resize viewports                     |
| 32  | ⭐ Series thumbnail strip | 🅿️     | P1    | Bottom bar with all series           |
| 33  | Active viewport highlight | 🅿️     | P1    | Visual focus indicator               |
| 34  | Series drag to viewport   | 🅾️     | P2    | Drag series from strip to viewport   |
| 35  | Linked scrolling          | 🅾️     | P2    | Sync scroll across viewports         |
| 36  | Viewport maximization     | 🅿️     | P2    | Expand to full screen                |
| 37  | Layout presets            | 🅿️     | P2    | Quick layout switching               |

### 2.3 Cine Playback

| #   | Feature            | Source | Phase | Description                 |
| --- | ------------------ | ------ | ----- | --------------------------- |
| 38  | ⭐ Play/pause      | 🅿️     | P2    | Multi-frame cine loop       |
| 39  | Speed control      | 🅿️     | P2    | Adjustable FPS              |
| 40  | Forward/reverse    | 🅿️     | P2    | Direction toggle            |
| 41  | Auto-start (US/MR) | 🅿️     | P2    | Auto-play when cine-capable |
| 42  | Frame indicator    | 🅿️     | P2    | "Frame 12/64"               |

### 2.4 DICOM Metadata

| #   | Feature                 | Source | Phase | Description                                   |
| --- | ----------------------- | ------ | ----- | --------------------------------------------- |
| 43  | ⭐ DICOM Tag Browser    | 🅿️     | P2    | Deep metadata inspection (via DicomTagParser) |
| 44  | Instance tags endpoint  | 🅿️     | P2    | GET /api/instance/{uid}/tags                  |
| 45  | Custom tag filtering    | 🅿️     | P2    | Filter tags by (group, element) pairs         |
| 46  | Transfer syntax display | 🅿️     | P2    | Show image compression type                   |
| 47  | Series metadata panel   | 🅿️     | P2    | Series description, # instances               |

---

## 3. Measurement & Annotation Tools

| #   | Feature                    | Source | Phase | Description          |
| --- | -------------------------- | ------ | ----- | -------------------- |
| 48  | ⭐ Length measurement      | 🅿️     | P3    | Linear distance      |
| 49  | ⭐ Elliptical ROI          | 🅿️     | P3    | Area + statistics    |
| 50  | ⭐ Rectangle ROI           | 🅿️     | P3    | Rectangular region   |
| 51  | ⭐ Circle measurement      | 🅿️     | P3    | Circle area          |
| 52  | ⭐ Angle measurement       | 🅿️     | P3    | Angular degrees      |
| 53  | ⭐ Bi-directional (RECIST) | 🅿️     | P3    | RECIST 1.1 compliant |
| 54  | Freehand ROI               | 🅾️     | P3    | Free-drawn region    |
| 55  | Point annotation           | 🅿️     | P3    | Coordinate marker    |
| 56  | Arrow annotation           | 🅿️     | P3    | Directional arrow    |
| 57  | Text annotation            | 🅿️     | P3    | Free text overlay    |

### Annotation Persistence

| #   | Feature                      | Source | Phase | Description                    |
| --- | ---------------------------- | ------ | ----- | ------------------------------ |
| 58  | ⭐ Save as DICOM PR          | 🅿️     | P3    | DICOM Presentation State       |
| 59  | ⭐ Save as DICOM SR          | 🅿️     | P3    | DICOM Structured Report        |
| 60  | Save as JPEG overlay         | 🅿️     | P3    | Burn annotation onto image     |
| 61  | Annotation list per study    | 🅿️     | P3    | GET /api/annotations/{study}   |
| 62  | Annotation per image         | 🅿️     | P3    | GET /api/annotation/{uid}      |
| 63  | Annotation visibility toggle | 🅾️     | P3    | Show/hide specific annotations |
| 64  | Measurement table panel      | 🅾️     | P3    | Tabular view of measurements   |

---

## 4. Advanced Visualization

| #   | Feature                    | Source | Phase | Description                           |
| --- | -------------------------- | ------ | ----- | ------------------------------------- |
| 65  | MPR (MPR reconstruction)   | 🅾️     | P4    | Via DCMTK + custom rendering          |
| 66  | MIP / MinIP                | 🅾️     | P4    | Maximum/minimum intensity projection  |
| 67  | PET/CT fusion              | 🅾️     | P4    | Overlay with opacity control          |
| 68  | 3D volume rendering (link) | 🅿️     | P4    | Call external 3D tool                 |
| 69  | Smoothing (interpolation)  | 🅿️     | P3    | Bilinear/bicubic interpolation toggle |

---

## 5. ECG Viewer

| #   | Feature                  | Source | Phase | Description                       |
| --- | ------------------------ | ------ | ----- | --------------------------------- |
| 70  | ⭐ 12-lead ECG display   | 🅿️     | P4    | Port from MedDream's ecg/ classes |
| 71  | ⭐ Clinical grid overlay | 🅿️     | P4    | 1mm minor, 5mm major grid         |
| 72  | ⭐ Configurable columns  | 🅿️     | P4    | 1-4 column lead layout            |
| 73  | ⭐ Speed control (mm/s)  | 🅿️     | P4    | Default 25mm/s                    |
| 74  | ⭐ Gain control (mm/mV)  | 🅿️     | P4    | Default 10mm/mV                   |
| 75  | Lead labels              | 🅿️     | P4    | I, II, III, aVR, aVL, aVF, V1-V6  |

### ECG Signal Processing (PHP→JS port)

| #   | Feature                    | Source | Phase | Description                     |
| --- | -------------------------- | ------ | ----- | ------------------------------- |
| 76  | ⭐ DICOM waveform parsing  | 🅿️     | P4    | Parse 0x5400 group tags         |
| 77  | ⭐ Multi-format samples    | 🅿️     | P4    | SB/UB/SS/US sample types        |
| 78  | ⭐ Low-pass FIR filter     | 🅿️     | P4    | Remove high-frequency noise     |
| 79  | ⭐ High-pass FIR filter    | 🅿️     | P4    | Remove baseline wander          |
| 80  | ⭐ Notch filter (50/60 Hz) | 🅿️     | P4    | Power-line interference removal |
| 81  | ⭐ Baseline drift removal  | 🅿️     | P4    | Adaptive correction             |
| 82  | R-peak detection           | 🅿️     | P4    | QRS detection algorithm         |

### ECG Export

| #   | Feature          | Source | Phase | Description                 |
| --- | ---------------- | ------ | ----- | --------------------------- |
| 83  | ECG to PNG image | 🅿️     | P4    | Via ECGImage::create() port |
| 84  | ECG to PDF       | 🅿️     | P5    | Full report + waveform      |
| 85  | ECG print        | 🅿️     | P5    | Print layout                |

---

## 6. Structured Reports (SR)

| #   | Feature                    | Source | Phase | Description               |
| --- | -------------------------- | ------ | ----- | ------------------------- |
| 86  | ⭐ SR viewer               | 🅿️     | P4    | Render DICOM SR to HTML   |
| 87  | SR format endpoint         | 🅿️     | P4    | GET /api/sr/{uid}         |
| 88  | Key Object Selection (KOS) | 🅿️     | P4    | Key image marking         |
| 89  | Presentation State (GSPS)  | 🅿️     | P4    | Save/restore viewer state |

---

## 7. Report Module

| #   | Feature                   | Source | Phase | Description               |
| --- | ------------------------- | ------ | ----- | ------------------------- |
| 90  | ⭐ CKEditor 5 rich editor | 🅿️     | P4    | WYSIWYG report editing    |
| 91  | Report templates          | 🅿️     | P4    | Manageable in admin panel |
| 92  | Template management       | 🅿️     | P4    | CRUD via admin UI         |
| 93  | Report-to-study link      | 🅿️     | P4    | Associated via Study UID  |
| 94  | Draft/complete status     | 🅿️     | P4    | Workflow states           |
| 95  | Report history            | 🅿️     | P4    | Version tracking          |
| 96  | Report PDF export         | 🅿️     | P5    | Dompdf / TCPDF generation |
| 97  | Report print              | 🅿️     | P5    | Print template            |

---

## 8. PACS Integration

| #   | Feature              | Source | Phase | Description                     |
| --- | -------------------- | ------ | ----- | ------------------------------- |
| 98  | ⭐ FileSystem PACS   | 🅿️     | P1    | Local DICOM file viewing        |
| 99  | ⭐ Orthanc DICOMweb  | 🅾️     | P1    | Via QIDO-RS / WADO-RS endpoints |
| 100 | ⭐ DCM4CHEE 2.x      | 🅿️     | P2    | Database-based PACS             |
| 101 | ⭐ dcm4chee-arc 5.x  | 🅿️     | P2    | Modern enterprise PACS          |
| 102 | Conquest DICOM       | 🅿️     | P2    | Conquest .ini parsing           |
| 103 | WADO (HTTP)          | 🅿️     | P2    | Legacy WADO protocol            |
| 104 | DICOM (dcmqr/dcmrcv) | 🅿️     | P3    | Direct DICOM protocol           |
| 105 | ClearCanvas          | 🅿️     | P3    | Database-based PACS             |
| 106 | PacsOne              | 🅿️     | P3    | Windows-based PACS              |
| 107 | DCMSYS               | 🅿️     | P3    | Custom DICOM system             |

### DICOM Protocol Operations

| #   | Feature              | Source | Phase | Description                 |
| --- | -------------------- | ------ | ----- | --------------------------- |
| 108 | ⭐ C-FIND (query)    | 🅿️     | P2    | Via DCMTK dcmqr             |
| 109 | ⭐ C-MOVE (retrieve) | 🅿️     | P3    | Via DCMTK + dcmrcv listener |
| 110 | ⭐ C-STORE (store)   | 🅿️     | P3    | Via DCMTK dcmsnd            |
| 111 | ⭐ C-ECHO (verify)   | 🅿️     | P2    | Via DCMTK dcmecho           |
| 112 | WADO-RS (DICOMweb)   | 🅾️     | P2    | RESTful image retrieval     |
| 113 | QIDO-RS (DICOMweb)   | 🅾️     | P1    | RESTful study query         |
| 114 | STOW-RS (DICOMweb)   | 🅾️     | P2    | RESTful study upload        |

### Forward & Export

| #   | Feature              | Source | Phase | Description                  |
| --- | -------------------- | ------ | ----- | ---------------------------- |
| 115 | ⭐ C-STORE forward   | 🅿️     | P5    | Send to remote AET           |
| 116 | AET configuration    | 🅿️     | P5    | Manage destinations in admin |
| 117 | Forward dialog       | 🅿️     | P5    | Select AET, confirm          |
| 118 | Forward status check | 🅿️     | P5    | Job progress tracking        |
| 119 | DICOM file download  | 🅿️     | P5    | Individual/study ZIP         |
| 120 | JPEG/PNG screenshot  | 🅿️     | P5    | Viewport export              |
| 121 | DICOMDIR creation    | 🅿️     | P5    | Via DCMTK                    |
| 122 | Anonymized export    | ➕     | P5    | PHI removal on export        |
| 123 | Export status/delete | 🅿️     | P5    | Queue-based async export     |

---

## 9. Authentication & Authorization

| #   | Feature               | Source | Phase | Description                 |
| --- | --------------------- | ------ | ----- | --------------------------- |
| 124 | ⭐ Login form         | 🅿️     | P1    | Laravel authentication      |
| 125 | ⭐ Session management | 🅿️     | P1    | Laravel session + Redis     |
| 126 | JWT token auth (API)  | 🅾️     | P1    | Laravel Sanctum             |
| 127 | OAuth2 / SSO          | 🅾️     | P6    | Keycloak / SAML integration |
| 128 | RBAC (admin/user)     | 🅿️     | P1    | Laravel Gates + Policies    |
| 129 | PACS-level DB auth    | 🅿️     | P2    | Per-PACS credentials        |
| 130 | Demo login            | 🅿️     | P1    | Pre-configured demo account |
| 131 | Multi-PACS login      | 🅿️     | P1    | Database selection on login |
| 132 | Logoff                | 🅿️     | P1    | Clean session end           |

---

## 10. Settings & Configuration

| #   | Feature                     | Source | Phase | Description                          |
| --- | --------------------------- | ------ | ----- | ------------------------------------ |
| 133 | ⭐ Search properties        | 🅿️     | P2    | Default date, modality, page size    |
| 134 | ⭐ Viewer layout defaults   | 🅿️     | P2    | Default viewport layout per modality |
| 135 | ⭐ Info labels config       | 🅿️     | P2    | Patient/study/image overlay fields   |
| 136 | ⭐ Toolbar config           | 🅿️     | P2    | Show/hide/reorder tools              |
| 137 | ⭐ Windowing presets        | 🅿️     | P2    | Create/edit/delete WW/WC presets     |
| 138 | Report settings             | 🅿️     | P4    | Default template, editor config      |
| 139 | Admin-only settings         | 🅿️     | P6    | System-wide defaults                 |
| 140 | Settings JSON export/import | 🅿️     | P2    | Backup/restore settings              |
| 141 | Settings persistence        | 🅿️     | P2    | Saved per-user or global             |

---

## 11. Administration (Admin Panel)

| #   | Feature                 | Source | Phase | Description                    |
| --- | ----------------------- | ------ | ----- | ------------------------------ |
| 142 | ⭐ User management      | 🅿️     | P6    | CRUD users (Laravel Filament)  |
| 143 | Role/permission mgmt    | ➕     | P6    | RBAC configuration UI          |
| 144 | ⭐ Audit log viewer     | 🅿️     | P6    | Searchable audit trail         |
| 145 | PACS connection mgmt    | 🅿️     | P6    | Add/edit PACS connections      |
| 146 | System health dashboard | ➕     | P6    | Server status, uptime, metrics |
| 147 | License management      | 🅿️     | P6    | License key entry/validation   |
| 148 | System info page        | 🅿️     | P6    | PHP version, DCMTK, DB status  |
| 149 | Language management     | 🅿️     | P6    | Enable/disable locales         |
| 150 | Temp file cleanup       | 🅿️     | P6    | Configure cleanup schedule     |

---

## 12. Internationalization (i18n)

| #   | Feature                  | Source | Phase | Description                |
| --- | ------------------------ | ------ | ----- | -------------------------- |
| 151 | ⭐ English               | 🅿️     | P1    | Full English UI            |
| 152 | German (DE)              | 🅿️     | P2    | Port MedDream translations |
| 153 | French (FR)              | 🅿️     | P2    | Port MedDream translations |
| 154 | Spanish (ES)             | 🅿️     | P2    | Port MedDream translations |
| 155 | Lithuanian (LT)          | 🅿️     | P2    | Port MedDream translations |
| 156 | Russian (RU)             | 🅿️     | P2    | Port MedDream translations |
| 157 | Finnish (FI)             | 🅿️     | P2    | Port MedDream translations |
| 158 | Swedish (SE)             | 🅿️     | P2    | Port MedDream translations |
| 159 | Language selector        | 🅿️     | P1    | Dropdown in UI             |
| 160 | Auto-detect browser lang | 🅿️     | P1    | i18next browser detection  |

---

## 13. Mobile Viewer

| #   | Feature                | Source | Phase | Description                 |
| --- | ---------------------- | ------ | ----- | --------------------------- |
| 161 | ⭐ Mobile-optimized UI | 🅿️     | P5    | Responsive Bootstrap layout |
| 162 | Touch controls         | 🅿️     | P5    | Swipe, pinch-zoom, tap      |
| 163 | Mobile viewport        | 🅿️     | P5    | Optimized for small screens |
| 164 | Device detection       | 🅿️     | P5    | Auto-switch mobile/desktop  |
| 165 | Mobile toolbar         | 🅿️     | P5    | Essential tools only        |
| 166 | PWA support            | ➕     | P5    | Installable, offline cache  |

---

## 14. Infrastructure

| #   | Feature                 | Source | Phase | Description                 |
| --- | ----------------------- | ------ | ----- | --------------------------- |
| 167 | ⭐ Docker Compose       | 🅾️     | P6    | PHP + Nginx + MySQL + Redis |
| 168 | FrankenPHP / RoadRunner | ➕     | P6    | Persistent PHP workers      |
| 169 | Laravel Queue (Redis)   | 🅿️     | P2    | Background job processing   |
| 170 | Redis caching           | 🅿️     | P2    | Session + thumbnail cache   |
| 171 | Thumbnail cache         | 🅿️     | P2    | Filesystem + Redis          |

## 15. Feature Count Summary

| Category             | MedDream (PHP) | OHIF Inspired |  New  |  TOTAL  |
| -------------------- | :------------: | :-----------: | :---: | :-----: |
| Search & Management  |       14       |       1       |   0   | **15**  |
| Viewer Core          |       26       |       4       |   0   | **30**  |
| Measurements         |       14       |       3       |   0   | **17**  |
| Advanced Viz         |       3        |       3       |   0   |  **6**  |
| ECG Viewer           |       16       |       0       |   0   | **16**  |
| Structured Reports   |       4        |       0       |   0   |  **4**  |
| Report Module        |       8        |       0       |   0   |  **8**  |
| PACS Integration     |       25       |       2       |   1   | **28**  |
| Auth & Authorization |       8        |       1       |   0   |  **9**  |
| Settings & Config    |       8        |       0       |   1   |  **9**  |
| Administration       |       8        |       0       |   2   | **10**  |
| i18n                 |       9        |       0       |   0   |  **9**  |
| Mobile               |       5        |       0       |   1   |  **6**  |
| Infrastructure       |       2        |       0       |   2   |  **4**  |
| **TOTAL**            |    **150**     |    **14**     | **7** | **171** |

**Key Insight:** ~88% of features come directly from MedDream's existing PHP codebase, ~8% are inspired by OHIF architecture patterns, and ~4% are new features.

---

## 16. PHP Dependencies (Composer Packages)

| Package                     | Purpose                  | Required |
| --------------------------- | ------------------------ | -------- |
| `laravel/framework`         | Full-stack framework     | ✅       |
| `laravel/sanctum`           | API token auth           | ✅       |
| `rbraunm/class_dicom`       | DICOM parsing (PHP 8.5+) | ⭐       |
| `cboden/ratchet`            | WebSocket server         | Optional |
| `predis/predis`             | Redis client             | ✅       |
| `barryvdh/laravel-dompdf`   | PDF generation           | ⭐       |
| `spatie/laravel-permission` | RBAC                     | ✅       |
| `laravel/horizon`           | Queue monitoring         | ✅       |
| `laravel/telescope`         | Debug toolbar            | Dev      |
| `filament/filament`         | Admin panel              | ✅       |

## 17. JavaScript Dependencies (NPM Packages)

| Package               | Purpose                | Required |
| --------------------- | ---------------------- | -------- |
| `pixi.js`             | 2D canvas rendering    | ✅       |
| `riot`                | Reactive UI components | ✅       |
| `i18next`             | Internationalization   | ✅       |
| `i18next-xhr-backend` | Translation loading    | ✅       |
| `jquery`              | DOM manipulation       | ✅       |
| `bootstrap`           | UI framework           | ✅       |
| `datatables`          | Search table           | ✅       |
| `ckeditor5`           | Report editor          | ✅       |
| `hammerjs`            | Touch gestures         | ⭐       |
| `vite`                | Build tool             | ✅       |
