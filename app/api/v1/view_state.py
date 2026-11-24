"""D4: per-user 3D view state endpoints (thin router).

GET/PUT ``/api/v1/studies/{uid}/view-state`` — the saved camera/preset/crop/curves
for the signed-in reader. No SQL here: the query and the tenant predicate live in
``view_state_service``, and the router only maps outcomes to status codes.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user_any, tenant_scope
from app.db.models import User
from app.db.session import get_db
from app.services import view_state_service

router = APIRouter(prefix="/api/v1", tags=["view-state"])


class ViewStateBody(BaseModel):
    """The viewer's own versioned document, opaque to the server."""

    state: str = Field(min_length=2, max_length=view_state_service.MAX_VIEW_STATE_BYTES)


@router.get("/studies/{study_uid}/view-state")
async def get_view_state(
    study_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_any),
) -> JSONResponse:
    """Return the caller's saved 3D view; 404 when there is none or the study is not theirs."""
    tenant_id, include_all = tenant_scope(user)
    try:
        payload = await view_state_service.get_view_state(
            db, study_uid, user=user, tenant_id=tenant_id, include_all=include_all
        )
    except view_state_service.ViewStateNotFound as exc:
        raise HTTPException(status_code=404, detail="Study not found") from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="No saved 3D view for this study")
    return JSONResponse(payload)


@router.put("/studies/{study_uid}/view-state")
async def put_view_state(
    study_uid: str,
    body: ViewStateBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_any),
) -> JSONResponse:
    """Insert or replace the caller's saved 3D view for this study."""
    tenant_id, include_all = tenant_scope(user)
    try:
        payload = await view_state_service.put_view_state(
            db, study_uid, user=user, tenant_id=tenant_id, include_all=include_all, state=body.state
        )
    except view_state_service.ViewStateNotFound as exc:
        raise HTTPException(status_code=404, detail="Study not found") from exc
    except view_state_service.ViewStateInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(payload)
