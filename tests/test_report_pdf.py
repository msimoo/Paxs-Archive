"""Tests for server-side diagnostic report PDF export."""

from types import SimpleNamespace

from app.api.v1.reports import _safe_pdf_filename
from app.services.report_pdf import render_report_pdf


def _sample_data(findings: str, impression: str, addenda: list | None = None) -> dict:
    return {
        "study_instance_uid": "1.2.840.10008.5.1.4.123456",
        "patient_name": "Doe^Jane",
        "patient_id": "MRN-10042",
        "patient_age": "45Y",
        "patient_sex": "F",
        "accession_number": "ACC-2026-009",
        "modality": "CT",
        "study_date": "20261123",
        "study_description": "CT Chest With IV Contrast",
        "report": {
            "status": "FINALIZED",
            "radiologist": "Dr. A. Radiologist",
            "finalized_at": "2026-11-23T14:30:00",
            "findings": findings,
            "impression": impression,
            "addenda": addenda or [],
        },
    }


def _sample_org() -> SimpleNamespace:
    return SimpleNamespace(
        org_name_en="PAXS Medical Center",
        logo_path=None,
        phone="+1 800 555 0199",
        email="radiology@paxs.local",
        header_html_text=None,
        footer_html_text="PAXS Medical Center · Electronic Report",
    )


def test_render_report_pdf_produces_valid_pdf_with_rich_html():
    data = _sample_data(
        findings=(
            "<h2>Chest CT Findings</h2>"
            "<p><b>Lungs:</b> Clear bilaterally. No effusion or pneumothorax.</p>"
            "<ol><li>Normal cardiomediastinal silhouette.</li><li>Intact osseous structures.</li></ol>"
            "<ul><li>No suspicious pulmonary nodule.</li></ul>"
            "<blockquote>Comparison made with prior study from 2025.</blockquote>"
            "<hr><script>alert('xss')</script>"
        ),
        impression="<p><strong>Unremarkable</strong> contrast-enhanced CT chest.</p>",
        addenda=[
            {
                "id": 1,
                "author": "dr.smith",
                "text": "Reviewed with referring physician.",
                "created_at": "2026-11-23T15:00:00",
            }
        ],
    )
    pdf = render_report_pdf(data, _sample_org())

    assert pdf.startswith(b"%PDF-1.4\n")
    assert b"xref\n" in pdf
    assert b"%%EOF\n" in pdf
    assert b"/Type /Catalog" in pdf
    assert b"/Type /Page" in pdf
    assert b"/Producer (PAXS Archive Report PDF Engine)" in pdf
    assert b"alert('xss')" not in pdf
    assert len(pdf) > 1200


def test_render_report_pdf_paginates_long_reports_cleanly():
    long_findings = "\n".join(
        f"- Segment {i}: Normal parenchymal attenuation without focal mass, hemorrhage, or acute ischemia."
        for i in range(1, 95)
    )
    pdf = render_report_pdf(_sample_data(long_findings, "Normal long report."), _sample_org())

    assert pdf.startswith(b"%PDF-1.4\n")
    assert pdf.count(b"/Type /Page ") >= 2
    assert b"%%EOF\n" in pdf


def test_render_report_pdf_handles_unicode_and_arabic_without_error():
    data = _sample_data(
        findings="<p>Normal brain CT.</p><p dir='rtl'>لا يوجد نزيف دماغي حاد</p>",
        impression="<p>Lesion &lt; 5 mm — stable.</p>",
    )
    pdf = render_report_pdf(data, _sample_org())

    assert pdf.startswith(b"%PDF-1.4\n")
    assert b"%%EOF\n" in pdf


def test_safe_pdf_filename_strips_unsafe_characters():
    assert _safe_pdf_filename({"accession_number": "ACC/2026:01"}, "1.2.3") == "PAXS_Report_ACC_2026_01.pdf"
    assert _safe_pdf_filename({}, "1.2.840.10008.99") == "PAXS_Report_1.2.840.10008.99.pdf"
