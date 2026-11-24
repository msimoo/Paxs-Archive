"""The rendered documentation page (viewer guide and the sibling explain docs).

``GET /paxs-viewer-guide`` used to return ``text/markdown``, which a browser
renders as a wall of pipes and asterisks — or downloads outright. This module
turns the same source file into a reading page: a hero, a sticky outline, a
table of contents generated from the document's own headings, and styled
tables/shortcuts.

The Markdown file stays the single source of truth. Nothing here is content —
every word on the page still comes from the ``.md``.

Pure functions only: the route reads the file, hands over the text, and serves
what comes back.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime
from pathlib import Path
from string import Template

from app.core.markdown_render import Rendered, Section, render

_DOCS_STATIC = Path(__file__).resolve().parent.parent / "static" / "docs"


def _asset_version() -> str:
    """A cache key that changes whenever either asset file changes.

    ``/static/docs/*`` is served with ``Cache-Control: public, max-age=86400``
    and no content hash in the filename, so a hand-maintained version constant
    meant that a stylesheet edit with a forgotten bump shipped yesterday's
    styles for a day. Hashing the two files' stamps makes the bump automatic:
    same bytes → same version → the browser may keep its copy; different bytes
    → new URL → a fetch.
    """
    digest = hashlib.sha256()
    for name in ("doc-page.css", "doc-page.js"):
        try:
            stat = (_DOCS_STATIC / name).stat()
        except OSError:
            # best-effort: a missing asset is a deployment error the page will
            # show anyway (unstyled), so fall back to a stable key rather than
            # failing the route.
            return "0"
        digest.update(f"{name}:{int(stat.st_mtime)}:{stat.st_size};".encode())
    return digest.hexdigest()[:12]


ASSET_VERSION = _asset_version()
CSS_HREF = f"/static/docs/doc-page.css?v={ASSET_VERSION}"
JS_HREF = f"/static/docs/doc-page.js?v={ASSET_VERSION}"

# The document's own summary table duplicates the sidebar outline, which is
# generated from the same headings. Keep the table (its "what is in it" column
# is real content) but do not list it twice.
_REDUNDANT_OUTLINE = frozenset({"الفهرس"})

_TITLE_SPLIT = re.compile(r"\s+[—–]\s+|\s+-\s+")
_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

# Sun / moon, 16px, stroke-based so they inherit currentColor.
# A hexagon + crosshair: the imaging mark the header uses instead of the
# vendor logo file, which is a red wordmark that fights the dark palette.
_ICON_MARK = (
    '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round">'
    '<path d="M12 2.8 20 7.4v9.2L12 21.2 4 16.6V7.4z"/>'
    '<path d="M12 8.3v7.4M8.3 12h7.4"/></svg>'
)

_ICON_SUN = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
    'M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>'
)
_ICON_MOON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 '
    '8.5 0 1 0 10.5 10.5z"/></svg>'
)

_SHELL = Template(
    """<!DOCTYPE html>
<html lang="ar" dir="rtl" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>$page_title</title>
<meta name="description" content="$description">
<meta name="robots" content="noindex">
<link rel="icon" type="image/png" href="/viewer/assets/logo.png">
<script>
/* Restore the reader's theme before first paint (no flash of the wrong
   palette). Same key the dashboard and viewer use. */
(function () {
  try {
    var t = localStorage.getItem("paxs_theme");
    if (t === "light" || t === "dark") {
      document.documentElement.setAttribute("data-theme", t);
    }
  } catch (e) { /* storage blocked — the dark default stands */ }
})();
</script>
<link rel="stylesheet" href="$css_href">
</head>
<body>
<a class="skip-link" href="#doc">تخطَّ إلى المحتوى</a>
<div class="scroll-progress" aria-hidden="true"><i id="scrollProgress"></i></div>

<header class="topbar">
  <div class="topbar-in">
    <a class="brand" href="/studies-ui" title="PAXS Archive — قائمة الدراسات">
      <span class="brand-mark" aria-hidden="true">$icon_mark</span>
      <span class="brand-txt"><b>PAXS</b> Archive</span>
    </a>
    <span class="crumb" aria-hidden="true">/</span>
    <span class="crumb-here">$doc_label</span>
    <div class="topbar-actions">
      <a class="btn btn-ghost" href="$raw_href" title="المصدر الخام (Markdown)">MD</a>
      <button class="btn btn-ghost" id="printBtn" type="button">طباعة</button>
      <button class="btn btn-icon" id="themeBtn" type="button" aria-label="تبديل السمة"
              title="تبديل السمة">$icon_sun<span class="theme-moon">$icon_moon</span></button>
      <a class="btn btn-primary" href="/studies-ui">الدراسات</a>
    </div>
  </div>
</header>

<section class="hero">
  <div class="hero-in">
    <p class="eyebrow"><span class="dot"></span>PAXS Archive · PaxsViewer</p>
    <h1>$hero_title</h1>
    <p class="hero-sub">$hero_sub</p>
    <div class="chips">$chips</div>
  </div>
</section>

<div class="layout">
  <aside class="toc" id="toc">
    <div class="toc-head">
      <span class="toc-title">في هذه الصفحة</span>
      <button class="toc-toggle" id="tocToggle" type="button" aria-expanded="false"
              aria-controls="tocNav">الفهرس</button>
    </div>
    <label class="toc-search">
      <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
           stroke-width="1.9" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11"
           r="7"/><path d="M20 20l-4.2-4.2"/></svg>
      <input id="tocSearch" type="search" placeholder="ابحث في الجداول…" autocomplete="off"
             aria-label="بحث في جداول الدليل">
      <span class="search-count" id="searchCount" hidden></span>
    </label>
    <nav id="tocNav" class="toc-nav" aria-label="فهرس الصفحة">
      $toc
    </nav>
  </aside>

  <main class="doc" id="doc">
    $content
  </main>
</div>

<footer class="foot">
  <div class="foot-in">
    <p><b>PAXS Archive</b> · $doc_label</p>
    <p class="foot-meta">
      المصدر <a href="$raw_href">$raw_name</a>
      <span class="sep">·</span> آخر تحديث $updated
    </p>
  </div>
</footer>

<button class="to-top" id="toTop" type="button" aria-label="إلى الأعلى" title="إلى الأعلى">
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
       stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/>
       <path d="M5 12l7-7 7 7"/></svg>
</button>

<script src="$js_href" defer></script>
</body>
</html>
"""
)


def render_doc_page(
    text: str,
    *,
    doc_label: str,
    raw_href: str,
    raw_name: str,
    updated: str,
) -> str:
    """Build the full HTML page for one Markdown document."""
    doc = render(text)
    hero_title, hero_sub = _split_title(doc.title)
    outline = [section for section in doc.sections if section.label not in _REDUNDANT_OUTLINE]
    return _SHELL.substitute(
        page_title=f"{hero_title} — PAXS Archive",
        description=hero_sub or hero_title,
        css_href=CSS_HREF,
        js_href=JS_HREF,
        raw_href=html.escape(raw_href, quote=True),
        raw_name=html.escape(raw_name),
        doc_label=html.escape(doc_label),
        updated=html.escape(updated),
        hero_title=html.escape(hero_title),
        hero_sub=html.escape(hero_sub),
        chips=_chips(doc, outline, updated),
        toc=_toc(outline),
        icon_mark=_ICON_MARK,
        icon_sun=_ICON_SUN,
        icon_moon=_ICON_MOON,
        content=doc.html,
    )


def file_stamp(path: Path) -> str:
    """``2026-10-22`` for the file's mtime — the page's "last updated" line."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        # The file is about to be read anyway; a missing stamp must not 500.
        return "—"


def _split_title(title: str) -> tuple[str, str]:
    """``"شرح X — عارض ..."`` → ``("شرح X", "عارض ...")``."""
    parts = _TITLE_SPLIT.split(title.strip(), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return title.strip(), ""


def _chips(doc: Rendered, outline: list[Section], updated: str) -> str:
    """Factual meta chips — counts are derived, never hard-coded."""
    tables = doc.html.count("<table")
    items = [
        ("الأقسام", _arabic(len(outline))),
        ("الجداول", _arabic(tables)),
        ("آخر تحديث", updated),
    ]
    return "".join(
        f'<span class="chip"><span class="chip-k">{html.escape(key)}</span>'
        f'<span class="chip-v">{html.escape(value)}</span></span>'
        for key, value in items
    )


def _arabic(value: int) -> str:
    """Western digits → Arabic-Indic, to match the document's own numbering."""
    return str(value).translate(_ARABIC_DIGITS)


def _toc(sections: list[Section]) -> str:
    """The outline: one entry per h2, indented children for h3."""
    if not sections:
        return '<p class="toc-none">لا توجد أقسام.</p>'
    rows = ['<ol class="toc-list">']
    for section in sections:
        rows.append("<li>")
        rows.append(_toc_link(section, top=True))
        if section.children:
            rows.append('<ol class="toc-sub">')
            rows.extend(f"<li>{_toc_link(child, top=False)}</li>" for child in section.children)
            rows.append("</ol>")
        rows.append("</li>")
    rows.append("</ol>")
    return "".join(rows)


def _toc_link(section: Section, *, top: bool) -> str:
    """One outline entry: ordinal chip (or a bullet) + label."""
    marker = (
        f'<span class="toc-ord">{html.escape(section.ordinal)}</span>'
        if section.ordinal
        else '<span class="toc-bullet" aria-hidden="true"></span>'
    )
    icon = f'<span class="toc-ico" aria-hidden="true">{section.icon}</span>' if section.icon else ""
    cls = "toc-link toc-link-top" if top else "toc-link"
    return (
        f'<a class="{cls}" href="#{section.anchor}">{marker}{icon}'
        f'<span class="toc-txt">{html.escape(section.label)}</span></a>'
    )
