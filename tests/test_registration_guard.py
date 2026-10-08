"""H02 — self-registration cannot mint privileged accounts.

Registration is OFF by default; the bootstrap account (the very first user) is
always allowed so a fresh install can be set up; when registration is enabled it
may only provision least-privilege roles.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.roles import DESTRUCTIVE_ROLES

REGISTER = "/api/v1/auth/register"


def _register(client, username: str, role: str | None = None, password: str = "secret123"):
    body: dict = {"username": username, "password": password}
    if role is not None:
        body["role"] = role
    return client.post(REGISTER, json=body)


# --------------------------------------------------------------------------- #
# the bootstrap account must keep working
# --------------------------------------------------------------------------- #
def test_first_user_still_bootstraps_as_admin(client, monkeypatch):
    """A fresh install must remain possible with registration disabled."""
    monkeypatch.setattr(settings, "ALLOW_REGISTER", False, raising=False)
    resp = _register(client, "bootstrap_admin")
    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["role"] == "ADMIN"


def test_bootstrap_cannot_be_claimed_twice(client, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_REGISTER", False, raising=False)
    assert _register(client, "first_admin").status_code == 201
    # The second attempt is a normal registration, which is disabled.
    assert _register(client, "second_admin").status_code == 403


# --------------------------------------------------------------------------- #
# default posture
# --------------------------------------------------------------------------- #
def test_registration_is_disabled_by_default(client, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_REGISTER", False, raising=False)
    _register(client, "existing_user")           # bootstrap, becomes ADMIN
    resp = _register(client, "another_user")
    assert resp.status_code == 403, resp.text
    assert "disabled" in resp.text.lower()


def test_shipped_default_is_off():
    """The code default must stay off (a test-env override enables it)."""
    import inspect

    from app.core import config as config_module

    source = inspect.getsource(config_module)
    assert "ALLOW_REGISTER: bool = False" in source


# --------------------------------------------------------------------------- #
# least-privilege provisioning when registration IS enabled
# --------------------------------------------------------------------------- #
@pytest.fixture()
def registration_on(client, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_REGISTER", True, raising=False)
    _register(client, "reg_bootstrap")           # first user = ADMIN
    return client


@pytest.mark.parametrize("role", sorted(DESTRUCTIVE_ROLES))
def test_self_registration_cannot_mint_destructive_roles(registration_on, role):
    resp = _register(registration_on, f"wants_{role.lower()}", role=role)
    assert resp.status_code == 403, resp.text
    assert "administrator" in resp.text.lower()


def test_self_registration_defaults_to_least_privilege(registration_on):
    resp = _register(registration_on, "no_role_requested")
    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["role"] == "PHYSICIAN"


def test_self_registration_honours_an_allowed_role(registration_on):
    resp = _register(registration_on, "wants_physician", role="PHYSICIAN")
    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["role"] == "PHYSICIAN"


def test_self_registration_rejects_an_unknown_role(registration_on):
    resp = _register(registration_on, "wants_superuser", role="SUPERUSER")
    assert resp.status_code == 422, resp.text


def test_allowed_roles_setting_is_honoured(registration_on, monkeypatch):
    """An operator may widen the allowed set, but never to a destructive role."""
    monkeypatch.setattr(
        settings, "REGISTER_ALLOWED_ROLES", "PHYSICIAN,TECHNOLOGIST,RADIOLOGIST",
        raising=False,
    )
    # TECHNOLOGIST was added by the operator -> allowed.
    assert _register(registration_on, "reg_tech", role="TECHNOLOGIST").status_code == 201
    # RADIOLOGIST was requested but is destructive -> still refused.
    assert _register(registration_on, "reg_rad", role="RADIOLOGIST").status_code == 403


def test_registered_account_cannot_delete_studies(client, monkeypatch, auth_client):
    """End to end: a self-registered account has no destructive capability."""
    monkeypatch.setattr(settings, "ALLOW_REGISTER", True, raising=False)
    resp = _register(client, "reg_plain")
    assert resp.status_code == 201
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    # Destructive role required -> refused for a PHYSICIAN.
    assert client.delete("/api/v1/studies/1.2.3.4", headers=headers).status_code == 403
