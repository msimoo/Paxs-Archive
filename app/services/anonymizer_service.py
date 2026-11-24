"""Compatibility entry point for the DICOM anonymizer service.

The application historically imports :mod:`app.services.anonymizer`; keep that
public path while exposing the same implementation under the service-oriented
name used by newer integrations.
"""

from app.services.anonymizer import (
    DATE_KEYWORDS,
    PHI_KEYWORDS,
    PHI_SEQUENCES,
    PS315_DEIDENTIFICATION_METHOD,
    SAFE_PRIVATE_TAGS,
    TIME_KEYWORDS,
    anonymize_dataset,
    anonymize_files_to_zip,
    anonymize_files_to_zip_file,
    derive_tenant_date_salt,
)

__all__ = [
    "DATE_KEYWORDS",
    "PHI_KEYWORDS",
    "PHI_SEQUENCES",
    "PS315_DEIDENTIFICATION_METHOD",
    "SAFE_PRIVATE_TAGS",
    "TIME_KEYWORDS",
    "anonymize_dataset",
    "anonymize_files_to_zip",
    "anonymize_files_to_zip_file",
    "derive_tenant_date_salt",
]
