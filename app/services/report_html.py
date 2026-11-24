"""Safe rich-text HTML sanitization and plain-text conversion for diagnostic reports.

Radiology reports and report templates may contain either:
  * legacy plain text (including clinical comparisons like ``lesion < 5 mm``), or
  * rich HTML produced by the PAXS diagnostic rich-text editor (bold, italic,
    underline, font family/size, colors, bulleted/numbered lists, headings,
    alignment, indentation, sub/superscript, RTL/LTR).

Security contract
-----------------
* Only an explicit allowlist of formatting tags and strictly validated inline
  CSS properties may survive sanitization.
* Script/style/iframe/svg/form containers are dropped along with their inner
  text. All event handlers (``on*``) and unsafe URL schemes are stripped.
* Plain text without formatting tags is left untouched by ``sanitize_stored_html``
  and escaped verbatim by ``render_rich_report_html`` so clinical symbols such as
  ``< 5 mm`` or ``A & B`` are never altered or swallowed.
* HL7 ORU^R01 uses ``html_to_plain_text`` so downstream EMRs receive clean
  readable text (with bullet points, numbering, and line breaks) rather than
  raw HTML markup.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# Recognise real formatting markup (not clinical text like "lesion < 5 mm" or "<3cm").
_ALLOWED_TAGS: frozenset[str] = frozenset({
    "p", "br", "div", "span",
    "strong", "b", "em", "i", "u", "s", "strike", "sub", "sup",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4",
    "blockquote", "hr",
    "table", "thead", "tbody", "tr", "th", "td",
})

_VOID_TAGS: frozenset[str] = frozenset({"br", "hr"})

_DROP_CONTENT_TAGS: frozenset[str] = frozenset({
    "script", "style", "iframe", "object", "embed", "applet",
    "svg", "math", "form", "input", "textarea", "button", "select",
    "option", "link", "meta", "head", "title", "noscript", "template",
})

_TAG_DETECTOR_RE = re.compile(
    r"<\s*/?\s*(?:" + "|".join(sorted(_ALLOWED_TAGS | _DROP_CONTENT_TAGS | {"font", "center"})) + r")(?:\s[^<>]*|/?\s*)>",
    re.IGNORECASE,
)

# Strictly validated inline style declarations.
_SAFE_FONT_FAMILIES: dict[str, str] = {
    "inter": "'Inter', 'Segoe UI', Arial, sans-serif",
    "sans-serif": "'Inter', 'Segoe UI', Arial, sans-serif",
    "arial": "Arial, Helvetica, sans-serif",
    "helvetica": "Arial, Helvetica, sans-serif",
    "calibri": "Calibri, 'Segoe UI', Arial, sans-serif",
    "segoe ui": "'Segoe UI', Arial, sans-serif",
    "tahoma": "Tahoma, Geneva, sans-serif",
    "verdana": "Verdana, Geneva, sans-serif",
    "georgia": "Georgia, 'Times New Roman', serif",
    "times new roman": "'Times New Roman', Times, serif",
    "serif": "Georgia, 'Times New Roman', serif",
    "courier new": "'Courier New', Courier, monospace",
    "jetbrains mono": "'JetBrains Mono', 'Courier New', monospace",
    "monospace": "'JetBrains Mono', 'Courier New', monospace",
}

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB_COLOR_RE = re.compile(
    r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*(0|1|0?\.\d+))?\s*\)$",
    re.IGNORECASE,
)
_SAFE_NAMED_COLORS: frozenset[str] = frozenset({
    "black", "white", "gray", "grey", "red", "darkred", "crimson",
    "orange", "darkorange", "amber", "gold", "yellow", "green", "darkgreen",
    "teal", "cyan", "blue", "darkblue", "navy", "indigo", "purple", "violet",
    "magenta", "brown", "slate", "transparent", "inherit",
})
_FONT_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)(px|pt|em|rem|%)$", re.IGNORECASE)
_LENGTH_RE = re.compile(r"^(\d+(?:\.\d+)?)(px|pt|em|rem)$", re.IGNORECASE)
_LINE_HEIGHT_RE = re.compile(r"^(\d+(?:\.\d+)?)(px|pt|em|rem|%)?$", re.IGNORECASE)
_TEXT_ALIGN_VALUES: frozenset[str] = frozenset({"left", "center", "right", "justify", "start", "end"})
_DIR_VALUES: frozenset[str] = frozenset({"ltr", "rtl", "auto"})
_FONT_WEIGHT_VALUES: frozenset[str] = frozenset({"normal", "bold", "bolder", "lighter", "400", "500", "600", "700", "800"})
_FONT_STYLE_VALUES: frozenset[str] = frozenset({"normal", "italic", "oblique"})
_TEXT_DECORATION_VALUES: frozenset[str] = frozenset({"none", "underline", "line-through", "underline line-through"})

# Map legacy HTML <font size="1..7"> to clean px values.
_LEGACY_FONT_SIZE_MAP: dict[str, str] = {
    "1": "10px",
    "2": "12px",
    "3": "14px",
    "4": "16px",
    "5": "18px",
    "6": "22px",
    "7": "26px",
}


def looks_like_rich_html(value: str | None) -> bool:
    """True when ``value`` contains recognised HTML formatting tags."""
    if not value:
        return False
    return bool(_TAG_DETECTOR_RE.search(value))


def _sanitize_color(raw: str) -> str | None:
    val = raw.strip()
    if not val:
        return None
    if _HEX_COLOR_RE.fullmatch(val):
        return val.lower()
    m = _RGB_COLOR_RE.fullmatch(val)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if r <= 255 and g <= 255 and b <= 255:
            if m.group(4) is not None:
                return f"rgba({r}, {g}, {b}, {m.group(4)})"
            return f"rgb({r}, {g}, {b})"
        return None
    low = val.lower()
    if low in _SAFE_NAMED_COLORS:
        return low
    return None


def _sanitize_font_family(raw: str) -> str | None:
    for part in raw.split(","):
        cleaned = part.strip().strip("\"'").lower()
        if cleaned in _SAFE_FONT_FAMILIES:
            return _SAFE_FONT_FAMILIES[cleaned]
    return None


def _sanitize_font_size(raw: str) -> str | None:
    m = _FONT_SIZE_RE.fullmatch(raw.strip())
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2).lower()
    limits = {"px": (9, 36), "pt": (7, 28), "em": (0.6, 2.5), "rem": (0.6, 2.5), "%": (60, 220)}
    lo, hi = limits[unit]
    clamped = max(lo, min(hi, num))
    num_str = str(int(clamped)) if clamped == int(clamped) else f"{clamped:.2f}".rstrip("0").rstrip(".")
    return f"{num_str}{unit}"


def _sanitize_indent_length(raw: str) -> str | None:
    m = _LENGTH_RE.fullmatch(raw.strip())
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2).lower()
    limits = {"px": (0, 240), "pt": (0, 180), "em": (0, 16), "rem": (0, 16)}
    lo, hi = limits[unit]
    clamped = max(lo, min(hi, num))
    num_str = str(int(clamped)) if clamped == int(clamped) else f"{clamped:.2f}".rstrip("0").rstrip(".")
    return f"{num_str}{unit}"


def _sanitize_line_height(raw: str) -> str | None:
    m = _LINE_HEIGHT_RE.fullmatch(raw.strip())
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if not unit:
        clamped = max(1.0, min(3.0, num))
        return f"{clamped:.2f}".rstrip("0").rstrip(".")
    if unit == "%":
        clamped = max(100.0, min(300.0, num))
        return f"{int(clamped)}%"
    if unit in ("em", "rem"):
        clamped = max(1.0, min(3.0, num))
        return f"{clamped:.2f}".rstrip("0").rstrip(".")
    clamped = max(12.0, min(60.0, num))
    return f"{int(clamped)}{unit}"


def _sanitize_style(style_raw: str) -> str:
    """Return a strictly allowlisted inline CSS string (or ``""``)."""
    if not style_raw:
        return ""
    lowered = style_raw.lower()
    if "url(" in lowered or "expression(" in lowered or "javascript:" in lowered or "@import" in lowered:
        return ""

    kept: list[str] = []
    for decl in style_raw.split(";"):
        if ":" not in decl:
            continue
        prop, val = decl.split(":", 1)
        prop = prop.strip().lower()
        val = val.strip()
        if not prop or not val:
            continue

        safe_val: str | None = None
        if prop == "font-family":
            safe_val = _sanitize_font_family(val)
        elif prop == "font-size":
            safe_val = _sanitize_font_size(val)
        elif prop == "font-weight":
            low = val.lower()
            if low in _FONT_WEIGHT_VALUES:
                safe_val = low
        elif prop == "font-style":
            low = val.lower()
            if low in _FONT_STYLE_VALUES:
                safe_val = low
        elif prop in ("text-decoration", "text-decoration-line"):
            tokens = [t for t in val.lower().split() if t in ("underline", "line-through", "none")]
            if tokens:
                joined = " ".join(dict.fromkeys(tokens))
                if joined in _TEXT_DECORATION_VALUES:
                    prop = "text-decoration"
                    safe_val = joined
        elif prop == "text-align":
            low = val.lower()
            if low in _TEXT_ALIGN_VALUES:
                safe_val = low
        elif prop in ("color", "background-color"):
            safe_val = _sanitize_color(val)
        elif prop in ("margin-left", "padding-left", "margin-right", "padding-right"):
            safe_val = _sanitize_indent_length(val)
        elif prop == "line-height":
            safe_val = _sanitize_line_height(val)

        if safe_val is not None:
            kept.append(f"{prop}: {safe_val}")

    return "; ".join(kept)


class _ReportHtmlSanitizer(HTMLParser):
    """Strict allowlist HTML sanitizer built on the Python stdlib parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._stack: list[str] = []
        self._drop_depth: int = 0

    def _normalize_legacy_tag(self, tag: str, attr_map: dict[str, str]) -> tuple[str, dict[str, str]]:
        """Map legacy ``<font>`` and ``<center>`` tags to clean span/div + style."""
        if tag == "font":
            styles = [s for s in (
                attr_map.get("style"),
                f"font-family: {attr_map['face']}" if attr_map.get("face") else "",
                f"color: {attr_map['color']}" if attr_map.get("color") else "",
                f"font-size: {_LEGACY_FONT_SIZE_MAP[attr_map['size']]}" if attr_map.get("size") in _LEGACY_FONT_SIZE_MAP else "",
            ) if s]
            return "span", {"style": "; ".join(styles)}
        if tag == "center":
            return "div", {"style": f"{attr_map.get('style', '')}; text-align: center"}
        return tag, attr_map

    def _safe_attrs_for(self, tag: str, attr_map: dict[str, str]) -> list[str]:
        safe_attrs: list[str] = []
        style_parts: list[str] = []
        if "align" in attr_map and attr_map["align"].strip().lower() in _TEXT_ALIGN_VALUES:
            style_parts.append(f"text-align: {attr_map['align'].strip().lower()}")
        if "style" in attr_map:
            style_parts.append(attr_map["style"])
        if style_parts:
            clean_style = _sanitize_style("; ".join(style_parts))
            if clean_style:
                safe_style_attr = (
                    clean_style.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace('"', "&quot;")
                )
                safe_attrs.append(f'style="{safe_style_attr}"')
        if "dir" in attr_map and attr_map["dir"].strip().lower() in _DIR_VALUES:
            safe_attrs.append(f'dir="{attr_map["dir"].strip().lower()}"')
        if tag in ("td", "th"):
            for span_attr in ("colspan", "rowspan"):
                val = attr_map.get(span_attr, "").strip()
                if val.isdigit() and 1 <= int(val) <= 20:
                    safe_attrs.append(f'{span_attr}="{val}"')
        return safe_attrs

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth > 0:
            return
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        tag, attr_map = self._normalize_legacy_tag(tag, attr_map)
        if tag not in _ALLOWED_TAGS:
            return
        safe_attrs = self._safe_attrs_for(tag, attr_map)
        attr_str = (" " + " ".join(safe_attrs)) if safe_attrs else ""
        self._out.append(f"<{tag}{attr_str}>")
        if tag not in _VOID_TAGS:
            self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS or self._drop_depth > 0:
            return
        if tag in _VOID_TAGS:
            self._out.append(f"<{tag}>")
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            if self._drop_depth > 0:
                self._drop_depth -= 1
            return
        if self._drop_depth > 0:
            return
        if tag == "font":
            tag = "span"
        elif tag == "center":
            tag = "div"
        if tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        if tag in self._stack:
            while self._stack:
                top = self._stack.pop()
                self._out.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self._drop_depth > 0:
            return
        self._out.append(html.escape(data, quote=False))

    def get_html(self) -> str:
        while self._stack:
            self._out.append(f"</{self._stack.pop()}>")
        return "".join(self._out).strip()


def sanitize_report_html(raw: str | None) -> str:
    """Sanitize rich HTML markup against the strict clinical allowlist."""
    if not raw:
        return ""
    parser = _ReportHtmlSanitizer()
    parser.feed(raw)
    parser.close()
    return parser.get_html()


def sanitize_stored_field(raw: str | None) -> str | None:
    """Sanitize a report/template field on write while preserving plain text verbatim."""
    if raw is None:
        return None
    if not looks_like_rich_html(raw):
        return raw
    return sanitize_report_html(raw)


def render_rich_report_html(raw: str | None, fallback: str = "") -> str:
    """Render either rich HTML (sanitized) or legacy plain text (escaped + newlines)."""
    text = (raw or "").strip()
    if not text:
        return html.escape(fallback)
    if looks_like_rich_html(text):
        cleaned = sanitize_report_html(text)
        return cleaned if cleaned else html.escape(fallback)
    return html.escape(text).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


class _HtmlToPlainText(HTMLParser):
    """Convert sanitized report HTML into clean plain text (e.g. for HL7 ORU^R01)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._list_stack: list[tuple[str, int]] = []
        self._drop_depth: int = 0

    def _ensure_newline(self, count: int = 1) -> None:
        if not self._parts:
            return
        tail = "".join(self._parts[-3:])
        existing = 0
        for ch in reversed(tail):
            if ch == "\n":
                existing += 1
            else:
                break
        if existing < count:
            self._parts.append("\n" * (count - existing))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth > 0:
            return
        if tag == "br":
            self._parts.append("\n")
        elif tag == "hr":
            self._ensure_newline(1)
            self._parts.append("---")
            self._ensure_newline(1)
        elif tag == "ul":
            self._ensure_newline(1)
            self._list_stack.append(("ul", 0))
        elif tag == "ol":
            self._ensure_newline(1)
            self._list_stack.append(("ol", 0))
        elif tag == "li":
            self._ensure_newline(1)
            indent = "  " * max(0, len(self._list_stack) - 1)
            if self._list_stack and self._list_stack[-1][0] == "ol":
                kind, idx = self._list_stack[-1]
                idx += 1
                self._list_stack[-1] = (kind, idx)
                self._parts.append(f"{indent}{idx}. ")
            else:
                self._parts.append(f"{indent}- ")
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "blockquote", "tr"):
            self._ensure_newline(1)
        elif tag in ("td", "th") and self._parts and not self._parts[-1].endswith(("\n", "\t")):
            self._parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            if self._drop_depth > 0:
                self._drop_depth -= 1
            return
        if self._drop_depth > 0:
            return
        if tag in ("ul", "ol") and self._list_stack:
            self._list_stack.pop()
            self._ensure_newline(1)
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "blockquote", "li", "tr"):
            self._ensure_newline(1)

    def handle_data(self, data: str) -> None:
        if self._drop_depth > 0:
            return
        self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        lines = [line.rstrip() for line in raw.splitlines()]
        collapsed: list[str] = []
        blank_run = 0
        for line in lines:
            if not line.strip():
                blank_run += 1
                if blank_run <= 1:
                    collapsed.append("")
            else:
                blank_run = 0
                collapsed.append(line)
        return "\n".join(collapsed).strip()


def html_to_plain_text(raw: str | None) -> str:
    """Convert rich HTML into clean plain text; return plain text untouched."""
    if not raw:
        return ""
    if not looks_like_rich_html(raw):
        return raw.strip()
    parser = _HtmlToPlainText()
    parser.feed(raw)
    parser.close()
    return parser.get_text()
