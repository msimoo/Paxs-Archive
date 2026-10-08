"""Phase 4 unit tests — the hybrid decision engine (plan_viewer_process.md §7.1).

``resolve_hybrid_mode`` is pure given its inputs, so every branch is exercised
directly rather than through HTTP: the point of these tests is the *decision*,
and a full request cycle would only add setup noise around the same six lines
of arithmetic.

The parameterised matrix below is deliberately exhaustive on the boundaries —
``webgl_score`` at 0/19/20/39/40/41 and the RTT threshold at exactly
``HYBRID_RTT_THRESHOLD_MS`` — because an off-by-one here silently misroutes a
whole class of workstations, and a misroute is invisible until a user notices
their scroll lag.
"""

from __future__ import annotations

import pytest

from app.api.v1.volume import (
    HYBRID_CLIENT_MIN_SCORE,
    HYBRID_MODES,
    resolve_hybrid_mode,
)
from app.core.config import settings


# A load state that is comfortably below every saturation limit, so the
# decision falls through to the capability/latency branches.
UNLOADED = {"active_server_sessions": 0, "server_vol_cache_mb": 0}


def _resolve(webgl_score, rtt_ms=0, **load):
    return resolve_hybrid_mode(webgl_score, rtt_ms, **(load or UNLOADED))


# =========================================================================== #
#  The returned vocabulary
# =========================================================================== #


def test_resolve_returns_one_of_the_three_hybrid_modes():
    # Whatever the inputs, the engine must answer with a mode the viewer knows.
    for score in range(0, 101, 5):
        for rtt in (0, 50, 500):
            assert _resolve(score, rtt) in HYBRID_MODES


# =========================================================================== #
#  Branch 1 — no WebGL2 at all → server
# =========================================================================== #


@pytest.mark.parametrize("score", [0, -1, -100])
def test_zero_webgl_always_forces_server(score):
    # A browser that cannot make a 3D texture cannot run either local pipeline,
    # so even a saturated server cannot push the work back to it.
    assert _resolve(score, rtt_ms=0) == "server"
    assert _resolve(score, rtt_ms=5000) == "server"
    assert resolve_hybrid_mode(
        score, 5000, active_server_sessions=999, server_vol_cache_mb=99999,
    ) == "server"


def test_zero_webgl_beats_even_the_strongest_latency_case():
    # High RTT normally prefers the client; with no GPU there is no client.
    assert resolve_hybrid_mode(0, 10_000) == "server"


# =========================================================================== #
#  Branch 2 — server saturation
# =========================================================================== #


def test_session_saturation_at_the_limit_pushes_to_client():
    at_limit = {"active_server_sessions": settings.HYBRID_MAX_SERVER_MPR_SESSIONS}
    assert _resolve(50, 0, **at_limit) == "client"


def test_session_saturation_below_the_limit_does_not_trigger():
    below = {"active_server_sessions": settings.HYBRID_MAX_SERVER_MPR_SESSIONS - 1}
    # A strong client is `client` anyway, but for the strong-client reason —
    # prove the saturation branch is not the thing doing the work.
    assert _resolve(50, 0, **below) == "client"
    # A *marginal* client is the discriminating case: unsaturataed → split.
    assert _resolve(30, 0, **below) == "split"


def test_vol_cache_budget_saturation_at_the_limit_pushes_to_client():
    at_budget = {"server_vol_cache_mb": settings.HYBRID_SERVER_VOL_BUDGET_MB}
    assert _resolve(50, 0, **at_budget) == "client"


def test_vol_cache_budget_below_the_limit_does_not_trigger():
    below = {"server_vol_cache_mb": settings.HYBRID_SERVER_VOL_BUDGET_MB - 1}
    assert _resolve(30, 0, **below) == "split"


def test_saturation_with_a_barely_capable_client_stays_on_the_server():
    # Shedding load onto a client that cannot carry it just moves the stall.
    saturated = {"active_server_sessions": settings.HYBRID_MAX_SERVER_MPR_SESSIONS}
    assert _resolve(HYBRID_CLIENT_MIN_SCORE - 1, 0, **saturated) == "server"
    assert _resolve(0, 0, **saturated) == "server"


def test_saturation_with_a_capable_client_moves_work_off_the_server():
    saturated = {"server_vol_cache_mb": settings.HYBRID_SERVER_VOL_BUDGET_MB}
    assert _resolve(HYBRID_CLIENT_MIN_SCORE, 0, **saturated) == "client"
    assert _resolve(100, 0, **saturated) == "client"


def test_saturation_is_checked_before_the_strong_client_branch():
    # Both would say `client`, but with a *weak* client only saturation can
    # legally produce `client` (score 20 < threshold). That proves ordering.
    saturated_cache = {"server_vol_cache_mb": settings.HYBRID_SERVER_VOL_BUDGET_MB}
    weak = HYBRID_CLIENT_MIN_SCORE
    assert weak < settings.HYBRID_CLIENT_SCORE_THRESHOLD, "precondition"
    assert _resolve(weak, 0, **saturated_cache) == "client"


# =========================================================================== #
#  Branch 3 — strong client
# =========================================================================== #


@pytest.mark.parametrize("score", [40, 41, 60, 85, 100])
def test_strong_client_gets_the_full_local_pipeline(score):
    assert _resolve(score, rtt_ms=0) == "client"


def test_score_exactly_at_the_threshold_is_client():
    # The plan writes `score >= threshold`, so the boundary belongs to client.
    assert _resolve(settings.HYBRID_CLIENT_SCORE_THRESHOLD, 0) == "client"
    assert _resolve(settings.HYBRID_CLIENT_SCORE_THRESHOLD - 1, 0) != "client"


# =========================================================================== #
#  Branch 4 — high latency
# =========================================================================== #


@pytest.mark.parametrize("score", [20, 25, 39])
def test_high_latency_promotes_a_marginal_client_to_client(score):
    # A slow link punishes *every* server-rendered slice, so even an adequate
    # (not strong) GPU is the faster path.
    assert score < settings.HYBRID_CLIENT_SCORE_THRESHOLD, "precondition"
    assert _resolve(score, settings.HYBRID_RTT_THRESHOLD_MS + 1) == "client"


def test_latency_exactly_at_the_threshold_does_not_promote():
    # The plan writes `rtt_ms > threshold`, so equality is NOT high latency.
    assert _resolve(30, settings.HYBRID_RTT_THRESHOLD_MS) == "split"


def test_high_latency_does_not_promote_a_sub_minimum_client():
    assert _resolve(HYBRID_CLIENT_MIN_SCORE - 1, 9999) == "server"


def test_high_latency_is_irrelevant_for_a_strong_client():
    # Already client for capability reasons; the RTT must not change that.
    assert _resolve(90, 9999) == "client"


# =========================================================================== #
#  Branch 5 — marginal client → split
# =========================================================================== #


@pytest.mark.parametrize("score", [20, 21, 30, 39])
def test_marginal_client_gets_split_processing(score):
    assert HYBRID_CLIENT_MIN_SCORE <= score < settings.HYBRID_CLIENT_SCORE_THRESHOLD
    assert _resolve(score, rtt_ms=0) == "split"


def test_the_minimum_score_boundary_belongs_to_split():
    assert _resolve(HYBRID_CLIENT_MIN_SCORE, 0) == "split"
    assert _resolve(HYBRID_CLIENT_MIN_SCORE - 1, 0) == "server"


# =========================================================================== #
#  Branch 6 — default
# =========================================================================== #


@pytest.mark.parametrize("score", [1, 5, 10, 19])
def test_weak_but_present_webgl_defaults_to_server(score):
    # Above zero (so not the "no WebGL" branch) but below the split floor.
    assert _resolve(score, rtt_ms=0) == "server"


# =========================================================================== #
#  Full decision matrix — the regression net
# =========================================================================== #


@pytest.mark.parametrize(
    "label,score,rtt,load,expected",
    [
        # --- no WebGL2 ---------------------------------------------------
        ("no_webgl",                        0,    0, UNLOADED, "server"),
        ("no_webgl_high_rtt",               0, 5000, UNLOADED, "server"),
        # --- server saturation -------------------------------------------
        ("saturated_sessions_strong",      80,    0,
         {"active_server_sessions": 20}, "client"),
        ("saturated_sessions_weak",         5,    0,
         {"active_server_sessions": 20}, "server"),
        ("saturated_cache_marginal",       25,    0,
         {"server_vol_cache_mb": 4096}, "client"),
        ("saturated_cache_weak",            5,    0,
         {"server_vol_cache_mb": 4096}, "server"),
        # --- strong client -----------------------------------------------
        ("strong_at_threshold",            40,    0, UNLOADED, "client"),
        ("strong_above_threshold",         75,    0, UNLOADED, "client"),
        # --- high latency ------------------------------------------------
        ("latency_promotes_marginal",      25, 1000, UNLOADED, "client"),
        ("latency_at_threshold_no",        25,  150, UNLOADED, "split"),
        ("latency_cannot_save_weak",        5, 1000, UNLOADED, "server"),
        # --- marginal → split --------------------------------------------
        ("split_floor",                    20,    0, UNLOADED, "split"),
        ("split_mid",                      35,    0, UNLOADED, "split"),
        ("split_ceiling",                  39,    0, UNLOADED, "split"),
        # --- weak → server -----------------------------------------------
        ("weak_one",                        1,    0, UNLOADED, "server"),
        ("weak_below_split_floor",         19,    0, UNLOADED, "server"),
    ],
)
def test_decision_matrix(label, score, rtt, load, expected):
    assert resolve_hybrid_mode(score, rtt, **load) == expected, label


# =========================================================================== #
#  Live-state defaults
# =========================================================================== #


def test_load_defaults_are_read_from_live_process_state(monkeypatch):
    """Omitting the load kwargs must consult the real session/cache counters."""
    calls = {"sessions": 0, "cache": 0}

    def fake_sessions():
        calls["sessions"] += 1
        return 0

    def fake_cache():
        calls["cache"] += 1
        return 0

    monkeypatch.setattr(
        "app.api.v1.volume.volume_service.active_server_session_count", fake_sessions,
    )
    monkeypatch.setattr(
        "app.api.v1.volume.volume_service.estimated_vol_cache_mb", fake_cache,
    )
    assert resolve_hybrid_mode(80, 0) == "client"
    assert calls == {"sessions": 1, "cache": 1}, "both counters must be consulted"


def test_explicit_zero_load_overrides_live_state(monkeypatch):
    """An explicit 0 must NOT fall through to the live (possibly high) value."""
    monkeypatch.setattr(
        "app.api.v1.volume.volume_service.active_server_session_count", lambda: 999,
    )
    monkeypatch.setattr(
        "app.api.v1.volume.volume_service.estimated_vol_cache_mb", lambda: 99999,
    )
    # Explicit 0 means "nothing loaded", so a marginal client gets split, not
    # the saturation branch's `client`.
    assert resolve_hybrid_mode(25, 0, active_server_sessions=0, server_vol_cache_mb=0) == "split"


# =========================================================================== #
#  Configurability — thresholds must be honoured, not hard-coded
# =========================================================================== #


def test_client_score_threshold_is_read_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "HYBRID_CLIENT_SCORE_THRESHOLD", 70)
    assert resolve_hybrid_mode(70, 0, **UNLOADED) == "client"
    assert resolve_hybrid_mode(69, 0, **UNLOADED) == "split"


def test_rtt_threshold_is_read_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "HYBRID_RTT_THRESHOLD_MS", 10)
    assert resolve_hybrid_mode(25, 11, **UNLOADED) == "client"
    assert resolve_hybrid_mode(25, 10, **UNLOADED) == "split"


def test_session_cap_is_read_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "HYBRID_MAX_SERVER_MPR_SESSIONS", 2)
    assert resolve_hybrid_mode(25, 0, active_server_sessions=2, server_vol_cache_mb=0) == "client"
    assert resolve_hybrid_mode(25, 0, active_server_sessions=1, server_vol_cache_mb=0) == "split"


def test_vol_budget_is_read_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "HYBRID_SERVER_VOL_BUDGET_MB", 100)
    assert resolve_hybrid_mode(25, 0, active_server_sessions=0, server_vol_cache_mb=100) == "client"
    assert resolve_hybrid_mode(25, 0, active_server_sessions=0, server_vol_cache_mb=99) == "split"


# =========================================================================== #
#  Back-compat wrapper
# =========================================================================== #


def test_negotiate_hybrid_wrapper_still_answers_client_or_server():
    """Phase 1's `_negotiate_hybrid` must keep working for its call sites."""
    from app.api.v1.volume import _negotiate_hybrid

    # Only the exhaustive branch set can produce 'split'; the wrapper is a
    # straight pass-through, so pin that it returns the same answer.
    for score in (0, 10, 25, 40, 90):
        assert _negotiate_hybrid(score, 0) == resolve_hybrid_mode(score, 0)
