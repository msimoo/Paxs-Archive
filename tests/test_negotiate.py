"""Tests for roadmap #51 — Viewer processing mode negotiation (Phase 1).

Covers:
  * Default mode negotiation returns ``hybrid``.
  * Explicit ``server`` and ``client`` global settings return the expected
    mode, endpoints and capability flags.
  * Per-role override resolution (RADIOLOGIST → client, PHYSICIAN → server).
  * Hybrid decision algorithm: WebGL score threshold, RTT threshold, server
    overload (session count + cache budget).
  * Response headers ``X-PAXS-Processing-Mode`` and ``X-PAXS-Server-Load``.
  * Volume service session tracking helpers (unit tests).
"""

from __future__ import annotations

import json
import time

import numpy as np
import pytest
from pydicom.uid import generate_uid

from app.core.config import settings
from app.services import volume_service
from tests.helpers import (
    auth_for_role,
    build_stow_body,
    ds_bytes,
    make_instance,
    stow_headers,
)

AXIAL_ORIENTATION = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


# ---------------------------------------------------------------- helpers ---

def _ingest_series(client, n_slices: int = 2):
    """STOW a tiny synthetic CT stack and return (study_uid, series_uid)."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    datasets = []
    for i in range(n_slices):
        pixel = np.full((16, 16), 100 + i * 10, dtype=np.uint16)
        ds = make_instance(
            patient_id="NEG_PAT",
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=generate_uid(),
            instance_num=i + 1,
            rows=16,
            cols=16,
            pixel=pixel,
            position=[0.0, 0.0, float(i * 5)],
            orientation=list(AXIAL_ORIENTATION),
            rescale_slope="1",
            rescale_intercept="0",
            slice_thickness="5",
        )
        datasets.append(ds_bytes(ds))
    resp = client.post(
        f"/dicom-web/studies/{study_uid}",
        content=build_stow_body(datasets),
        headers=stow_headers(),
    )
    assert resp.status_code == 200
    return study_uid, series_uid


@pytest.fixture(autouse=True)
def _restore_viewer_settings():
    """Snapshot and restore the 6 viewer-processing settings after each test."""
    keys = (
        "VIEWER_PROCESSING_MODE",
        "HYBRID_CLIENT_SCORE_THRESHOLD",
        "HYBRID_SERVER_VOL_BUDGET_MB",
        "HYBRID_RTT_THRESHOLD_MS",
        "HYBRID_MAX_SERVER_MPR_SESSIONS",
        "VIEWER_PROCESSING_ROLE_MAP",
    )
    snap = {k: getattr(settings, k) for k in keys}
    yield
    for k, v in snap.items():
        setattr(settings, k, v)
    # Clear any sessions registered during the test
    with volume_service._VOL_CACHE_LOCK:
        volume_service._ACTIVE_SERVER_SESSIONS.clear()


# C03: the volume routes require authentication. Override the anonymous
# ``client`` fixture for this module so its requests carry a valid ADMIN
# bearer token.
@pytest.fixture()
def client(auth_client):
    """Authenticated client (ADMIN bearer token)."""
    return auth_client


# =========================================================================== #
#  §1 — Session tracking unit tests (volume_service helpers)
# =========================================================================== #


class TestSessionTracking:
    """Unit tests for the lightweight MPR session bookkeeping."""

    @pytest.fixture(autouse=True)
    def _clean_sessions(self):
        """Start each test from an empty session map.

        `_ACTIVE_SERVER_SESSIONS` is module-level state shared with every other
        suite in the run, and these tests assert *absolute* counts. Without a
        pre-test clear the result depends on test collection order — e.g. any
        earlier suite that hits /mpr/slice or /tiles leaves a registration
        behind and test_register_and_count sees more than 2.
        """
        with volume_service._VOL_CACHE_LOCK:
            volume_service._ACTIVE_SERVER_SESSIONS.clear()
        yield
        with volume_service._VOL_CACHE_LOCK:
            volume_service._ACTIVE_SERVER_SESSIONS.clear()

    def test_register_and_count(self):
        volume_service.register_server_session("1.2.3.100")
        volume_service.register_server_session("1.2.3.200")
        assert volume_service.active_server_session_count() == 2

    def test_release(self):
        volume_service.register_server_session("1.2.3.300")
        volume_service.release_server_session("1.2.3.300")
        assert volume_service.active_server_session_count() == 0

    def test_release_nonexistent_is_safe(self):
        volume_service.release_server_session("1.2.3.404")  # no-op

    def test_expiry(self, monkeypatch):
        volume_service.register_server_session("1.2.3.400")
        # Simulate 6 minutes of inactivity (timeout = 300s)
        with volume_service._VOL_CACHE_LOCK:
            volume_service._ACTIVE_SERVER_SESSIONS["1.2.3.400"] = (
                time.monotonic() - 400
            )
        assert volume_service.active_server_session_count() == 0

    def test_estimated_vol_cache_mb_empty(self):
        assert volume_service.estimated_vol_cache_mb() == 0


# =========================================================================== #
#  §2 — Default negotiate: mode = hybrid (zero behavioral change)
# =========================================================================== #


def test_negotiate_default_is_hybrid(client):
    """The default VIEWER_PROCESSING_MODE='hybrid' is returned."""
    _, series_uid = _ingest_series(client)
    resp = client.get(f"/api/v1/volume/{series_uid}/negotiate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured_mode"] == "hybrid"
    # With no webgl_score reported, effective_mode should be "server"
    # (the hybrid algorithm defaults to server for score=0).
    assert body["effective_mode"] == "server"
    assert body["series_uid"] == series_uid
    assert "endpoints" in body
    assert "capabilities" in body
    assert "hybrid_thresholds" in body


def test_negotiate_hybrid_returns_thresholds(client):
    """Hybrid mode includes the thresholds for the client to self-assess."""
    _, series_uid = _ingest_series(client)
    resp = client.get(f"/api/v1/volume/{series_uid}/negotiate")
    thresholds = resp.json()["hybrid_thresholds"]
    assert thresholds["client_score_threshold"] == 40
    assert thresholds["rtt_threshold_ms"] == 150
    assert thresholds["server_vol_budget_mb"] == 4096
    assert thresholds["max_server_sessions"] == 20


def test_negotiate_hybrid_strong_client(client, monkeypatch):
    """A strong client (webgl_score >= threshold) resolves to 'client'."""
    monkeypatch.setattr(settings, "HYBRID_CLIENT_SCORE_THRESHOLD", 40)
    _, series_uid = _ingest_series(client)
    resp = client.get(
        f"/api/v1/volume/{series_uid}/negotiate?webgl_score=72&rtt_ms=30"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured_mode"] == "hybrid"
    assert body["effective_mode"] == "client"


def test_negotiate_hybrid_marginal_client_resolves_to_split(client, monkeypatch):
    """A *marginal* client (20 <= score < threshold, low RTT) resolves to 'split'.

    Phase 4 (§7.1 branch 5) turned this band into split processing: a coarse
    local preview for orientation plus server-refined fine slices. Before
    Phase 4 it fell through to 'server', which is the behaviour this test
    originally pinned.
    """
    monkeypatch.setattr(settings, "HYBRID_CLIENT_SCORE_THRESHOLD", 40)
    _, series_uid = _ingest_series(client)
    resp = client.get(
        f"/api/v1/volume/{series_uid}/negotiate?webgl_score=25&rtt_ms=30"
    )
    assert resp.json()["effective_mode"] == "split"


def test_negotiate_hybrid_weak_client_resolves_to_server(client, monkeypatch):
    """A genuinely weak client (score < 20) still resolves to 'server'.

    20 is Phase 4's ``HYBRID_CLIENT_MIN_SCORE`` floor: below it a downsample=4
    preview would still stall, so the server pipeline is the only sane answer.
    """
    monkeypatch.setattr(settings, "HYBRID_CLIENT_SCORE_THRESHOLD", 40)
    _, series_uid = _ingest_series(client)
    resp = client.get(
        f"/api/v1/volume/{series_uid}/negotiate?webgl_score=10&rtt_ms=30"
    )
    assert resp.json()["effective_mode"] == "server"


def test_negotiate_hybrid_high_latency_prefers_client(client, monkeypatch):
    """High RTT pushes a marginal client to 'client' mode (avoid per-slice latency)."""
    monkeypatch.setattr(settings, "HYBRID_CLIENT_SCORE_THRESHOLD", 40)
    monkeypatch.setattr(settings, "HYBRID_RTT_THRESHOLD_MS", 100)
    _, series_uid = _ingest_series(client)
    # Score 25 < threshold (40), but RTT 200 > 100 and score >= 20
    resp = client.get(
        f"/api/v1/volume/{series_uid}/negotiate?webgl_score=25&rtt_ms=200"
    )
    assert resp.json()["effective_mode"] == "client"


def test_negotiate_hybrid_no_webgl_forces_server(client):
    """A client with no WebGL (score=0) always resolves to 'server'."""
    _, series_uid = _ingest_series(client)
    resp = client.get(
        f"/api/v1/volume/{series_uid}/negotiate?webgl_score=0&rtt_ms=500"
    )
    assert resp.json()["effective_mode"] == "server"


# =========================================================================== #
#  §3 — Explicit server / client mode
# =========================================================================== #


def test_negotiate_explicit_server_mode(client, monkeypatch):
    """When VIEWER_PROCESSING_MODE='server', the effective mode is always 'server'."""
    monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
    _, series_uid = _ingest_series(client)
    resp = client.get(f"/api/v1/volume/{series_uid}/negotiate?webgl_score=99")
    body = resp.json()
    assert body["configured_mode"] == "server"
    assert body["effective_mode"] == "server"
    # No hybrid_thresholds when not hybrid
    assert "hybrid_thresholds" not in body
    # Endpoints should include mpr_slice but NOT data
    assert "mpr_slice" in body["endpoints"]
    assert "data" not in body["endpoints"]
    # Capabilities
    assert body["capabilities"]["server_renders_slices"] is True
    assert body["capabilities"]["raw_volume_stream"] is False


def test_negotiate_explicit_client_mode(client, monkeypatch):
    """When VIEWER_PROCESSING_MODE='client', the effective mode is always 'client'."""
    monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
    _, series_uid = _ingest_series(client)
    resp = client.get(f"/api/v1/volume/{series_uid}/negotiate?webgl_score=10")
    body = resp.json()
    assert body["configured_mode"] == "client"
    assert body["effective_mode"] == "client"
    assert "hybrid_thresholds" not in body
    # Endpoints should include data but NOT mpr_slice
    assert "data" in body["endpoints"]
    assert "mpr_slice" not in body["endpoints"]
    # Capabilities
    assert body["capabilities"]["client_webgl_required"] is True
    assert body["capabilities"]["raw_volume_stream"] is True
    assert body["capabilities"]["server_renders_slices"] is False


# =========================================================================== #
#  §4 — Per-role overrides
# =========================================================================== #


def test_negotiate_role_override_radiologist_gets_client(
    client, auth_headers, monkeypatch
):
    """RADIOLOGIST role is overridden to 'client' regardless of global mode."""
    role_map = json.dumps({"RADIOLOGIST": "client", "PHYSICIAN": "server"})
    monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
    monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", role_map)

    _, series_uid = _ingest_series(client)

    rad_h = auth_for_role(
        client, "neg_radiologist", role="RADIOLOGIST", admin=auth_headers
    )

    resp = client.get(
        f"/api/v1/volume/{series_uid}/negotiate?webgl_score=10",
        headers=rad_h,
    )
    assert resp.status_code == 200
    body = resp.json()
    # The role override should override global hybrid → client
    assert body["configured_mode"] == "client"
    assert body["effective_mode"] == "client"


def test_negotiate_role_override_physician_gets_server(
    client, auth_headers, monkeypatch
):
    """PHYSICIAN role is overridden to 'server' regardless of global mode."""
    role_map = json.dumps({"RADIOLOGIST": "client", "PHYSICIAN": "server"})
    monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
    monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", role_map)

    _, series_uid = _ingest_series(client)

    phys_h = auth_for_role(
        client, "neg_physician", role="PHYSICIAN", admin=auth_headers
    )

    resp = client.get(
        f"/api/v1/volume/{series_uid}/negotiate?webgl_score=99",
        headers=phys_h,
    )
    body = resp.json()
    assert body["configured_mode"] == "server"
    assert body["effective_mode"] == "server"


def test_negotiate_invalid_role_map_is_tolerated(client, monkeypatch):
    """A malformed VIEWER_PROCESSING_ROLE_MAP JSON is silently ignored."""
    monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
    monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "NOT VALID JSON{{{")

    _, series_uid = _ingest_series(client)
    resp = client.get(f"/api/v1/volume/{series_uid}/negotiate?webgl_score=50")
    assert resp.status_code == 200
    # Falls through to hybrid → client (score=50 >= threshold=40)
    assert resp.json()["configured_mode"] == "hybrid"


# =========================================================================== #
#  §5 — Server load triggers
# =========================================================================== #


def test_negotiate_hybrid_server_overloaded_by_sessions(client, monkeypatch):
    """When active sessions >= max, hybrid pushes capable clients to 'client'."""
    monkeypatch.setattr(settings, "HYBRID_MAX_SERVER_MPR_SESSIONS", 2)
    monkeypatch.setattr(settings, "HYBRID_CLIENT_SCORE_THRESHOLD", 80)
    # Register 2 sessions (= threshold)
    volume_service.register_server_session("1.2.3.OVERLOAD_1")
    volume_service.register_server_session("1.2.3.OVERLOAD_2")

    _, series_uid = _ingest_series(client)
    # Score 30 is below normal threshold (80) but >= 20, so overload → client
    resp = client.get(
        f"/api/v1/volume/{series_uid}/negotiate?webgl_score=30"
    )
    assert resp.json()["effective_mode"] == "client"


def test_negotiate_hybrid_server_overloaded_weak_client_stays_server(
    client, monkeypatch
):
    """An overloaded server can't push a very weak client (score < 20) to client."""
    monkeypatch.setattr(settings, "HYBRID_MAX_SERVER_MPR_SESSIONS", 1)
    volume_service.register_server_session("1.2.3.WEAK_OL")

    _, series_uid = _ingest_series(client)
    resp = client.get(
        f"/api/v1/volume/{series_uid}/negotiate?webgl_score=10"
    )
    assert resp.json()["effective_mode"] == "server"


# =========================================================================== #
#  §6 — Response headers
# =========================================================================== #


def test_negotiate_response_headers(client):
    """The negotiate response includes the expected custom headers.

    Phase 4 (§7.3) widened ``X-PAXS-Server-Load`` from a bare integer to a
    ``<active>/<max>`` ratio and added ``X-PAXS-Vol-Cache-MB``, so the client
    can tell "3 of 20" from "3 of 3" without knowing this server's config.
    """
    _, series_uid = _ingest_series(client)
    resp = client.get(f"/api/v1/volume/{series_uid}/negotiate")
    assert resp.headers["X-PAXS-Processing-Mode"] in ("server", "client", "hybrid")
    active, _, max_sessions = resp.headers["X-PAXS-Server-Load"].partition("/")
    assert active.isdigit() and max_sessions.isdigit()
    cache_mb, _, budget_mb = resp.headers["X-PAXS-Vol-Cache-MB"].partition("/")
    assert cache_mb.isdigit() and budget_mb.isdigit()
    assert resp.headers["Cache-Control"] == "no-store"


def test_negotiate_nonexistent_series(client):
    """Negotiating for a series that does not exist returns 404."""
    resp = client.get("/api/v1/volume/1.2.3.DOES_NOT_EXIST/negotiate")
    assert resp.status_code == 404


def test_negotiate_streaming_disabled(client, monkeypatch):
    """If VOLUME_STREAMING_ENABLED is False, negotiate returns 503."""
    monkeypatch.setattr(settings, "VOLUME_STREAMING_ENABLED", False)
    resp = client.get("/api/v1/volume/1.2.3.FAKE/negotiate")
    assert resp.status_code == 503
    # Restore so other tests aren't affected
    monkeypatch.setattr(settings, "VOLUME_STREAMING_ENABLED", True)


def test_negotiate_anonymous_rejected(client):
    """Anonymous access without share token or credentials returns 401 (C03)."""
    _, series_uid = _ingest_series(client)
    resp = client._client.get(f"/api/v1/volume/{series_uid}/negotiate")
    assert resp.status_code == 401



# =========================================================================== #
#  §7 — Config defaults validation
# =========================================================================== #


class TestConfigDefaults:
    """Verify the 6 new settings exist with correct defaults."""

    def test_viewer_processing_mode_default(self):
        assert settings.VIEWER_PROCESSING_MODE == "hybrid"

    def test_hybrid_client_score_threshold_default(self):
        assert settings.HYBRID_CLIENT_SCORE_THRESHOLD == 40

    def test_hybrid_server_vol_budget_default(self):
        assert settings.HYBRID_SERVER_VOL_BUDGET_MB == 4096

    def test_hybrid_rtt_threshold_default(self):
        assert settings.HYBRID_RTT_THRESHOLD_MS == 150

    def test_hybrid_max_server_sessions_default(self):
        assert settings.HYBRID_MAX_SERVER_MPR_SESSIONS == 20

    def test_viewer_processing_role_map_default(self):
        assert settings.VIEWER_PROCESSING_ROLE_MAP == ""


# =========================================================================== #
#  §8 — Settings registry validation
# =========================================================================== #


class TestSettingsRegistry:
    """Verify the new keys are registered in EDITABLE_KEYS and GROUPS."""

    def test_viewer_keys_in_editable_keys(self):
        from app.services.settings_service import EDITABLE_KEYS
        viewer_keys = [
            "VIEWER_PROCESSING_MODE",
            "HYBRID_CLIENT_SCORE_THRESHOLD",
            "HYBRID_SERVER_VOL_BUDGET_MB",
            "HYBRID_RTT_THRESHOLD_MS",
            "HYBRID_MAX_SERVER_MPR_SESSIONS",
            "VIEWER_PROCESSING_ROLE_MAP",
        ]
        for key in viewer_keys:
            assert key in EDITABLE_KEYS, f"{key} not found in EDITABLE_KEYS"
            assert EDITABLE_KEYS[key]["group"] == "viewer"

    def test_viewer_group_in_groups(self):
        from app.services.settings_service import GROUPS
        assert "viewer" in GROUPS
        assert GROUPS["viewer"]["label"] == "Viewer"

    def test_processing_mode_is_select_type(self):
        from app.services.settings_service import EDITABLE_KEYS
        spec = EDITABLE_KEYS["VIEWER_PROCESSING_MODE"]
        assert spec["type"] == "select"
        assert set(spec["options"]) == {"hybrid", "server", "client"}
