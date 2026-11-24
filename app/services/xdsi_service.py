"""Cross-Enterprise Document Sharing for Imaging (XDS-I.b) Service.

Implements IHE XDS-I.b Integration Profile profile transactions:
  * Provide and Register Document Set-b [ITI-41 / RAD-68]
  * FindDocuments Registry Query [ITI-18]
  * RetrieveDocumentSet Repository Retrieve [ITI-43]
"""

from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from pydicom import dcmread
from pydicom.uid import KeyObjectSelectionDocumentStorage, UID, generate_uid
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Study, XDSDocument
from app.services import audit_service, query_service, storage_service

log = logging.getLogger("paxs.xdsi")

XDS_REPOSITORY_UNIQUE_ID = "1.2.826.0.1.3680043.8.498.REPO"


def _scope_document_to_tenant(statement: Any, tenant_id: int | None, include_all: bool) -> Any:
    """Scope XDS ownership and any linked Study to the same tenant."""
    statement = query_service.apply_tenant_scope(
        statement, XDSDocument.tenant_id, tenant_id, include_all
    )
    if include_all:
        return statement
    study_scope = query_service.tenant_predicate(Study.tenant_id, tenant_id, False)
    linked_study = select(Study.id).where(
        Study.study_instance_uid == XDSDocument.study_instance_uid,
        study_scope,
    ).exists()
    return statement.where(or_(XDSDocument.study_instance_uid.is_(None), linked_study))


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


def validate_dicom_manifest(content: bytes, study_uid: str | None) -> None:
    """Require a DICOM Part 10 Key Object Selection document with image references."""
    invalid = "Expected a DICOM Key Object Selection manifest with valid image references"
    try:
        ds = dcmread(BytesIO(content), stop_before_pixels=True)
        evidence = ds.CurrentRequestedProcedureEvidenceSequence
        sop_uid = UID(ds.SOPInstanceUID)
        matching_evidence = any(
            UID(study.StudyInstanceUID).is_valid
            and (study_uid is None or study.StudyInstanceUID == study_uid)
            and any(
                UID(series.SeriesInstanceUID).is_valid
                and any(
                    UID(ref.ReferencedSOPClassUID).is_valid and UID(ref.ReferencedSOPInstanceUID).is_valid
                    for ref in series.ReferencedSOPSequence
                )
                for series in study.ReferencedSeriesSequence
            )
            for study in evidence
        )
        valid = (
            ds.SOPClassUID == ds.file_meta.MediaStorageSOPClassUID == KeyObjectSelectionDocumentStorage
            and sop_uid.is_valid and sop_uid == ds.file_meta.MediaStorageSOPInstanceUID
            and bool(ds.ContentSequence) and matching_evidence
        )
    except Exception as exc:  # Malformed DICOM can fail in the parser or on attribute access.
        raise ValueError(invalid) from exc
    if not valid:
        raise ValueError(invalid)


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
    tenant_id: int | None = None,
    include_all: bool = False,
) -> tuple[XDSDocument, storage_service.StagedObject]:
    """Provide and Register a Document Set-b [ITI-41 / RAD-68] entry into XDS-I.b Repository."""
    entry_uuid = f"urn:uuid:{uuid.uuid4()}"
    unique_id = generate_uid()
    document_tenant_id = await _resolve_registration_tenant(
        session, study_instance_uid, tenant_id, include_all
    )

    file_path = storage_service.xds_document_key(unique_id, mime_type)
    staged: storage_service.StagedObject | None = None
    try:
        staged = await asyncio.to_thread(
            storage_service.stage_bytes,
            file_path,
            content_bytes,
            tenant_id=document_tenant_id,
            require_isolated=True,
        )
        await asyncio.to_thread(staged.verify)
        sha256_hash = hashlib.sha256(content_bytes).hexdigest()
        doc = XDSDocument(
            entry_uuid=entry_uuid,
            unique_id=unique_id,
            tenant_id=document_tenant_id,
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
            file_path=staged.key,
        )
        session.add(doc)
        await session.flush()
        audit_service.add_audit(
            session,
            sop_instance_uid=None,
            study_instance_uid=study_instance_uid,
            action="XDS_DOCUMENT_REGISTERED",
            performed_by=performed_by,
            file_path=staged.key,
            sha256=sha256_hash,
        )
    except Exception:  # discard staged bytes while preserving the original error
        if staged is not None:
            await asyncio.to_thread(staged.discard)
        raise

    log.info("XDS-I.b document registered: %s (unique_id=%s)", entry_uuid, unique_id)
    return doc, staged


async def _resolve_registration_tenant(
    session: AsyncSession,
    study_instance_uid: str | None,
    tenant_id: int | None,
    include_all: bool,
) -> int | None:
    """Bind linked documents to their Study owner; never infer from Patient ID."""
    if not study_instance_uid:
        return tenant_id
    statement = select(Study.id, Study.tenant_id).where(
        Study.study_instance_uid == study_instance_uid
    )
    if not include_all:
        statement = query_service.apply_study_tenant_scope(
            statement, tenant_id, include_all=False
        )
    study = (await session.execute(statement)).one_or_none()
    if study is None:
        raise LookupError("Study not found")

    owner_tenant_id = study.tenant_id
    if include_all:
        owner_check = query_service.apply_study_tenant_scope(
            select(Study.id), owner_tenant_id, include_all=False
        ).where(Study.study_instance_uid == study_instance_uid)
        if await session.scalar(owner_check) is None:
            raise LookupError("Study not found")
    return owner_tenant_id


async def query_documents(
    session: AsyncSession,
    patient_id: str | None = None,
    class_code: str | None = None,
    availability_status: str = "APPROVED",
    limit: int = 100,
    tenant_id: int | None = None,
    include_all: bool = False,
    study_instance_uid: str | None = None,
) -> list[XDSDocument]:
    """FindDocuments [ITI-18] ebXML Registry Query."""
    stmt = _scope_document_to_tenant(select(XDSDocument), tenant_id, include_all)
    if patient_id:
        stmt = stmt.where(XDSDocument.patient_id == patient_id)
    if study_instance_uid:
        stmt = stmt.where(XDSDocument.study_instance_uid == study_instance_uid)
    if class_code:
        stmt = stmt.where(XDSDocument.class_code == class_code.upper())
    if availability_status:
        stmt = stmt.where(XDSDocument.availability_status == availability_status.upper())
    stmt = stmt.order_by(XDSDocument.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def get_document_by_uuid(
    session: AsyncSession,
    entry_uuid: str,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> XDSDocument | None:
    """Get XDS Document registry entry by entry_uuid or unique_id."""
    stmt = select(XDSDocument).where(
        (XDSDocument.entry_uuid == entry_uuid) | (XDSDocument.unique_id == entry_uuid)
    )
    stmt = _scope_document_to_tenant(stmt, tenant_id, include_all)
    return (await session.execute(stmt)).scalar_one_or_none()


async def retrieve_document(
    session: AsyncSession,
    entry_uuid: str,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> tuple[XDSDocument, bytes]:
    """RetrieveDocumentSet [ITI-43] Repository Retrieve."""
    doc = await get_document_by_uuid(
        session, entry_uuid, tenant_id, include_all
    )
    if not doc or not await asyncio.to_thread(storage_service.exists, doc.file_path):
        raise ValueError(f"XDS Document '{entry_uuid}' not found in repository")
    content = await asyncio.to_thread(
        storage_service.read_bytes,
        doc.file_path,
        backend_name=getattr(doc, "storage_backend", None),
    )
    return doc, content
