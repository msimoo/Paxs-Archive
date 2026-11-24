"""Record and play dictation with the same tenant scope as its study."""

import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import write_audit
from app.api.v1.deps import get_current_user_any, require_study_access
from app.core.roles import DICTATION_LISTEN_ROLES, DICTATION_RECORD_ROLES
from app.db.models import StudyDictation, User
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/studies/{study_uid}/dictations", tags=["dictations"])
MAX_BYTES = 16 * 1024 * 1024
MAX_SECONDS = 15 * 60
AUDIO_MARKERS = {
    "audio/webm": (0, bytes.fromhex("1a45dfa3")),
    "audio/ogg": (0, b"OggS"),
    "audio/mp4": (4, b"ftyp"),
}
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


async def require_listener(user: User = Depends(get_current_user_any)) -> User:
    if user.role not in DICTATION_LISTEN_ROLES:
        raise HTTPException(403, "Dictation playback requires a radiologist, report writer, or administrator")
    return user


@router.get("/permission")
async def permission(
    study_uid: str, response: Response, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_any),
) -> dict:
    response.headers.update(PRIVATE_HEADERS)
    if user.role not in DICTATION_RECORD_ROLES:
        return {"can_record": False}
    await require_study_access(db, study_uid, user)
    return {"can_record": True, "max_seconds": MAX_SECONDS}


@router.get("")
async def list_dictations(
    study_uid: str, response: Response, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_listener),
) -> list[dict]:
    study = await require_study_access(db, study_uid, user)
    response.headers.update(PRIVATE_HEADERS)
    rows = await db.execute(
        select(StudyDictation.id, StudyDictation.recorded_by, StudyDictation.created_at)
        .where(StudyDictation.study_id == study.id)
        .order_by(StudyDictation.created_at.desc(), StudyDictation.id)
    )
    return [
        {"id": str(row.id), "recorded_by": row.recorded_by, "created_at": row.created_at.isoformat()}
        for row in rows
    ]


@router.put("/{recording_id}", status_code=201)
async def store_dictation(
    study_uid: str, recording_id: UUID, request: Request,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user_any),
) -> dict:
    if user.role not in DICTATION_RECORD_ROLES:
        raise HTTPException(403, "Recording requires a radiologist or administrator")
    study = await require_study_access(db, study_uid, user)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in AUDIO_MARKERS:
        raise HTTPException(415, "Unsupported audio format")
    length = request.headers.get("content-length", "")
    if length.isdecimal() and (len(length) > 10 or int(length) > MAX_BYTES):
        raise HTTPException(413, "Dictation exceeds 16 MiB")

    audio = bytearray()
    async for chunk in request.stream():
        if len(audio) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "Dictation exceeds 16 MiB")
        audio.extend(chunk)
    offset, marker = AUDIO_MARKERS[content_type]
    if len(audio) <= offset + len(marker) or audio[offset:offset + len(marker)] != marker:
        raise HTTPException(415, "Invalid audio container")

    inserted_id = await db.scalar(
        insert(StudyDictation).values(
            id=recording_id, study_id=study.id, recorded_by=user.username,
            content_type=content_type, audio=bytes(audio),
        ).on_conflict_do_nothing(index_elements=[StudyDictation.id]).returning(StudyDictation.id)
    )
    if inserted_id is None:
        existing = await db.get(StudyDictation, recording_id)
        if existing is None or existing.study_id != study.id or existing.recorded_by != user.username:
            raise HTTPException(409, "Recording identifier is already in use")
    else:
        await write_audit(db, user.id, "dictation_recorded", request, {"recording_id": str(recording_id)})
    await db.commit()
    return {"id": str(recording_id)}


def _audio_response(audio: bytes, content_type: str, range_header: str) -> Response:
    headers = {**PRIVATE_HEADERS, "Accept-Ranges": "bytes"}
    match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", range_header)
    if not match or not any(match.groups()):
        return Response(audio, media_type=content_type, headers=headers)

    first, last = match.groups()
    size = len(audio)
    start = int(first) if first else max(0, size - int(last))
    end = min(size - 1, int(last)) if first and last else size - 1
    if not 0 <= start <= end:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    return Response(
        audio[start:end + 1], status_code=206, media_type=content_type,
        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"},
    )


@router.get("/{recording_id}/audio")
async def play_dictation(
    study_uid: str, recording_id: UUID, request: Request,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_listener),
) -> Response:
    study = await require_study_access(db, study_uid, user)
    track = (
        await db.execute(
            select(StudyDictation.audio, StudyDictation.content_type)
            .where(StudyDictation.study_id == study.id, StudyDictation.id == recording_id)
        )
    ).one_or_none()
    if track is None:
        raise HTTPException(404, "Dictation not found")
    return _audio_response(track.audio, track.content_type, request.headers.get("range", ""))
