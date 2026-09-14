"""Tests for PAXS Phase 3 Hardware-Bound Licensing & Cryptographic Protection."""

import base64
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.licensing.crypto import (
    LicenseData,
    decrypt_license_container,
    encrypt_license_container,
    generate_rsa_key_pair,
    sign_license,
    verify_license_signature,
)
from app.core.licensing.deps import require_license_feature, require_valid_license
from app.core.licensing.fingerprint import get_hardware_info, get_machine_fingerprint
from app.core.licensing.manager import LicenseManager, LicenseStatus
from app.main import app


@pytest.fixture
def rsa_keys():
    priv, pub = generate_rsa_key_pair()
    return priv, pub


def test_machine_fingerprint_format_and_stability():
    """Hardware fingerprint must be 32 uppercase hex characters and stable."""
    fp1 = get_machine_fingerprint()
    fp2 = get_machine_fingerprint()
    assert fp1 == fp2
    assert len(fp1) == 32
    assert fp1.isalnum()
    assert fp1 == fp1.upper()

    info = get_hardware_info()
    assert "os" in info
    assert "fingerprint" in info
    assert info["fingerprint"] == fp1


def test_license_crypto_signing_and_tampering(rsa_keys):
    """Verifies RSA-2048-PSS signature and detects payload tampering."""
    priv_pem, pub_pem = rsa_keys
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    pub_key = serialization.load_pem_public_key(pub_pem, backend=default_backend())

    now = datetime.now(timezone.utc)
    lic = LicenseData(
        license_id="PAXS-TEST-001",
        customer_name="Test General Hospital",
        edition="hospital",
        machine_fingerprint="*",
        issued_at=now,
        expires_at=now + timedelta(days=365),
        max_users=50,
        max_studies_per_year=50000,
        features=["recist", "lung_rads", "agatston"],
    )

    signed = sign_license(lic, priv_pem)
    assert signed.signature is not None

    # Valid signature
    assert verify_license_signature(signed, pub_key) is True

    # Tampered max_users
    tampered = signed.model_copy(update={"max_users": 999})
    assert verify_license_signature(tampered, pub_key) is False

    # Tampered features
    tampered_feat = signed.model_copy(update={"features": ["recist", "lung_rads", "agatston", "unlicensed_ai"]})
    assert verify_license_signature(tampered_feat, pub_key) is False


def test_license_container_encryption_and_decryption(rsa_keys):
    """Verifies AES-256-GCM container encrypt/decrypt cycle."""
    priv_pem, _ = rsa_keys
    lic = LicenseData(
        license_id="PAXS-CONTAINER-TEST",
        customer_name="St. Jude Clinic",
        edition="clinic",
        machine_fingerprint=get_machine_fingerprint(),
    )
    signed = sign_license(lic, priv_pem)

    container = encrypt_license_container(signed)
    assert container.startswith(b"PAXS")

    decrypted = decrypt_license_container(container)
    assert decrypted.license_id == "PAXS-CONTAINER-TEST"
    assert decrypted.customer_name == "St. Jude Clinic"
    assert decrypted.machine_fingerprint == get_machine_fingerprint()

    # Corrupted ciphertext
    bad_container = container[:20] + b"\xff\xff\xff\xff" + container[24:]
    with pytest.raises(ValueError):
        decrypt_license_container(bad_container)


def test_license_manager_lifecycle(rsa_keys, tmp_path):
    """Tests LicenseManager state transitions (VALID, HARDWARE_MISMATCH, EXPIRED, GRACE_PERIOD)."""
    priv_pem, pub_pem = rsa_keys
    host_fp = get_machine_fingerprint()

    # 1. Valid matching license
    lic_file = tmp_path / "valid.lic"
    lic = LicenseData(
        license_id="PAXS-VALID-01",
        customer_name="City Health",
        edition="enterprise",
        machine_fingerprint=host_fp,
        features=["recist", "mpr_3d"],
    )
    sign_license(lic, priv_pem)
    lic_file.write_bytes(encrypt_license_container(lic))

    os.environ["PAXS_LICENSE_PUBLIC_KEY"] = pub_pem.decode("ascii")
    manager = LicenseManager(lic_file)

    assert manager.status == LicenseStatus.VALID
    assert manager.is_active is True
    assert manager.has_feature("recist") is True
    assert manager.has_feature("mpr_3d") is True
    assert manager.has_feature("unlicensed") is False
    assert manager.check_user_seat(10) is True

    # 2. Hardware Mismatch
    mismatch_file = tmp_path / "mismatch.lic"
    lic_mismatch = LicenseData(
        license_id="PAXS-MISMATCH-01",
        customer_name="Other Machine",
        edition="hospital",
        machine_fingerprint="00000000000000000000000000000000",
    )
    sign_license(lic_mismatch, priv_pem)
    mismatch_file.write_bytes(encrypt_license_container(lic_mismatch))

    manager_mismatch = LicenseManager(mismatch_file)
    assert manager_mismatch.status == LicenseStatus.HARDWARE_MISMATCH
    assert manager_mismatch.is_active is False

    # 3. Grace Period (expired 2 days ago, within 7-day grace)
    grace_file = tmp_path / "grace.lic"
    lic_grace = LicenseData(
        license_id="PAXS-GRACE-01",
        customer_name="Grace Period Hospital",
        edition="clinic",
        machine_fingerprint="*",
        expires_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    sign_license(lic_grace, priv_pem)
    grace_file.write_bytes(encrypt_license_container(lic_grace))

    manager_grace = LicenseManager(grace_file)
    assert manager_grace.status == LicenseStatus.GRACE_PERIOD
    assert manager_grace.is_active is True

    # 4. Hard Expired (expired 30 days ago)
    exp_file = tmp_path / "expired.lic"
    lic_exp = LicenseData(
        license_id="PAXS-EXP-01",
        customer_name="Expired Hospital",
        edition="clinic",
        machine_fingerprint="*",
        expires_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    sign_license(lic_exp, priv_pem)
    exp_file.write_bytes(encrypt_license_container(lic_exp))

    manager_exp = LicenseManager(exp_file)
    assert manager_exp.status == LicenseStatus.EXPIRED
    assert manager_exp.is_active is False


def test_license_api_status_and_fingerprint(client):
    """Tests GET /api/v1/license/status and GET /api/v1/license/fingerprint."""
    # Register/login admin user
    reg = client.post("/api/v1/auth/register", json={
        "username": f"lic_admin_{os.urandom(3).hex()}",
        "password": "Password123!",
        "role": "ADMIN",
    })
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    res_fp = client.get("/api/v1/license/fingerprint", headers=headers)
    assert res_fp.status_code == 200
    data_fp = res_fp.json()
    assert "fingerprint" in data_fp
    assert "hardware_info" in data_fp
    assert len(data_fp["fingerprint"]) == 32

    res_st = client.get("/api/v1/license/status", headers=headers)
    assert res_st.status_code == 200
    data_st = res_st.json()
    assert "status" in data_st
    assert "is_active" in data_st
    assert "edition" in data_st
    assert "server_fingerprint" in data_st


def test_license_api_activation(client, rsa_keys, tmp_path):
    """Tests POST /api/v1/license/activate with valid base64 payload."""
    priv_pem, pub_pem = rsa_keys
    os.environ["PAXS_LICENSE_PUBLIC_KEY"] = pub_pem.decode("ascii")

    lic_path = Path("paxs.lic")
    orig_bytes = lic_path.read_bytes() if lic_path.exists() else None

    try:
        reg = client.post("/api/v1/auth/register", json={
            "username": f"lic_act_admin_{os.urandom(3).hex()}",
            "password": "Password123!",
            "role": "ADMIN",
        })
        token = reg.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        lic = LicenseData(
            license_id="PAXS-ACTIVATE-API-TEST",
            customer_name="API Activation Customer",
            edition="enterprise",
            machine_fingerprint="*",
            features=["recist", "pe_clot", "breast_density"],
        )
        sign_license(lic, priv_pem)
        container = encrypt_license_container(lic)
        b64_str = base64.b64encode(container).decode("ascii")

        res = client.post("/api/v1/license/activate", json={"license_b64": b64_str}, headers=headers)
        assert res.status_code == 200
        res_data = res.json()
        assert res_data["success"] is True
        assert res_data["summary"]["customer_name"] == "API Activation Customer"
        assert res_data["summary"]["edition"] == "ENTERPRISE"
        assert "pe_clot" in res_data["summary"]["features"]
    finally:
        if orig_bytes is not None:
            lic_path.write_bytes(orig_bytes)
        os.environ.pop("PAXS_LICENSE_PUBLIC_KEY", None)
