"""VNA (Vendor Neutral Archive) tests.

Covers the tenant model of STANDARD_PACS_FEATURES_PLAN.md §2: institutions /
departments with studies isolated per AE title. Verifies:

  * tenant CRUD + admin-only permissions + user assignment,
  * ingest isolation — C-STORE from a mapped AE tags the study with the tenant,
  * query isolation — C-FIND / C-MOVE from a mapped AE only see that tenant,
  * REST/QIDO scoping — a tenant user browses only their tenant (guests and
    unassigned users keep the legacy unscoped scope),
  * backward compatibility — pre-VNA (unscoped) data still flows end to end.
"""

import socket
import time

import pydicom
from pynetdicom import AE, StoragePresentationContexts, evt
from pynetdicom.sop_class import (
    CTImageStorage,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
)
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian
from sqlalchemy import select

from app.db.models import Study, User
from app.db.session import SyncSessionLocal
from tests.helpers import auth_for_role, build_stow_body, ds_bytes, make_instance, stow_headers


def _store_as(host, port, datasets: list[bytes], calling_ae: str) -> list[int]:
    """C-STORE via an SCU whose *calling* AE title is `calling_ae` (VNA
    isolation is keyed off the requestor's AE title)."""
    ae = AE(ae_title=calling_ae)
    ae.add_requested_context(CTImageStorage, ExplicitVRLittleEndian)
    assoc = ae.associate(host, port, ae_title="PAXS_TEST_SCP")
    assert assoc.is_established, "association failed"
    statuses = []
    for i, data in enumerate(datasets):
        status = assoc.send_c_store(
            pydicom.dcmread(pydicom.filebase.BytesIO(data)), msg_id=i + 1
        )
        statuses.append(int(status.Status) if status else -1)
    assoc.release()
    return statuses


def _auth(client, username, password="secret123"):
    client.post("/api/v1/auth/register", json={"username": username, "password": password})
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _wait_for(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met within timeout")


def _make_tenant(client, admin_h, name, code, **extra):
    r = client.post(
        "/api/v1/tenants", headers=admin_h,
        json={"name": name, "code": code, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _stow(client, ds, headers=None):
    r = client.post(
        "/dicom-web/studies",
        headers={**stow_headers("vna-test-1"), **(headers or {})},
        content=build_stow_body([ds_bytes(ds)], boundary="vna-test-1"),
    )
    assert r.status_code == 200, r.text
    return r


def _c_find_study_uids(host, port, querying_ae) -> list[str]:
    ae = AE(ae_title=querying_ae)
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind, ExplicitVRLittleEndian)
    assoc = ae.associate(host, port, ae_title="PAXS_TEST_SCP")
    assert assoc.is_established, "C-FIND association failed"
    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    query.StudyInstanceUID = ""
    query.PatientName = ""
    found = []
    for status, identifier in assoc.send_c_find(
        query, query_model=StudyRootQueryRetrieveInformationModelFind
    ):
        if status and status.Status == 0xFF00 and identifier is not None:
            found.append(str(identifier.StudyInstanceUID))
    assoc.release()
    return found


# --------------------------------------------------------------------------- #
# Tenant CRUD + permissions
# --------------------------------------------------------------------------- #
def test_tenant_crud_and_permissions(client):
    admin_h = _auth(client, "boss")
    r = client.get("/api/v1/tenants", headers=admin_h)
    assert r.status_code == 200 and r.json() == []

    t1 = _make_tenant(client, admin_h, "Radiology Dept A", "rad-a",
                      description="First floor")
    assert t1["code"] == "rad-a" and t1["is_active"] is True
    assert t1["ae_titles"] == [] and t1["study_count"] == 0

    # Duplicate name / code -> 409
    assert client.post("/api/v1/tenants", headers=admin_h,
                       json={"name": "Radiology Dept A", "code": "rad-b"}).status_code == 409
    assert client.post("/api/v1/tenants", headers=admin_h,
                       json={"name": "Other", "code": "rad-a"}).status_code == 409

    # Radiologist may list but not create/modify (ADMIN-only writes).
    # H02: a privileged role cannot self-register, so it is provisioned by the
    # admin (same helper the user-management tests use).
    rad_h = auth_for_role(client, "dr.rad", role="RADIOLOGIST", admin=admin_h)
    assert client.get("/api/v1/tenants", headers=rad_h).status_code == 200
    assert client.post("/api/v1/tenants", headers=rad_h,
                       json={"name": "No", "code": "no"}).status_code == 403
    assert client.patch("/api/v1/tenants/999", headers=rad_h,
                        json={"is_active": False}).status_code == 403

    # Update
    r = client.patch(f"/api/v1/tenants/{t1['id']}", headers=admin_h,
                     json={"is_active": False, "description": "Renovation"})
    assert r.status_code == 200 and r.json()["is_active"] is False

    # AE mapping + unmap
    assert client.post(f"/api/v1/tenants/{t1['id']}/aes", headers=admin_h,
                       json={"ae_title": "CT1"}).status_code == 201
    assert client.post(f"/api/v1/tenants/{t1['id']}/aes", headers=admin_h,
                       json={"ae_title": "CT1"}).status_code == 409  # duplicate
    assert client.delete(f"/api/v1/tenants/{t1['id']}/aes/CT1",
                         headers=admin_h).status_code == 204

    # Delete
    assert client.delete(f"/api/v1/tenants/{t1['id']}", headers=admin_h).status_code == 204
    assert client.get("/api/v1/tenants", headers=admin_h).json() == []

    # Unauthenticated -> 401. The logins above left a session cookie in the
    # shared client and the API also accepts cookie sessions (C03), so clear
    # the jar to make this request genuinely anonymous.
    client.cookies.clear()
    assert client.get("/api/v1/tenants").status_code == 401


def test_user_assignment_and_context(client):
    admin_h = _auth(client, "adm.vna")
    t = _make_tenant(client, admin_h, "Cardiology", "cardio")
    user_h = _auth(client, "dr.cardio")

    r = client.get("/api/v1/tenants/context", headers=user_h)
    assert r.json() == {"is_admin": False, "tenant_id": None,
                        "tenant_name": None, "scoped": False}

    assert client.post(f"/api/v1/tenants/{t['id']}/users", headers=admin_h,
                       json={"username": "dr.cardio"}).status_code == 200
    r = client.get("/api/v1/tenants/context", headers=user_h)
    assert r.json()["scoped"] is True and r.json()["tenant_name"] == "Cardiology"

    # Unknown user -> 404
    assert client.post(f"/api/v1/tenants/{t['id']}/users", headers=admin_h,
                       json={"username": "nobody"}).status_code == 404

    assert client.delete(f"/api/v1/tenants/{t['id']}/users/dr.cardio",
                         headers=admin_h).status_code == 204
    r = client.get("/api/v1/tenants/context", headers=user_h)
    assert r.json()["scoped"] is False

    # Admin context
    r = client.get("/api/v1/tenants/context", headers=admin_h)
    assert r.json()["is_admin"] is True


def test_ae_title_normalization_and_validation(client):
    """AE titles are uppercased on map/unmap and validated (DICOM PS3.5)."""
    admin_h = _auth(client, "adm.norm")
    t = _make_tenant(client, admin_h, "Norm Dept", "norm")

    # Lowercase input maps to its uppercase canonical form.
    r = client.post(f"/api/v1/tenants/{t['id']}/aes", headers=admin_h,
                    json={"ae_title": "ct_norm"})
    assert r.status_code == 201 and r.json()["ae_title"] == "CT_NORM"
    # A different case is the same AE -> duplicate.
    assert client.post(f"/api/v1/tenants/{t['id']}/aes", headers=admin_h,
                       json={"ae_title": "CT_NORM"}).status_code == 409

    # Invalid AE titles are rejected outright.
    for bad in ("bad/ae title!", "", "x" * 17, "sp ace", "semi;colon"):
        assert client.post(f"/api/v1/tenants/{t['id']}/aes", headers=admin_h,
                           json={"ae_title": bad}).status_code == 422, bad

    # Unmapping normalizes too (case-insensitive delete).
    assert client.delete(f"/api/v1/tenants/{t['id']}/aes/ct_norm",
                         headers=admin_h).status_code == 204


# --------------------------------------------------------------------------- #
# DIMSE isolation by AE title (real C-STORE + C-FIND + C-MOVE)
# --------------------------------------------------------------------------- #
def test_ae_ingest_and_cfind_isolation(receiver, client):
    port, _ = receiver
    admin_h = _auth(client, "vna.chief")
    t_a = _make_tenant(client, admin_h, "Tenant A", "ta")
    t_b = _make_tenant(client, admin_h, "Tenant B", "tb")
    client.post(f"/api/v1/tenants/{t_a['id']}/aes", headers=admin_h,
                json={"ae_title": "CT_TENANT_A"}).status_code == 201
    client.post(f"/api/v1/tenants/{t_b['id']}/aes", headers=admin_h,
                json={"ae_title": "CT_TENANT_B"}).status_code == 201

    sa = "1.2.840.113619.2.408.500.1"
    sb = "1.2.840.113619.2.408.500.2"
    sx = "1.2.840.113619.2.408.500.3"
    ds_a = make_instance(study_uid=sa, series_uid="1.2.3.4.500.1", sop_uid="1.2.3.5.500.1")
    ds_b = make_instance(study_uid=sb, series_uid="1.2.3.4.500.2", sop_uid="1.2.3.5.500.2")
    ds_x = make_instance(study_uid=sx, series_uid="1.2.3.4.500.3", sop_uid="1.2.3.5.500.3")

    # Ingest: each study arrives from a different (calling) AE title.
    assert _store_as("127.0.0.1", port, [ds_bytes(ds_a)], "CT_TENANT_A") == [0x0000]
    assert _store_as("127.0.0.1", port, [ds_bytes(ds_b)], "CT_TENANT_B") == [0x0000]
    assert _store_as("127.0.0.1", port, [ds_bytes(ds_x)], "CT_LEGACY") == [0x0000]

    def _all_stored():
        with SyncSessionLocal() as s:
            return s.execute(select(Study)).scalars().all()
    _wait_for(lambda: len(_all_stored()) == 3)

    with SyncSessionLocal() as s:
        studies = {st.study_instance_uid: st for st in s.execute(select(Study)).scalars().all()}
        assert studies[sa].tenant_id == t_a["id"]
        assert studies[sb].tenant_id == t_b["id"]
        assert studies[sx].tenant_id is None

    # C-FIND isolation: each AE sees exactly its own scope.
    assert _c_find_study_uids("127.0.0.1", port, "CT_TENANT_A") == [sa]
    assert _c_find_study_uids("127.0.0.1", port, "CT_TENANT_B") == [sb]
    assert set(_c_find_study_uids("127.0.0.1", port, "PAXS_QUERY_SCU")) == {sx}


def test_c_move_isolation(receiver, client):
    """A mapped AE can only C-MOVE instances inside its own tenant."""
    src_port, _ = receiver
    admin_h = _auth(client, "vna.mover")
    t_a = _make_tenant(client, admin_h, "Mover A", "mv-a")
    client.post(f"/api/v1/tenants/{t_a['id']}/aes", headers=admin_h,
                json={"ae_title": "MV_A"}).status_code == 201

    mine = "1.2.840.113619.2.408.501.1"
    theirs = "1.2.840.113619.2.408.501.2"
    ds_mine = make_instance(study_uid=mine, series_uid="1.2.3.4.501.1",
                            sop_uid="1.2.3.5.501.1", instance_num=1)
    ds_theirs = make_instance(study_uid=theirs, series_uid="1.2.3.4.501.2",
                              sop_uid="1.2.3.5.501.2")
    assert _store_as("127.0.0.1", src_port, [ds_bytes(ds_mine)], "MV_A") == [0x0000]
    # The other study lands in the legacy (unscoped) scope — not MV_A's tenant.
    assert _store_as("127.0.0.1", src_port, [ds_bytes(ds_theirs)], "MV_OTHER") == [0x0000]

    def _stored(uid):
        with SyncSessionLocal() as s:
            return s.execute(select(Study).where(Study.study_instance_uid == uid)).scalar_one_or_none()
    _wait_for(lambda: _stored(mine) is not None and _stored(theirs) is not None)

    # Destination SCP that records received SOP UIDs.
    from pynetdicom.sop_class import Verification
    received = []

    def on_store(event):
        received.append(str(event.dataset.SOPInstanceUID))
        return 0x0000

    def on_echo(event):
        return 0x0000

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        dest_port = s.getsockname()[1]
    dest_ae = AE(ae_title="VNA_DEST")
    dest_ae.supported_contexts = StoragePresentationContexts
    dest_ae.add_supported_context(Verification, ExplicitVRLittleEndian)
    dest_ae.start_server(("127.0.0.1", dest_port), block=False, evt_handlers=[
        (evt.EVT_C_STORE, on_store), (evt.EVT_C_ECHO, on_echo),
    ])
    from app.db.models import DicomNode
    with SyncSessionLocal() as s:
        s.add(DicomNode(ae_title="VNA_DEST", host="127.0.0.1", port=dest_port, enabled=True))
        s.commit()

    def _move(uid, querying_ae):
        ae = AE(ae_title=querying_ae)
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove, ExplicitVRLittleEndian)
        assoc = ae.associate("127.0.0.1", src_port, ae_title="PAXS_TEST_SCP")
        assert assoc.is_established
        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = uid
        final = None
        for status, _ in assoc.send_c_move(
            query, move_aet="VNA_DEST", query_model=StudyRootQueryRetrieveInformationModelMove
        ):
            if status is not None:
                final = int(status.Status)
        assoc.release()
        return final

    try:
        # Can move its own tenant study...
        assert _move(mine, "MV_A") == 0x0000
        _wait_for(lambda: "1.2.3.5.501.1" in received)
        # ...but a cross-tenant study yields zero sub-operations.
        assert _move(theirs, "MV_A") == 0x0000
        time.sleep(0.3)
        assert "1.2.3.5.501.2" not in received
    finally:
        dest_ae.shutdown()


# --------------------------------------------------------------------------- #
# REST / QIDO scoping by user + backward compatibility
# --------------------------------------------------------------------------- #
def test_rest_and_qido_scoping(client):
    admin_h = _auth(client, "adm.chief")
    t_a = _make_tenant(client, admin_h, "Scope A", "sc-a")
    t_b = _make_tenant(client, admin_h, "Scope B", "sc-b")

    dr_a_h = _auth(client, "dr.scope.a")
    dr_b_h = _auth(client, "dr.scope.b")
    assert client.post(f"/api/v1/tenants/{t_a['id']}/users", headers=admin_h,
                       json={"username": "dr.scope.a"}).status_code == 200
    assert client.post(f"/api/v1/tenants/{t_b['id']}/users", headers=admin_h,
                       json={"username": "dr.scope.b"}).status_code == 200

    sa = "1.2.840.113619.2.408.502.1"
    sb = "1.2.840.113619.2.408.502.2"
    sx = "1.2.840.113619.2.408.502.3"
    _stow(client, make_instance(study_uid=sa, series_uid="1.2.3.4.502.1",
                                sop_uid="1.2.3.5.502.1"), headers=dr_a_h)
    _stow(client, make_instance(study_uid=sb, series_uid="1.2.3.4.502.2",
                                sop_uid="1.2.3.5.502.2"), headers=dr_b_h)
    # A guest STOW must be genuinely unauthenticated: the logins above left a
    # session cookie in the shared client, and ingest attributes a study to the
    # authenticated caller's tenant (C04).
    client.cookies.clear()
    _stow(client, make_instance(study_uid=sx, series_uid="1.2.3.4.502.3",
                                sop_uid="1.2.3.5.502.3"))  # guest STOW -> legacy

    with SyncSessionLocal() as s:
        tenant_of = {st.study_instance_uid: st.tenant_id
                     for st in s.execute(select(Study)).scalars().all()}
        assert tenant_of[sa] == t_a["id"]
        assert tenant_of[sb] == t_b["id"]
        assert tenant_of[sx] is None

    def _study_uids(headers=None, params=None):
        r = client.get("/api/v1/studies", headers=headers, params=params or {})
        assert r.status_code == 200
        return {i["study_uid"] for i in r.json()["items"]}

    # Tenant user A sees only A; user B only B; admin all.
    assert _study_uids(headers=dr_a_h) == {sa}
    assert _study_uids(headers=dr_b_h) == {sb}
    assert _study_uids(headers=admin_h) == {sa, sb, sx}
    # C03: the REST study list is authenticated-only, so an anonymous caller is
    # refused; a signed-in user *without* a tenant keeps the legacy scope (the
    # anonymous path for legacy data is DICOMweb QIDO, checked below).
    client.cookies.clear()
    assert client.get("/api/v1/studies").status_code == 401
    legacy_h = auth_for_role(client, "dr.legacy", role="PHYSICIAN", admin=admin_h)
    assert _study_uids(headers=legacy_h) == {sx}
    # Admin tenant filter
    assert _study_uids(headers=admin_h, params={"tenant": t_a["id"]}) == {sa}
    assert _study_uids(headers=admin_h, params={"tenant": "none"}) == {sx}

    # Tenant label surfaced in listings
    r = client.get("/api/v1/studies", headers=admin_h)
    by_uid = {i["study_uid"]: i for i in r.json()["items"]}
    assert by_uid[sa]["tenant_name"] == "Scope A"
    assert by_uid[sa]["tenant_id"] == t_a["id"]
    assert by_uid[sx]["tenant_name"] is None

    # QIDO-RS obeys the same scope
    def _qido(headers=None):
        r = client.get("/dicom-web/studies", headers=headers)
        assert r.status_code == 200
        return {row["0020000D"]["Value"][0] for row in r.json()}
    assert _qido(headers=dr_a_h) == {sa}
    assert _qido() == {sx}


def test_delete_tenant_releases_studies_to_legacy(client):
    admin_h = _auth(client, "adm.clean")
    t = _make_tenant(client, admin_h, "Temp Dept", "tmp")
    user_h = _auth(client, "dr.tmp")
    client.post(f"/api/v1/tenants/{t['id']}/users", headers=admin_h,
                json={"username": "dr.tmp"}).status_code == 200
    client.post(f"/api/v1/tenants/{t['id']}/aes", headers=admin_h,
                json={"ae_title": "TMP_CT"}).status_code == 201

    suid = "1.2.840.113619.2.408.503.1"
    _stow(client, make_instance(study_uid=suid, series_uid="1.2.3.4.503.1",
                                sop_uid="1.2.3.5.503.1"), headers=user_h)
    with SyncSessionLocal() as s:
        assert s.execute(select(Study).where(Study.study_instance_uid == suid)).scalar_one().tenant_id == t["id"]

    assert client.delete(f"/api/v1/tenants/{t['id']}", headers=admin_h).status_code == 204

    # Data preserved, released back to the legacy scope (both engines).
    with SyncSessionLocal() as s:
        study = s.execute(select(Study).where(Study.study_instance_uid == suid)).scalar_one()
        assert study.tenant_id is None
        user = s.execute(select(User).where(User.username == "dr.tmp")).scalar_one()
        assert user.tenant_id is None
    # AE mapping is gone too
    assert client.get("/api/v1/tenants", headers=admin_h).json() == []
