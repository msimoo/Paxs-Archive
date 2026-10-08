"""P1 hardening — H04 (session expiry), H07 (rate-limit identity), H03 (permission
matrix), H05 (upload validation + print escaping), H06 (production security
defaults fail closed) and H10 (S3/boto3 is a declared production dependency).
"""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydicom.uid import generate_uid
from sqlalchemy import func, select

from app.core.config import (
    Settings,
    is_production_environment,
    settings,
    validate_production_settings,
)
from app.core.security import hash_refresh_token
from app.db.models import Session, User
from app.db.session import AsyncSessionLocal
from tests.helpers import (
    auth_for_role,
    build_stow_body,
    ds_bytes,
    make_instance,
    stow_headers,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stow_study(client, headers, patient_id: str = "P1_PAT") -> str:
    study_uid = generate_uid()
    ds = make_instance(
        patient_id=patient_id,
        study_uid=study_uid,
        series_uid=generate_uid(),
        sop_uid=generate_uid(),
        instance_num=1,
        rows=8,
        cols=8,
        pixel=np.full((8, 8), 30, dtype=np.uint16),
    )
    resp = client.post(
        "/dicom-web/studies",
        content=build_stow_body([ds_bytes(ds)]),
        headers={**stow_headers(), **headers},
    )
    assert resp.status_code == 200, resp.text
    return study_uid


# --------------------------------------------------------------------------- #
# H04 — session expiry is enforced on every request
# --------------------------------------------------------------------------- #
def _make_session(client, username: str, *, expired: bool) -> str:
    """Create a session row for `username` and return its raw refresh token."""

    async def run():
        async with AsyncSessionLocal() as session:
            async with session.begin():
                user = (
                    await session.execute(select(User).where(User.username == username))
                ).scalar_one()
                raw = f"tok-{username}-{'expired' if expired else 'valid'}"
                next_id = int(
                    (await session.execute(select(func.coalesce(func.max(Session.id), 0)))).scalar_one()
                ) + 1
                session.add(
                    Session(
                        id=next_id,
                        family_id=f"fam-{username}",
                        user_id=user.id,
                        refresh_token_hash=hash_refresh_token(raw),
                        expires_at=_naive_utc() + (timedelta(hours=-1) if expired else timedelta(hours=1)),
                    )
                )
                return raw

    return asyncio.run(run())


def _cookie_request(token: str):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"cookie", f"{settings.AUTH_COOKIE_NAME}={token}".encode())],
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_expired_session_cookie_is_rejected(client, auth_headers):
    """H04: expiry is checked on the request, not left to the background sweep."""
    from app.api.v1.deps import get_current_user_from_cookie

    from tests.conftest import _create_test_user

    _create_test_user("h04_user", "PHYSICIAN")
    valid = _make_session(client, "h04_user", expired=False)
    expired = _make_session(client, "h04_user", expired=True)

    assert asyncio.run(get_current_user_from_cookie(_cookie_request(valid))) is not None
    assert asyncio.run(get_current_user_from_cookie(_cookie_request(expired))) is None


def test_password_reset_revokes_sessions(client, auth_headers):
    """H04: a stolen cookie must stop working once the password changes."""
    from tests.conftest import _create_test_user

    _create_test_user("h04_reset", "PHYSICIAN")
    token = _make_session(client, "h04_reset", expired=False)

    async def user_id():
        async with AsyncSessionLocal() as session:
            return (
                await session.execute(select(User.id).where(User.username == "h04_reset"))
            ).scalar_one()

    uid = asyncio.run(user_id())
    resp = client.post(
        f"/api/v1/users/{uid}/reset-password",
        headers=auth_headers,
        json={"password": "newpass456"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sessions_revoked"] >= 1

    from app.api.v1.deps import get_current_user_from_cookie

    assert asyncio.run(get_current_user_from_cookie(_cookie_request(token))) is None


# --------------------------------------------------------------------------- #
# H07 — X-Forwarded-For is only believed from a trusted proxy
# --------------------------------------------------------------------------- #
def test_untrusted_peer_cannot_rotate_xff_to_bypass_the_cap(client, monkeypatch):
    """H07: rotating the header from an untrusted peer must not reset the cap."""
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "10.9.9.9", raising=False)

    # The route binds its limit at import time, so exceed the configured value.
    limit = settings.RATE_LIMIT_LOGIN_PER_MINUTE
    statuses = []
    for i in range(limit + 3):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "nobody", "password": "wrong"},
            headers={"X-Forwarded-For": f"203.0.113.{i}"},
        )
        statuses.append(resp.status_code)
    assert 429 in statuses, f"rotating X-Forwarded-For bypassed the cap: {statuses}"


def test_trusted_proxy_header_is_honoured(client, monkeypatch):
    """H07: a configured proxy's header does identify the real client."""
    from app.api.ratelimit import _client_key, _trusted_proxies
    from starlette.requests import Request

    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "127.0.0.1", raising=False)
    assert "127.0.0.1" in _trusted_proxies()

    def req(peer: str, forwarded: str | None):
        headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
        return Request(
            {"type": "http", "method": "GET", "path": "/", "headers": headers,
             "client": (peer, 1234)}
        )

    assert _client_key(req("127.0.0.1", "198.51.100.7")) == "198.51.100.7"
    # Same header from an untrusted peer is ignored.
    assert _client_key(req("203.0.113.5", "198.51.100.7")) == "203.0.113.5"


# --------------------------------------------------------------------------- #
# H03 — the permission matrix
# --------------------------------------------------------------------------- #
PRIVILEGED_MUTATIONS = [
    ("post", "/api/v1/lifecycle/rules"),
    ("post", "/api/v1/lifecycle/run"),
    ("post", "/api/v1/routing/rules"),
    ("post", "/api/v1/compression/lossy/rules"),
    ("post", "/api/v1/compression/lossy/run"),
    ("post", "/api/v1/studies/split"),
    ("post", "/api/v1/studies/merge"),
]


@pytest.mark.parametrize("method,path", PRIVILEGED_MUTATIONS)
def test_ordinary_user_is_denied_privileged_mutations(client, auth_headers, method, path):
    """H03: an ordinary clinical account must not reach these."""
    physician = auth_for_role(client, "p1_physician", role="PHYSICIAN", admin=auth_headers)
    resp = client.post(path, headers=physician, json={})
    assert resp.status_code == 403, f"{path} -> {resp.status_code} (expected 403)"


def test_privileged_mutations_require_authentication(client):
    for _method, path in PRIVILEGED_MUTATIONS:
        assert client.post(path, json={}).status_code in (401, 403), path


def test_reads_stay_available_to_ordinary_users(client, auth_headers):
    """The matrix must not lock out the read paths."""
    physician = auth_for_role(client, "p1_reader", role="PHYSICIAN", admin=auth_headers)
    for path in (
        "/api/v1/lifecycle/rules",
        "/api/v1/routing/rules",
        "/api/v1/compression/lossy/rules",
    ):
        assert client.get(path, headers=physician).status_code == 200, path


# --------------------------------------------------------------------------- #
# H05 — upload validation and print escaping
# --------------------------------------------------------------------------- #
def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_valid_png_logo_is_accepted(client, auth_headers):
    resp = client.post(
        "/api/v1/settings/organization",
        headers=auth_headers,
        data={"org_name_en": "PAXS"},
        files={"logo_file": ("logo.png", _png_bytes(), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["logo_path"].endswith(".png")


@pytest.mark.parametrize(
    "filename,payload,mime",
    [
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "image/svg+xml"),
        ("logo.html", b"<html><script>alert(1)</script></html>", "text/html"),
        ("logo.png", b"<html><script>alert(1)</script></html>", "image/png"),
        ("logo.png", b"not an image at all", "image/png"),
    ],
)
def test_non_image_uploads_are_rejected(client, auth_headers, filename, payload, mime):
    """H05: SVG/HTML and fake images must never reach the static tree."""
    resp = client.post(
        "/api/v1/settings/organization",
        headers=auth_headers,
        data={"org_name_en": "PAXS"},
        files={"logo_file": (filename, payload, mime)},
    )
    assert resp.status_code in (413, 422), f"{filename} -> {resp.status_code}"


def test_oversized_logo_is_rejected(client, auth_headers):
    """H05: the 2 MB cap is enforced."""
    from app.api.v1.settings import MAX_LOGO_BYTES

    payload = _png_bytes() + b"\x00" * (MAX_LOGO_BYTES + 1)
    resp = client.post(
        "/api/v1/settings/organization",
        headers=auth_headers,
        data={"org_name_en": "PAXS"},
        files={"logo_file": ("big.png", payload, "image/png")},
    )
    assert resp.status_code == 413, resp.text


def test_print_report_escapes_clinical_text(client, auth_headers):
    """H05: a report body containing markup must be rendered as text."""
    study_uid = _stow_study(client, auth_headers)
    payload = "<script>alert('xss')</script>"
    saved = client.post(
        "/api/v1/reports/save",
        headers=auth_headers,
        json={
            "study_instance_uid": study_uid,
            "findings": payload,
            "impression": payload,
            "status": "DRAFT",
        },
    )
    assert saved.status_code == 200, saved.text

    page = client.get(f"/api/v1/reports/print/{study_uid}", headers=auth_headers)
    assert page.status_code == 200
    body = page.text
    assert "<script>alert" not in body, "clinical text was interpolated unescaped"
    assert "&lt;script&gt;" in body, "clinical text should appear escaped"


# --------------------------------------------------------------------------- #
# H06 — production security defaults fail closed
# --------------------------------------------------------------------------- #
def _production_settings(**overrides) -> Settings:
    """A Settings object that is *valid* in production, then apply overrides."""
    s = Settings()
    s.ENVIRONMENT = "production"
    s.SECRET_KEY = "x" * 48
    s.AUTH_COOKIE_SECURE = True
    s.AUTH_COOKIE_SAMESITE = "lax"
    s.DEBUG = False
    s.RATE_LIMIT_ENABLED = True
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def test_hardened_production_configuration_is_accepted():
    validate_production_settings(_production_settings())  # must not raise


def test_the_shipped_secret_default_is_a_known_insecure_value():
    """The default must be recognisable, otherwise production cannot refuse it."""
    assert Settings.model_fields["SECRET_KEY"].default == "paxs-insecure-dev-key-change-me"
    assert Settings.model_fields["AUTH_COOKIE_SECURE"].default is False
    assert Settings.model_fields["ENVIRONMENT"].default == "development"


def test_development_keeps_the_insecure_defaults_working():
    """Outside production the shipped defaults stay usable (local dev / run_all.bat)."""
    s = Settings()
    s.ENVIRONMENT = "development"
    s.SECRET_KEY = "paxs-insecure-dev-key-change-me"
    s.AUTH_COOKIE_SECURE = False
    s.AUTH_COOKIE_SAMESITE = "lax"
    s.DEBUG = True
    validate_production_settings(s)  # must not raise outside production


def test_shipped_defaults_are_not_treated_as_production():
    """A plain local install must keep booting (no DEBUG/ENVIRONMENT pinned)."""
    assert is_production_environment(Settings()) is False


@pytest.mark.parametrize(
    "value,expected",
    [
        ("production", True),
        ("prod", True),
        ("PRODUCTION", True),
        ("staging", True),   # unknown on purpose: fail closed
        ("", True),          # empty/typo'd: fail closed
        ("development", False),
        ("local", False),
        ("test", False),
    ],
)
def test_environment_classification_fails_closed(value, expected):
    s = Settings()
    s.ENVIRONMENT = value
    assert is_production_environment(s) is expected


def test_pinning_debug_off_alone_requires_production_settings():
    """H06: ``DEBUG=False`` switched off explicitly is a production signal."""
    s = Settings()
    s.ENVIRONMENT = "development"
    s.DEBUG = False  # explicit setattr, mirroring an env-provided value
    assert is_production_environment(s) is True
    with pytest.raises(RuntimeError):
        validate_production_settings(s)  # the shipped defaults are insecure


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"SECRET_KEY": "paxs-insecure-dev-key-change-me"}, "SECRET_KEY"),
        ({"SECRET_KEY": "short-secret"}, "SECRET_KEY"),
        ({"AUTH_COOKIE_SECURE": False}, "AUTH_COOKIE_SECURE"),
        ({"AUTH_COOKIE_SAMESITE": ""}, "AUTH_COOKIE_SAMESITE"),
        ({"AUTH_COOKIE_SAMESITE": "Laxx"}, "AUTH_COOKIE_SAMESITE"),
        ({"DEBUG": True}, "DEBUG"),
        ({"RATE_LIMIT_ENABLED": False}, "RATE_LIMIT_ENABLED"),
    ],
)
def test_production_refuses_each_insecure_default(override, expected):
    """H06: an insecure default is a startup error, not a warning."""
    with pytest.raises(RuntimeError) as exc:
        validate_production_settings(_production_settings(**override))
    assert expected in str(exc.value)


def test_production_refusal_message_is_human_readable():
    """The refusal must name the header and list one problem per line."""
    with pytest.raises(RuntimeError) as exc:
        validate_production_settings(
            _production_settings(SECRET_KEY="short", AUTH_COOKIE_SECURE=False)
        )
    text = str(exc.value)
    header, _, rest = text.partition("\n")
    # Pre-fix this line carried the mangled separator ("configuration://n  - ")
    # glued to the first problem, so this pins the whole first line.
    assert header == "Refusing to start in production with an insecure configuration:"
    assert text.count("\n  - ") == 2, "each problem belongs on its own dash line"
    assert "//n" not in text, "an escaped newline must not leak into the message"
    for problem in ("SECRET_KEY", "AUTH_COOKIE_SECURE"):
        assert problem in rest


def test_session_cookie_honours_the_configured_policy(monkeypatch):
    """H06: the cookie policy settings must reach the cookie, not be hardcoded."""
    from fastapi import Response

    from app.core.security import set_session_cookie

    monkeypatch.setattr(settings, "AUTH_COOKIE_SAMESITE", "strict")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", True)

    response = Response()
    set_session_cookie(response, "refresh-token-value")

    header = response.headers["set-cookie"].lower()
    assert "samesite=strict" in header, "AUTH_COOKIE_SAMESITE must be applied"
    assert "secure" in header, "AUTH_COOKIE_SECURE must be applied"
    assert "httponly" in header


def test_share_cookie_stays_lax_even_when_sessions_are_strict(
    client, auth_headers, monkeypatch
):
    """A share link opens cross-site, so SameSite=strict would drop its cookie."""
    monkeypatch.setattr(settings, "AUTH_COOKIE_SAMESITE", "strict")

    study_uid = _stow_study(client, auth_headers, patient_id="SHARE_PAT")
    created = client.post("/api/v1/share", headers=auth_headers, json={"study_uid": study_uid})
    assert created.status_code == 201, created.text

    opened = client.get(f"/api/v1/share/{created.json()['token']}")
    assert opened.status_code == 200, opened.text
    share_headers = [
        value for value in opened.headers.get_list("set-cookie")
        if value.lower().startswith("paxs_share=")
    ]
    assert share_headers, "opening a share must hand out the study-scoped cookie"
    assert "samesite=lax" in share_headers[0].lower()


def test_production_https_fails_closed_when_certificates_are_missing(monkeypatch, tmp_path):
    """H06: never silently downgrade to plaintext in production (entry point)."""
    import run_paxs

    monkeypatch.setattr(run_paxs.settings, "HTTPS_ENABLED", True, raising=False)
    monkeypatch.setattr(run_paxs.settings, "ENVIRONMENT", "production", raising=False)
    monkeypatch.setattr(run_paxs.settings, "HTTPS_CERT", str(tmp_path / "absent.crt"))
    monkeypatch.setattr(run_paxs.settings, "HTTPS_KEY", str(tmp_path / "absent.key"))

    with pytest.raises(SystemExit) as exc:
        run_paxs._load_ssl_context()
    assert "refusing to start" in str(exc.value).lower()


def test_production_https_fails_closed_when_certificates_are_unreadable(monkeypatch, tmp_path):
    """H06: present-but-unreadable certs must not silently downgrade either."""
    import run_paxs

    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("placeholder", encoding="utf-8")
    key.write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(run_paxs.settings, "HTTPS_ENABLED", True, raising=False)
    monkeypatch.setattr(run_paxs.settings, "ENVIRONMENT", "production", raising=False)
    monkeypatch.setattr(run_paxs.settings, "HTTPS_CERT", str(cert))
    monkeypatch.setattr(run_paxs.settings, "HTTPS_KEY", str(key))
    # chmod() cannot express "unreadable" on Windows, so deny readability directly.
    monkeypatch.setattr(run_paxs.os, "access", lambda *a, **k: False)

    with pytest.raises(SystemExit) as exc:
        run_paxs._load_ssl_context()
    assert "refusing to start" in str(exc.value).lower()
    assert "unreadable" in str(exc.value).lower()


def test_development_https_falls_back_to_http_with_a_warning(monkeypatch, tmp_path, capsys):
    """Outside production a missing certificate stays a loud warning, not a crash."""
    import run_paxs

    monkeypatch.setattr(run_paxs.settings, "HTTPS_ENABLED", True, raising=False)
    monkeypatch.setattr(run_paxs.settings, "ENVIRONMENT", "development", raising=False)
    monkeypatch.setattr(run_paxs.settings, "HTTPS_CERT", str(tmp_path / "absent.crt"))
    monkeypatch.setattr(run_paxs.settings, "HTTPS_KEY", str(tmp_path / "absent.key"))

    assert run_paxs._load_ssl_context() is None
    assert "HTTP" in capsys.readouterr().out


def test_auto_reload_is_disabled_in_production(monkeypatch):
    """H06: production must not run uvicorn's file watcher."""
    import run_paxs

    for value, expected in (
        ("production", True),
        ("prod", True),
        ("staging", True),
        ("", True),  # unknown/empty fails closed
        ("development", False),
        ("local", False),
    ):
        monkeypatch.setattr(run_paxs.settings, "ENVIRONMENT", value, raising=False)
        assert run_paxs._is_production() is expected, value

    source = (PROJECT_ROOT / "run_paxs.py").read_text(encoding="utf-8")
    assert "reload=not _is_production()" in source, "reload must be derived from the environment"
    # One shared predicate: the runner must not re-implement the check.
    assert "is_production_environment(settings)" in source


# --------------------------------------------------------------------------- #
# H10 — S3 object storage ships with its driver
# --------------------------------------------------------------------------- #
def _active_requirement_lines() -> list[str]:
    text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_boto3_is_an_active_requirement():
    """H10: the S3 backend must not depend on a commented-out driver."""
    assert any(line.startswith("boto3") for line in _active_requirement_lines()), (
        "boto3 must be installed: the LB/Docker deployment sets STORAGE_BACKEND=s3"
    )


def test_botocore_is_an_active_requirement():
    """H10: boto3 alone is not enough — botocore is the runtime it loads."""
    assert any(line.startswith("botocore") for line in _active_requirement_lines()), (
        "botocore must be declared alongside boto3 for reproducible images"
    )


def test_boto3_is_importable_in_the_running_environment():
    import boto3

    assert boto3.__version__


def test_botocore_is_importable_in_the_running_environment():
    import botocore

    assert botocore.__version__


def test_container_image_installs_the_requirements_that_declare_boto3():
    dockerfile = (PROJECT_ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    assert "-r requirements.txt" in dockerfile, "the image must install requirements.txt"


def test_compose_s3_environment_matches_the_settings_fields():
    """A mismatch here is the H10 defect class: S3 selected, config/driver missing."""
    compose = (PROJECT_ROOT / "deploy" / "docker-compose.lb.yml").read_text(encoding="utf-8")
    assert "STORAGE_BACKEND: s3" in compose
    for name in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET"):
        assert name in compose, f"{name} must be provided to the node containers"
        assert name in Settings.model_fields, f"compose sets {name} but Settings has no such field"


def test_s3_backend_initialises_without_a_missing_module_error():
    """The lazy boto3 import must resolve offline (no network, no MinIO needed)."""
    from app.services.storage import S3StorageBackend

    backend = S3StorageBackend(
        bucket="paxs-test",
        endpoint_url="http://127.0.0.1:9000",
        region="us-east-1",
        access_key="test-key",
        secret_key="test-secret",
    )
    client = backend._s3()
    assert type(client).__name__.lower().startswith("s3")
    # The client is a real botocore-backed boto3 client, not a stub.
    import botocore.client

    assert isinstance(client, botocore.client.BaseClient)
