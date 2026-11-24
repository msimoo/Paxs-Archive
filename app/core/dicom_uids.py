"""A-17 — mandatory DICOM Type 1 UID validation.

``StudyInstanceUID`` (0020,000D), ``SeriesInstanceUID`` (0020,000E) and
``SOPInstanceUID`` (0008,0018) are Type 1: a conformant dataset always carries
them, and the archive's entire identity model is built on them.

When one was absent the ingest path substituted a *constant* sentinel — the
literal string ``"UNKNOWN"`` in the indexer and ``sha1("")`` in the storage key
builder. Both are the same value for every dataset, so every UID-less instance
from every patient, modality and institution collapsed into one bucket. Two
unrelated examinations silently merged and a radiologist could read one
patient's images under another patient's study. That is a diagnostic-safety
defect, not a data-quality nit, which is why this module **rejects** rather than
repairs.

Three rules, applied in order:

1. **Present and non-blank** — always fatal. A dataset that omits a Type 1 UID
   is non-conformant and cannot be filed safely.
2. **Not a shared placeholder** — always fatal. ``"UNKNOWN"``, ``"NO_UID"`` and
   friends are values *many* datasets carry, so accepting one recreates exactly
   the collision this module exists to prevent.
3. **PS3.5 UID grammar** — ``^[0-9]+(\\.[0-9]+)*$``, at most 64 characters.
   Fatal by default, relaxable via ``DICOM_UID_STRICT_FORMAT``.

Rules 1 and 2 have no escape hatch — they *are* the defect. Rule 3 is a policy
choice with a real trade-off: rejecting an odd-but-stable identifier loses a
patient's images, while accepting it costs nothing, because the value is still
unique and the archive will echo it back faithfully. The default follows the
standard; the setting exists so an operator whose legacy archive contains
non-conformant UIDs can keep ingesting, with a warning naming each offender
rather than a silent rejection.

Call :func:`validate_mandatory_uids` **before** anything is staged, indexed or
written — that ordering is what makes "nothing is left behind" true.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.core.config import settings

log = logging.getLogger("paxs.dicom_uids")

# The three mandatory Type 1 UIDs as (attribute keyword, DICOM tag), in IOD
# hierarchy order — which is also the order a failure should be reported in, so
# a dataset missing all three always names the study first.
MANDATORY_UIDS: tuple[tuple[str, str], ...] = (
    ("StudyInstanceUID", "(0020,000D)"),
    ("SeriesInstanceUID", "(0020,000E)"),
    ("SOPInstanceUID", "(0008,0018)"),
)

# PS3.5 A.1: a UID is one or more numeric components separated by dots. The
# leading-digit requirement also rejects every path-dangerous value (``..``,
# ``a/b``, ``C:evil``, ``CON``) for free, which is why the format rule runs
# before the storage-key check.
UID_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)*$")

# PS3.5 caps a UID at 64 bytes.
MAX_UID_LENGTH = 64

# Values that name "no identifier" rather than an identifier. Each is shared by
# every dataset that carries it, so accepting one recreates A-17 exactly.
# Compared case-insensitively after stripping surrounding whitespace.
#
# ``"0"`` and ``"0.0"`` are included deliberately: they are syntactically valid
# UIDs, but they are what a broken sender emits when it has no identifier, and
# every such dataset would merge. No conformant modality uses them.
PLACEHOLDER_UIDS = frozenset({
    "unknown", "no_uid", "nouid", "noid", "none", "null", "nil", "na", "n/a",
    "undefined", "unset", "missing", "placeholder", "tbd", "0", "0.0",
})

# DICOM JSON element tags used to build a STOW-RS failure dataset.
_TAG_REF_SOP_CLASS = "00081150"      # (0008,1150) ReferencedSOPClassUID
_TAG_REF_SOP_INSTANCE = "00081155"   # (0008,1155) ReferencedSOPInstanceUID
_TAG_FAILURE_REASON = "00081197"     # (0008,1197) FailureReason

# A neutral SOP Class UID for a failure item whose own class is unknown.
_UNKNOWN_SOP_CLASS = "1.2.840.10008.1.1"

# PS3.4 C.7.6.1.1.1 FailureReason: 0x0110 "Processing failure — Missing
# Attribute" is defined for exactly this case, so a refused dataset reports it
# rather than a generic code.
_FAILURE_REASON_MISSING = 0x0110
_FAILURE_REASON_UNREADABLE = 0xC000

# PS3.4 Annex B: the C-STORE failure status returned for a dataset this service
# refuses to file. 0xC000 ("Error: Cannot understand") is the defined code for
# "the payload is unusable"; 0x0110 is an N-service/MPPS code and is not a
# valid C-STORE status, so it is not returned on the DIMSE path.
_DIMSE_CANNOT_UNDERSTAND = 0xC000


class Type1UIDError(ValueError):
    """A dataset must not be filed because of a mandatory UID.

    Carries the offending keyword and tag so each ingest path can report the
    failure in its own vocabulary — a DIMSE status for C-STORE, a
    FailedSOPSequence item for STOW-RS — without re-deriving which tag was bad.
    """

    def __init__(self, keyword: str, tag: str, value: Any, reason: str) -> None:
        self.keyword = keyword
        self.tag = tag
        self.value = value
        self.reason = reason
        super().__init__(f"{keyword} {tag} {reason}")

    @property
    def dimse_status(self) -> int:
        """The C-STORE DIMSE failure status this refusal maps to."""
        return _DIMSE_CANNOT_UNDERSTAND

    @property
    def failure_reason(self) -> int:
        """The PS3.4 FailedSOPSequence FailureReason for this refusal."""
        return _FAILURE_REASON_UNREADABLE

    def as_dict(self) -> dict[str, Any]:
        """PHI-free description, for a JSON response body or a structured log."""
        return {
            "keyword": self.keyword,
            "tag": self.tag,
            "reason": self.reason,
            "failure_reason": f"0x{self.failure_reason:04X}",
        }


class MissingType1UID(Type1UIDError):
    """The attribute is absent or blank — the A-17 defect."""

    @property
    def failure_reason(self) -> int:
        return _FAILURE_REASON_MISSING


class PlaceholderType1UID(Type1UIDError):
    """The attribute holds a shared sentinel such as ``"UNKNOWN"``."""

    @property
    def failure_reason(self) -> int:
        return _FAILURE_REASON_MISSING


class MalformedType1UID(Type1UIDError):
    """The attribute is present but is not a well-formed DICOM UID."""


def strict_format_enabled() -> bool:
    """Whether PS3.5 UID grammar is enforced (rule 3) or only warned about."""
    return bool(getattr(settings, "DICOM_UID_STRICT_FORMAT", True))


def check_uid(value: Any, keyword: str, tag: str, *,
              strict_format: bool | None = None) -> str:
    """Return the usable UID, or raise :class:`Type1UIDError`.

    ``strict_format`` overrides the ``DICOM_UID_STRICT_FORMAT`` setting; tests
    use it to exercise both policies without mutating global configuration.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        raise MissingType1UID(
            keyword, tag, value, "is absent or blank (Type 1 is mandatory)"
        )
    if text.casefold() in PLACEHOLDER_UIDS:
        raise PlaceholderType1UID(
            keyword, tag, value,
            f"holds the shared placeholder {text!r}, which would merge unrelated studies",
        )
    if len(text) > MAX_UID_LENGTH:
        raise MalformedType1UID(
            keyword, tag, value,
            f"is {len(text)} characters (PS3.5 caps a UID at {MAX_UID_LENGTH})",
        )
    if not UID_PATTERN.match(text):
        reason = ("is not a DICOM UID (expected digits separated by dots, "
                  "e.g. 1.2.840.10008.5.1.4.1.1.2)")
        if strict_format is None:
            strict_format = strict_format_enabled()
        if strict_format:
            raise MalformedType1UID(keyword, tag, value, reason)
        # Relaxed mode: keep ingesting legacy identifiers, but never silently.
        # `%r` escapes control characters so a hostile value cannot forge a log
        # line, and UIDs are not PHI (AGENTS.md N-2).
        log.warning("A-17: %s %s %s — accepted because DICOM_UID_STRICT_FORMAT "
                    "is disabled", keyword, tag, reason)
    return text


def validate_mandatory_uids(ds: Any, *,
                            strict_format: bool | None = None) -> dict[str, str]:
    """Validate the three Type 1 UIDs on ``ds``; return them by DICOM keyword.

    Raises on the *first* violation, so the refusal is precise and cheap. Call
    this before anything is staged, indexed or written.
    """
    return {
        keyword: check_uid(getattr(ds, keyword, None), keyword, tag,
                           strict_format=strict_format)
        for keyword, tag in MANDATORY_UIDS
    }


def failed_sop_item(exc: Type1UIDError, *,
                    sop_class_uid: str | None = None,
                    sop_instance_uid: str | None = None) -> dict[str, Any]:
    """Build the DICOM JSON FailedSOPSequence item for a refused dataset.

    ``(0008,1198)`` carries these items. A dataset whose *own* SOP Instance UID
    is the missing attribute cannot name itself, so the value is emitted empty
    rather than invented — the ``error`` string alongside the response names the
    offending tag for a human.
    """
    return {
        _TAG_REF_SOP_CLASS: {"vr": "UI", "Value": [sop_class_uid or _UNKNOWN_SOP_CLASS]},
        _TAG_REF_SOP_INSTANCE: {"vr": "UI", "Value": [sop_instance_uid or ""]},
        _TAG_FAILURE_REASON: {"vr": "US", "Value": [exc.failure_reason]},
    }
