"""C07 — no fabricated clinical content in the study UI or its shipped assets.

The study-detail drawer used to render invented demographics and "normal"
findings/impression for any study that had a report status, and a mock viewer
shipped fabricated AI CAD findings and synthetic HU values. Both were clinical
fabrication shown inside real patient views.

This is a denylist guard: it fails if any of that content comes back into the
source *or* into the assets the server actually serves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UI_SRC = REPO / "study-list-ui" / "src"
SERVED = REPO / "app" / "static" / "studies-ui"

# The exact fabricated strings found during the C07 reconnaissance. Each one is
# clinical content that was invented, not derived from the archive.
BANNED = [
    "1980-01-01",                              # hardcoded date of birth
    "Patient presents with acute pain",        # fabricated clinical history
    "Visualized structures appear unremarkable",  # fabricated findings
    "Unremarkable examination",                # fabricated impression
    "No acute fracture or dislocation",        # fabricated findings
    "Bifurcation Carina",                      # fabricated AI finding
    "Normal airway lumen diameter",            # fabricated AI note
    "6.2mm RUL Nodule",                        # fabricated AI measurement
    "AI Lung Nodule CAD",                      # fabricated AI series
    "AI CAD INFERENCE ACTIVE",                 # fabricated AI status
    "AI SEGMENTATION:",                        # fabricated AI overlay label
]

# Invented series arithmetic: `#{i + 1} • {Math.floor((i + 1) * 15)} images`.
BANNED_PATTERNS = [
    re.compile(r"Math\.floor\(\s*\(\s*i\s*\+\s*1\s*\)\s*\*\s*15\s*\)"),
    re.compile(r"useState<string>\(\s*'145 HU'\s*\)"),
]


def _text_files(root: Path, suffixes: tuple[str, ...]):
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in suffixes:
            yield path


def _scan(root: Path, suffixes: tuple[str, ...]) -> list[str]:
    """Lines under `root` containing banned clinical content."""
    hits: list[str] = []
    for path in _text_files(root, suffixes):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - unreadable artifact
            continue
        for needle in BANNED:
            if needle in text:
                hits.append(f"{path.relative_to(REPO)}: contains {needle!r}")
        for pattern in BANNED_PATTERNS:
            if pattern.search(text):
                hits.append(f"{path.relative_to(REPO)}: matches {pattern.pattern!r}")
    return hits


def test_source_has_no_fabricated_clinical_content():
    """The UI source must not contain invented clinical text."""
    hits = _scan(UI_SRC, (".tsx", ".ts"))
    assert not hits, "fabricated clinical content is back in the UI source:\n  " + "\n  ".join(hits)


def test_served_assets_have_no_fabricated_clinical_content():
    """What the server actually serves must be clean too (not just the source)."""
    hits = _scan(SERVED, (".js", ".html", ".css"))
    assert not hits, (
        "fabricated clinical content is being served to browsers:\n  " + "\n  ".join(hits)
    )


def test_served_index_references_an_existing_bundle():
    """Guards the build/sync step: a stale index.html would 404 the app."""
    index = SERVED / "index.html"
    if not index.is_file():
        pytest.skip("the study-list UI has not been built into app/static yet")
    html = index.read_text(encoding="utf-8")
    bundles = re.findall(r"assets/(index-[A-Za-z0-9_-]+\.js)", html)
    assert bundles, "index.html does not reference a JS bundle"
    for bundle in bundles:
        assert (SERVED / "assets" / bundle).is_file(), (
            f"index.html references {bundle}, which is not present in the served assets "
            "(run the frontend build and sync it into app/static/studies-ui)"
        )


def test_mock_viewer_dashboard_is_removed():
    """The fabricated AI CAD / synthetic-HU dashboard must not exist."""
    assert not (UI_SRC / "components" / "viewer" / "PacsViewerDashboard.tsx").exists()
    for path in _text_files(UI_SRC, (".tsx", ".ts")):
        assert "PacsViewerDashboard" not in path.read_text(encoding="utf-8", errors="ignore"), (
            f"{path.relative_to(REPO)} still references the removed mock viewer"
        )


def test_drawer_does_not_invent_demographics_or_report_text():
    """The drawer must read demographics/report text from the archive only."""
    drawer = UI_SRC / "components" / "study-list" / "StudyDetailDrawer.tsx"
    source = drawer.read_text(encoding="utf-8")
    # Real sources, with explicit fallbacks instead of substituted values.
    assert "patient_birth_date" in source
    assert "patient_sex" in source
    assert "DOB: Unknown" in source
    assert "Sex: N/A" in source
    # Report text comes from the API, never from a literal.
    assert "fetchStudyReport" in source


def test_report_modal_gates_finalize_by_role():
    """C06: the UI must not offer signing to roles the server refuses."""
    enhance = REPO / "app" / "static" / "viewer" / "paxs-study-list-enhance.js"
    source = enhance.read_text(encoding="utf-8")
    assert "paxsCanFinalize" in source
    assert "paxsUser.role === 'RADIOLOGIST'" in source
    # And it must surface a refusal instead of reporting success.
    assert "Could not save the report" in source
    assert "res.ok" in source


def test_served_page_loads_the_enhancement_script():
    """The hand-maintained enhancement script must survive a frontend build."""
    index = SERVED / "index.html"
    if not index.is_file():
        pytest.skip("the study-list UI has not been built into app/static yet")
    html = index.read_text(encoding="utf-8")
    assert "paxs-study-list-enhance.js" in html, (
        "the served page no longer loads the enhancement script "
        "(the report modal and user chip would disappear)"
    )


# --------------------------------------------------------------------------- #
# H01 / H16 / H17 — the study-list UI must not fake actions or invent data.
# --------------------------------------------------------------------------- #
UI_ACTION_BANNED = [
    "paxs.local",        # fabricated share domain
    "10.0.0.5",          # hardcoded forward destination IPs
    "192.168.1.50",
    "10.0.0.9",
    "ARCHIVE_A",         # hardcoded forward destination AE titles
    "ORTHANC_REMOTE",
    "PACS_BACKUP",
    "Simulate delete",   # the old mock-success comment
    "Fake QR",
]

UI_ACTION_PATTERNS = [
    # A QR "code" drawn from randomness.
    re.compile(r"Math\.random\(\)\s*>\s*0\.5"),
    # The mock-success shape: a timer that reports success with no API call.
    re.compile(r"setTimeout\([^)]*onToast\(\s*[`'\"]Successfully forwarded"),
]


def test_no_fabricated_ui_actions_in_source():
    """H01/H16/H17: no invented destinations, domains or simulated success."""
    hits = _scan(UI_SRC, (".tsx", ".ts")) + _scan(
        REPO / "app" / "static" / "viewer", (".js",)
    )
    for needle in UI_ACTION_BANNED:
        for path in _text_files(UI_SRC, (".tsx", ".ts")):
            if needle in path.read_text(encoding="utf-8", errors="ignore"):
                hits.append(f"{path.relative_to(REPO)}: contains {needle!r}")
    for pattern in UI_ACTION_PATTERNS:
        for path in _text_files(UI_SRC, (".tsx", ".ts")):
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                hits.append(f"{path.relative_to(REPO)}: matches {pattern.pattern!r}")
    assert not hits, "fabricated UI behaviour is back:/n  " + "\n  ".join(hits)


def test_no_fabricated_ui_actions_in_served_assets():
    hits = []
    for needle in UI_ACTION_BANNED:
        for path in _text_files(SERVED, (".js", ".html")):
            if needle in path.read_text(encoding="utf-8", errors="ignore"):
                hits.append(f"{path.relative_to(REPO)}: contains {needle!r}")
    assert not hits, "fabricated UI behaviour is being served:/n  " + "\n  ".join(hits)


def test_report_action_binds_to_the_selection_not_the_first_row():
    """H16: the global report button must never fall back to studies[0]."""
    source = (UI_SRC / "components" / "study-list" / "StudyListPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "studies[0]?.study_uid" not in source, (
        "the report action still opens the first listed study"
    )
    assert "selectedStudy.study_uid" in source
    assert "disabled={!selectedStudy}" in source


def test_select_items_use_non_empty_values():
    """H17: Radix reserves "" for the placeholder."""
    toolbar = (UI_SRC / "components" / "study-list" / "SearchToolbar.tsx").read_text(
        encoding="utf-8"
    )
    assert '<SelectItem value="">' not in toolbar
    assert '<SelectItem value="all">' in toolbar
    # And the shared primitive refuses an empty value.
    primitive = (UI_SRC / "components" / "ui" / "select.tsx").read_text(encoding="utf-8")
    assert "assertNonEmptyValue" in primitive


def test_modals_call_real_endpoints():
    """H01: each modal must reach its API rather than simulating success."""
    modals = UI_SRC / "components" / "study-list"
    assert "deleteStudy" in (modals / "DeleteModal.tsx").read_text(encoding="utf-8")
    forward = (modals / "ForwardModal.tsx").read_text(encoding="utf-8")
    assert "fetchNodes" in forward and "forwardStudy" in forward
    assert "Queued for forwarding" in forward, "must not claim delivery"
    share = (modals / "ShareModal.tsx").read_text(encoding="utf-8")
    assert "createShare" in share and "qr_base64" in share
