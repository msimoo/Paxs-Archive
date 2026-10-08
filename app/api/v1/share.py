"""Patient share links (Zero-CD): create/list/delete temporary links + QR."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db.models import AuditLog, Series, Study
from app.db.session import get_db
from app.services import query_service
from app.services.sharing_service import ShareError, create_share, get_share, qr_base64, share_url
from app.api.v1.deps import (
    SHARE_COOKIE_NAME,
    get_current_user,
    require_destructive_role,
    require_study_access,
    share_cookie_max_age,
)

router = APIRouter(prefix="/api/v1/share", tags=["share"])


class CreateShareBody(BaseModel):
    study_uid: str = Field(min_length=1)
    expiry_days: int | None = Field(default=None, ge=1, le=365)
    phone: str | None = None


@router.post("", status_code=201)
async def create_share_endpoint(request: Request, body: CreateShareBody,
                                db: AsyncSession = Depends(get_db),
                                user=Depends(get_current_user)):
    # C04: creating a public link is a disclosure — it must be your tenant's study.
    await require_study_access(db, body.study_uid, user)
    try:
        share = await create_share(db, body.study_uid, body.expiry_days, body.phone)
    except ShareError as exc:
        raise HTTPException(404, str(exc)) from exc

    db.add(AuditLog(
        study_instance_uid=body.study_uid,
        action="SHARED",
        performed_by=user.username,
    ))

    url = share_url(share.share_token, request)
    return {
        "token": share.share_token,
        "url": url,
        "study_uid": body.study_uid,
        "expires_at": share.expires_at.isoformat() + "Z",
        "expiry_days": body.expiry_days or settings.SHARE_EXPIRY_DAYS,
        "phone": share.phone,
        "qr_base64": qr_base64(url),
    }


@router.get("/{token}")
async def get_share_info(token: str, response: Response, db: AsyncSession = Depends(get_db)):
    share = await get_share(db, token)
    if share is None:
        raise HTTPException(404, "Share link not found or expired")
    share.accessed_count += 1

    # Hand the caller a study-scoped cookie so the viewer's later DICOMweb and
    # volume reads keep working when DICOMWEB_PUBLIC is false (C03). The cookie
    # only ever authorises READ access to this one study.
    response.set_cookie(
        SHARE_COOKIE_NAME,
        token,
        max_age=share_cookie_max_age(share.expires_at),
        httponly=True,
        # Deliberately lax, not AUTH_COOKIE_SAMESITE: share links are followed
        # from external sites, where SameSite=strict would drop the cookie.
        samesite="lax",
        secure=settings.AUTH_COOKIE_SECURE,
        path="/",
    )

    study = (
        await db.execute(
            select(Study).options(joinedload(Study.patient))
            .where(Study.study_instance_uid == share.study_instance_uid)
        )
    ).scalar_one_or_none()
    if study is None:
        raise HTTPException(404, "Study not found")

    series_list = (
        await db.execute(
            select(Series).where(Series.study_id == study.id)
            .order_by(Series.series_number.is_not(None).desc(), Series.series_number, Series.id)
        )
    ).scalars().all()
    counts = await query_service.series_instance_counts(db, [s.id for s in series_list])

    return {
        "study_uid": study.study_instance_uid,
        "expires_at": share.expires_at.isoformat() + "Z",
        "accessed_count": share.accessed_count,
        "patient": {
            "name": study.patient.patient_name,
            "id": study.patient.patient_id,
        },
        "study": {
            "date": study.study_date,
            "description": study.study_description,
            "accession": study.accession_number,
        },
        "series": [
            {
                "series_uid": s.series_instance_uid,
                "modality": s.modality,
                "series_number": s.series_number,
                "description": s.series_description,
                "instances": counts.get(s.id, 0),
            }
            for s in series_list
        ],
    }


@router.get("/{token}/qr.png")
async def share_qr(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    share = await get_share(db, token)
    if share is None:
        raise HTTPException(404, "Share link not found or expired")
    url = share_url(share.share_token, request)
    import base64

    return Response(content=base64.b64decode(qr_base64(url)), media_type="image/png")


@router.delete("/{token}", status_code=204)
async def delete_share(token: str, db: AsyncSession = Depends(get_db),
                       user=Depends(require_destructive_role)):
    share = await get_share(db, token)
    if share is None:
        raise HTTPException(404, "Share link not found or expired")
    await db.delete(share)
