import base64

from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers


def _setup(client):
    ds = make_instance(patient_id="P300", study_uid="1.2.3.300")
    res = client.post(
        "/dicom-web/studies",
        content=build_stow_body([ds_bytes(ds)]),
        headers=stow_headers(),
    )
    assert res.status_code == 200

    token = client.post("/api/v1/auth/register", json={
        "username": "sharer", "password": "secret123",
    }).json()["access_token"]
    return str(ds.StudyInstanceUID), token


def test_create_and_read_share(client):
    study_uid, token = _setup(client)

    created = client.post(
        "/api/v1/share",
        json={"study_uid": study_uid, "expiry_days": 7},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["study_uid"] == study_uid
    assert data["expires_at"]
    assert data["qr_base64"]
    img = base64.b64decode(data["qr_base64"])
    assert img[:8] == b"\x89PNG\r\n\x1a\n"
    assert data["url"].startswith("http://paxs.test/viewer?share=")

    # public read (no auth) works
    info = client.get(f"/api/v1/share/{data['token']}")
    assert info.status_code == 200
    body = info.json()
    assert body["study_uid"] == study_uid
    assert body["patient"]["name"] == "Doe^John"
    assert body["series"][0]["modality"] == "CT"
    assert body["accessed_count"] == 1

    # qr endpoint
    qr = client.get(f"/api/v1/share/{data['token']}/qr.png")
    assert qr.status_code == 200
    assert qr.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_share_requires_auth_and_valid_study(client):
    anon = client.post("/api/v1/share", json={"study_uid": "1.2.3.999"})
    assert anon.status_code == 401

    _setup(client)
    token = client.post("/api/v1/auth/login", json={"username": "sharer", "password": "secret123"}).json()["access_token"]
    missing = client.post("/api/v1/share", json={"study_uid": "1.2.3.999"},
                          headers={"Authorization": f"Bearer {token}"})
    assert missing.status_code == 404


def test_share_delete(client):
    study_uid, token = _setup(client)
    created = client.post("/api/v1/share", json={"study_uid": study_uid},
                          headers={"Authorization": f"Bearer {token}"}).json()

    res = client.delete(f"/api/v1/share/{created['token']}",
                        headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 204

    gone = client.get(f"/api/v1/share/{created['token']}")
    assert gone.status_code == 404


def test_physician_cannot_delete_or_destroy_shares(client):
    # First user becomes ADMIN; second becomes PHYSICIAN.
    client.post("/api/v1/auth/register", json={"username": "admin1", "password": "secret123"})
    client.post("/api/v1/auth/register", json={"username": "phys1", "password": "secret123"})
    phys_token = client.post("/api/v1/auth/login",
                             json={"username": "phys1", "password": "secret123"}).json()["access_token"]

    study_uid, token = _setup(client)  # reuses "sharer" (ADMIN)
    share = client.post("/api/v1/share", json={"study_uid": study_uid},
                        headers={"Authorization": f"Bearer {token}"}).json()

    denied_study = client.delete(f"/api/v1/studies/{study_uid}",
                                 headers={"Authorization": f"Bearer {phys_token}"})
    assert denied_study.status_code == 403

    denied_share = client.delete(f"/api/v1/share/{share['token']}",
                                 headers={"Authorization": f"Bearer {phys_token}"})
    assert denied_share.status_code == 403

    # The study still exists.
    assert client.get(f"/api/v1/studies/{study_uid}").status_code == 200
