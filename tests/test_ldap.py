"""Tests for LDAP / Active Directory authentication (``plan_9_2026.md`` C02).

These replace the previous suite, which asserted that the removed development
backdoor worked (``ldap_rad_john`` / ``domainpass`` -> 200 with a lowercase
role). Directory access is now exercised through a **mocked ldap3 module**, so
the real bind path is covered without needing a directory server.
"""

from __future__ import annotations

import sys
import types

import pytest

from app.api.ratelimit import reset_rate_limits
from app.core.config import settings
from app.core.roles import ROLES, normalize_role
from app.services import ldap_service

LEGACY_BACKDOOR_USER = "ldap_rad_john"
LEGACY_BACKDOOR_PASSWORD = "domainpass"


# --------------------------------------------------------------------------- #
# fake ldap3
# --------------------------------------------------------------------------- #
class _FakeEntry:
    def __init__(self, member_of=None, display_name=None):
        self.memberOf = member_of
        self.displayName = display_name


class _FakeConnection:
    def __init__(self, server, user=None, password=None, **kwargs):
        self.server = server
        self.user = user
        self.password = password
        self.bound = False
        self.result = "invalidCredentials"
        self.entries = []

    def bind(self):
        # Read the configuration from the *installed module instance* so the
        # fixture can vary it per test.
        module = sys.modules.get("ldap3")
        ok = bool(getattr(module, "BIND_RESULT", True))
        groups = tuple(getattr(module, "GROUPS", ()))
        self.bound = ok
        if ok:
            self.result = "success"
            self.entries = [_FakeEntry(
                member_of=list(groups),
                display_name="Directory User",
            )]
        return self.bound

    def search(self, *args, **kwargs):
        return True


class _FakeLdap3(types.ModuleType):
    """Stand-in for the optional ldap3 package."""

    NONE = "NONE"
    BIND_RESULT = True
    GROUPS: tuple[str, ...] = ()

    def __init__(self):
        super().__init__("ldap3")
        self.Server = _FakeServer
        self.Connection = _FakeConnection


class _FakeServer:
    def __init__(self, host, **kwargs):
        self.host = host
        self.kwargs = kwargs


@pytest.fixture()
def fake_ldap(monkeypatch):
    """Install a fake ldap3 module; configure bind result and group membership."""

    def _install(bind_ok: bool = True, groups: tuple[str, ...] = ()) -> _FakeLdap3:
        module = _FakeLdap3()
        module.BIND_RESULT = bind_ok
        module.GROUPS = groups
        monkeypatch.setitem(sys.modules, "ldap3", module)
        return module

    return _install


@pytest.fixture()
def ldap_enabled(monkeypatch):
    """Enable LDAP with a directory + group->role map (no default role)."""
    monkeypatch.setattr(settings, "LDAP_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "LDAP_SERVER", "ldaps://dc01.hospital.org", raising=False)
    monkeypatch.setattr(settings, "LDAP_BASE_DN", "DC=hospital,DC=org", raising=False)
    monkeypatch.setattr(
        settings, "LDAP_USER_DN_TEMPLATE", "uid={username},{base_dn}", raising=False
    )
    monkeypatch.setattr(
        settings,
        "LDAP_GROUP_ROLE_MAP",
        "CN=Radiologists=ADMIN;CN=Doctors=PHYSICIAN",
        raising=False,
    )
    monkeypatch.setattr(settings, "LDAP_DEFAULT_ROLE", "", raising=False)
    return settings


def _login(client, username: str, password: str):
    return client.post("/api/v1/ldap/login", json={"username": username, "password": password})


# --------------------------------------------------------------------------- #
# the backdoor is gone
# --------------------------------------------------------------------------- #
def test_legacy_backdoor_credentials_are_rejected_while_disabled(client):
    """The removed fallback must never authenticate again."""
    resp = _login(client, LEGACY_BACKDOOR_USER, LEGACY_BACKDOOR_PASSWORD)
    assert resp.status_code == 503, resp.text
    assert "access_token" not in resp.text


def test_legacy_backdoor_credentials_are_rejected_with_directory_enabled(
    client, ldap_enabled, fake_ldap
):
    """Even with LDAP configured, the old credential pattern does nothing:
    the directory rejects the bind."""
    fake_ldap(bind_ok=False)
    resp = _login(client, LEGACY_BACKDOOR_USER, LEGACY_BACKDOOR_PASSWORD)
    assert resp.status_code == 401, resp.text
    assert "access_token" not in resp.text


def test_shared_local_password_is_not_provisioned(
    client, ldap_enabled, fake_ldap
):
    """A directory user must not be reachable through the local password login."""
    fake_ldap(bind_ok=True, groups=("CN=Radiologists,DC=hospital,DC=org",))
    assert _login(client, "dr.house", "any-directory-password").status_code == 200

    for guess in ("ldap_managed_password", "any-directory-password", "", "password"):
        local = client.post(
            "/api/v1/auth/login", json={"username": "dr.house", "password": guess}
        )
        assert local.status_code == 401, f"local login must fail for guess {guess!r}"


# --------------------------------------------------------------------------- #
# fail-closed gates
# --------------------------------------------------------------------------- #
def test_login_refused_while_disabled(client):
    resp = _login(client, "anyone", "secret")
    assert resp.status_code == 503


def test_login_refused_when_server_missing(client, monkeypatch):
    monkeypatch.setattr(settings, "LDAP_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "LDAP_SERVER", "", raising=False)
    monkeypatch.setattr(settings, "LDAP_BASE_DN", "DC=hospital,DC=org", raising=False)
    resp = _login(client, "anyone", "secret")
    assert resp.status_code == 503


def test_login_refused_when_base_dn_missing(client, monkeypatch):
    monkeypatch.setattr(settings, "LDAP_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "LDAP_SERVER", "ldap://dc01", raising=False)
    monkeypatch.setattr(settings, "LDAP_BASE_DN", "", raising=False)
    resp = _login(client, "anyone", "secret")
    assert resp.status_code == 503


def test_missing_ldap3_dependency_denies_login(client, ldap_enabled, monkeypatch):
    monkeypatch.setitem(sys.modules, "ldap3", None)  # `import ldap3` -> ImportError
    resp = _login(client, "dr.house", "secret")
    assert resp.status_code == 401
    assert "access_token" not in resp.text


def test_empty_credentials_are_rejected(client, ldap_enabled, fake_ldap):
    fake_ldap(bind_ok=True, groups=("CN=Radiologists,DC=hospital,DC=org",))
    assert _login(client, "", "").status_code == 401
    assert _login(client, "dr.house", "").status_code == 401


# --------------------------------------------------------------------------- #
# successful bind -> canonical role
# --------------------------------------------------------------------------- #
def test_successful_bind_issues_token_with_canonical_uppercase_role(
    client, ldap_enabled, fake_ldap
):
    fake_ldap(bind_ok=True, groups=("CN=Radiologists,DC=hospital,DC=org",))
    resp = _login(client, "dr.house", "correct-horse")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "ADMIN"
    assert body["role"] in ROLES
    assert body["auth_provider"] == "LDAP/ActiveDirectory"
    assert "access_token" in body

    # The token must be usable on a protected endpoint.
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["role"] == "ADMIN"


def test_role_mapping_is_case_insensitive_for_the_directory_group(
    client, ldap_enabled, fake_ldap
):
    fake_ldap(bind_ok=True, groups=("cn=radiologists,dc=hospital,dc=org",))
    resp = _login(client, "dr.smith", "pw")
    assert resp.status_code == 200
    assert resp.json()["role"] == "ADMIN"


def test_group_cn_only_also_matches(client, ldap_enabled, fake_ldap):
    fake_ldap(bind_ok=True, groups=("Doctors",))
    resp = _login(client, "dr.zhang", "pw")
    assert resp.status_code == 200
    assert resp.json()["role"] == "PHYSICIAN"


def test_unmapped_groups_are_denied(client, ldap_enabled, fake_ldap):
    fake_ldap(bind_ok=True, groups=("CN=Visitors,DC=hospital,DC=org",))
    resp = _login(client, "visitor", "pw")
    assert resp.status_code == 401, resp.text
    assert "access_token" not in resp.text


def test_default_role_applies_when_configured(client, ldap_enabled, fake_ldap, monkeypatch):
    monkeypatch.setattr(settings, "LDAP_DEFAULT_ROLE", "TECHNOLOGIST", raising=False)
    fake_ldap(bind_ok=True, groups=("CN=Visitors,DC=hospital,DC=org",))
    resp = _login(client, "visitor", "pw")
    assert resp.status_code == 200
    assert resp.json()["role"] == "TECHNOLOGIST"


def test_invalid_default_role_denies_rather_than_granting(
    client, ldap_enabled, fake_ldap, monkeypatch
):
    monkeypatch.setattr(settings, "LDAP_DEFAULT_ROLE", "SUPERUSER", raising=False)
    fake_ldap(bind_ok=True, groups=("CN=Visitors,DC=hospital,DC=org",))
    assert _login(client, "visitor", "pw").status_code == 401


def test_role_is_never_lowercase_from_the_directory(client, ldap_enabled, fake_ldap, monkeypatch):
    """Regression: the service used to emit lowercase roles."""
    monkeypatch.setattr(settings, "LDAP_GROUP_ROLE_MAP", "CN=Radiologists=radiologist", raising=False)
    fake_ldap(bind_ok=True, groups=("CN=Radiologists,DC=hospital,DC=org",))
    resp = _login(client, "dr.lower", "pw")
    assert resp.status_code == 200
    assert resp.json()["role"] == "RADIOLOGIST"


def test_disabled_account_cannot_log_in_via_ldap(client, ldap_enabled, fake_ldap):
    fake_ldap(bind_ok=True, groups=("CN=Radiologists,DC=hospital,DC=org",))
    assert _login(client, "dr.disabled", "pw").status_code == 200

    # Disable the account, then try again.
    import asyncio

    from app.db.session import AsyncSessionLocal
    from app.db.models import User
    from sqlalchemy import select

    async def _disable():
        async with AsyncSessionLocal() as session:
            user = (
                await session.execute(select(User).where(User.username == "dr.disabled"))
            ).scalar_one()
            user.is_active = False
            await session.commit()

    asyncio.run(_disable())

    resp = _login(client, "dr.disabled", "pw")
    assert resp.status_code == 401, resp.text


# --------------------------------------------------------------------------- #
# rate limiting
# --------------------------------------------------------------------------- #
def test_ldap_login_is_rate_limited(client, ldap_enabled, fake_ldap, monkeypatch):
    fake_ldap(bind_ok=False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True, raising=False)
    reset_rate_limits()
    try:
        codes = [_login(client, "flood", "pw").status_code for _ in range(12)]
    finally:
        reset_rate_limits()
    assert 429 in codes, f"expected the login limiter to trip, got {codes}"
    assert codes[0] == 401, "the first attempts must reach the directory path"


# --------------------------------------------------------------------------- #
# unit coverage of the mapping helpers
# --------------------------------------------------------------------------- #
def test_parse_group_role_map_ignores_unknown_roles():
    mapping = ldap_service.parse_group_role_map(
        "CN=Radiologists=ADMIN;CN=Doctors=physician;CN=Bad=SUPERUSER;;"
    )
    assert mapping["cn=radiologists"] == "ADMIN"
    assert mapping["radiologists"] == "ADMIN"
    assert mapping["cn=doctors"] == "PHYSICIAN"
    assert mapping["doctors"] == "PHYSICIAN"
    assert all(role in ROLES for role in mapping.values())


def test_parse_group_role_map_empty():
    assert ldap_service.parse_group_role_map("") == {}
    assert ldap_service.parse_group_role_map(None) == {}


def test_resolve_role_prefers_mapped_group_over_default(monkeypatch):
    monkeypatch.setattr(settings, "LDAP_GROUP_ROLE_MAP", "CN=Radiologists=ADMIN", raising=False)
    monkeypatch.setattr(settings, "LDAP_DEFAULT_ROLE", "PHYSICIAN", raising=False)
    assert ldap_service.resolve_role(["CN=Radiologists,DC=h"]) == "ADMIN"
    assert ldap_service.resolve_role(["CN=Other,DC=h"]) == "PHYSICIAN"
    assert ldap_service.resolve_role([]) == "PHYSICIAN"


def test_resolve_role_denies_without_default(monkeypatch):
    monkeypatch.setattr(settings, "LDAP_GROUP_ROLE_MAP", "CN=Radiologists=ADMIN", raising=False)
    monkeypatch.setattr(settings, "LDAP_DEFAULT_ROLE", "", raising=False)
    assert ldap_service.resolve_role(["CN=Other,DC=h"]) is None


def test_user_dn_template_is_applied(monkeypatch):
    monkeypatch.setattr(settings, "LDAP_BASE_DN", "DC=hospital,DC=org", raising=False)
    monkeypatch.setattr(settings, "LDAP_USER_DN_TEMPLATE", "uid={username},{base_dn}", raising=False)
    assert ldap_service._user_dn("dr.house") == "uid=dr.house,DC=hospital,DC=org"


def test_invalid_user_dn_template_is_reported(monkeypatch):
    monkeypatch.setattr(settings, "LDAP_BASE_DN", "DC=h", raising=False)
    monkeypatch.setattr(settings, "LDAP_USER_DN_TEMPLATE", "uid={nope}", raising=False)
    with pytest.raises(ldap_service.LDAPConfigurationError):
        ldap_service._user_dn("dr.house")


def test_no_local_password_hash_is_unusable():
    from app.core.security import verify_password

    for guess in ("", "ldap_managed_password", "anything"):
        assert verify_password(guess, ldap_service.NO_LOCAL_PASSWORD_HASH) is False


def test_normalize_role_canonicalises_and_rejects():
    assert normalize_role("radiologist") == "RADIOLOGIST"
    assert normalize_role(" Admin ") == "ADMIN"
    assert normalize_role("superuser") is None
    assert normalize_role(None) is None
