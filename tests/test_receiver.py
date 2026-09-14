"""Integration tests: push real DICOM over DIMSE with pynetdicom and verify
the full pipeline (file on disk, DB hierarchy, audit trail, compression job).
"""

import io
import time

import pydicom
from pynetdicom import AE
from pynetdicom.sop_class import CTImageStorage
from pydicom.uid import ExplicitVRLittleEndian
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AuditLog, CompressionJob, Instance, Patient, Series, Study
from app.db.session import SyncSessionLocal
from tests.helpers import ds_bytes, make_instance


def _store_via_scu(host, port, datasets: list[bytes], ae_title="PAXS_TEST_SCP") -> list[int]:
    ae = AE(ae_title="PAXS_TEST_SCU")
    ae.add_requested_context(CTImageStorage, ExplicitVRLittleEndian)
    assoc = ae.associate(host, port, ae_title=ae_title)
    assert assoc.is_established, "association failed"
    statuses = []
    for i, data in enumerate(datasets):
        status = assoc.send_c_store(pydicom.dcmread(io.BytesIO(data)), msg_id=i + 1)
        statuses.append(int(status.Status) if status else -1)
    assoc.release()
    return statuses


def test_c_store_pipeline(receiver):
    port, _ = receiver
    settings.COMPRESSION_ENABLED = True  # enable job creation for this test
    try:
        study_uid = "1.2.840.113619.2.408.1"
        ds1 = make_instance(study_uid=study_uid, series_uid="1.2.3.4.1",
                            sop_uid="1.2.3.5.1", instance_num=1)
        ds2 = make_instance(study_uid=study_uid, series_uid="1.2.3.4.1",
                            sop_uid="1.2.3.5.2", instance_num=2)
        statuses = _store_via_scu("127.0.0.1", port, [ds_bytes(ds1), ds_bytes(ds2)])
        assert statuses == [0x0000, 0x0000], statuses

        # DB hierarchy + audit + compression job
        for _ in range(50):
            with SyncSessionLocal() as s:
                n_inst = s.execute(select(Instance)).scalars().all()
                if len(n_inst) == 2:
                    break
            time.sleep(0.1)
        with SyncSessionLocal() as s:
            assert s.execute(select(Patient).where(Patient.patient_id == "P0001")).scalar_one()
            study = s.execute(select(Study).where(Study.study_instance_uid == study_uid)).scalar_one()
            series = s.execute(select(Series).where(Series.study_id == study.id)).scalar_one()
            instances = s.execute(select(Instance).where(Instance.series_id == series.id)).scalars().all()
            assert len(instances) == 2
            assert all(i.file_size_bytes > 0 for i in instances)
            audits = s.execute(select(AuditLog).where(AuditLog.action == "STORED")).scalars().all()
            assert len(audits) == 2
            jobs = s.execute(select(CompressionJob)).scalars().all()
            assert len(jobs) == 2
            assert all(j.status in ("PENDING", "RUNNING", "DONE") for j in jobs)
    finally:
        settings.COMPRESSION_ENABLED = False


def test_c_echo(receiver):
    port, _ = receiver
    ae = AE(ae_title="PAXS_TEST_SCU")
    ae.add_requested_context("1.2.840.10008.1.1")  # Verification SOP Class
    assoc = ae.associate("127.0.0.1", port, ae_title="PAXS_TEST_SCP")
    assert assoc.is_established
    status = assoc.send_c_echo()
    assoc.release()
    assert int(status.Status) == 0x0000


def test_c_find_study_level(receiver):
    port, _ = receiver
    settings.COMPRESSION_ENABLED = False
    ds = make_instance(study_uid="1.2.840.113619.2.408.9", study_date="20250301")
    statuses = _store_via_scu("127.0.0.1", port, [ds_bytes(ds)])
    assert statuses == [0x0000]

    for _ in range(50):
        with SyncSessionLocal() as s:
            if s.execute(select(Study)).scalars().all():
                break
        time.sleep(0.1)

    # Query for the stored study
    from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind
    from pydicom.dataset import Dataset

    ae = AE(ae_title="PAXS_QUERY_SCU")
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind, ExplicitVRLittleEndian)
    assoc = ae.associate("127.0.0.1", port, ae_title="PAXS_TEST_SCP")
    assert assoc.is_established

    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    query.StudyDate = "20250301"
    query.StudyInstanceUID = ""
    query.PatientName = ""

    found = []
    for status, identifier in assoc.send_c_find(
        query, query_model=StudyRootQueryRetrieveInformationModelFind
    ):
        if status and status.Status == 0xFF00 and identifier is not None:
            found.append(identifier)
    assoc.release()

    assert len(found) == 1
    assert found[0].StudyInstanceUID == "1.2.840.113619.2.408.9"
    assert "PatientName" in found[0]  # requested key returned
