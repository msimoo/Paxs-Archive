"""Phase 5 tests — per-role processing overrides & viewer telemetry (§5.1–§5.5).

Scope of this file:

  * **§5.1 — role override priority.** ``VIEWER_PROCESSING_ROLE_MAP`` must win
    over the global ``VIEWER_PROCESSING_MODE``; a role mapped to ``hybrid`` must
    still run the normal dynamic engine (it means "let the engine decide for
    this role", *not* "pin to a fixed pipeline"); an unmapped role must fall
    through to the global mode untouched.
  * **§5.1 — fail-safe parsing.** The role map is free text an administrator
    types into a JSON textarea. Empty, malformed JSON, a JSON array/scalar, and
    unknown mode values must all degrade to "no overrides" with a *warning*, and
    must never surface as a 500 on ``/negotiate``. Covered both at the unit
    level (``parse_role_map``) and end to end over HTTP.
  * **§5.4 — audit trail.** Exactly one structured ``Viewer mode negotiated:``
    INFO record per negotiation, carrying series / configured / effective /
    role / reason — and carrying *no* patient identifiers and *no* filesystem
    paths.
  * **§5.2 + §5.3 — monitoring.** ``GET /api/v1/dashboard/stats`` exposes the
    live ``viewer_processing`` block the hybrid engine actually load-balances on
    (active server sessions, materialised volume cache) plus the configured
    thresholds, and reflects a real registered session.

Regression intent: Phase 1 already covered a two-entry role map; Phase 5 is the
proof that the map is now *parsed defensively*, that every consumer (endpoint
guards, negotiate payload, dashboard) shares one implementation, and that its
resolution is disclosed rather than silent.
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pytest
from pydicom.uid import generate_uid

from app.core.config import settings
from app.services import volume_service
from app.api.v1 import volume as volume_module
from app.api.v1.volume import (
    parse_role_map,
    resolve_mode_detail,
    resolve_role_override,
)
from tests.helpers import (
    auth_for_role,
    build_stow_body,
    ds_bytes,
    make_instance,
    stow_headers,
)

AXIAL_ORIENTATION = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]

# The dashboard lives on the bare ``/api/v1`` router (``app/api/v1/dashboard.py``),
# not under an ``/api/v1/dashboard`` prefix.
STATS_URL = "/api/v1/stats"


class _CaptureHandler(logging.Handler):
    """Collect records from a specific logger into a list.

    ``caplog`` is not usable here: ``app.main`` calls ``logging.basicConfig``
    at import time, which installs a root handler and (because pytest's own
    capture is configured at the root) leaves the ``paxs.*`` loggers' records
    outside the fixture's list.  Attaching directly to the logger that emits
    the audit line is both deterministic and scoped to the logger under test.
    """

    def __init__(self, logger: logging.Logger):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._logger = logger
        self._previous_level = logger.level

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.records.append(record)

    def __enter__(self):
        self._logger.addHandler(self)
        self._logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self)
        self._logger.setLevel(self._previous_level)
        return False

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]

    def containing(self, needle: str) -> list[str]:
        return [m for m in self.messages() if needle in m]


@pytest.fixture()
def audit_log():
    """Capture ``paxs.volume`` records — the sink ``log_mode_decision`` writes to."""
    with _CaptureHandler(logging.getLogger("paxs.volume")) as cap:
        yield cap


@pytest.fixture()
def map_log():
    """Capture ``paxs.api.volume`` records — where ``parse_role_map`` warns."""
    with _CaptureHandler(logging.getLogger("paxs.api.volume")) as cap:
        yield cap


# Settings touched by this suite — snapshotted/restored around every test so the
# role-map mutations cannot leak into another module's collection order.
_SETTING_KEYS = (
    "VIEWER_PROCESSING_MODE",
    "VIEWER_PROCESSING_ROLE_MAP",
    "VOLUME_STREAMING_ENABLED",
    "HYBRID_CLIENT_SCORE_THRESHOLD",
    "HYBRID_SERVER_VOL_BUDGET_MB",
    "HYBRID_RTT_THRESHOLD_MS",
    "HYBRID_MAX_SERVER_MPR_SESSIONS",
)


@pytest.fixture(autouse=True)
def _isolate_viewer_state():
    """Restore settings and clear module-level session/volume caches per test.

    ``_ACTIVE_SERVER_SESSIONS`` / ``_VOL_CACHE`` are process-global, so an
    absolute-count assertion (dashboard telemetry) must not be able to observe a
    session another test left behind.
    """
    snap = {k: getattr(settings, k, None) for k in _SETTING_KEYS}
    with volume_service._VOL_CACHE_LOCK:
        volume_service._ACTIVE_SERVER_SESSIONS.clear()
        volume_service._VOL_CACHE.clear()
    yield
    for k, v in snap.items():
        setattr(settings, k, v)
    with volume_service._VOL_CACHE_LOCK:
        volume_service._ACTIVE_SERVER_SESSIONS.clear()
        volume_service._VOL_CACHE.clear()


@pytest.fixture()
def client(auth_client):
    """Authenticated client.

    ``/negotiate`` and ``/mpr/slice`` both sit behind ``require_series_read``,
    which 401s an anonymous caller (a share link is the only exception).  Every
    sibling suite overrides ``client`` the same way; the bare ``conftest.client``
    is anonymous on purpose and cannot drive these routes.
    """
    return auth_client


def _ingest_series(client, n_slices: int = 2):
    """STOW a tiny synthetic CT stack; returns (study_uid, series_uid)."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    datasets = []
    for i in range(n_slices):
        pixel = np.full((16, 16), 100 + i * 10, dtype=np.uint16)
        ds = make_instance(
            patient_id="ROLEMAP_PAT",
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


def _negotiate(client, series_uid: str, headers=None, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/api/v1/volume/{series_uid}/negotiate"
    if qs:
        url += "?" + qs
    return client.get(url, headers=headers)


class _Role:
    """Minimal stand-in for ``User`` — ``resolve_*`` only reads ``.role``."""

    def __init__(self, role):
        self.role = role


# =========================================================================== #
#  §5.1 — parse_role_map: defensive parsing of administrator free text
# =========================================================================== #


class TestParseRoleMap:
    """``parse_role_map`` must never raise, whatever the operator typed."""

    def test_empty_string_yields_no_overrides(self, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "")
        assert parse_role_map() == {}

    def test_whitespace_only_yields_no_overrides(self, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "   \n\t ")
        assert parse_role_map() == {}

    def test_none_yields_no_overrides(self, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", None)
        assert parse_role_map() == {}

    def test_valid_map_is_upper_cased(self, monkeypatch):
        monkeypatch.setattr(
            settings,
            "VIEWER_PROCESSING_ROLE_MAP",
            json.dumps({"radiologist": "client", "physician": "server"}),
        )
        assert parse_role_map() == {"RADIOLOGIST": "client", "PHYSICIAN": "server"}

    def test_role_entry_is_stripped(self, monkeypatch):
        """A role pasted with surrounding whitespace still matches the caller."""
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP", json.dumps({"  RADIOLOGIST ": "Client"})
        )
        assert parse_role_map() == {"RADIOLOGIST": "client"}

    def test_malformed_json_warns_and_returns_empty(self, monkeypatch, map_log):
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP", "NOT VALID JSON{{{"
        )
        result = parse_role_map()

        assert result == {}
        assert map_log.containing("VIEWER_PROCESSING_ROLE_MAP is not valid JSON"), (
            f"expected a JSON warning, got {map_log.messages()}"
        )

    def test_trailing_comma_is_rejected_safely(self, monkeypatch, map_log):
        """The classic JSON textarea typo must be a warning, not an exception."""
        monkeypatch.setattr(
            settings,
            "VIEWER_PROCESSING_ROLE_MAP",
            '{"RADIOLOGIST": "client",}',
        )
        assert parse_role_map() == {}
        assert map_log.containing("not valid JSON")

    def test_json_array_warns_and_returns_empty(self, monkeypatch, map_log):
        """A JSON array parses fine but is not a role→mode object."""
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP", '["RADIOLOGIST", "client"]'
        )
        result = parse_role_map()

        assert result == {}
        assert map_log.containing("must be a JSON object")

    def test_bare_string_warns_and_returns_empty(self, monkeypatch, map_log):
        """``"RADIOLOGIST"`` is valid JSON but not a mapping."""
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP", '"RADIOLOGIST"'
        )
        assert parse_role_map() == {}
        assert map_log.containing("must be a JSON object")

    def test_unknown_mode_values_are_dropped(self, monkeypatch):
        """A typo'd mode ('clinet') must not pin a role to a bogus pipeline."""
        monkeypatch.setattr(
            settings,
            "VIEWER_PROCESSING_ROLE_MAP",
            json.dumps({"RADIOLOGIST": "clinet", "PHYSICIAN": "server"}),
        )
        assert parse_role_map() == {"PHYSICIAN": "server"}

    def test_non_string_values_are_dropped(self, monkeypatch):
        """Nested objects / numbers cannot be a mode — skip, don't crash."""
        monkeypatch.setattr(
            settings,
            "VIEWER_PROCESSING_ROLE_MAP",
            json.dumps({"RADIOLOGIST": ["client"], "PHYSICIAN": 3, "ADMIN": "client"}),
        )
        assert parse_role_map() == {"ADMIN": "client"}

    def test_map_with_no_usable_entries_yields_empty(self, monkeypatch):
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP", json.dumps({"RADIOLOGIST": "nope"})
        )
        assert parse_role_map() == {}


# =========================================================================== #
#  §5.1 — resolve_role_override: the "no override" cases are one decision
# =========================================================================== #


class TestResolveRoleOverride:
    @pytest.mark.parametrize(
        "role_map, role, expected",
        [
            # Mapped roles → their pinned mode.
            ('{"RADIOLOGIST": "client"}', "RADIOLOGIST", "client"),
            ('{"RADIOLOGIST": "client"}', "radiologist", "client"),
            ('{"PHYSICIAN": "server"}', "PHYSICIAN", "server"),
            ('{"ADMIN": "hybrid"}', "ADMIN", "hybrid"),
            # Unmapped role → None (fall through to the global mode).
            ('{"RADIOLOGIST": "client"}', "PHYSICIAN", None),
            ('{"RADIOLOGIST": "client"}', "TECHNOLOGIST", None),
            # Empty map → None for every role.
            ("{}", "RADIOLOGIST", None),
            # Whitespace-padded role the caller supplied still matches.
            ('{"RADIOLOGIST": "client"}', "  RADIOLOGIST  ", "client"),
        ],
    )
    def test_override_lookup(self, monkeypatch, role_map, role, expected):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", role_map)
        assert resolve_role_override(_Role(role)) == expected

    def test_anonymous_user_has_no_override(self, monkeypatch):
        """A share-link viewer is ``None`` — never an AttributeError."""
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP", '{"RADIOLOGIST": "client"}'
        )
        assert resolve_role_override(None) is None

    @pytest.mark.parametrize("role", [None, "", "   "])
    def test_missing_or_blank_role_has_no_override(self, monkeypatch, role):
        """A token without a role claim falls through, it does not crash."""
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP", '{"RADIOLOGIST": "client"}'
        )
        assert resolve_role_override(_Role(role)) is None

    def test_malformed_map_degrades_to_no_override(self, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "{{{ not json")
        assert resolve_role_override(_Role("RADIOLOGIST")) is None

    def test_empty_map_short_circuits(self, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "")
        assert resolve_role_override(_Role("ADMIN")) is None


# =========================================================================== #
#  §5.1 — resolve_mode_detail: override priority + explanation
# =========================================================================== #


class TestResolveModeDetail:
    def test_override_wins_over_global_mode(self, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP", '{"RADIOLOGIST": "client"}'
        )
        detail = resolve_mode_detail(_Role("RADIOLOGIST"))

        assert detail["mode"] == "client"
        assert detail["override"] == "client"
        assert detail["global_mode"] == "hybrid"
        assert detail["role"] == "RADIOLOGIST"

    def test_unmapped_role_uses_global_mode(self, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP", '{"RADIOLOGIST": "client"}'
        )
        detail = resolve_mode_detail(_Role("PHYSICIAN"))

        assert detail["mode"] == "server"
        assert detail["override"] is None
        assert detail["global_mode"] == "server"
        assert detail["role"] == "PHYSICIAN"

    def test_mapped_to_hybrid_reports_hybrid_not_a_fixed_pipeline(self, monkeypatch):
        """Mapping a role to ``hybrid`` = "let the engine decide for this role"."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", '{"ADMIN": "hybrid"}')
        detail = resolve_mode_detail(_Role("ADMIN"))

        assert detail["mode"] == "hybrid"
        assert detail["override"] == "hybrid"
        assert detail["global_mode"] == "server"

    def test_malformed_map_falls_back_to_global_mode(self, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "broken {")
        detail = resolve_mode_detail(_Role("RADIOLOGIST"))

        assert detail["mode"] == "client"
        assert detail["override"] is None

    def test_unknown_global_mode_coerces_to_hybrid(self, monkeypatch):
        """A typo'd global setting must not become the resolved mode."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "wat")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "")
        assert resolve_mode_detail(_Role("ADMIN"))["mode"] == "hybrid"

    def test_anonymous_user_is_reported_as_role_none(self, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "")
        detail = resolve_mode_detail(None)

        assert detail["mode"] == "hybrid"
        assert detail["override"] is None
        assert detail["role"] is None


# =========================================================================== #
#  §5.1 — role override priority over HTTP (/negotiate)
# =========================================================================== #


class TestNegotiateRoleOverridePriority:
    """End-to-end: the override must reach the wire *and* be disclosed."""

    def test_radiologist_mapped_to_client_gets_client(
        self, client, auth_headers, monkeypatch
    ):
        """RADIOLOGIST → client even when the global mode and score say server."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(
            settings,
            "VIEWER_PROCESSING_ROLE_MAP",
            json.dumps({"RADIOLOGIST": "client", "PHYSICIAN": "server"}),
        )
        _, series = _ingest_series(client)
        headers = auth_for_role(client, "rolemap_rad", role="RADIOLOGIST", admin=auth_headers)

        # webgl_score=0 would otherwise resolve to 'server' — the override wins.
        resp = _negotiate(client, series, headers=headers, webgl_score=0)

        assert resp.status_code == 200
        body = resp.json()
        assert body["configured_mode"] == "client"
        assert body["effective_mode"] == "client"
        assert resp.headers["X-PAXS-Processing-Mode"] == "client"
        assert resp.headers["X-PAXS-Role-Override"] == "RADIOLOGIST"

    def test_physician_mapped_to_server_gets_server(
        self, client, auth_headers, monkeypatch
    ):
        """PHYSICIAN → server even when the client is strong and RTT is tiny."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(
            settings,
            "VIEWER_PROCESSING_ROLE_MAP",
            json.dumps({"RADIOLOGIST": "client", "PHYSICIAN": "server"}),
        )
        _, series = _ingest_series(client)
        headers = auth_for_role(client, "rolemap_phys", role="PHYSICIAN", admin=auth_headers)

        resp = _negotiate(client, series, headers=headers, webgl_score=99, rtt_ms=1)

        assert resp.status_code == 200
        body = resp.json()
        assert body["configured_mode"] == "server"
        assert body["effective_mode"] == "server"
        assert resp.headers["X-PAXS-Role-Override"] == "PHYSICIAN"

    def test_override_is_disclosed_in_the_payload(self, client, auth_headers, monkeypatch):
        """A support engineer must be able to tell "my role pinned this" apart
        from "the engine chose this" without reading the settings file."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", '{"RADIOLOGIST": "client"}')
        _, series = _ingest_series(client)
        headers = auth_for_role(client, "rolemap_payload", role="RADIOLOGIST", admin=auth_headers)

        body = _negotiate(client, series, headers=headers, webgl_score=5).json()

        assert body["role_override"] == {
            "role": "RADIOLOGIST",
            "mapped_mode": "client",
            "global_mode": "hybrid",
        }

    def test_no_override_key_when_the_role_is_unmapped(self, client, monkeypatch):
        """An unmapped role negotiates normally and carries no override claim."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", '{"RADIOLOGIST": "client"}')
        _, series = _ingest_series(client)

        resp = _negotiate(client, series, webgl_score=90)

        assert resp.status_code == 200
        body = resp.json()
        assert "role_override" not in body
        assert "X-PAXS-Role-Override" not in resp.headers

    def test_role_mapped_to_hybrid_runs_the_dynamic_engine(
        self, client, auth_headers, monkeypatch
    ):
        """Mapping a role to ``hybrid`` must NOT pin it — the engine decides."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", '{"ADMIN": "hybrid"}')
        _, series = _ingest_series(client)
        headers = auth_for_role(client, "rolemap_admin", role="ADMIN", admin=auth_headers)

        strong = _negotiate(client, series, headers=headers, webgl_score=90, rtt_ms=5).json()
        weak = _negotiate(client, series, headers=headers, webgl_score=5, rtt_ms=5).json()

        # Both are 'hybrid' as the configured mode, but they resolve differently:
        # that difference is the proof the engine ran rather than being pinned.
        assert strong["configured_mode"] == "hybrid"
        assert strong["effective_mode"] == "client"
        assert weak["configured_mode"] == "hybrid"
        assert weak["effective_mode"] == "server"
        assert strong["role_override"]["mapped_mode"] == "hybrid"

    def test_role_override_takes_precedence_over_global_client_mode(
        self, client, auth_headers, monkeypatch
    ):
        """An override applies in *both* directions, not only away from hybrid."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", '{"PHYSICIAN": "server"}')
        _, series = _ingest_series(client)
        headers = auth_for_role(client, "rolemap_phys2", role="PHYSICIAN", admin=auth_headers)

        body = _negotiate(client, series, headers=headers, webgl_score=99).json()

        assert body["configured_mode"] == "server"
        assert body["role_override"]["global_mode"] == "client"


# =========================================================================== #
#  §5.1 — fail-safe parsing over HTTP (no 500, ever)
# =========================================================================== #


class TestNegotiateFailSafeRoleMap:
    """A broken role map is a *configuration* problem, not a request failure."""

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "NOT VALID JSON{{{",
            '{"RADIOLOGIST": "client",}',  # trailing comma
            '["RADIOLOGIST", "client"]',   # right shape, wrong type
            '"RADIOLOGIST"',               # scalar
            "null",                        # parses to None
            "12",                          # parses to a number
            '{"RADIOLOGIST": "clinet"}',   # unknown mode value
        ],
    )
    def test_broken_role_map_never_breaks_negotiate(
        self, client, auth_headers, monkeypatch, raw
    ):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", raw)
        _, series = _ingest_series(client)
        headers = auth_for_role(
            client, f"rolemap_broken_{abs(hash(raw))}", role="RADIOLOGIST", admin=auth_headers
        )

        resp = _negotiate(client, series, headers=headers, webgl_score=50)

        assert resp.status_code == 200, f"raw={raw!r} -> {resp.status_code} {resp.text}"
        body = resp.json()
        # No override survived → the global mode applies and the engine runs.
        assert body["configured_mode"] == "hybrid"
        assert "role_override" not in body
        assert "X-PAXS-Role-Override" not in resp.headers

    def test_partially_valid_map_keeps_the_valid_entries(
        self, client, auth_headers, monkeypatch
    ):
        """One typo'd entry must not discard the whole map."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(
            settings,
            "VIEWER_PROCESSING_ROLE_MAP",
            json.dumps({"RADIOLOGIST": "clinet", "PHYSICIAN": "server"}),
        )
        _, series = _ingest_series(client)
        headers = auth_for_role(client, "rolemap_partial", role="PHYSICIAN", admin=auth_headers)

        body = _negotiate(client, series, headers=headers, webgl_score=99).json()

        assert body["configured_mode"] == "server"
        assert body["role_override"]["mapped_mode"] == "server"


# =========================================================================== #
#  §5.4 — the endpoint guards share the same resolver as negotiate
# =========================================================================== #


class TestGuardsHonourRoleOverride:
    """A role pinned to ``client`` must trip the Phase 3 server-slicing guard
    even though the *global* mode is ``hybrid`` — proof the guards read the
    resolved mode, not the setting."""

    def test_role_pinned_to_client_trips_server_slicing_guard(
        self, client, auth_headers, monkeypatch
    ):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", '{"RADIOLOGIST": "client"}')
        _, series = _ingest_series(client)
        headers = auth_for_role(client, "rolemap_guard", role="RADIOLOGIST", admin=auth_headers)

        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?axis=z&index=0", headers=headers
        )

        assert resp.status_code == 503
        assert resp.headers.get("X-PAXS-Mode-Hint") == "client"

    def test_same_series_is_still_serviceable_for_an_unmapped_role(
        self, client, auth_headers, monkeypatch
    ):
        """The guard is per-caller: the series itself is unchanged."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", '{"PHYSICIAN": "server"}')
        _, series = _ingest_series(client)
        headers = auth_for_role(client, "rolemap_guard2", role="PHYSICIAN", admin=auth_headers)

        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?axis=z&index=0", headers=headers
        )

        assert resp.status_code == 200
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_force_bypass_still_wins_for_an_overridden_role(
        self, client, auth_headers, monkeypatch
    ):
        """``?force=1`` remains the documented escape hatch for any caller."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", '{"RADIOLOGIST": "client"}')
        _, series = _ingest_series(client)
        headers = auth_for_role(client, "rolemap_force", role="RADIOLOGIST", admin=auth_headers)

        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?axis=z&index=0&force=1", headers=headers
        )

        assert resp.status_code == 200
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


# =========================================================================== #
#  §5.4 — structured audit trail
# =========================================================================== #


class TestAuditTrail:
    """One greppable INFO line per negotiation, carrying no PHI and no paths."""

    def test_negotiate_emits_one_structured_audit_line(
        self, client, audit_log, monkeypatch
    ):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "")
        _, series = _ingest_series(client)

        assert _negotiate(client, series, webgl_score=90, rtt_ms=5).status_code == 200

        lines = audit_log.containing("Viewer mode negotiated")
        assert len(lines) == 1, f"expected exactly one audit line, got {lines}"

        msg = lines[0]
        assert f"series={series}" in msg
        assert "configured=hybrid" in msg
        assert "effective=" in msg
        assert "role=" in msg
        assert "reason=" in msg

    def test_audit_line_records_the_role_override_reason(
        self, client, auth_headers, audit_log, monkeypatch
    ):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", '{"RADIOLOGIST": "client"}')
        _, series = _ingest_series(client)
        headers = auth_for_role(client, "rolemap_audit", role="RADIOLOGIST", admin=auth_headers)

        assert _negotiate(client, series, headers=headers, webgl_score=10).status_code == 200

        msg = audit_log.containing("Viewer mode negotiated")[0]
        assert "role=RADIOLOGIST" in msg
        assert "reason=role_override:RADIOLOGIST->client" in msg

    def test_share_link_negotiation_is_labelled_anonymous(
        self, client, audit_log, monkeypatch
    ):
        """A share-link viewer has no role claim — the audit line says ``anonymous``.

        Not merely cosmetic, and deliberately awkward to reach:

        * ``/negotiate`` sits behind ``require_series_read``, which rejects an
          anonymous caller *unless* a real patient-share token covers exactly
          this study — so the token is minted through the API, not faked.
        * The ``anonymous_client`` fixture is **not** sufficient here: it wraps
          the same underlying ``TestClient`` that ``auth_client`` logged in on,
          so the ``AUTH_COOKIE_NAME`` refresh cookie is set and
          ``get_optional_user`` still resolves the ADMIN. A separate
          ``TestClient`` instance is the only way to be genuinely anonymous.
        """
        from starlette.testclient import TestClient

        from app.main import app

        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "")
        study_uid, series = _ingest_series(client)

        created = client.post("/api/v1/share", json={"study_uid": study_uid, "expiry_days": 1})
        assert created.status_code in (200, 201), created.text
        token = created.json()["token"]

        with TestClient(app) as fresh:
            resp = _negotiate(fresh, series, share=token, webgl_score=10)

        assert resp.status_code == 200, resp.text
        msg = audit_log.containing("Viewer mode negotiated")[0]
        assert "role=anonymous" in msg

    def test_audit_line_carries_no_patient_identifiers_and_no_paths(
        self, client, audit_log, monkeypatch
    ):
        """The log is not a clinical record — no PHI, no filesystem paths.

        The ingest fixture stamps the patient id as ``ROLEMAP_PAT``; the audit
        line must not echo it, nor may it contain a drive letter / storage path.
        """
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "")
        _, series = _ingest_series(client)

        assert _negotiate(client, series, webgl_score=90).status_code == 200

        msg = audit_log.containing("Viewer mode negotiated")[0]
        bad = ["ROLEMAP_PAT", "PatientName", "patient", "MRN"]
        for token in bad:
            assert token not in msg, f"sensitive token {token!r} leaked into audit line: {msg}"
        assert "STORAGE_ROOT" not in msg
        assert "\\" not in msg and ":/" not in msg
        # The vocabulary that *should* be there, for contrast.
        assert msg.startswith("Viewer mode negotiated:")

    def test_malformed_map_warns_on_the_negotiation_that_reads_it(
        self, client, map_log, monkeypatch
    ):
        """A broken map logs its one warning on the negotiation that parses it."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "broken{{")
        _, series = _ingest_series(client)

        assert _negotiate(client, series, webgl_score=90).status_code == 200

        assert map_log.containing("VIEWER_PROCESSING_ROLE_MAP is not valid JSON")


# =========================================================================== #
#  §5.2 + §5.3 — dashboard telemetry
# =========================================================================== #


class TestDashboardViewerTelemetry:
    URL = STATS_URL

    def _viewer_block(self, resp):
        body = resp.json()
        assert "features" in body
        assert "viewer_processing" in body["features"]
        return body["features"]["viewer_processing"]

    def test_stats_exposes_the_viewer_processing_block(self, client):
        resp = client.get(self.URL)
        assert resp.status_code == 200
        vp = self._viewer_block(resp)

        for key in (
            "active_server_sessions",
            "server_vol_cache_mb",
            "configured_mode",
            "max_server_sessions",
            "vol_budget_mb",
        ):
            assert key in vp, f"missing telemetry field {key!r}: {vp}"

    def test_telemetry_reports_the_configured_mode_and_thresholds(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        monkeypatch.setattr(settings, "HYBRID_MAX_SERVER_MPR_SESSIONS", 7)
        monkeypatch.setattr(settings, "HYBRID_SERVER_VOL_BUDGET_MB", 2048)

        vp = self._viewer_block(client.get(self.URL))

        assert vp["configured_mode"] == "client"
        assert vp["max_server_sessions"] == 7
        assert vp["vol_budget_mb"] == 2048

    def test_telemetry_reflects_a_registered_server_session(self, client, monkeypatch):
        """The dashboard must expose the *live* count the engine balances on."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        baseline = self._viewer_block(client.get(self.URL))["active_server_sessions"]

        volume_service.register_server_session("1.2.3.ROLEMAP_SESSION")

        vp = self._viewer_block(client.get(self.URL))
        assert vp["active_server_sessions"] == baseline + 1

        volume_service.release_server_session("1.2.3.ROLEMAP_SESSION")
        vp_after = self._viewer_block(client.get(self.URL))
        assert vp_after["active_server_sessions"] == baseline

    def test_telemetry_counts_role_overrides_by_mode(self, client, monkeypatch):
        monkeypatch.setattr(
            settings,
            "VIEWER_PROCESSING_ROLE_MAP",
            json.dumps({"RADIOLOGIST": "client", "PHYSICIAN": "server", "ADMIN": "client"}),
        )

        vp = self._viewer_block(client.get(self.URL))

        assert vp["role_overrides"] == {
            "total": 3,
            "by_mode": {"client": 2, "server": 1},
        }

    def test_telemetry_override_counts_are_zero_without_a_map(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "")
        vp = self._viewer_block(client.get(self.URL))
        assert vp["role_overrides"] == {"total": 0, "by_mode": {}}

    def test_telemetry_survives_a_malformed_role_map(self, client, monkeypatch):
        """Telemetry is the thing an operator checks *because* config broke."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_ROLE_MAP", "{{{ not json")
        resp = client.get(self.URL)

        assert resp.status_code == 200
        assert self._viewer_block(resp)["role_overrides"] == {"total": 0, "by_mode": {}}

    def test_telemetry_reports_streaming_enabled_flag(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VOLUME_STREAMING_ENABLED", True)
        assert self._viewer_block(client.get(self.URL))["streaming_enabled"] is True

        monkeypatch.setattr(settings, "VOLUME_STREAMING_ENABLED", False)
        assert self._viewer_block(client.get(self.URL))["streaming_enabled"] is False

    def test_telemetry_block_is_independent_of_the_volume_streaming_section(
        self, client
    ):
        """The new block must not shadow the pre-existing volume_streaming block."""
        body = client.get(self.URL).json()
        assert "volume_streaming" in body["features"]
        assert "viewer_processing" in body["features"]
        assert body["features"]["viewer_processing"] is not body["features"]["volume_streaming"]


# =========================================================================== #
#  §5.4 — log_mode_decision is the single sink, and it is PHI-free
# =========================================================================== #


class TestLogModeDecision:
    """Unit-level contract for the shared audit sink."""

    def test_emits_one_info_record_with_the_expected_fields(self, audit_log):
        volume_service.log_mode_decision(
            series_uid="1.2.3.SERIES",
            configured_mode="hybrid",
            effective_mode="client",
            role="RADIOLOGIST",
            reason="role_override:RADIOLOGIST->client",
        )

        records = [r for r in audit_log.records if "Viewer mode negotiated" in r.getMessage()]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        msg = records[0].getMessage()
        assert "series=1.2.3.SERIES" in msg
        assert "configured=hybrid" in msg
        assert "effective=client" in msg
        assert "role=RADIOLOGIST" in msg
        assert "reason=role_override:RADIOLOGIST->client" in msg

    def test_none_role_is_rendered_anonymous(self, audit_log):
        volume_service.log_mode_decision(
            series_uid="1.2.3.SERIES",
            configured_mode="hybrid",
            effective_mode="server",
            role=None,
            reason="hybrid:score=0,rtt=0ms->server",
        )

        msg = audit_log.containing("Viewer mode negotiated")[0]
        assert "role=anonymous" in msg

    def test_does_not_log_at_warning_or_above(self, audit_log):
        """A negotiation is normal traffic — it must not pollute error streams."""
        volume_service.log_mode_decision(
            series_uid="1.2.3.SERIES",
            configured_mode="hybrid",
            effective_mode="client",
            role="ADMIN",
            reason="hybrid:score=90,rtt=1ms->client",
        )

        assert all(
            r.levelno < logging.WARNING
            for r in audit_log.records
            if "Viewer mode negotiated" in r.getMessage()
        )


# =========================================================================== #
#  §5.1 — the negotiation reason token is machine-greppable
# =========================================================================== #


class TestNegotiationReason:
    def test_override_reason_names_the_role_and_target(self):
        reason = volume_module._negotiation_reason(
            {"override": "client", "mode": "client", "role": "RADIOLOGIST"},
            webgl_score=10, rtt_ms=5, effective_mode="client",
        )
        assert reason == "role_override:RADIOLOGIST->client"

    def test_global_reason_when_no_override_and_not_hybrid(self):
        reason = volume_module._negotiation_reason(
            {"override": None, "mode": "server", "role": "PHYSICIAN"},
            webgl_score=0, rtt_ms=0, effective_mode="server",
        )
        assert reason == "global:server"

    def test_hybrid_reason_carries_the_inputs_and_outcome(self):
        reason = volume_module._negotiation_reason(
            {"override": None, "mode": "hybrid", "role": None},
            webgl_score=55, rtt_ms=200, effective_mode="split",
        )
        assert reason == "hybrid:score=55,rtt=200ms->split"
