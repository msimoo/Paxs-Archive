"""AI emergency triage — pluggable detector pipeline.

Detectors inspect series pixel data and emit structured findings. The worker
runs every series through the registered detectors once and stores findings in
`triage_flags`; the dashboard surfaces unreviewed HIGH findings immediately.

The bundled :class:`PneumothoraxHeuristicDetector` is a *demo heuristic*, NOT
clinical AI: it looks for a unilateral air pocket (very low HU region) in CT
chest slices, which is a crude approximation of a pneumothorax. It exists to
prove the pipeline end-to-end and as a template for real models (MONAI, etc.)
— plug a real detector in by appending to :data:`DETECTORS`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from app.core.config import settings
from app.services import storage_service

log = logging.getLogger("paxs.triage")


@dataclass
class Finding:
    finding: str
    severity: str  # HIGH / MEDIUM / LOW / INFO
    confidence: float
    summary: str
    detector: str = ""
    sop_instance_uid: str | None = None
    series_instance_uid: str | None = None
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #
class PneumothoraxHeuristicDetector:
    """Crude CT pneumothorax proxy: unilateral low-HU (air) region.

    * Reads up to TRIAGE_MAX_SLICES_PER_SERIES slices from a CT series.
    * Converts raw pixels to Hounsfield Units (rescale slope/intercept).
    * Air is HU < -900. If air occupies a large unilateral fraction of the
      slice (one lung field far more than the other), flag HIGH.
    """

    name = "pneumothorax-heuristic"
    MODALITIES = ("CT",)

    def detect(self, ds_list: list) -> list[Finding]:
        findings: list[Finding] = []
        air_frac_left, air_frac_right = [], []
        sop_uid = None
        for ds in ds_list:
            sop_uid = str(getattr(ds, "SOPInstanceUID", "")) or sop_uid
            try:
                arr = ds.pixel_array.astype(np.float64)
            except Exception as exc:
                # Compressed transfer syntax without a decode handler, or a
                # dataset with no pixel data — best-effort, skip. Logged at
                # DEBUG (not silently dropped) with the transfer syntax so a
                # systematic codec gap is diagnosable without flooding the log
                # for a whole series: this runs once per instance of a study.
                from app.core.dicom_diagnostics import decode_context

                log.debug(
                    "triage: skipping instance (%s: %s) | %s",
                    type(exc).__name__, exc, decode_context(ds),
                )
                continue
            # Multi-frame: analyze the middle frame.
            if arr.ndim == 3:
                arr = arr[arr.shape[0] // 2]
            if arr.ndim != 2 or arr.shape[0] < 16 or arr.shape[1] < 16:
                continue
            try:
                slope = float(getattr(ds, "RescaleSlope", 1) or 1)
                intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
                hu = arr * slope + intercept
            except (TypeError, ValueError):
                hu = arr
            h, w = hu.shape
            left, right = hu[:, : w // 2], hu[:, w // 2 :]
            air_left = float(np.mean(left < -900))
            air_right = float(np.mean(right < -900))
            air_frac_left.append(air_left)
            air_frac_right.append(air_right)

        if not air_frac_left or not air_frac_right:
            return findings

        left = float(np.mean(air_frac_left))
        right = float(np.mean(air_frac_right))
        # One field much airier than the other => suspect pneumothorax.
        ratio = max(left, right) / max(min(left, right), 1e-6)
        max_frac = max(left, right)
        if ratio > 2.0 and max_frac > 0.10:
            dominant = "left" if left > right else "right"
            confidence = min(0.95, 0.45 + 0.4 * min(ratio / 4.0, 1.0))
            findings.append(Finding(
                finding="PNEUMOTHORAX",
                severity="HIGH",
                confidence=round(confidence, 3),
                summary=(f"Heuristic: large unilateral air region on the {dominant} "
                         f"side (L {left:.0%} vs R {right:.0%}) — review urgently"),
                detector=self.name,
                sop_instance_uid=sop_uid,
            ))
        return findings


DETECTORS = [PneumothoraxHeuristicDetector()]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _slice_subset(ds_list: list, max_slices: int) -> list:
    """Evenly sample up to `max_slices` from a series (deterministic)."""
    n = len(ds_list)
    if n <= max_slices:
        return ds_list
    step = n / max_slices
    return [ds_list[int(i * step)] for i in range(max_slices)]


def run_detectors_on_series(series: object, instance_rows: list) -> list[Finding]:
    """Run every applicable detector over a series' instances.

    `instance_rows` are ORM Instance objects with `file_path`, `series` and
    `sop_instance_uid`. Corrupt/unreadable files are skipped, never fatal.
    """
    findings: list[Finding] = []
    if not settings.TRIAGE_ENABLED:
        return findings
    if not instance_rows:
        return findings

    modality = (getattr(series, "modality", "") or "").upper()

    max_slices = settings.TRIAGE_MAX_SLICES_PER_SERIES
    sampled = _slice_subset(instance_rows, max_slices)

    for detector in DETECTORS:
        if modality and detector.MODALITIES and modality not in detector.MODALITIES:
            continue
        ds_list: list = []
        try:
            for inst in sampled:
                try:
                    ds_list.append(storage_service.read_dataset(
                        inst.file_path, backend_name=getattr(inst, "storage_backend", None)
                    ))
                except Exception as exc:
                    log.debug("Triage skip unreadable %s: %s", inst.file_path, exc)
            detected = detector.detect(ds_list)
        except Exception:
            log.exception("Detector %s failed on series %s",
                          detector.name, getattr(series, "series_instance_uid", "?"))
            continue
        for f in detected:
            f.detector = f.detector or detector.name
            f.series_instance_uid = getattr(series, "series_instance_uid", None)
            findings.append(f)
    return findings
