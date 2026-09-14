"""Cross-Enterprise Document Sharing for Imaging (XDS-I.b) Service.

Implements IHE XDS-I.b Integration Profile profile transactions:
  * Provide and Register Document Set-b [ITI-41 / RAD-68]
  * FindDocuments Registry Query [ITI-18]
  * RetrieveDocumentSet Repository Retrieve [ITI-43]
"""

from __future__ import annotations

import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import XDSDocument
from app.services import audit_service, storage_service

log = logging.getLogger("paxs.xdsi")

XDS_REPOSITORY_UNIQUE_ID = "1.2.826.0.1.3680043.8.498.REPO"


def generate_ebxml_metadata(
    entry_uuid: str,
    unique_id: str,
    patient_id: str,
    document_title: str,
    mime_type: str = "application/dicom",
    class_code: str = "IMAGES",
    format_code: str = "urn:ihe:rad:TEXT:2011",
    availability_status: str = "APPROVED",
) -> str:
    """Build IHE XDS-I.b ebXML SubmitObjectsRequest metadata XML string."""
    root = ET.Element(
        "lcm:SubmitObjectsRequest",
        {
            "xmlns:lcm": "urn:oasis:names:tc:ebxml-regrep:xsd:lcm:3.0",
            "xmlns:rim": "urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0",
        },
    )
    obj_list = ET.SubElement(root, "rim:RegistryObjectList")

    ext_obj = ET.SubElement(
        obj_list,
        "rim:ExtrinsicObject",
        {
            "id": entry_uuid,
            "objectType": "urn:uuid:7f547165-e8d7-425d-869d-a7a8264789d6",
            "mimeType": mime_type,
            "status": f"urn:oasis:names:tc:ebxml-regrep:ResponseStatusType:{availability_status}",
        },
    )

    # Title
    name = ET.SubElement(ext_obj, "rim:Name")
    loc_str = ET.SubElement(name, "rim:LocalizedString", {"value": document_title})

    # External Identifiers (Patient ID & Unique ID)
    ext_pat = ET.SubElement(
        ext_obj,
        "rim:ExternalIdentifier",
        {
            "id": f"urn:uuid:{uuid.uuid4()}",
            "identificationScheme": "urn:uuid:58a74358-b6e1-4aa4-3072-625d14a80a2d",
            "value": patient_id,
        },
    )
    ext_pat_name = ET.SubElement(ext_pat, "rim:Name")
    ET.SubElement(ext_pat_name, "rim:LocalizedString", {"value": "XDSDocumentEntry.patientId"})

    ext_uid = ET.SubElement(
        ext_obj,
        "rim:ExternalIdentifier",
        {
            "id": f"urn:uuid:{uuid.uuid4()}",
            "identificationScheme": "urn:uuid:2e8241b7-9b31-4e6c-8d8d-72b84726e0e6",
            "value": unique_id,
        },
    )
    ext_uid_name = ET.SubElement(ext_uid, "rim:Name")
    ET.SubElement(ext_uid_name, "rim:LocalizedString", {"value": "XDSDocumentEntry.uniqueId"})

    return ET.tostring(root, encoding="utf-8").decode("utf-8")


async def register_document(
    session: AsyncSession,
    patient_id: str,
    document_title: str,
    content_bytes: bytes,
    mime_type: str = "application/dicom",
    study_instance_uid: str | None = None,
    class_code: str = "IMAGES",
    type_code: str = "RADIOLOGY",
    format_code: str = "urn:ihe:rad:TEXT:2011",
    performed_by: str = "system",
) -> XDSDocument:
    """Provide and Register a Document Set-b [ITI-41 / RAD-68] entry into XDS-I.b Repository."""
    entry_uuid = f"urn:uuid:{uuid.uuid4()}"
    unique_id = generate_uid()

    import hashlib

    # Store file binary
    filename_key = f"xds_{patient_id}_{unique_id}.dcm" if mime_type == "application/dicom" else f"xds_{patient_id}_{unique_id}.bin"
    storage_service.save_bytes(filename_key, content_bytes)
    file_path = filename_key
    sha256_hash = hashlib.sha256(content_bytes).hexdigest()

    doc = XDSDocument(
        entry_uuid=entry_uuid,
        unique_id=unique_id,
        patient_id=patient_id,
        study_instance_uid=study_instance_uid,
        document_title=document_title,
        mime_type=mime_type,
        class_code=class_code,
        type_code=type_code,
        format_code=format_code,
        repository_unique_id=XDS_REPOSITORY_UNIQUE_ID,
        availability_status="APPROVED",
        size_bytes=len(content_bytes),
        sha256_hash=sha256_hash,
        file_path=file_path,
    )
    session.add(doc)
    await session.flush()

    audit_service.add_audit(
        session,
        sop_instance_uid=None,
        study_instance_uid=study_instance_uid,
        action="XDS_DOCUMENT_REGISTERED",
        performed_by=performed_by,
        file_path=file_path,
        sha256=sha256_hash,
    )

    log.info("XDS-I.b document registered: %s (unique_id=%s)", entry_uuid, unique_id)
    return doc


async def query_documents(
    session: AsyncSession,
    patient_id: str | None = None,
    class_code: str | None = None,
    availability_status: str = "APPROVED",
    limit: int = 100,
) -> list[XDSDocument]:
    """FindDocuments [ITI-18] ebXML Registry Query."""
    stmt = select(XDSDocument)
    if patient_id:
        stmt = stmt.where(XDSDocument.patient_id == patient_id)
    if class_code:
        stmt = stmt.where(XDSDocument.class_code == class_code.upper())
    if availability_status:
        stmt = stmt.where(XDSDocument.availability_status == availability_status.upper())
    stmt = stmt.order_by(XDSDocument.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def get_document_by_uuid(session: AsyncSession, entry_uuid: str) -> XDSDocument | None:
    """Get XDS Document registry entry by entry_uuid or unique_id."""
    stmt = select(XDSDocument).where(
        (XDSDocument.entry_uuid == entry_uuid) | (XDSDocument.unique_id == entry_uuid)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def retrieve_document(session: AsyncSession, entry_uuid: str) -> tuple[XDSDocument, bytes]:
    """RetrieveDocumentSet [ITI-43] Repository Retrieve."""
    doc = await get_document_by_uuid(session, entry_uuid)
    if not doc or not storage_service.exists(doc.file_path):
        raise ValueError(f"XDS Document '{entry_uuid}' not found in repository")
    content = storage_service.read_bytes(
        doc.file_path, backend_name=getattr(doc, "storage_backend", None)
    )
    return doc, content
