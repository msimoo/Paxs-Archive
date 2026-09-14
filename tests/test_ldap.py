"""Tests for LDAP / Active Directory Integration feature."""

from __future__ import annotations

import pytest


def test_ldap_login_api(client):
    """Test LDAP authentication and user provisioning REST API."""
    # 1. Invalid LDAP credentials
    resp_fail = client.post(
        "/api/v1/ldap/login",
        json={"username": "ldap_invalid", "password": "wrongpassword"},
    )
    assert resp_fail.status_code == 401

    # 2. Valid LDAP domain credentials
    resp_success = client.post(
        "/api/v1/ldap/login",
        json={"username": "ldap_rad_john", "password": "domainpass"},
    )
    assert resp_success.status_code == 200
    token_data = resp_success.json()
    assert "access_token" in token_data
    assert token_data["username"] == "ldap_rad_john"
    assert token_data["role"] == "radiologist"
    assert token_data["auth_provider"] == "LDAP/ActiveDirectory"
