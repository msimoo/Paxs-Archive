"""Feature #2 — C-FIND (SCP/SCU) milestone tests.

The SCP side (answer queries against the DB) is covered in test_receiver /
test_vna / test_worklist / test_mpps. This suite exercises the SCU side end to
end over real DIMSE:

  * ``sender.send_find`` — generic C-FIND SCU (raw matches, all Q/R levels),
  * ``sender.query_remote_node_sync`` — node-aware query with keyword filters,
  * the REST trigger ``POST /api/v1/nodes/{ae}/find``,
  * wildcard matching through the SCU -> SCP round trip,
  * prefetch reusing ``send_find`` (query_remote_priors_sync still works),
  * error paths — unknown/disabled node, bad level, non-admin, no auth.
"""

import socket
import time

from pydicom.dataset import Dataset
from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    PatientRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelFind,
    Verification,
)
from pydicom.uid import ExplicitVRLittleEndian
from sqlalchemy import select

from app.db.models import DicomNode, Study
from app.db.session import SyncSessionLocal
from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers
from tests.test_receiver import _store_via_scu


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _recording_scp(port, ae_title="PAXS_DEST"):
    """A minimal destination SCP that answers C-FIND with echo-style matches
    and C-ECHO — lets us test the SCU against a *remote* archive that is not
    our own receiver (so failures / unsupported levels are observable)."""
    received_queries = []

    def on_find(event):
        q = event.identifier
        received_queries.append((str(getattr(q, "QueryRetrieveLevel", "")), dict(q.items())))
        # Echo the query back as a single match with the PatientID filled.
        match = Dataset()
        match.QueryRetrieveLevel = getattr(q, "QueryRetrieveLevel", "STUDY")
        if "PatientID" in q and str(getattr(q, "PatientID", "")).strip():
            match.PatientID = str(q.PatientID)
        match.PatientName = "Remote^Peer"
        match.StudyInstanceUID = "1.2.840.113619.2.408.77.1"
        yield 0xFF00, match
        yield 0x0000, None

    def on_echo(event):
        return 0x0000

    ae = AE(ae_title=ae_title)
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelFind, ExplicitVRLittleEndian)
    ae.add_supported_context(PatientRootQueryRetrieveInformationModelFind, ExplicitVRLittleEndian)
    ae.add_supported_context(Verification, ExplicitVRLittleEndian)
    ae.start_server(("127.0.0.1", port), block=False, evt_handlers=[
        (evt.EVT_C_FIND, on_find), (evt.EVT_C_ECHO, on_echo),
    ])
    return ae, received_queries


def _wait_for(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met within timeout")


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
    return r.json()


def _stow(client, ds, boundary="cfind-1"):
    r = client.post(
        "/dicom-web/studies",
        headers=stow_headers(boundary),
        content=build_stow_body([ds_bytes(ds)], boundary=boundary),
    )
    assert r.status_code == 200, r.text
    return r


def _wait_stored(study_uid):
    def _check():
        with SyncSessionLocal() as s:
            return s.execute(
                select(Study).where(Study.study_instance_uid == study_uid)
            ).scalar_one_or_none()
    _wait_for(lambda: _check() is not None)


# --------------------------------------------------------------------------- #
# send_find SCU — raw helper
# --------------------------------------------------------------------------- #
def test_send_find_scu_against_remote(receiver):
    """sender.send_find queries a *remote* SCP (not our receiver) and collects
    the 0xFF00 matches."""
    port = _free_port()
    dest_ae, received_queries = _recording_scp(port)
    try:
        from app.dicom.sender import send_find

        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.PatientID = "MRN999"
        query.PatientName = ""
        result = send_find("127.0.0.1", port, "PAXS_DEST", query)
        assert result["ok"] is True
        assert result["status"] == 0x0000
        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert str(m.StudyInstanceUID) == "1.2.840.113619.2.408.77.1"
        assert str(m.PatientName) == "Remote^Peer"
    finally:
        dest_ae.shutdown()


def test_send_find_rejects_bad_peer():
    """An unreachable peer yields a graceful failure dict, not an exception."""
    from app.dicom.sender import send_find

    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    result = send_find("127.0.0.1", 1, "GHOST", query)  # port 1 -> refused
    assert result["ok"] is False
    assert result["reason"] == "association rejected"


# --------------------------------------------------------------------------- #
# query_remote_node_sync — node-aware SCU against our own receiver
# --------------------------------------------------------------------------- #
def test_query_remote_node_study_level(receiver):
    """Full round trip: STOW into our receiver, register it as a node, query it
    via query_remote_node_sync and get the study back."""
    src_port, _ = receiver
    study_uid = "1.2.840.113619.2.408.700"
    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.700",
                       sop_uid="1.2.3.5.700.1", patient_id="CFIND001",
                       patient_name="Find^Case")
    assert _store_via_scu("127.0.0.1", src_port, [ds_bytes(ds)]) == [0x0000]
    _wait_stored(study_uid)

    with SyncSessionLocal() as s:
        s.add(DicomNode(ae_title="PAXS_TEST_SCP", host="127.0.0.1", port=src_port, enabled=True))
        s.commit()

    from app.dicom.sender import query_remote_node_sync

    # Query by PatientID, request StudyDate back (empty value).
    result = query_remote_node_sync(
        "PAXS_TEST_SCP", level="STUDY",
        filters={"PatientID": "CFIND001", "StudyInstanceUID": "", "StudyDate": ""},
    )
    assert result["ok"] is True
    assert len(result["matches"]) == 1
    m = result["matches"][0]
    assert m["StudyInstanceUID"] == study_uid
    assert m["StudyDate"] == "20250115"  # returned because requested empty


def test_query_remote_node_auto_return_fields(receiver):
    """A query with only match keys still gets the standard per-level return
    fields back (empty-value request keys added by query_remote_node_sync), and
    numeric VRs (SeriesNumber etc.) never turn into stray None filters."""
    src_port, _ = receiver
    study_uid = "1.2.840.113619.2.408.730"
    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.730",
                       sop_uid="1.2.3.5.730.1", patient_id="RET001",
                       patient_name="Return^Fields", accession="ACC-RET-1")
    assert _store_via_scu("127.0.0.1", src_port, [ds_bytes(ds)]) == [0x0000]
    _wait_stored(study_uid)

    with SyncSessionLocal() as s:
        s.add(DicomNode(ae_title="PAXS_TEST_SCP", host="127.0.0.1", port=src_port, enabled=True))
        s.commit()

    from app.dicom.sender import query_remote_node_sync

    # STUDY level: only a match key given — return fields still come back.
    study = query_remote_node_sync("PAXS_TEST_SCP", "STUDY", {"PatientID": "RET001"})
    assert study["ok"] is True and len(study["matches"]) == 1
    m = study["matches"][0]
    assert m["StudyInstanceUID"] == study_uid
    assert m["PatientName"] == "Return^Fields"
    assert m["PatientID"] == "RET001"

    # SERIES level: auto return fields include series context; the numeric
    # SeriesNumber return key must not have filtered anything out.
    series = query_remote_node_sync("PAXS_TEST_SCP", "SERIES", {"SeriesInstanceUID": "1.2.3.4.730"})
    assert series["ok"] is True and len(series["matches"]) == 1, series
    sm = series["matches"][0]
    assert sm["SeriesInstanceUID"] == "1.2.3.4.730"
    assert sm["Modality"] == "CT"
    # Patient/study context rides along at series level too.
    assert sm["PatientID"] == "RET001"
    assert sm["StudyInstanceUID"] == study_uid

    # INSTANCE level: SOP context + ancestors.
    inst = query_remote_node_sync("PAXS_TEST_SCP", "INSTANCE", {"SOPInstanceUID": "1.2.3.5.730.1"})
    assert inst["ok"] is True and len(inst["matches"]) == 1
    im = inst["matches"][0]
    assert im["SOPInstanceUID"] == "1.2.3.5.730.1"
    assert im["SeriesInstanceUID"] == "1.2.3.4.730"
    assert im["StudyInstanceUID"] == study_uid


def test_query_remote_node_wildcard(receiver):
    """Wildcard matching flows through the SCU -> SCP round trip (SMITH*)."""
    src_port, _ = receiver
    study_uid = "1.2.840.113619.2.408.701"
    ds = make_instance(study_uid=study_uid, patient_name="Smith^John",
                       patient_id="WILD001")
    assert _store_via_scu("127.0.0.1", src_port, [ds_bytes(ds)]) == [0x0000]
    _wait_stored(study_uid)

    with SyncSessionLocal() as s:
        s.add(DicomNode(ae_title="PAXS_TEST_SCP", host="127.0.0.1", port=src_port, enabled=True))
        s.commit()

    from app.dicom.sender import query_remote_node_sync

    hit = query_remote_node_sync("PAXS_TEST_SCP", level="STUDY",
                                 filters={"PatientName": "SMITH*"})
    assert hit["ok"] is True and len(hit["matches"]) == 1
    miss = query_remote_node_sync("PAXS_TEST_SCP", level="STUDY",
                                  filters={"PatientName": "JONES*"})
    assert miss["ok"] is True and miss["matches"] == []


def test_query_remote_node_all_levels(receiver):
    """PATIENT / STUDY / SERIES / INSTANCE queries all resolve against the SCP."""
    src_port, _ = receiver
    study_uid = "1.2.840.113619.2.408.702"
    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.702",
                       sop_uid="1.2.3.5.702.1", patient_id="LEV002")
    assert _store_via_scu("127.0.0.1", src_port, [ds_bytes(ds)]) == [0x0000]
    _wait_stored(study_uid)

    with SyncSessionLocal() as s:
        s.add(DicomNode(ae_title="PAXS_TEST_SCP", host="127.0.0.1", port=src_port, enabled=True))
        s.commit()

    from app.dicom.sender import query_remote_node_sync

    patient = query_remote_node_sync("PAXS_TEST_SCP", "PATIENT", {"PatientID": "LEV002"})
    assert patient["ok"] is True and len(patient["matches"]) == 1
    assert patient["matches"][0]["PatientID"] == "LEV002"

    study = query_remote_node_sync("PAXS_TEST_SCP", "STUDY", {"StudyInstanceUID": study_uid})
    assert study["ok"] is True and len(study["matches"]) == 1

    series = query_remote_node_sync("PAXS_TEST_SCP", "SERIES", {"SeriesInstanceUID": "1.2.3.4.702"})
    assert series["ok"] is True and len(series["matches"]) == 1
    assert series["matches"][0]["SeriesInstanceUID"] == "1.2.3.4.702"

    inst = query_remote_node_sync("PAXS_TEST_SCP", "INSTANCE", {"SOPInstanceUID": "1.2.3.5.702.1"})
    assert inst["ok"] is True and len(inst["matches"]) == 1
    assert inst["matches"][0]["SOPInstanceUID"] == "1.2.3.5.702.1"


def test_study_find_returns_patient_string_not_fk(receiver):
    """Regression: requesting PatientID as a *return* field (empty value) at
    STUDY level must come back as the string MRN, not the integer patient FK
    (previously overwrote PatientID with the FK -> DICOM encode failure 0xC312)."""
    src_port, _ = receiver
    study_uid = "1.2.840.113619.2.408.720"
    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.720",
                       sop_uid="1.2.3.5.720.1", patient_id="FKBUG001",
                       patient_name="Fk^Bug")
    assert _store_via_scu("127.0.0.1", src_port, [ds_bytes(ds)]) == [0x0000]
    _wait_stored(study_uid)

    with SyncSessionLocal() as s:
        s.add(DicomNode(ae_title="PAXS_TEST_SCP", host="127.0.0.1", port=src_port, enabled=True))
        s.commit()

    from app.dicom.sender import query_remote_node_sync

    result = query_remote_node_sync(
        "PAXS_TEST_SCP", level="STUDY",
        filters={"StudyInstanceUID": study_uid, "PatientID": "", "PatientName": ""},
    )
    assert result["ok"] is True, result
    assert len(result["matches"]) == 1
    m = result["matches"][0]
    assert m["PatientID"] == "FKBUG001"  # string MRN — not the integer FK
    assert m["PatientName"] == "Fk^Bug"


def test_query_remote_node_unknown_and_disabled():
    from app.dicom.sender import query_remote_node_sync

    assert query_remote_node_sync("NO_SUCH")["reason"] == "Node not found"
    with SyncSessionLocal() as s:
        s.add(DicomNode(ae_title="DISABLED", host="127.0.0.1", port=1, enabled=False))
        s.commit()
    assert query_remote_node_sync("DISABLED")["reason"] == "Node is disabled"


# --------------------------------------------------------------------------- #
# REST trigger: POST /api/v1/nodes/{ae}/find
# --------------------------------------------------------------------------- #
def test_rest_find_queries_node(client, receiver):
    src_port, _ = receiver
    study_uid = "1.2.840.113619.2.408.710"
    ds = make_instance(study_uid=study_uid, series_uid="1.2.3.4.710",
                       sop_uid="1.2.3.5.710.1", patient_id="RESTFIND1",
                       patient_name="Rest^Finder", accession="ACC-FIND-1")
    assert _store_via_scu("127.0.0.1", src_port, [ds_bytes(ds)]) == [0x0000]
    _wait_stored(study_uid)

    admin_h = _auth(client, "find.boss")
    _register_node(client, admin_h, "PAXS_TEST_SCP", "127.0.0.1", src_port)

    r = client.post("/api/v1/nodes/PAXS_TEST_SCP/find", headers=admin_h, json={
        "level": "STUDY",
        "filters": {"PatientID": "RESTFIND1", "StudyInstanceUID": "", "AccessionNumber": ""},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["ae_title"] == "PAXS_TEST_SCP" and body["level"] == "STUDY"
    assert len(body["matches"]) == 1
    m = body["matches"][0]
    assert m["StudyInstanceUID"] == study_uid
    assert m["AccessionNumber"] == "ACC-FIND-1"


def test_rest_find_empty_results(client, receiver):
    src_port, _ = receiver
    admin_h = _auth(client, "find.empty")
    _register_node(client, admin_h, "PAXS_TEST_SCP", "127.0.0.1", src_port)

    r = client.post("/api/v1/nodes/PAXS_TEST_SCP/find", headers=admin_h, json={
        "level": "STUDY", "filters": {"PatientID": "NOBODY"},
    })
    assert r.status_code == 200, r.text
    assert r.json()["matches"] == []


def test_rest_find_validation(client):
    admin_h = _auth(client, "find.val")
    _register_node(client, admin_h, "PAXS_TEST_SCP", "127.0.0.1", 1)

    # Unknown node -> 404
    r = client.post("/api/v1/nodes/NO_SUCH/find", headers=admin_h, json={"level": "STUDY"})
    assert r.status_code == 404
    # Disabled node -> 409
    _register_node(client, admin_h, "DISABLED", "127.0.0.1", 1, enabled=False)
    r = client.post("/api/v1/nodes/DISABLED/find", headers=admin_h, json={"level": "STUDY"})
    assert r.status_code == 409
    # Bad level -> 422 (pydantic pattern)
    r = client.post("/api/v1/nodes/PAXS_TEST_SCP/find", headers=admin_h, json={"level": "BOGUS"})
    assert r.status_code == 422
    # Unreachable node -> 502 (remote C-FIND failed)
    _register_node(client, admin_h, "GHOST", "127.0.0.1", 1)
    r = client.post("/api/v1/nodes/GHOST/find", headers=admin_h, json={"level": "STUDY"})
    assert r.status_code == 502
    # No auth -> 401
    r = client.post("/api/v1/nodes/PAXS_TEST_SCP/find", json={"level": "STUDY"})
    assert r.status_code == 401


def test_rest_find_requires_destructive_role(client):
    client.post("/api/v1/auth/register", json={"username": "chief.f", "password": "secret123"})
    admin_h = _auth(client, "chief.f")
    _register_node(client, admin_h, "PAXS_TEST_SCP", "127.0.0.1", 1)
    phys_h = _auth(client, "dr.physfind", role="PHYSICIAN")
    r = client.post("/api/v1/nodes/PAXS_TEST_SCP/find", headers=phys_h, json={"level": "STUDY"})
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Prefetch reuses send_find
# --------------------------------------------------------------------------- #
def test_prefetch_query_remote_priors_reuses_send_find():
    """query_remote_priors_sync still returns priors via the shared SCU."""
    port = _free_port()
    dest_ae, _ = _recording_scp(port)
    try:
        from app.services.prefetch_service import query_remote_priors_sync

        priors = query_remote_priors_sync("127.0.0.1", port, "PAXS_DEST", "MRN999")
        assert len(priors) == 1
        assert priors[0]["study_instance_uid"] == "1.2.840.113619.2.408.77.1"

        # Unreachable -> empty list, no exception.
        assert query_remote_priors_sync("127.0.0.1", 1, "GHOST", "X") == []
    finally:
        dest_ae.shutdown()
