"""Query builders shared between DICOMweb QIDO-RS (async sessions) and the
DICOM C-FIND SCP (sync sessions). Translates DICOM query semantics:
  * wildcard   "SMITH*" -> LIKE 'SMITH%'
  * date range "20240101-20240131" -> BETWEEN
  * exact value otherwise
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, overload

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, joinedload, undefer
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import (
    Instance,
    KeyImageNote,
    Patient,
    PresentationState,
    RadiationDoseReport,
    Series,
    Study,
)

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

# --------------------------------------------------------------------------- #
# QIDO-RS addresses attributes by *tag number*, not by keyword
#
# PS3.18 §6.7.1: a QIDO query parameter is the attribute tag in 8 hex digits,
# e.g. ``?00100020=BRAIN`` for PatientID. OHIF (and dcm4che, Weasis, every
# standard client) therefore never sends ``?PatientID=``. The filter tables
# above are keyed by keyword, so before this table a tag-number parameter fell
# through ``STUDY_FILTERS.get()`` and was **silently dropped** — which does not
# narrow the query, it widens it to the whole archive. That is how the viewer's
# study browser came to list another patient's study: it asks "give me the
# studies of PatientID X" and is answered with every study in the tenant.
#
# A filter we cannot honour must never be dropped. ``unsupported_query_filters``
# names the offending tags so the router can answer 400 instead of returning PHI.
# --------------------------------------------------------------------------- #
DICOM_TAG_ALIASES = {
    "00080016": "SOPClassUID",
    "00080018": "SOPInstanceUID",
    "00080020": "StudyDate",
    "00080030": "StudyTime",
    "00080050": "AccessionNumber",
    "00080060": "Modality",
    "00080061": "ModalitiesInStudy",
    "00080090": "ReferringPhysicianName",
    "00081030": "StudyDescription",
    "0008103E": "SeriesDescription",
    "00100010": "PatientName",
    "00100020": "PatientID",
    "00100030": "PatientBirthDate",
    "00180015": "BodyPartExamined",
    "0020000D": "StudyInstanceUID",
    "0020000E": "SeriesInstanceUID",
    "00200010": "StudyID",
    "00200011": "SeriesNumber",
    "00200013": "InstanceNumber",
}

# Query parameters that carry no attribute filter and are handled elsewhere
# (paging, projection, DICOM's own optional matching mode). They are not
# "unsupported filters" and must not be rejected.
QIDO_CONTROL_PARAMS = frozenset(
    {"limit", "offset", "includefield", "fuzzymatching", "cursor", "requestTag"}
)


def normalize_filter_key(key: str) -> str:
    """Map a QIDO tag number (``"00100020"``) to its keyword (``"PatientID"``).

    Keywords pass through untouched, so both spellings work — the C-FIND SCP
    and the internal callers use keywords, DICOMweb clients use tags.
    """
    return DICOM_TAG_ALIASES.get(str(key).strip().upper(), key)


def unsupported_query_filters(filters: dict, supported: dict | set | frozenset) -> list[str]:
    """Tag-number parameters this level cannot translate into a predicate.

    Only tags are reported: an unknown *keyword* is not a DICOMweb filter (the
    spec requires tags) and is ignored, but an unknown tag is a real filter the
    caller believes is being applied. Dropping it silently would answer with a
    superset of the requested rows, so the caller turns this into a 400.
    """
    allowed = set(supported) | {"ModalitiesInStudy", "Modality"}
    bad = []
    for key in filters:
        raw = str(key).strip()
        if raw.lower() in QIDO_CONTROL_PARAMS:
            continue
        if len(raw) != 8 or any(c not in "0123456789abcdefABCDEF" for c in raw):
            continue
        if normalize_filter_key(raw) not in allowed:
            bad.append(raw)
    return sorted(set(bad))


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
        # DICOMweb sends tag numbers; the tables are keyed by keyword.
        keyword = normalize_filter_key(keyword)
        if keyword in ("ModalitiesInStudy", "Modality"):
            # `00080060` (Modality) is a *series* attribute, but a client that
            # filters /studies by it means "studies containing that modality".
            value = str(raw)
            pattern = _pattern(value)
            cond = Series.modality.ilike(pattern) if pattern else Series.modality == value
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
        entry = SERIES_FILTERS.get(normalize_filter_key(keyword))
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
        entry = INSTANCE_FILTERS.get(normalize_filter_key(keyword))
        if not entry:
            continue
        column, kind = entry
        stmt = _apply(stmt, column, kind, str(raw))
    return stmt


def _apply(stmt: Select, column: InstrumentedAttribute[Any], kind: str,
           value: str) -> Select:
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
        return stmt.where(column.ilike(pattern))
    if kind == "like":
        # Fuzzy substring matching for name/description fields (PAXs
        # supportsFuzzyMatching, dcm4chee/Orthanc PN behaviour): a bare
        # "SMITH" matches any name containing SMITH. Exact values still hit
        # (a full-name query is just a substring of itself).
        return stmt.where(column.ilike(f"%{value}%"))
    return stmt.where(column == value)


# --------------------------------------------------------------------------- #
# VNA tenant scoping
#
# C04: `include_all` now defaults to **False** on every builder below. Tenant
# scoping is the default and archive-wide access is the explicit opt-in, so a
# caller that forgets the argument gets the safe behaviour instead of every
# institution's data.
# --------------------------------------------------------------------------- #
@overload
def tenant_predicate(column: InstrumentedAttribute[int | None],
                     tenant_id: int | None,
                     include_all: Literal[False] = False) -> ColumnElement[bool]: ...


@overload
def tenant_predicate(column: InstrumentedAttribute[int | None],
                     tenant_id: int | None,
                     include_all: Literal[True]) -> None: ...


@overload
def tenant_predicate(column: InstrumentedAttribute[int | None],
                     tenant_id: int | None,
                     include_all: bool) -> ColumnElement[bool] | None: ...


def tenant_predicate(column: InstrumentedAttribute[int | None],
                    tenant_id: int | None,
                    include_all: bool = False) -> ColumnElement[bool] | None:
    """**THE** tenant-isolation predicate (FA-1 / C04).

    Every tenant-scoped query in the codebase funnels through this function.
    Before it existed there were four competing implementations
    (``deps.tenant_scope``, ``fhir._tenant_predicate``,
    ``indexing_service._tenant_filter``, ``reading_worklist_service._scope_condition``)
    plus several hand-rolled inline comparisons — and divergence between them is
    exactly what produced the audited cross-tenant PHI leaks. There is now one
    rule, in one place:

    * ``include_all`` (ADMIN, or a share token scoped to one study)
      -> ``None`` — no restriction, every tenant is visible;
    * ``tenant_id`` given -> ``column == tenant_id``;
    * ``tenant_id`` is ``None`` -> ``column IS NULL``, the legacy/unscoped
      bucket, so a pre-VNA archive keeps working unchanged.

    Returns ``None`` when no restriction applies, so callers can skip the
    ``.where()`` entirely rather than add a tautology. The overloads make that
    precise: passing ``include_all=True`` (a literal) is the *only* way to get
    ``None``, so a default call is typed as a non-optional clause and can go
    straight into ``.where()``. A runtime ``bool`` still yields the union, and
    the caller must branch on it.

    Use with a *tenant-bearing column*: ``Study.tenant_id``, ``Patient.tenant_id``,
    ``PatientIdentity.tenant_id``, ...
    """
    if include_all:
        return None
    return column.is_(None) if tenant_id is None else column == tenant_id


def apply_tenant_scope(stmt: Select, column: InstrumentedAttribute[int | None],
                       tenant_id: int | None = None,
                       include_all: bool = False) -> Select:
    """Apply :func:`tenant_predicate` to ``stmt`` (no-op when unrestricted)."""
    cond = tenant_predicate(column, tenant_id, include_all)
    return stmt if cond is None else stmt.where(cond)


def apply_study_tenant_scope(stmt: Select, tenant_id: int | None = None,
                             include_all: bool = False) -> Select:
    """Scope Study rows and their Patient demographics to the same tenant.

    ``include_all=True`` (admin) leaves the query untouched. ``tenant_id=None``
    selects only legacy rows where both Study and Patient are unscoped; other
    callers must match both owner columns. An EXISTS predicate avoids adding a
    join to callers that already join Patient for filtering or serialization.
    """
    study_scope = tenant_predicate(Study.tenant_id, tenant_id, include_all)
    if study_scope is None:
        return stmt
    patient_scope = tenant_predicate(Patient.tenant_id, tenant_id, False)
    patient_belongs = select(Patient.id).where(
        Patient.id == Study.patient_id,
        patient_scope,
    ).correlate(Study).exists()
    return stmt.where(study_scope, patient_belongs)


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
# Secondary clinical tables (KIN / RDSR) — tenant scoping by study UID
#
# B-9: `key_image_notes` and `radiation_dose_reports` carry no `tenant_id` of
# their own; they key off `study_instance_uid`. Every read of them therefore has
# to reach the tenant through the *study* it belongs to, and before these
# builders existed neither the KIN nor the dose router joined `studies` at all —
# so any authenticated caller could read another institution's dose metrics and
# annotations by UID. These two builders are the only supported way to read
# those tables; the routers contain no SQL of their own.
# --------------------------------------------------------------------------- #
def apply_study_uid_tenant_scope(stmt: Select, entity: type[Any], uid_column: Any,
                                 tenant_id: int | None = None,
                                 include_all: bool = False) -> Select:
    """Scope a child table that carries ``study_instance_uid`` but no tenant.

    ``entity`` is the child table (``KeyImageNote`` / ``RadiationDoseReport``)
    and ``uid_column`` its ``study_instance_uid`` column; the predicate is
    applied to the **joined ``Study``**.

    The join is deliberately an **outer** join. A row whose study is not indexed
    at all has no tenant to compare against, and an outer join maps it to the
    legacy (NULL) bucket: a legacy/unscoped caller still sees it, while every
    tenant caller does not (fail closed). An inner join would instead hide those
    rows from the legacy scope — a silent data regression for pre-VNA archives.

    ``include_all=True`` (ADMIN, share token) leaves the query untouched, so no
    join is added either.
    """
    if include_all:
        return stmt
    stmt = stmt.select_from(entity).outerjoin(
        Study, Study.study_instance_uid == uid_column
    )
    if tenant_id is None:
        patient_scope = tenant_predicate(Patient.tenant_id, None, False)
        linked_patient = select(Patient.id).where(
            Patient.id == Study.patient_id,
            patient_scope,
        ).exists()
        return stmt.where(
            or_(
                Study.id.is_(None),
                and_(Study.tenant_id.is_(None), linked_patient),
            )
        )
    patient_scope = tenant_predicate(Patient.tenant_id, tenant_id, False)
    linked_patient = select(Patient.id).where(
        Patient.id == Study.patient_id,
        patient_scope,
    ).exists()
    return stmt.where(Study.tenant_id == tenant_id, linked_patient)


def key_image_note_query(*, study_uid: str | None = None,
                         sop_uid: str | None = None,
                         instance_uid: str | None = None,
                         tenant_id: int | None = None,
                         include_all: bool = False) -> Select:
    """Tenant-scoped ``KeyImageNote`` query (B-9).

    ``instance_uid`` matches notes whose referenced-image list contains it; it
    is a LIKE-style substring match, so it narrows the tenant-scoped set rather
    than replacing the scope.
    """
    stmt = select(KeyImageNote)
    if study_uid:
        stmt = stmt.where(KeyImageNote.study_instance_uid == study_uid)
    if sop_uid:
        stmt = stmt.where(KeyImageNote.sop_instance_uid == sop_uid)
    if instance_uid:
        stmt = stmt.where(KeyImageNote.referenced_sop_instance_uids.contains(instance_uid))
    return apply_study_uid_tenant_scope(
        stmt, KeyImageNote, KeyImageNote.study_instance_uid, tenant_id, include_all
    )


def dose_report_query(*, study_uid: str | None = None,
                      sop_uid: str | None = None,
                      patient_id: str | None = None,
                      tenant_id: int | None = None,
                      include_all: bool = False) -> Select:
    """Tenant-scoped ``RadiationDoseReport`` query (B-9).

    ``patient_id`` alone is not an isolation boundary — patient identifiers are
    unique per tenant, not globally (A-8) — so it is always combined with the
    tenant predicate.
    """
    stmt = select(RadiationDoseReport)
    if study_uid:
        stmt = stmt.where(RadiationDoseReport.study_instance_uid == study_uid)
    if sop_uid:
        stmt = stmt.where(RadiationDoseReport.sop_instance_uid == sop_uid)
    if patient_id:
        stmt = stmt.where(RadiationDoseReport.patient_id == patient_id)
    return apply_study_uid_tenant_scope(
        stmt, RadiationDoseReport, RadiationDoseReport.study_instance_uid,
        tenant_id, include_all,
    )


def dose_aggregate_query(columns: Sequence[Any], tenant_id: int | None = None,
                         include_all: bool = False) -> Select:
    """Tenant-scoped aggregate over ``radiation_dose_reports`` (B-9).

    Used by the cumulative-patient and archive-analytics endpoints, which are
    ``SUM``/``COUNT`` queries and therefore cannot go through
    :func:`dose_report_query`. Same join, same predicate — an ADMIN still sees
    the whole archive, a tenant user only their own dose metrics.
    """
    stmt = select(*columns)
    return apply_study_uid_tenant_scope(
        stmt, RadiationDoseReport, RadiationDoseReport.study_instance_uid,
        tenant_id, include_all,
    )


def instance_owning_study_uid_query(sop_instance_uid: str) -> Select:
    """The ``StudyInstanceUID`` that owns an instance, by SOPInstanceUID.

    Used to turn a by-instance lookup (KIN notes / GSPS presentations that
    reference an image) into a study access check. It selects only the study
    UID — the tenant decision is made by the caller through
    ``require_study_access`` — so it carries no predicate of its own and must
    never be used to return clinical data.
    """
    return (
        select(Study.study_instance_uid)
        .join(Series, Series.study_id == Study.id)
        .join(Instance, Instance.series_id == Series.id)
        .where(Instance.sop_instance_uid == sop_instance_uid)
    )


async def owning_study_uid(session: AsyncSession, sop_instance_uid: str) -> str | None:
    """Resolve an instance's study UID, or ``None`` when the instance is unknown.

    Companion executor for :func:`instance_owning_study_uid_query`, kept here so
    the by-instance routers (``kin.py``, ``gsps.py``) hold no SQL statement of
    their own — B-9's Definition of Done requires exactly that, and the lookup
    was previously copy-pasted into both.

    ``None`` means the instance is not indexed; it says nothing about tenancy,
    and a caller that gets ``None`` must fall back to its tenant-scoped query
    rather than treat the row as readable.
    """
    return (
        await session.execute(instance_owning_study_uid_query(sop_instance_uid))
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# DICOMweb page executors (Q-3)
#
# ``api/v1/dicomweb.py`` used to build and run every QIDO/WADO statement itself.
# The builders above already knew how to filter and scope; these executors add
# the paging/ordering and run them, so the router only maps rows to DICOM JSON.
# --------------------------------------------------------------------------- #
#: QIDO studies order: dated studies first (newest first), NULL dates last,
#: ``id`` as the stable tiebreak so keyset paging cannot skip or repeat a row.
STUDY_ORDER = (Study.study_date.is_not(None).desc(), Study.study_date.desc(),
               Study.id.desc())
SERIES_ORDER = (Series.series_number.is_not(None).desc(), Series.series_number,
                Series.id)
INSTANCE_ORDER = (Instance.instance_number.is_not(None).desc(),
                  Instance.instance_number, Instance.id)


def keyset_predicate(study_date: str | None, cursor_id: int) -> ColumnElement[bool]:
    """WHERE clause for rows *after* a ``(study_date, id)`` cursor.

    Rows after the cursor in :data:`STUDY_ORDER` are either undated (they sort
    last) or dated older than the cursor, or share its date with a smaller id.
    After an undated cursor only later undated rows remain.
    """
    if study_date is None:
        return and_(Study.study_date.is_(None), Study.id < cursor_id)
    return or_(
        Study.study_date.is_(None),
        Study.study_date < study_date,
        and_(Study.study_date == study_date, Study.id < cursor_id),
    )


async def qido_study_page(
    session: AsyncSession,
    filters: dict,
    *,
    tenant_id: int | None,
    include_all: bool,
    limit: int,
    offset: int,
    cursor: tuple[str | None, int] | None = None,
) -> tuple[list[Study], bool]:
    """One QIDO studies page plus "is there a row after it".

    ``has_more`` is probed one row past the window rather than inferred from a
    full page: a page that happens to be exactly ``limit`` rows long is not
    evidence of a next page, and advertising a cursor that returns nothing
    makes the client render an empty final page.
    """
    base = study_query(filters, tenant_id=tenant_id, include_all=include_all)
    if cursor is not None:
        base = base.where(keyset_predicate(*cursor))
    page = await session.execute(
        base.order_by(*STUDY_ORDER).limit(limit).offset(offset)
    )
    studies = list(page.scalars().unique().all())
    if len(studies) < limit or not studies:
        return studies, False
    probe = await session.execute(
        base.order_by(*STUDY_ORDER).limit(1).offset(offset + limit)
    )
    return studies, probe.first() is not None


async def qido_series_page(
    session: AsyncSession,
    study_uid: str | None,
    filters: dict,
    *,
    tenant_id: int | None,
    include_all: bool,
    limit: int,
    offset: int,
) -> list[Series]:
    """One QIDO series page, ordered by series number."""
    stmt = (
        series_query(study_uid, filters, tenant_id=tenant_id, include_all=include_all)
        .order_by(*SERIES_ORDER)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def qido_instance_page(
    session: AsyncSession,
    study_uid: str | None,
    series_uid: str | None,
    filters: dict,
    *,
    tenant_id: int | None,
    include_all: bool,
    limit: int,
    offset: int,
) -> list[Instance]:
    """One QIDO instances page, ordered by instance number."""
    stmt = (
        instance_query(study_uid, series_uid, filters,
                       tenant_id=tenant_id, include_all=include_all)
        .order_by(*INSTANCE_ORDER)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def instances_of(
    session: AsyncSession,
    study_uid: str,
    series_uid: str | None = None,
    *,
    tenant_id: int | None,
    include_all: bool,
    with_metadata: bool = False,
) -> list[Instance]:
    """Every instance of a (tenant-scoped) study or series, unordered.

    ``with_metadata`` undefer()s ``Instance.metadata_json`` — the WADO-RS
    /metadata route is the only caller that wants it. It is ~12 KB per row and
    deferred by default, so a frame fetch or a delete plan does not pull the
    study's whole metadata payload along with the rows it actually needs.
    """
    stmt = instance_query(study_uid, series_uid, tenant_id=tenant_id,
                          include_all=include_all)
    if with_metadata:
        stmt = stmt.options(undefer(Instance.metadata_json))
    return list((await session.execute(stmt)).scalars().all())


async def instance_stamp(
    session: AsyncSession,
    study_uid: str,
    series_uid: str | None = None,
    *,
    tenant_id: int | None,
    include_all: bool,
) -> tuple[int, int, int, int]:
    """``(count, sum(id), max(id), sum(content_version))`` of the scoped rows.

    Freshness stamp for the cached WADO-RS /metadata body: any instance added,
    removed or rewritten (C-04 bumps ``content_version``) changes it. Same
    tenant scope as :func:`instances_of`.
    """
    stmt = instance_query(study_uid, series_uid, tenant_id=tenant_id,
                          include_all=include_all).with_only_columns(
        func.count(Instance.id),
        func.coalesce(func.sum(Instance.id), 0),
        func.coalesce(func.max(Instance.id), 0),
        func.coalesce(func.sum(Instance.content_version), 0),
    )
    row = (await session.execute(stmt)).one()
    return tuple(int(value or 0) for value in row)


async def series_identities(
    session: AsyncSession, series_ids: set[int],
) -> dict[int, tuple[str, str, str | None]]:
    """series.id -> (StudyInstanceUID, SeriesInstanceUID, Modality).

    For the WADO-RS metadata fallback only. The ids come from instances the
    caller already fetched through a tenant-scoped query, so no further scope
    is applied here (and an empty set issues no query).
    """
    if not series_ids:
        return {}
    stmt = (
        select(Series.id, Study.study_instance_uid, Series.series_instance_uid, Series.modality)
        .join(Study, Series.study_id == Study.id)
        .where(Series.id.in_(series_ids))
    )
    rows = (await session.execute(stmt)).all()
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


async def find_instance(
    session: AsyncSession,
    study_uid: str | None,
    series_uid: str | None,
    sop_uid: str,
    *,
    tenant_id: int | None,
    include_all: bool,
) -> Instance | None:
    """One instance by SOP UID inside the caller's scope, or None."""
    return (
        await session.execute(
            instance_query(study_uid, series_uid, {"SOPInstanceUID": sop_uid},
                           tenant_id=tenant_id, include_all=include_all)
        )
    ).scalar_one_or_none()


async def study_exists(session: AsyncSession, study_uid: str) -> bool:
    """True when the study UID is indexed (STOW-RS' new-study probe)."""
    return (await session.execute(
        select(Study.id).where(Study.study_instance_uid == study_uid).limit(1)
    )).scalar_one_or_none() is not None


async def presentation_state_by_sop_uid(session: AsyncSession,
                                        sop_uid: str) -> PresentationState | None:
    """The GSPS object stored under ``sop_uid``, or None.

    Deliberately unscoped: the caller is rendering an instance it has already
    access-checked, and the presentation state only alters *that* image.
    """
    return (await session.execute(
        select(PresentationState).where(
            PresentationState.sop_instance_uid == sop_uid
        )
    )).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Aggregates (modalities + counts per study, instance counts per series)
# --------------------------------------------------------------------------- #
async def study_aggregates(session: AsyncSession,
                           study_ids: list[int]) -> dict[int, dict]:
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


async def series_instance_counts(session: AsyncSession,
                                 series_ids: list[int]) -> dict[int, int]:
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
    return dict(rows)
