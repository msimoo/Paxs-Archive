"""Instance Availability Notification (IAN) Service.

Sends DICOM N-CREATE DIMSE notifications (SOP Class 1.2.840.10008.5.1.4.33)
to listening RIS/EMR AE nodes when new studies finish arriving at PAXS.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import pydicom
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, UID, generate_uid
from pynetdicom import AE
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db.models import DicomNode, InstanceAvailabilityNotification, Series, Study
from app.db.session import AsyncSessionLocal, SyncSessionLocal
from app.services import audit_service, query_service
from app.services.node_service import get_node_sync

log = logging.getLogger("paxs.ian")

IAN_SOP_CLASS_UID = "1.2.840.10008.5.1.4.33"


def build_ian_dataset(
    study_instance_uid: str,
    series_structure: list[dict[str, Any]],
    availability: str = "ONLINE",
) -> Dataset:
    """Build a DICOM N-CREATE Instance Availability Notification Dataset."""
    ds = Dataset()
    ds.SOPClassUID = IAN_SOP_CLASS_UID
    ds.SOPInstanceUID = generate_uid()
    ds.StudyInstanceUID = study_instance_uid

    ref_series_seq = []
    for s_info in series_structure:
        s_item = Dataset()
        s_item.SeriesInstanceUID = s_info["series_instance_uid"]

        ref_sop_seq = []
        for inst in s_info.get("instances", []):
            sop_item = Dataset()
            sop_item.ReferencedSOPClassUID = inst.get("sop_class_uid", "1.2.840.10008.5.1.4.1.1.2")
            sop_item.ReferencedSOPInstanceUID = inst["sop_instance_uid"]
            sop_item.InstanceAvailability = availability
            ref_sop_seq.append(sop_item)

        s_item.ReferencedSOPSequence = Sequence(ref_sop_seq)
        ref_series_seq.append(s_item)

    ds.ReferencedSeriesSequence = Sequence(ref_series_seq)
    return ds


def send_ian_notification_sync(
    host: str,
    port: int,
    ae_title: str,
    ian_ds: Dataset,
    our_ae_title: str = "PAXS_SCU",
) -> dict[str, Any]:
    """Send N-CREATE DICOM IAN notification to remote AE node."""
    ian_sop_uid = UID(IAN_SOP_CLASS_UID)
    ae = AE(ae_title=our_ae_title)
    ae.add_requested_context(ian_sop_uid, ExplicitVRLittleEndian)

    assoc = ae.associate(host, port, ae_title=ae_title)
    if not assoc.is_established:
        assoc.release()
        return {"ok": False, "reason": "Association rejected by remote AE"}

    results = {"ok": False, "status": None}
    try:
        status = assoc.send_n_create(
            ian_ds,
            sop_class=ian_sop_uid,
            sop_instance=ian_ds.SOPInstanceUID,
            msg_id=1,
        )
        code = int(status.Status) if status is not None else None
        results["status"] = code
        results["ok"] = (code == 0x0000)
    except Exception as exc:  # noqa: BLE001 - boundary guard: the caller reads the failure from "reason"
        # IAN delivery is best-effort: a refused/failed N-CREATE must not raise
        # into the finalize path, but it must be visible in the log.
        log.warning("ian_send_failed error_type=%s", type(exc).__name__)
        results["reason"] = "IAN N-CREATE failed"
    finally:
        assoc.release()

    return results


async def notify_study_available(
    session: AsyncSession,
    study_instance_uid: str,
    target_ae_title: str | None = None,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[InstanceAvailabilityNotification]:
    """Build IAN payload and send N-CREATE notifications to registered RIS nodes."""
    # 1. Fetch study + series + instances
    statement = (
        select(Study)
        .options(joinedload(Study.series_list).joinedload(Series.instances))
        .where(Study.study_instance_uid == study_instance_uid)
    )
    statement = query_service.apply_study_tenant_scope(statement, tenant_id, include_all)
    study = (await session.execute(statement)).scalars().unique().one_or_none()

    if not study:
        log.warning("Cannot send IAN: Study %s not found", study_instance_uid)
        return []

    # 2. Extract series structure
    series_structure = []
    for s in study.series_list:
        inst_list = [
            {
                "sop_instance_uid": inst.sop_instance_uid,
                "sop_class_uid": inst.sop_class_uid or "1.2.840.10008.5.1.4.1.1.2",
            }
            for inst in s.instances
        ]
        series_structure.append(
            {
                "series_instance_uid": s.series_instance_uid,
                "instances": inst_list,
            }
        )

    # 3. Resolve target nodes
    nodes: list[DicomNode] = []
    if target_ae_title:
        node = (
            await session.execute(
                select(DicomNode).where(
                    DicomNode.ae_title == target_ae_title, DicomNode.enabled.is_(True)
                )
            )
        ).scalar_one_or_none()
        if node:
            nodes.append(node)
    else:
        # Default: notify all registered enabled nodes
        nodes = list(
            (
                await session.execute(
                    select(DicomNode).where(DicomNode.enabled.is_(True))
                )
            ).scalars().all()
        )

    if not nodes:
        log.debug("No enabled DICOM nodes registered for IAN notification")
        return []

    ian_ds = build_ian_dataset(study_instance_uid, series_structure, availability="ONLINE")
    payload_dict = {
        "study_instance_uid": study_instance_uid,
        "sop_instance_uid": ian_ds.SOPInstanceUID,
        "availability": "ONLINE",
        "num_series": len(series_structure),
    }

    notification_records = []
    loop = asyncio.get_running_loop()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for node in nodes:
        notif = InstanceAvailabilityNotification(
            study_instance_uid=study_instance_uid,
            target_ae_title=node.ae_title,
            instance_availability="ONLINE",
            status="PENDING",
            payload_json=json.dumps(payload_dict),
            created_at=now,
            updated_at=now,
        )
        session.add(notif)
        await session.flush()

        # Send N-CREATE via threadpool executor
        res = await loop.run_in_executor(
            None,
            send_ian_notification_sync,
            node.host,
            node.port,
            node.ae_title,
            ian_ds,
            settings.DICOM_AE_TITLE,
        )

        if res.get("ok"):
            notif.status = "SENT"
            notif.sent_at = now
            audit_service.add_audit(
                session,
                sop_instance_uid=None,
                study_instance_uid=study_instance_uid,
                action="IAN_NOTIFICATION_SENT",
                performed_by=f"ian:{node.ae_title}",
            )
        else:
            notif.status = "FAILED"
            failure_status = res.get("status")
            notif.error_message = (
                f"IAN N-CREATE failed (status=0x{failure_status:04X})"
                if isinstance(failure_status, int)
                else "IAN N-CREATE failed"
            )

        notification_records.append(notif)

    await session.flush()
    return notification_records


async def list_notifications(
    session: AsyncSession,
    study_instance_uid: str | None = None,
    status: str | None = None,
    limit: int = 100,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[InstanceAvailabilityNotification]:
    """List IAN notification logs."""
    stmt = (
        select(InstanceAvailabilityNotification)
        .join(
            Study,
            Study.study_instance_uid == InstanceAvailabilityNotification.study_instance_uid,
        )
    )
    if study_instance_uid:
        stmt = stmt.where(InstanceAvailabilityNotification.study_instance_uid == study_instance_uid)
    if status:
        stmt = stmt.where(InstanceAvailabilityNotification.status == status.upper())
    stmt = query_service.apply_study_tenant_scope(stmt, tenant_id, include_all)
    stmt = stmt.order_by(InstanceAvailabilityNotification.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def get_notification(
    session: AsyncSession, notification_id: int, *,
    tenant_id: int | None = None, include_all: bool = False,
) -> InstanceAvailabilityNotification | None:
    """Fetch one IAN record only through its owning study's tenant boundary."""
    statement = (
        select(InstanceAvailabilityNotification)
        .join(
            Study,
            Study.study_instance_uid == InstanceAvailabilityNotification.study_instance_uid,
        )
        .where(InstanceAvailabilityNotification.id == notification_id)
    )
    statement = query_service.apply_study_tenant_scope(statement, tenant_id, include_all)
    return (await session.execute(statement)).scalar_one_or_none()
