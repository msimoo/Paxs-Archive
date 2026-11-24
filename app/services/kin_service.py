"""Key Image Notes (KIN / KO) Service.

Parses DICOM Key Object Selection Documents (SOP Class 1.2.840.10008.5.1.4.1.1.88.22),
synthesizes new DICOM KO objects, and indexes bookmarked image references
with radiologist notes for PACS viewer presentation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KeyImageNote
from app.services import audit_service, indexing_service, query_service, storage_service

log = logging.getLogger("paxs.kin")

KIN_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.88.22"

# Standard DICOM Title Concept Codes (DCM Code Scheme)
KIN_TITLE_MAP = {
    "113000": "Of Interest",
    "113001": "For Quality Control",
    "113002": "For Diagnostic Intent",
    "113003": "For Surgery",
    "113004": "For Teaching",
    "113005": "For Conference",
    "113030": "Manifestation",
}


@dataclass
class ParsedKINContent:
    title_code: str = "113000"
    title_text: str = "Of Interest"
    notes: str | None = None
    author_name: str | None = None
    referenced_sop_uids: list[str] = field(default_factory=list)


def _append_kin_item(content: ParsedKINContent, item: Dataset) -> None:
    """Merge one KIN content item into its parsed title/note/references."""
    val_type = str(getattr(item, "ValueType", "")).upper()
    if val_type == "TEXT":
        text = str(getattr(item, "TextValue", ""))
        if text:
            content.notes = text
    elif val_type == "IMAGE":
        ref_seq = getattr(item, "ReferencedSOPSequence", None)
        if ref_seq:
            for ref_item in ref_seq:
                uid = str(getattr(ref_item, "ReferencedSOPInstanceUID", ""))
                if uid and uid not in content.referenced_sop_uids:
                    content.referenced_sop_uids.append(uid)


def parse_kin(ds: Dataset) -> ParsedKINContent:
    """Extract concept titles, notes, and referenced SOP UIDs from a KO dataset."""
    content = ParsedKINContent()

    # Concept Name Code Sequence (0040,A043)
    cn_seq = getattr(ds, "ConceptNameCodeSequence", None)
    if cn_seq and len(cn_seq) > 0:
        item = cn_seq[0]
        code = str(getattr(item, "CodeValue", "113000"))
        meaning = str(getattr(item, "CodeMeaning", KIN_TITLE_MAP.get(code, "Of Interest")))
        content.title_code = code
        content.title_text = meaning

    # Author
    author = getattr(ds, "VerifyingObserverName", None) or getattr(ds, "OperatorsName", None)
    if author:
        content.author_name = str(author)

    # Content Sequence (0040,A730)
    seq = getattr(ds, "ContentSequence", None)
    if seq:
        for item in seq:
            _append_kin_item(content, item)

    # Fallback to dataset text if notes missing
    if not content.notes and hasattr(ds, "TextValue"):
        content.notes = str(ds.TextValue)

    return content


async def index_key_image_note(
    session: AsyncSession,
    meta: dict,
    ds: Dataset,
    file_path: str,
) -> KeyImageNote | None:
    """Index an incoming KO dataset into the database."""
    sop_uid = meta.get("sop_instance_uid") or ""
    if not sop_uid:
        return None

    content = parse_kin(ds)
    ref_uids_str = ",".join(content.referenced_sop_uids) if content.referenced_sop_uids else None
    study_uid = meta.get("study_instance_uid") or ""

    existing = (
        await session.execute(
            select(KeyImageNote).where(KeyImageNote.sop_instance_uid == sop_uid)
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.study_instance_uid != study_uid:
            raise ValueError("KIN SOP UID belongs to another study")
        existing.file_path = file_path
        existing.title_concept_code = content.title_code
        existing.title_concept_text = content.title_text
        existing.notes = content.notes
        existing.author_name = content.author_name
        existing.referenced_sop_instance_uids = ref_uids_str
        await session.flush()
        return existing

    kin = KeyImageNote(
        sop_instance_uid=sop_uid,
        study_instance_uid=study_uid,
        series_instance_uid=meta.get("series_instance_uid") or "",
        title_concept_code=content.title_code,
        title_concept_text=content.title_text,
        notes=content.notes,
        author_name=content.author_name,
        referenced_sop_instance_uids=ref_uids_str,
        file_path=file_path,
    )
    session.add(kin)
    await session.flush()

    audit_service.add_audit(
        session,
        sop_instance_uid=sop_uid,
        study_instance_uid=kin.study_instance_uid,
        action="KEY_IMAGE_NOTE_CREATED",
        performed_by=content.author_name or "radiologist",
    )

    log.info("Key Image Note indexed: %s (refs: %d images)", sop_uid, len(content.referenced_sop_uids))
    return kin


async def create_key_image_note(
    session: AsyncSession,
    study_instance_uid: str,
    series_instance_uid: str,
    referenced_sop_uids: list[str],
    notes: str | None = None,
    title_code: str = "113000",
    author_name: str = "radiologist",
    tenant_id: int | None = None,
    include_all: bool = False,
) -> KeyImageNote:
    """Synthesize a new DICOM KO dataset, save to storage, and index in DB."""
    sop_uid = generate_uid()
    title_text = KIN_TITLE_MAP.get(title_code, "Of Interest")

    # Build DICOM KO Dataset
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = KIN_SOP_CLASS_UID
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset("kin.dcm", {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = KIN_SOP_CLASS_UID
    ds.SOPInstanceUID = sop_uid
    ds.Modality = "KO"
    ds.StudyInstanceUID = study_instance_uid
    ds.SeriesInstanceUID = series_instance_uid
    ds.OperatorsName = author_name
    ds.VerifyingObserverName = author_name

    # Concept Name Code Sequence
    cn_item = Dataset()
    cn_item.CodeValue = title_code
    cn_item.CodingSchemeDesignator = "DCM"
    cn_item.CodeMeaning = title_text
    ds.ConceptNameCodeSequence = Sequence([cn_item])

    content_items = []
    if notes:
        text_item = Dataset()
        text_item.ValueType = "TEXT"
        text_item.TextValue = notes
        content_items.append(text_item)

    for ref_uid in referenced_sop_uids:
        img_item = Dataset()
        img_item.ValueType = "IMAGE"
        ref_sop = Dataset()
        ref_sop.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"  # CT default
        ref_sop.ReferencedSOPInstanceUID = ref_uid
        img_item.ReferencedSOPSequence = Sequence([ref_sop])
        content_items.append(img_item)

    ds.ContentSequence = Sequence(content_items)

    # Save to storage facade
    file_key, sha256 = storage_service.save_dataset(ds)

    meta = {
        "sop_instance_uid": sop_uid,
        "study_instance_uid": study_instance_uid,
        "series_instance_uid": series_instance_uid,
        "sop_class_uid": KIN_SOP_CLASS_UID,
        "modality": "KO",
    }

    # Index instance into Instance table
    await indexing_service.index_new_instance(
        session, meta, file_key, sha256, performed_by=author_name,
        tenant_id=tenant_id, include_all=include_all,
    )

    # Index into KeyImageNote table
    kin = await index_key_image_note(session, meta, ds, file_key)
    return kin or KeyImageNote(
        sop_instance_uid=sop_uid,
        study_instance_uid=study_instance_uid,
        series_instance_uid=series_instance_uid,
        title_concept_code=title_code,
        title_concept_text=title_text,
        notes=notes,
        author_name=author_name,
        referenced_sop_instance_uids=",".join(referenced_sop_uids),
        file_path=file_key,
    )


async def list_kin_for_study(
    session: AsyncSession,
    study_instance_uid: str,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[KeyImageNote]:
    """List all Key Image Notes in a study.

    B-9: scoped to the caller's tenant. `key_image_notes` has no tenant column,
    so the predicate is applied to the joined study; a note belonging to another
    institution is not returned (the router turns that into 404).
    """
    stmt = query_service.key_image_note_query(
        study_uid=study_instance_uid, tenant_id=tenant_id, include_all=include_all
    ).order_by(KeyImageNote.id.desc())
    return list((await session.execute(stmt)).scalars().all())


async def find_kin_for_instance(
    session: AsyncSession,
    sop_instance_uid: str,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> list[KeyImageNote]:
    """Find Key Image Notes that reference a specific image instance (B-9 scoped)."""
    stmt = query_service.key_image_note_query(
        instance_uid=sop_instance_uid, tenant_id=tenant_id, include_all=include_all
    ).order_by(KeyImageNote.id.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_kin_by_sop(
    session: AsyncSession,
    sop_instance_uid: str,
    *,
    tenant_id: int | None = None,
    include_all: bool = False,
) -> KeyImageNote | None:
    """One Key Image Note by its own SOPInstanceUID, or None (B-9 scoped).

    None means either "no such note" or "another tenant's note" — the router
    must not distinguish the two (a 403 would confirm the object exists).
    """
    stmt = query_service.key_image_note_query(
        sop_uid=sop_instance_uid, tenant_id=tenant_id, include_all=include_all
    )
    return (await session.execute(stmt)).scalar_one_or_none()
