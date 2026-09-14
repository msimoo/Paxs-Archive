"""FHIR R4 gateway tests."""

from tests.test_dicomweb import _upload_study


def test_fhir_metadata(client):
    res = client.get("/fhir/metadata")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/fhir+json"
    body = res.json()
    assert body["resourceType"] == "CapabilityStatement"
    types = {r["type"] for r in body["rest"][0]["resource"]}
    assert {"Patient", "ImagingStudy"} <= types


def test_fhir_patient_search_and_read(client):
    study_uid, _ = _upload_study(client, n=1, patient_id="P-FHIR")
    bundle = client.get("/fhir/Patient?identifier=P-FHIR").json()
    assert bundle["resourceType"] == "Bundle"
    assert bundle["total"] >= 1
    patient = bundle["entry"][0]["resource"]
    assert patient["resourceType"] == "Patient"
    assert patient["identifier"][0]["value"] == "P-FHIR"
    # DICOM name "Doe^John" -> family/given
    assert patient["name"][0]["family"] == "Doe"
    assert patient["name"][0]["given"] == ["John"]

    pid = patient["id"]
    single = client.get(f"/fhir/Patient/{pid}").json()
    assert single["id"] == pid


def test_fhir_patient_by_name(client):
    _upload_study(client, n=1)
    bundle = client.get("/fhir/Patient?name=Doe").json()
    assert bundle["total"] >= 1


def test_fhir_total_respects_search_filters(client):
    # Two distinct patients; the filtered total must match the filtered rows.
    _upload_study(client, n=1, patient_id="P-ONE")
    _upload_study(client, n=1, patient_id="P-TWO")

    all_bundle = client.get("/fhir/Patient").json()
    assert all_bundle["total"] == 2
    assert len(all_bundle["entry"]) == 2

    one = client.get("/fhir/Patient?identifier=P-ONE").json()
    assert one["total"] == 1
    assert len(one["entry"]) == 1

    none = client.get("/fhir/Patient?name=NoSuchName").json()
    assert none["total"] == 0
    assert none["entry"] == []


def test_fhir_imagingstudy_read(client):
    study_uid, series_uid = _upload_study(client, n=2, patient_id="P-IMG")
    res = client.get(f"/fhir/ImagingStudy/{study_uid}")
    assert res.status_code == 200
    body = res.json()
    assert body["resourceType"] == "ImagingStudy"
    assert body["status"] == "available"
    assert body["numberOfSeries"] == 1
    assert body["numberOfInstances"] == 2
    assert body["subject"]["reference"].startswith("Patient/")
    assert body["series"][0]["uid"] == series_uid
    assert body["series"][0]["numberOfInstances"] == 2


def test_fhir_imagingstudy_search_by_patient(client):
    _upload_study(client, n=1, patient_id="P-A")
    _upload_study(client, n=1, patient_id="P-B")

    bundle = client.get("/fhir/ImagingStudy?patient=P-A").json()
    assert bundle["total"] >= 1
    for entry in bundle["entry"]:
        subj = entry["resource"]["subject"]["reference"]
        patient_id = client.get(f"/fhir/{subj}").json()["identifier"][0]["value"]
        assert patient_id == "P-A"


def test_fhir_missing_patient_404(client):
    assert client.get("/fhir/Patient/999999").status_code == 404
    assert client.get("/fhir/ImagingStudy/1.2.3.404").status_code == 404


def test_fhir_invalid_patient_id_400(client):
    assert client.get("/fhir/Patient/abc").status_code == 400
