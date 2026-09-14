"""Tests for Hanging Protocols feature."""

from __future__ import annotations

import pytest


def _get_auth_headers(client, username="admin_hp"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "adminpassword", "full_name": "Admin HP"},
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


def test_hanging_protocols_crud_and_match(client):
    """Test Hanging Protocols REST API CRUD and matching engine."""
    headers = _get_auth_headers(client, "admin_hp_api")

    # 1. POST /api/v1/hanging-protocols (Create)
    resp = client.post(
        "/api/v1/hanging-protocols",
        json={
            "name": "Chest CT 2x2 Layout",
            "description": "Standard 2x2 viewport layout for Chest CT exams",
            "modality": "CT",
            "body_part_examined": "CHEST",
            "priority": 5,
            "layout_rows": 2,
            "layout_columns": 2,
            "configuration_json": '{"presets": ["lung", "mediastinum"]}',
        },
        headers=headers,
    )
    assert resp.status_code == 201
    hp_data = resp.json()
    hp_id = hp_data["id"]
    assert hp_data["modality"] == "CT"

    # 2. GET /api/v1/hanging-protocols (List)
    resp_list = client.get("/api/v1/hanging-protocols", headers=headers)
    assert resp_list.status_code == 200
    assert len(resp_list.json()) >= 1

    # 3. GET /api/v1/hanging-protocols/match (Match)
    resp_match = client.get("/api/v1/hanging-protocols/match?modality=CT&body_part=CHEST", headers=headers)
    assert resp_match.status_code == 200
    assert resp_match.json()["id"] == hp_id

    # 4. GET /api/v1/hanging-protocols/{id} (Get)
    resp_get = client.get(f"/api/v1/hanging-protocols/{hp_id}", headers=headers)
    assert resp_get.status_code == 200
    assert resp_get.json()["name"] == "Chest CT 2x2 Layout"

    # 5. DELETE /api/v1/hanging-protocols/{id} (Delete)
    resp_del = client.delete(f"/api/v1/hanging-protocols/{hp_id}", headers=headers)
    assert resp_del.status_code == 200
