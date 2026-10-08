"""C05 — the FHIR /Patient endpoints must be tenant-scoped.

Before the fix, ``GET /fhir/Patient`` and ``GET /fhir/Patient/{id}`` queried the
``patients`` table without a tenant filter, so a tenant-A user could enumerate or
read tenant-B demographics (name / MRN / birth date / sex) — a VNA/HIPAA
isolation breach. ``ImagingStudy`` was already scoped; these tests pin the same
guarantee for ``Patient`` and guard the shared predicate against drift.

Note: the FHIR router is mounted at ``/fhir`` (not ``/api/v1/fhir``).
"""

from __future__ import annotations

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


def _make_tenant(client, admin_h: dict, name: str, code: str) -> dict:
    r = client.post("/api/v1/tenants", headers=admin_h, json={"name": name, "code": code})
    assert r.status_code == 201, r.text
    return r.json()


def _make_tenant_user(client, admin_h: dict, username: str, tenant_id: int) -> dict[str, str]:
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


def _stow_patient(client, headers: dict[str, str], patient_id: str) -> str:
    """STOW a 1-slice study as the given caller; returns the study UID.

    Ingest via a tenant user's token tags the patient/study with that tenant.
    """
    study_uid, series_uid, sop_uid = generate_uid(), generate_uid(), generate_uid()
    ds = make_instance(
        patient_id=patient_id,
        study_uid=study_uid,
        series_uid=series_uid,
        sop_uid=sop_uid,
        instance_num=1,
        rows=8,
        cols=8,
    )
    resp = client.post(
        f"/dicom-web/studies/{study_uid}",
        content=build_stow_body([ds_bytes(ds)]),
        headers={**stow_headers(), **headers},
    )
    assert resp.status_code == 200, resp.text
    return study_uid


@pytest.fixture()
def two_tenants(client, auth_headers):
    """Tenant A and tenant B, each with a user and its own patient."""
    admin_h = auth_headers
    ta = _make_tenant(client, admin_h, "FHIR Tenant A", "fhir_a")
    tb = _make_tenant(client, admin_h, "FHIR Tenant B", "fhir_b")
    user_a = _make_tenant_user(client, admin_h, "fhir_user_a", ta["id"])
    user_b = _make_tenant_user(client, admin_h, "fhir_user_b", tb["id"])
    _stow_patient(client, user_a, "FHIR_PAT_A")
    _stow_patient(client, user_b, "FHIR_PAT_B")
    return {"admin_h": admin_h, "user_a": user_a, "user_b": user_b}


def _patient_ids(bundle: dict) -> set[str]:
    return {e["resource"]["identifier"][0]["value"] for e in bundle["entry"]}


def _patient_pk(client, headers: dict, identifier: str) -> str:
    bundle = client.get(f"/fhir/Patient?identifier={identifier}", headers=headers).json()
    assert bundle["total"] >= 1, f"{identifier} not found for this caller: {bundle}"
    return bundle["entry"][0]["resource"]["id"]


# --------------------------------------------------------------------------- #
# search must not cross tenants
# --------------------------------------------------------------------------- #
def test_patient_search_is_tenant_scoped(client, two_tenants):
    r = client.get("/fhir/Patient", headers=two_tenants["user_a"])
    assert r.status_code == 200, r.text
    bundle = r.json()

    ids = _patient_ids(bundle)
    assert "FHIR_PAT_A" in ids, "tenant A must see its own patient"
    assert "FHIR_PAT_B" not in ids, "tenant A must NOT see tenant B's patient"

    # The reported total must agree with the returned page (no leak via the count).
    assert bundle["total"] == len(bundle["entry"])


def test_patient_search_by_name_is_tenant_scoped(client, two_tenants):
    # "Doe^John" is the default name for every synthetic instance, so an
    # unscoped name search would surface tenant B's patient too.
    bundle = client.get("/fhir/Patient?name=Doe", headers=two_tenants["user_a"]).json()
    assert "FHIR_PAT_B" not in _patient_ids(bundle)
    assert "FHIR_PAT_A" in _patient_ids(bundle)


def test_patient_search_by_identifier_cannot_probe_other_tenant(client, two_tenants):
    """Guessing a tenant-B MRN must return nothing to a tenant-A caller."""
    bundle = client.get("/fhir/Patient?identifier=FHIR_PAT_B", headers=two_tenants["user_a"]).json()
    assert bundle["total"] == 0
    assert bundle["entry"] == []


# --------------------------------------------------------------------------- #
# direct id read must not cross tenants
# --------------------------------------------------------------------------- #
def test_direct_patient_read_is_tenant_scoped(client, two_tenants):
    # Resolve tenant B's numeric patient id as ADMIN (archive-wide access).
    pid_b = _patient_pk(client, two_tenants["admin_h"], "FHIR_PAT_B")

    # Tenant A must get 404 (not 403 — existence is not disclosed).
    res = client.get(f"/fhir/Patient/{pid_b}", headers=two_tenants["user_a"])
    assert res.status_code == 404

    # Its own patient resolves normally.
    pid_a = _patient_pk(client, two_tenants["user_a"], "FHIR_PAT_A")
    own = client.get(f"/fhir/Patient/{pid_a}", headers=two_tenants["user_a"])
    assert own.status_code == 200
    assert own.json()["identifier"][0]["value"] == "FHIR_PAT_A"

    # ADMIN keeps archive-wide access (the multi-tenant superadmin case).
    assert client.get(f"/fhir/Patient/{pid_b}", headers=two_tenants["admin_h"]).status_code == 200


def test_tenant_b_sees_only_its_own(client, two_tenants):
    """Symmetric check — tenant B is scoped the same way."""
    bundle = client.get("/fhir/Patient", headers=two_tenants["user_b"]).json()
    ids = _patient_ids(bundle)
    assert "FHIR_PAT_B" in ids
    assert "FHIR_PAT_A" not in ids

    pid_a = _patient_pk(client, two_tenants["admin_h"], "FHIR_PAT_A")
    assert client.get(f"/fhir/Patient/{pid_a}", headers=two_tenants["user_b"]).status_code == 404


# --------------------------------------------------------------------------- #
# regression: the shared predicate must not weaken ImagingStudy scoping
# --------------------------------------------------------------------------- #
def test_imagingstudy_search_remains_tenant_scoped(client, two_tenants):
    bundle = client.get("/fhir/ImagingStudy", headers=two_tenants["user_a"]).json()
    for entry in bundle["entry"]:
        ref = entry["resource"]["subject"]["reference"]  # "Patient/<id>"
        # Every referenced patient must be readable by tenant A.
        assert client.get(f"/fhir/{ref}", headers=two_tenants["user_a"]).status_code == 200
    assert bundle["total"] >= 1
