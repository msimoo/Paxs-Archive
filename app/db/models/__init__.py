"""ORM models mirroring the DICOM IOD hierarchy, plus supporting tables.

**This module is a facade.** Every entity is declared in exactly one domain
module and re-exported here, so `from app.db.models import Study` keeps working
for all 25+ call sites (and for Alembic's metadata) even though the class itself
now lives in `study.py`.

| module        | owns                                                        |
| ------------- | ----------------------------------------------------------- |
| `base.py`     | `Base`, the PK type, the timestamp/tenant mixins            |
| `patient.py`  | `Patient`, `AssigningAuthority`, `PatientIdentity`, …        |
| `study.py`    | `Study`, `Series`, `Instance`                                |
| `clinical.py` | reports, key images, dose, peer review, protocols, templates |
| `worklist.py` | MWL / MPPS / IAN rows, triage flags                          |
| `routing.py`  | DIMSE nodes, routing, compression, migration, prefetch queues |
| `ops.py`      | tenants, accounts, audit, settings, storage backends         |

Datetimes are naive UTC. DICOM dates are stored as YYYYMMDD strings to survive
messy real-world tags losslessly.

`__all__` at the bottom is the authoritative export list — `import *`, `pydoc`
and IDE completion read it, and `tests/test_model_base.py` asserts it covers
every mapped class that is reachable from this package.
"""

from __future__ import annotations

from app.db.models.base import (
    AutoBigIntPK,
    Base,
    TenantScopedMixin,
    TimestampMixin,
    pk,
)
from app.db.models.dictation import StudyDictation
from app.db.models.clinical import (
    ClinicalProtocol,
    CriticalResult,
    HangingProtocol,
    KeyImageNote,
    OrganizationSettings,
    PeerReviewCase,
    PresentationState,
    RadiationDoseReport,
    RadiologyReport,
    ReportAddendum,
    ReportTemplate,
    StudyViewState,
    XDSDocument,
)
from app.db.models.ops import (
    AuditLog,
    AuthAuditLog,
    AuthSource,
    OrphanedObject,
    RoutingQueueItem,
    RoutingRule,
    ServerSetting,
    Session,
    StorageBackendConfig,
    StorageNode,
    StorageQuota,
    Tenant,
    TenantAeTitle,
    User,
)
from app.db.models.patient import (
    AssigningAuthority,
    Patient,
    PatientIdentity,
    PatientIdentityConflict,
    PatientShare,
)
from app.db.models.routing import (
    CompressionJob,
    DicomNode,
    LosslessCompressionRule,
    LossyCompressionRule,
    MigrationJob,
    PrefetchJob,
    PrefetchRule,
    ReplicationRecord,
    StorageCommitRequest,
    StudyLifecycleRule,
    TransferJob,
)
from app.db.models.study import Instance, Series, Study
from app.db.models.worklist import (
    InstanceAvailabilityNotification,
    PerformedProcedureStep,
    ScheduledProcedureStep,
    TriageFlag,
)

# `Report` is a convenience alias for `RadiologyReport`; this schema has no
# separate report entity. Provided so `from app.db.models import Report`
# resolves. An amendment is a separate `ReportAddendum` row, so a signed
# original stays immutable.
Report = RadiologyReport

# Backward-compatible alias retained for existing imports. New models should
# use AutoBigIntPK directly or the pk() helper above.
BigIntPK = AutoBigIntPK


# --------------------------------------------------------------------------- #
# Authoritative export list.
#
# `tests/test_model_base.py::test_package_all_covers_every_model` fails if a
# mapped class is missing from this list, or if a name listed here does not
# resolve on the package.
# --------------------------------------------------------------------------- #
__all__ = [
    "StudyDictation",
    "AssigningAuthority",
    "AuditLog",
    "AuthAuditLog",
    "AuthSource",
    "AutoBigIntPK",
    "Base",
    "BigIntPK",
    "ClinicalProtocol",
    "CompressionJob",
    "CriticalResult",
    "DicomNode",
    "HangingProtocol",
    "Instance",
    "InstanceAvailabilityNotification",
    "KeyImageNote",
    "LosslessCompressionRule",
    "LossyCompressionRule",
    "MigrationJob",
    "OrganizationSettings",
    "OrphanedObject",
    "Patient",
    "PatientIdentity",
    "PatientIdentityConflict",
    "PatientShare",
    "PeerReviewCase",
    "PerformedProcedureStep",
    "PrefetchJob",
    "PrefetchRule",
    "PresentationState",
    "RadiationDoseReport",
    "RadiologyReport",
    "ReplicationRecord",
    "Report",
    "ReportAddendum",
    "ReportTemplate",
    "RoutingQueueItem",
    "RoutingRule",
    "ScheduledProcedureStep",
    "Series",
    "ServerSetting",
    "Session",
    "StorageBackendConfig",
    "StorageCommitRequest",
    "StorageNode",
    "StorageQuota",
    "Study",
    "StudyLifecycleRule",
    "Tenant",
    "TenantAeTitle",
    "TenantScopedMixin",
    "TimestampMixin",
    "TriageFlag",
    "TransferJob",
    "User",
    "XDSDocument",
    "pk",
]
