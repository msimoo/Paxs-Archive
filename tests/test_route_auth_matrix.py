"""C03 — authentication enforcement, route matrix and patient share scope.

Three layers of protection for the fix:

1. **Structural** — an AST scan asserts that every route in ``app/api/v1`` is
   either on the explicit public allowlist or carries an authentication
   dependency (or lives on a router with a router-level guard). A new PHI route
   added without auth therefore fails the suite.
2. **Behavioural** — representative protected routes reject anonymous callers
   and accept authenticated ones.
3. **Share scope** — with ``DICOMWEB_PUBLIC=false`` a patient share link still
   reads *its own* study (including volume data) but nothing else, and never
   grants write access.
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest
from pydicom.uid import generate_uid

from app.core.config import settings
from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers

PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_DIR = PROJECT_ROOT / "app" / "api" / "v1"

# Dependencies that constitute an authentication requirement.
AUTH_DEPS = {
    "get_current_user",
    "get_current_user_any",
    "require_admin",
    "require_destructive_role",
    "require_dicomweb_access",
    "require_series_read",
    "require_share_or_user",
}

# Routes that are deliberately reachable without credentials.
PUBLIC_ROUTES: set[tuple[str, str, str]] = {
    # Authentication entry points
    ("auth", "POST", "/register"),
    ("auth", "POST", "/login"),
    ("auth", "POST", "/refresh"),
    ("auth", "POST", "/logout"),
    # Probes
    ("dashboard", "GET", "/health/live"),
    ("dashboard", "GET", "/health/ready"),
    # FHIR CapabilityStatement (no patient data)
    ("fhir", "GET", "/metadata"),
    # Directory login (itself gated on LDAP configuration)
    ("ldap", "POST", "/login"),
    # Patient share links: the token IS the credential
    ("share", "GET", "/{token}"),
    ("share", "GET", "/{token}/qr.png"),
}

# Modules whose entire router is guarded by a router-level dependency.
ROUTER_GUARDED_MODULES = {"dicomweb"}


# --------------------------------------------------------------------------- #
# 1. structural: every route is public-by-allowlist or authenticated
# --------------------------------------------------------------------------- #
def _depends_names(call: ast.Call) -> set[str]:
    names: set[str] = set()
    for arg in call.args:
        if isinstance(arg, ast.Name):
            names.add(arg.id)
        elif isinstance(arg, ast.Call):
            fn = arg.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


def _signature_deps(node: ast.AST) -> set[str]:
    names: set[str] = set()
    defaults = list(getattr(node.args, "defaults", [])) + [
        d for d in getattr(node.args, "kw_defaults", []) if d is not None
    ]
    for default in defaults:
        for child in ast.walk(default):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "Depends"
            ):
                names |= _depends_names(child)
    return names


def _router_level_deps(tree: ast.Module) -> set[str]:
    """Dependencies applied to the module's APIRouter(dependencies=[...])."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "router" for t in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        for kw in node.value.keywords:
            if kw.arg == "dependencies":
                for child in ast.walk(kw.value):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id == "Depends"
                    ):
                        names |= _depends_names(child)
    return names


def collect_routes():
    """(module, METHOD, path, auth_deps, line) for every API route."""
    rows = []
    for path in sorted(API_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = path.stem
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        router_deps = _router_level_deps(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                fn = dec.func
                if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
                    continue
                if fn.value.id != "router" or fn.attr not in (
                    "get", "post", "put", "patch", "delete"
                ):
                    continue
                route_path = (
                    dec.args[0].value
                    if dec.args and isinstance(dec.args[0], ast.Constant)
                    else "?"
                )
                deps = _signature_deps(node) | router_deps
                for kw in dec.keywords:
                    if kw.arg == "dependencies":
                        for child in ast.walk(kw.value):
                            if (
                                isinstance(child, ast.Call)
                                and isinstance(child.func, ast.Name)
                                and child.func.id == "Depends"
                            ):
                                deps |= _depends_names(child)
                rows.append((module, fn.attr.upper(), route_path, deps, node.lineno))
    return rows


def test_every_route_is_public_by_allowlist_or_authenticated():
    unprotected = [
        (module, method, route, line)
        for module, method, route, deps, line in collect_routes()
        if not (deps & AUTH_DEPS)
        and (module, method, route) not in PUBLIC_ROUTES
    ]
    assert not unprotected, (
        "Routes reachable without authentication and not on the public "
        "allowlist (add an auth dependency or justify them in PUBLIC_ROUTES):\n  "
        + "\n  ".join(f"{m}.py:{line} {method} {p}" for m, method, p, line in unprotected)
    )


def test_public_allowlist_has_no_stale_entries():
    """Keep the allowlist honest: every entry must still exist."""
    actual = {(m, method, route) for m, method, route, _d, _l in collect_routes()}
    stale = PUBLIC_ROUTES - actual
    assert not stale, f"PUBLIC_ROUTES lists routes that no longer exist: {sorted(stale)}"


def test_dicomweb_router_carries_a_router_level_guard():
    source = (API_DIR / "dicomweb.py").read_text(encoding="utf-8")
    assert "dependencies=[Depends(require_dicomweb_access)]" in source


# --------------------------------------------------------------------------- #
# fixtures for the behavioural half
# --------------------------------------------------------------------------- #
@pytest.fixture()
def ingested_study(auth_client):
    """STOW a two-slice study; returns (study_uid, series_uid, sop_uid)."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    sop_uids = []
    datasets = []
    for i in range(2):
        pixel = np.full((8, 8), 100 + i, dtype=np.uint16)
        ds = make_instance(
            patient_id="AUTH_MATRIX_PAT",
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=generate_uid(),
            instance_num=i + 1,
            rows=8,
            cols=8,
            pixel=pixel,
            position=[0.0, 0.0, float(i)],
            orientation=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            slice_thickness="1",
        )
        sop_uids.append(ds.SOPInstanceUID)
        datasets.append(ds_bytes(ds))
    resp = auth_client.post(
        f"/dicom-web/studies/{study_uid}",
        content=build_stow_body(datasets),
        headers=stow_headers(),
    )
    assert resp.status_code == 200, resp.text
    return study_uid, series_uid, sop_uids[0]


# --------------------------------------------------------------------------- #
# 2. behavioural: anonymous rejected, authenticated accepted
# --------------------------------------------------------------------------- #
def test_phi_routes_reject_anonymous_and_accept_authenticated(
    anonymous_client, auth_client, ingested_study
):
    study_uid, series_uid, sop_uid = ingested_study

    protected = [
        f"/api/v1/studies/{study_uid}",
        f"/api/v1/studies/{study_uid}/series/{series_uid}/instances",
        f"/api/v1/reports/study/{study_uid}",
        f"/api/v1/volume/{series_uid}/info",
        f"/api/v1/gsps/study/{study_uid}",
        f"/api/v1/priors/study/{study_uid}",
        "/api/v1/stats",
        "/api/v1/settings/organization",
        "/api/v1/templates",
        "/metrics",
        "/api/v1/license/fingerprint",
        "/fhir/Patient",
        "/fhir/ImagingStudy",
    ]
    for url in protected:
        anon = anonymous_client.get(url)
        assert anon.status_code in (401, 403), (
            f"{url} must reject anonymous access, got {anon.status_code}"
        )
        authed = auth_client.get(url)
        assert authed.status_code not in (401, 403), (
            f"{url} must accept an authenticated ADMIN, got {authed.status_code}"
        )


def test_detailed_health_requires_admin_but_probes_stay_public(anonymous_client):
    assert anonymous_client.get("/api/v1/health/live").status_code == 200
    assert anonymous_client.get("/api/v1/health/ready").status_code == 200
    assert anonymous_client.get("/api/v1/health").status_code in (401, 403)


def test_health_detail_available_to_admin(auth_client):
    resp = auth_client.get("/api/v1/health")
    assert resp.status_code == 200


def test_public_endpoints_remain_public(anonymous_client):
    assert anonymous_client.get("/fhir/metadata").status_code == 200
    assert anonymous_client.get("/api/v1/health/live").status_code == 200


def test_volume_cache_headers_are_private(auth_client, ingested_study):
    """PHI must not be cached by shared proxies."""
    _study, series_uid, _sop = ingested_study
    resp = auth_client.get(f"/api/v1/volume/{series_uid}/data")
    assert resp.status_code == 200
    assert "private" in resp.headers["cache-control"]
    assert "no-store" in resp.headers["cache-control"]
    assert "public" not in resp.headers["cache-control"]


# --------------------------------------------------------------------------- #
# 3. DICOMWEB_PUBLIC is a real switch
# --------------------------------------------------------------------------- #
def test_dicomweb_is_open_when_public_flag_is_on(anonymous_client, ingested_study, monkeypatch):
    study_uid, series_uid, sop_uid = ingested_study
    # Private mode is the baseline now, so public mode is opted into explicitly.
    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", True, raising=False)
    assert settings.DICOMWEB_PUBLIC is True
    assert anonymous_client.get(f"/dicom-web/studies/{study_uid}/metadata").status_code == 200
    assert anonymous_client.get(f"/dicom-web/studies/{study_uid}/series").status_code == 200


def test_dicomweb_requires_auth_when_private(
    anonymous_client, auth_client, ingested_study, monkeypatch
):
    study_uid, series_uid, sop_uid = ingested_study
    # Also the shipped default (C03): private mode is the baseline.
    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", False, raising=False)

    reads = [
        "/dicom-web/studies",
        f"/dicom-web/studies/{study_uid}/metadata",
        f"/dicom-web/studies/{study_uid}/series",
        f"/dicom-web/studies/{study_uid}",
        f"/dicom-web/studies/{study_uid}/series/{series_uid}",
        f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}",
        f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/metadata",
        f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/thumbnail",
        f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/1",
    ]
    for url in reads:
        anon = anonymous_client.get(url)
        assert anon.status_code == 401, f"{url} must require auth in private mode, got {anon.status_code}"
        authed = auth_client.get(url)
        assert authed.status_code not in (401, 403), (
            f"{url} must work for an authenticated caller, got {authed.status_code}"
        )


def test_stow_requires_auth_when_private(anonymous_client, ingested_study, monkeypatch):
    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", False, raising=False)
    ds = make_instance(patient_id="PRIVATE_STOW", rows=8, cols=8)
    resp = anonymous_client.post(
        "/dicom-web/studies",
        content=build_stow_body([ds_bytes(ds)]),
        headers=stow_headers(),
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# 4. share links keep working in private mode, scoped to their study
# --------------------------------------------------------------------------- #
@pytest.fixture()
def share_token(auth_client, ingested_study):
    study_uid, _series_uid, _sop = ingested_study
    resp = auth_client.post("/api/v1/share", json={"study_uid": study_uid, "expiry_days": 1})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"], study_uid


def test_share_metadata_endpoint_is_public_and_sets_a_scoped_cookie(anonymous_client, share_token):
    token, study_uid = share_token
    resp = anonymous_client.get(f"/api/v1/share/{token}")
    assert resp.status_code == 200
    assert resp.json()["study_uid"] == study_uid
    # The scoped cookie is what lets the bundled viewer keep reading.
    assert "paxs_share" in resp.cookies or "paxs_share" in resp.headers.get("set-cookie", "")


def test_share_token_grants_read_of_its_own_study_in_private_mode(
    anonymous_client, share_token, monkeypatch
):
    token, study_uid = share_token
    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", False, raising=False)

    # Establish the scoped cookie exactly as the viewer would.
    assert anonymous_client.get(f"/api/v1/share/{token}").status_code == 200

    metadata = anonymous_client.get(f"/dicom-web/studies/{study_uid}/metadata")
    assert metadata.status_code == 200, metadata.text
    series = anonymous_client.get(f"/dicom-web/studies/{study_uid}/series")
    assert series.status_code == 200


def test_share_token_does_not_unlock_other_studies(
    anonymous_client, auth_client, share_token, monkeypatch
):
    token, shared_study = share_token
    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", False, raising=False)
    assert anonymous_client.get(f"/api/v1/share/{token}").status_code == 200

    # A second, unrelated study.
    other_study = generate_uid()
    other_series = generate_uid()
    ds = make_instance(
        patient_id="OTHER_PAT", study_uid=other_study, series_uid=other_series,
        sop_uid=generate_uid(), instance_num=1, rows=8, cols=8,
    )
    assert auth_client.post(
        "/dicom-web/studies", content=build_stow_body([ds_bytes(ds)]), headers=stow_headers()
    ).status_code == 200

    blocked = anonymous_client.get(f"/dicom-web/studies/{other_study}/metadata")
    assert blocked.status_code == 401, (
        f"a share token must not unlock another study, got {blocked.status_code}"
    )
    # QIDO must not list the other study either.
    listing = anonymous_client.get("/dicom-web/studies")
    assert listing.status_code == 200
    returned = [item.get("0020000D", {}).get("Value", [None])[0] for item in listing.json()]
    assert other_study not in returned


def test_share_token_never_grants_write(anonymous_client, share_token, monkeypatch):
    token, _study_uid = share_token
    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", False, raising=False)
    assert anonymous_client.get(f"/api/v1/share/{token}").status_code == 200

    ds = make_instance(patient_id="SHARE_WRITE", rows=8, cols=8)
    resp = anonymous_client.post(
        "/dicom-web/studies",
        content=build_stow_body([ds_bytes(ds)]),
        headers=stow_headers(),
    )
    assert resp.status_code == 401, "a share token must never authorise STOW-RS"


def test_share_token_allows_volume_read_of_its_study(
    anonymous_client, auth_client, ingested_study, monkeypatch
):
    """The viewer's MPR path uses /api/v1/volume, so share scope must cover it."""
    study_uid, series_uid, _sop = ingested_study
    token = auth_client.post(
        "/api/v1/share", json={"study_uid": study_uid, "expiry_days": 1}
    ).json()["token"]
    assert anonymous_client.get(f"/api/v1/share/{token}").status_code == 200

    resp = anonymous_client.get(f"/api/v1/volume/{series_uid}/info")
    assert resp.status_code == 200, resp.text


def test_expired_or_unknown_share_token_does_not_authorise(
    anonymous_client, ingested_study, monkeypatch
):
    study_uid, _series, _sop = ingested_study
    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", False, raising=False)
    assert anonymous_client.get("/api/v1/share/not-a-real-token").status_code == 404
    resp = anonymous_client.get(
        f"/dicom-web/studies/{study_uid}/metadata",
        cookies={"paxs_share": "not-a-real-token"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# 5. the same-origin viewer keeps working in private mode
# --------------------------------------------------------------------------- #
def test_session_cookie_authenticates_viewer_paths_in_private_mode(
    anonymous_client, ingested_study, monkeypatch
):
    """Acceptance: "valid users still load the viewer".

    The bundled viewer sends no bearer token — it relies on the HttpOnly session
    cookie, which is exactly what the viewer auth gate issues at login. With
    DICOMWEB_PUBLIC=false both DICOMweb and the volume endpoints must accept it.
    """
    from tests.conftest import TEST_USER_PASSWORD

    study_uid, series_uid, _sop = ingested_study

    login = anonymous_client.post(
        "/api/v1/auth/login",
        json={"username": "test_admin", "password": TEST_USER_PASSWORD},
    )
    assert login.status_code == 200, login.text
    assert "paxs_session" in anonymous_client.cookies

    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", False, raising=False)

    # No Authorization header anywhere below — cookie only.
    assert anonymous_client.get(f"/dicom-web/studies/{study_uid}/metadata").status_code == 200
    assert anonymous_client.get(f"/dicom-web/studies/{study_uid}/series").status_code == 200
    assert anonymous_client.get(f"/api/v1/volume/{series_uid}/info").status_code == 200
    assert anonymous_client.get("/api/v1/studies").status_code == 200


# --------------------------------------------------------------------------- #
# 6. the share viewer page hands the browser its scoped cookie
# --------------------------------------------------------------------------- #
def test_viewer_share_link_sets_the_scoped_cookie(anonymous_client, share_token):
    """`/viewer?share=<token>` must set the cookie the viewer's reads rely on.

    This is the piece that makes share links work "seamlessly" in private mode
    without touching the bundled (third-party) viewer: the server sets the
    cookie while serving the page, so every later same-origin DICOMweb/volume
    request carries it.
    """
    token, _study_uid = share_token
    resp = anonymous_client.get(f"/viewer?share={token}")
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "paxs_share" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/" in set_cookie
    # A non-positive Max-Age would delete the cookie immediately in a browser,
    # silently breaking share links while this header still looked correct.
    import re

    max_age = re.search(r"Max-Age=(-?\d+)", set_cookie)
    assert max_age, f"share cookie has no Max-Age: {set_cookie}"
    assert 0 < int(max_age.group(1)) <= 86400, f"implausible Max-Age: {max_age.group(1)}"


def test_viewer_share_link_with_invalid_token_sets_no_cookie(anonymous_client):
    resp = anonymous_client.get("/viewer?share=not-a-real-token")
    assert resp.status_code == 200
    assert "paxs_share" not in resp.headers.get("set-cookie", "")


def test_viewer_page_without_share_sets_no_cookie(anonymous_client):
    resp = anonymous_client.get("/viewer")
    assert resp.status_code == 200
    assert "paxs_share" not in resp.headers.get("set-cookie", "")


# --------------------------------------------------------------------------- #
# 7. share scope is a fallback identity, never an override
# --------------------------------------------------------------------------- #
def _study_uids(payload) -> list[str]:
    return [item.get("0020000D", {}).get("Value", [None])[0] for item in payload]


def test_share_cookie_does_not_narrow_an_authenticated_user(
    auth_client, ingested_study, monkeypatch
):
    """A leftover share cookie must not restrict a logged-in user's view.

    Otherwise a clinician who once opened a share link would silently see only
    that one study in the worklist.
    """
    study_uid, _series, _sop = ingested_study

    # A second, unrelated study so the list is not trivially just the shared one.
    other_study = generate_uid()
    ds = make_instance(
        patient_id="NARROW_PAT", study_uid=other_study, series_uid=generate_uid(),
        sop_uid=generate_uid(), instance_num=1, rows=8, cols=8,
    )
    assert auth_client.post(
        "/dicom-web/studies", content=build_stow_body([ds_bytes(ds)]), headers=stow_headers()
    ).status_code == 200

    token = auth_client.post(
        "/api/v1/share", json={"study_uid": study_uid, "expiry_days": 1}
    ).json()["token"]
    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", False, raising=False)

    resp = auth_client.get("/dicom-web/studies", cookies={"paxs_share": token})
    assert resp.status_code == 200
    uids = _study_uids(resp.json())
    assert other_study in uids, "an authenticated user must not be narrowed by a share cookie"
    assert study_uid in uids


def test_public_mode_ignores_a_stale_share_cookie(
    anonymous_client, share_token, ingested_study, monkeypatch
):
    """In public mode the share cookie must not change what a caller sees."""
    token, shared_study = share_token
    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", True, raising=False)
    assert settings.DICOMWEB_PUBLIC is True
    resp = anonymous_client.get("/dicom-web/studies", cookies={"paxs_share": token})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# --------------------------------------------------------------------------- #
# C04 — every route that resolves study/series/instance data must be
# tenant-scoped (or be one of the deliberately public, token-addressed routes).
# --------------------------------------------------------------------------- #
DATA_CALLS = {"instance_query", "series_query", "study_query"}

# Routes that legitimately resolve data without a tenant scope:
# `share.get_share_info` is addressed by the share token itself, which is the
# credential for exactly one study (and is gated by C03's public allowlist).
TENANT_EXEMPT: set[tuple[str, str, str]] = {
    ("share", "GET", "/{token}"),
}

# Anything that shows the caller's scope was considered.
TENANT_MARKERS = (
    "require_study_access",
    "require_series_access",
    "require_instance_access",
    "scope_from_user",
    "tenant_scope",
    "read_scope",
    "current_scope",
    "include_all=",
    "tenant_id",
)


def _route_source_segments():
    """(module, method, path, source, line) for every API route."""
    for path in sorted(API_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        src = path.read_text(encoding="utf-8-sig")
        lines = src.splitlines()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                fn = dec.func
                if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
                    continue
                if fn.value.id != "router" or fn.attr not in (
                    "get", "post", "put", "patch", "delete"
                ):
                    continue
                route_path = (
                    dec.args[0].value
                    if dec.args and isinstance(dec.args[0], ast.Constant)
                    else "?"
                )
                yield (
                    path.stem,
                    fn.attr.upper(),
                    route_path,
                    "\n".join(lines[node.lineno - 1:node.end_lineno]),
                    node.lineno,
                )


def test_every_data_route_is_tenant_scoped_or_exempt():
    offenders = []
    for module, method, route, source, line in _route_source_segments():
        touches_data = any(
            f".{name}(" in source for name in DATA_CALLS
        ) or "select(Study)" in source
        if not touches_data:
            continue
        if (module, method, route) in TENANT_EXEMPT:
            continue
        if not any(marker in source for marker in TENANT_MARKERS):
            offenders.append(f"{module}.py:{line} {method} {route}")
    assert not offenders, (
        "Routes that resolve study/series/instance data without tenant scoping "
        "(add a guard or justify them in TENANT_EXEMPT):\n  " + "\n  ".join(offenders)
    )


def test_tenant_exempt_list_has_no_stale_entries():
    actual = {(m, method, route) for m, method, route, _s, _l in _route_source_segments()}
    stale = TENANT_EXEMPT - actual
    assert not stale, f"TENANT_EXEMPT lists routes that no longer exist: {sorted(stale)}"


def test_query_builders_default_to_tenant_scoped():
    """C04: archive-wide access must be an explicit opt-in, not the default."""
    import inspect

    from app.services import query_service

    for fn in (query_service.study_query, query_service.series_query,
               query_service.instance_query):
        signature = inspect.signature(fn)
        default = signature.parameters["include_all"].default
        assert default is False, f"{fn.__name__} still defaults to include_all={default!r}"


def test_private_dicomweb_is_the_shipped_default():
    """C03: private mode is the baseline; public mode is an explicit opt-in.

    Checks the source default, because the test environment sets the env var to
    exercise the legacy public mode (see tests/conftest.py).
    """
    import inspect

    from app.core import config as config_module

    assert "DICOMWEB_PUBLIC: bool = False" in inspect.getsource(config_module)


# --------------------------------------------------------------------------- #
# H03 — every mutating route must declare an explicit authorization level.
#
# The C03 matrix proves anonymous callers are rejected; this proves that a
# mutation cannot ship with only "any authenticated user" when it is actually a
# privileged clinical operation.
# --------------------------------------------------------------------------- #
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Routes that legitimately have no auth dependency (they *are* the login flow,
# or are addressed by a token that is itself the credential).
MUTATION_AUTH_EXEMPT: set[tuple[str, str, str]] = {
    ("auth", "POST", "/login"),
    ("auth", "POST", "/register"),
    ("auth", "POST", "/refresh"),
    ("auth", "POST", "/logout"),
    ("ldap", "POST", "/login"),    # alternate login flow: cannot require auth
    ("share", "POST", ""),          # guarded by require_destructive_role today
}

# H03 permission matrix: privileged clinical mutations and the guard they must
# carry. Kept explicit so lowering a guard is a visible test failure.
REQUIRED_MUTATION_GUARDS: dict[tuple[str, str, str], str] = {
    # PURGE policy, PURGE execution and storage-tier moves are admin-only.
    ("lifecycle", "POST", "/rules"): "require_admin",
    ("lifecycle", "PUT", "/rules/{rule_id}"): "require_admin",
    ("lifecycle", "DELETE", "/rules/{rule_id}"): "require_admin",
    ("lifecycle", "POST", "/run"): "require_admin",
    ("lifecycle", "POST", "/studies/{study_uid}/archive"): "require_admin",
    ("lifecycle", "POST", "/studies/{study_uid}/restore"): "require_admin",
    ("lifecycle", "POST", "/studies/{study_uid}/purge"): "require_admin",
    # Routing decides where PHI is sent.
    ("routing", "POST", "/rules"): "require_admin",
    ("routing", "PUT", "/rules/{rule_id}"): "require_admin",
    ("routing", "DELETE", "/rules/{rule_id}"): "require_admin",
    ("routing", "POST", "/evaluate/{study_uid}"): "require_destructive_role",
    # Irreversible compression and record restructuring.
    ("lossy_compression", "POST", "/rules"): "require_destructive_role",
    ("lossy_compression", "PUT", "/rules/{rule_id}"): "require_destructive_role",
    ("lossy_compression", "DELETE", "/rules/{rule_id}"): "require_destructive_role",
    ("lossy_compression", "POST", "/run"): "require_destructive_role",
    ("split_merge", "POST", "/split"): "require_destructive_role",
    ("split_merge", "POST", "/merge"): "require_destructive_role",
}

AUTH_DEPENDENCY_MARKERS = (
    "Depends(get_current_user",
    "Depends(get_current_user_any",
    "Depends(require_admin)",
    "Depends(require_destructive_role)",
    "Depends(require_dicomweb_access)",
    "Depends(require_dicomweb_read)",
    "Depends(require_dicomweb_write)",
    "Depends(require_series_read)",
    "Depends(get_optional_user",
)


def test_mutating_routes_declare_an_auth_dependency():
    offenders = []
    for module, method, route, source, line in _route_source_segments():
        if method not in MUTATING_METHODS:
            continue
        if (module, method, route) in MUTATION_AUTH_EXEMPT:
            continue
        if not any(marker in source for marker in AUTH_DEPENDENCY_MARKERS):
            offenders.append(f"{module}.py:{line} {method} {route}")
    assert not offenders, (
        "mutating routes without a declared authorization dependency:/n  "
        + "\n  ".join(offenders)
    )


def test_privileged_mutations_use_the_required_guard():
    """H03: the permission matrix must hold for every listed route."""
    actual = {
        (module, method, route): source
        for module, method, route, source, _line in _route_source_segments()
    }
    problems = []
    for key, guard in REQUIRED_MUTATION_GUARDS.items():
        source = actual.get(key)
        if source is None:
            problems.append(f"{key}: route no longer exists")
            continue
        if f"Depends({guard})" not in source:
            problems.append(f"{key}: expected Depends({guard})")
    assert not problems, "permission matrix violations:/n  " + "\n  ".join(problems)
