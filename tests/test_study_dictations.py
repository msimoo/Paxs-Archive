import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.dictations import _audio_response, require_listener


@pytest.mark.parametrize(("header", "body", "expected_range"), [
    ("bytes=1-3", b"bcd", "bytes 1-3/6"),
    ("bytes=4-", b"ef", "bytes 4-5/6"),
    ("bytes=-2", b"ef", "bytes 4-5/6"),
])
def test_dictation_audio_ranges_allow_native_player_seeking(header, body, expected_range):
    response = _audio_response(b"abcdef", "audio/webm", header)

    assert response.status_code == 206
    assert response.body == body
    assert response.headers["content-range"] == expected_range
    assert response.headers["cache-control"] == "private, no-store"


def test_dictation_audio_rejects_unsatisfiable_range():
    response = _audio_response(b"abcdef", "audio/webm", "bytes=6-")

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */6"


@pytest.mark.parametrize("role", ["ADMIN", "RADIOLOGIST", "REPORT_WRITER"])
def test_dictation_roles_can_listen(role):
    user = SimpleNamespace(role=role)
    assert asyncio.run(require_listener(user)) is user


@pytest.mark.parametrize("role", ["PHYSICIAN", "TECHNOLOGIST"])
def test_other_archive_roles_cannot_listen(role):
    with pytest.raises(HTTPException) as denied:
        asyncio.run(require_listener(SimpleNamespace(role=role)))

    assert denied.value.status_code == 403

