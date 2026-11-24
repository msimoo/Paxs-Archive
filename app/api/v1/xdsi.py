"""Cross-Enterprise Document Sharing for Imaging (XDS-I.b) REST API.

  POST /api/v1/xdsi/register                   — Provide and Register Document Set-b [ITI-41 / RAD-68]
  GET  /api/v1/xdsi/documents                  — FindDocuments Registry Query [ITI-18]
  GET  /api/v1/xdsi/documents/{entry_uuid}       — RetrieveDocumentSet Repository Retrieve [ITI-43]
  GET  /api/v1/xdsi/documents/{entry_uuid}/ebxml — Download ebXML metadata XML
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_admin, require_csrf, tenant_scope
from app.core.config import settings
from app.core.concurrency import run_blocking
from app.db.models import User
from app.db.session import get_db
from app.services import xdsi_service

log = logging.getLogger("paxs.api.xdsi")

router = APIRouter(
    prefix="/api/v1/xdsi",
    tags=["XDS-I.b Cross-Enterprise Document Sharing"],
    dependencies=[Depends(require_csrf)],
)
_BASE64_SHAPE = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_SAFE_XDS_RETRIEVAL_MIME_TYPES = frozenset(
    {"application/dicom", "application/pdf", "text/xml"}
)


class RegisterDocumentRequest(BaseModel):
    patient_id: str = Field(..., description="Patient ID (CX format)")
    document_title: str = Field(..., description="Document Title (e.g. Chest CT Manifest)")
    content_base64: str | None = Field(None, description="Base64 encoded document bytes")
    content_text: str | None = Field(None, description="Plaintext / JSON manifest content")
    mime_type: str = Field("application/dicom", description="MIME type (application/dicom, application/pdf, text/xml)")
    study_instance_uid: str | None = Field(None, description="Optional StudyInstanceUID")
    class_code: str = Field("IMAGES", description="Class code")
    type_code: str = Field("RADIOLOGY", description="Type code")
    format_code: str = Field("urn:ihe:rad:TEXT:2011", description="Format code")

def _safe_xds_mime_type(value: str | None) -> str:
    """Map legacy or invalid metadata to an inert type on every read surface."""
    normalized = (value or "").split(";", 1)[0].strip().lower()
    if normalized in _SAFE_XDS_RETRIEVAL_MIME_TYPES:
        return normalized
    return "application/octet-stream"


def _base64_decoded_size_if_well_formed(value: str) -> int | None:
    """Estimate decoded bytes without allocation; return None for malformed shape."""
    if len(value) % 4 or _BASE64_SHAPE.fullmatch(value) is None:
        return None
    padding = len(value) - len(value.rstrip("="))
    return (len(value) // 4) * 3 - padding


def _doc_to_dict(doc: Any) -> dict[str, Any]:
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
        "mime_type": _safe_xds_mime_type(d.get("mime_type")),
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
    user: User = Depends(require_admin),
):
    """Provide and Register Document Set-b [ITI-41 / RAD-68]."""
    import base64
    import binascii
    tenant_id, include_all = tenant_scope(user)

    if (req.content_base64 is None) == (req.content_text is None):
        raise HTTPException(422, "Supply exactly one of content_base64 or content_text")

    mime_type = _safe_xds_mime_type(req.mime_type)
    if mime_type == "application/dicom" and req.content_base64 is None:
        raise HTTPException(422, "DICOM manifests require Base64-encoded DICOM content")
    if req.content_base64 is not None:
        decoded_size = _base64_decoded_size_if_well_formed(req.content_base64)
        if (
            settings.XDS_MAX_DOCUMENT_BYTES
            and decoded_size is not None
            and decoded_size > settings.XDS_MAX_DOCUMENT_BYTES
        ):
            raise HTTPException(413, "XDS document exceeds the configured size limit")
        try:
            content_bytes = base64.b64decode(req.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(400, "content_base64 must contain valid Base64 data") from exc
    else:
        content_bytes = req.content_text.encode("utf-8")

    if not content_bytes:
        raise HTTPException(422, "Document content must not be empty")

    if settings.XDS_MAX_DOCUMENT_BYTES and len(content_bytes) > settings.XDS_MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "XDS document exceeds the configured size limit")

    if mime_type == "application/dicom":
        try:
            await run_blocking(xdsi_service.validate_dicom_manifest, content_bytes, req.study_instance_uid)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    doc = None
    staged = None
    committed = False
    published = False
    try:
        doc, staged = await xdsi_service.register_document(
            db,
            patient_id=req.patient_id,
            document_title=req.document_title,
            content_bytes=content_bytes,
            mime_type=mime_type,
            study_instance_uid=req.study_instance_uid,
            class_code=req.class_code,
            type_code=req.type_code,
            format_code=req.format_code,
            performed_by=user.username,
            tenant_id=tenant_id,
            include_all=include_all,
        )
        await db.commit()
        committed = True
        await asyncio.to_thread(staged.promote)
        published = True
        return JSONResponse(_doc_to_dict(doc), status_code=201)
    except LookupError as exc:
        await db.rollback()
        if staged is not None and staged.staged_path is not None:
            await asyncio.to_thread(staged.discard)
        raise HTTPException(404, "Study not found") from exc
    except Exception as exc:
        await db.rollback()
        if committed and not published and doc is not None:
            try:
                await db.delete(doc)
                await db.commit()
            except Exception as cleanup_exc:  # compensation must not hide the publish error
                await db.rollback()
                log.error(
                    "XDS metadata compensation failed error_type=%s",
                    type(cleanup_exc).__name__,
                )
        if staged is not None and staged.staged_path is not None:
            await asyncio.to_thread(staged.discard)
        log.error("XDS registration failed error_type=%s", type(exc).__name__)
        raise HTTPException(400, "XDS Registration failed") from exc


@router.get("/documents", response_class=JSONResponse)
async def query_documents(
    patient_id: str | None = Query(None, description="Filter by Patient ID"),
    study_instance_uid: str | None = Query(None, description="Filter by StudyInstanceUID"),
    class_code: str | None = Query(None, description="Filter by Class Code"),
    status: str = Query("APPROVED", description="Availability Status (APPROVED / DEPRECATED)"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """FindDocuments [ITI-18] ebXML Registry Query."""
    tenant_id, include_all = tenant_scope(user)
    docs = await xdsi_service.query_documents(
        db, patient_id=patient_id, study_instance_uid=study_instance_uid, class_code=class_code,
        availability_status=status, tenant_id=tenant_id,
        include_all=include_all,
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
        tenant_id, include_all = tenant_scope(user)
        doc, content = await xdsi_service.retrieve_document(
            db, entry_uuid, tenant_id, include_all
        )
        media_type = _safe_xds_mime_type(doc.mime_type)
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": "attachment",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except ValueError as val_err:
        raise HTTPException(404, str(val_err)) from val_err


@router.get("/documents/{entry_uuid}/ebxml", response_class=Response)
async def get_ebxml_metadata(
    entry_uuid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Download ebXML SubmitObjectsRequest metadata XML string."""
    tenant_id, include_all = tenant_scope(user)
    doc = await xdsi_service.get_document_by_uuid(
        db, entry_uuid, tenant_id, include_all
    )
    if not doc:
        raise HTTPException(404, f"XDS Document '{entry_uuid}' not found")

    xml_str = xdsi_service.generate_ebxml_metadata(
        doc.entry_uuid,
        doc.unique_id,
        doc.patient_id,
        doc.document_title,
        _safe_xds_mime_type(doc.mime_type),
        doc.class_code,
        doc.format_code,
        doc.availability_status,
    )
    return Response(content=xml_str, media_type="text/xml")
