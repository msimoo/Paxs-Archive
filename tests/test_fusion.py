"""Tests for roadmap #29 — Fusion (Frame of Reference series alignment).

Covers the /api/v1/fusion/{study} manifest: FoR grouping, the same-FoR pair
matrix (PET on CT / MRI on US overlay contract), per-series volume geometry,
unregistered series, tenant scoping, and the 404 for unknown studies.
"""

from __future__ import annotations

import numpy as np
from pydicom.uid import generate_uid

from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers

FOR_A = "1.2.840.113619.2.408.1"
FOR_B = "1.2.840.113619.2.408.2"
# Key Object Selection Document — a non-image SOP (no volumetric content).
KO_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.88.22"

AXIAL = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


def _stow_study(client, *, study_uid, series_specs):
    """STOW one study built from series_specs: list of dicts with
    (modality, frame_of_reference, n, series_num, desc, position_z)."""
    datasets = []
    for spec in series_specs:
        series_uid = spec["series_uid"]
        for i in range(spec.get("n", 2)):
            pixel = np.full((16, 16), 200 + i * 5, dtype=np.uint16)
            pixel[4:12, 4:12] = 1800
            ds = make_instance(
                patient_id="FUS_PAT",
                study_uid=study_uid,
                series_uid=series_uid,
                sop_uid=generate_uid(),
                instance_num=i + 1,
                rows=16,
                cols=16,
                modality=spec["modality"],
                series_num=spec["series_num"],
                series_desc=spec.get("desc"),
                **({"sop_class_uid": spec["sop_class_uid"]}
                   if spec.get("sop_class_uid") else {}),
                pixel=pixel,
                position=[0.0, 0.0, spec.get("z0", 0.0) + i * 5.0],
                orientation=AXIAL,
                rescale_slope="1",
                rescale_intercept="-1024",
                slice_thickness="5",
                frame_of_reference=spec.get("frame_of_reference"),
            )
            datasets.append(ds_bytes(ds))
    resp = client.post(
        f"/dicom-web/studies/{study_uid}", content=build_stow_body(datasets),
        headers=stow_headers(),
    )
    assert resp.status_code == 200, resp.text


def test_fusion_pairs_same_for(client):
    """PET + CT sharing a FoR produce exactly one aligned pair with geometry."""
    study, ct_series, pet_series = generate_uid(), generate_uid(), generate_uid()
    _stow_study(client, study_uid=study, series_specs=[
        {"modality": "CT", "series_uid": ct_series, "series_num": 1,
         "frame_of_reference": FOR_A, "desc": "CT CHEST"},
        {"modality": "PT", "series_uid": pet_series, "series_num": 2,
         "frame_of_reference": FOR_A, "desc": "PET WB"},
    ])

    resp = client.get(f"/api/v1/fusion/{study}")
    assert resp.status_code == 200
    m = resp.json()
    assert m["study_instance_uid"] == study
    assert m["not_found"] is False
    assert m["series_count"] == 2
    assert len(m["frames_of_reference"]) == 1
    assert m["frames_of_reference"][0]["frame_of_reference_uid"] == FOR_A

    # Exactly one pair (PET, CT), identity transform, both geometries attached
    assert len(m["pairs"]) == 1
    pair = m["pairs"][0]
    assert pair["frame_of_reference_uid"] == FOR_A
    assert pair["transform"]["type"] == "IDENTITY"
    assert pair["transform"]["registered"] is True
    mods = {pair["series_a"]["modality"], pair["series_b"]["modality"]}
    assert mods == {"CT", "PT"}

    for s in (pair["series_a"], pair["series_b"]):
        geo = s["geometry"]
        assert geo is not None
        assert geo["rows"] == 16 and geo["columns"] == 16
        assert geo["slices"] == 2
        assert geo["orientation"] == "AXIAL"
        assert geo["slice_spacing"] == 5.0
        assert geo["rescale_intercept"] == -1024.0


def test_fusion_multiple_for_groups(client):
    """Two distinct FoR groups each produce their own pairs; unregistered
    series are reported separately and never fused."""
    study = generate_uid()
    ct_a = generate_uid(); pt_a = generate_uid()
    ct_b = generate_uid(); us_b = generate_uid()
    no_for = generate_uid()
    _stow_study(client, study_uid=study, series_specs=[
        {"modality": "CT", "series_uid": ct_a, "series_num": 1, "frame_of_reference": FOR_A},
        {"modality": "PT", "series_uid": pt_a, "series_num": 2, "frame_of_reference": FOR_A},
        {"modality": "CT", "series_uid": ct_b, "series_num": 3, "frame_of_reference": FOR_B},
        {"modality": "US", "series_uid": us_b, "series_num": 4, "frame_of_reference": FOR_B},
        {"modality": "CR", "series_uid": no_for, "series_num": 5, "frame_of_reference": None},
    ])

    m = client.get(f"/api/v1/fusion/{study}").json()
    assert m["series_count"] == 5
    assert len(m["frames_of_reference"]) == 2
    assert len(m["pairs"]) == 2
    for_used = {p["frame_of_reference_uid"] for p in m["pairs"]}
    assert for_used == {FOR_A, FOR_B}

    # The CR without a FoR is not fused
    assert len(m["unregistered"]) == 1
    assert m["unregistered"][0]["modality"] == "CR"
    assert m["unregistered"][0]["series_instance_uid"] == no_for


def test_fusion_single_series_no_pairs(client):
    """A lone volumetric series has a FoR group but zero pairs."""
    study, series = generate_uid(), generate_uid()
    _stow_study(client, study_uid=study, series_specs=[
        {"modality": "CT", "series_uid": series, "series_num": 1,
         "frame_of_reference": FOR_A},
    ])
    m = client.get(f"/api/v1/fusion/{study}").json()
    assert len(m["frames_of_reference"]) == 1
    assert len(m["frames_of_reference"][0]["series"]) == 1
    assert m["pairs"] == []


def test_fusion_404_unknown_study(client):
    resp = client.get(f"/api/v1/fusion/{generate_uid()}")
    assert resp.status_code == 404


def _auth(client, username, password="secret123"):
    client.post("/api/v1/auth/register", json={"username": username, "password": password})
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def test_fusion_tenant_scoping(client):
    """VNA: a tenant-scoped user only sees their own study's fusion manifest;
    another tenant's study is a 404."""
    admin_h = _auth(client, "fus_admin")
    admin = client.get("/api/v1/auth/me", headers=admin_h).json()
    assert admin["role"] == "ADMIN"

    # Tenant A + a tenant-A user
    r = client.post("/api/v1/tenants", headers=admin_h,
                    json={"name": "Fus Tenant A", "code": "fus_a"})
    assert r.status_code in (200, 201)
    tenant_a = r.json()["id"]
    user_a = client.post("/api/v1/users", headers=admin_h, json={
        "username": "fus_user_a", "password": "secret123", "role": "RADIOLOGIST",
        "tenant_id": tenant_a,
    })
    assert user_a.status_code in (200, 201)
    user_a_h = _auth(client, "fus_user_a")

    # STOW a study into tenant A (via the tenant user's token so it gets tagged)
    study, ct, pet = generate_uid(), generate_uid(), generate_uid()
    datasets = []
    for mod, s_uid, num in (("CT", ct, 1), ("PT", pet, 2)):
        for i in range(2):
            ds = make_instance(
                patient_id="FUS_TEN", study_uid=study, series_uid=s_uid,
                sop_uid=generate_uid(), instance_num=i + 1, rows=16, cols=16,
                modality=mod, series_num=num,
                position=[0.0, 0.0, i * 5.0], orientation=AXIAL,
                rescale_slope="1", rescale_intercept="-1024",
                slice_thickness="5", frame_of_reference=FOR_A,
            )
            datasets.append(ds_bytes(ds))
    stow = client.post(
        f"/dicom-web/studies/{study}", content=build_stow_body(datasets),
        headers={**stow_headers(), **user_a_h},
    )
    assert stow.status_code == 200, stow.text

    # The tenant-A user sees the fused pair
    m = client.get(f"/api/v1/fusion/{study}", headers=user_a_h).json()
    assert m["not_found"] is False
    assert len(m["pairs"]) == 1

    # A tenant-B user gets 404 for the same study
    r = client.post("/api/v1/tenants", headers=admin_h,
                    json={"name": "Fus Tenant B", "code": "fus_b"})
    assert r.status_code in (200, 201)
    tenant_b = r.json()["id"]
    client.post("/api/v1/users", headers=admin_h, json={
        "username": "fus_user_b", "password": "secret123", "role": "RADIOLOGIST",
        "tenant_id": tenant_b,
    })
    user_b_h = _auth(client, "fus_user_b")
    assert client.get(f"/api/v1/fusion/{study}", headers=user_b_h).status_code == 404

    # Guest (unauthenticated, legacy scope) also misses tenant-tagged data
    assert client.get(f"/api/v1/fusion/{study}").status_code == 404


def test_fusion_geometry_nonvolumetric_series(client):
    """A same-FoR pair where one series is non-volumetric still fuses; the
    geometry is None for that side but the pair contract is intact."""
    study = generate_uid()
    ct = generate_uid(); ko = generate_uid()
    _stow_study(client, study_uid=study, series_specs=[
        {"modality": "CT", "series_uid": ct, "series_num": 1, "frame_of_reference": FOR_A},
        # KO (Key Object) has no pixel data → volume_service returns None
        {"modality": "KO", "series_uid": ko, "series_num": 2,
         "frame_of_reference": FOR_A, "sop_class_uid": KO_SOP_CLASS},
    ])
    m = client.get(f"/api/v1/fusion/{study}").json()
    assert len(m["pairs"]) == 1
    pair = m["pairs"][0]
    geos = {pair["series_a"]["modality"]: pair["series_a"]["geometry"],
            pair["series_b"]["modality"]: pair["series_b"]["geometry"]}
    assert geos["CT"] is not None
    assert geos["KO"] is None
