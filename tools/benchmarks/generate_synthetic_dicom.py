"""Create a bounded, reproducible, non-clinical DICOM capacity dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from pydicom.pixels import get_encoder
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (
    CTImageStorage,
    ComputedRadiographyImageStorage,
    DigitalXRayImageStorageForPresentation,
    EnhancedCTImageStorage,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    JPEGLSLossless,
    MultiFrameGrayscaleWordSecondaryCaptureImageStorage,
    MRImageStorage,
    PositronEmissionTomographyImageStorage,
    SecondaryCaptureImageStorage,
    UltrasoundImageStorage,
)

_NAMESPACE = uuid.UUID("9f09d02a-541d-4f6c-b6a3-8fc8cb4d3bc7")
_SOP_CLASSES = {
    "CT": CTImageStorage,
    "MR": MRImageStorage,
    "CR": ComputedRadiographyImageStorage,
    "DX": DigitalXRayImageStorageForPresentation,
    "US": UltrasoundImageStorage,
}
_TRANSFER_SYNTAXES = {
    "explicit-vr-le": ExplicitVRLittleEndian,
    "implicit-vr-le": ImplicitVRLittleEndian,
}
_MAX_DIMENSION = 4096
_DEFAULT_MAX_INSTANCES = 2000
_DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
_ESTIMATED_HEADER_BYTES = 64 * 1024


@dataclass(frozen=True)
class DatasetProfile:
    """Validated shape and hard bounds for one synthetic generation run."""

    studies: int
    series_per_study: int
    instances_per_series: int
    rows: int
    columns: int
    modality: str
    transfer_syntax: str
    seed: int
    max_instances: int
    max_total_bytes: int
    pattern: str = "capacity"

    @property
    def total_instances(self) -> int:
        """Number of DICOM objects that will be written."""
        return self.studies * self.series_per_study * self.instances_per_series

    @property
    def estimated_bytes(self) -> int:
        """Conservative uncompressed size estimate used before any output is made."""
        object_bytes = self.rows * self.columns * 2 + _ESTIMATED_HEADER_BYTES
        return self.total_instances * object_bytes


def _uid(seed: int, kind: str, *indices: int) -> str:
    """Create a stable DICOM UID from synthetic run coordinates."""
    name = ":".join((str(seed), kind, *(str(index) for index in indices)))
    value = uuid.uuid5(_NAMESPACE, name)
    return f"2.25.{value.int}"


def _validate_profile(args: argparse.Namespace) -> DatasetProfile:
    """Validate all dimensions, supported syntax, count and byte ceilings."""
    modality = args.modality.upper()
    if modality not in _SOP_CLASSES:
        raise ValueError(f"unsupported modality: {args.modality}")
    if args.transfer_syntax not in _TRANSFER_SYNTAXES:
        raise ValueError(f"unsupported transfer syntax: {args.transfer_syntax}")
    for name in ("studies", "series_per_study", "instances_per_series"):
        if getattr(args, name) < 1:
            raise ValueError(f"{name} must be at least 1")
    for name in ("rows", "columns"):
        value = getattr(args, name)
        if value < 16 or value > _MAX_DIMENSION:
            raise ValueError(f"{name} must be between 16 and {_MAX_DIMENSION}")
    if args.max_instances < 0 or args.max_total_bytes < 0:
        raise ValueError("capacity limits cannot be negative; zero disables that limit")
    profile = DatasetProfile(
        studies=args.studies,
        series_per_study=args.series_per_study,
        instances_per_series=args.instances_per_series,
        rows=args.rows,
        columns=args.columns,
        modality=modality,
        transfer_syntax=args.transfer_syntax,
        seed=args.seed,
        max_instances=args.max_instances,
        max_total_bytes=args.max_total_bytes,
    )
    if profile.max_instances and profile.total_instances > profile.max_instances:
        raise ValueError(
            f"requested {profile.total_instances} instances exceeds limit "
            f"{profile.max_instances}"
        )
    if profile.max_total_bytes and profile.estimated_bytes > profile.max_total_bytes:
        raise ValueError(
            f"estimated dataset size {profile.estimated_bytes} exceeds byte limit "
            f"{profile.max_total_bytes}"
        )
    return profile


def _build_instance(
    profile: DatasetProfile,
    study_index: int,
    series_index: int,
    instance_index: int,
) -> FileDataset:
    """Build one deterministic, uncompressed synthetic image instance."""
    sop_uid = _uid(profile.seed, "sop", study_index, series_index, instance_index)
    syntax_uid = _TRANSFER_SYNTAXES[profile.transfer_syntax]
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = _SOP_CLASSES[profile.modality]
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = syntax_uid
    file_meta.ImplementationClassUID = _uid(profile.seed, "implementation")
    dataset = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = _SOP_CLASSES[profile.modality]
    dataset.SOPInstanceUID = sop_uid
    dataset.StudyInstanceUID = _uid(profile.seed, "study", study_index)
    dataset.SeriesInstanceUID = _uid(profile.seed, "series", study_index, series_index)
    dataset.Modality = profile.modality
    dataset.PatientName = f"SYNTHETIC^{study_index:06d}"
    dataset.PatientID = f"SYNTHETIC-{study_index:06d}"
    dataset.PatientBirthDate = "19000101"
    dataset.PatientSex = "O"
    dataset.StudyDate = "20260101"
    dataset.StudyTime = "120000"
    dataset.StudyDescription = "Synthetic capacity-test study"
    dataset.SeriesDescription = "Synthetic capacity-test series"
    dataset.SeriesNumber = series_index + 1
    dataset.InstanceNumber = instance_index + 1
    dataset.ImageType = ["ORIGINAL", "PRIMARY", "SYNTHETIC"]
    dataset.Rows = profile.rows
    dataset.Columns = profile.columns
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 1
    dataset.PixelSpacing = ["0.8", "0.8"]
    dataset.ImageOrientationPatient = ["1", "0", "0", "0", "1", "0"]
    dataset.ImagePositionPatient = ["0", "0", str(instance_index)]
    dataset.SliceThickness = "1.0"
    dataset.RescaleSlope = "1"
    dataset.RescaleIntercept = "-1024"
    _set_synthetic_pixels(dataset, profile, study_index, series_index, instance_index)
    return dataset


def _set_synthetic_pixels(dataset: FileDataset, profile: DatasetProfile,
                          study_index: int, series_index: int, instance_index: int) -> None:
    """Set either the historical random payload or the mathematical CT phantom."""
    if profile.pattern == "viewer-ct":
        y, x = np.indices((profile.rows, profile.columns))
        dataset.PixelRepresentation = 0
        dataset.PixelData = (24 + 2*x + 3*y + 10*instance_index
                             + 1600 * series_index).astype("<u2").tobytes()
        dataset.PatientPosition = "HFS"
        dataset.FrameOfReferenceUID = _uid(profile.seed, "frame", study_index)
        dataset.ImagePositionPatient = ["0", "0", str(instance_index * 2.5)]
        dataset.SliceThickness = "2.5"
        dataset.SpacingBetweenSlices = "2.5"
        dataset.WindowCenter = "-500"
        dataset.WindowWidth = "1000"
        dataset.StudyDescription = "SYNTHETIC mathematical CT - NOT FOR CLINICAL USE"
    else:
        rng = np.random.default_rng(profile.seed + study_index * 1_000_000
                                    + series_index * 10_000 + instance_index)
        pixels = rng.integers(-1024, 3072, size=(profile.rows, profile.columns), dtype=np.int16)
        dataset.PixelData = pixels.tobytes()


def _write_instance(dataset: FileDataset, path: Path) -> tuple[int, str]:
    """Write one DICOM file and return its byte count and SHA-256 digest."""
    dataset.save_as(path, enforce_file_format=True)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _enhanced_multiframe_dataset(
    frames: int, rows: int, columns: int, seed: int, *,
    study_uid: str | None = None, series_uid: str | None = None,
) -> tuple[FileDataset, np.ndarray]:
    """Build an Enhanced CT phantom with per-frame position and calibration."""
    pixels = np.stack([
        np.full((rows, columns), frame_index + 100, dtype="<u2")
        for frame_index in range(frames)
    ])
    sop_uid = _uid(seed, "enhanced-sop", frames, rows, columns)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = EnhancedCTImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = _uid(seed, "implementation")
    dataset = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = EnhancedCTImageStorage
    dataset.SOPInstanceUID = sop_uid
    dataset.StudyInstanceUID = study_uid or _uid(seed, "enhanced-study")
    dataset.SeriesInstanceUID = series_uid or _uid(seed, "enhanced-series")
    dataset.FrameOfReferenceUID = _uid(seed, "enhanced-frame-of-reference")
    dataset.Modality = "CT"
    dataset.PatientName = "SYNTHETIC^MULTIFRAME"
    dataset.PatientID = "SYNTHETIC-MULTIFRAME"
    dataset.StudyDate = "20260101"
    dataset.NumberOfFrames = str(frames)
    dataset.Rows = rows
    dataset.Columns = columns
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.PixelData = pixels.tobytes()

    shared = Dataset()
    measures = Dataset()
    measures.PixelSpacing = ["0.7", "0.7"]
    measures.SliceThickness = "1.0"
    shared.PixelMeasuresSequence = Sequence([measures])
    orientation = Dataset()
    orientation.ImageOrientationPatient = ["1", "0", "0", "0", "1", "0"]
    shared.PlaneOrientationSequence = Sequence([orientation])
    dataset.SharedFunctionalGroupsSequence = Sequence([shared])

    per_frame = []
    for frame_index in range(frames):
        group = Dataset()
        position = Dataset()
        position.ImagePositionPatient = ["0", "0", str(frame_index)]
        group.PlanePositionSequence = Sequence([position])
        content = Dataset()
        content.DimensionIndexValues = [frame_index + 1]
        content.InStackPositionNumber = frame_index + 1
        group.FrameContentSequence = Sequence([content])
        transform = Dataset()
        transform.RescaleSlope = "1"
        transform.RescaleIntercept = "-1024"
        group.PixelValueTransformationSequence = Sequence([transform])
        per_frame.append(group)
    dataset.PerFrameFunctionalGroupsSequence = Sequence(per_frame)
    return dataset, pixels


def generate_multiframe_fixture_set(
    output: Path, *, frames: int = 256, seed: int = 20261104
) -> dict[str, Any]:
    """Write deterministic synthetic Enhanced, odd-size, mixed-size, and JPEG-LS fixtures."""
    if frames < 2 or frames > 3000:
        raise ValueError("frames must be between 2 and 3000")
    output.mkdir(parents=True, exist_ok=False)
    study_uid = _uid(seed, "fixture-study")
    series_uid = _uid(seed, "fixture-series")
    fixtures: list[dict[str, Any]] = []

    enhanced, _pixels = _enhanced_multiframe_dataset(
        frames, 64, 64, seed, study_uid=study_uid, series_uid=series_uid
    )
    enhanced_path = output / "enhanced-ct-multiframe.dcm"
    size, digest = _write_instance(enhanced, enhanced_path)
    fixtures.append({"path": enhanced_path.name, "bytes": size, "sha256": digest,
                     "frames": frames, "rows": 64, "columns": 64,
                     "transfer_syntax": str(ExplicitVRLittleEndian)})

    odd, _odd_pixels = _enhanced_multiframe_dataset(
        9, 17, 19, seed + 1, study_uid=study_uid, series_uid=series_uid
    )
    odd_path = output / "odd-dimensions-17x19.dcm"
    size, digest = _write_instance(odd, odd_path)
    fixtures.append({"path": odd_path.name, "bytes": size, "sha256": digest,
                     "frames": 9, "rows": 17, "columns": 19,
                     "transfer_syntax": str(ExplicitVRLittleEndian)})

    mixed_sizes = []
    mixed_dir = output / "mixed-size-series"
    mixed_dir.mkdir()
    for index, (rows, columns) in enumerate(((64, 80), (73, 91)), 1):
        mixed, _mixed_pixels = _enhanced_multiframe_dataset(
            4, rows, columns, seed + index + 1,
            study_uid=study_uid, series_uid=series_uid,
        )
        mixed_path = mixed_dir / f"instance-{index:02d}.dcm"
        size, digest = _write_instance(mixed, mixed_path)
        mixed_sizes.append({"path": mixed_path.relative_to(output).as_posix(),
                            "bytes": size, "sha256": digest, "frames": 4,
                            "rows": rows, "columns": columns,
                            "transfer_syntax": str(ExplicitVRLittleEndian)})
    fixtures.extend(mixed_sizes)

    codec_status = "unsupported: JPEG-LS encoder unavailable"
    encoder = get_encoder(JPEGLSLossless)
    if encoder.is_available:
        compressed, compressed_pixels = _enhanced_multiframe_dataset(
            8, 32, 31, seed + 4, study_uid=study_uid, series_uid=series_uid
        )
        try:
            compressed.compress(JPEGLSLossless, arr=compressed_pixels,
                                generate_instance_uid=False)
            compressed_path = output / "jpeg-ls-lossless-multiframe.dcm"
            size, digest = _write_instance(compressed, compressed_path)
            fixtures.append({"path": compressed_path.name, "bytes": size,
                             "sha256": digest, "frames": 8, "rows": 32,
                             "columns": 31,
                             "transfer_syntax": str(JPEGLSLossless)})
            codec_status = "generated"
        except (NotImplementedError, RuntimeError, ValueError) as exc:
            codec_status = f"unsupported: encoder failure ({type(exc).__name__})"

    manifest = {
        "format": "paxs-synthetic-multiframe-wado-v1",
        "phi_free": True,
        "not_for_clinical_use": True,
        "seed": seed,
        "study_uid": study_uid,
        "series_uid": series_uid,
        "fixtures": fixtures,
        "codecs": {"jpeg_ls_lossless": codec_status},
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def generate_dataset(profile: DatasetProfile, output: Path) -> dict[str, Any]:
    """Write a new synthetic dataset and a PHI-free manifest without overwrites."""
    output.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    for study_index in range(profile.studies):
        for series_index in range(profile.series_per_study):
            series_dir = output / f"study-{study_index + 1:06d}" / f"series-{series_index + 1:03d}"
            series_dir.mkdir(parents=True, exist_ok=True)
            for instance_index in range(profile.instances_per_series):
                dataset = _build_instance(profile, study_index, series_index, instance_index)
                file_path = series_dir / f"instance-{instance_index + 1:05d}.dcm"
                size, digest = _write_instance(dataset, file_path)
                records.append({
                    "path": file_path.relative_to(output).as_posix(),
                    "bytes": size,
                    "sha256": digest,
                    "modality": profile.modality,
                    "transfer_syntax": profile.transfer_syntax,
                })
    manifest = {
        "format": "paxs-synthetic-dicom-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile": asdict(profile),
        "counts": {
            "studies": profile.studies,
            "series": profile.studies * profile.series_per_study,
            "instances": len(records),
            "bytes": sum(record["bytes"] for record in records),
        },
        "instances": records,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def generate_viewer_ct(output: Path, seed: int = 20261028) -> dict[str, Any]:
    """Generate 50 axial planes: HU(x,y,z)=-1000+2x+3y+10z, z spacing 2.5 mm."""
    profile = DatasetProfile(1, 1, 50, 64, 64, "CT", "explicit-vr-le", seed,
                             50, 8*1024*1024, "viewer-ct")
    return generate_dataset(profile, output)


_LARGE_CT_PIXEL_SPACING_MM = 0.8
_LARGE_CT_SLICE_SPACING_MM = 2.5
_LARGE_CT_SLICE_THICKNESS_MM = 0.625
_LARGE_CT_RESCALE_INTERCEPT = -1024
_LARGE_CT_HU_AIR = -1000
_LARGE_CT_HU_WATER = 0
_LARGE_CT_HU_SOFT_TISSUE = 40
_LARGE_CT_HU_BONE = 1000
_LARGE_CT_MAX_SLICES = 10000
_LARGE_CT_MIN_FREE_SPACE_BYTES = 256 * 1024 * 1024
_LARGE_CT_DISK_RESERVE_DIVISOR = 20


def _large_ct_lesions(slices: int) -> list[dict[str, float | int]]:
    """Define three reproducible spherical HU markers across the volume."""
    last_slice_z = (slices - 1) * _LARGE_CT_SLICE_SPACING_MM
    return [
        {"diameter_mm": 10.0, "center_x_mm": -55.0, "center_y_mm": -18.0,
         "center_z_mm": last_slice_z * 0.25, "hu": 120},
        {"diameter_mm": 20.0, "center_x_mm": 32.0, "center_y_mm": -24.0,
         "center_z_mm": last_slice_z * 0.50, "hu": 240},
        {"diameter_mm": 30.0, "center_x_mm": 48.0, "center_y_mm": 42.0,
         "center_z_mm": last_slice_z * 0.75, "hu": 360},
    ]


def _large_ct_xy_geometry(
    rows: int, cols: int
) -> tuple[float, float, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Precompute patient-plane coordinates and deterministic HU regions."""
    spacing = _LARGE_CT_PIXEL_SPACING_MM
    x0 = -((cols - 1) * spacing) / 2
    y0 = -((rows - 1) * spacing) / 2
    x_mm = x0 + np.arange(cols, dtype=np.float32) * spacing
    y_mm = y0 + np.arange(rows, dtype=np.float32) * spacing
    x_grid, y_grid = np.meshgrid(x_mm, y_mm)
    radius_squared = x_grid * x_grid + y_grid * y_grid
    return (x0, y0, x_grid, y_grid, radius_squared <= 170.0 ** 2,
            radius_squared <= 45.0 ** 2,
            (radius_squared >= 112.0 ** 2) & (radius_squared <= 118.0 ** 2))


def _large_ct_pixels(
    rows: int,
    cols: int,
    slice_index: int,
    lesion_specs: list[dict[str, float | int]],
    geometry: tuple[float, float, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
) -> np.ndarray:
    """Create one signed little-endian plane with exact stepped HU values."""
    _, _, x_grid, y_grid, body_mask, water_mask, bone_mask = geometry
    intercept = _LARGE_CT_RESCALE_INTERCEPT
    pixels = np.full((rows, cols), _LARGE_CT_HU_AIR - intercept, dtype="<i2")
    pixels[body_mask] = _LARGE_CT_HU_SOFT_TISSUE - intercept
    pixels[water_mask] = _LARGE_CT_HU_WATER - intercept
    pixels[bone_mask] = _LARGE_CT_HU_BONE - intercept
    z_mm = slice_index * _LARGE_CT_SLICE_SPACING_MM
    for lesion in lesion_specs:
        radius = float(lesion["diameter_mm"]) / 2
        dz = z_mm - float(lesion["center_z_mm"])
        if abs(dz) <= radius:
            lesion_mask = ((x_grid - float(lesion["center_x_mm"])) ** 2
                           + (y_grid - float(lesion["center_y_mm"])) ** 2
                           + dz * dz <= radius * radius)
            pixels[lesion_mask] = int(lesion["hu"]) - _LARGE_CT_RESCALE_INTERCEPT
    return pixels


def _large_ct_dataset(
    slice_index: int,
    rows: int,
    cols: int,
    x0: float,
    y0: float,
    study_uid: str,
    series_uid: str,
    frame_uid: str,
    seed: int,
    pixels: np.ndarray,
) -> FileDataset:
    """Build a DICOM CT instance with complete, aligned patient geometry."""
    sop_uid = _uid(seed, "large-ct-sop", slice_index)
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = _uid(seed, "large-ct-implementation")
    dataset = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = sop_uid
    dataset.StudyInstanceUID = study_uid
    dataset.SeriesInstanceUID = series_uid
    dataset.FrameOfReferenceUID = frame_uid
    dataset.Modality = "CT"
    dataset.PatientName = "SYNTHETIC^LARGE_CT"
    dataset.PatientID = "SYNTHETIC-LARGE-CT"
    dataset.PatientSex = "O"
    dataset.StudyDate = "20200101"
    dataset.StudyTime = "120000"
    dataset.StudyDescription = "SYNTHETIC 5500-SLICE CT PHANTOM - NOT FOR CLINICAL USE"
    dataset.SeriesDescription = "SYNTHETIC STEPPED-HU SPHERICAL LESION PHANTOM"
    dataset.SeriesNumber = 1
    dataset.InstanceNumber = slice_index + 1
    dataset.ImageType = ["ORIGINAL", "PRIMARY", "SYNTHETIC"]
    dataset.PatientPosition = "HFS"
    dataset.Rows = rows
    dataset.Columns = cols
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 1
    dataset.PixelSpacing = [str(_LARGE_CT_PIXEL_SPACING_MM)] * 2
    dataset.ImageOrientationPatient = ["1", "0", "0", "0", "1", "0"]
    dataset.ImagePositionPatient = [str(x0), str(y0), str(slice_index * _LARGE_CT_SLICE_SPACING_MM)]
    dataset.SliceThickness = str(_LARGE_CT_SLICE_THICKNESS_MM)
    dataset.SpacingBetweenSlices = str(_LARGE_CT_SLICE_SPACING_MM)
    dataset.RescaleSlope = "1"
    dataset.RescaleIntercept = str(_LARGE_CT_RESCALE_INTERCEPT)
    dataset.RescaleType = "HU"
    dataset.WindowCenter = "40"
    dataset.WindowWidth = "400"
    dataset.PixelData = pixels.tobytes()
    return dataset


def _large_ct_manifest(
    slices: int,
    rows: int,
    cols: int,
    seed: int,
    study_uid: str,
    series_uid: str,
    frame_uid: str,
    lesion_specs: list[dict[str, float | int]],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe the deterministic fixture without adding run-time metadata."""
    return {
        "format": "paxs-synthetic-large-ct-v1", "phi_free": True,
        "not_for_clinical_use": True, "seed": seed, "study_uid": study_uid,
        "series_uid": series_uid, "frame_of_reference_uid": frame_uid,
        "geometry": {"slices": slices, "rows": rows, "columns": cols,
                     "pixel_spacing_mm": [_LARGE_CT_PIXEL_SPACING_MM] * 2,
                     "slice_thickness_mm": _LARGE_CT_SLICE_THICKNESS_MM,
                     "spacing_between_slices_mm": _LARGE_CT_SLICE_SPACING_MM,
                     "image_orientation_patient": [1, 0, 0, 0, 1, 0],
                     "rescale_slope": 1, "rescale_intercept": _LARGE_CT_RESCALE_INTERCEPT,
                     "pixel_representation": "signed 16-bit little-endian"},
        "phantom_hu": {"air": _LARGE_CT_HU_AIR, "water": _LARGE_CT_HU_WATER,
                       "soft_tissue": _LARGE_CT_HU_SOFT_TISSUE, "bone": _LARGE_CT_HU_BONE},
        "spherical_lesions": lesion_specs,
        "counts": {"studies": 1, "series": 1, "instances": slices,
                   "pixel_bytes": slices * rows * cols * 2,
                   "file_bytes": sum(record["bytes"] for record in records)},
        "instances": records,
    }


def generate_large_synthetic_ct(
    slices: int = 5500,
    rows: int = 512,
    cols: int = 512,
    *,
    output: Path,
    seed: int = 20261031,
) -> dict[str, Any]:
    """Write a deterministic PHI-free CT phantom, streaming one plane at a time."""
    if min(slices, rows, cols) < 1 or rows > _MAX_DIMENSION or cols > _MAX_DIMENSION:
        raise ValueError("slices must be positive and rows/cols must be within the supported dimensions")
    if slices > _LARGE_CT_MAX_SLICES:
        raise ValueError(f"slices cannot exceed the {_LARGE_CT_MAX_SLICES}-instance benchmark safety limit")
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite synthetic output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload_bytes = slices * rows * cols * 2
    required_bytes = payload_bytes + max(
        _LARGE_CT_MIN_FREE_SPACE_BYTES, payload_bytes // _LARGE_CT_DISK_RESERVE_DIVISOR
    )
    if shutil.disk_usage(output.parent).free < required_bytes:
        raise OSError(f"insufficient free disk space for synthetic dataset ({required_bytes} bytes required)")

    study_uid, series_uid, frame_uid = (
        _uid(seed, "large-ct-study"), _uid(seed, "large-ct-series"), _uid(seed, "large-ct-frame")
    )
    geometry = _large_ct_xy_geometry(rows, cols)
    x0, y0 = geometry[:2]
    lesion_specs = _large_ct_lesions(slices)
    output.mkdir(parents=True, exist_ok=False)
    series_dir = output / "study-000001" / "series-001"
    series_dir.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    try:
        for slice_index in range(slices):
            pixels = _large_ct_pixels(rows, cols, slice_index, lesion_specs, geometry)
            dataset = _large_ct_dataset(slice_index, rows, cols, x0, y0, study_uid,
                                        series_uid, frame_uid, seed, pixels)
            file_path = series_dir / f"instance-{slice_index + 1:05d}.dcm"
            size, digest = _write_instance(dataset, file_path)
            records.append({"path": file_path.relative_to(output).as_posix(),
                            "bytes": size, "sha256": digest, "instance_number": slice_index + 1})
    except (OSError, ValueError):
        shutil.rmtree(output)
        raise

    manifest = _large_ct_manifest(slices, rows, cols, seed, study_uid, series_uid,
                                  frame_uid, lesion_specs, records)
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def generate_hanging_protocol_fixtures(output: Path, seed: int = 20261030) -> dict[str, Any]:
    """Generate small synthetic CT and MR studies for layout matching and synchronization."""
    output.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    studies: dict[str, str] = {}
    profiles = {
        "CT": (DatasetProfile(1, 2, 4, 64, 64, "CT", "explicit-vr-le", seed,
                               8, 2*1024*1024), ["SYNTHETIC AXIAL LUNG", "SYNTHETIC AXIAL MEDIASTINUM"], "CHEST"),
        "MR": (DatasetProfile(1, 4, 2, 64, 64, "MR", "explicit-vr-le", seed + 1,
                               8, 2*1024*1024), ["SYNTHETIC T1 AXIAL", "SYNTHETIC T2 AXIAL",
                                                  "SYNTHETIC PD AXIAL", "SYNTHETIC T2 SAGITTAL"], "KNEE"),
    }
    for modality, (profile, series_descriptions, body_part) in profiles.items():
        modality_dir = output / modality.lower()
        modality_dir.mkdir()
        frame_uid = _uid(seed, "protocol-frame", 0 if modality == "CT" else 1)
        for series_index, series_description in enumerate(series_descriptions):
            series_dir = modality_dir / f"series-{series_index + 1:02d}"
            series_dir.mkdir()
            for instance_index in range(profile.instances_per_series):
                dataset = _build_instance(profile, 0, series_index, instance_index)
                dataset.StudyInstanceUID = _uid(seed, "protocol-study", 0 if modality == "CT" else 1)
                dataset.SeriesInstanceUID = _uid(seed, f"protocol-series-{modality}", series_index)
                dataset.SOPInstanceUID = _uid(seed, f"protocol-sop-{modality}", series_index, instance_index)
                dataset.file_meta.MediaStorageSOPInstanceUID = dataset.SOPInstanceUID
                dataset.SeriesDescription = series_description
                dataset.StudyDescription = f"SYNTHETIC {modality} {body_part} hanging protocol study"
                dataset.BodyPartExamined = body_part
                dataset.FrameOfReferenceUID = frame_uid
                dataset.InstanceNumber = instance_index + 1
                dataset.ImagePositionPatient = ["0", "0", str(instance_index * 1.5)]
                if modality == "MR":
                    del dataset.RescaleSlope, dataset.RescaleIntercept
                    if "SAGITTAL" in series_description:
                        dataset.ImageOrientationPatient = ["0", "1", "0", "0", "0", "1"]
                path = series_dir / f"instance-{instance_index + 1:02d}.dcm"
                size, digest = _write_instance(dataset, path)
                records.append({"path": path.relative_to(output).as_posix(), "bytes": size,
                                "sha256": digest, "modality": modality,
                                "series_description": series_description})
        studies[modality] = _uid(seed, "protocol-study", 0 if modality == "CT" else 1)
    return {"studies": studies, "instances": records}


def _rgb_reference() -> np.ndarray:
    """Return exact synthetic red, green, blue, and white test bands."""
    pixels = np.zeros((64, 64, 3), dtype=np.uint8)
    pixels[:, :16] = [255, 0, 0]
    pixels[:, 16:32] = [0, 255, 0]
    pixels[:, 32:48] = [0, 0, 255]
    pixels[:, 48:] = [255, 255, 255]
    return pixels


def _ybr422_reference(rgb: np.ndarray) -> bytes:
    """Pack 8-bit YBR_FULL_422 pairs as Y1, Y2, Cb, Cr."""
    source = rgb.astype(np.float64)
    red, green, blue = source[..., 0], source[..., 1], source[..., 2]
    y = np.rint(0.299 * red + 0.587 * green + 0.114 * blue)
    cb = np.rint(128 - 0.168736 * red - 0.331264 * green + 0.5 * blue)
    cr = np.rint(128 + 0.5 * red - 0.418688 * green - 0.081312 * blue)
    packed = np.empty((rgb.shape[0], rgb.shape[1] // 2, 4), dtype=np.uint8)
    packed[..., 0] = np.clip(y[:, 0::2], 0, 255)
    packed[..., 1] = np.clip(y[:, 1::2], 0, 255)
    packed[..., 2] = np.clip(cb[:, 0::2], 0, 255)
    packed[..., 3] = np.clip(cr[:, 0::2], 0, 255)
    return packed.tobytes()


def _reference_case(case: str, index: int) -> FileDataset:
    """Construct one deterministic synthetic pixel-pipeline boundary object."""
    profile = DatasetProfile(1, 1, 1, 64, 64, "CT", "explicit-vr-le", 20261029,
                             4, 1024*1024, "viewer-ct")
    ds = _build_instance(profile, 100 + index, 0, 0)
    ds.SeriesDescription = f"SYNTHETIC Pixel Case {case}"
    pixels = np.tile(np.repeat([24, 1024, 1064, 2024], 16), (64, 1)).astype("<i2")
    if case == "A":
        ds.PixelRepresentation, ds.BitsStored, ds.HighBit = 1, 12, 11
        ds.PixelPaddingValue, ds.PixelPaddingRangeLimit = -2000, -1990
        pixels[:2, :] = -2000
        del ds.WindowCenter, ds.WindowWidth
    elif case == "B":
        ds.Modality, ds.SOPClassUID = "CR", ComputedRadiographyImageStorage
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        ds.BitsStored, ds.HighBit = 12, 11
        ds.PhotometricInterpretation = "MONOCHROME1"
        ds.WindowCenter, ds.WindowWidth = 2048, 4096
        pixels = np.tile(np.repeat([0, 4095], 32), (64, 1)).astype("<u2")
        del ds.RescaleSlope, ds.RescaleIntercept
    elif case == "C":
        ds.WindowCenter, ds.WindowWidth = [40, 400, 75], [80, 1800, 215]
        ds.WindowCenterWidthExplanation = ["Brain", "Bone", "Subdural"]
    elif case == "C_ENDPOINTS":
        ds.WindowCenter, ds.WindowWidth = 0, 100
        pixels = np.tile(np.repeat([974, 1024, 1073, 1074], 16), (64, 1)).astype("<i2")
    elif case == "D":
        ds.Modality, ds.SOPClassUID = "MR", MRImageStorage
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        del ds.RescaleSlope, ds.RescaleIntercept
    elif case == "D_US":
        ds.Modality, ds.SOPClassUID = "US", UltrasoundImageStorage
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        del ds.RescaleSlope, ds.RescaleIntercept
    elif case == "C_SIGMOID":
        ds.WindowCenter, ds.WindowWidth, ds.VOILUTFunction = 40, 80, "SIGMOID"
    elif case == "C_MODLUT":
        ds.PixelRepresentation, ds.BitsStored, ds.HighBit = 0, 12, 11
        pixels = np.tile(np.repeat([0, 1, 2, 3], 16), (64, 1)).astype("<u2")
        del ds.RescaleSlope, ds.RescaleIntercept
        if "RescaleType" in ds:
            del ds.RescaleType
        del ds.WindowCenter, ds.WindowWidth
        modality_lut, voi_lut = Dataset(), Dataset()
        modality_lut.LUTDescriptor = [4, 0, 16]
        modality_lut.LUTData = np.array([0, 1, 2, 3], dtype="<u2").tobytes()
        modality_lut.ModalityLUTType = "US"
        voi_lut.LUTDescriptor = [4, 0, 8]
        voi_lut.LUTData = np.array([0, 64, 128, 255], dtype="<u2").tobytes()
        ds.ModalityLUTSequence = Sequence([modality_lut])
        ds.VOILUTSequence = Sequence([voi_lut])
        ds.PresentationLUTShape = "INVERSE"
    elif case == "E":
        ds.RescaleType = "RELATIVE"
        ds.RescaleSlope, ds.RescaleIntercept = "2", "10"
    elif case == "F":
        ds.Modality, ds.SOPClassUID = "PT", PositronEmissionTomographyImageStorage
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        ds.RescaleType, ds.RescaleSlope, ds.RescaleIntercept = "BQML", "2", "0"
        ds.Units = "BQML"
    elif case in ("G_RGB", "G_YBR422"):
        ds.Modality, ds.SOPClassUID = "OT", SecondaryCaptureImageStorage
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        ds.SamplesPerPixel, ds.PlanarConfiguration = 3, 0
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 8, 8, 7, 0
        rgb = _rgb_reference()
        if case == "G_RGB":
            ds.PhotometricInterpretation = "RGB"
            pixels = rgb
        else:
            ds.PhotometricInterpretation = "YBR_FULL_422"
            pixels = None
    elif case == "G_PALETTE":
        ds.Modality, ds.SOPClassUID = "OT", SecondaryCaptureImageStorage
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        ds.SamplesPerPixel, ds.PlanarConfiguration = 1, 0
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 8, 8, 7, 0
        ds.PhotometricInterpretation = "PALETTE COLOR"
        pixels = np.tile(np.repeat([0, 1, 2, 3], 16), (64, 1)).astype(np.uint8)
        for channel, values in (("Red", [255, 0, 0, 255]),
                                ("Green", [0, 255, 0, 255]),
                                ("Blue", [0, 0, 255, 255])):
            setattr(ds, f"{channel}PaletteColorLookupTableDescriptor", [4, 0, 8])
            setattr(ds, f"{channel}PaletteColorLookupTableData", bytes(values))
    elif case == "H":
        ds.Modality, ds.SOPClassUID = "OT", MultiFrameGrayscaleWordSecondaryCaptureImageStorage
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        ds.SamplesPerPixel, ds.PlanarConfiguration = 1, 0
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
        ds.NumberOfFrames, ds.FrameTime = "2", "1000"
        ds.FrameIncrementPointer = [0x00181063]
        ds.WindowCenter, ds.WindowWidth = 2048, 4096
        frame_one = np.tile(np.repeat([0, 512, 1024, 1536], 16), (64, 1)).astype("<u2")
        frame_two = np.tile(np.repeat([2560, 3072, 3584, 4095], 16), (64, 1)).astype("<u2")
        pixels = np.stack([frame_one, frame_two])
    if case == "G_YBR422":
        ds.PixelData = _ybr422_reference(rgb)
    else:
        ds.PixelData = pixels.tobytes()
    return ds


def generate_pixel_reference_cases(output: Path) -> dict[str, dict[str, Any]]:
    """Write deterministic PHI-free grayscale, color, LUT, and multi-frame fixtures."""
    output.mkdir(parents=True, exist_ok=False)
    result: dict[str, dict[str, Any]] = {}
    case_names = ("A", "B", "C", "C_ENDPOINTS", "D", "D_US", "C_SIGMOID", "C_MODLUT",
                  "E", "F", "G_RGB", "G_YBR422", "G_PALETTE", "H")
    for index, case in enumerate(case_names):
        ds = _reference_case(case, index)
        name = f"case-{case.replace('_', '-')}.dcm"
        size, digest = _write_instance(ds, output / name)
        result[case] = {"path": name, "bytes": size, "sha256": digest,
                       "studyUID": str(ds.StudyInstanceUID), "seriesUID": str(ds.SeriesInstanceUID),
                       "sopUID": str(ds.SOPInstanceUID)}
    return result


def _parser() -> argparse.ArgumentParser:
    """Build the bounded dataset generator command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new output directory; existing paths are refused")
    parser.add_argument("--studies", type=int)
    parser.add_argument("--series-per-study", type=int, default=1)
    parser.add_argument("--instances-per-series", type=int, default=16)
    parser.add_argument("--rows", type=int, default=512)
    parser.add_argument("--columns", type=int, default=512)
    parser.add_argument("--modality", choices=sorted(_SOP_CLASSES), default="CT")
    parser.add_argument("--transfer-syntax", choices=sorted(_TRANSFER_SYNTAXES),
                        default="explicit-vr-le")
    parser.add_argument("--seed", type=int, default=20261026)
    parser.add_argument("--max-instances", type=int, default=_DEFAULT_MAX_INSTANCES)
    parser.add_argument("--max-total-bytes", type=int, default=_DEFAULT_MAX_BYTES,
                        help="estimated uncompressed output cap; zero disables this byte cap")
    parser.add_argument("--large-ct", action="store_true",
                        help="generate the deterministic large synthetic signed CT phantom")
    parser.add_argument("--slices", type=int, default=5500)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Validate CLI inputs and generate the requested non-clinical profile."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.large_ct:
            manifest = generate_large_synthetic_ct(args.slices, args.rows, args.columns,
                                                   output=args.output, seed=args.seed)
        else:
            if args.studies is None:
                parser.error("--studies is required unless --large-ct is selected")
            profile = _validate_profile(args)
            manifest = generate_dataset(profile, args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(manifest["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
