"""Organization Settings & Report Templates REST API.

Provides endpoints for updating institutional branding (logo, header/footer HTML,
contact info) and managing report templates for Jinja2 pre-filling.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_optional_user
from app.db.models import User
from app.db.session import get_db
from app.services import report_service

log = logging.getLogger("paxs.api.settings")

router = APIRouter(tags=["settings"])

UPLOADS_DIR = Path(__file__).parent.parent.parent / "static" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


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
async def get_org_settings(db: AsyncSession = Depends(get_db)):
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
    user: User | None = Depends(get_optional_user),
):
    """Update organization settings and handle logo file upload."""
    logo_path = None
    if logo_file and logo_file.filename:
        filename = f"logo_{Path(logo_file.filename).name}"
        save_path = UPLOADS_DIR / filename
        content = await logo_file.read()
        with open(save_path, "wb") as f:
            f.write(content)
        logo_path = f"/static/uploads/{filename}"

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
async def list_templates(modality: str | None = None, db: AsyncSession = Depends(get_db)):
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
    user: User | None = Depends(get_optional_user),
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
async def get_template(template_id: int, db: AsyncSession = Depends(get_db)):
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
    user: User | None = Depends(get_optional_user),
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
    user: User | None = Depends(get_optional_user),
):
    deleted = await report_service.delete_report_template(db, template_id)
    if not deleted:
        raise HTTPException(404, "Template not found")
    await db.commit()
    return {"ok": True, "message": "Template deleted"}
