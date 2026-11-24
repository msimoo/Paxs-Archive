"""Dependency-free Markdown → HTML for the served documentation pages.

Scope is deliberately narrow: only the constructs the repository's
``*_explain.md`` documents actually use — ATX headings, paragraphs, pipe
tables, blockquotes, nested bullet/ordered lists, fenced code, horizontal
rules, and the inline spans ``code``, ``**bold**``, ``*italic*`` and
``[text](href)``.

Two invariants:

* **The source is escaped before any markup is applied.** There is no raw-HTML
  passthrough, so a document can never inject markup into a served page.
* **``render()`` is pure.** Same input, same output — which is what lets the
  route serve the page with a stable body and no per-request state.

Anything the subset does not understand degrades to a paragraph, never to a
silently dropped block: a doc edit must never make text vanish.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

# --- block markers ---------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?$")
_FENCE = re.compile(r"^```")

# --- inline markers --------------------------------------------------------

_INLINE_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)([^*]+?)(?<!\*)\*(?!\*)")

#: Schemes a link may use. Anything else — ``javascript:``, ``data:``, a
#: protocol-relative ``//host`` — is rendered as an inert reference chip.
_ALLOWED_SCHEMES = frozenset({"http", "https", "mailto"})
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")

# A section heading carries an optional Arabic/ASCII ordinal ("١)") and an
# optional trailing emoji ("🚀"). Both are lifted out of the label so the
# sidebar can render them as chips instead of burying them in the text.
#
# The "." form needs a guard: "## 1.5 GB limit" is a version number, not an
# ordinal, and stripping the "1." turned the heading into "5 GB limit".
_ORDINAL = re.compile(r"^([٠-٩0-9]{1,3})\s*(?:[)\-–—]\s*|\.(?!\d)\s*)")
_TRAILING_EMOJI = re.compile(
    r"\s*([\U0001f000-\U0001faff\u2190-\u21ff\u2600-\u27bf\u2b00-\u2bff\ufe0f]+)\s*$"
)

# Columns whose cells are keyboard shortcuts get <kbd> instead of <code>.
_KBD_HEADERS = frozenset({"الاختصار", "المفتاح"})

# Callout tone by leading emoji of a blockquote.
_TONES = {
    "\U0001f510": "secure",  # 🔐
    "\U0001f525": "hot",  # 🔥
    "\U0001f3af": "goal",  # 🎯
    "\U0001f4dd": "note",  # 📝
    "\u26a1": "fast",  # ⚡
}


@dataclass(frozen=True)
class Section:
    """One entry in the page's table of contents."""

    anchor: str
    label: str
    ordinal: str = ""
    icon: str = ""
    children: tuple[Section, ...] = ()


@dataclass(frozen=True)
class Rendered:
    """The rendered document: body HTML plus its outline."""

    title: str
    html: str
    sections: tuple[Section, ...]


@dataclass
class _State:
    """Mutable cursor shared by the block helpers for one render pass."""

    in_quote: bool = False
    title: str = ""
    body: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    open_section: bool = False
    intro_open: bool = False
    section_no: int = 0
    child_no: int = 0
    anchor: str = ""
    label: str = ""
    ordinal: str = ""
    icon: str = ""
    pending_children: list[Section] = field(default_factory=list)

    def close_section(self) -> None:
        """Flush the open section into the outline and close its tag."""
        if not self.open_section:
            return
        self.sections.append(
            Section(
                anchor=self.anchor,
                label=self.label,
                ordinal=self.ordinal,
                icon=self.icon,
                children=tuple(self.pending_children),
            )
        )
        self.pending_children = []
        self.body.append("</section>")
        self.open_section = False

    def close_intro(self) -> None:
        """Close the pre-first-section wrapper."""
        if self.intro_open:
            self.body.append("</div>")
            self.intro_open = False

    def open_intro(self) -> None:
        """Wrap leading prose (before the first h2) in its own container."""
        if self.in_quote or self.open_section or self.intro_open:
            return
        self.body.append('<div class="doc-intro">')
        self.intro_open = True


def render(text: str) -> Rendered:
    """Render ``text`` (a Markdown document) into body HTML + an outline."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    state = _State()
    _blocks(lines, state)
    state.close_section()
    state.close_intro()
    return Rendered(
        title=state.title,
        html="".join(state.body),
        sections=tuple(state.sections),
    )


# ---------------------------------------------------------------------------
# Inline spans
# ---------------------------------------------------------------------------


def _inline(text: str, *, kbd: bool = False) -> str:
    """Escape ``text`` then apply the inline subset.

    Code spans are stashed before the emphasis passes so their contents are
    never re-interpreted (``a * b`` inside backticks must stay literal).
    """
    if not text:
        return ""
    out = html.escape(text, quote=False)
    spans: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        spans.append(match.group(1))
        return f"\x00{len(spans) - 1}\x00"

    out = _INLINE_CODE.sub(_stash, out)
    out = _LINK.sub(_link, out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: _span(spans[int(m.group(1))], kbd), out)


def _span(content: str, kbd: bool) -> str:
    """One code span. ``dir="ltr"`` keeps ``W/L`` and API paths un-reordered."""
    if kbd:
        return f'<kbd dir="ltr">{content}</kbd>'
    return f'<code dir="ltr">{content}</code>'


def _link(match: re.Match[str]) -> str:
    """A link, or an inert reference chip when the target is not served.

    Sibling ``*.md`` files are only reachable when they are in the route's
    allowlist; anything else would render as a link to a 404, which reads as a
    broken page. A chip states the reference without promising navigation.
    """
    label, href = match.group(1), match.group(2)
    if href.startswith(("./", "../")) or href.endswith(".md"):
        stem = href.rsplit("/", 1)[-1].removesuffix(".md")
        return f'<span class="doc-ref" title="{_attr(stem)}">{label}</span>'
    safe = _safe_href(href)
    if safe is None:
        return f'<span class="doc-ref" title="{_attr(href)}">{label}</span>'
    target = ' target="_blank" rel="noopener noreferrer"' if safe.startswith("http") else ""
    return f'<a href="{_attr(safe)}"{target}>{label}</a>'


def _attr(value: str) -> str:
    """Quote a value that has *already* been HTML-escaped.

    ``_inline`` escapes the source with ``quote=False``, so ``&`` and ``<`` are
    already entities while ``"`` is still literal — enough to close the
    attribute and add another one. Re-escaping the whole string would turn the
    existing ``&amp;`` into ``&amp;amp;`` and corrupt every query string, so
    only the quote is neutralised.
    """
    return value.replace('"', "&quot;")


def _safe_href(href: str) -> str | None:
    """``href`` when its scheme is allowed, else ``None``.

    A document is trusted here (it ships in the repository), but the module
    promises that a document cannot inject markup, and ``[x](javascript:…)``
    would otherwise become a live script link on a page served to clinicians.
    """
    scheme = _SCHEME.match(href)
    if scheme and scheme.group(1).lower() not in _ALLOWED_SCHEMES:
        return None
    if href.startswith("//"):
        # Protocol-relative: no scheme, but it leaves the archive's origin.
        return None
    return href


# ---------------------------------------------------------------------------
# Block structure
# ---------------------------------------------------------------------------


def _blocks(lines: Sequence[str], state: _State) -> None:
    """Render a run of lines into ``state.body``."""
    index = 0
    total = len(lines)
    while index < total:
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            index += 1
            continue
        if _FENCE.match(stripped):
            index = _fence(lines, index, state)
            continue
        if _RULE.match(stripped):
            state.body.append('<hr class="doc-rule">')
            index += 1
            continue
        heading = _HEADING.match(stripped)
        if heading:
            _heading(len(heading.group(1)), heading.group(2), state)
            index += 1
            continue
        if _is_table(lines, index):
            index = _table(lines, index, state)
            continue
        if stripped.startswith(">"):
            index = _quote(lines, index, state)
            continue
        if _BULLET.match(raw) or _ORDERED.match(raw):
            index = _list(lines, index, state)
            continue
        index = _paragraph(lines, index, state)


def _heading(level: int, text: str, state: _State) -> None:
    """Open a section (h2), a child (h3), or emit a plain heading."""
    label = text.strip()
    if state.in_quote:
        _quoted_heading(level, label, state)
        return
    if level == 1:
        # The first h1 is the page title (the shell renders it as the hero), so
        # it is not repeated in the body. A second h1 in the same file is a
        # top-level heading in all but name — promote it to a section.
        if not state.title:
            state.title = label
            return
        level = 2
    if level == 2:
        _open_section(label, state)
        return
    if level == 3 and state.open_section:
        _open_subsection(label, state)
        return
    state.body.append(f'<h{level} class="doc-h{level}">{_inline(label)}</h{level}>')


def _quoted_heading(level: int, label: str, state: _State) -> None:
    """A heading inside a callout: emphasis, not structure.

    Opening a section there emitted an unclosed ``<section>`` inside the
    ``<aside>`` and reused the page's ``sec-1`` id, so the outline pointed at
    the wrong node. Handled before the h1 branch so a ``#`` in a callout is not
    mistaken for the page title and dropped.
    """
    tag = f"h{min(level + 2, 6)}"
    state.body.append(f'<{tag} class="doc-h3">{_inline(label)}</{tag}>')


def _open_section(label: str, state: _State) -> None:
    """Start an ``<h2>`` section: close the previous one, number this one."""
    state.close_section()
    state.close_intro()
    state.section_no += 1
    state.child_no = 0
    ordinal, icon, clean = _split_ordinal(label)
    state.anchor = f"sec-{state.section_no}"
    state.label, state.ordinal, state.icon = clean, ordinal, icon
    state.open_section = True
    state.body.append(f'<section class="doc-sec" id="{state.anchor}">')
    state.body.append(_heading_inner("h2", "doc-h2", ordinal, icon, clean, state))


def _open_subsection(label: str, state: _State) -> None:
    """An ``<h3>`` under the open section; it joins the outline as a child."""
    state.child_no += 1
    anchor = f"{state.anchor}-{state.child_no}"
    ordinal, icon, clean = _split_ordinal(label)
    state.pending_children.append(Section(anchor=anchor, label=clean, ordinal=ordinal, icon=icon))
    state.body.append(_heading_inner("h3", "doc-h3", ordinal, icon, clean, state, anchor))


def _heading_inner(
    tag: str,
    css: str,
    ordinal: str,
    icon: str,
    label: str,
    state: _State,
    anchor: str = "",
) -> str:
    """Heading markup: ordinal chip + emoji chip + label."""
    ident = f' id="{anchor}"' if anchor else ""
    chip = f'<span class="sec-ordinal">{html.escape(ordinal)}</span>' if ordinal else ""
    emoji = f'<span class="sec-icon" aria-hidden="true">{icon}</span>' if icon else ""
    return (
        f'<{tag} class="{css}"{ident}>{chip}{emoji}'
        f'<span class="sec-label">{_inline(label)}</span></{tag}>'
    )


def _split_ordinal(label: str) -> tuple[str, str, str]:
    """``"١) فتح العارض 🚀"`` → ``("١", "🚀", "فتح العارض")``."""
    ordinal = ""
    match = _ORDINAL.match(label)
    if match:
        ordinal = match.group(1)
        label = label[match.end() :]
    icon = ""
    emoji = _TRAILING_EMOJI.search(label)
    if emoji:
        icon = emoji.group(1).strip()
        label = label[: emoji.start()]
    return ordinal, icon, label.strip() or label


def _fence(lines: Sequence[str], index: int, state: _State) -> int:
    """A fenced code block; the fence line's language hint is ignored."""
    index += 1
    collected: list[str] = []
    while index < len(lines) and not _FENCE.match(lines[index].strip()):
        collected.append(lines[index])
        index += 1
    state.body.append(
        '<pre class="code-block" dir="ltr"><code>' + html.escape("\n".join(collected)) + "</code></pre>"
    )
    return index + 1


def _is_table(lines: Sequence[str], index: int) -> bool:
    """A pipe table: a row of cells followed by a ``| --- |`` separator."""
    if "|" not in lines[index] or index + 1 >= len(lines):
        return False
    return bool(_TABLE_SEP.match(lines[index + 1].strip()))


def _cells(line: str) -> list[str]:
    """Split a table row on ``|``, dropping the optional edge pipes."""
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def _table(lines: Sequence[str], index: int, state: _State) -> int:
    """A pipe table; shortcut columns render their cells as ``<kbd>``."""
    header = _cells(lines[index])
    kbd_columns = {column for column, cell in enumerate(header) if cell in _KBD_HEADERS}
    out = ['<div class="table-wrap"><table class="doc-table"><thead><tr>']
    out.extend(f"<th>{_inline(cell)}</th>" for cell in header)
    out.append("</tr></thead><tbody>")
    index += 2
    while index < len(lines) and "|" in lines[index] and lines[index].strip():
        out.append("<tr>")
        for column, cell in enumerate(_cells(lines[index])):
            out.append(
                f"<td>{_inline(cell, kbd=column in kbd_columns)}</td>"
            )
        out.append("</tr>")
        index += 1
    out.append("</tbody></table></div>")
    state.body.append("".join(out))
    return index


def _quote(lines: Sequence[str], index: int, state: _State) -> int:
    """A blockquote → callout card; its inner blocks are rendered recursively."""
    collected: list[str] = []
    while index < len(lines) and lines[index].strip().startswith(">"):
        # Strip the marker and exactly one following space — any deeper
        # indentation is significant (it nests lists and wraps list items).
        body = lines[index].strip()[1:]
        collected.append(body[1:] if body.startswith(" ") else body)
        index += 1
    tone = _TONES.get(_first_char(collected), "info")
    state.body.append(f'<aside class="callout callout-{tone}">')
    inner = _State(in_quote=True)
    _blocks(collected, inner)
    state.body.extend(inner.body)
    state.body.append("</aside>")
    return index


def _first_char(lines: Sequence[str]) -> str:
    """The first non-space character of the first non-empty line."""
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped[0]
    return ""


def _list(lines: Sequence[str], index: int, state: _State) -> int:
    """A bullet/ordered list run, with nesting by indentation.

    A non-marker line indented deeper than the item it follows is a *lazy
    continuation* of that item (the docs wrap long Arabic bullets this way) and
    is folded back into its text rather than being split off as a paragraph.
    """
    items: list[tuple[int, bool, str]] = []
    while index < len(lines):
        raw = lines[index]
        bullet = _BULLET.match(raw)
        ordered = None if bullet else _ORDERED.match(raw)
        if not bullet and not ordered:
            if items and _continues(raw, items[-1][0]):
                indent, flag, text = items[-1]
                items[-1] = (indent, flag, f"{text} {raw.strip()}")
                index += 1
                continue
            break
        if bullet:
            items.append((len(bullet.group(1)), False, bullet.group(2)))
        elif ordered is not None:
            items.append((len(ordered.group(1)), True, ordered.group(3)))
        index += 1
    state.body.append(_build_list(items))
    return index


def _continues(raw: str, item_indent: int) -> bool:
    """Is ``raw`` a wrapped continuation of the item at ``item_indent``?"""
    stripped = raw.strip()
    if not stripped or len(raw) - len(raw.lstrip()) <= item_indent:
        return False
    return not (_HEADING.match(stripped) or _RULE.match(stripped) or _FENCE.match(stripped))


def _build_list(items: Sequence[tuple[int, bool, str]]) -> str:
    """Recursively build ``<ul>``/``<ol>`` from ``(indent, ordered, text)``."""
    if not items:
        return ""
    tag = "ol" if items[0][1] else "ul"
    out = [f"<{tag}>"]
    index = 0
    while index < len(items):
        indent, _ordered, text = items[index]
        out.append(f"<li>{_inline(text)}")
        index += 1
        nested: list[tuple[int, bool, str]] = []
        while index < len(items) and items[index][0] > indent:
            nested.append(items[index])
            index += 1
        if nested:
            out.append(_build_list(nested))
        out.append("</li>")
    out.append(f"</{tag}>")
    return "".join(out)


def _paragraph(lines: Sequence[str], index: int, state: _State) -> int:
    """A paragraph: consecutive lines until a blank line or a block marker."""
    collected: list[str] = []
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            break
        if collected and (
            _HEADING.match(stripped)
            or _RULE.match(stripped)
            or _FENCE.match(stripped)
            or stripped.startswith(">")
            or _BULLET.match(lines[index])
            or _ORDERED.match(lines[index])
        ):
            break
        collected.append(stripped)
        index += 1
    state.open_intro()
    state.body.append(f"<p>{_inline(' '.join(collected))}</p>")
    return index
