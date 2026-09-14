"""Tests for Cross-Enterprise Document Sharing (XDS-I.b) feature."""

from __future__ import annotations

import pytest
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, XDSDocument
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import xdsi_service


@pytest.mark.anyio
async def test_xdsi_service_register_and_query():
    """Test XDS-I.b document registration, ebXML metadata generation, and query."""
    study_uid = generate_uid()
    patient_id = "XDS_PAT_01^^^&1.2.3.4&ISO"

    # 1. Register document
    async with AsyncSessionLocal() as session:
        async with session.begin():
            doc = await xdsi_service.register_document(
                session,
                patient_id=patient_id,
                document_title="Chest CT Imaging Manifest",
                content_bytes=b"DUMMY DICOM KOS MANIFEST CONTENT",
                mime_type="application/dicom",
                study_instance_uid=study_uid,
                class_code="IMAGES",
                performed_by="test-radiologist",
            )
            assert doc.entry_uuid.startswith("urn:uuid:")
            assert doc.unique_id is not None
            assert doc.size_bytes > 0
            entry_uuid = doc.entry_uuid

    # 2. Query documents [ITI-18]
    async with AsyncSessionLocal() as session:
        docs = await xdsi_service.query_documents(session, patient_id=patient_id)
        assert len(docs) == 1
        assert docs[0].entry_uuid == entry_uuid
        assert docs[0].document_title == "Chest CT Imaging Manifest"

    # 3. Retrieve document [ITI-43]
    async with AsyncSessionLocal() as session:
        ret_doc, content = await xdsi_service.retrieve_document(session, entry_uuid)
        assert ret_doc.unique_id == docs[0].unique_id
        assert content == b"DUMMY DICOM KOS MANIFEST CONTENT"

    # 4. Check Audit Log
    with Session(sync_engine) as session:
        audits = session.execute(
            select(AuditLog).where(AuditLog.action == "XDS_DOCUMENT_REGISTERED")
        ).scalars().all()
        assert len(audits) >= 1


def _get_auth_headers(client, username="admin_xds"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin XDS"},
    )
    if reg.status_code == 201:
        token = reg.json()["access_token"]
    else:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "adminpassword"},
        )
        token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_xdsi_api(client):
    """Test XDS-I.b REST API endpoints."""
    headers = _get_auth_headers(client, "admin_xds_api")
    pid = "PAT_XDS_API_100"

    # 1. POST /api/v1/xdsi/register (Provide and Register Document Set-b)
    resp_reg = client.post(
        "/api/v1/xdsi/register",
        json={
            "patient_id": pid,
            "document_title": "Brain MRI Key Object Selection",
            "content_text": "XDS-I.b KOS MANIFEST PAYLOAD",
            "mime_type": "application/dicom",
            "class_code": "IMAGES",
        },
        headers=headers,
    )
    assert resp_reg.status_code == 201
    doc_data = resp_reg.json()
    entry_uuid = doc_data["entry_uuid"]
    assert doc_data["patient_id"] == pid

    # 2. GET /api/v1/xdsi/documents (FindDocuments Query)
    resp_q = client.get(f"/api/v1/xdsi/documents?patient_id={pid}", headers=headers)
    assert resp_q.status_code == 200
    q_docs = resp_q.json()
    assert len(q_docs) == 1
    assert q_docs[0]["entry_uuid"] == entry_uuid

    # 3. GET /api/v1/xdsi/documents/{entry_uuid} (RetrieveDocumentSet)
    resp_ret = client.get(f"/api/v1/xdsi/documents/{entry_uuid}", headers=headers)
    assert resp_ret.status_code == 200
    assert resp_ret.content == b"XDS-I.b KOS MANIFEST PAYLOAD"

    # 4. GET /api/v1/xdsi/documents/{entry_uuid}/ebxml (Get ebXML Metadata)
    resp_xml = client.get(f"/api/v1/xdsi/documents/{entry_uuid}/ebxml", headers=headers)
    assert resp_xml.status_code == 200
    assert "SubmitObjectsRequest" in resp_xml.text
    assert entry_uuid in resp_xml.text
