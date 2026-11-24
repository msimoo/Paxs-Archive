"""Radiology reports REST API (Phase E2 — HL7 ORU write-back to the EMR).

Reports are created as DRAFT, edited, then FINALIZED — which locks the report
and (when ``HL7_EMR_ENABLED``) delivers an ORU^R01 to the EMR over MLLP.
Finalizing and re-sending require a destructive role (ADMIN / RADIOLOGIST);
drafting and viewing require any authenticated user.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_user,
    get_current_user_any,
    require_destructive_role,
    require_study_access,
    tenant_scope,
)
from app.db.models import RadiologyReport, User
from app.db.session import get_db
from app.services import report_pdf, report_print, report_service

log = logging.getLogger("paxs.api.reports")

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


class ReportIn(BaseModel):
    study_instance_uid: str = Field(min_length=1, examples=["1.2.840.113619.2.408.77"])
    clinical_history: str | None = None
    findings: str | None = None
    impression: str | None = None


class ReportPatch(BaseModel):
    clinical_history: str | None = None
    findings: str | None = None
    impression: str | None = None


def _report_out(r: RadiologyReport) -> dict:
    return {
        "id": r.id,
        "study_instance_uid": r.study_instance_uid,
        "patient_id": r.patient_id,
        "status": r.status,
        "clinical_history": r.clinical_history,
        "findings": r.findings,
        "impression": r.impression,
        "radiologist": r.radiologist,
        "finalized_at": r.finalized_at.isoformat() if r.finalized_at else None,
        "oru_status": r.oru_status,
        "oru_sent_at": r.oru_sent_at.isoformat() if r.oru_sent_at else None,
        "oru_error": r.oru_error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _addendum_out(a) -> dict:
    return {
        "id": a.id,
        "report_id": a.report_id,
        "author": a.author,
        "text": a.text,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.post("", status_code=201)
async def create_report(body: ReportIn, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user_any)) -> dict:
    """Draft a report for a study (one report per study)."""
    tenant_id, include_all = tenant_scope(user)
    try:
        report = await report_service.create_report(
            db, body.model_dump(), performed_by=user.username,
            tenant_id=tenant_id, include_all=include_all,
        )
    except ValueError as exc:
        message = str(exc)
        if "Study not found" in message:
            raise HTTPException(404, message) from exc
        raise HTTPException(409, message) from exc
    await db.commit()
    await db.refresh(report)
    return _report_out(report)


@router.get("")
async def list_reports(status: str | None = None, study_uid: str | None = None,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)) -> list[dict]:
    if status and status.upper() not in report_service.VALID_STATUSES:
        raise HTTPException(422, f"status must be one of {report_service.VALID_STATUSES}")
    tenant_id, include_all = tenant_scope(user)
    reports = await report_service.list_reports(
        db, status=status, study_uid=study_uid,
        tenant_id=tenant_id, include_all=include_all,
    )
    return [_report_out(r) for r in reports]


@router.get("/{report_id}")
async def get_report(report_id: int, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)) -> dict:
    tenant_id, include_all = tenant_scope(user)
    report = await report_service.get_report(
        db, report_id, tenant_id=tenant_id, include_all=include_all
    )
    if report is None:
        raise HTTPException(404, "Report not found")
    addenda = await report_service.list_addenda(
        db, report_id, tenant_id=tenant_id, include_all=include_all
    )
    return {**_report_out(report), "addenda": [_addendum_out(a) for a in addenda]}


@router.patch("/{report_id}")
async def update_report(report_id: int, body: ReportPatch,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user_any)) -> dict:
    tenant_id, include_all = tenant_scope(user)
    report = await report_service.get_report(
        db, report_id, tenant_id=tenant_id, include_all=include_all
    )
    if report is None:
        raise HTTPException(404, "Report not found")
    try:
        report = await report_service.update_report(
            db, report, body.model_dump(exclude_none=True)
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    await db.refresh(report)
    return _report_out(report)


@router.post("/{report_id}/finalize")
async def finalize_report(report_id: int, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_destructive_role)) -> dict:
    """Lock the report FINALIZED and deliver ORU^R01 to the EMR (when enabled)."""
    tenant_id, include_all = tenant_scope(user)
    report = await report_service.get_report(
        db, report_id, tenant_id=tenant_id, include_all=include_all
    )
    if report is None:
        raise HTTPException(404, "Report not found")
    await db.rollback()
    try:
        result = await asyncio.to_thread(
            report_service.finalize_report, report_id, performed_by=user.username,
            tenant_id=tenant_id, include_all=include_all,
        )
    except Exception as exc:  # keep the MLLP failure contained
        log.warning("report_finalize_failed error_type=%s", type(exc).__name__)
        raise HTTPException(502, "Report finalized but delivery failed") from None
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Report not found"))
    return result


@router.post("/{report_id}/resend")
async def resend_report(report_id: int, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_destructive_role)) -> dict:
    """Re-deliver ORU^R01 for a finalized report whose delivery failed."""
    tenant_id, include_all = tenant_scope(user)
    report = await report_service.get_report(
        db, report_id, tenant_id=tenant_id, include_all=include_all
    )
    if report is None:
        raise HTTPException(404, "Report not found")
    await db.rollback()
    result = await asyncio.to_thread(
        report_service.resend_report, report_id,
        tenant_id=tenant_id, include_all=include_all,
    )
    if not result.get("ok"):
        message = result.get("error", "Resend failed")
        status = 404 if "not found" in message else 409
        raise HTTPException(status, message)
    return result


# --------------------------------------------------------------------------- #
# Extended Reporting Engine (Study List Integration & HTML Print View)
# --------------------------------------------------------------------------- #


class SaveReportIn(BaseModel):
    study_instance_uid: str
    clinical_history: str | None = None
    findings: str | None = None
    impression: str | None = None
    status: str = "DRAFT"  # DRAFT or FINALIZED or SIGNED


@router.get("/study/{study_instance_uid}")
async def get_study_report(study_instance_uid: str, db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user_any)) -> dict:
    """Check if report exists for study UID; if new, returns pre-filled DICOM metadata."""
    # C04: a report carries patient data — resolve it through the tenant guard.
    await require_study_access(db, study_instance_uid, user)
    try:
        tenant_id, include_all = tenant_scope(user)
        data = await report_service.get_study_report_metadata(
            db, study_instance_uid, tenant_id=tenant_id, include_all=include_all
        )
        return data
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc



@router.post("/save")
async def save_report(
    body: SaveReportIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_any),
) -> dict:
    """Create/edit a DRAFT report, or finalize it (C06, A-7).

    The whole decision lives in ``report_service.save_or_finalize_report``: the
    radiologist identity is always the authenticated actor (a client-supplied
    name is discarded), ordinary edits are DRAFT-only, and finalizing applies
    the submitted edits **and** the signature in a single transaction so the
    signed text is the text that was just written. An unknown study is a 404,
    a content change to a FINALIZED report is a 409.
    """
    try:
        tenant_id, include_all = tenant_scope(user)
        report, delivery = await report_service.save_or_finalize_report(
            db,
            actor=user.username,
            role=user.role,
            study_uid=(body.study_instance_uid or "").strip(),
            edits={
                "clinical_history": body.clinical_history,
                "findings": body.findings,
                "impression": body.impression,
            },
            wants_final=(body.status or "").strip().upper() in ("FINALIZED", "SIGNED"),
            tenant_id=tenant_id,
            include_all=include_all,
        )
    except report_service.ReportForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except report_service.ReportNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except report_service.ReportConflict as exc:
        raise HTTPException(409, str(exc)) from exc

    payload = _report_out(report)
    if delivery is not None:
        payload["delivery"] = delivery
    return payload


class AddendumIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@router.post("/{report_id}/addendum", status_code=201)
async def add_report_addendum(
    report_id: int,
    body: AddendumIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
) -> dict:
    """Append an addendum to a FINALIZED report (C06).

    The original clinical fields are never modified — the addendum is stored
    beside them and attributed to the authenticated author.
    """
    tenant_id, include_all = tenant_scope(user)
    report = await report_service.get_report(
        db, report_id, tenant_id=tenant_id, include_all=include_all
    )
    if report is None:
        raise HTTPException(404, "Report not found")
    await require_study_access(db, report.study_instance_uid, user)
    # The authorization reads above leave a SQLite read transaction open. The
    # service owns the write transaction in its sync worker, so release that
    # snapshot before dispatch rather than holding a competing connection lock.
    await db.rollback()
    try:
        result = await asyncio.to_thread(
            report_service.add_report_addendum,
            report_id,
            body.text,
            user.username,
            tenant_id=tenant_id,
            include_all=include_all,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Report not found"))
    return result["addendum"]


@router.get("/print/{study_instance_uid}", response_class=HTMLResponse)
async def print_report(study_instance_uid: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user_any)) -> HTMLResponse:
    """Generate and render a clean, professional printable HTML report."""
    await require_study_access(db, study_instance_uid, user)
    try:
        tenant_id, include_all = tenant_scope(user)
        data = await report_service.get_study_report_metadata(
            db, study_instance_uid, tenant_id=tenant_id, include_all=include_all
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    org = await report_service.get_or_create_org_settings(db)
    return HTMLResponse(content=report_print.render_report_html(data, org))


def _safe_pdf_filename(data: dict, study_uid: str) -> str:
    """Build an ASCII-safe Content-Disposition PDF filename without PHI."""
    raw = (data.get("accession_number") or study_uid[:24] or "report").strip()
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)[:48] or "report"
    return f"PAXS_Report_{safe}.pdf"


@router.get(
    "/pdf/{study_instance_uid}",
    responses={200: {"content": {"application/pdf": {}}}},
)
async def export_report_pdf(
    study_instance_uid: str,
    inline: bool = Query(False, description="Render inline in browser instead of downloading"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_any),
) -> Response:
    """Export the diagnostic radiology report for a study as a PDF file."""
    await require_study_access(db, study_instance_uid, user)
    try:
        tenant_id, include_all = tenant_scope(user)
        data = await report_service.get_study_report_metadata(
            db, study_instance_uid, tenant_id=tenant_id, include_all=include_all
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    org = await report_service.get_or_create_org_settings(db)
    pdf_bytes = await asyncio.to_thread(report_pdf.render_report_pdf, data, org)
    filename = _safe_pdf_filename(data, study_instance_uid)
    disposition = "inline" if inline else "attachment"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )

