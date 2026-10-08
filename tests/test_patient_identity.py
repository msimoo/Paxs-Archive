"""C10 — tenant-scoped patient identity.

Patient identity used to be global: `patients.patient_id` was unique across the
whole installation, so two institutions using the same MRN shared one row and
each ingest overwrote the other's demographics.

These tests cover the three behaviours the fix must guarantee:
  * identical MRNs in two tenants stay distinct;
  * a later ingest cannot rewrite another institution's demographics;
  * missing IDs do not silently merge unrelated people;
plus legacy (unscoped) behaviour being unchanged.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from pydicom.uid import generate_uid
from sqlalchemy import select

from app.db.models import Patient, PatientIdentity, PatientIdentityConflict
from app.db.session import AsyncSessionLocal
from app.services import indexing_service
from app.services.indexing_service import (
    UNIDENTIFIED_PREFIX,
    default_authority_oid,
    resolve_patient_identity,
)
from tests.helpers import build_stow_body, create_user, ds_bytes, make_instance, stow_headers


def _make_tenant(client, admin_h, name: str, code: str) -> dict:
    r = client.post("/api/v1/tenants", headers=admin_h, json={"name": name, "code": code})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def tenant_ids(client, auth_headers) -> dict[str, int]:
    """Two real tenants — `patients.tenant_id` is a foreign key."""
    a = _make_tenant(client, auth_headers, "C10 Unit A", "c10_unit_a")
    b = _make_tenant(client, auth_headers, "C10 Unit B", "c10_unit_b")
    return {"a": a["id"], "b": b["id"]}


def _meta(patient_id: str | None, name: str | None = None, study_uid: str | None = None,
          issuer: str | None = None, birth: str | None = None, sex: str | None = None) -> dict:
    meta: dict = {"study_instance_uid": study_uid or generate_uid()}
    if patient_id is not None:
        meta["patient_id"] = patient_id
    if name:
        meta["patient_name"] = name
    if issuer:
        meta["issuer_of_patient_id"] = issuer
    if birth:
        meta["patient_birth_date"] = birth
    if sex:
        meta["patient_sex"] = sex
    return meta


def _resolve(meta: dict, tenant_id: int | None = None, source: str | None = "test"):
    """Run the resolver in its own session/transaction and return the outcome."""

    async def run():
        async with AsyncSessionLocal() as session:
            async with session.begin():
                resolution = await resolve_patient_identity(
                    session, meta, tenant_id=tenant_id, source=source
                )
                return (
                    resolution.outcome,
                    resolution.identifier,
                    resolution.authority,
                    resolution.patient.id,
                    resolution.patient.tenant_id,
                    resolution.patient.patient_name,
                    dict(resolution.conflicts),
                )

    return asyncio.run(run())


def _patients(patient_id: str) -> list[tuple]:
    """(tenant_id, patient_id, name) for every row with this MRN."""

    async def run():
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(Patient).where(Patient.patient_id == patient_id)
                )
            ).scalars().all()
            return [(p.tenant_id, p.patient_id, p.patient_name) for p in rows]

    return asyncio.run(run())


def _identities(patient_pk: int) -> list[tuple]:
    """(assigning_authority_id, identifier, tenant_id) for a patient."""

    async def run():
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(PatientIdentity).where(PatientIdentity.patient_id == patient_pk)
                )
            ).scalars().all()
            return [(i.assigning_authority_id, i.identifier, i.tenant_id) for i in rows]

    return asyncio.run(run())


def _conflicts() -> list[PatientIdentityConflict]:

    async def run():
        async with AsyncSessionLocal() as session:
            return list(
                (await session.execute(select(PatientIdentityConflict))).scalars().all()
            )

    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# 1. identical MRNs in two tenants stay distinct
# --------------------------------------------------------------------------- #
def test_same_mrn_in_two_tenants_creates_distinct_patients(client, tenant_ids):
    a = _resolve(_meta("MRN-12345", "Tenant A Patient"), tenant_id=tenant_ids["a"])
    b = _resolve(_meta("MRN-12345", "Tenant B Patient"), tenant_id=tenant_ids["b"])

    assert a[0] == "created" and b[0] == "created"
    assert a[3] != b[3], "each tenant must get its own patient row"
    assert a[4] == tenant_ids["a"] and b[4] == tenant_ids["b"]

    rows = _patients("MRN-12345")
    assert len(rows) == 2, f"expected two distinct patients, got {rows}"
    assert {r[0] for r in rows} == {tenant_ids["a"], tenant_ids["b"]}


def test_same_tenant_same_mrn_reuses_the_patient(client, tenant_ids):
    first = _resolve(_meta("MRN-999", "Same Patient"), tenant_id=tenant_ids["a"])
    second = _resolve(_meta("MRN-999", "Same Patient"), tenant_id=tenant_ids["a"])
    assert first[3] == second[3]
    assert second[0] == "matched"
    assert len(_patients("MRN-999")) == 1


def test_identity_is_recorded_with_the_tenant_authority(client, tenant_ids):
    outcome = _resolve(_meta("MRN-AUTH"), tenant_id=tenant_ids["a"])
    expected = default_authority_oid(tenant_ids["a"])
    assert outcome[2] == expected and expected.startswith("TENANT_")


def test_issuer_of_patient_id_is_recorded_as_the_authority(client, tenant_ids):
    """The issuer qualifies the identity domain (0010,0021)."""
    outcome = _resolve(_meta("MRN-ISS", issuer="1.2.3.4.5.6.7"), tenant_id=tenant_ids["a"])
    assert outcome[2] == "1.2.3.4.5.6.7", "the issuer must be used as the assigning authority"
    assert outcome[0] == "created"


def test_same_mrn_under_a_second_authority_links_to_the_same_patient(client, tenant_ids):
    """Within one tenant an MRN identifies one local patient.

    The assigning authority qualifies how an identity was *found* — the PIX
    lookup key is (tenant, authority, identifier) — while `patients` is unique on
    (tenant, patient_id). So a second authority carrying the same MRN records an
    additional identity on the same local patient instead of fragmenting that
    institution's own records. Separating *institutions* is what C10 requires,
    and the two-tenant tests above cover it.

    (This resolves the question left open earlier; the resolver previously also
    mis-reported the outcome as "created" when it had adopted the existing row.)
    """
    first = _resolve(_meta("MRN-ISS2", issuer="1.2.3.4.5.6.7"), tenant_id=tenant_ids["a"])
    second = _resolve(_meta("MRN-ISS2", issuer="9.9.9.9"), tenant_id=tenant_ids["a"])

    assert second[3] == first[3], "one MRN is one local patient within a tenant"
    assert second[0] == "matched", "adopting the existing row is a match, not a create"
    assert second[2] == "9.9.9.9", "the second assigning authority is recorded"
    identities = _identities(first[3])
    assert len(identities) == 2, f"both authorities must attach to the patient: {identities}"
    assert {i[2] for i in identities} == {tenant_ids["a"]}


def test_conflicting_demographics_are_quarantined_not_overwritten(client, tenant_ids):
    """REQUIRED EVIDENCE: a later ingest cannot rewrite validated demographics."""
    original = _resolve(
        _meta("MRN-CONFLICT", "Original^Name", birth="19800101", sex="M"),
        tenant_id=tenant_ids["a"],
    )
    assert original[0] == "created"

    # A later ingest for the same (tenant, MRN) carries different demographics.
    second = _resolve(
        _meta("MRN-CONFLICT", "Different^Name", birth="19900202", sex="F"),
        tenant_id=tenant_ids["a"],
    )
    assert second[0] == "conflict", second
    assert second[5] == "Original^Name", "the validated name must be untouched"
    assert set(second[6]) == {"patient_name", "patient_birth_date", "patient_sex"}

    # The stored row itself is unchanged...
    rows = _patients("MRN-CONFLICT")
    assert rows == [(tenant_ids["a"], "MRN-CONFLICT", "Original^Name")]

    # ...and the discrepancy is recorded for human reconciliation.
    conflicts = _conflicts()
    assert len(conflicts) == 1
    record = conflicts[0]
    assert record.patient_id == original[3]
    assert record.tenant_id == tenant_ids["a"]
    assert record.resolved is False
    assert "Original^Name" in record.detail and "Different^Name" in record.detail


def test_empty_fields_are_backfilled_but_populated_ones_are_not(client, tenant_ids):
    _resolve(_meta("MRN-BACKFILL", "Known^Name"), tenant_id=tenant_ids["a"])
    # No name supplied -> nothing to compare, and the existing name survives.
    after = _resolve(_meta("MRN-BACKFILL", sex="F"), tenant_id=tenant_ids["a"])
    assert after[0] == "matched"
    assert after[5] == "Known^Name"
    rows = _patients("MRN-BACKFILL")
    assert rows == [(tenant_ids["a"], "MRN-BACKFILL", "Known^Name")]


def test_missing_patient_id_creates_a_per_study_identity(client):
    first_study, second_study = generate_uid(), generate_uid()
    a = _resolve(_meta(None, "Unknown^One", study_uid=first_study))
    b = _resolve(_meta(None, "Unknown^Two", study_uid=second_study))

    assert a[0] == "unidentified" and b[0] == "unidentified"
    assert a[1] == f"{UNIDENTIFIED_PREFIX}{first_study}"
    assert b[1] == f"{UNIDENTIFIED_PREFIX}{second_study}"
    assert a[3] != b[3], "unidentified patients must never share a row"
    assert len(_patients(a[1])) == 1 and len(_patients(b[1])) == 1


def test_no_shared_anonymous_patient(client):
    """The old code collapsed every blank MRN into one literal 'ANONYMOUS' row."""
    for _ in range(3):
        _resolve(_meta(None))
    assert _patients("ANONYMOUS") == [], "no shared ANONYMOUS identity may exist"


def test_blank_id_is_stable_for_the_same_study(client):
    study = generate_uid()
    first = _resolve(_meta(None, study_uid=study))
    second = _resolve(_meta(None, study_uid=study))
    assert first[3] == second[3], "re-ingesting the same study keeps one identity"


# --------------------------------------------------------------------------- #
# 4. legacy / unscoped behaviour is unchanged
# --------------------------------------------------------------------------- #
def test_legacy_ingest_still_resolves_without_a_tenant(client):
    first = _resolve(_meta("LEGACY-MRN", "Legacy^Patient"), tenant_id=None)
    second = _resolve(_meta("LEGACY-MRN", "Legacy^Patient"), tenant_id=None)
    assert first[0] == "created" and second[0] == "matched"
    assert first[3] == second[3]
    assert first[2] == "LEGACY_DEFAULT"


def test_legacy_and_tenant_patients_are_separate(client, tenant_ids):
    legacy = _resolve(_meta("SHARED-MRN", "Legacy"), tenant_id=None)
    tenant = _resolve(_meta("SHARED-MRN", "Tenant"), tenant_id=tenant_ids["a"])
    assert legacy[3] != tenant[3]
    assert len(_patients("SHARED-MRN")) == 2


# --------------------------------------------------------------------------- #
# 5. end to end through the ingest API
# --------------------------------------------------------------------------- #
def _stow_with_mrn(client, headers, mrn: str, name: str) -> str:
    study_uid = generate_uid()
    ds = make_instance(
        patient_id=mrn,
        patient_name=name,
        study_uid=study_uid,
        series_uid=generate_uid(),
        sop_uid=generate_uid(),
        instance_num=1,
        rows=8,
        cols=8,
        pixel=np.full((8, 8), 40, dtype=np.uint16),
    )
    resp = client.post(
        "/dicom-web/studies",
        content=build_stow_body([ds_bytes(ds)]),
        headers={**stow_headers(), **headers},
    )
    assert resp.status_code == 200, resp.text
    return study_uid


def test_stow_from_two_tenants_keeps_patients_apart(client, auth_headers):
    """The same MRN ingested by two institutions must not share a patient row."""
    ta = _make_tenant(client, auth_headers, "C10 Tenant A", "c10_a")
    tb = _make_tenant(client, auth_headers, "C10 Tenant B", "c10_b")
    user_a = create_user(client, auth_headers, "c10_user_a", "RADIOLOGIST", tenant_id=ta["id"])
    user_b = create_user(client, auth_headers, "c10_user_b", "RADIOLOGIST", tenant_id=tb["id"])

    _stow_with_mrn(client, user_a, "SHARED-API-MRN", "Institution A")
    _stow_with_mrn(client, user_b, "SHARED-API-MRN", "Institution B")

    rows = _patients("SHARED-API-MRN")
    assert len(rows) == 2, f"cross-tenant MRN conflation: {rows}"
    assert {r[0] for r in rows} == {ta["id"], tb["id"]}


def test_stow_conflict_does_not_rewrite_demographics(client, auth_headers):
    ta = _make_tenant(client, auth_headers, "C10 Conflict", "c10_c")
    user = create_user(client, auth_headers, "c10_user_c", "RADIOLOGIST", tenant_id=ta["id"])

    _stow_with_mrn(client, user, "CONFLICT-API-MRN", "Original^Name")
    _stow_with_mrn(client, user, "CONFLICT-API-MRN", "Overwrite^Attempt")

    rows = _patients("CONFLICT-API-MRN")
    assert len(rows) == 1
    assert rows[0][2] == "Original^Name", "the second ingest overwrote the patient name"
    assert len(_conflicts()) >= 1
