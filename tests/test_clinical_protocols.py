"""Tests for the Clinical Protocols Library (roadmap #31).

Maps study descriptions / modalities / body parts to a standardized clinical
protocol with a local RIS code, and auto-suggests the best match for an
incoming study context.
"""

from __future__ import annotations


def _auth(client, username="cp_admin"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "pw123456", "role": "ADMIN"},
    )
    token = (reg.json().get("access_token") if reg.status_code == 201
             else client.post("/api/v1/auth/login",
                              json={"username": username, "password": "pw123456"})
             .json()["access_token"])
    return {"Authorization": f"Bearer {token}"}


def _add(client, headers, name="CT Chest w/ Contrast", **kw):
    body = {
        "name": name,
        "standardized_name": "CT CHEST W CONTRAST",
        "ris_code": "CT-CHEST",
        "modality": "CT",
        "body_part_examined": "CHEST",
        "study_description_pattern": "CHEST,THORAX",
        "priority": 10,
    }
    body.update(kw)
    return client.post("/api/v1/clinical-protocols", json=body, headers=headers)


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

def test_crud_requires_auth(client):
    assert client.get("/api/v1/clinical-protocols").status_code in (401, 403)
    assert client.post("/api/v1/clinical-protocols", json={}).status_code in (401, 403)


def test_create_and_list(client):
    headers = _auth(client)
    r = _add(client, headers)
    assert r.status_code == 201
    cp = r.json()
    assert cp["name"] == "CT Chest w/ Contrast"
    assert cp["standardized_name"] == "CT CHEST W CONTRAST"
    assert cp["ris_code"] == "CT-CHEST"

    rows = client.get("/api/v1/clinical-protocols", headers=headers).json()
    assert any(p["id"] == cp["id"] for p in rows)

    # Duplicate name -> 409
    assert _add(client, headers).status_code == 409


def test_writes_require_curation_role(client):
    """PHYSICIAN can read but not create/update/delete protocols."""
    # Seed an admin first so the next registration is not bootstrapped to ADMIN.
    admin = _auth(client, "cp_admin0")
    # Default-registered users are PHYSICIAN (no role passed).
    reg = client.post("/api/v1/auth/register",
                      json={"username": "cp_phys2", "password": "pw123456"})
    phys_token = (reg.json().get("access_token") if reg.status_code == 201
                  else client.post("/api/v1/auth/login",
                                   json={"username": "cp_phys2", "password": "pw123456"})
                  .json()["access_token"])
    phys_headers = {"Authorization": f"Bearer {phys_token}"}

    # Read is open to any authenticated user.
    assert client.get("/api/v1/clinical-protocols", headers=phys_headers).status_code == 200

    # Writes are 403 for non-curation roles.
    assert _add(client, phys_headers).status_code == 403

    cp = _add(client, admin).json()
    assert client.patch(f"/api/v1/clinical-protocols/{cp['id']}",
                        json={"ris_code": "X"}, headers=phys_headers).status_code == 403
    assert client.delete(f"/api/v1/clinical-protocols/{cp['id']}",
                         headers=phys_headers).status_code == 403


def test_update_can_clear_nullable_fields(client):
    headers = _auth(client)
    cp = _add(client, headers, study_description_pattern="CHEST").json()
    pid = cp["id"]

    # Explicit null clears the pattern (PATCH sends only the changed keys).
    r = client.patch(f"/api/v1/clinical-protocols/{pid}",
                     json={"study_description_pattern": None}, headers=headers)
    assert r.status_code == 200
    assert r.json()["study_description_pattern"] is None

    # And the protocol no longer matches by that pattern.
    m = client.get(
        "/api/v1/clinical-protocols/match?study_description=CHEST", headers=headers
    ).json()
    assert m["match"] is None


def test_update_and_delete(client):
    headers = _auth(client)
    cp = _add(client, headers).json()
    pid = cp["id"]

    r = client.patch(
        f"/api/v1/clinical-protocols/{pid}",
        json={"ris_code": "CT-CHEST-V2", "enabled": False},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["ris_code"] == "CT-CHEST-V2"
    assert r.json()["enabled"] is False

    assert client.delete(f"/api/v1/clinical-protocols/{pid}", headers=headers).status_code == 200
    assert client.get(f"/api/v1/clinical-protocols/{pid}", headers=headers).status_code == 404


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def test_match_by_description(client):
    headers = _auth(client)
    _add(client, headers, name="Chest CT", ris_code="R1",
         study_description_pattern="CHEST,THORAX")
    _add(client, headers, name="Head CT", ris_code="R2",
         standardized_name="CT HEAD", modality="CT", body_part_examined="HEAD",
         study_description_pattern="HEAD,BRAIN")

    # Description match beats everything else.
    r = client.get(
        "/api/v1/clinical-protocols/match?study_description=CT%20CHEST%20W%20CONTRAST",
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["match"]["ris_code"] == "R1"

    # Modality + body part fallback.
    r = client.get(
        "/api/v1/clinical-protocols/match?modality=CT&body_part=HEAD",
        headers=headers,
    )
    assert r.json()["match"]["ris_code"] == "R2"

    # No signal -> no match.
    r = client.get(
        "/api/v1/clinical-protocols/match?modality=MR",
        headers=headers,
    )
    assert r.json()["match"] is None


def test_match_respects_enabled(client):
    headers = _auth(client)
    _add(client, headers, name="Old Chest", ris_code="OLD",
         study_description_pattern="CHEST")
    rows = client.get("/api/v1/clinical-protocols", headers=headers).json()
    old = next(p for p in rows if p["name"] == "Old Chest")
    client.patch(f"/api/v1/clinical-protocols/{old['id']}",
                 json={"enabled": False}, headers=headers)

    r = client.get(
        "/api/v1/clinical-protocols/match?study_description=CT%20CHEST",
        headers=headers,
    )
    assert r.json()["match"] is None


def test_priority_breaks_ties(client):
    headers = _auth(client)
    _add(client, headers, name="Generic Chest", ris_code="GEN",
         study_description_pattern="CHEST", priority=50)
    _add(client, headers, name="Focused Chest", ris_code="FOC",
         study_description_pattern="CHEST", priority=5)

    r = client.get(
        "/api/v1/clinical-protocols/match?study_description=CHEST",
        headers=headers,
    )
    assert r.json()["match"]["ris_code"] == "FOC"


def test_suggest_returns_ranked_candidates(client):
    headers = _auth(client)
    _add(client, headers, name="A", ris_code="A", modality="CT",
         body_part_examined="CHEST", study_description_pattern=None)
    _add(client, headers, name="B", ris_code="B", modality="CT",
         body_part_examined="ABDOMEN", study_description_pattern="CHEST")

    r = client.get(
        "/api/v1/clinical-protocols/suggest?study_description=CHEST&modality=CT",
        headers=headers,
    )
    assert r.status_code == 200
    rows = r.json()["items"]
    assert rows, "both protocols match CHEST+CT"
    assert rows[0]["ris_code"] == "B"  # description match outranks body-part only


def test_match_via_service_on_studies(client):
    """End-to-end: protocols can be matched for an ingested study's context."""
    from app.services import clinical_protocol_service as cps
    from app.db.session import AsyncSessionLocal
    from app.api.v1.clinical_protocols import _cp_to_dict

    headers = _auth(client)
    _add(client, headers, name="MR Knee", ris_code="MRK",
         modality="MR", body_part_examined="KNEE")

    async def _match():
        async with AsyncSessionLocal() as s:
            cp = await cps.match_best_protocol(
                s, study_description="MR KNEE W CONTRAST", modality="MR",
                body_part="KNEE",
            )
            return _cp_to_dict(cp) if cp else None

    import asyncio
    result = asyncio.run(_match())
    assert result and result["ris_code"] == "MRK"
