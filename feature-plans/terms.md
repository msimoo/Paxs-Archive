## Terms and Conditions

### 1. Acceptance of Terms

By accessing, installing, or integrating this DICOM Medical Images Archiving System ("System" or "Service"), operated and developed by **Mohamed Omer Elsamani** ("Developer", "we", "us"), any accessing entity, healthcare institution, or clinical user ("User", "Licensee") agrees to be bound by these Terms and Conditions.

### 2. Scope of Service & Technical Description

The System functions as a Picture Archiving and Communication System (PACS) and Vendor Neutral Archive (VNA) component designed for DICOM (Digital Imaging and Communications in Medicine) image ingest, indexing, storage, routing, and DICOMweb retrieval (C-STORE, C-FIND, C-MOVE, WADO-RS, STOW-RS, QIDO-RS).

### 3. Clinical and Medical Disclaimer

* **Regulatory & Diagnostic Notice**: Unless explicitly certified as a cleared medical device under applicable jurisdiction standards (such as FDA 510(k), CE-MDR, or local regulatory health authorities), this software is provided for archival, communication, secondary review, and administrative workflows.
* **Clinical Responsibility**: The System does not provide medical decisions or clinical diagnoses. Licensed medical practitioners and radiologists retain sole liability for primary diagnostic interpretations, patient management, and treatment decisions.

### 4. Patient Privacy, Health Data, and Compliance

* **Regulatory Compliance**: Users storing, transmitting, or processing Protected Health Information (PHI) or Personally Identifiable Information (PII) must ensure their deployment complies with national and international health data privacy legislation (e.g., HIPAA, GDPR, local Health Data Governance laws).
* **Access Control**: Users are strictly responsible for implementing Role-Based Access Controls (RBAC), securing administrative credentials, and preventing unauthorized access to DICOM study archives.

### 5. System Uptime, Storage, and Data Retention

* While the System employs data integrity checks and encryption standards, the end-user or hosting institution remains responsible for maintaining off-site secondary disaster recovery backups.
* The Developer is not responsible for data loss resulting from hardware failure, cloud provider outages, misconfigured storage buckets, or unauthorized network intrusions at the licensee's site.

### 6. Limitation of Liability

To the maximum extent permitted by law, Mohamed Omer Elsamani shall not be liable for any direct, indirect, incidental, consequential, or punitive damages—including loss of medical records, system downtime, diagnostic delay, or financial loss—arising out of the deployment or misuse of this System.

---

## Privacy Policy

### 1. Information Processing & Storage

* **DICOM Metadata**: The System processes header information embedded in uploaded DICOM files, including Patient ID, Accession Number, Modality (CT, MRI, X-Ray, Ultrasound), Study Date, Referring Physician, and Transfer Syntax specifications.
* **Audit & Transaction Logs**: The System records DICOM transfer activity, API calls, IP addresses, user authentication attempts, and DICOM C-STORE / C-MOVE event logs for security auditing.

### 2. Data Security & Technical Standards

* **Encryption in Transit**: All DICOM protocol transmissions (DICOM-TLS) and web API communications (DICOMweb / REST) require TLS 1.3 encryption.
* **Encryption at Rest**: Stored DICOM files (`.dcm`) and associated database indices are protected using AES-256 bit encryption.
* **De-identification Capabilities**: Integrated de-identification features strictly follow DICOM PS 3.15 Annex E standards for research, teaching, or secondary export pipelines.

### 3. Data Disclosures and Third Parties

We do not sell, license, or monetize stored DICOM datasets or patient records. Data stored within the System is accessed solely by authorized endpoints, PACS nodes, or integrated healthcare interfaces (HL7 / FHIR) configured directly by the system administrator.

---

## Digital Footprint & Application Footer

### Standard Web & App Interface Footer

```text
================================================================================
DICOM Medical Image Archiving System (PACS / VNA)
Developer: Mohamed Omer Elsamani | Portfolio: https://elsamanii.rf.gd | GitHub: https://github.com/msimoo

[ Terms of Service ] | [ Privacy & PHI Policy ] | [ DICOM Conformance Statement ] | [ Security Overview ]

Medical Disclaimer:
This System is engineered for DICOM image archiving and network communication. Primary diagnostic decisions 
must be conducted on validated, regulatory-compliant medical display workstations.

© 2026 Mohamed Omer Elsamani. All Rights Reserved.
================================================================================

```

### Metadata & Code Header Footprint (For Source Code & API Documentation)

```text
/**
 * Package: DICOM Image Archiving & PACS Server
 * Author: Mohamed Omer Elsamani
 * Portfolio: https://elsamanii.rf.gd
 * Source Code: https://github.com/msimoo
 * License: Proprietary / Authorized Healthcare License
 * Compliance: DICOM PS 3.15 / TLS 1.3 / AES-256 Encrypted
 */

 