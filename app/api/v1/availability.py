"""Tenant-scoped viewer availability endpoint (A1)."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user_any, tenant_scope
from app.core.config import settings
from app.db.models import User
from app.db.session import get_db
from app.services import availability_service

router = APIRouter(prefix="/api/v1", tags=["availability"])


@router.get("/studies/{study_uid}/availability")
async def get_study_availability(
    study_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_any),
) -> JSONResponse:
    """Return a bounded sample of object presence for every visible series."""
    if settings.AVAILABILITY_SAMPLE_PER_SERIES == 0:
        raise HTTPException(status_code=404, detail="Not found")
    tenant_id, include_all = tenant_scope(user)
    try:
        payload = await availability_service.study_availability(
            db, study_uid, tenant_id=tenant_id, include_all=include_all,
            sample=settings.AVAILABILITY_SAMPLE_PER_SERIES,
            max_checks=settings.AVAILABILITY_MAX_CHECKS,
        )
    except availability_service.StudyAvailabilityNotFound as exc:
        raise HTTPException(status_code=404, detail="Study not found") from exc
    return JSONResponse(payload)
