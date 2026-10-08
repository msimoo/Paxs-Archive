"""C04 — tenant isolation across every API surface.

Two tenants, each with its own user and study. The matrix asserts that a
tenant-A user gets **404** (never 403, so existence is not disclosed) for every
tenant-B object, across reads, exports, shares and destructive operations, while
its own objects keep working and an ADMIN keeps archive-wide access.

The share-token case is included deliberately: a share link for a tenant-tagged
study must keep working anonymously (the token is an authorization for exactly
that study), which is easy to break when tightening tenant scoping.
"""

from __future__ import annotations

import numpy as np
import pytest
from pydicom.uid import generate_uid

from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _auth(client, username: str, password: str = "secret123") -> dict[str, str]:
    client.post("/api/v1/auth/register", json={"username": username, "password": password})
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _make_tenant(client, admin_h, name: str, code: str) -> dict:
    r = client.post("/api/v1/tenants", headers=admin_h, json={"name": name, "code": code})
    assert r.status_code == 201, r.text
    return r.json()


def _make_tenant_user(client, admin_h, username: str, tenant_id: int) -> dict[str, str]:
    r = client.post(
        "/api/v1/users",
        headers=admin_h,
        json={
            "username": username,
            "password": "secret123",
            "role": "RADIOLOGIST",
            "tenant_id": tenant_id,
        },
    )
    assert r.status_code in (200, 201), r.text
    return _auth(client, username)


def _stow_study(client, headers: dict[str, str], patient_id: str = "ISO_PAT"):
    """STOW a 2-slice study as the given caller; returns its identifiers."""
    study_uid, series_uid = generate_uid(), generate_uid()
    sop_uids, datasets = [], []
    for i in range(2):
        ds = make_instance(
            patient_id=patient_id,
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=generate_uid(),
            instance_num=i + 1,
            rows=8,
            cols=8,
            pixel=np.full((8, 8), 100 + i, dtype=np.uint16),
            position=[0.0, 0.0, float(i)],
            orientation=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            slice_thickness="1",
        )
        sop_uids.append(str(ds.SOPInstanceUID))
        datasets.append(ds_bytes(ds))
    resp = client.post(
        f"/dicom-web/studies/{study_uid}",
        content=build_stow_body(datasets),
        headers={**stow_headers(), **headers},
    )
    assert resp.status_code == 200, resp.text
    return study_uid, series_uid, sop_uids[0]


@pytest.fixture()
def tenants(client, auth_headers):
    """Tenant A and tenant B, each with a user and a study of its own."""
    admin_h = auth_headers
    ta = _make_tenant(client, admin_h, "Iso Tenant A", "iso_a")
    tb = _make_tenant(client, admin_h, "Iso Tenant B", "iso_b")
    user_a = _make_tenant_user(client, admin_h, "iso_user_a", ta["id"])
    user_b = _make_tenant_user(client, admin_h, "iso_user_b", tb["id"])
    study_a, series_a, sop_a = _stow_study(client, user_a, "ISO_PAT_A")
    study_b, series_b, sop_b = _stow_study(client, user_b, "ISO_PAT_B")
    return {
        "admin_h": admin_h,
        "user_a": user_a,
        "user_b": user_b,
        "a": (study_a, series_a, sop_a),
        "b": (study_b, series_b, sop_b),
    }


def _tenant_b_urls(study: str, series: str, sop: str) -> list[tuple[str, str, dict | None]]:
    """(label, url, json_body) for every surface that addresses tenant B's study."""
    return [
        ("dashboard study detail", f"/api/v1/studies/{study}", None),
        ("dashboard series instances", f"/api/v1/studies/{study}/series/{series}/instances", None),
        ("dashboard thumbnail", f"/api/v1/studies/{study}/series/{series}/thumbnail.png", None),
        ("dashboard presign", f"/api/v1/storage/presign/{study}/{series}/{sop}", None),
        ("volume info", f"/api/v1/volume/{series}/info", None),
        ("volume data", f"/api/v1/volume/{series}/data", None),
        ("volume mip", f"/api/v1/volume/{series}/mip", None),
        ("volume mpr slice", f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=0", None),
        ("dicomweb metadata", f"/dicom-web/studies/{study}/metadata", None),
        ("dicomweb series", f"/dicom-web/studies/{study}/series", None),
        ("dicomweb instance", f"/dicom-web/studies/{study}/series/{series}/instances/{sop}", None),
        ("dicomweb frames", f"/dicom-web/studies/{study}/series/{series}/instances/{sop}/frames/1", None),
        ("export anonymized", f"/api/v1/export/{study}/anonymized", None),
        ("report for study", f"/api/v1/reports/study/{study}", None),
        ("report print", f"/api/v1/reports/print/{study}", None),
        ("gsps for study", f"/api/v1/gsps/study/{study}", None),
        ("priors for study", f"/api/v1/priors/study/{study}", None),
        ("fhir imagingstudy", f"/fhir/ImagingStudy/{study}", None),
        ("dicomdir preview", "/api/v1/dicomdir/preview", {"study_instance_uids": [study]}),
        ("share creation", "/api/v1/share", {"study_uid": study, "expiry_days": 1}),
    ]


# --------------------------------------------------------------------------- #
# reads / exports / shares must not cross tenants
# --------------------------------------------------------------------------- #
def test_tenant_a_cannot_reach_tenant_b_reads(client, tenants):
    study_b, series_b, sop_b = tenants["b"]
    failures = []
    for label, url, body in _tenant_b_urls(study_b, series_b, sop_b):
        resp = (
            client.post(url, headers=tenants["user_a"], json=body)
            if body is not None
            else client.get(url, headers=tenants["user_a"])
        )
        if resp.status_code != 404:
            failures.append(f"{label}: {url} -> {resp.status_code} (expected 404)")
    assert not failures, "cross-tenant access was not refused:\n  " + "\n  ".join(failures)


def test_tenant_a_cannot_delete_tenant_b_study(client, tenants):
    study_b, _series_b, _sop_b = tenants["b"]
    resp = client.delete(f"/api/v1/studies/{study_b}", headers=tenants["user_a"])
    assert resp.status_code == 404, resp.text
    # And the study is still there for its owner.
    assert client.get(f"/api/v1/studies/{study_b}", headers=tenants["user_b"]).status_code == 200


def test_tenant_a_own_study_still_works(client, tenants):
    """Positive control — the guards must not break normal access."""
    study_a, series_a, _sop_a = tenants["a"]
    h = tenants["user_a"]
    assert client.get(f"/api/v1/studies/{study_a}", headers=h).status_code == 200
    assert client.get(f"/api/v1/studies/{study_a}/series/{series_a}/instances", headers=h).status_code == 200
    assert client.get(f"/api/v1/volume/{series_a}/info", headers=h).status_code == 200
    assert client.get(f"/dicom-web/studies/{study_a}/metadata", headers=h).status_code == 200
    assert client.get(f"/api/v1/reports/study/{study_a}", headers=h).status_code == 200
    assert client.get(f"/fhir/ImagingStudy/{study_a}", headers=h).status_code == 200


def test_admin_keeps_archive_wide_access(client, tenants):
    """The intentional admin exception."""
    study_b, series_b, _sop_b = tenants["b"]
    assert client.get(f"/api/v1/studies/{study_b}", headers=tenants["admin_h"]).status_code == 200
    assert client.get(f"/api/v1/volume/{series_b}/info", headers=tenants["admin_h"]).status_code == 200
    assert client.get(f"/dicom-web/studies/{study_b}/metadata", headers=tenants["admin_h"]).status_code == 200


# --------------------------------------------------------------------------- #
# search results, not just direct reads
# --------------------------------------------------------------------------- #
def test_qido_search_does_not_leak_other_tenants(client, tenants):
    study_a, _sa, _oa = tenants["a"]
    study_b, _sb, _ob = tenants["b"]
    resp = client.get("/dicom-web/studies", headers=tenants["user_a"])
    assert resp.status_code == 200
    uids = [item.get("0020000D", {}).get("Value", [None])[0] for item in resp.json()]
    assert study_a in uids, "the tenant's own study must be listed"
    assert study_b not in uids, "another tenant's study leaked into the search results"


def test_fhir_search_does_not_leak_other_tenants(client, tenants):
    study_b, _sb, _ob = tenants["b"]
    resp = client.get("/fhir/ImagingStudy", headers=tenants["user_a"])
    assert resp.status_code == 200
    body = resp.json()
    uids = [
        entry["resource"]["id"]
        for entry in body.get("entry", [])
        if entry.get("resource", {}).get("id")
    ]
    assert study_b not in uids


# --------------------------------------------------------------------------- #
# ingest boundary
# --------------------------------------------------------------------------- #
def test_tenant_a_cannot_append_to_tenant_b_study(client, tenants):
    study_b, _series_b, _sop_b = tenants["b"]
    ds = make_instance(
        patient_id="ISO_PAT_B",
        study_uid=study_b,          # tenant B's study UID
        series_uid=generate_uid(),
        sop_uid=generate_uid(),
        instance_num=9,
        rows=8,
        cols=8,
    )
    resp = client.post(
        f"/dicom-web/studies/{study_b}",
        content=build_stow_body([ds_bytes(ds)]),
        headers={**stow_headers(), **tenants["user_a"]},
    )
    assert resp.status_code == 400, resp.text
    # Tenant B's study is untouched: still 2 instances.
    detail = client.get(f"/api/v1/studies/{study_b}", headers=tenants["user_b"]).json()
    total = sum(s.get("instances", 0) for s in detail.get("series", []))
    assert total == 2, f"tenant B's study was modified: {detail}"


def test_admin_may_append_to_any_study(client, tenants):
    """The admin exception also applies at ingest."""
    study_b, _series_b, _sop_b = tenants["b"]
    ds = make_instance(
        patient_id="ISO_PAT_B",
        study_uid=study_b,
        series_uid=generate_uid(),
        sop_uid=generate_uid(),
        instance_num=10,
        rows=8,
        cols=8,
    )
    resp = client.post(
        f"/dicom-web/studies/{study_b}",
        content=build_stow_body([ds_bytes(ds)]),
        headers={**stow_headers(), **tenants["admin_h"]},
    )
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# share links keep working for their own (tenant-tagged) study
# --------------------------------------------------------------------------- #
def test_share_link_still_reads_its_own_tenant_tagged_study(client, tenants, monkeypatch):
    """Regression: tightening tenant scoping must not break patient sharing."""
    from app.core.config import settings

    study_b, series_b, _sop_b = tenants["b"]
    token = client.post(
        "/api/v1/share", headers=tenants["user_b"], json={"study_uid": study_b, "expiry_days": 1}
    ).json()["token"]

    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", False, raising=False)
    assert client.get(f"/api/v1/share/{token}").status_code == 200

    assert client.get(f"/dicom-web/studies/{study_b}/metadata").status_code == 200
    assert client.get(f"/api/v1/volume/{series_b}/info").status_code == 200


def test_share_token_does_not_widen_a_tenant_users_access(client, tenants, monkeypatch):
    """A share cookie must not let tenant A read tenant B via the share branch."""
    from app.core.config import settings

    study_a, _sa, _oa = tenants["a"]
    study_b, _sb, _ob = tenants["b"]
    token_a = client.post(
        "/api/v1/share", headers=tenants["user_a"], json={"study_uid": study_a, "expiry_days": 1}
    ).json()["token"]
    monkeypatch.setattr(settings, "DICOMWEB_PUBLIC", False, raising=False)

    resp = client.get(
        f"/dicom-web/studies/{study_b}/metadata",
        headers=tenants["user_a"],
        cookies={"paxs_share": token_a},
    )
    assert resp.status_code == 404, "a share token must not widen tenant access"


# --------------------------------------------------------------------------- #
# unassigned (legacy) scope is unchanged
# --------------------------------------------------------------------------- #
def test_unassigned_user_sees_only_legacy_unscoped_studies(client, auth_headers, tenants):
    """A user with no tenant keeps the pre-VNA scope: tenant-tagged data is hidden."""
    legacy_h = _auth(client, "iso_legacy_user")
    # The user exists without a tenant assignment.
    study_b, _sb, _ob = tenants["b"]
    assert client.get(f"/api/v1/studies/{study_b}", headers=legacy_h).status_code == 404
    resp = client.get("/dicom-web/studies", headers=legacy_h)
    assert resp.status_code == 200
    uids = [item.get("0020000D", {}).get("Value", [None])[0] for item in resp.json()]
    assert study_b not in uids
