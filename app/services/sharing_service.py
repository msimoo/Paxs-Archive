"""Patient sharing (Zero-CD): temporary links + QR codes instead of CDs.

A share is a random unguessable token bound to a study with an expiry.
"""

from __future__ import annotations

import base64
import io
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import qrcode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import resolve_public_base_url, settings
from app.core.proxy_prefix import prefix_path
from app.db.models import PatientShare, Study


class ShareError(RuntimeError):
    pass


def _now():
    """Naive UTC now (matching the model's naive UTC datetimes)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def qr_base64(url: str) -> str:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def create_share(
    session: AsyncSession,
    study_uid: str,
    expiry_days: int | None = None,
    phone: str | None = None,
) -> PatientShare:
    study = (
        await session.execute(select(Study).where(Study.study_instance_uid == study_uid))
    ).scalar_one_or_none()
    if study is None:
        raise ShareError(f"Study {study_uid} not found")

    days = expiry_days or settings.SHARE_EXPIRY_DAYS
    share = PatientShare(
        study_instance_uid=study_uid,
        share_token=secrets.token_urlsafe(32),
        phone=phone,
        expires_at=_now() + timedelta(days=days),
    )
    session.add(share)
    await session.flush()
    return share


async def get_share(session: AsyncSession, token: str) -> PatientShare | None:
    share = (
        await session.execute(select(PatientShare).where(PatientShare.share_token == token))
    ).scalar_one_or_none()
    if share is None:
        return None
    if share.expires_at < _now():
        return None
    return share


def share_url(token: str, request=None) -> str:
    """Absolute URL for a share token (used in links + QR codes).

    The origin comes from :func:`resolve_public_base_url`, so an unconfigured
    deployment derives it from the request (X-Forwarded-Host / Host) instead of
    minting a dead ``http://0.0.0.0/...`` link (V2-2).
    """
    base = resolve_public_base_url(request)
    root_path = request.scope.get("root_path", "") if request is not None else ""
    route = prefix_path(root_path, "/api/v1/share/exchange")
    return f"{base}{route}?{urlencode({'share': token})}"


def human_expiry(share: PatientShare) -> str:
    return share.expires_at.strftime("%Y-%m-%d %H:%M UTC")
