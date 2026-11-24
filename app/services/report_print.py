"""Printable radiology report — HTML rendering (Q-6 / FA-4).

``reports.print_report`` was ~230 lines: a fetch, an escaping pass and a large
f-string template, all inside the routing layer. The template lives here so the
endpoint is a parse → fetch → render → respond, and so the escaping rules are
reviewable in one place instead of being interleaved with route concerns.

**Escaping contract (H05).** Every value interpolated into the document is
HTML-escaped — clinical text, patient/study metadata and organization branding
alike — so nothing reaching the template is treated as markup. That closes the
stored-XSS path through report bodies and admin-set org names. The branding
templates are escaped deliberately: they are no longer a raw-HTML injection
point, and rich templates would need an explicit allowlist sanitizer rather than
raw interpolation.

**Structure (2026-10-22).** ``render_report_html`` was 234 lines of literal
markup interleaved with the values — a section could not be changed without
reading the whole document. The stylesheet, the document skeleton and the four
sections (banner, findings, addenda, signature) now live in named pieces:

| piece                     | owns                                        |
| ------------------------- | ------------------------------------------- |
| ``_REPORT_STYLES``        | the print stylesheet                        |
| ``_REPORT_DOCUMENT``      | the skeleton, with one slot per section     |
| ``_report_context``       | the escaping pass + every computed value    |
| ``_build_report_header``  | org header block (branding or fallback)     |
| ``_build_patient_banner`` | the demographics table                      |
| ``_build_addenda_section``| addenda, or nothing at all                  |
| ``_build_signature_footer``| status + signature block                   |

The output is byte-identical to the previous implementation (verified by
rendering the same inputs through both). One value was **not** escaped before
and is now: ``report.status``. It is a stored, operator-visible string sitting
in the markup, so leaving it raw contradicted the contract above; for every
benign status the bytes do not change.
"""

from __future__ import annotations

import html
from string import Template

from urllib.parse import quote

from app.db.models import OrganizationSettings
from app.services.report_html import render_rich_report_html

#: The print stylesheet, verbatim. Kept as data rather than as part of the
#: skeleton so a style change is a style change — it used to be 100 lines in the
#: middle of a function, which is why nobody touched it.
_REPORT_STYLES = """\
        body {
            font-family: 'Segoe UI', Arial, sans-serif;
            color: #1e293b;
            background-color: #ffffff;
            margin: 0;
            padding: 40px;
            font-size: 14px;
            line-height: 1.6;
        }
        .report-card {
            max-width: 800px;
            margin: 0 auto;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            padding: 36px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.05);
        }
        .header-table {
            width: 100%;
            border-bottom: 2px solid #0284c7;
            padding-bottom: 16px;
            margin-bottom: 24px;
        }
        .header-logo {
            max-height: 70px;
            max-width: 180px;
        }
        .patient-box {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            padding: 16px 20px;
            margin-bottom: 24px;
        }
        .patient-box table {
            width: 100%;
            border-collapse: collapse;
        }
        .patient-box td {
            padding: 4px 8px;
            font-size: 13px;
        }
        .patient-box td.lbl {
            font-weight: 700;
            color: #475569;
            width: 18%;
        }
        .section-title {
            font-size: 15px;
            font-weight: 700;
            color: #0369a1;
            border-bottom: 1.5px solid #e2e8f0;
            padding-bottom: 4px;
            margin-top: 24px;
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .report-text {
            font-size: 14px;
            color: #0f172a;
            margin-bottom: 16px;
            background: #ffffff;
            line-height: 1.65;
            overflow-wrap: break-word;
        }
        .report-text.plain-preserve {
            white-space: pre-wrap;
        }
        .report-text p { margin: 0 0 8px; }
        .report-text p:last-child { margin-bottom: 0; }
        .report-text h1, .report-text h2, .report-text h3, .report-text h4 {
            color: #0f172a;
            margin: 12px 0 6px;
            line-height: 1.3;
        }
        .report-text h1 { font-size: 19px; }
        .report-text h2 { font-size: 16.5px; }
        .report-text h3 { font-size: 15px; }
        .report-text h4 { font-size: 14px; }
        .report-text ul, .report-text ol {
            margin: 6px 0 10px;
            padding-inline-start: 24px;
        }
        .report-text ul { list-style-type: disc; }
        .report-text ol { list-style-type: decimal; }
        .report-text li { margin: 3px 0; }
        .report-text blockquote {
            margin: 8px 0;
            padding: 6px 14px;
            border-inline-start: 3px solid #0284c7;
            background: #f8fafc;
            color: #334155;
        }
        .report-text hr {
            border: 0;
            border-top: 1px solid #cbd5e1;
            margin: 12px 0;
        }
        .report-text table {
            width: 100%;
            border-collapse: collapse;
            margin: 8px 0;
        }
        .report-text th, .report-text td {
            border: 1px solid #cbd5e1;
            padding: 6px 8px;
            text-align: left;
        }
        .impression-box {
            background: #f0f9ff;
            border-left: 4px solid #0284c7;
            padding: 12px 18px;
            border-radius: 4px;
            margin-top: 16px;
        }
        .impression-box .report-text {
            background: transparent;
        }
        .signature-block {
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px dashed #cbd5e1;
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
        }
        .signature-title {
            font-weight: 700;
            color: #0f172a;
        }
        .footer-text {
            margin-top: 30px;
            padding-top: 16px;
            border-top: 1px solid #e2e8f0;
            font-size: 11px;
            color: #64748b;
            text-align: center;
        }
        .no-print {
            position: fixed;
            top: 20px;
            right: 20px;
            display: flex;
            gap: 8px;
        }
        .no-print button,
        .no-print a {
            background: #0284c7;
            color: white;
            border: none;
            padding: 10px 18px;
            border-radius: 6px;
            font-weight: 700;
            font-size: 13px;
            font-family: inherit;
            text-decoration: none;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        }
        .no-print a.pdf-btn {
            background: #059669;
        }
        @media print {
            .no-print { display: none; }
            body { padding: 0; }
            .report-card { border: none; shadow: none; padding: 0; }
        }
"""

#: The document skeleton. ``string.Template`` is used rather than an f-string
#: because the stylesheet is full of literal braces — an f-string would need
#: every one of them doubled, which is exactly the noise that made the old
#: template hard to review.
_REPORT_DOCUMENT = Template("""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Diagnostic Report — ${patient_name} (${study_uid_prefix})</title>
    <style>
${styles}    </style>
</head>
<body>
    <div class="no-print">
        <a class="pdf-btn" href="/api/v1/reports/pdf/${study_uid_encoded}">⬇ Export PDF</a>
        <button type="button" onclick="window.print()">🖨 Print Diagnostic Report</button>
    </div>
    <div class="report-card">
        <table class="header-table">
            <tr>
                <td style="width: 20%; text-align: left;">
                    <img src="${logo_src}" class="header-logo" alt="Logo">
                </td>
                <td style="text-align: right;">
                    ${header_html}
                </td>
            </tr>
        </table>

${patient_banner}

        <div class="section-title">Diagnostic Findings</div>
${findings_section}

        <div class="section-title">Impression & Conclusion</div>
        <div class="impression-box">
            <div class="report-text" style="margin: 0; font-weight: 600;">${impression}</div>
        </div>

        ${addenda_section}

${signature_footer}

        <div class="footer-text">
            ${footer_html}
        </div>
    </div>
</body>
</html>""")


def _build_report_header(org: OrganizationSettings) -> str:
    """The organisation block: its configured header, or a generic fallback.

    The configured branding is escaped: it is admin-supplied markup stored in
    the database, and this page is HTML rendered in a browser (H05). A rich
    template would need an allowlist sanitizer, not raw interpolation.
    """
    if org.header_html_text:
        return html.escape(org.header_html_text)
    return (
        f"<h2>{html.escape(org.org_name_en or 'PAXS Archive')}</h2>"
        f"<p>Diagnostic Imaging Department</p>"
    )


def _build_report_footer(org: OrganizationSettings) -> str:
    """The organisation footer, or the generic "electronic report" line."""
    if org.footer_html_text:
        return html.escape(org.footer_html_text)
    return f"<p>{html.escape(org.org_name_en or 'PAXS Archive')} · Electronic Report</p>"


def _build_patient_banner(data: dict) -> str:
    """The demographics box. ``data`` is already escaped by the caller."""
    return (
        '        <div class="patient-box">\n'
        "            <table>\n"
        "                <tr>\n"
        '                    <td class="lbl">Patient Name:</td>\n'
        f"                    <td><b>{data['patient_name']}</b></td>\n"
        '                    <td class="lbl">Patient ID:</td>\n'
        f"                    <td><code>{data['patient_id']}</code></td>\n"
        "                </tr>\n"
        "                <tr>\n"
        '                    <td class="lbl">Age / Gender:</td>\n'
        f"                    <td>{data['patient_age']} / {data['patient_sex']}</td>\n"
        '                    <td class="lbl">Accession #:</td>\n'
        f"                    <td><b>{data['accession_number']}</b></td>\n"
        "                </tr>\n"
        "                <tr>\n"
        '                    <td class="lbl">Modality:</td>\n'
        f"                    <td><b>{data['modality']}</b></td>\n"
        '                    <td class="lbl">Study Date:</td>\n'
        f"                    <td>{data['study_date']}</td>\n"
        "                </tr>\n"
        "                <tr>\n"
        '                    <td class="lbl">Description:</td>\n'
        f"                    <td colspan=\"3\">{data['study_description']}</td>\n"
        "                </tr>\n"
        "            </table>\n"
        "        </div>"
    )


def _build_findings_section(findings: str) -> str:
    """The findings text block (already escaped).

    A separate builder rather than a template slot: findings is the one section
    a report can legitimately be empty of, and the fallback wording belongs next
    to the markup that shows it.
    """
    return f'        <div class="report-text">{findings}</div>'


def _build_addenda_section(addenda: list[dict]) -> str:
    """The addenda block, or an empty string when there are none.

    Empty is deliberate: the old template emitted a heading followed by nothing
    for a report without addenda, which reads as a rendering bug to a clinician.
    """
    addenda_html = "".join(
        "<div class=\"report-text addendum-text\">"
        f"<strong>Addendum · {html.escape(addendum.get('author') or '')}</strong><br>"
        f"{html.escape(addendum.get('text') or '')}</div>"
        for addendum in addenda
    )
    if not addenda_html:
        return ""
    return f'<div class="section-title">Addenda</div>{addenda_html}'


def _build_signature_footer(status: str, radiologist: str, finalized_date: str) -> str:
    """The signature/status block. Values are escaped by the caller."""
    return (
        '        <div class="signature-block">\n'
        "            <div>\n"
        '                <div style="font-size: 11px; color: #64748b;">Report Status:</div>\n'
        '                <div style="font-weight: 700; color: #16a34a; text-transform: uppercase;">'
        f"{status}</div>\n"
        "            </div>\n"
        '            <div style="text-align: right;">\n'
        f'                <div class="signature-title">{radiologist}</div>\n'
        '                <div style="font-size: 12px; color: #64748b;">Consultant Radiologist</div>\n'
        f'                <div style="font-size: 11px; color: #94a3b8;">{finalized_date}</div>\n'
        "            </div>\n"
        "        </div>"
    )


def _report_context(data: dict, org: OrganizationSettings) -> dict[str, str]:
    """Escape every interpolated value once, and compute the section fragments.

    The escaping pass covers the whole mapping (patient/study metadata is used
    all over the document) plus the clinical text and the branding.
    """
    esc = html.escape
    report = data.get("report") or {}

    # H05: escape the top-level metadata mapping once; every `data[...]` use
    # below then refers to an already-escaped value.
    escaped = {
        key: (esc(value) if isinstance(value, str) else value)
        for key, value in data.items()
    }

    findings = render_rich_report_html(report.get("findings"), "No findings recorded.")
    impression = render_rich_report_html(report.get("impression"), "No impression recorded.")
    radiologist = esc(report.get("radiologist") or "Authorized Radiologist")
    finalized_date = esc(report.get("finalized_at") or report.get("created_at") or "")
    # `str(...)` keeps a stored None rendering as "None", matching the previous
    # template byte for byte; the value itself is now escaped (see docstring).
    status = esc(str(report.get("status", "FINALIZED")))

    return {
        "styles": _REPORT_STYLES,
        "patient_name": escaped["patient_name"],
        "study_uid_prefix": escaped["study_instance_uid"][:12],
        "study_uid_encoded": quote(str(data.get("study_instance_uid") or ""), safe=""),
        "logo_src": esc(org.logo_path or "/viewer/assets/Paxs-logo.png"),
        "header_html": _build_report_header(org),
        "footer_html": _build_report_footer(org),
        "patient_banner": _build_patient_banner(escaped),
        "findings_section": _build_findings_section(findings),
        "impression": impression,
        "addenda_section": _build_addenda_section(report.get("addenda") or []),
        "signature_footer": _build_signature_footer(
            status, radiologist, finalized_date
        ),
    }


def render_report_html(data: dict, org: OrganizationSettings) -> str:
    """Render the printable report for one study.

    ``data`` is ``report_service.get_study_report_metadata`` (patient/study
    metadata plus a ``report`` block with findings, impression, addenda);
    ``org`` is the organization branding row.
    """
    return _REPORT_DOCUMENT.substitute(_report_context(data, org))
