"""Research export: download a study as de-identified DICOM (ZIP).

Auth-gated (any logged-in user). Produces a zip where every instance has its
PHI stripped and its UIDs remapped consistently, per app/services/anonymizer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import AuditLog, Instance, Series, Study, User
from app.db.session import get_db
from app.api.v1.deps import get_current_user, require_study_access
from app.services import anonymizer

router = APIRouter(prefix="/api/v1", tags=["export"])


@router.get(
    "/export/{study_uid}/anonymized",
    responses={200: {"content": {"application/zip": {}}}},
    summary="Download a study as anonymized DICOM (ZIP)",
)
async def export_anonymized(
    study_uid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # C04: an export is a bulk read — resolve it through the tenant guard.
    study = await require_study_access(db, study_uid, user)

    instances = (
        await db.execute(
            select(Instance)
            .options(joinedload(Instance.series))
            .join(Series, Instance.series_id == Series.id)
            .where(Series.study_id == study.id)
            .order_by(Instance.id)
        )
    ).scalars().all()
    if not instances:
        raise HTTPException(404, "Study has no instances")

    paths = [i.file_path for i in instances]
    backends = {i.file_path: i.storage_backend for i in instances}
    try:
        data = anonymizer.anonymize_files_to_zip(
            paths, zip_name=f"{study_uid}.zip", backends=backends
        )
    except FileNotFoundError as exc:
        raise HTTPException(422, f"Missing DICOM file on disk: {exc}") from exc
    except Exception as exc:
        # Corrupt/unreadable DICOM (pydicom InvalidDicomError etc.) -> 422,
        # not a 500.
        raise HTTPException(422, f"Could not anonymize study: {exc}") from exc

    # Record who exported what (audit trail).
    db.add(AuditLog(
        study_instance_uid=study_uid,
        action="EXPORTED",
        performed_by=user.username,
    ))

    filename = f"paxs_anonymized_{study_uid}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
