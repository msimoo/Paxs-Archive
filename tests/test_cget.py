"""Feature #4 — C-GET (SCP/SCU) milestone tests.

The C-GET SCP side (handle_get, tenant-scoped retrieval) is covered in
test_receiver / test_vna. This suite exercises the SCU + ingest path over
real DIMSE:

  * ``sender.send_get`` — raw C-GET SCU (collects received instance datasets),
  * ``sender.pull_from_node_sync`` — node-aware C-GET that stores + indexes
    every received instance (idempotent re-import, VNA tenant tagging),
  * the REST trigger ``POST /api/v1/nodes/{ae}/pull``,
  * resilience — one bad instance never aborts the rest of the pull,
  * error paths — unknown/disabled node, bad level, non-admin, no auth.
"""

import socket
import time

from pydicom.dataset import Dataset
from pynetdicom import AE, StoragePresentationContexts, evt
from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelGet
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
from sqlalchemy import select

from app.db.models import AuditLog, Instance, Study
from app.db.session import SyncSessionLocal
from app.services import storage_service
from tests.helpers import ds_bytes, make_instance
from tests.test_receiver import _store_via_scu


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cget_recording_scp(port, ae_title="PAXS_SOURCE", instances=None):
    """A remote archive that answers C-GET by yielding the given instance
    datasets (defaults to a 2-instance CT study) — lets us pull from a peer
    that is *not* our own receiver and observe what the SCP received."""
    instances = instances if instances is not None else [
        make_instance(study_uid="1.2.840.113619.2.408.777.1",
                      series_uid="1.2.3.4.777.1", sop_uid="1.2.3.5.777.1",
                      patient_id="PULL001", accession="ACC-PULL-1", instance_num=1),
        make_instance(study_uid="1.2.840.113619.2.408.777.1",
                      series_uid="1.2.3.4.777.1", sop_uid="1.2.3.5.777.2",
                      patient_id="PULL001", accession="ACC-PULL-1", instance_num=2),
    ]
    received_queries = []

    def on_get(event):
        q = event.identifier
        received_queries.append(str(getattr(q, "QueryRetrieveLevel", "")))
        # C-GET protocol: the sub-operation count must be yielded first;
        # pynetdicom sends the final response when the generator is exhausted.
        yield len(instances)
        for ds in instances:
            yield 0xFF00, ds

    ae = AE(ae_title=ae_title)
    # The C-GET SCP pushes instances back as C-STORE sub-operations, so it
    # must support the storage SOP classes explicitly (role negotiation
    # requires declared contexts — auto-accepted contexts carry no roles).
    for cx in StoragePresentationContexts:
        ae.add_supported_context(
            cx.abstract_syntax, [ImplicitVRLittleEndian, ExplicitVRLittleEndian],
            scp_role=True, scu_role=False,
        )
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelGet, ExplicitVRLittleEndian)
    ae.start_server(("127.0.0.1", port), block=False, evt_handlers=[(evt.EVT_C_GET, on_get)])
    return ae, received_queries


def _cget_empty_scp(port, ae_title="PAXS_EMPTY"):
    """A remote archive that answers C-GET with no matches."""

    def on_get(event):
        yield 0  # no matches


    ae = AE(ae_title=ae_title)
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelGet, ExplicitVRLittleEndian)
    ae.start_server(("127.0.0.1", port), block=False, evt_handlers=[(evt.EVT_C_GET, on_get)])
    return ae


def _reset_db():
    """Plain (non-HTTP) tests share the suite DB — truncate so registrations
    and ingested rows from earlier tests don't collide."""
    from app.db.models import Base
    from app.db.session import sync_engine

    with sync_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


def _wait_for(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met within timeout")


def _instance_count() -> int:
    with SyncSessionLocal() as s:
        return len(s.execute(select(Instance)).scalars().all())


def _auth(client, username, password="secret123", role=None):
    body = {"username": username, "password": password}
    if role:
        body["role"] = role
    client.post("/api/v1/auth/register", json=body)
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _register_node(client, admin_h, ae_title, host, port, enabled=True):
    r = client.post("/api/v1/nodes", headers=admin_h, json={
        "ae_title": ae_title, "host": host, "port": port, "enabled": enabled,
    })
    assert r.status_code == 201, r.text


def _make_tenant(client, admin_h, name, code):
    r = client.post("/api/v1/tenants", headers=admin_h,
                    json={"name": name, "code": code})
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# send_get SCU — raw helper
# --------------------------------------------------------------------------- #
def test_send_get_scu_against_remote():
    """sender.send_get pulls instances from a *remote* SCP over C-GET and
    collects the 0xFF00 payloads with the right transfer status."""
    port = _free_port()
    source_ae, received_queries = _cget_recording_scp(port)
    try:
        from app.dicom.sender import send_get

        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = "1.2.840.113619.2.408.777.1"
        result = send_get("127.0.0.1", port, "PAXS_SOURCE", query)
        assert result["ok"] is True
        assert result["status"] == 0x0000
        assert len(result["received"]) == 2
        sop_uids = {str(ds.SOPInstanceUID) for ds in result["received"]}
        assert sop_uids == {"1.2.3.5.777.1", "1.2.3.5.777.2"}
        assert received_queries == ["STUDY"]
    finally:
        source_ae.shutdown()


def test_send_get_rejects_bad_peer():
    from app.dicom.sender import send_get

    result = send_get("127.0.0.1", 1, "GHOST")
    assert result["ok"] is False
    assert result["reason"] == "association rejected"


def test_send_get_against_our_receiver(receiver):
    """send_get works against our own C-GET SCP: store a study, then pull it
    back out over C-GET and verify the datasets round-trip."""
    src_port, _ = receiver
    study_uid = "1.2.840.113619.2.408.778"
    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.778",
                       sop_uid="1.2.3.5.778.1", patient_id="GET778")
    assert _store_via_scu("127.0.0.1", src_port, [ds_bytes(ds)]) == [0x0000]

    def _stored():
        with SyncSessionLocal() as s:
            return s.execute(select(Study).where(Study.study_instance_uid == study_uid)).scalar_one_or_none()
    _wait_for(lambda: _stored() is not None)

    from app.dicom.sender import send_get

    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    query.StudyInstanceUID = study_uid
    result = send_get("127.0.0.1", src_port, "PAXS_TEST_SCP", query)
    assert result["ok"] is True
    assert len(result["received"]) == 1
    assert str(result["received"][0].SOPInstanceUID) == "1.2.3.5.778.1"


# --------------------------------------------------------------------------- #
# pull_from_node_sync — node-aware C-GET + local ingest
# --------------------------------------------------------------------------- #
def test_pull_from_node_round_trip():
    """pull_from_node_sync resolves a registered node, C-GETs the study and
    ingests every received instance through the normal pipeline (real DB rows,
    STORED audit rows with the dicom-pull provenance)."""
    _reset_db()
    port = _free_port()
    source_ae, _ = _cget_recording_scp(port)
    try:
        from app.db.models import DicomNode
        with SyncSessionLocal() as s:
            s.add(DicomNode(ae_title="PAXS_SOURCE", host="127.0.0.1", port=port, enabled=True))
            s.commit()

        from app.dicom.sender import pull_from_node_sync

        result = pull_from_node_sync(
            "PAXS_SOURCE", level="STUDY",
            filters={"StudyInstanceUID": "1.2.840.113619.2.408.777.1"},
        )
        assert result["ok"] is True, result
        assert result["pulled"] == 2 and result["failed"] == 0

        with SyncSessionLocal() as s:
            instances = {i.sop_instance_uid for i in s.execute(select(Instance)).scalars().all()}
            assert instances == {"1.2.3.5.777.1", "1.2.3.5.777.2"}
            study = s.execute(select(Study).where(
                Study.study_instance_uid == "1.2.840.113619.2.408.777.1"
            )).scalar_one()
            assert study.accession_number == "ACC-PULL-1"
            # Ingest audit rows carry the pull provenance.
            pulls = s.execute(select(AuditLog).where(AuditLog.performed_by == "dicom-pull")).scalars().all()
            assert len(pulls) == 2
            assert all(a.action == "STORED" for a in pulls)
    finally:
        source_ae.shutdown()


def test_pull_is_idempotent():
    """Re-pulling the same study must not duplicate rows (upsert by SOP UID)."""
    _reset_db()
    port = _free_port()
    source_ae, _ = _cget_recording_scp(port)
    try:
        from app.db.models import DicomNode
        with SyncSessionLocal() as s:
            s.add(DicomNode(ae_title="PAXS_SOURCE", host="127.0.0.1", port=port, enabled=True))
            s.commit()

        from app.dicom.sender import pull_from_node_sync

        kw = {"level": "STUDY", "filters": {"StudyInstanceUID": "1.2.840.113619.2.408.777.1"}}
        assert pull_from_node_sync("PAXS_SOURCE", **kw)["pulled"] == 2
        assert pull_from_node_sync("PAXS_SOURCE", **kw)["pulled"] == 2
        assert _instance_count() == 2  # still only two rows
    finally:
        source_ae.shutdown()


def test_pull_empty_result():
    """A remote that matches nothing yields ok=True with zero instances."""
    _reset_db()
    port = _free_port()
    source_ae = _cget_empty_scp(port)
    try:
        from app.db.models import DicomNode
        with SyncSessionLocal() as s:
            s.add(DicomNode(ae_title="PAXS_EMPTY", host="127.0.0.1", port=port, enabled=True))
            s.commit()

        from app.dicom.sender import pull_from_node_sync

        result = pull_from_node_sync("PAXS_EMPTY", level="STUDY",
                                     filters={"StudyInstanceUID": "1.2.3.4.999"})
        assert result["ok"] is True
        assert result["pulled"] == 0 and result["results"] == []
    finally:
        source_ae.shutdown()


def test_pull_partial_failure_continues(monkeypatch):
    """One instance that fails to ingest must not abort the rest of the pull."""
    _reset_db()
    port = _free_port()
    source_ae, _ = _cget_recording_scp(port)
    try:
        from app.db.models import DicomNode
        with SyncSessionLocal() as s:
            s.add(DicomNode(ae_title="PAXS_SOURCE", host="127.0.0.1", port=port, enabled=True))
            s.commit()

        calls = {"n": 0}
        orig = storage_service.save_dataset

        def flaky(ds, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return orig(ds, **kw)

        monkeypatch.setattr(storage_service, "save_dataset", flaky)

        from app.dicom.sender import pull_from_node_sync

        result = pull_from_node_sync("PAXS_SOURCE", level="STUDY",
                                     filters={"StudyInstanceUID": "1.2.840.113619.2.408.777.1"})
        assert result["ok"] is True
        assert result["pulled"] == 1 and result["failed"] == 1
        by_sop = {r["sop_uid"]: r for r in result["results"]}
        assert by_sop["1.2.3.5.777.1"]["ok"] is True
        assert by_sop["1.2.3.5.777.2"]["ok"] is False
        assert "disk full" in by_sop["1.2.3.5.777.2"]["reason"]
        assert _instance_count() == 1  # the good instance was still ingested
    finally:
        source_ae.shutdown()


def test_pull_unknown_and_disabled_node():
    _reset_db()
    from app.dicom.sender import pull_from_node_sync

    assert pull_from_node_sync("NO_SUCH")["reason"] == "Node not found"
    from app.db.models import DicomNode
    with SyncSessionLocal() as s:
        s.add(DicomNode(ae_title="DISABLED", host="127.0.0.1", port=1, enabled=False))
        s.commit()
    assert pull_from_node_sync("DISABLED")["reason"] == "Node is disabled"


# --------------------------------------------------------------------------- #
# REST trigger: POST /api/v1/nodes/{ae}/pull
# --------------------------------------------------------------------------- #
def test_rest_pull_end_to_end(client):
    """Full REST flow: register a remote source, pull a study into the archive,
    verify instances landed and PULLED audit rows exist."""
    port = _free_port()
    source_ae, _ = _cget_recording_scp(port)
    try:
        admin_h = _auth(client, "pull.boss")
        _register_node(client, admin_h, "PAXS_SOURCE", "127.0.0.1", port)

        r = client.post("/api/v1/nodes/PAXS_SOURCE/pull", headers=admin_h, json={
            "level": "STUDY",
            "filters": {"StudyInstanceUID": "1.2.840.113619.2.408.777.1"},
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["pulled"] == 2 and body["failed"] == 0
        assert body["level"] == "STUDY" and body["ae_title"] == "PAXS_SOURCE"

        with SyncSessionLocal() as s:
            assert _instance_count() == 2
            # PULLED audit rows (one per ingested instance, by the caller).
            pulls = s.execute(select(AuditLog).where(AuditLog.action == "PULLED")).scalars().all()
            assert {a.sop_instance_uid for a in pulls} == {"1.2.3.5.777.1", "1.2.3.5.777.2"}
            assert all(a.performed_by == "pull.boss" for a in pulls)
    finally:
        source_ae.shutdown()


def test_rest_pull_error_paths(client):
    admin_h = _auth(client, "pull.err")
    _register_node(client, admin_h, "PAXS_TEST_SCP", "127.0.0.1", 1)

    # Unknown node -> 404
    r = client.post("/api/v1/nodes/NO_SUCH/pull", headers=admin_h,
                    json={"level": "STUDY", "filters": {"StudyInstanceUID": "1.2.3"}})
    assert r.status_code == 404
    # Disabled node -> 409
    _register_node(client, admin_h, "DISABLED", "127.0.0.1", 1, enabled=False)
    r = client.post("/api/v1/nodes/DISABLED/pull", headers=admin_h,
                    json={"level": "STUDY", "filters": {"StudyInstanceUID": "1.2.3"}})
    assert r.status_code == 409
    # Bad level -> 422 (pydantic pattern)
    r = client.post("/api/v1/nodes/PAXS_TEST_SCP/pull", headers=admin_h,
                    json={"level": "BOGUS", "filters": {"StudyInstanceUID": "1.2.3"}})
    assert r.status_code == 422
    # Unreachable node -> 502 (remote C-GET failed)
    _register_node(client, admin_h, "GHOST", "127.0.0.1", 1)
    r = client.post("/api/v1/nodes/GHOST/pull", headers=admin_h,
                    json={"level": "STUDY", "filters": {"StudyInstanceUID": "1.2.3"}})
    assert r.status_code == 502
    # No auth -> 401. Logging in above left a session cookie in the shared test
    # client, and these routes also accept cookie sessions (C03), so the jar
    # must be cleared for this request to be genuinely anonymous.
    client.cookies.clear()
    r = client.post("/api/v1/nodes/PAXS_TEST_SCP/pull",
                    json={"level": "STUDY", "filters": {"StudyInstanceUID": "1.2.3"}})
    assert r.status_code == 401


def test_rest_pull_requires_destructive_role(client):
    client.post("/api/v1/auth/register", json={"username": "chief.p", "password": "secret123"})
    admin_h = _auth(client, "chief.p")
    _register_node(client, admin_h, "PAXS_TEST_SCP", "127.0.0.1", 1)
    phys_h = _auth(client, "dr.pullphys", role="PHYSICIAN")
    r = client.post("/api/v1/nodes/PAXS_TEST_SCP/pull", headers=phys_h,
                    json={"level": "STUDY", "filters": {"StudyInstanceUID": "1.2.3"}})
    assert r.status_code == 403


def test_rest_pull_vna_tenant_tagging(client):
    """A tenant-assigned user pulling a study tags it with their tenant."""
    port = _free_port()
    source_ae, _ = _cget_recording_scp(port)
    try:
        admin_h = _auth(client, "pull.chief")
        tenant = _make_tenant(client, admin_h, "Pull Dept", "pull-t")
        _register_node(client, admin_h, "PAXS_SOURCE", "127.0.0.1", port)

        from tests.helpers import create_user

        create_user(client, admin_h, "dr.pull.t", "RADIOLOGIST")
        tenant_h = {
            "Authorization": "Bearer " + client.post(
                "/api/v1/auth/login",
                json={"username": "dr.pull.t", "password": "secret123"},
            ).json()["access_token"]
        }
        assert client.post(f"/api/v1/tenants/{tenant['id']}/users", headers=admin_h,
                           json={"username": "dr.pull.t"}).status_code == 200

        r = client.post("/api/v1/nodes/PAXS_SOURCE/pull", headers=tenant_h, json={
            "level": "STUDY",
            "filters": {"StudyInstanceUID": "1.2.840.113619.2.408.777.1"},
        })
        assert r.status_code == 200, r.text
        assert r.json()["pulled"] == 2

        with SyncSessionLocal() as s:
            study = s.execute(select(Study).where(
                Study.study_instance_uid == "1.2.840.113619.2.408.777.1"
            )).scalar_one()
            assert study.tenant_id == tenant["id"]
    finally:
        source_ae.shutdown()
