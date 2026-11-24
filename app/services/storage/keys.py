"""Storage key validation and construction (C05).

Keys are relative POSIX paths under ``STORAGE_ROOT``, and every component comes
from a DICOM UID — i.e. from an outside party. A component is therefore
*validated, never sanitised*: a value that could change the path it names is
refused, because silently rewriting it would file one patient's object under
another's directory (or outside the root).

This is the bottom of the storage package: it depends on
:mod:`app.services.storage.backend` only, so every other storage module can
build keys without a cycle.
"""

from __future__ import annotations

from app.services.storage.backend import StorageError


class InvalidStorageKey(StorageError):
    """A UID/key component that cannot safely be used as a path segment (C05).

    Raised instead of silently rewriting the value: ingest turns it into a 400
    (STOW-RS) or a Processing Failure (C-STORE), so a malformed identifier is
    rejected at the door rather than creating an object outside the root.
    """


# Characters that let a value change the *path* it names: separators, Windows
# drive colons, NTFS alternate-data-stream colons, and shell/glob metacharacters
# that some backends interpret.
_UNSAFE_CHARS = frozenset('/\\:*?"<>|')

# Windows reserves these device names in every directory (and silently strips
# trailing dots/spaces), so they can never be a safe path segment.
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

# DICOM UIDs are capped at 64 characters (PS3.5); a longer value is either
# corrupt or hostile.
_MAX_COMPONENT_LEN = 64

# Values that name a directory instead of a child of one.
_DOT_SEGMENTS = frozenset([".", ".."])


def unsafe_reason(value: str) -> str | None:
    """Why ``value`` cannot be a path segment, or None when it is safe.

    Deliberately conservative about *dangerous* input only: it does not enforce
    DICOM UID syntax, because real archives contain non-standard-but-harmless
    identifiers (e.g. hex-containing UIDs) that must keep working.
    """
    if value in _DOT_SEGMENTS:
        return "dot segment"
    bad = sorted({ch for ch in value if ch in _UNSAFE_CHARS or ord(ch) < 32 or ord(ch) == 127})
    if bad:
        printable = ", ".join(repr(ch) for ch in bad)
        return f"illegal character(s) {printable}"
    if len(value) > _MAX_COMPONENT_LEN:
        return f"longer than {_MAX_COMPONENT_LEN} characters"
    if value.endswith((".", " ")):
        # Windows silently strips these, so "1.2.3." and "1.2.3" would collide.
        return "trailing dot or space"
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        return "reserved Windows device name"
    return None


def validate_component(value: str | None, *, field: str = "UID") -> str:
    """Return ``value`` as a safe path component, or raise.

    * empty/whitespace -> ``InvalidStorageKey``. **A-17:** this used to return a
      deterministic ``{fallback}_{sha1("")}`` name, which is the *same* string
      for every dataset that omits the UID — so every UID-less instance from
      every patient landed in one directory and unrelated studies merged. Ingest
      now rejects such datasets at the door (``app.core.dicom_uids``); this is
      the last line of defence and must refuse, never repair.
    * dangerous (dot segment, separator, control char, device name, overlong)
      -> ``InvalidStorageKey`` — never silently rewritten (C05 hard-reject).
    """
    text = str(value if value is not None else "").strip()
    if not text:
        raise InvalidStorageKey(f"{field} is empty")
    reason = unsafe_reason(text)
    if reason:
        raise InvalidStorageKey(f"{field} {text!r} is not a safe path segment ({reason})")
    return text


# --------------------------------------------------------------------------- #
# Key construction
# --------------------------------------------------------------------------- #
def instance_key(study_uid: str, series_uid: str, sop_uid: str) -> str:
    return (
        f"{validate_component(study_uid, field='StudyInstanceUID')}/"
        f"{validate_component(series_uid, field='SeriesInstanceUID')}/"
        f"{validate_component(sop_uid, field='SOPInstanceUID')}.dcm"
    )


def thumbnail_key(sop_uid: str) -> str:
    return f".thumbnails/{validate_component(sop_uid, field='SOPInstanceUID')}.png"


def xds_document_key(unique_id: str, mime_type: str) -> str:
    """Build the established XDS object key after validating its generated UID."""
    extension = "dcm" if mime_type == "application/dicom" else "bin"
    component = validate_component(unique_id, field="XDSDocument.uniqueId")
    return f"xds/{component}.{extension}"
