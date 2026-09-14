
# الموجهات المعمارية وتنظيم المشروع (Architecture & Setup Prompts) 

Role: Senior Python Software Architect specializing in Medical Imaging (PACS/DICOM Systems).

Context:
I am re-structuring my medical image archiving system named "paxs archive".
Core Tech Stack:
- Database: MySQL
- Frontend/Viewer: Custom modified OHIF viewer ("paxs viewer") integrating via DICOMweb (WADO-RS, QIDO-RS).

Task:
Refactor and reorganize the project directory structure to follow Domain-Driven Design (DDD) and Clean Architecture principles.

Requirements:
1. Provide a comprehensive directory tree layout showing clear separation between DICOM SCP/SCU services, REST API endpoints (DICOMweb), MySQL ORM/Database access layers, File Storage Management, and the Viewer build output.
2. Draft a `config.py` module design that cleanly manages environment variables (DICOM AE Title, Port, MySQL credentials, Storage Paths, WADO URIs).
3. Provide Python code for a clean Dependency Injection pattern separating the C-STORE DIMSE listener from the MySQL metadata indexer.

Ensure the code adheres to PEP 8 standards, handles DICOM Transfer Syntaxes safely, and avoids tight coupling.