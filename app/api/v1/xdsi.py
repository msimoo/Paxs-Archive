"""Cross-Enterprise Document Sharing for Imaging (XDS-I.b) REST API.

  POST /api/v1/xdsi/register                   — Provide and Register Document Set-b [ITI-41 / RAD-68]
  GET  /api/v1/xdsi/documents                  — FindDocuments Registry Query [ITI-18]
  GET  /api/v1/xdsi/documents/{entry_uuid}       — RetrieveDocumentSet Repository Retrieve [ITI-43]
  GET  /api/v1/xdsi/documents/{entry_uuid}/ebxml — Download ebXML metadata XML
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.db.models import User, XDSDocument
from app.db.session import get_db
from app.services import xdsi_service

log = logging.getLogger("paxs.api.xdsi")

router = APIRouter(prefix="/api/v1/xdsi", tags=["XDS-I.b Cross-Enterprise Document Sharing"])


class RegisterDocumentRequest(BaseModel):
    patient_id: str = Field(..., description="Patient ID (CX format)")
    document_title: str = Field(..., description="Document Title (e.g. Chest CT Manifest)")
    content_base64: str | None = Field(None, description="Base64 encoded binary content or raw string")
    content_text: str | None = Field(None, description="Plaintext / JSON manifest content")
    mime_type: str = Field("application/dicom", description="MIME type (application/dicom, application/pdf, text/xml)")
    study_instance_uid: str | None = Field(None, description="Optional StudyInstanceUID")
    class_code: str = Field("IMAGES", description="Class code")
    type_code: str = Field("RADIOLOGY", description="Type code")
    format_code: str = Field("urn:ihe:rad:TEXT:2011", description="Format code")


def _doc_to_dict(doc: XDSDocument) -> dict[str, Any]:
    d = doc.__dict__
    c_at = d.get("created_at")
    u_at = d.get("updated_at")
    return {
        "id": d.get("id"),
        "entry_uuid": d.get("entry_uuid"),
        "unique_id": d.get("unique_id"),
        "patient_id": d.get("patient_id"),
        "study_instance_uid": d.get("study_instance_uid"),
        "document_title": d.get("document_title"),
        "mime_type": d.get("mime_type"),
        "class_code": d.get("class_code"),
        "type_code": d.get("type_code"),
        "format_code": d.get("format_code"),
        "repository_unique_id": d.get("repository_unique_id"),
        "availability_status": d.get("availability_status"),
        "size_bytes": d.get("size_bytes", 0),
        "sha256_hash": d.get("sha256_hash"),
        "file_path": d.get("file_path"),
        "created_at": c_at.isoformat() if hasattr(c_at, "isoformat") else (str(c_at) if c_at else None),
        "updated_at": u_at.isoformat() if hasattr(u_at, "isoformat") else (str(u_at) if u_at else None),
    }


@router.post("/register", response_class=JSONResponse)
async def register_document(
    req: RegisterDocumentRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Provide and Register Document Set-b [ITI-41 / RAD-68]."""
    import base64

    content_bytes = b""
    if req.content_base64:
        content_bytes = base64.b64decode(req.content_base64)
    elif req.content_text:
        content_bytes = req.content_text.encode("utf-8")
    else:
        content_bytes = f"XDS-I.b MANIFEST FOR STUDY {req.study_instance_uid}".encode("utf-8")

    try:
        doc = await xdsi_service.register_document(
            db,
            patient_id=req.patient_id,
            document_title=req.document_title,
            content_bytes=content_bytes,
            mime_type=req.mime_type,
            study_instance_uid=req.study_instance_uid,
            class_code=req.class_code,
            type_code=req.type_code,
            format_code=req.format_code,
            performed_by=user.username,
        )
        await db.commit()
        return JSONResponse(_doc_to_dict(doc), status_code=201)
    except Exception as exc:
        log.exception("Failed to register XDS document for patient %s", req.patient_id)
        raise HTTPException(400, f"XDS Registration failed: {exc}") from exc


@router.get("/documents", response_class=JSONResponse)
async def query_documents(
    patient_id: str | None = Query(None, description="Filter by Patient ID"),
    class_code: str | None = Query(None, description="Filter by Class Code"),
    status: str = Query("APPROVED", description="Availability Status (APPROVED / DEPRECATED)"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """FindDocuments [ITI-18] ebXML Registry Query."""
    docs = await xdsi_service.query_documents(
        db, patient_id=patient_id, class_code=class_code, availability_status=status
    )
    return JSONResponse([_doc_to_dict(d) for d in docs])


@router.get("/documents/{entry_uuid}", response_class=Response)
async def retrieve_document(
    entry_uuid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """RetrieveDocumentSet [ITI-43] Repository Retrieve."""
    try:
        doc, content = await xdsi_service.retrieve_document(db, entry_uuid)
        return Response(content=content, media_type=doc.mime_type)
    except ValueError as val_err:
        raise HTTPException(404, str(val_err)) from val_err


@router.get("/documents/{entry_uuid}/ebxml", response_class=Response)
async def get_ebxml_metadata(
    entry_uuid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Download ebXML SubmitObjectsRequest metadata XML string."""
    doc = await xdsi_service.get_document_by_uuid(db, entry_uuid)
    if not doc:
        raise HTTPException(404, f"XDS Document '{entry_uuid}' not found")

    xml_str = xdsi_service.generate_ebxml_metadata(
        doc.entry_uuid,
        doc.unique_id,
        doc.patient_id,
        doc.document_title,
        doc.mime_type,
        doc.class_code,
        doc.format_code,
        doc.availability_status,
    )
    return Response(content=xml_str, media_type="text/xml")
