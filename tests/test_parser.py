from tests.helpers import make_instance
from app.dicom.parser import parse_metadata


def test_parse_full_instance():
    ds = make_instance(patient_id="P007", patient_name="Smith^John", modality="CT")
    meta = parse_metadata(ds)
    assert meta["patient_id"] == "P007"
    assert meta["patient_name"] == "Smith^John"
    assert meta["modality"] == "CT"
    assert meta["study_date"] == "20250115"
    assert meta["sop_instance_uid"] == str(ds.SOPInstanceUID)
    assert meta["transfer_syntax_uid"] == "1.2.840.10008.1.2.1"
    assert meta["rows"] == "64"
    assert meta["columns"] == "64"
    # Only the curated schema fields are extracted — no tag soup.
    assert "window_center" not in meta


def test_parse_missing_tags_does_not_crash():
    ds = make_instance()
    del ds.PatientBirthDate
    del ds.BodyPartExamined
    meta = parse_metadata(ds)
    assert meta["patient_birth_date"] is None
    assert meta["body_part_examined"] is None
    assert meta["sop_instance_uid"]  # still parsed
