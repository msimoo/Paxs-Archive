"""Server-side Diagnostic Report PDF generator (PDF 1.4, zero extra dependencies).

Produces a clean, multi-page A4 clinical radiology report PDF using Python's
standard library plus Pillow (already required by PAXS Archive) for optional
institution logo embedding and non-WinAnsi Unicode fallback rendering.

Supports:
  * Institutional header banner (logo + organization name + contact line)
  * Structured patient & study demographics card
  * Sanitized rich-text Findings and Impression (headings, bold, italic,
    underline, strikethrough, font family/size, colors, bulleted/numbered
    lists, callout quotes, horizontal rules, alignment) and legacy plain text
  * Chronological Addenda chain (for finalized reports)
  * Signature / status footer block and per-page running footer (Page X of Y)
"""

from __future__ import annotations

import io
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.db.models import OrganizationSettings
from app.services.report_html import (
    html_to_plain_text,
    looks_like_rich_html,
    sanitize_report_html,
)
from app.services.report_service import resolve_org_logo_path

PAGE_W = 595.28
PAGE_H = 841.89
MARGIN_X = 44.0
MARGIN_TOP = 46.0
MARGIN_BOTTOM = 56.0
CONTENT_W = PAGE_W - (2.0 * MARGIN_X)

_APP_DIR = Path(__file__).resolve().parents[1]

# Standard PDF Type1 font aliases mapped to resource names F1..F10.
_PDF_FONTS: tuple[tuple[str, str], ...] = (
    ("F1", "Helvetica"),
    ("F2", "Helvetica-Bold"),
    ("F3", "Helvetica-Oblique"),
    ("F4", "Helvetica-BoldOblique"),
    ("F5", "Times-Roman"),
    ("F6", "Times-Bold"),
    ("F7", "Times-Oblique"),
    ("F8", "Times-BoldOblique"),
    ("F9", "Courier"),
    ("F10", "Courier-Bold"),
)

_NAMED_RGB: dict[str, tuple[float, float, float]] = {
    "black": (0.06, 0.09, 0.16),
    "gray": (0.39, 0.45, 0.55),
    "grey": (0.39, 0.45, 0.55),
    "red": (0.86, 0.15, 0.15),
    "darkred": (0.60, 0.10, 0.10),
    "crimson": (0.86, 0.08, 0.24),
    "orange": (0.85, 0.47, 0.02),
    "amber": (0.71, 0.33, 0.04),
    "green": (0.02, 0.59, 0.41),
    "darkgreen": (0.04, 0.42, 0.28),
    "teal": (0.05, 0.58, 0.53),
    "blue": (0.01, 0.52, 0.78),
    "darkblue": (0.11, 0.31, 0.65),
    "navy": (0.10, 0.20, 0.45),
    "indigo": (0.31, 0.27, 0.90),
    "purple": (0.49, 0.23, 0.93),
    "violet": (0.49, 0.23, 0.93),
    "brown": (0.47, 0.27, 0.14),
}

_INK_DARK = (0.06, 0.09, 0.16)
_INK_MUTED = (0.35, 0.42, 0.52)
_INK_FAINT = (0.55, 0.62, 0.71)
_INK_ACCENT = (0.01, 0.52, 0.78)
_INK_SECTION = (0.01, 0.41, 0.63)
_INK_OK = (0.09, 0.64, 0.29)
_INK_WARN = (0.71, 0.33, 0.04)

_SYSTEM_UNICODE_FONTS: tuple[str, ...] = (
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


@dataclass
class InlineStyle:
    """Resolved inline styling for a span of text inside a PDF paragraph."""

    family: str = "sans"  # sans | serif | mono
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    size: float = 10.0
    color: tuple[float, float, float] = _INK_DARK


@dataclass
class TextRun:
    """One styled piece of text inside a paragraph."""

    text: str
    style: InlineStyle


@dataclass
class RichBlock:
    """A parsed block element (paragraph, heading, list item, quote, divider)."""

    kind: str = "p"  # p | h1 | h2 | h3 | h4 | li | blockquote | hr
    runs: list[TextRun] = field(default_factory=list)
    align: str = "left"  # left | center | right | justify
    prefix: str = ""
    indent: float = 0.0


@dataclass
class EmbeddedImage:
    """JPEG image payload prepared for PDF XObject embedding."""

    name: str
    width_px: int
    height_px: int
    jpeg_bytes: bytes


def _font_key(style: InlineStyle) -> str:
    if style.family == "mono":
        return "F10" if style.bold else "F9"
    if style.family == "serif":
        if style.bold and style.italic:
            return "F8"
        if style.bold:
            return "F6"
        if style.italic:
            return "F7"
        return "F5"
    if style.bold and style.italic:
        return "F4"
    if style.bold:
        return "F2"
    if style.italic:
        return "F3"
    return "F1"


def _parse_rgb(raw: str | None) -> tuple[float, float, float] | None:
    if not raw:
        return None
    val = raw.strip().lower()
    if val in _NAMED_RGB:
        return _NAMED_RGB[val]
    m_hex = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", val)
    if m_hex:
        h = m_hex.group(1)
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        r, g, b = int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0
        # Avoid near-white ink on white paper.
        if r > 0.88 and g > 0.88 and b > 0.88:
            return _INK_DARK
        return (r, g, b)
    m_rgb = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,[^)]+)?\)", val)
    if m_rgb:
        r = min(255, int(m_rgb.group(1))) / 255.0
        g = min(255, int(m_rgb.group(2))) / 255.0
        b = min(255, int(m_rgb.group(3))) / 255.0
        if r > 0.88 and g > 0.88 and b > 0.88:
            return _INK_DARK
        return (r, g, b)
    return None


def _parse_style_dict(style_attr: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (style_attr or "").split(";"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        out[k.strip().lower()] = v.strip()
    return out


def _apply_css_to_style(base: InlineStyle, css: dict[str, str]) -> InlineStyle:
    st = InlineStyle(
        family=base.family,
        bold=base.bold,
        italic=base.italic,
        underline=base.underline,
        strike=base.strike,
        size=base.size,
        color=base.color,
    )
    ff = css.get("font-family", "").lower()
    if any(tok in ff for tok in ("courier", "mono")):
        st.family = "mono"
    elif any(tok in ff for tok in ("georgia", "times", "serif")) and "sans-serif" not in ff:
        st.family = "serif"
    elif ff:
        st.family = "sans"

    fs = css.get("font-size", "")
    m_sz = re.fullmatch(r"(\d+(?:\.\d+)?)(px|pt)", fs, re.IGNORECASE)
    if m_sz:
        pts = float(m_sz.group(1)) * (0.75 if m_sz.group(2).lower() == "px" else 1.0)
        st.size = max(7.5, min(22.0, pts))

    fw = css.get("font-weight", "").lower()
    if fw in ("bold", "bolder", "600", "700", "800"):
        st.bold = True
    if css.get("font-style", "").lower() in ("italic", "oblique"):
        st.italic = True
    td = css.get("text-decoration", "").lower()
    if "underline" in td:
        st.underline = True
    if "line-through" in td:
        st.strike = True
    rgb = _parse_rgb(css.get("color"))
    if rgb is not None:
        st.color = rgb
    return st


class _RichBlockParser(HTMLParser):
    """Parse sanitized report HTML into a flat list of styled RichBlock items."""

    def __init__(self, base_style: InlineStyle) -> None:
        super().__init__(convert_charrefs=True)
        self._blocks: list[RichBlock] = []
        self._style_stack: list[InlineStyle] = [base_style]
        self._list_stack: list[tuple[str, int]] = []
        self._current: RichBlock | None = None

    def _start_block(self, kind: str, align: str = "left", prefix: str = "", indent: float = 0.0) -> None:
        self._flush_current()
        self._current = RichBlock(kind=kind, align=align, prefix=prefix, indent=indent)

    def _flush_current(self) -> None:
        if self._current is None:
            return
        has_text = any(run.text.strip() for run in self._current.runs)
        if has_text or self._current.kind == "hr":
            self._blocks.append(self._current)
        self._current = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        css = _parse_style_dict(attr_map.get("style", ""))
        align = css.get("text-align", attr_map.get("align", "left")).lower()
        if align not in ("left", "center", "right", "justify"):
            align = "left"

        cur = self._style_stack[-1]
        nxt = _apply_css_to_style(cur, css)
        if tag in ("b", "strong"):
            nxt.bold = True
        elif tag in ("i", "em"):
            nxt.italic = True
        elif tag == "u":
            nxt.underline = True
        elif tag in ("s", "strike"):
            nxt.strike = True
        elif tag in ("h1", "h2", "h3", "h4"):
            nxt.bold = True
            nxt.size = {"h1": 14.5, "h2": 12.5, "h3": 11.2, "h4": 10.5}[tag]
        elif tag == "blockquote":
            nxt.italic = True
            nxt.color = _INK_MUTED
        self._style_stack.append(nxt)

        if tag == "hr":
            self._flush_current()
            self._blocks.append(RichBlock(kind="hr"))
        elif tag == "br":
            if self._current is not None:
                self._current.runs.append(TextRun("\n", nxt))
        elif tag == "ul":
            self._flush_current()
            self._list_stack.append(("ul", 0))
        elif tag == "ol":
            self._flush_current()
            self._list_stack.append(("ol", 0))
        elif tag == "li":
            depth = max(1, len(self._list_stack))
            prefix = "• "
            if self._list_stack and self._list_stack[-1][0] == "ol":
                kind, idx = self._list_stack[-1]
                idx += 1
                self._list_stack[-1] = (kind, idx)
                prefix = f"{idx}. "
            self._start_block("li", align=align, prefix=prefix, indent=14.0 * depth)
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "blockquote"):
            indent = 12.0 if tag == "blockquote" else 0.0
            self._start_block(tag if tag != "div" else "p", align=align, indent=indent)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if len(self._style_stack) > 1:
            self._style_stack.pop()
        if tag in ("ul", "ol") and self._list_stack:
            self._list_stack.pop()
            self._flush_current()
        elif tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "blockquote"):
            self._flush_current()

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self._current is None:
            if not data.strip():
                return
            self._current = RichBlock(kind="p")
        self._current.runs.append(TextRun(data, self._style_stack[-1]))

    def blocks(self) -> list[RichBlock]:
        self._flush_current()
        return self._blocks


def _parse_rich_blocks(raw: str | None, fallback: str, base_style: InlineStyle) -> list[RichBlock]:
    text = (raw or "").strip()
    if not text:
        return [RichBlock(kind="p", runs=[TextRun(fallback, base_style)])]
    if looks_like_rich_html(text):
        parser = _RichBlockParser(base_style)
        parser.feed(sanitize_report_html(text))
        parser.close()
        parsed = parser.blocks()
        if parsed:
            return parsed
    blocks: list[RichBlock] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        if not line:
            blocks.append(RichBlock(kind="p", runs=[TextRun(" ", base_style)]))
            continue
        m_bullet = re.match(r"^\s*[-•*]\s+(.*)$", line)
        if m_bullet:
            blocks.append(RichBlock(kind="li", prefix="• ", indent=14.0, runs=[TextRun(m_bullet.group(1), base_style)]))
        else:
            blocks.append(RichBlock(kind="p", runs=[TextRun(line, base_style)]))
    return blocks


def _is_winansi(text: str) -> bool:
    try:
        text.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def _pdf_escape(text: str) -> str:
    raw = text.encode("cp1252", errors="replace")
    out: list[str] = []
    for byte in raw:
        if byte == 0x28:
            out.append("\\(")
        elif byte == 0x29:
            out.append("\\)")
        elif byte == 0x5C:
            out.append("\\\\")
        elif 0x20 <= byte <= 0x7E:
            out.append(chr(byte))
        else:
            out.append(f"\\{byte:03o}")
    return "".join(out)


def _char_width_factor(ch: str, style: InlineStyle) -> float:
    if style.family == "mono":
        return 0.60
    if ch in "ilI1.,;:'!| ":
        return 0.29
    if ch in "mwMW@#%":
        return 0.82
    if ch.isupper():
        return 0.67
    return 0.54 if style.bold else 0.51


def _measure_text(text: str, style: InlineStyle) -> float:
    return sum(_char_width_factor(ch, style) for ch in text) * style.size


def _wrap_runs(runs: list[TextRun], max_width: float) -> list[list[TextRun]]:
    """Word-wrap styled text runs into visual lines fitting ``max_width``."""
    lines: list[list[TextRun]] = []
    current: list[TextRun] = []
    cur_w = 0.0

    for run in runs:
        for seg_idx, segment in enumerate(run.text.split("\n")):
            if seg_idx > 0:
                lines.append(current)
                current, cur_w = [], 0.0
            for token in re.findall(r"\S+\s*|\s+", segment):
                w = _measure_text(token, run.style)
                if cur_w + w > max_width and current:
                    lines.append(current)
                    current = []
                    cur_w = 0.0
                    token = token.lstrip()
                    if not token:
                        continue
                    w = _measure_text(token, run.style)
                current.append(TextRun(token, run.style))
                cur_w += w
    if current or not lines:
        lines.append(current)
    return lines


def _load_logo_image(org: OrganizationSettings) -> EmbeddedImage | None:
    """Load the institution logo via Pillow and encode as RGB JPEG for PDF."""
    rel = resolve_org_logo_path(getattr(org, "logo_path", None))
    clean = rel.lstrip("/")
    candidates = [
        _APP_DIR / "static" / clean.removeprefix("static/"),
        _APP_DIR / "static" / "viewer" / clean.removeprefix("viewer/"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return None
    try:
        with Image.open(path) as img:
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            bg.thumbnail((360, 140), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            bg.save(buf, format="JPEG", quality=92)
            return EmbeddedImage("ImLogo", bg.width, bg.height, buf.getvalue())
    except OSError:
        return None


def _rasterize_unicode_block(text: str, max_w_pt: float, style: InlineStyle, img_name: str) -> EmbeddedImage | None:
    """Render non-WinAnsi text (e.g. Arabic) to a crisp 2x JPEG strip via Pillow."""
    scale = 2.5
    px_w = max(120, int(max_w_pt * scale))
    font_px = max(14, int(style.size * scale * 1.15))
    font: Any = None
    for candidate in _SYSTEM_UNICODE_FONTS:
        if Path(candidate).is_file():
            try:
                font = ImageFont.truetype(candidate, font_px)
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10), (255, 255, 255)))
    words = text.split()
    wrapped: list[str] = []
    cur = ""
    for word in words:
        trial = f"{cur} {word}".strip()
        bbox = probe.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] > px_w - 8 and cur:
            wrapped.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        wrapped.append(cur)
    if not wrapped:
        return None

    line_h = int(font_px * 1.45)
    px_h = max(line_h + 6, len(wrapped) * line_h + 8)
    canvas = Image.new("RGB", (px_w, px_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    rgb = tuple(int(c * 255) for c in style.color)
    y = 3
    for line in wrapped:
        draw.text((2, y), line, fill=rgb, font=font)
        y += line_h
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=92)
    return EmbeddedImage(img_name, px_w, px_h, buf.getvalue())


class _PdfCanvas:
    """Multi-page PDF 1.4 drawing surface with automatic page breaks."""

    def __init__(self, footer_text: str) -> None:
        self._pages: list[list[str]] = [[]]
        self._images: dict[str, EmbeddedImage] = {}
        self._img_seq = 0
        self._footer_text = footer_text
        self.y = PAGE_H - MARGIN_TOP

    def _ops(self) -> list[str]:
        return self._pages[-1]

    def ensure_space(self, needed: float) -> None:
        if self.y - needed < MARGIN_BOTTOM:
            self._pages.append([])
            self.y = PAGE_H - MARGIN_TOP

    def register_image(self, image: EmbeddedImage) -> str:
        self._images[image.name] = image
        return image.name

    def draw_rect(
        self,
        x: float,
        y_bottom: float,
        w: float,
        h: float,
        *,
        fill: tuple[float, float, float] | None = None,
        stroke: tuple[float, float, float] | None = None,
        line_width: float = 0.75,
    ) -> None:
        ops = self._ops()
        ops.append("q")
        if fill is not None:
            ops.append(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg")
        if stroke is not None:
            ops.append(f"{line_width:.2f} w {stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG")
        op = "B" if (fill and stroke) else ("f" if fill else "S")
        ops.append(f"{x:.2f} {y_bottom:.2f} {w:.2f} {h:.2f} re {op}")
        ops.append("Q")

    def draw_line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        color: tuple[float, float, float] = (0.80, 0.84, 0.88),
        width: float = 0.8,
        dashed: bool = False,
    ) -> None:
        ops = self._ops()
        ops.append("q")
        if dashed:
            ops.append("[3 3] 0 d")
        ops.append(f"{width:.2f} w {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG")
        ops.append(f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")
        ops.append("Q")

    def draw_image(self, name: str, x: float, y_bottom: float, w: float, h: float) -> None:
        self._ops().append(f"q {w:.2f} 0 0 {h:.2f} {x:.2f} {y_bottom:.2f} cm /{name} Do Q")

    def draw_text(self, x: float, y: float, text: str, style: InlineStyle) -> None:
        if not text:
            return
        font = _font_key(style)
        r, g, b = style.color
        esc = _pdf_escape(text)
        self._ops().append(
            f"BT /{font} {style.size:.2f} Tf {r:.3f} {g:.3f} {b:.3f} rg "
            f"1 0 0 1 {x:.2f} {y:.2f} Tm ({esc}) Tj ET"
        )
        if style.underline or style.strike:
            w = _measure_text(text, style)
            line_y = (y - 1.6) if style.underline else (y + style.size * 0.32)
            self.draw_line(x, line_y, x + w, line_y, color=style.color, width=0.6)

    def draw_rich_blocks(
        self,
        blocks: list[RichBlock],
        *,
        x_left: float = MARGIN_X,
        max_w: float = CONTENT_W,
    ) -> None:
        for blk in blocks:
            if blk.kind == "hr":
                self.ensure_space(12.0)
                self.y -= 5.0
                self.draw_line(x_left, self.y, x_left + max_w, self.y)
                self.y -= 7.0
                continue

            full_text = (blk.prefix + "".join(r.text for r in blk.runs)).strip()
            if full_text and not _is_winansi(full_text):
                self._draw_unicode_fallback(full_text, x_left + blk.indent, max_w - blk.indent, blk.runs[0].style)
                continue

            usable_w = max(80.0, max_w - blk.indent)
            lines = _wrap_runs(blk.runs, usable_w)
            is_heading = blk.kind in ("h1", "h2", "h3", "h4")
            if is_heading:
                self.y -= 4.0

            quote_start_y = self.y
            for line_idx, line_runs in enumerate(lines):
                line_size = max((r.style.size for r in line_runs), default=10.0)
                lead = line_size * 1.48
                self.ensure_space(lead + 2.0)
                baseline = self.y - line_size
                line_w = sum(_measure_text(r.text, r.style) for r in line_runs)
                cur_x = x_left + blk.indent
                if blk.align == "center":
                    cur_x = x_left + blk.indent + max(0.0, (usable_w - line_w) / 2.0)
                elif blk.align == "right":
                    cur_x = x_left + blk.indent + max(0.0, usable_w - line_w)

                if line_idx == 0 and blk.prefix:
                    p_style = line_runs[0].style if line_runs else InlineStyle()
                    self.draw_text(x_left + max(0.0, blk.indent - 12.0), baseline, blk.prefix, p_style)

                for run in line_runs:
                    self.draw_text(cur_x, baseline, run.text, run.style)
                    cur_x += _measure_text(run.text, run.style)
                self.y -= lead

            if blk.kind == "blockquote" and quote_start_y > self.y:
                self.draw_line(x_left + 4.0, quote_start_y - 2.0, x_left + 4.0, self.y + 3.0, color=_INK_ACCENT, width=2.2)

            self.y -= 4.5 if blk.kind != "li" else 2.5

    def _draw_unicode_fallback(self, text: str, x: float, max_w: float, style: InlineStyle) -> None:
        self._img_seq += 1
        img = _rasterize_unicode_block(text, max_w, style, f"ImUni{self._img_seq}")
        if img is None:
            return
        pt_w = min(max_w, img.width_px / 2.5)
        pt_h = img.height_px / 2.5
        self.ensure_space(pt_h + 6.0)
        self.register_image(img)
        self.draw_image(img.name, x, self.y - pt_h, pt_w, pt_h)
        self.y -= pt_h + 4.0

    def _stamp_footers(self) -> None:
        total = len(self._pages)
        foot_style = InlineStyle(size=8.0, color=_INK_FAINT)
        for idx, ops in enumerate(self._pages, start=1):
            y_line = MARGIN_BOTTOM - 16.0
            ops.append(
                f"q 0.60 w 0.85 0.88 0.92 RG {MARGIN_X:.2f} {y_line:.2f} m "
                f"{PAGE_W - MARGIN_X:.2f} {y_line:.2f} l S Q"
            )
            left_text = _pdf_escape(self._footer_text[:110])
            right_text = _pdf_escape(f"Page {idx} of {total}")
            right_w = _measure_text(f"Page {idx} of {total}", foot_style)
            ops.append(
                f"BT /F1 8.00 Tf {_INK_FAINT[0]:.3f} {_INK_FAINT[1]:.3f} {_INK_FAINT[2]:.3f} rg "
                f"1 0 0 1 {MARGIN_X:.2f} {y_line - 12.0:.2f} Tm ({left_text}) Tj ET"
            )
            ops.append(
                f"BT /F1 8.00 Tf {_INK_FAINT[0]:.3f} {_INK_FAINT[1]:.3f} {_INK_FAINT[2]:.3f} rg "
                f"1 0 0 1 {PAGE_W - MARGIN_X - right_w:.2f} {y_line - 12.0:.2f} Tm ({right_text}) Tj ET"
            )

    def _serialize_pdf(self, objects: list[bytes], catalog_id: int, info_id: int) -> bytes:
        """Emit PDF 1.4 header, indirect objects, byte-offset xref table, and trailer."""
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = [0]
        for idx, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out.extend(f"{idx} 0 obj\n".encode("ascii"))
            out.extend(body)
            out.extend(b"\nendobj\n")

        xref_pos = len(out)
        out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        out.extend(b"0000000000 65535 f \n")
        for off in offsets[1:]:
            out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
        out.extend(
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R /Info {info_id} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n".encode("ascii")
        )
        return bytes(out)

    def build(self, title: str) -> bytes:
        """Assemble the final PDF 1.4 byte stream with xref table."""
        self._stamp_footers()
        objects: list[bytes] = [b"", b""]  # 1: Catalog, 2: Pages
        catalog_id, pages_id = 1, 2

        def add_obj(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font_refs = [
            f"/{alias} {add_obj(f'<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding >>'.encode('ascii'))} 0 R"
            for alias, base in _PDF_FONTS
        ]
        xobj_refs = [
            f"/{img.name} {add_obj(f'<< /Type /XObject /Subtype /Image /Width {img.width_px} /Height {img.height_px} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(img.jpeg_bytes)} >>\nstream\n'.encode('ascii') + img.jpeg_bytes + b'\nendstream')} 0 R"
            for img in self._images.values()
        ]
        resources = (
            "<< /Font << " + " ".join(font_refs) + " >>"
            + (f" /XObject << {' '.join(xobj_refs)} >>" if xobj_refs else "")
            + " >>"
        )

        page_ids: list[int] = []
        for ops in self._pages:
            stream_data = zlib.compress("\n".join(ops).encode("latin-1"))
            content_id = add_obj(
                f"<< /Filter /FlateDecode /Length {len(stream_data)} >>\nstream\n".encode("ascii")
                + stream_data
                + b"\nendstream"
            )
            page_ids.append(
                add_obj(
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
                    f"/Resources {resources} /Contents {content_id} 0 R >>".encode("ascii")
                )
            )

        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii")
        objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")

        now_pdf = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
        info_id = add_obj(
            f"<< /Title ({_pdf_escape(title)}) /Creator (PAXS Archive) /Producer (PAXS Archive Report PDF Engine) "
            f"/CreationDate ({now_pdf}) >>".encode("ascii")
        )
        return self._serialize_pdf(objects, catalog_id, info_id)


def _draw_header(canvas: _PdfCanvas, org: OrganizationSettings) -> None:
    top_y = canvas.y
    logo = _load_logo_image(org)
    if logo is not None:
        canvas.register_image(logo)
        max_w, max_h = 128.0, 46.0
        scale = min(max_w / max(1, logo.width_px), max_h / max(1, logo.height_px))
        lw, lh = logo.width_px * scale, logo.height_px * scale
        canvas.draw_image(logo.name, MARGIN_X, top_y - lh, lw, lh)

    org_name = (getattr(org, "org_name_en", None) or "PAXS Archive").strip()
    dept = "Diagnostic Imaging Department"
    contact_parts = [p.strip() for p in (getattr(org, "phone", None) or "", getattr(org, "email", None) or "") if p.strip()]
    contact = " · ".join(contact_parts)

    name_style = InlineStyle(bold=True, size=14.0, color=_INK_DARK)
    dept_style = InlineStyle(size=9.5, color=_INK_SECTION)
    contact_style = InlineStyle(size=8.2, color=_INK_MUTED)

    right_edge = PAGE_W - MARGIN_X
    canvas.draw_text(right_edge - _measure_text(org_name, name_style), top_y - 14.0, org_name, name_style)
    canvas.draw_text(right_edge - _measure_text(dept, dept_style), top_y - 28.0, dept, dept_style)
    if contact:
        canvas.draw_text(right_edge - _measure_text(contact, contact_style), top_y - 40.0, contact, contact_style)

    canvas.y = top_y - 54.0
    canvas.draw_line(MARGIN_X, canvas.y, right_edge, canvas.y, color=_INK_ACCENT, width=1.6)
    canvas.y -= 14.0


def _draw_patient_banner(canvas: _PdfCanvas, data: dict[str, Any]) -> None:
    box_h = 74.0
    canvas.ensure_space(box_h + 12.0)
    bottom = canvas.y - box_h
    canvas.draw_rect(MARGIN_X, bottom, CONTENT_W, box_h, fill=(0.973, 0.980, 0.988), stroke=(0.886, 0.910, 0.941))

    lbl = InlineStyle(bold=True, size=8.5, color=_INK_MUTED)
    val = InlineStyle(size=9.2, color=_INK_DARK)
    val_b = InlineStyle(bold=True, size=9.2, color=_INK_DARK)
    val_m = InlineStyle(family="mono", size=8.8, color=_INK_DARK)

    pname = str(data.get("patient_name") or "UNKNOWN").replace("^", " ").strip()
    pid = str(data.get("patient_id") or "--")
    age_sex = f"{data.get('patient_age') or '--'} / {data.get('patient_sex') or '--'}"
    acc = str(data.get("accession_number") or "--")
    mod = str(data.get("modality") or "--")
    sdate = str(data.get("study_date") or "--")
    desc = str(data.get("study_description") or "--")[:82]

    rows = [
        ("Patient Name:", pname[:34], val_b, "Patient ID:", pid[:24], val_m),
        ("Age / Gender:", age_sex, val, "Accession #:", acc[:24], val_b),
        ("Modality:", mod, val_b, "Study Date:", sdate, val),
    ]
    c1_k, c1_v = MARGIN_X + 12.0, MARGIN_X + 86.0
    c2_k, c2_v = MARGIN_X + 278.0, MARGIN_X + 348.0
    row_y = canvas.y - 16.0
    for k1, v1, s1, k2, v2, s2 in rows:
        canvas.draw_text(c1_k, row_y, k1, lbl)
        canvas.draw_text(c1_v, row_y, v1, s1)
        canvas.draw_text(c2_k, row_y, k2, lbl)
        canvas.draw_text(c2_v, row_y, v2, s2)
        row_y -= 16.0
    canvas.draw_text(c1_k, row_y, "Description:", lbl)
    canvas.draw_text(c1_v, row_y, desc, val)
    canvas.y = bottom - 16.0


def _draw_section_heading(canvas: _PdfCanvas, title: str) -> None:
    canvas.ensure_space(28.0)
    style = InlineStyle(bold=True, size=10.5, color=_INK_SECTION)
    canvas.draw_text(MARGIN_X, canvas.y - 10.0, title.upper(), style)
    canvas.draw_line(MARGIN_X, canvas.y - 14.0, PAGE_W - MARGIN_X, canvas.y - 14.0, color=(0.886, 0.910, 0.941), width=1.0)
    canvas.y -= 22.0


def _draw_impression_box(canvas: _PdfCanvas, blocks: list[RichBlock]) -> None:
    canvas.ensure_space(42.0)
    start_y = canvas.y
    start_page_count = len(canvas._pages)
    # Render text inset so a callout accent bar can frame it cleanly.
    canvas.y -= 6.0
    canvas.draw_rich_blocks(blocks, x_left=MARGIN_X + 14.0, max_w=CONTENT_W - 22.0)
    end_y = canvas.y - 4.0
    if len(canvas._pages) == start_page_count:
        box_h = max(24.0, start_y - end_y)
        # Insert the light-sky background behind the impression text ops.
        bg_op = (
            f"q 0.941 0.976 1.000 rg {MARGIN_X:.2f} {end_y:.2f} {CONTENT_W:.2f} {box_h:.2f} re f "
            f"0.008 0.518 0.780 rg {MARGIN_X:.2f} {end_y:.2f} 3.20 {box_h:.2f} re f Q"
        )
        canvas._pages[-1].insert(0, bg_op)
    else:
        canvas.draw_line(MARGIN_X + 2.0, PAGE_H - MARGIN_TOP, MARGIN_X + 2.0, end_y, color=_INK_ACCENT, width=3.0)
    canvas.y = end_y - 12.0


def _draw_addenda(canvas: _PdfCanvas, addenda: list[dict[str, Any]]) -> None:
    if not addenda:
        return
    _draw_section_heading(canvas, "Addenda")
    meta_style = InlineStyle(bold=True, size=9.0, color=_INK_SECTION)
    body_style = InlineStyle(size=9.5, color=_INK_DARK)
    for idx, item in enumerate(addenda, start=1):
        author = str(item.get("author") or "Authorized Radiologist")
        created = str(item.get("created_at") or "").replace("T", " ").split(".")[0]
        header_line = f"Addendum {idx} · {author}" + (f" · {created}" if created else "")
        canvas.ensure_space(28.0)
        canvas.draw_text(MARGIN_X, canvas.y - 9.0, header_line, meta_style)
        canvas.y -= 15.0
        blocks = _parse_rich_blocks(item.get("text"), "", body_style)
        canvas.draw_rich_blocks(blocks, x_left=MARGIN_X + 8.0, max_w=CONTENT_W - 8.0)
        canvas.y -= 4.0


def _draw_signature_block(canvas: _PdfCanvas, report: dict[str, Any]) -> None:
    canvas.ensure_space(56.0)
    canvas.y -= 8.0
    canvas.draw_line(MARGIN_X, canvas.y, PAGE_W - MARGIN_X, canvas.y, color=(0.80, 0.84, 0.88), width=0.8, dashed=True)
    canvas.y -= 16.0

    status = str(report.get("status") or "DRAFT").upper()
    radiologist = str(report.get("radiologist") or "Authorized Radiologist")
    stamp = str(report.get("finalized_at") or report.get("created_at") or "").replace("T", " ").split(".")[0]

    lbl = InlineStyle(size=8.5, color=_INK_MUTED)
    st_style = InlineStyle(bold=True, size=10.5, color=_INK_OK if status == "FINALIZED" else _INK_WARN)
    sig_name = InlineStyle(bold=True, size=10.5, color=_INK_DARK)
    sig_sub = InlineStyle(size=8.8, color=_INK_MUTED)
    sig_time = InlineStyle(family="mono", size=8.0, color=_INK_FAINT)

    canvas.draw_text(MARGIN_X, canvas.y, "Report Status:", lbl)
    canvas.draw_text(MARGIN_X, canvas.y - 14.0, status, st_style)

    right_edge = PAGE_W - MARGIN_X
    canvas.draw_text(right_edge - _measure_text(radiologist, sig_name), canvas.y, radiologist, sig_name)
    canvas.draw_text(right_edge - _measure_text("Consultant Radiologist", sig_sub), canvas.y - 13.0, "Consultant Radiologist", sig_sub)
    if stamp:
        canvas.draw_text(right_edge - _measure_text(stamp, sig_time), canvas.y - 25.0, stamp, sig_time)
    canvas.y -= 38.0


def render_report_pdf(data: dict[str, Any], org: OrganizationSettings) -> bytes:
    """Render a complete diagnostic radiology report as a valid PDF 1.4 document."""
    report = data.get("report") or {}
    org_name = (getattr(org, "org_name_en", None) or "PAXS Archive").strip()
    footer_raw = html_to_plain_text(getattr(org, "footer_html_text", None))
    footer_text = footer_raw.splitlines()[0] if footer_raw else f"{org_name} · Electronic Diagnostic Report"

    canvas = _PdfCanvas(footer_text=footer_text)
    _draw_header(canvas, org)
    _draw_patient_banner(canvas, data)

    _draw_section_heading(canvas, "Diagnostic Findings")
    findings_blocks = _parse_rich_blocks(
        report.get("findings"),
        "No findings recorded.",
        InlineStyle(size=10.0, color=_INK_DARK),
    )
    canvas.draw_rich_blocks(findings_blocks)
    canvas.y -= 6.0

    _draw_section_heading(canvas, "Impression & Conclusion")
    impression_blocks = _parse_rich_blocks(
        report.get("impression"),
        "No impression recorded.",
        InlineStyle(bold=True, size=10.2, color=_INK_DARK),
    )
    _draw_impression_box(canvas, impression_blocks)

    _draw_addenda(canvas, report.get("addenda") or [])
    _draw_signature_block(canvas, report)

    patient = str(data.get("patient_name") or "Patient").replace("^", " ").strip()
    return canvas.build(title=f"Diagnostic Report - {patient}")
