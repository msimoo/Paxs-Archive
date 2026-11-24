"""Lossy Compression Engine Service.

Transcodes aging studies to a lossy transfer syntax to reduce archive storage
consumption, driven by admin-defined :class:`LossyCompressionRule` policies.

Safety contract (issues C-2, C-3, C-4)
--------------------------------------
* **Fail-closed.** Nothing runs unless ``settings.LOSSY_COMPRESSION_ENABLED`` is
  true; both entry points return without touching any object otherwise.
* **Native pixel data only.** Compression operates on the dataset's own
  ``pixel_array`` through a real DICOM codec. It never routes pixels through the
  display pipeline (``imaging.dataset_to_image``), which applied the VOI LUT, a
  1-99% stretch and an 8-bit RGB conversion — irreversibly destroying 16-bit
  diagnostic data (C-2).
* **Encoder matched to the declared syntax.** The encoder is resolved *from*
  ``target_ts_uid``; if no installed encoder implements it, the operation aborts.
  It never writes a codestream of one type under another type's UID (C-3 — the
  old code emitted baseline-JPEG bytes labelled JPEG-LS).
* **Verified before publish.** Bit depth and geometry are asserted unchanged
  after encoding, and the transcoded object is decoded once to prove it is
  readable, before it is staged and atomically promoted.
* **Eligibility.** Only uncompressed or verified-lossless sources are eligible;
  already-lossy instances are excluded from the candidate query (C-4 — the old
  query excluded only baseline JPEG, so JPEG-LS / JPEG 2000 lossy data was
  re-encoded on every run, degrading it further each time).
* **Batched sweep.** :func:`evaluate_and_run_lossy_compression` walks the
  candidate set with keyset pagination (``WHERE id > :last_id ORDER BY id ASC
  LIMIT :batch``) and ``session.expunge_all()`` between chunks (C-11), so peak
  memory is one chunk rather than the entire candidate table. ``OFFSET`` is
  deliberately avoided: it re-walks everything it skips and can skip or repeat a
  row when a concurrent ingest inserts into the range already passed.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timezone
from typing import Any

import pydicom
from pydicom.uid import (
    JPEG2000,
    JPEG2000MC,
    UID,
    HTJ2KLossless,
    JPEG2000Lossless,
    JPEGBaseline8Bit,
    JPEGExtended12Bit,
    JPEGLossless,
    JPEGLosslessSV1,
    JPEGLSLossless,
    JPEGLSNearLossless,
    RLELossless,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db.models import Instance, LossyCompressionRule, Series, Study
from app.services import audit_service, content_version, storage_service
from app.services.dicomweb_service import dataset_to_json_text

log = logging.getLogger("paxs.lossy_compression")

#: Candidate instances transcoded per sweep chunk (C-11). Bounds peak memory to
#: one chunk plus the audit rows it produces, instead of the whole table.
LOSSY_SWEEP_BATCH_SIZE = 50


class LossyCompressionError(RuntimeError):
    """Raised when a lossy transcode cannot be performed safely.

    Callers must treat this as "abort, leave the object untouched" — never as a
    signal to store something anyway.
    """


# Lossy transfer syntaxes — never a valid *source* (re-encoding degrades them)
# and never silently produced unless an encoder matches exactly.
_LOSSY_UIDS: frozenset[str] = frozenset({
    str(JPEGBaseline8Bit),      # 1.2.840.10008.1.2.4.50
    str(JPEGExtended12Bit),     # 1.2.840.10008.1.2.4.51 (may be lossy)
    str(JPEGLSNearLossless),    # 1.2.840.10008.1.2.4.81
    str(JPEG2000),              # 1.2.840.10008.1.2.4.91
    str(JPEG2000MC),            # 1.2.840.10008.1.2.4.93
})

# Verified-lossless compressed syntaxes — bit-exact, so a legitimate source for a
# deliberate lossy policy (no pre-existing generational loss).
_LOSSLESS_COMPRESSED_UIDS: frozenset[str] = frozenset({
    str(RLELossless),           # 1.2.840.10008.1.2.5
    str(JPEGLSLossless),        # 1.2.840.10008.1.2.4.80
    str(JPEG2000Lossless),      # 1.2.840.10008.1.2.4.90
    str(HTJ2KLossless),         # 1.2.840.10008.1.2.4.201
    str(JPEGLossless),          # 1.2.840.10008.1.2.4.57
    str(JPEGLosslessSV1),       # 1.2.840.10008.1.2.4.70
})

# Lossy targets this engine can actually ENCODE. Deliberately excludes JPEG
# Baseline (1.2.840.10008.1.2.4.50): pydicom 3.0.x implements no baseline
# encoder, so producing it would mean hand-rolling a codestream and mislabelling
# it — exactly the C-3 defect. Both entries below preserve 16-bit samples.
LOSSY_TARGETS: dict[str, str] = {
    str(JPEGLSNearLossless): "JPEG-LS Near-Lossless",
    str(JPEG2000): "JPEG 2000 Lossy",
}

# Kept for backward compatibility with callers that referenced the old names.
JPEG_BASELINE_LOSSY = str(JPEGBaseline8Bit)
JPEG_LS_LOSSY = str(JPEGLSNearLossless)

# C-01: the PS3.3 C.7.6.1.1.5 method code recorded in (0028,2114) per target.
_LOSSY_METHOD_CODES: dict[str, str] = {
    str(JPEGLSNearLossless): "ISO_14495_1",
    str(JPEG2000): "ISO_15444_1",
}

# C-01: breast imaging is never lossy-compressed by policy. Lossy images are not
# acceptable for primary mammographic interpretation (e.g. FDA MQSA guidance),
# and an age-based storage rule must not be able to produce one.
_MAMMOGRAPHY_MODALITIES: frozenset[str] = frozenset({"MG"})
_BREAST_IMAGING_SOP_CLASSES: frozenset[str] = frozenset({
    "1.2.840.10008.5.1.4.1.1.1.2",      # Digital Mammography X-Ray - For Presentation
    "1.2.840.10008.5.1.4.1.1.1.2.1",    # Digital Mammography X-Ray - For Processing
    "1.2.840.10008.5.1.4.1.1.13.1.3",   # Breast Tomosynthesis Image
    "1.2.840.10008.5.1.4.1.1.13.1.4",   # Breast Projection X-Ray - For Presentation
    "1.2.840.10008.5.1.4.1.1.13.1.5",   # Breast Projection X-Ray - For Processing
})


def is_breast_imaging(modality: str | None, sop_class_uid: str | None) -> bool:
    """True for mammography / breast tomosynthesis objects (never lossy)."""
    if str(modality or "").strip().upper() in _MAMMOGRAPHY_MODALITIES:
        return True
    return str(sop_class_uid or "").strip() in _BREAST_IMAGING_SOP_CLASSES


# Geometry / bit-depth attributes that must survive a lossy re-encode unchanged.
_GEOMETRY_ATTRS = (
    "BitsAllocated",
    "BitsStored",
    "PhotometricInterpretation",
    "SamplesPerPixel",
    "Rows",
    "Columns",
)


# --------------------------------------------------------------------------- #
# Eligibility + encoder resolution
# --------------------------------------------------------------------------- #
def is_eligible_source(transfer_syntax_uid: str | None) -> bool:
    """True when a source in this syntax may be lossily re-encoded.

    Uncompressed (Explicit/Implicit VR Little/Big Endian, Deflated) and
    verified-lossless compressed sources qualify. Anything already lossy — or of
    unknown compressed form — is refused so diagnostic data is never degraded a
    second time.
    """
    text = str(transfer_syntax_uid or "").strip()
    if not text:
        # Unknown / absent syntax: the stored bytes are almost certainly raw;
        # the per-instance file_meta check in transcode_instance_lossy is the
        # authoritative gate.
        return True
    if text in _LOSSY_UIDS:
        return False
    try:
        uid = UID(text)
    except Exception:
        return False
    if not uid.is_compressed:
        return True
    return text in _LOSSLESS_COMPRESSED_UIDS


def encoder_available(transfer_syntax_uid: str) -> bool:
    """True when an installed pydicom encoder can produce this syntax."""
    try:
        from pydicom.pixels.encoders.base import get_encoder

        return bool(get_encoder(UID(transfer_syntax_uid)).is_available)
    except Exception:
        return False


def validate_target_transfer_syntax(transfer_syntax_uid: str | None) -> str:
    """Return ``transfer_syntax_uid`` when it is an encodable lossy target, else raise.

    Used both when a rule is created/updated (fail early) and before a transcode.
    """
    text = str(transfer_syntax_uid or "").strip()
    if text not in LOSSY_TARGETS:
        raise LossyCompressionError(
            f"unsupported lossy target transfer syntax {text!r}; "
            f"supported: {sorted(LOSSY_TARGETS)}"
        )
    if not encoder_available(text):
        raise LossyCompressionError(
            f"no installed encoder for {text} ({LOSSY_TARGETS[text]}) — "
            "refusing to store a codestream whose type cannot be produced"
        )
    return text


def _codec_kwargs(transfer_syntax_uid: str, quality_factor: int) -> dict:
    """Map a 1..100 quality factor onto the target codec's native parameter.

    Higher quality -> less loss. The mappings are deliberately conservative:
    JPEG-LS never goes below ``jls_error=1`` (its near-lossless floor) and JPEG
    2000 never above a ratio that would visibly degrade a diagnostic image.
    """
    q = max(1, min(100, int(quality_factor)))
    if transfer_syntax_uid == str(JPEGLSNearLossless):
        # jls_error = max absolute sample error. q=100 -> 1, q=1 -> 15.
        return {"jls_error": max(1, min(15, round((100 - q) / 100 * 15) or 1))}
    if transfer_syntax_uid == str(JPEG2000):
        # j2k_cr = compression ratio (higher q -> lower ratio). q=100 -> 2, q=1 -> ~39.6.
        return {"j2k_cr": [round(2.0 + 38.0 * (100 - q) / 100.0, 2)]}
    raise LossyCompressionError(f"no quality mapping for {transfer_syntax_uid}")


def _geometry(ds: pydicom.Dataset) -> dict:
    """The bit-depth / geometry attributes that must not change."""
    out: dict = {}
    for attr in _GEOMETRY_ATTRS:
        out[attr] = getattr(ds, attr, None)
    return out


def _values(value: Any) -> list[str]:
    """A DICOM attribute value as a list of strings (VM 1 arrives as a scalar)."""
    if value is None or value == "":
        return []
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        return [str(value)]
    return [str(item) for item in value]


def _mark_lossy(out: pydicom.Dataset, src: pydicom.Dataset, target_ts_uid: str) -> None:
    """Record an irreversible compression as PS3.3 C.7.6.1.1.5 requires (C-01).

    Sets Lossy Image Compression (0028,2110) to "01" and *appends* this step's
    ratio (0028,2112) and method (0028,2114) to any history the source carries,
    so successive lossy steps stay traceable. Marks the image DERIVED and states
    the derivation, so no viewer can present it as an original acquisition.
    """
    method = _LOSSY_METHOD_CODES[target_ts_uid]
    source_bytes = len(getattr(src, "PixelData", b"") or b"")
    ratio = source_bytes / max(1, len(out.PixelData))
    ratios = _values(getattr(src, "LossyImageCompressionRatio", None))
    methods = _values(getattr(src, "LossyImageCompressionMethod", None))
    out.LossyImageCompression = "01"
    out.LossyImageCompressionRatio = [*ratios, f"{ratio:.2f}"[:16]]
    out.LossyImageCompressionMethod = [*methods, method]
    image_type = _values(getattr(src, "ImageType", None))
    out.ImageType = ["DERIVED", *image_type[1:]] if image_type else ["DERIVED", "SECONDARY"]
    out.DerivationDescription = f"Lossy compressed {method} ratio {ratio:.1f}:1"[:1024]


def _assert_geometry_preserved(before: dict, after_ds: pydicom.Dataset) -> None:
    """Raise when a lossy re-encode altered bit depth or image geometry.

    A change here means the encoder silently down-converted or rescaled the
    pixels (the C-2 failure mode), so the result must never be stored.
    """
    after = _geometry(after_ds)
    for attr, expected in before.items():
        got = after.get(attr)
        if attr == "BitsStored" and expected is None:
            continue
        if got != expected:
            raise LossyCompressionError(
                f"lossy re-encode changed {attr}: {expected!r} -> {got!r}; "
                "refusing to store a geometry-altered instance"
            )


# --------------------------------------------------------------------------- #
# Transcoding
# --------------------------------------------------------------------------- #
def transcode_dataset_lossy(
    ds: pydicom.Dataset,
    target_ts_uid: str = JPEG_LS_LOSSY,
    quality_factor: int = 85,
) -> tuple[pydicom.Dataset, int]:
    """Lossily re-encode a dataset's NATIVE pixel data with the matching codec.

    Returns ``(new_dataset, encapsulated_pixel_bytes)``. Raises
    :class:`LossyCompressionError` when the target syntax has no encoder, the
    source is not eligible, or the result fails verification. The input dataset
    is never mutated.
    """
    validate_target_transfer_syntax(target_ts_uid)

    pixel_data = getattr(ds, "PixelData", None)
    if pixel_data is None or len(pixel_data) == 0:
        raise LossyCompressionError("dataset has no PixelData")

    src_ts = str(getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", "") or "")
    if not is_eligible_source(src_ts):
        raise LossyCompressionError(
            f"source transfer syntax {src_ts!r} is already compressed/lossy — "
            "re-encoding would degrade diagnostic data"
        )

    if is_breast_imaging(getattr(ds, "Modality", None), getattr(ds, "SOPClassUID", None)):
        raise LossyCompressionError(
            "breast imaging (MG / tomosynthesis) is never lossy-compressed"
        )

    before = _geometry(ds)
    sop_uid = str(getattr(ds, "SOPInstanceUID", "") or "")

    out = copy.deepcopy(ds)
    # ds.compress() encodes out.pixel_array with the encoder registered for
    # target_ts_uid and encapsulates the result. No display pipeline is involved.
    #
    # generate_instance_uid=False is essential: pydicom otherwise mints a NEW
    # SOP Instance UID when the transfer syntax changes, which would write the
    # result to a different storage key and orphan the object the database row
    # still points at. This is an in-place re-encode of the *same* instance.
    out.compress(
        UID(target_ts_uid),
        generate_instance_uid=False,
        **_codec_kwargs(target_ts_uid, quality_factor),
    )

    if str(out.file_meta.TransferSyntaxUID) != str(target_ts_uid):
        raise LossyCompressionError(
            f"encoder produced {out.file_meta.TransferSyntaxUID!r} but "
            f"{target_ts_uid!r} was requested — refusing to mislabel"
        )
    if sop_uid and str(getattr(out, "SOPInstanceUID", "")) != sop_uid:
        raise LossyCompressionError(
            "encoder changed the SOP Instance UID — refusing to relocate the instance"
        )

    _mark_lossy(out, ds, target_ts_uid)
    _assert_geometry_preserved(before, out)

    # Prove the codestream is readable before it can ever be stored (this is the
    # guarantee C-3 lacked). Decoding once here is cheap next to the archival
    # benefit of never storing an undecodable object.
    try:
        out.pixel_array
    except Exception as exc:
        raise LossyCompressionError(
            f"transcoded codestream does not decode as {target_ts_uid}: {exc}"
        ) from exc

    return out, len(out.PixelData)


def _read_and_transcode(
    file_path: str,
    backend_name: str | None,
    known_size: int | None,
    target_ts_uid: str,
    quality_factor: int,
) -> tuple[pydicom.Dataset, str, int] | None:
    """Read + encode + verification decode + JSON view, in a worker thread (H-02).

    Returns ``None`` when the object is missing, else the transcoded dataset,
    its WADO-RS JSON view (the cached view must describe the *replacement*,
    or the viewer would decode it with the wrong codec) and the source size.
    Touches neither the database nor the store's canonical keys.
    """
    if not storage_service.exists(file_path, backend_name=backend_name):
        return None
    ds = storage_service.read_dataset(file_path, backend_name=backend_name)
    old_size = known_size or storage_service.size(file_path, backend_name=backend_name)
    new_ds, _pixel_bytes = transcode_dataset_lossy(ds, target_ts_uid, quality_factor)
    return new_ds, dataset_to_json_text(new_ds), int(old_size or 0)


def _stage_transcoded(
    new_ds: pydicom.Dataset,
    tenant_id: int | None,
    backend_name: str | None,
    old_size: int,
) -> storage_service.StagedObject:
    """Stage and verify the transcoded object, unpublished, in a worker thread (H-02).

    A lossy result is normally smaller, so the quota delta is usually zero and
    no reservation is taken. A failed verify discards the stage.
    """
    staged = storage_service.stage_dataset_with_quota(
        new_ds,
        tenant_id,
        backend_name=backend_name,
        charge_bytes=max(0, len(new_ds.PixelData) - (old_size or 0)),
    )
    try:
        staged.verify()
    except Exception:
        staged.discard()
        raise
    return staged


async def transcode_instance_lossy(
    session: AsyncSession,
    instance: Instance,
    target_ts_uid: str = JPEG_LS_LOSSY,
    quality_factor: int = 85,
    *,
    study_uid: str | None = None,
    tenant_id: int | None = None,
) -> bool:
    """Lossily re-encode one stored instance, publishing atomically.

    Returns True when the object was replaced, False when it was skipped (feature
    disabled, object missing, or source not eligible). Raises nothing: a genuine
    failure is logged and reported as False so one bad instance cannot abort a
    whole rule run.

    ``study_uid`` / ``tenant_id`` are optional: callers that already loaded the
    study relationship (e.g. :func:`evaluate_and_run_lossy_compression`) pass
    them so no lazy load is attempted inside the async session.
    """
    if not settings.LOSSY_COMPRESSION_ENABLED:
        log.info(
            "Lossy compression disabled (LOSSY_COMPRESSION_ENABLED=false); "
            "skipping %s", instance.sop_instance_uid,
        )
        return False

    bk = getattr(instance, "storage_backend", None)

    try:
        # H-02 / N-4: read, encode + verification decode, JSON view and staging
        # are seconds of CPU and I/O for a multi-frame object, so they run in
        # worker threads and the loop keeps serving; the row mutation stays
        # here. Two hops, because staging needs the tenant resolved below.
        read = await asyncio.to_thread(
            _read_and_transcode, instance.file_path, bk,
            getattr(instance, "file_size_bytes", None), target_ts_uid, quality_factor,
        )
        if read is None:
            return False
        new_ds, metadata_json, old_size = read

        # Resolve the owning study for quota scoping + audit WITHOUT forcing a
        # lazy load in the async context (that raises MissingGreenlet and would
        # silently fail the whole transcode).
        if study_uid is None and tenant_id is None:
            try:
                from sqlalchemy import inspect as sa_inspect

                if "series" not in sa_inspect(instance).unloaded:
                    study = instance.series.study if instance.series else None
                    if study is not None:
                        study_uid = study.study_instance_uid
                        tenant_id = study.tenant_id
            except (OSError, ValueError):  # context is best-effort
                log.debug("could not resolve study context for %s", instance.sop_instance_uid)

        staged = await asyncio.to_thread(
            _stage_transcoded, new_ds, tenant_id, bk, old_size,
        )

        try:
            instance.transfer_syntax_uid = str(new_ds.file_meta.TransferSyntaxUID)
            # The replacement is not published yet, so reading the object back
            # would measure the *old* bytes. The size was measured while staging.
            instance.file_size_bytes = staged.bytes_written
            instance.photometric_interpretation = new_ds.PhotometricInterpretation
            instance.bits_allocated = int(new_ds.BitsAllocated)
            instance.metadata_json = metadata_json
            # C-04: new bytes behind the same SOP UID.
            content_version.bump(instance)
            await session.flush()

            audit_service.add_audit(
                session,
                sop_instance_uid=instance.sop_instance_uid,
                study_instance_uid=study_uid,
                action="LOSSY_COMPRESSED",
                performed_by=(
                    f"lossy-compression-engine:{LOSSY_TARGETS.get(target_ts_uid, target_ts_uid)}"
                    f":Q{quality_factor}"
                ),
            )
        except Exception:
            # The row update or the audit entry failed. Nothing has been
            # published and nothing is registered yet, so the stage is simply
            # dropped — the original object and the row still agree.
            staged.discard()
            raise

        # A-2: publish strictly *after* the caller's transaction commits.
        #
        # The row is what *describes* the bytes, so promoting here would
        # overwrite the lossless object while the row still read "lossless"; if
        # the caller's commit then failed, the rollback would restore a row that
        # calls JPEG-LS bytes Explicit VR Little Endian — a mislabelled
        # instance, with the original data already gone.
        #
        # These are session events, not inline calls, so the ordering is a
        # property of the transaction rather than a convention each caller has
        # to remember: exactly one of them runs. On commit the promote fires and
        # the discard is disarmed; on rollback the promote is disarmed and the
        # discard runs, releasing the reservation and leaving the original
        # object untouched. Registration is deliberately last, so a failure
        # above can never leave a publish armed.
        storage_service.defer_after_commit(session, staged.promote)
        storage_service.defer_after_rollback(session, staged.discard)

        log.info(
            "Lossy compressed instance %s (%s, Q=%d): %d -> %d bytes",
            instance.sop_instance_uid,
            LOSSY_TARGETS.get(target_ts_uid, target_ts_uid),
            quality_factor,
            old_size,
            instance.file_size_bytes,
        )
        return True
    except LossyCompressionError as exc:
        # Expected, safe refusal — the object is left untouched.
        log.warning(
            "lossy_compression_skipped sop_instance_uid=%s error_type=%s",
            instance.sop_instance_uid,
            type(exc).__name__,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - boundary guard: codec/storage failures may carry PHI or object paths
        log.error("lossy_compression_failed error_type=%s", type(exc).__name__)
        return False


def _instance_age_days(study: Study, now: datetime) -> int:
    """Whole days between the study date (falling back to ``created_at``) and now."""
    study_date_str = study.study_date
    if study_date_str and len(study_date_str) == 8 and study_date_str.isdigit():
        try:
            return (now - datetime.strptime(study_date_str, "%Y%m%d")).days
        except ValueError as exc:
            # An 8-digit-but-invalid StudyDate falls back to created_at below.
            log.debug("lossy_study_date_unparsable error_type=%s", type(exc).__name__)
    return (now - study.created_at).days if study.created_at else 0


def _rule_matches_instance(rule: LossyCompressionRule, inst: Instance, age_days: int) -> bool:
    """True when the rule's age / modality / body-part filters accept the instance.

    An absent series modality / body part means "not stated", which the rule
    cannot contradict — the filter is skipped rather than failing the instance.
    """
    if age_days < rule.min_age_days:
        return False
    series = inst.series
    # C-01: breast imaging is excluded whatever the rule says.
    if is_breast_imaging(
        getattr(series, "modality", None), getattr(inst, "sop_class_uid", None)
    ):
        return False
    if series is None:
        return True
    if rule.modality and series.modality and rule.modality.upper() != series.modality.upper():
        return False
    if rule.body_part_examined and series.body_part_examined:
        return rule.body_part_examined.upper() == series.body_part_examined.upper()
    return True


async def _apply_rules_to_instance(
    session: AsyncSession,
    inst: Instance,
    rules: list[LossyCompressionRule],
    now: datetime,
) -> bool | None:
    """Transcode one instance under the first matching rule.

    Returns ``True`` when it was replaced, ``False`` when it was deliberately
    skipped, and ``None`` when no rule applied (or the rule's target syntax is
    unsupported — a configuration error that skips the instance rather than
    aborting the run).
    """
    study = inst.series.study if inst.series else None
    if not study:
        return None

    # Belt-and-braces: never select an ineligible source, even if the DB
    # transfer_syntax_uid is stale relative to the file.
    if not is_eligible_source(inst.transfer_syntax_uid):
        return False

    age_days = _instance_age_days(study, now)
    for rule in rules:
        if not _rule_matches_instance(rule, inst, age_days):
            continue
        try:
            validate_target_transfer_syntax(rule.transfer_syntax_uid)
        except LossyCompressionError as exc:
            log.warning(
                "lossy_rule_target_unsupported rule_id=%s error_type=%s",
                rule.id,
                type(exc).__name__,
            )
            return None
        return await transcode_instance_lossy(
            session, inst, rule.transfer_syntax_uid, rule.quality_factor,
            study_uid=study.study_instance_uid, tenant_id=study.tenant_id,
        )
    return None


async def evaluate_and_run_lossy_compression(
    session: AsyncSession,
    rule_id: int | None = None,
    *,
    batch_size: int = LOSSY_SWEEP_BATCH_SIZE,
) -> dict[str, Any]:
    """Evaluate enabled rules and transcode eligible instances.

    Fail-closed: with ``LOSSY_COMPRESSION_ENABLED=false`` this returns
    immediately and modifies nothing.

    C-11/FP-8: candidates are fetched one keyset page at a time and the chunk is
    dropped from the identity map before the next one is read, so peak memory is
    one chunk instead of the whole candidate table.
    """
    if not settings.LOSSY_COMPRESSION_ENABLED:
        log.warning(
            "Lossy compression is disabled (LOSSY_COMPRESSION_ENABLED=false) — "
            "no rules evaluated and no object modified."
        )
        return {"processed": 0, "skipped": 0, "disabled": True}

    stmt = select(LossyCompressionRule).where(LossyCompressionRule.enabled.is_(True))
    if rule_id:
        stmt = stmt.where(LossyCompressionRule.id == rule_id)
    stmt = stmt.order_by(LossyCompressionRule.priority.asc(), LossyCompressionRule.id.asc())

    rules = (await session.execute(stmt)).scalars().all()
    if not rules:
        return {"processed": 0, "skipped": 0}

    # Candidate filter: exclude every known lossy syntax so an already-lossy
    # instance can never be re-encoded (C-4). The per-instance file_meta check in
    # transcode_instance_lossy is the authoritative gate; this keeps the working
    # set small.
    eligible_syntax = or_(
        Instance.transfer_syntax_uid.is_(None),
        Instance.transfer_syntax_uid.notin_(tuple(sorted(_LOSSY_UIDS))),
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    processed_count = 0
    skipped_count = 0
    last_id = 0

    while True:
        inst_stmt = (
            select(Instance)
            .options(joinedload(Instance.series).joinedload(Series.study))
            .where(Instance.id > last_id, eligible_syntax)
            .order_by(Instance.id.asc())
            .limit(batch_size)
        )
        batch = (await session.execute(inst_stmt)).scalars().unique().all()
        if not batch:
            break

        for inst in batch:
            outcome = await _apply_rules_to_instance(session, inst, rules, now)
            if outcome is True:
                processed_count += 1
            elif outcome is False:
                skipped_count += 1

        last_id = batch[-1].id
        # Flush before dropping the chunk: `expunge_all()` discards *pending*
        # work, and each transcoded instance has a pending row update plus its
        # audit entry. Flushed work stays in the transaction.
        await session.flush()
        session.expunge_all()

    await session.flush()
    return {"processed": processed_count, "skipped": skipped_count}


# --------------------------------------------------------------------------- #
# CRUD helpers
# --------------------------------------------------------------------------- #
async def create_rule(session: AsyncSession, data: dict[str, Any]) -> LossyCompressionRule:
    """Create a rule, rejecting an unsupported lossy target up front."""
    if data.get("transfer_syntax_uid") is not None:
        validate_target_transfer_syntax(data["transfer_syntax_uid"])
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
