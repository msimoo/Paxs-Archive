"""Dashboard service-matrix tests.

The admin dashboard surfaces every completed Core DICOM milestone as a live
capability matrix: ``/api/v1/health`` lists each DIMSE service (C-STORE /
C-FIND / C-GET / C-MOVE / C-ECHO / MWL / MPPS / Storage Commitment) with its
SOP class and roles, plus the web/integration layers, and ``/api/v1/stats``
carries a compact ``features.dicom_services`` counter for the feature card.
"""

import pytest


from app.core.config import settings
from app.api.v1.dashboard import DIMSE_SERVICES

EXPECTED_SERVICE_NAMES = {
    "C-STORE", "C-FIND (Study Root)", "C-GET (Study Root)",
    "C-MOVE (Study Root)", "C-ECHO (Verification)",
    "Modality Worklist (MWL)", "MPPS", "Storage Commitment",
}


def test_health_exposes_dicom_service_matrix(client):
    health = client.get("/api/v1/health").json()
    services = health["services"]
    dimse = services["dicom"]

    assert len(dimse) == len(DIMSE_SERVICES) == 8
    assert {d["name"] for d in dimse} == EXPECTED_SERVICE_NAMES
    for d in dimse:
        assert d["sop_class"].startswith("1.2.840.10008")
        assert d["roles"] in ("SCP + SCU", "SCP")
        assert isinstance(d["enabled"], bool)

    # The matrix follows the configured receiver flag (off in the test env).
    assert services["dicom_enabled"] is False
    assert all(d["enabled"] is False for d in dimse)
    assert services["tls_enabled"] == settings.DICOM_TLS_ENABLED

    # Web / integration layers are present and their flags match settings.
    web = {w["name"]: w for w in services["web"]}
    assert {"DICOMweb (QIDO/WADO/STOW)", "HL7 MLLP (HIS ADT + ORM)", "FHIR R4"} <= set(web)
    assert web["DICOMweb (QIDO/WADO/STOW)"]["enabled"] is True
    assert web["HL7 MLLP (HIS ADT + ORM)"]["enabled"] == settings.HL7_ENABLED
    assert web["FHIR R4"]["enabled"] == settings.FHIR_ENABLED


def test_health_still_serves_core_fields(client):
    health = client.get("/api/v1/health").json()
    assert health["status"] == "ok"
    assert health["dicom"]["enabled"] is False
    assert "database" in health and "storage" in health and "rate_limit_per_minute" in health
    # The dashboard builds absolute viewer deep-links from this value.
    assert health["public_base_url"] == settings.PUBLIC_BASE_URL


def test_dashboard_viewer_links_use_public_base_helper():
    """Every PaxsViewer deep-link in the dashboard goes through the single
    ``viewerUrl()`` helper that honors PUBLIC_BASE_URL and targets the
    /viewer/viewer mode route — no inline URLs drift back in."""
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )

    assert "function viewerUrl(studyUid)" in js
    assert "window.PAXS_PUBLIC_BASE_URL || location.origin" in js
    assert "window.PAXS_PUBLIC_BASE_URL = h.public_base_url" in js

    # The deep-link path literal may appear only inside viewerUrl(); every
    # other caller uses the helper so the base URL is applied uniformly.
    assert js.count("/viewer/viewer?StudyInstanceUIDs=") == 1
    # Sanity: the helper is actually used by the button + anchor renderers.
    assert "location.href=viewerUrl('" in js
    assert 'href="${viewerUrl(' in js


def test_stats_features_include_dicom_services_counter(client):
    stats = client.get("/api/v1/stats").json()
    ds = stats["features"]["dicom_services"]
    assert ds["total"] == 8
    assert ds["enabled"] == 0  # receiver disabled in the test env
    assert ds["tls"] == settings.DICOM_TLS_ENABLED


def test_stats_feature_modules_still_present(client):
    """The earlier feature counters (workflow/enterprise modules) stay intact."""
    stats = client.get("/api/v1/stats").json()
    f = stats["features"]
    for key in ("worklist", "mpps", "commitments", "critical_results",
                "routing", "lossy_compression", "migration", "kin",
                "triage", "audit", "tenants"):
        assert key in f, f"missing feature module {key}"


# --------------------------------------------------------------------------- #
# C03: the volume / FHIR / GSPS / dashboard / metrics routes require
# authentication. Override the anonymous ``client`` fixture for this module so
# its requests carry a valid ADMIN bearer token. Tests that assert anonymous
# rejection live in test_route_auth_matrix.py and use ``anonymous_client``.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(auth_client):
    """Authenticated client (ADMIN bearer token)."""
    return auth_client
