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
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

import pydicom
from pydicom.uid import (
    HTJ2KLossless,
    JPEG2000,
    JPEG2000Lossless,
    JPEG2000MC,
    JPEGBaseline8Bit,
    JPEGExtended12Bit,
    JPEGLossless,
    JPEGLosslessSV1,
    JPEGLSLossless,
    JPEGLSNearLossless,
    RLELossless,
    UID,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db.models import Instance, LossyCompressionRule, Series, Study
from app.services import audit_service, storage_service

log = logging.getLogger("paxs.lossy_compression")


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

    _assert_geometry_preserved(before, out)

    # Prove the codestream is readable before it can ever be stored (this is the
    # guarantee C-3 lacked). Decoding once here is cheap next to the archival
    # benefit of never storing an undecodable object.
    try:
        out.pixel_array
    except Exception as exc:  # noqa: BLE001 - any decode failure is fatal
        raise LossyCompressionError(
            f"transcoded codestream does not decode as {target_ts_uid}: {exc}"
        ) from exc

    return out, len(out.PixelData)


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
    if not storage_service.exists(instance.file_path, backend_name=bk):
        return False

    try:
        ds = storage_service.read_dataset(instance.file_path, backend_name=bk)
        old_size = (
            instance.file_size_bytes
            or storage_service.size(instance.file_path, backend_name=bk)
        )

        new_ds, _pixel_bytes = transcode_dataset_lossy(
            ds, target_ts_uid, quality_factor
        )

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
            except Exception:  # noqa: BLE001 - context is best-effort
                log.debug("could not resolve study context for %s", instance.sop_instance_uid)

        # Stage the transcoded object and publish it only after every check has
        # passed. A lossy result is normally smaller, so the quota delta is
        # usually zero and no reservation is taken.
        staged = storage_service.stage_dataset_with_quota(
            new_ds,
            tenant_id,
            backend_name=bk,
            charge_bytes=max(0, len(new_ds.PixelData) - (old_size or 0)),
        )
        try:
            staged.promote()
        except Exception:
            staged.discard()
            raise

        instance.transfer_syntax_uid = str(new_ds.file_meta.TransferSyntaxUID)
        instance.file_size_bytes = storage_service.size(instance.file_path, backend_name=bk)
        instance.photometric_interpretation = new_ds.PhotometricInterpretation
        instance.bits_allocated = int(new_ds.BitsAllocated)
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
        log.warning("Lossy compression skipped for %s: %s", instance.sop_instance_uid, exc)
        return False
    except Exception:
        log.exception("Lossy compression failed for %s", instance.sop_instance_uid)
        return False


async def evaluate_and_run_lossy_compression(
    session: AsyncSession,
    rule_id: int | None = None,
) -> dict[str, Any]:
    """Evaluate enabled rules and transcode eligible instances.

    Fail-closed: with ``LOSSY_COMPRESSION_ENABLED=false`` this returns
    immediately and modifies nothing.
    """
    if not settings.LOSSY_COMPRESSION_ENABLED:
        log.warning(
            "Lossy compression is disabled (LOSSY_COMPRESSION_ENABLED=false) — "
            "no rules evaluated and no object modified."
        )
        return {"processed": 0, "skipped": 0, "disabled": True}

    stmt = select(LossyCompressionRule).where(LossyCompressionRule.enabled == True)  # noqa: E712
    if rule_id:
        stmt = stmt.where(LossyCompressionRule.id == rule_id)
    stmt = stmt.order_by(LossyCompressionRule.priority.asc(), LossyCompressionRule.id.asc())

    rules = (await session.execute(stmt)).scalars().all()
    if not rules:
        return {"processed": 0, "skipped": 0}

    # Candidate query: exclude every known lossy syntax so an already-lossy
    # instance can never be re-encoded (C-4). The per-instance file_meta check in
    # transcode_instance_lossy is the authoritative gate; this keeps the working
    # set small.
    inst_stmt = (
        select(Instance)
        .options(joinedload(Instance.series).joinedload(Series.study))
        .where(
            or_(
                Instance.transfer_syntax_uid.is_(None),
                Instance.transfer_syntax_uid.notin_(tuple(sorted(_LOSSY_UIDS))),
            )
        )
    )
    instances = (await session.execute(inst_stmt)).scalars().unique().all()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    processed_count = 0
    skipped_count = 0

    for inst in instances:
        study = inst.series.study if inst.series else None
        if not study:
            continue

        # Belt-and-braces: never select an ineligible source, even if the DB
        # transfer_syntax_uid is stale relative to the file.
        if not is_eligible_source(inst.transfer_syntax_uid):
            skipped_count += 1
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

            # An unsupported rule target is a configuration error, not a data
            # error: skip this instance rather than abort the run.
            try:
                validate_target_transfer_syntax(rule.transfer_syntax_uid)
            except LossyCompressionError as exc:
                log.warning(
                    "Rule %s targets an unsupported transfer syntax — skipping: %s",
                    rule.name, exc,
                )
                break

            ok = await transcode_instance_lossy(
                session, inst, rule.transfer_syntax_uid, rule.quality_factor,
                study_uid=study.study_instance_uid, tenant_id=study.tenant_id,
            )
            if ok:
                processed_count += 1
            else:
                skipped_count += 1
            break

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
