"""Lossy Compression Engine Service.

Manages rules and transcoding operations for aging diagnostic imaging archives,
reducing storage costs via lossy JPEG / JPEG-LS compression policies.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np
from PIL import Image
import pydicom
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import Instance, LossyCompressionRule, Series, Study
from app.services import audit_service, storage_service

log = logging.getLogger("paxs.lossy_compression")

# Standard Lossy Transfer Syntaxes
JPEG_BASELINE_LOSSY = "1.2.840.10008.1.2.4.50"
JPEG_LS_LOSSY = "1.2.840.10008.1.2.4.81"


def transcode_dataset_lossy(
    ds: pydicom.Dataset,
    target_ts_uid: str = JPEG_BASELINE_LOSSY,
    quality_factor: int = 85,
) -> tuple[pydicom.Dataset, int]:
    """Transcode a pydicom Dataset's pixel array using lossy compression."""
    if not hasattr(ds, "PixelData"):
        return ds, 0

    from pydicom.encaps import encapsulate
    from app.services.imaging import dataset_to_image

    orig_size = len(ds.PixelData)
    img = dataset_to_image(ds)

    buf = io.BytesIO()
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=quality_factor)
    compressed_bytes = buf.getvalue()

    # Encapsulate JPEG stream for DICOM Pixel Data
    ds.PixelData = encapsulate([compressed_bytes])
    ds.PhotometricInterpretation = "YBR_FULL_422" if target_ts_uid == JPEG_BASELINE_LOSSY else "RGB"
    ds.SamplesPerPixel = 3
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.Rows = img.height
    ds.Columns = img.width

    if ds.file_meta is not None:
        ds.file_meta.TransferSyntaxUID = target_ts_uid

    return ds, len(compressed_bytes)


async def transcode_instance_lossy(
    session: AsyncSession,
    instance: Instance,
    target_ts_uid: str = JPEG_BASELINE_LOSSY,
    quality_factor: int = 85,
) -> bool:
    """Transcode a single stored instance to lossy compression in backend."""
    bk = getattr(instance, "storage_backend", None)
    if not storage_service.exists(instance.file_path, backend_name=bk):
        return False

    try:
        ds = storage_service.read_dataset(instance.file_path, backend_name=bk)
        orig_size = instance.file_size_bytes or storage_service.size(instance.file_path, backend_name=bk)

        new_ds, new_size = transcode_dataset_lossy(ds, target_ts_uid, quality_factor)

        # Save back to storage on the same backend.
        storage_service.save_dataset(new_ds, backend_name=bk)

        # Update instance attributes in DB
        instance.transfer_syntax_uid = target_ts_uid
        instance.file_size_bytes = storage_service.size(instance.file_path, backend_name=bk)
        instance.photometric_interpretation = new_ds.PhotometricInterpretation
        instance.bits_allocated = 8
        await session.flush()

        # Audit log
        study_uid = instance.series.study.study_instance_uid if (instance.series and instance.series.study) else None
        audit_service.add_audit(
            session,
            sop_instance_uid=instance.sop_instance_uid,
            study_instance_uid=study_uid,
            action="LOSSY_COMPRESSED",
            performed_by=f"lossy-compression-engine:Q{quality_factor}",
        )

        log.info(
            "Lossy compressed instance %s (Q=%d): %d -> %d bytes",
            instance.sop_instance_uid,
            quality_factor,
            orig_size,
            instance.file_size_bytes,
        )
        return True
    except Exception as exc:
        log.exception("Lossy compression failed for %s", instance.sop_instance_uid)
        return False


async def evaluate_and_run_lossy_compression(
    session: AsyncSession,
    rule_id: int | None = None,
) -> dict[str, Any]:
    """Scan stored studies, evaluate rules, and transcode eligible instances."""
    stmt = select(LossyCompressionRule).where(LossyCompressionRule.enabled == True)  # noqa: E712
    if rule_id:
        stmt = stmt.where(LossyCompressionRule.id == rule_id)
    stmt = stmt.order_by(LossyCompressionRule.priority.asc(), LossyCompressionRule.id.asc())

    rules = (await session.execute(stmt)).scalars().all()
    if not rules:
        return {"processed": 0}

    # Query all instances with eager loaded series and study
    inst_stmt = (
        select(Instance)
        .options(joinedload(Instance.series).joinedload(Series.study))
        .where(Instance.transfer_syntax_uid != JPEG_BASELINE_LOSSY)
    )
    instances = (await session.execute(inst_stmt)).scalars().unique().all()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    processed_count = 0

    for inst in instances:
        study = inst.series.study if inst.series else None
        if not study:
            continue

        study_date_str = study.study_date
        age_days = 0
        if study_date_str and len(study_date_str) == 8 and study_date_str.isdigit():
            try:
                st_dt = datetime.strptime(study_date_str, "%Y%m%d")
                age_days = (now - st_dt).days
            except ValueError:
                age_days = (now - study.created_at).days if study.created_at else 0
        elif study.created_at:
            age_days = (now - study.created_at).days

        for rule in rules:
            if age_days < rule.min_age_days:
                continue

            if rule.modality and inst.series.modality:
                if rule.modality.upper() != inst.series.modality.upper():
                    continue

            if rule.body_part_examined and inst.series.body_part_examined:
                if rule.body_part_examined.upper() != inst.series.body_part_examined.upper():
                    continue

            ok = await transcode_instance_lossy(
                session, inst, rule.transfer_syntax_uid, rule.quality_factor
            )
            if ok:
                processed_count += 1
            break

    await session.flush()
    return {"processed": processed_count}


# CRUD Helpers
async def create_rule(session: AsyncSession, data: dict[str, Any]) -> LossyCompressionRule:
    rule = LossyCompressionRule(**data)
    session.add(rule)
    await session.flush()
    # Materialize server_default columns (created_at/updated_at) eagerly —
    # MySQL/MariaDB lack RETURNING, so lazy access later raises MissingGreenlet.
    await session.refresh(rule)
    return rule


async def get_rule(session: AsyncSession, rule_id: int) -> LossyCompressionRule | None:
    return (
        await session.execute(
            select(LossyCompressionRule).where(LossyCompressionRule.id == rule_id)
        )
    ).scalar_one_or_none()


async def list_rules(session: AsyncSession) -> list[LossyCompressionRule]:
    return list(
        (
            await session.execute(
                select(LossyCompressionRule).order_by(
                    LossyCompressionRule.priority.asc(), LossyCompressionRule.id.asc()
                )
            )
        ).scalars().all()
    )


async def delete_rule(session: AsyncSession, rule_id: int) -> bool:
    rule = await get_rule(session, rule_id)
    if not rule:
        return False
    await session.delete(rule)
    await session.flush()
    return True
