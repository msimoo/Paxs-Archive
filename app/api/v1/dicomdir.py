"""DICOMDIR Media Export REST API.

  POST /api/v1/export/dicomdir         — Download standard DICOMDIR ZIP archive for CD/DVD media
  POST /api/v1/export/dicomdir/preview — Preview DICOMDIR catalog structure without binary download
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.v1.deps import get_current_user, require_study_access
from app.db.models import Series, Study, User
from app.db.session import get_db
from app.services import dicomdir_service

log = logging.getLogger("paxs.api.dicomdir")

router = APIRouter(prefix="/api/v1/export/dicomdir", tags=["DICOMDIR Media Export"])


class ExportDICOMDIRRequest(BaseModel):
    study_instance_uids: list[str] = Field(..., description="List of StudyInstanceUIDs to package into DICOMDIR export")


@router.post("", response_class=Response)
async def export_dicomdir_package(
    req: ExportDICOMDIRRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate and download a standard DICOMDIR export ZIP archive (contains DICOMDIR file + IMAGES/)."""
    if not req.study_instance_uids:
        raise HTTPException(400, "study_instance_uids list cannot be empty")
    # C04: a bulk package must not mix in another institution's studies.
    for _uid in req.study_instance_uids:
        await require_study_access(db, _uid, user)

    try:
        zip_bytes, filename = await dicomdir_service.export_dicomdir_package(
            db, req.study_instance_uids, performed_by=user.username
        )
        await db.commit()

        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as val_err:
        raise HTTPException(404, str(val_err)) from val_err
    except Exception as exc:
        log.exception("Failed to generate DICOMDIR export package")
        raise HTTPException(500, f"DICOMDIR export failed: {exc}") from exc


@router.post("/preview", response_class=JSONResponse)
async def preview_dicomdir_structure(
    req: ExportDICOMDIRRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Preview the DICOMDIR record hierarchy structure (PATIENT -> STUDY -> SERIES -> IMAGE)."""
    if not req.study_instance_uids:
        raise HTTPException(400, "study_instance_uids list cannot be empty")
    # C04
    for _uid in req.study_instance_uids:
        await require_study_access(db, _uid, user)

    stmt = (
        select(Study)
        .options(
            joinedload(Study.patient),
            joinedload(Study.series_list).joinedload(Series.instances),
        )
        .where(Study.study_instance_uid.in_(req.study_instance_uids))
    )
    studies = list((await db.execute(stmt)).scalars().unique().all())
    if not studies:
        raise HTTPException(404, "No matching studies found")

    studies_structure = []
    img_counter = 1
    for st in studies:
        st_data = {
            "patient_id": st.patient.patient_id if st.patient else "UNKNOWN",
            "patient_name": st.patient.patient_name if st.patient else "UNKNOWN",
            "study_instance_uid": st.study_instance_uid,
            "series_count": len(st.series_list),
            "series_list": [],
        }
        for ser in st.series_list:
            ser_data = {
                "series_instance_uid": ser.series_instance_uid,
                "modality": ser.modality,
                "image_count": len(ser.instances),
                "instances": [
                    {
                        "sop_instance_uid": inst.sop_instance_uid,
                        "relative_file_id": ["IMAGES", f"IMG{img_counter + idx:05d}"],
                    }
                    for idx, inst in enumerate(ser.instances)
                ],
            }
            img_counter += len(ser.instances)
            st_data["series_list"].append(ser_data)
        studies_structure.append(st_data)

    ds = dicomdir_service.create_dicomdir_dataset(studies_structure)
    records_count = len(ds.DirectoryRecordSequence) if hasattr(ds, "DirectoryRecordSequence") else 0

    return JSONResponse(
        {
            "sop_class_uid": ds.file_meta.MediaStorageSOPClassUID,
            "total_directory_records": records_count,
            "studies": studies_structure,
        }
    )
