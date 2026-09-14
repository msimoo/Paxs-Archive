"""Tests for Peer Review / QA (RADPEER) feature."""

from __future__ import annotations

import pytest
from pydicom.uid import generate_uid


def _get_auth_headers(client, username="admin_pr"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin PR"},
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


def test_peer_review_full_workflow(client):
    """Test the complete peer review lifecycle: create → assign → score → list."""
    headers = _get_auth_headers(client, "pr_admin_1")
    study_uid = generate_uid()

    # 1. Create a peer review case
    resp = client.post(
        "/api/v1/peer-review",
        json={
            "study_instance_uid": study_uid,
            "original_reader": "dr.smith",
            "patient_id": "PR_PAT_001",
            "modality": "CT",
            "body_part_examined": "CHEST",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    case = resp.json()
    case_id = case["id"]
    assert case["status"] == "PENDING"
    assert case["original_reader"] == "dr.smith"
    assert case["score"] is None

    # 2. Assign a reviewer (must differ from original reader)
    resp_assign = client.post(
        f"/api/v1/peer-review/{case_id}/assign",
        json={"reviewer": "dr.jones"},
        headers=headers,
    )
    assert resp_assign.status_code == 200
    assert resp_assign.json()["status"] == "ASSIGNED"
    assert resp_assign.json()["reviewer"] == "dr.jones"

    # 3. Assigning the original reader as reviewer must fail
    resp_self = client.post(
        f"/api/v1/peer-review/{case_id}/assign",
        json={"reviewer": "dr.smith"},
        headers=headers,
    )
    assert resp_self.status_code == 400

    # 4. Submit a RADPEER score
    resp_score = client.post(
        f"/api/v1/peer-review/{case_id}/score",
        json={"score": 1, "comments": "Concur with interpretation, no discrepancy."},
        headers=headers,
    )
    assert resp_score.status_code == 200
    scored = resp_score.json()
    assert scored["status"] == "COMPLETED"
    assert scored["score"] == 1
    assert scored["completed_at"] is not None

    # 5. List cases
    resp_list = client.get("/api/v1/peer-review", headers=headers)
    assert resp_list.status_code == 200
    assert any(c["id"] == case_id for c in resp_list.json())

    # 6. Filter by status
    resp_completed = client.get("/api/v1/peer-review?status=COMPLETED", headers=headers)
    assert resp_completed.status_code == 200
    assert all(c["status"] == "COMPLETED" for c in resp_completed.json())

    # 7. Get single case
    resp_get = client.get(f"/api/v1/peer-review/{case_id}", headers=headers)
    assert resp_get.status_code == 200
    assert resp_get.json()["score"] == 1

    # 8. Score summary
    resp_summary = client.get("/api/v1/peer-review/summary", headers=headers)
    assert resp_summary.status_code == 200
    summary = resp_summary.json()
    assert summary["total_completed"] >= 1
    assert summary["avg_score"] >= 1.0


def test_peer_review_invalid_score(client):
    """Verify that invalid RADPEER scores (outside 1-4) are rejected."""
    headers = _get_auth_headers(client, "pr_admin_2")
    study_uid = generate_uid()

    # Create case
    resp = client.post(
        "/api/v1/peer-review",
        json={
            "study_instance_uid": study_uid,
            "original_reader": "dr.alpha",
            "modality": "MR",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    case_id = resp.json()["id"]

    # Score 0 — invalid (pydantic ge=1 validation)
    resp_bad = client.post(
        f"/api/v1/peer-review/{case_id}/score",
        json={"score": 0},
        headers=headers,
    )
    assert resp_bad.status_code == 422

    # Score 5 — invalid (pydantic le=4 validation)
    resp_bad2 = client.post(
        f"/api/v1/peer-review/{case_id}/score",
        json={"score": 5},
        headers=headers,
    )
    assert resp_bad2.status_code == 422
