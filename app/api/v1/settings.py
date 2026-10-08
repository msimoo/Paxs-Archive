"""Organization Settings & Report Templates REST API.

Provides endpoints for updating institutional branding (logo, header/footer HTML,
contact info) and managing report templates for Jinja2 pre-filling.
"""

from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user_any, require_admin
from app.db.models import User
from app.db.session import get_db
from PIL import Image

from app.services import report_service

log = logging.getLogger("paxs.api.settings")

router = APIRouter(tags=["settings"])

UPLOADS_DIR = Path(__file__).parent.parent.parent / "static" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# H05: the logo is served from the static tree, so it is a security boundary.
MAX_LOGO_BYTES = 2 * 1024 * 1024
_LOGO_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


def _save_validated_logo(content: bytes) -> str:
    """Validate and persist an uploaded logo; returns its served path.

    Only real raster images are accepted: the bytes are decoded with Pillow and
    the stored extension comes from the *decoded* format, never from the client's
    filename. SVG and HTML are refused (they are active content), as are
    oversized or undecodable/polyglot payloads. Responses already carry
    ``X-Content-Type-Options: nosniff`` globally, so the served file cannot be
    reinterpreted as markup.
    """
    if not content:
        raise HTTPException(422, "Logo file is empty")
    if len(content) > MAX_LOGO_BYTES:
        raise HTTPException(
            413, f"Logo exceeds the {MAX_LOGO_BYTES // (1024 * 1024)} MB limit"
        )
    try:
        with Image.open(io.BytesIO(content)) as probe:
            probe.verify()  # rejects truncated, corrupt or polyglot data
            fmt = (probe.format or "").upper()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad upload
        raise HTTPException(
            422, "Logo must be a valid PNG, JPEG or WebP image"
        ) from exc

    if fmt not in _LOGO_FORMATS:
        raise HTTPException(
            422,
            "Logo must be a valid PNG, JPEG or WebP image "
            "(SVG and HTML are not accepted)",
        )

    filename = f"logo_{uuid.uuid4().hex}{_LOGO_FORMATS[fmt]}"
    (UPLOADS_DIR / filename).write_bytes(content)
    log.info("branding logo stored as %s (%s, %d bytes)", filename, fmt, len(content))
    return f"/static/uploads/{filename}"


class TemplateIn(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    modality: str | None = None
    body_findings_template: str | None = None
    default_impression_template: str | None = None
    is_default: bool = False


class TemplatePatch(BaseModel):
    title: str | None = None
    modality: str | None = None
    body_findings_template: str | None = None
    default_impression_template: str | None = None
    is_default: bool | None = None


# --------------------------------------------------------------------------- #
# Organization Settings API
# --------------------------------------------------------------------------- #
@router.get("/api/v1/settings/organization")
async def get_org_settings(db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user_any)):
    """Fetch current organization settings."""
    org = await report_service.get_or_create_org_settings(db)
    updated_at_str = None
    try:
        if org.updated_at:
            updated_at_str = org.updated_at.isoformat()
    except Exception:
        pass
    return {
        "id": org.id,
        "org_name_ar": org.org_name_ar or "",
        "org_name_en": org.org_name_en or "",
        "logo_path": org.logo_path or "",
        "header_html_text": org.header_html_text or "",
        "footer_html_text": org.footer_html_text or "",
        "phone": org.phone or "",
        "email": org.email or "",
        "address": org.address or "",
        "updated_at": updated_at_str,
    }


@router.post("/api/v1/settings/organization")
async def update_org_settings(
    org_name_ar: str = Form(None),
    org_name_en: str = Form(None),
    header_html_text: str = Form(None),
    footer_html_text: str = Form(None),
    phone: str = Form(None),
    email: str = Form(None),
    address: str = Form(None),
    logo_file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Update organization settings and handle logo file upload."""
    logo_path = None
    if logo_file and logo_file.filename:
        logo_path = _save_validated_logo(await logo_file.read())

    data = {
        "org_name_ar": org_name_ar,
        "org_name_en": org_name_en,
        "header_html_text": header_html_text,
        "footer_html_text": footer_html_text,
        "phone": phone,
        "email": email,
        "address": address,
    }
    org = await report_service.update_org_settings(db, data, logo_path=logo_path)
    await db.commit()
    await db.refresh(org)
    return {
        "ok": True,
        "message": "Organization settings updated successfully",
        "logo_path": org.logo_path,
    }


# --------------------------------------------------------------------------- #
# Report Templates CRUD API
# --------------------------------------------------------------------------- #
@router.get("/api/v1/templates")
async def list_templates(modality: str | None = None, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user_any)):
    """List report templates, optionally filtered by modality."""
    tmpls = await report_service.list_report_templates(db, modality=modality)
    return [
        {
            "id": t.id,
            "title": t.title,
            "modality": t.modality,
            "body_findings_template": t.body_findings_template,
            "default_impression_template": t.default_impression_template,
            "is_default": t.is_default,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }
        for t in tmpls
    ]


@router.post("/api/v1/templates", status_code=201)
async def create_template(
    body: TemplateIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Create a new report template."""
    tmpl = await report_service.create_report_template(db, body.model_dump())
    await db.commit()
    await db.refresh(tmpl)
    return {
        "id": tmpl.id,
        "title": tmpl.title,
        "modality": tmpl.modality,
        "body_findings_template": tmpl.body_findings_template,
        "default_impression_template": tmpl.default_impression_template,
        "is_default": tmpl.is_default,
    }


@router.get("/api/v1/templates/{template_id}")
async def get_template(template_id: int, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user_any)):
    tmpl = await report_service.get_report_template(db, template_id)
    if tmpl is None:
        raise HTTPException(404, "Template not found")
    return {
        "id": tmpl.id,
        "title": tmpl.title,
        "modality": tmpl.modality,
        "body_findings_template": tmpl.body_findings_template,
        "default_impression_template": tmpl.default_impression_template,
        "is_default": tmpl.is_default,
    }


@router.patch("/api/v1/templates/{template_id}")
async def update_template(
    template_id: int,
    body: TemplatePatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    tmpl = await report_service.update_report_template(
        db, template_id, body.model_dump(exclude_none=True)
    )
    if tmpl is None:
        raise HTTPException(404, "Template not found")
    await db.commit()
    await db.refresh(tmpl)
    return {
        "id": tmpl.id,
        "title": tmpl.title,
        "modality": tmpl.modality,
        "body_findings_template": tmpl.body_findings_template,
        "default_impression_template": tmpl.default_impression_template,
        "is_default": tmpl.is_default,
    }


@router.delete("/api/v1/templates/{template_id}")
async def delete_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    deleted = await report_service.delete_report_template(db, template_id)
    if not deleted:
        raise HTTPException(404, "Template not found")
    await db.commit()
    return {"ok": True, "message": "Template deleted"}
