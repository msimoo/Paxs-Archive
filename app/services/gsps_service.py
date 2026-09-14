"""GSPS (Grayscale Softcopy Presentation State) service.

Parses GSPS DICOM objects (SOP Class 1.2.840.10008.5.1.4.1.1.11.1),
extracts display parameters (VOI LUT, spatial transforms, annotations,
shutters), indexes them in the database, and applies them to referenced
images for the WADO-RS /rendered endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pydicom.dataset import Dataset
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PresentationState

log = logging.getLogger("paxs.gsps")

# GSPS SOP Class UID
GSPS_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.11.1"


# --------------------------------------------------------------------------- #
# Data classes for parsed GSPS content
# --------------------------------------------------------------------------- #
@dataclass
class GraphicObject:
    """A vector graphic annotation (POLYLINE, CIRCLE, ELLIPSE, POINT)."""
    graphic_type: str          # POINT | POLYLINE | INTERPOLATED | CIRCLE | ELLIPSE
    data: list[float]          # Flat coordinate pairs [x1,y1,x2,y2,...]
    units: str = "PIXEL"       # PIXEL or DISPLAY
    filled: bool = False


@dataclass
class TextObject:
    """A text annotation."""
    text: str
    anchor_point: tuple[float, float] | None = None
    bounding_box_tlhc: tuple[float, float] | None = None
    bounding_box_brhc: tuple[float, float] | None = None


@dataclass
class DisplayShutter:
    """Display shutter (rectangular, circular, or polygonal)."""
    shape: str                 # RECTANGULAR | CIRCULAR | POLYGONAL
    # Rectangular
    left: int = 0
    right: int = 0
    top: int = 0
    bottom: int = 0
    # Circular
    center: tuple[int, int] = (0, 0)
    radius: int = 0
    # Polygonal
    vertices: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class GSPSContent:
    """Parsed content of a GSPS object."""
    # VOI LUT (window/level)
    window_center: float | None = None
    window_width: float | None = None
    # Spatial transform
    rotation: int = 0              # degrees: 0, 90, 180, 270
    horizontal_flip: bool = False
    # Annotations
    graphic_annotations: list[GraphicObject] = field(default_factory=list)
    text_annotations: list[TextObject] = field(default_factory=list)
    # Shutters
    shutters: list[DisplayShutter] = field(default_factory=list)
    # Referenced SOP Instance UIDs
    referenced_sop_uids: list[str] = field(default_factory=list)
    # Metadata
    presentation_label: str | None = None
    presentation_description: str | None = None


# --------------------------------------------------------------------------- #
# Parser: DICOM Dataset -> GSPSContent
# --------------------------------------------------------------------------- #
def parse_gsps(ds: Dataset) -> GSPSContent:
    """Extract display parameters from a GSPS DICOM dataset."""
    content = GSPSContent()

    # -- Presentation metadata ------------------------------------------------
    # pydicom >= 3.x keywords: the DICOM GSPS IOD calls these "Presentation
    # Label/Description/Creator", but the official element keywords are
    # ContentLabel/ContentDescription/ContentCreatorName (0070,0080/81/84).
    content.presentation_label = _str(ds, "ContentLabel")
    content.presentation_description = _str(ds, "ContentDescription")

    # -- Softcopy VOI LUT Sequence (0028,3110) --------------------------------
    voi_seq = getattr(ds, "SoftcopyVOILUTSequence", None)
    if voi_seq and len(voi_seq) > 0:
        item = voi_seq[0]
        wc = getattr(item, "WindowCenter", None)
        ww = getattr(item, "WindowWidth", None)
        content.window_center = _first_float(wc)
        content.window_width = _first_float(ww)
    # Fallback to dataset-level W/L if no sequence
    if content.window_center is None:
        content.window_center = _first_float(getattr(ds, "WindowCenter", None))
    if content.window_width is None:
        content.window_width = _first_float(getattr(ds, "WindowWidth", None))

    # -- Spatial Transformation (0070,0041/0042/0044) -------------------------
    spatial_seq = getattr(ds, "SpatialTransformationSequence", None)
    if spatial_seq and len(spatial_seq) > 0:
        item = spatial_seq[0]
        content.rotation = int(getattr(item, "ImageRotation", 0) or 0)
        flip = getattr(item, "ImageHorizontalFlip", "N")
        content.horizontal_flip = str(flip).upper() == "Y"

    # -- Graphic Annotation Sequence (0070,0001) ------------------------------
    ga_seq = getattr(ds, "GraphicAnnotationSequence", None)
    if ga_seq:
        for ga_item in ga_seq:
            # Text objects
            text_seq = getattr(ga_item, "TextObjectSequence", None)
            if text_seq:
                for t in text_seq:
                    text = str(getattr(t, "UnformattedTextValue", ""))
                    anchor = None
                    ap = getattr(t, "AnchorPoint", None)
                    if ap and len(ap) >= 2:
                        anchor = (float(ap[0]), float(ap[1]))
                    bb_tlhc = None
                    bb_brhc = None
                    tlhc = getattr(t, "BoundingBoxTopLeftHandCorner", None)
                    brhc = getattr(t, "BoundingBoxBottomRightHandCorner", None)
                    if tlhc and len(tlhc) >= 2:
                        bb_tlhc = (float(tlhc[0]), float(tlhc[1]))
                    if brhc and len(brhc) >= 2:
                        bb_brhc = (float(brhc[0]), float(brhc[1]))
                    content.text_annotations.append(
                        TextObject(text=text, anchor_point=anchor,
                                   bounding_box_tlhc=bb_tlhc,
                                   bounding_box_brhc=bb_brhc)
                    )
            # Graphic objects
            graphic_seq = getattr(ga_item, "GraphicObjectSequence", None)
            if graphic_seq:
                for g in graphic_seq:
                    gtype = str(getattr(g, "GraphicType", "POLYLINE")).upper()
                    data_raw = getattr(g, "GraphicData", [])
                    data = [float(x) for x in data_raw]
                    units = str(getattr(g, "GraphicAnnotationUnits", "PIXEL")).upper()
                    filled = str(getattr(g, "GraphicFilled", "N")).upper() == "Y"
                    content.graphic_annotations.append(
                        GraphicObject(graphic_type=gtype, data=data,
                                      units=units, filled=filled)
                    )

    # -- Display Shutter Sequence (0018,1600) ---------------------------------
    shutter_shape = _str(ds, "ShutterShape")
    if shutter_shape:
        for shape in shutter_shape.split("\\"):
            shape = shape.strip().upper()
            shutter = DisplayShutter(shape=shape)
            if shape == "RECTANGULAR":
                shutter.top = int(getattr(ds, "ShutterUpperHorizontalEdge", 0) or 0)
                shutter.bottom = int(getattr(ds, "ShutterLowerHorizontalEdge", 0) or 0)
                shutter.left = int(getattr(ds, "ShutterLeftVerticalEdge", 0) or 0)
                shutter.right = int(getattr(ds, "ShutterRightVerticalEdge", 0) or 0)
            elif shape == "CIRCULAR":
                center = getattr(ds, "CenterOfCircularShutter", None)
                if center and len(center) >= 2:
                    shutter.center = (int(center[0]), int(center[1]))
                shutter.radius = int(getattr(ds, "RadiusOfCircularShutter", 0) or 0)
            elif shape == "POLYGONAL":
                verts = getattr(ds, "VerticesOfThePolygonalShutter", [])
                pairs = [(int(verts[i]), int(verts[i + 1]))
                         for i in range(0, len(verts) - 1, 2)]
                shutter.vertices = pairs
            content.shutters.append(shutter)

    # -- Referenced Series Sequence (0008,1115) -> Referenced Image Seq -------
    ref_series_seq = getattr(ds, "ReferencedSeriesSequence", None)
    if ref_series_seq:
        for rs in ref_series_seq:
            ref_img_seq = getattr(rs, "ReferencedImageSequence", None)
            if ref_img_seq:
                for ri in ref_img_seq:
                    uid = _str(ri, "ReferencedSOPInstanceUID")
                    if uid:
                        content.referenced_sop_uids.append(uid)

    return content


# --------------------------------------------------------------------------- #
# Database operations
# --------------------------------------------------------------------------- #
async def index_presentation_state(
    session: AsyncSession,
    meta: dict,
    ds: Dataset,
    file_path: str,
) -> PresentationState | None:
    """Parse a GSPS dataset and upsert a PresentationState row.

    Called by the indexing pipeline when the incoming SOP Class UID matches
    the GSPS SOP Class (1.2.840.10008.5.1.4.1.1.11.1).
    """
    sop_uid = meta.get("sop_instance_uid") or ""
    if not sop_uid:
        return None

    content = parse_gsps(ds)

    # Comma-separated referenced SOP UIDs for fast DB lookups.
    ref_uids_str = ",".join(content.referenced_sop_uids) if content.referenced_sop_uids else None

    existing = (
        await session.execute(
            select(PresentationState).where(PresentationState.sop_instance_uid == sop_uid)
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.file_path = file_path
        existing.window_center = str(content.window_center) if content.window_center is not None else None
        existing.window_width = str(content.window_width) if content.window_width is not None else None
        existing.rotation = content.rotation
        existing.horizontal_flip = content.horizontal_flip
        existing.referenced_sop_instance_uids = ref_uids_str
        existing.presentation_label = content.presentation_label
        existing.presentation_description = content.presentation_description
        await session.flush()
        log.info("GSPS updated: %s (refs: %d images)", sop_uid, len(content.referenced_sop_uids))
        return existing

    ps = PresentationState(
        sop_instance_uid=sop_uid,
        study_instance_uid=meta.get("study_instance_uid") or "",
        series_instance_uid=meta.get("series_instance_uid") or "",
        presentation_label=content.presentation_label,
        presentation_description=content.presentation_description,
        presentation_creation_date=_str(ds, "PresentationCreationDate"),
        presentation_creation_time=_str(ds, "PresentationCreationTime"),
        presentation_creator=_str(ds, "ContentCreatorName"),
        window_center=str(content.window_center) if content.window_center is not None else None,
        window_width=str(content.window_width) if content.window_width is not None else None,
        rotation=content.rotation,
        horizontal_flip=content.horizontal_flip,
        referenced_sop_instance_uids=ref_uids_str,
        file_path=file_path,
    )
    session.add(ps)
    await session.flush()
    log.info("GSPS indexed: %s (refs: %d images)", sop_uid, len(content.referenced_sop_uids))
    return ps


async def find_for_instance(
    session: AsyncSession,
    sop_instance_uid: str,
    study_instance_uid: str | None = None,
) -> list[PresentationState]:
    """Find all GSPS objects that reference a given image instance."""
    stmt = select(PresentationState).where(
        PresentationState.referenced_sop_instance_uids.contains(sop_instance_uid)
    )
    if study_instance_uid:
        stmt = stmt.where(PresentationState.study_instance_uid == study_instance_uid)
    stmt = stmt.order_by(PresentationState.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_by_uid(
    session: AsyncSession,
    sop_instance_uid: str,
) -> PresentationState | None:
    """Retrieve a single GSPS by its SOP Instance UID."""
    return (
        await session.execute(
            select(PresentationState).where(
                PresentationState.sop_instance_uid == sop_instance_uid
            )
        )
    ).scalar_one_or_none()


async def list_for_study(
    session: AsyncSession,
    study_instance_uid: str,
) -> list[PresentationState]:
    """List all GSPS objects for a study."""
    return list(
        (
            await session.execute(
                select(PresentationState)
                .where(PresentationState.study_instance_uid == study_instance_uid)
                .order_by(PresentationState.created_at.desc())
            )
        ).scalars().all()
    )


# --------------------------------------------------------------------------- #
# Image rendering with GSPS applied
# --------------------------------------------------------------------------- #
def apply_gsps_to_image(
    img: Image.Image,
    content: GSPSContent,
) -> Image.Image:
    """Apply GSPS display parameters to a PIL Image.

    Operations applied in DICOM-standard order:
      1. Spatial transforms (rotation, flip)
      2. Display shutters
      3. Graphic annotations (lines, circles, ellipses, text)
    
    VOI LUT (window/level) is applied *before* this function, during pixel
    data extraction in the imaging service.
    """
    # 1. Spatial transform
    if content.horizontal_flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if content.rotation:
        # PIL rotates counter-clockwise; DICOM specifies clockwise.
        img = img.rotate(-content.rotation, expand=True)

    # 2. Display shutters — black out regions outside the shutter.
    for shutter in content.shutters:
        _apply_shutter(img, shutter)

    # 3. Graphic annotations — draw on an overlay.
    if content.graphic_annotations or content.text_annotations:
        # Convert to RGB for colored annotations on grayscale images.
        if img.mode == "L":
            img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        _ANNOTATION_COLOR = (255, 165, 0)  # Orange
        _TEXT_COLOR = (255, 255, 100)       # Yellow

        for g in content.graphic_annotations:
            _draw_graphic(draw, g, _ANNOTATION_COLOR)

        for t in content.text_annotations:
            _draw_text(draw, t, _TEXT_COLOR)

    return img


def _apply_shutter(img: Image.Image, shutter: DisplayShutter) -> None:
    """Black out regions outside the shutter opening."""
    w, h = img.size
    mask = Image.new("L", (w, h), 0)  # Start fully black
    draw = ImageDraw.Draw(mask)

    if shutter.shape == "RECTANGULAR":
        draw.rectangle(
            [shutter.left, shutter.top, shutter.right, shutter.bottom],
            fill=255,
        )
    elif shutter.shape == "CIRCULAR":
        cx, cy = shutter.center
        r = shutter.radius
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    elif shutter.shape == "POLYGONAL" and shutter.vertices:
        draw.polygon(shutter.vertices, fill=255)

    # Composite: keep pixels inside the shutter, black outside.
    black = Image.new(img.mode, (w, h), 0)
    img.paste(black, mask=Image.fromarray(255 - np.array(mask)))


def _draw_graphic(draw: ImageDraw.Draw, g: GraphicObject, color: tuple) -> None:
    """Draw a DICOM graphic annotation on the image."""
    coords = g.data
    width = 2

    if g.graphic_type == "POINT" and len(coords) >= 2:
        x, y = coords[0], coords[1]
        r = 3
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)

    elif g.graphic_type in ("POLYLINE", "INTERPOLATED") and len(coords) >= 4:
        points = [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]
        draw.line(points, fill=color, width=width)

    elif g.graphic_type == "CIRCLE" and len(coords) >= 4:
        cx, cy = coords[0], coords[1]
        ex, ey = coords[2], coords[3]
        r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=width)

    elif g.graphic_type == "ELLIPSE" and len(coords) >= 8:
        # DICOM ellipse: 4 points (major axis start/end, minor axis start/end)
        xs = [coords[i] for i in range(0, 8, 2)]
        ys = [coords[i] for i in range(1, 8, 2)]
        bbox = [min(xs), min(ys), max(xs), max(ys)]
        draw.ellipse(bbox, outline=color, width=width)


def _draw_text(draw: ImageDraw.Draw, t: TextObject, color: tuple) -> None:
    """Draw a DICOM text annotation on the image."""
    if not t.text:
        return
    pos = t.anchor_point
    if pos is None and t.bounding_box_tlhc:
        pos = t.bounding_box_tlhc
    if pos is None:
        pos = (10, 10)  # Fallback
    try:
        font = ImageFont.truetype("arial", 14)
    except (IOError, OSError):
        font = ImageFont.load_default()
    draw.text(pos, t.text, fill=color, font=font)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _str(ds: Dataset, keyword: str) -> str | None:
    val = getattr(ds, keyword, None)
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _first_float(val) -> float | None:
    """Extract a float from a DICOM value that may be a list or DSfloat."""
    if val is None:
        return None
    if hasattr(val, "__iter__") and not isinstance(val, str):
        try:
            return float(next(iter(val)))
        except (StopIteration, TypeError, ValueError):
            return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
