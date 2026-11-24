"""Tests for rich-text report sanitization, print rendering, and HL7 plain-text conversion."""

from types import SimpleNamespace

from app.services.report_html import (
    html_to_plain_text,
    looks_like_rich_html,
    render_rich_report_html,
    sanitize_report_html,
    sanitize_stored_field,
)
from app.services.report_print import render_report_html


def test_sanitizer_preserves_clinical_formatting_and_styles():
    raw = (
        '<h2 style="text-align: center; color: #0284c7">Brain CT</h2>'
        '<p style="font-family: Georgia, serif; font-size: 14px">'
        '<b>Acute findings:</b> No hemorrhage. Volume 12 cm<sup>3</sup>, T<sub>2</sub> normal.'
        '</p>'
        '<ol><li>Ventricles normal</li><li>Midline intact</li></ol>'
        '<ul dir="rtl"><li>لا يوجد نزيف</li></ul>'
    )
    cleaned = sanitize_report_html(raw)

    assert '<h2 style="text-align: center; color: #0284c7">Brain CT</h2>' in cleaned
    assert "font-family: Georgia, 'Times New Roman', serif" in cleaned
    assert "font-size: 14px" in cleaned
    assert "<b>Acute findings:</b>" in cleaned
    assert "<sup>3</sup>" in cleaned
    assert "<sub>2</sub>" in cleaned
    assert "<ol><li>Ventricles normal</li><li>Midline intact</li></ol>" in cleaned
    assert '<ul dir="rtl"><li>لا يوجد نزيف</li></ul>' in cleaned


def test_sanitizer_strips_xss_scripts_handlers_and_unsafe_styles():
    malicious = (
        '<p onclick="alert(1)" style="color: red; background-image: url(javascript:alert(1))">'
        'Safe <b>text</b><script>alert("xss")</script>'
        '<img src=x onerror=alert(2)>'
        '<iframe src="https://evil.test">bad</iframe>'
        '<svg onload=alert(3)><circle/></svg>'
        '<a href="javascript:alert(4)">link</a>'
        '</p>'
    )
    cleaned = sanitize_report_html(malicious)

    assert "script" not in cleaned.lower()
    assert "alert(" not in cleaned
    assert "onclick" not in cleaned.lower()
    assert "onerror" not in cleaned.lower()
    assert "onload" not in cleaned.lower()
    assert "iframe" not in cleaned.lower()
    assert "svg" not in cleaned.lower()
    assert "javascript:" not in cleaned.lower()
    assert "Safe <b>text</b>" in cleaned
    assert "link" in cleaned


def test_legacy_plain_text_with_clinical_inequalities_is_preserved():
    plain = "Nodule < 5 mm in RUL & no effusion.\nFollow-up in 6 months."

    assert not looks_like_rich_html(plain)
    assert sanitize_stored_field(plain) == plain

    rendered = render_rich_report_html(plain)
    assert "Nodule &lt; 5 mm in RUL &amp; no effusion.<br>Follow-up in 6 months." == rendered


def test_html_to_plain_text_formats_lists_and_paragraphs_for_hl7():
    rich = (
        "<h3>Findings</h3>"
        "<p><b>Liver:</b> Normal size.</p>"
        "<ol><li>No focal lesion</li><li>No biliary dilatation</li></ol>"
        "<ul><li>Spleen unremarkable</li></ul>"
    )
    plain = html_to_plain_text(rich)

    assert "<" not in plain
    assert "Findings" in plain
    assert "Liver: Normal size." in plain
    assert "1. No focal lesion" in plain
    assert "2. No biliary dilatation" in plain
    assert "- Spleen unremarkable" in plain


def test_print_report_renders_sanitized_rich_html_safely():
    data = {
        "study_instance_uid": "1.2.840.10008.99999",
        "patient_name": "Doe^Jane",
        "patient_id": "MRN-42",
        "patient_age": "45Y",
        "patient_sex": "F",
        "accession_number": "ACC-9",
        "modality": "CT",
        "study_date": "20261123",
        "study_description": "CT Chest",
        "report": {
            "status": "FINALIZED",
            "radiologist": "dr.smith",
            "finalized_at": "2026-11-23T12:00:00",
            "findings": "<p><strong>Clear</strong> lungs.</p><ul><li>No pneumothorax</li></ul><script>evil()</script>",
            "impression": "<p><em>Normal</em> chest CT.</p>",
            "addenda": [],
        },
    }
    org = SimpleNamespace(
        org_name_en="PAXS Hospital",
        logo_path=None,
        header_html_text=None,
        footer_html_text=None,
    )
    out = render_report_html(data, org)

    assert "<strong>Clear</strong> lungs." in out
    assert "<ul><li>No pneumothorax</li></ul>" in out
    assert "<em>Normal</em> chest CT." in out
    assert "<script>" not in out
    assert "evil()" not in out
