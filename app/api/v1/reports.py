"""Radiology reports REST API (Phase E2 — HL7 ORU write-back to the EMR).

Reports are created as DRAFT, edited, then FINALIZED — which locks the report
and (when ``HL7_EMR_ENABLED``) delivers an ORU^R01 to the EMR over MLLP.
Finalizing and re-sending require a destructive role (ADMIN / RADIOLOGIST);
drafting and viewing require any authenticated user.
"""

from __future__ import annotations

import html

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_user,
    get_current_user_any,
    require_destructive_role,
    require_study_access,
)
from app.core.roles import DESTRUCTIVE_ROLES
from app.db.models import RadiologyReport, User
from app.db.session import get_db
from app.services import report_service

log = logging.getLogger("paxs.api.reports")

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


class ReportIn(BaseModel):
    study_instance_uid: str = Field(min_length=1, examples=["1.2.840.113619.2.408.77"])
    clinical_history: str | None = None
    findings: str | None = None
    impression: str | None = None
    radiologist: str | None = None


class ReportPatch(BaseModel):
    clinical_history: str | None = None
    findings: str | None = None
    impression: str | None = None
    radiologist: str | None = None


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
                        user: User = Depends(get_current_user)):
    """Draft a report for a study (one report per study)."""
    try:
        report = await report_service.create_report(
            db, body.model_dump(), performed_by=user.username
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
                       user: User = Depends(get_current_user)):
    if status and status.upper() not in report_service.VALID_STATUSES:
        raise HTTPException(422, f"status must be one of {report_service.VALID_STATUSES}")
    reports = await report_service.list_reports(db, status=status, study_uid=study_uid)
    return [_report_out(r) for r in reports]


@router.get("/{report_id}")
async def get_report(report_id: int, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    report = await report_service.get_report(db, report_id)
    if report is None:
        raise HTTPException(404, "Report not found")
    addenda = await report_service.list_addenda(db, report_id)
    return {**_report_out(report), "addenda": [_addendum_out(a) for a in addenda]}


@router.patch("/{report_id}")
async def update_report(report_id: int, body: ReportPatch,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    report = await report_service.get_report(db, report_id)
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
                          user: User = Depends(require_destructive_role)):
    """Lock the report FINALIZED and deliver ORU^R01 to the EMR (when enabled)."""
    try:
        result = await asyncio.to_thread(
            report_service.finalize_report, report_id, performed_by=user.username
        )
    except Exception as exc:  # keep the MLLP failure contained
        log.warning("finalize report %s raised: %s", report_id, exc)
        raise HTTPException(502, f"Report finalized but delivery failed: {exc}") from exc
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Report not found"))
    return result


@router.post("/{report_id}/resend")
async def resend_report(report_id: int, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_destructive_role)):
    """Re-deliver ORU^R01 for a finalized report whose delivery failed."""
    result = await asyncio.to_thread(report_service.resend_report, report_id)
    if not result.get("ok"):
        message = result.get("error", "Resend failed")
        status = 404 if "not found" in message else 409
        raise HTTPException(status, message)
    return result


# --------------------------------------------------------------------------- #
# Extended Reporting Engine (Study List Integration & HTML Print View)
# --------------------------------------------------------------------------- #
from fastapi.responses import HTMLResponse  # noqa: E402


class SaveReportIn(BaseModel):
    study_instance_uid: str
    clinical_history: str | None = None
    findings: str | None = None
    impression: str | None = None
    radiologist: str | None = None
    status: str = "DRAFT"  # DRAFT or FINALIZED or SIGNED


@router.get("/study/{study_instance_uid}")
async def get_study_report(study_instance_uid: str, db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user_any)):
    """Check if report exists for study UID; if new, returns pre-filled DICOM metadata."""
    # C04: a report carries patient data — resolve it through the tenant guard.
    await require_study_access(db, study_instance_uid, user)
    try:
        data = await report_service.get_study_report_metadata(db, study_instance_uid)
        return data
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc



@router.post("/save")
async def save_report(
    body: SaveReportIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_any),
):
    """Create/edit a DRAFT report, or finalize it (C06).

    This route is the UI's Save/Finalize entry point and now delegates to the
    one supported service contract rather than mutating rows directly:

    * the radiologist identity is always the **authenticated actor** — a
      client-supplied name is discarded;
    * ordinary edits are DRAFT-only; a FINALIZED report is immutable, so
      follow-up text must go through the addendum endpoint;
    * finalizing requires a destructive role and goes through
      ``report_service.finalize_report``, which locks the record and delivers the
      ORU^R01 idempotently (a second finalize never duplicates it in the EMR);
    * an unknown study is a clean 404 rather than a 500.
    """
    study_uid = (body.study_instance_uid or "").strip()
    wants_final = (body.status or "").strip().upper() in ("FINALIZED", "SIGNED")
    actor = user.username

    if wants_final and user.role not in DESTRUCTIVE_ROLES:
        raise HTTPException(
            403, "Finalizing a report requires a radiologist or admin role"
        )

    try:
        meta = await report_service.get_study_report_metadata(db, study_uid)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    existing = meta.get("report")

    fields = {
        "clinical_history": body.clinical_history,
        "findings": body.findings,
        "impression": body.impression,
        # The signed identity is the authenticated actor, never the request body.
        "radiologist": actor,
    }

    if not existing:
        try:
            report = await report_service.create_report(
                db, {"study_instance_uid": study_uid, **fields}, performed_by=actor
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        await db.commit()
        await db.refresh(report)
    else:
        report = await report_service.get_report(db, existing["id"])
        if report is None:
            raise HTTPException(404, "Report not found")
        if not wants_final:
            try:
                report = await report_service.update_report(db, report, fields)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            await db.commit()
            await db.refresh(report)

    if not wants_final:
        return _report_out(report)

    # Finalize through the single supported path (lock + idempotent ORU delivery).
    result = await asyncio.to_thread(
        report_service.finalize_report, report.id, performed_by=actor
    )
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Report not found"))
    await db.refresh(report)
    return {**_report_out(report), "delivery": result.get("delivery")}


class AddendumIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@router.post("/{report_id}/addendum", status_code=201)
async def add_report_addendum(
    report_id: int,
    body: AddendumIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_destructive_role),
):
    """Append an addendum to a FINALIZED report (C06).

    The original clinical fields are never modified — the addendum is stored
    beside them and attributed to the authenticated author.
    """
    report = await report_service.get_report(db, report_id)
    if report is None:
        raise HTTPException(404, "Report not found")
    try:
        addendum = await report_service.add_addendum(
            db, report, body.text, author=user.username
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    await db.refresh(addendum)
    return _addendum_out(addendum)


@router.get("/print/{study_instance_uid}", response_class=HTMLResponse)
async def print_report(study_instance_uid: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user_any)):
    """Generate and render a clean, professional printable HTML report."""
    await require_study_access(db, study_instance_uid, user)
    try:
        data = await report_service.get_study_report_metadata(db, study_instance_uid)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    org = await report_service.get_or_create_org_settings(db)
    rpt = data.get("report") or {}

    # H05: the print page is HTML rendered in a browser, so every value
    # interpolated into it is escaped — clinical text, patient/study metadata
    # and organization branding alike. Nothing reaching the template is treated
    # as markup, which removes the stored-XSS path through report bodies and
    # admin-set org names. (Escaping the branding templates is deliberate: they
    # are no longer a raw-HTML injection point. If rich templates are wanted
    # later they need an explicit allowlist sanitizer, not raw interpolation.)
    esc = html.escape

    header_html = (
        esc(org.header_html_text) if org.header_html_text
        else f"<h2>{esc(org.org_name_en or 'PAXS Archive')}</h2>"
             f"<p>Diagnostic Imaging Department</p>"
    )
    footer_html = (
        esc(org.footer_html_text) if org.footer_html_text
        else f"<p>{esc(org.org_name_en or 'PAXS Archive')} · Electronic Report</p>"
    )
    logo_src = esc(org.logo_path or "/viewer/assets/Paxs-logo.png")

    findings = esc(rpt.get("findings") or "No findings recorded.")
    impression = esc(rpt.get("impression") or "No impression recorded.")
    radiologist = esc(rpt.get("radiologist") or "Authorized Radiologist")
    finalized_date = esc(rpt.get("finalized_at") or rpt.get("created_at") or "")

    # Patient/study metadata is interpolated all over the template; escaping the
    # mapping once covers every `{data[...]}` use.
    data = {
        key: (esc(value) if isinstance(value, str) else value)
        for key, value in data.items()
    }

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Diagnostic Report — {data['patient_name']} ({data['study_instance_uid'][:12]})</title>
    <style>
        body {{
            font-family: 'Segoe UI', Arial, sans-serif;
            color: #1e293b;
            background-color: #ffffff;
            margin: 0;
            padding: 40px;
            font-size: 14px;
            line-height: 1.6;
        }}
        .report-card {{
            max-width: 800px;
            margin: 0 auto;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            padding: 36px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.05);
        }}
        .header-table {{
            width: 100%;
            border-bottom: 2px solid #0284c7;
            padding-bottom: 16px;
            margin-bottom: 24px;
        }}
        .header-logo {{
            max-height: 70px;
            max-width: 180px;
        }}
        .patient-box {{
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            padding: 16px 20px;
            margin-bottom: 24px;
        }}
        .patient-box table {{
            width: 100%;
            border-collapse: collapse;
        }}
        .patient-box td {{
            padding: 4px 8px;
            font-size: 13px;
        }}
        .patient-box td.lbl {{
            font-weight: 700;
            color: #475569;
            width: 18%;
        }}
        .section-title {{
            font-size: 15px;
            font-weight: 700;
            color: #0369a1;
            border-bottom: 1.5px solid #e2e8f0;
            padding-bottom: 4px;
            margin-top: 24px;
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}
        .report-text {{
            white-space: pre-wrap;
            font-size: 14px;
            color: #0f172a;
            margin-bottom: 16px;
            background: #ffffff;
        }}
        .impression-box {{
            background: #f0f9ff;
            border-left: 4px solid #0284c7;
            padding: 12px 18px;
            border-radius: 4px;
            margin-top: 16px;
        }}
        .signature-block {{
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px dashed #cbd5e1;
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
        }}
        .signature-title {{
            font-weight: 700;
            color: #0f172a;
        }}
        .footer-text {{
            margin-top: 30px;
            padding-top: 16px;
            border-top: 1px solid #e2e8f0;
            font-size: 11px;
            color: #64748b;
            text-align: center;
        }}
        .no-print {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: #0284c7;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 6px;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        }}
        @media print {{
            .no-print {{ display: none; }}
            body {{ padding: 0; }}
            .report-card {{ border: none; shadow: none; padding: 0; }}
        }}
    </style>
</head>
<body>
    <button class="no-print" onclick="window.print()">🖨 Print Diagnostic Report</button>
    <div class="report-card">
        <table class="header-table">
            <tr>
                <td style="width: 20%; text-align: left;">
                    <img src="{logo_src}" class="header-logo" alt="Logo">
                </td>
                <td style="text-align: right;">
                    {header_html}
                </td>
            </tr>
        </table>

        <div class="patient-box">
            <table>
                <tr>
                    <td class="lbl">Patient Name:</td>
                    <td><b>{data['patient_name']}</b></td>
                    <td class="lbl">Patient ID:</td>
                    <td><code>{data['patient_id']}</code></td>
                </tr>
                <tr>
                    <td class="lbl">Age / Gender:</td>
                    <td>{data['patient_age']} / {data['patient_sex']}</td>
                    <td class="lbl">Accession #:</td>
                    <td><b>{data['accession_number']}</b></td>
                </tr>
                <tr>
                    <td class="lbl">Modality:</td>
                    <td><b>{data['modality']}</b></td>
                    <td class="lbl">Study Date:</td>
                    <td>{data['study_date']}</td>
                </tr>
                <tr>
                    <td class="lbl">Description:</td>
                    <td colspan="3">{data['study_description']}</td>
                </tr>
            </table>
        </div>

        <div class="section-title">Diagnostic Findings</div>
        <div class="report-text">{findings}</div>

        <div class="section-title">Impression & Conclusion</div>
        <div class="impression-box">
            <div class="report-text" style="margin: 0; font-weight: 600;">{impression}</div>
        </div>

        <div class="signature-block">
            <div>
                <div style="font-size: 11px; color: #64748b;">Report Status:</div>
                <div style="font-weight: 700; color: #16a34a; text-transform: uppercase;">{rpt.get('status', 'FINALIZED')}</div>
            </div>
            <div style="text-align: right;">
                <div class="signature-title">{radiologist}</div>
                <div style="font-size: 12px; color: #64748b;">Consultant Radiologist</div>
                <div style="font-size: 11px; color: #94a3b8;">{finalized_date}</div>
            </div>
        </div>

        <div class="footer-text">
            {footer_html}
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html_content)

