"""Query builders shared between DICOMweb QIDO-RS (async sessions) and the
DICOM C-FIND SCP (sync sessions). Translates DICOM query semantics:
  * wildcard   "SMITH*" -> LIKE 'SMITH%'
  * date range "20240101-20240131" -> BETWEEN
  * exact value otherwise
"""

from __future__ import annotations

from sqlalchemy import Select, and_, exists, func, select
from sqlalchemy.orm import joinedload

from app.db.models import Instance, Patient, Series, Study

# --------------------------------------------------------------------------- #
# Filter translation
# --------------------------------------------------------------------------- #
STUDY_FILTERS = {
    "PatientName": (Patient.patient_name, "like"),
    "PatientID": (Patient.patient_id, "exact"),
    "PatientBirthDate": (Patient.patient_birth_date, "range"),
    "StudyDate": (Study.study_date, "range"),
    "StudyTime": (Study.study_time, "exact"),
    "AccessionNumber": (Study.accession_number, "like"),
    "StudyDescription": (Study.study_description, "like"),
    "StudyInstanceUID": (Study.study_instance_uid, "exact"),
    "ReferringPhysicianName": (Study.referring_physician, "like"),
    "StudyID": (Study.study_id, "exact"),
}

SERIES_FILTERS = {
    "Modality": (Series.modality, "exact"),
    "SeriesInstanceUID": (Series.series_instance_uid, "exact"),
    "SeriesNumber": (Series.series_number, "exact"),
    "SeriesDescription": (Series.series_description, "like"),
    "BodyPartExamined": (Series.body_part_examined, "like"),
}

INSTANCE_FILTERS = {
    "SOPInstanceUID": (Instance.sop_instance_uid, "exact"),
    "SOPClassUID": (Instance.sop_class_uid, "exact"),
    "InstanceNumber": (Instance.instance_number, "exact"),
}


def _pattern(value: str) -> str | None:
    if "*" in value:
        return value.replace("*", "%")
    return None


def _range(value: str) -> tuple[str | None, str | None] | None:
    v = value.strip()
    if "-" not in v:
        return None
    start, _, end = v.partition("-")
    if not start and not end:
        return None
    return (start or None, end or None)


def apply_study_filters(stmt: Select, filters: dict) -> Select:
    stmt = stmt.join(Patient, Study.patient_id == Patient.id)
    for keyword, raw in filters.items():
        if keyword == "ModalitiesInStudy":
            value = str(raw)
            pattern = _pattern(value)
            cond = Series.modality.like(pattern) if pattern else Series.modality == value
            stmt = stmt.where(
                exists().where(and_(Series.study_id == Study.id, cond))
            )
            continue
        entry = STUDY_FILTERS.get(keyword)
        if not entry:
            continue
        column, kind = entry
        stmt = _apply(stmt, column, kind, str(raw))
    return stmt


def apply_series_filters(stmt: Select, filters: dict, study_uid: str | None = None) -> Select:
    if study_uid:
        stmt = stmt.join(Study, Series.study_id == Study.id).where(
            Study.study_instance_uid == study_uid
        )
    for keyword, raw in filters.items():
        entry = SERIES_FILTERS.get(keyword)
        if not entry:
            continue
        column, kind = entry
        stmt = _apply(stmt, column, kind, str(raw))
    return stmt


def apply_instance_filters(stmt: Select, filters: dict,
                           study_uid: str | None = None,
                           series_uid: str | None = None) -> Select:
    if study_uid or series_uid:
        stmt = (
            stmt.join(Series, Instance.series_id == Series.id)
            .join(Study, Series.study_id == Study.id)
        )
        if study_uid:
            stmt = stmt.where(Study.study_instance_uid == study_uid)
        if series_uid:
            stmt = stmt.where(Series.series_instance_uid == series_uid)
    for keyword, raw in filters.items():
        entry = INSTANCE_FILTERS.get(keyword)
        if not entry:
            continue
        column, kind = entry
        stmt = _apply(stmt, column, kind, str(raw))
    return stmt


def _apply(stmt: Select, column, kind: str, value: str) -> Select:
    rng = _range(value) if kind == "range" else None
    if rng:
        start, end = rng
        if start and end:
            return stmt.where(column.between(start, end))
        if start:
            return stmt.where(column >= start)
        if end:
            return stmt.where(column <= end)
    pattern = _pattern(value)
    if pattern:
        return stmt.where(column.like(pattern))
    if kind == "like":
        # Fuzzy substring matching for name/description fields (PAXs
        # supportsFuzzyMatching, dcm4chee/Orthanc PN behaviour): a bare
        # "SMITH" matches any name containing SMITH. Exact values still hit
        # (a full-name query is just a substring of itself).
        return stmt.where(column.like(f"%{value}%"))
    return stmt.where(column == value)


# --------------------------------------------------------------------------- #
# VNA tenant scoping
#
# C04: `include_all` now defaults to **False** on every builder below. Tenant
# scoping is the default and archive-wide access is the explicit opt-in, so a
# caller that forgets the argument gets the safe behaviour instead of every
# institution's data.
# --------------------------------------------------------------------------- #
def apply_study_tenant_scope(stmt: Select, tenant_id: int | None = None,
                             include_all: bool = False) -> Select:
    """Scope a query that selects (directly or via join) `Study` rows to one
    tenant. `include_all=True` (admin) leaves the query untouched;
    `tenant_id=None` means the legacy unscoped data only
    (``Study.tenant_id IS NULL``); otherwise exact tenant match."""
    if include_all:
        return stmt
    if tenant_id is None:
        return stmt.where(Study.tenant_id.is_(None))
    return stmt.where(Study.tenant_id == tenant_id)


# --------------------------------------------------------------------------- #
# Convenience builders
# --------------------------------------------------------------------------- #
def study_query(filters: dict | None = None, tenant_id: int | None = None,
                include_all: bool = False) -> Select:
    stmt = select(Study).options(joinedload(Study.patient))
    stmt = apply_study_filters(stmt, filters or {})
    return apply_study_tenant_scope(stmt, tenant_id, include_all)


def series_query(study_uid: str | None = None, filters: dict | None = None,
                 tenant_id: int | None = None, include_all: bool = False) -> Select:
    if not include_all:
        # Scoped: join Study once (covers both the tenant filter and, when
        # given, the study-UID restriction).
        stmt = select(Series).join(Study, Series.study_id == Study.id)
        if study_uid:
            stmt = stmt.where(Study.study_instance_uid == study_uid)
        stmt = apply_series_filters(stmt, filters or {})
        return apply_study_tenant_scope(stmt, tenant_id, False)
    return apply_series_filters(select(Series), filters or {}, study_uid)


def instance_query(study_uid: str | None = None, series_uid: str | None = None,
                   filters: dict | None = None, tenant_id: int | None = None,
                   include_all: bool = False) -> Select:
    if not include_all:
        stmt = (
            select(Instance)
            .join(Series, Instance.series_id == Series.id)
            .join(Study, Series.study_id == Study.id)
        )
        if study_uid:
            stmt = stmt.where(Study.study_instance_uid == study_uid)
        if series_uid:
            stmt = stmt.where(Series.series_instance_uid == series_uid)
        stmt = apply_instance_filters(stmt, filters or {})
        return apply_study_tenant_scope(stmt, tenant_id, False)
    return apply_instance_filters(select(Instance), filters or {}, study_uid, series_uid)


# --------------------------------------------------------------------------- #
# Aggregates (modalities + counts per study, instance counts per series)
# --------------------------------------------------------------------------- #
async def study_aggregates(session, study_ids: list[int]) -> dict[int, dict]:
    """Return {study_id: {"modalities": [...], "series": n, "instances": n}}.

    One row per SERIES (grouped by Series.id) so that multiple series sharing
    the same modality are counted correctly."""
    if not study_ids:
        return {}
    result: dict[int, dict] = {sid: {"modalities": [], "series": 0, "instances": 0} for sid in study_ids}

    series_rows = (
        await session.execute(
            select(Series.study_id, Series.modality, func.count(Instance.id))
            .join(Instance, Instance.series_id == Series.id)
            .where(Series.study_id.in_(study_ids))
            .group_by(Series.id)
        )
    ).all()
    for study_id, modality, n in series_rows:
        result[study_id]["series"] += 1
        result[study_id]["instances"] += n
        if modality and modality not in result[study_id]["modalities"]:
            result[study_id]["modalities"].append(modality)
    return result


async def series_instance_counts(session, series_ids: list[int]) -> dict[int, int]:
    if not series_ids:
        return {}
    rows = (
        await session.execute(
            select(Series.id, func.count(Instance.id))
            .join(Instance, Instance.series_id == Series.id)
            .where(Series.id.in_(series_ids))
            .group_by(Series.id)
        )
    ).all()
    return {sid: n for sid, n in rows}
