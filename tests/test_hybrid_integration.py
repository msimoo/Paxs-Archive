"""Phase 4 integration tests — hybrid mode end to end (§3.3, §7, §8 Phase 4).

These tests drive the real HTTP surface:

  * ``/negotiate`` returns ``effective_mode`` in {client, split, server} for the
    client-reported capability scores, so the dynamic engine is reachable from
    the browser exactly as the viewer calls it.
  * Every volume endpoint advertises ``X-PAXS-Server-Load`` and
    ``X-PAXS-Vol-Cache-MB`` as ``<current>/<limit>`` ratios (§7.3), so a client
    can re-evaluate mid-session without a dedicated metrics call.
  * The mode guards behave symmetrically under the new ``split`` resolution:
    ``split`` is a hybrid outcome and must never trip either guard, because a
    split session legitimately uses *both* pipelines.

Regression intent: ``hybrid`` is the shipped default.  These tests are the
proof that enabling the dynamic engine did not narrow the default deployment's
capabilities — a device that could stream raw voxels before can still do so,
and a device that could ask for a server slice can still do so.
"""

from __future__ import annotations

import numpy as np
import pytest
from pydicom.uid import generate_uid

from app.core.config import settings
from app.services import volume_service
from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
AXIAL_ORIENTATION = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


def _ingest_series(client, n_slices: int = 4):
    """STOW a synthetic axial CT stack; returns (study_uid, series_uid)."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    datasets = []
    for i in range(n_slices):
        pixel = np.full((32, 32), 100 + i * 10, dtype=np.uint16)
        pixel[8:24, 8:24] = 1500 + i * 100
        ds = make_instance(
            patient_id="HYB_PAT",
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=generate_uid(),
            instance_num=i + 1,
            rows=32,
            cols=32,
            pixel=pixel,
            position=[10.0, 20.0, float(i * 5)],
            orientation=list(AXIAL_ORIENTATION),
            rescale_slope="1",
            rescale_intercept="-1024",
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
def _isolate_viewer_state():
    """Restore settings and clear module-level caches/sessions around each test.

    ``_ACTIVE_SERVER_SESSIONS``/``_VOL_CACHE``/``_TILE_CACHE`` are module-level
    and shared across suites; clearing on both sides keeps the absolute-count
    assertions below independent of collection order (the leak the Phase 2 suite
    already guarded against).
    """
    keys = (
        "VIEWER_PROCESSING_MODE",
        "VIEWER_PROCESSING_ROLE_MAP",
        "VOLUME_STREAMING_ENABLED",
        "HYBRID_CLIENT_SCORE_THRESHOLD",
        "HYBRID_SERVER_VOL_BUDGET_MB",
        "HYBRID_RTT_THRESHOLD_MS",
        "HYBRID_MAX_SERVER_MPR_SESSIONS",
    )
    snap = {k: getattr(settings, k, None) for k in keys}
    with volume_service._VOL_CACHE_LOCK:
        volume_service._ACTIVE_SERVER_SESSIONS.clear()
    yield
    for k, v in snap.items():
        setattr(settings, k, v)
    with volume_service._VOL_CACHE_LOCK:
        volume_service._ACTIVE_SERVER_SESSIONS.clear()
        volume_service._VOL_CACHE.clear()
        volume_service._TILE_CACHE.clear()


@pytest.fixture()
def client(auth_client):
    """Authenticated client (ADMIN bearer token)."""
    return auth_client


def _negotiate(client, series, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/api/v1/volume/{series}/negotiate"
    if qs:
        url += "?" + qs
    return client.get(url)


# =========================================================================== #
#  §1 — /negotiate resolves each hybrid branch end to end
# =========================================================================== #


class TestNegotiateResolvesSplitClientServer:
    """The three hybrid outcomes must each be reachable over HTTP."""

    def test_strong_client_scores_resolve_to_client(self, client):
        _, series = _ingest_series(client)
        resp = _negotiate(client, series, webgl_score=90, rtt_ms=5)

        assert resp.status_code == 200
        body = resp.json()
        assert body["configured_mode"] == "hybrid"
        assert body["effective_mode"] == "client"
        assert resp.headers["X-PAXS-Processing-Mode"] == "client"

    def test_marginal_client_scores_resolve_to_split(self, client):
        _, series = _ingest_series(client)
        resp = _negotiate(client, series, webgl_score=30, rtt_ms=5)

        assert resp.status_code == 200
        body = resp.json()
        assert body["configured_mode"] == "hybrid"
        assert body["effective_mode"] == "split"
        assert resp.headers["X-PAXS-Processing-Mode"] == "split"

    def test_weak_client_scores_resolve_to_server(self, client):
        _, series = _ingest_series(client)
        resp = _negotiate(client, series, webgl_score=5, rtt_ms=5)

        assert resp.status_code == 200
        body = resp.json()
        assert body["effective_mode"] == "server"

    def test_no_webgl_resolves_to_server(self, client):
        _, series = _ingest_series(client)
        resp = _negotiate(client, series, webgl_score=0, rtt_ms=5)

        assert resp.json()["effective_mode"] == "server"

    def test_high_latency_promotes_a_marginal_client(self, client):
        _, series = _ingest_series(client)
        resp = _negotiate(
            client, series, webgl_score=25,
            rtt_ms=settings.HYBRID_RTT_THRESHOLD_MS + 500,
        )

        assert resp.json()["effective_mode"] == "client"

    def test_saturated_server_sheds_a_capable_client(self, client, monkeypatch):
        monkeypatch.setattr(settings, "HYBRID_MAX_SERVER_MPR_SESSIONS", 1)
        with volume_service._VOL_CACHE_LOCK:
            volume_service._ACTIVE_SERVER_SESSIONS["saturated-series"] = 1e9
        _, series = _ingest_series(client)

        resp = _negotiate(client, series, webgl_score=50, rtt_ms=5)

        assert resp.json()["effective_mode"] == "client"

    def test_saturated_server_keeps_an_incapable_client(self, client, monkeypatch):
        monkeypatch.setattr(settings, "HYBRID_MAX_SERVER_MPR_SESSIONS", 1)
        with volume_service._VOL_CACHE_LOCK:
            volume_service._ACTIVE_SERVER_SESSIONS["saturated-series"] = 1e9
        _, series = _ingest_series(client)

        resp = _negotiate(client, series, webgl_score=5, rtt_ms=5)

        assert resp.json()["effective_mode"] == "server"


class TestNegotiatePayloadShape:
    """The response must stay interpretable by the shipped viewer."""

    def test_split_negotiation_advertises_both_pipelines(self, client):
        _, series = _ingest_series(client)
        body = _negotiate(client, series, webgl_score=30, rtt_ms=5).json()

        # The resolved mode is split, but this is still a *hybrid* deployment:
        # capabilities describe what the browser may use, and a split session
        # needs the coarse raw stream AND server-refined slices. Advertising
        # only one would make the viewer pick a pipeline it cannot complete.
        assert body["effective_mode"] == "split"
        caps = body["capabilities"]
        assert caps["raw_volume_stream"] is True
        assert caps["server_renders_slices"] is True
        assert caps["split_preview"] is True

        endpoints = body["endpoints"]
        assert "data" in endpoints
        assert "mpr_slice" in endpoints

    def test_split_capabilities_are_reported_for_the_split_mode_itself(self):
        # The per-mode map (as opposed to the hybrid union) must mark a split
        # session as GPU-backed, since its coarse preview is rendered locally.
        from app.api.v1.volume import _mode_capabilities

        caps = _mode_capabilities("split")
        assert caps["split_preview"] is True
        assert caps["client_webgl_required"] is True
        assert caps["raw_volume_stream"] is True
        assert caps["server_renders_slices"] is True
        # GPU MPR/MIP are NOT available in split: that is the whole reason the
        # server still renders the fine slices.
        assert caps["gpu_mpr"] is False
        assert caps["gpu_mip"] is False

    def test_client_mode_capabilities_disable_server_slicing(self):
        from app.api.v1.volume import _mode_capabilities

        caps = _mode_capabilities("client")
        assert caps["raw_volume_stream"] is True
        assert caps["server_renders_slices"] is False
        assert caps["split_preview"] is False

    def test_server_mode_capabilities_disable_the_raw_stream(self):
        from app.api.v1.volume import _mode_capabilities

        caps = _mode_capabilities("server")
        assert caps["raw_volume_stream"] is False
        assert caps["server_renders_slices"] is True
        assert caps["client_webgl_required"] is False

    def test_explicit_server_deployment_advertises_no_raw_stream(self, client, monkeypatch):
        # Unlike hybrid, an explicit server deployment really does close /data
        # (503 + mode hint), so its capabilities must say so.
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)
        body = _negotiate(client, series, webgl_score=0, rtt_ms=5).json()

        assert body["effective_mode"] == "server"
        assert body["capabilities"]["raw_volume_stream"] is False

    def test_hybrid_response_reports_thresholds_and_decision(self, client):
        _, series = _ingest_series(client)
        body = _negotiate(client, series, webgl_score=30, rtt_ms=7).json()

        thresholds = body["hybrid_thresholds"]
        assert thresholds["client_score_threshold"] == settings.HYBRID_CLIENT_SCORE_THRESHOLD
        assert thresholds["min_client_score"] == 20
        assert thresholds["rtt_threshold_ms"] == settings.HYBRID_RTT_THRESHOLD_MS
        assert thresholds["max_server_sessions"] == settings.HYBRID_MAX_SERVER_MPR_SESSIONS

        # The decision must be explainable from the response alone.
        decision = body["hybrid_decision"]
        assert decision["webgl_score"] == 30
        assert decision["rtt_ms"] == 7
        assert decision["resolved"] == "split"

    def test_non_hybrid_modes_do_not_include_hybrid_blocks(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)
        body = _negotiate(client, series, webgl_score=90, rtt_ms=5).json()

        assert body["effective_mode"] == "server"
        assert "hybrid_thresholds" not in body
        assert "hybrid_decision" not in body


# =========================================================================== #
#  §2 — Server load reporting headers (§7.3)
# =========================================================================== #


class TestServerLoadHeaders:
    """Every volume endpoint must advertise `<current>/<limit>` load ratios."""

    def test_negotiate_reports_load_ratios(self, client):
        _, series = _ingest_series(client)
        resp = _negotiate(client, series, webgl_score=80, rtt_ms=5)

        load = resp.headers["X-PAXS-Server-Load"]
        cache = resp.headers["X-PAXS-Vol-Cache-MB"]
        assert load == f"0/{settings.HYBRID_MAX_SERVER_MPR_SESSIONS}"
        assert cache == f"0/{settings.HYBRID_SERVER_VOL_BUDGET_MB}"

    def test_info_reports_load_ratios(self, client):
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/info")

        assert resp.status_code == 200
        assert "/" in resp.headers["X-PAXS-Server-Load"]
        assert "/" in resp.headers["X-PAXS-Vol-Cache-MB"]

    def test_data_reports_load_ratios(self, client):
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/data")

        assert resp.status_code == 200
        assert resp.headers["X-PAXS-Server-Load"].split("/")[1] == str(
            settings.HYBRID_MAX_SERVER_MPR_SESSIONS)

    def test_mpr_slice_reports_load_ratios(self, client):
        _, series = _ingest_series(client)
        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&format=png")

        assert resp.status_code == 200
        assert resp.content[:8] == PNG_MAGIC
        assert "/" in resp.headers["X-PAXS-Server-Load"]

    def test_mip_reports_load_ratios(self, client):
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/mip?format=png")

        assert resp.status_code == 200
        assert resp.content[:8] == PNG_MAGIC
        assert "/" in resp.headers["X-PAXS-Server-Load"]

    def test_tiles_reports_load_ratios(self, client):
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=0-1")

        assert resp.status_code == 200
        assert "/" in resp.headers["X-PAXS-Server-Load"]

    def test_active_session_count_is_reflected_in_the_ratio(self, client, monkeypatch):
        monkeypatch.setattr(settings, "HYBRID_MAX_SERVER_MPR_SESSIONS", 7)
        _, series = _ingest_series(client)
        with volume_service._VOL_CACHE_LOCK:
            volume_service._ACTIVE_SERVER_SESSIONS["a"] = 1e9
            volume_service._ACTIVE_SERVER_SESSIONS["b"] = 1e9

        resp = client.get(f"/api/v1/volume/{series}/info")

        assert resp.headers["X-PAXS-Server-Load"] == "2/7"

    def test_load_headers_are_cors_exposed(self, client):
        # Without exposure the browser's JS cannot read them cross-origin, and
        # the whole mid-session adaptation path goes silently dead.
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/info")

        exposed = resp.headers["Access-Control-Expose-Headers"]
        assert "X-PAXS-Server-Load" in exposed
        assert "X-PAXS-Vol-Cache-MB" in exposed


# =========================================================================== #
#  §3 — Mode guards must not fire on a split resolution
# =========================================================================== #


class TestSplitModeKeepsBothPipelines:
    """`split` is a hybrid outcome: neither guard may trip for it.

    The guards key off the *configured* mode through ``_resolve_mode``, so a
    hybrid deployment resolves to ``hybrid`` regardless of what the engine
    decides per request — and therefore both endpoints stay open. That is what
    makes a split session possible at all.
    """

    def test_raw_stream_is_available_in_hybrid_mode(self, client):
        # Default settings → hybrid → /data must stream.
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/data")

        assert resp.status_code == 200
        assert resp.headers.get("X-PAXS-Mode-Hint") is None, "no guard tripped"

    def test_server_slicing_is_available_in_hybrid_mode(self, client):
        _, series = _ingest_series(client)
        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&format=png")

        assert resp.status_code == 200

    def test_both_pipelines_are_available_in_the_same_session(self, client):
        # The literal meaning of "split": one session, both endpoints.
        _, series = _ingest_series(client)
        stream = client.get(f"/api/v1/volume/{series}/data")
        slice_resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&format=png")

        assert stream.status_code == 200
        assert slice_resp.status_code == 200

    def test_coarse_downsample_stream_succeeds(self, client):
        # Split mode's coarse preview is `?downsample=4`; it must be a valid
        # request in hybrid mode, and must report the *strided* geometry.
        _, series = _ingest_series(client, n_slices=8)
        resp = client.get(f"/api/v1/volume/{series}/data?downsample=4")

        assert resp.status_code == 200
        # 8 slices strided by 4 → ceil(8/4) = 2; 32 px strided by 4 → 8.
        assert resp.headers["X-Volume-Slices"] == "2"
        assert resp.headers["X-Volume-Rows"] == "8"
        assert resp.headers["X-Volume-Cols"] == "8"


# =========================================================================== #
#  §4 — Adaptive re-evaluation surface
# =========================================================================== #


class TestMidSessionReevaluation:
    """A client re-negotiating mid-session must see the load it created."""

    def test_second_negotiate_sees_the_accumulated_load(self, client, monkeypatch):
        # Prove the header is live rather than a constant baked at import.
        monkeypatch.setattr(settings, "HYBRID_MAX_SERVER_MPR_SESSIONS", 4)
        _, series = _ingest_series(client)

        first = _negotiate(client, series, webgl_score=30, rtt_ms=5)
        assert first.headers["X-PAXS-Server-Load"] == "0/4"

        with volume_service._VOL_CACHE_LOCK:
            volume_service._ACTIVE_SERVER_SESSIONS["mid-session"] = 1e9

        second = _negotiate(client, series, webgl_score=30, rtt_ms=5)
        assert second.headers["X-PAXS-Server-Load"] == "1/4"

    def test_server_mode_session_registration_moves_the_ratio(self, client, monkeypatch):
        # In server mode /mpr/slice registers a session (§5.3); the next
        # response must reflect it, which is what drives mid-session shedding.
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        monkeypatch.setattr(settings, "HYBRID_MAX_SERVER_MPR_SESSIONS", 9)
        _, series = _ingest_series(client)

        before = client.get(f"/api/v1/volume/{series}/info")
        assert before.headers["X-PAXS-Server-Load"] == "0/9"

        client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&format=png")

        after = client.get(f"/api/v1/volume/{series}/info")
        assert after.headers["X-PAXS-Server-Load"] == "1/9"
