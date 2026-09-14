"""Terms & Conditions / Privacy Policy footer popup.

The dashboard footer renders a "Terms & Conditions" link (feature-plans/terms.md
is the source of truth) that opens a modal whose content is fetched from
GET /terms. These tests pin the route contract and the front-end wiring.
"""

from __future__ import annotations

from pathlib import Path

INDEX = Path(__file__).parent.parent / "app" / "static" / "index.html"


def test_terms_route_serves_markdown(client):
    res = client.get("/terms")
    assert res.status_code == 200
    assert "text/markdown" in res.headers["content-type"]
    text = res.text
    assert "Terms and Conditions" in text
    assert "Privacy Policy" in text
    # Key legal clauses must be present.
    assert "Clinical and Medical Disclaimer" in text
    assert "Patient Privacy, Health Data, and Compliance" in text
    assert "Limitation of Liability" in text


def test_footer_terms_link_and_modal_present():
    html = INDEX.read_text(encoding="utf-8")
    # Footer link button that opens the popup.
    assert 'id="btn-terms"' in html
    assert "Terms &amp; Conditions" in html
    # The popup itself with its tabs and disclaimer.
    assert 'id="terms-modal"' in html
    assert 'data-terms-tab="terms"' in html
    assert 'data-terms-tab="privacy"' in html
    assert 'id="terms-body"' in html
    assert "regulatory-compliant medical display workstations" in html
