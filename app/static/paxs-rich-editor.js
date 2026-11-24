/**
 * PAXS Archive — Clinical Rich Text Editor & Dictation Voice Player
 *
 * Zero-dependency classic script shared across:
 *   - /studies-ui (Diagnostic Report Editor popup & Study Dictation popover)
 *   - /settings   (Report Templates preparation modal)
 *   - /viewer     (Viewer Diagnostic Report dialog)
 *
 * Exposes:
 *   window.PaxsRichEditor  — WYSIWYG clinical rich-text editor + sanitizer + placeholders
 *   window.PaxsVoicePlayer — Custom interactive waveform dictation audio deck
 */
(function (root) {
  'use strict';

  if (root.PaxsRichEditor && root.PaxsVoicePlayer) return;

  /* ========================================================================
     Shared helpers & sanitizer
     ======================================================================== */
  var ALLOWED_TAGS = {
    p: 1, br: 1, div: 1, span: 1,
    strong: 1, b: 1, em: 1, i: 1, u: 1, s: 1, strike: 1, sub: 1, sup: 1,
    ul: 1, ol: 1, li: 1,
    h1: 1, h2: 1, h3: 1, h4: 1,
    blockquote: 1, hr: 1,
    table: 1, thead: 1, tbody: 1, tr: 1, th: 1, td: 1
  };

  var DROP_CONTENT_TAGS = {
    script: 1, style: 1, iframe: 1, object: 1, embed: 1, svg: 1, math: 1,
    form: 1, input: 1, textarea: 1, button: 1, select: 1, option: 1,
    link: 1, meta: 1, head: 1, title: 1, noscript: 1, template: 1
  };

  var LEGACY_FONT_SIZES = {
    '1': '10px', '2': '12px', '3': '14px', '4': '16px',
    '5': '18px', '6': '22px', '7': '26px'
  };

  var FONT_FAMILIES = [
    { label: 'Default Sans', value: '' },
    { label: 'Inter', value: "'Inter', 'Segoe UI', Arial, sans-serif" },
    { label: 'Arial', value: 'Arial, Helvetica, sans-serif' },
    { label: 'Calibri', value: "Calibri, 'Segoe UI', Arial, sans-serif" },
    { label: 'Georgia', value: "Georgia, 'Times New Roman', serif" },
    { label: 'Times New Roman', value: "'Times New Roman', Times, serif" },
    { label: 'Tahoma', value: 'Tahoma, Geneva, sans-serif' },
    { label: 'Courier Mono', value: "'Courier New', Courier, monospace" }
  ];

  var FONT_SIZES = [
    { label: 'Size', value: '' },
    { label: '11 px', value: '11px' },
    { label: '12 px', value: '12px' },
    { label: '13 px', value: '13px' },
    { label: '14 px', value: '14px' },
    { label: '16 px', value: '16px' },
    { label: '18 px', value: '18px' },
    { label: '20 px', value: '20px' },
    { label: '24 px', value: '24px' }
  ];

  var BLOCK_FORMATS = [
    { label: 'Paragraph', value: 'P' },
    { label: 'Heading 1', value: 'H1' },
    { label: 'Heading 2', value: 'H2' },
    { label: 'Heading 3', value: 'H3' },
    { label: 'Quote', value: 'BLOCKQUOTE' }
  ];

  var TEXT_COLORS = [
    { label: 'Default', value: '' },
    { label: 'Ink Slate', value: '#0f172a' },
    { label: 'Muted Slate', value: '#64748b' },
    { label: 'Critical Red', value: '#ef4444' },
    { label: 'Caution Amber', value: '#d97706' },
    { label: 'Normal Emerald', value: '#059669' },
    { label: 'Clinical Blue', value: '#0284c7' },
    { label: 'Sky Cyan', value: '#22d3ee' },
    { label: 'Indigo', value: '#6366f1' },
    { label: 'Violet', value: '#9333ea' },
    { label: 'Rose', value: '#e11d48' },
    { label: 'White', value: '#f8fafc' }
  ];

  var HIGHLIGHT_COLORS = [
    { label: 'None', value: '' },
    { label: 'Soft Yellow', value: '#fef08a' },
    { label: 'Warm Amber', value: '#fde68a' },
    { label: 'Soft Mint', value: '#bbf7d0' },
    { label: 'Soft Sky', value: '#bae6fd' },
    { label: 'Soft Rose', value: '#fecdd3' }
  ];

  var TAG_RE = /<\s*\/?\s*(?:p|br|div|span|strong|b|em|i|u|s|strike|sub|sup|ul|ol|li|h[1-4]|blockquote|hr|table|thead|tbody|tr|th|td|font|center|script|style|iframe)(?:\s[^<>]*|\/?\s*)>/i;

  function escHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function looksLikeHtml(raw) {
    return !!(raw && TAG_RE.test(String(raw)));
  }

  function isSafeColor(val) {
    var v = String(val || '').trim();
    if (!v) return false;
    if (/^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i.test(v)) return true;
    if (/^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}(?:\s*,\s*(?:0|1|0?\.\d+))?\s*\)$/i.test(v)) return true;
    return /^(?:black|white|gray|grey|red|darkred|crimson|orange|amber|yellow|green|teal|cyan|blue|navy|indigo|purple|violet|magenta|brown|transparent|inherit)$/i.test(v);
  }

  function sanitizeStyle(rawStyle) {
    if (!rawStyle) return '';
    var low = String(rawStyle).toLowerCase();
    if (low.indexOf('url(') !== -1 || low.indexOf('expression(') !== -1 || low.indexOf('javascript:') !== -1) {
      return '';
    }
    var kept = [];
    var parts = String(rawStyle).split(';');
    for (var i = 0; i < parts.length; i++) {
      var idx = parts[i].indexOf(':');
      if (idx === -1) continue;
      var prop = parts[i].slice(0, idx).trim().toLowerCase();
      var val = parts[i].slice(idx + 1).trim();
      if (!prop || !val) continue;

      if (prop === 'font-family') {
        if (/^[a-z0-9\s,'"-]+$/i.test(val)) kept.push('font-family: ' + val);
      } else if (prop === 'font-size') {
        if (/^\d+(?:\.\d+)?(?:px|pt|em|rem|%)$/i.test(val)) kept.push('font-size: ' + val);
      } else if (prop === 'font-weight') {
        if (/^(?:normal|bold|bolder|lighter|[4-8]00)$/i.test(val)) kept.push('font-weight: ' + val);
      } else if (prop === 'font-style') {
        if (/^(?:normal|italic|oblique)$/i.test(val)) kept.push('font-style: ' + val);
      } else if (prop === 'text-decoration' || prop === 'text-decoration-line') {
        if (/^(?:none|underline|line-through|underline line-through)$/i.test(val)) {
          kept.push('text-decoration: ' + val.toLowerCase());
        }
      } else if (prop === 'text-align') {
        if (/^(?:left|center|right|justify|start|end)$/i.test(val)) {
          kept.push('text-align: ' + val.toLowerCase());
        }
      } else if (prop === 'color' || prop === 'background-color') {
        if (isSafeColor(val)) kept.push(prop + ': ' + val);
      } else if (prop === 'margin-left' || prop === 'padding-left' || prop === 'margin-right' || prop === 'padding-right') {
        if (/^\d+(?:\.\d+)?(?:px|pt|em|rem)$/i.test(val)) kept.push(prop + ': ' + val);
      } else if (prop === 'line-height') {
        if (/^\d+(?:\.\d+)?(?:px|pt|em|rem|%)?$/i.test(val)) kept.push('line-height: ' + val);
      }
    }
    return kept.join('; ');
  }

  function cleanNode(node, doc) {
    if (node.nodeType === 3) {
      return doc.createTextNode(node.nodeValue || '');
    }
    if (node.nodeType !== 1) return null;

    var tag = node.tagName.toLowerCase();
    if (DROP_CONTENT_TAGS[tag]) return null;

    var extraStyle = [];
    if (tag === 'font') {
      tag = 'span';
      if (node.getAttribute('face')) extraStyle.push('font-family: ' + node.getAttribute('face'));
      if (node.getAttribute('color')) extraStyle.push('color: ' + node.getAttribute('color'));
      var sz = node.getAttribute('size');
      if (sz && LEGACY_FONT_SIZES[sz]) extraStyle.push('font-size: ' + LEGACY_FONT_SIZES[sz]);
    } else if (tag === 'center') {
      tag = 'div';
      extraStyle.push('text-align: center');
    }

    if (!ALLOWED_TAGS[tag]) {
      var frag = doc.createDocumentFragment();
      for (var c = 0; c < node.childNodes.length; c++) {
        var cleanedChild = cleanNode(node.childNodes[c], doc);
        if (cleanedChild) frag.appendChild(cleanedChild);
      }
      return frag;
    }

    var el = doc.createElement(tag);
    var align = node.getAttribute('align');
    if (align && /^(?:left|center|right|justify)$/i.test(align)) {
      extraStyle.push('text-align: ' + align.toLowerCase());
    }
    var styleRaw = (node.getAttribute('style') || '') + (extraStyle.length ? ';' + extraStyle.join(';') : '');
    var safeStyle = sanitizeStyle(styleRaw);
    if (safeStyle) el.setAttribute('style', safeStyle);

    var dir = node.getAttribute('dir');
    if (dir && /^(?:ltr|rtl|auto)$/i.test(dir)) el.setAttribute('dir', dir.toLowerCase());

    if (tag === 'td' || tag === 'th') {
      var cs = node.getAttribute('colspan');
      var rs = node.getAttribute('rowspan');
      if (cs && /^\d+$/.test(cs)) el.setAttribute('colspan', cs);
      if (rs && /^\d+$/.test(rs)) el.setAttribute('rowspan', rs);
    }

    for (var i = 0; i < node.childNodes.length; i++) {
      var child = cleanNode(node.childNodes[i], doc);
      if (child) el.appendChild(child);
    }
    return el;
  }

  function sanitizeHtml(raw) {
    if (!raw) return '';
    var tpl = document.createElement('template');
    tpl.innerHTML = String(raw);
    var out = document.createElement('div');
    var nodes = tpl.content.childNodes;
    for (var i = 0; i < nodes.length; i++) {
      var cleaned = cleanNode(nodes[i], document);
      if (cleaned) out.appendChild(cleaned);
    }
    return out.innerHTML.trim();
  }

  function plainTextToHtml(text) {
    var s = String(text || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
    if (!s) return '';
    var lines = s.split('\n');
    var html = [];
    var inList = false;
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var bullet = line.match(/^\s*[-•*]\s+(.*)$/);
      if (bullet) {
        if (!inList) { html.push('<ul>'); inList = true; }
        html.push('<li>' + escHtml(bullet[1]) + '</li>');
      } else {
        if (inList) { html.push('</ul>'); inList = false; }
        if (!line.trim()) {
          html.push('<p><br></p>');
        } else {
          html.push('<p>' + escHtml(line) + '</p>');
        }
      }
    }
    if (inList) html.push('</ul>');
    return html.join('');
  }

  function fromValue(raw) {
    if (!raw) return '';
    return looksLikeHtml(raw) ? sanitizeHtml(raw) : plainTextToHtml(raw);
  }

  function toPlainText(raw) {
    if (!raw) return '';
    var div = document.createElement('div');
    div.innerHTML = sanitizeHtml(raw);
    return (div.innerText || div.textContent || '').replace(/\u00a0/g, ' ').trim();
  }

  function applyPlaceholders(raw, meta) {
    if (!raw || !meta) return raw || '';
    var map = {
      patient_name: meta.patient_name || '',
      patient_id: meta.patient_id || '',
      patient_age: meta.patient_age || '',
      patient_sex: meta.patient_sex || '',
      study_description: meta.study_description || '',
      study_date: meta.study_date || '',
      accession_number: meta.accession_number || '',
      modality: meta.modality || '',
      referring_physician: meta.referring_physician || ''
    };
    return String(raw).replace(/\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g, function (match, key) {
      if (Object.prototype.hasOwnProperty.call(map, key)) {
        return escHtml(map[key]);
      }
      return match;
    });
  }

  /* ========================================================================
     SVG icons for toolbar (compact 15x15 clinical strokes)
     ======================================================================== */
  var ICONS = {
    undo: '<svg viewBox="0 0 24 24"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>',
    redo: '<svg viewBox="0 0 24 24"><path d="m15 14 5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/></svg>',
    bold: '<svg viewBox="0 0 24 24"><path d="M6 4h8a4 4 0 0 1 4 4 4 4 0 0 1-4 4H6z"/><path d="M6 12h9a4 4 0 0 1 4 4 4 4 0 0 1-4 4H6z"/></svg>',
    italic: '<svg viewBox="0 0 24 24"><line x1="19" y1="4" x2="10" y2="4"/><line x1="14" y1="20" x2="5" y2="20"/><line x1="15" y1="4" x2="9" y2="20"/></svg>',
    underline: '<svg viewBox="0 0 24 24"><path d="M6 3v7a6 6 0 0 0 12 0V3"/><line x1="4" y1="21" x2="20" y2="21"/></svg>',
    strike: '<svg viewBox="0 0 24 24"><path d="M16 4H9a3 3 0 0 0-2.83 4"/><path d="M14 12a4 4 0 0 1 0 8H6"/><line x1="4" y1="12" x2="20" y2="12"/></svg>',
    ul: '<svg viewBox="0 0 24 24"><line x1="9" y1="6" x2="20" y2="6"/><line x1="9" y1="12" x2="20" y2="12"/><line x1="9" y1="18" x2="20" y2="18"/><circle cx="4.5" cy="6" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="12" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="18" r="1.2" fill="currentColor" stroke="none"/></svg>',
    ol: '<svg viewBox="0 0 24 24"><line x1="10" y1="6" x2="21" y2="6"/><line x1="10" y1="12" x2="21" y2="12"/><line x1="10" y1="18" x2="21" y2="18"/><path d="M4 6h1v4"/><path d="M4 10h2"/><path d="M6 18H4c0-1 2-2 2-3s-1-1.5-2-1"/></svg>',
    indent: '<svg viewBox="0 0 24 24"><line x1="21" y1="6" x2="11" y2="6"/><line x1="21" y1="12" x2="11" y2="12"/><line x1="21" y1="18" x2="11" y2="18"/><polyline points="3 8 7 12 3 16"/></svg>',
    outdent: '<svg viewBox="0 0 24 24"><line x1="21" y1="6" x2="11" y2="6"/><line x1="21" y1="12" x2="11" y2="12"/><line x1="21" y1="18" x2="11" y2="18"/><polyline points="7 8 3 12 7 16"/></svg>',
    alignLeft: '<svg viewBox="0 0 24 24"><line x1="21" y1="6" x2="3" y2="6"/><line x1="15" y1="12" x2="3" y2="12"/><line x1="17" y1="18" x2="3" y2="18"/></svg>',
    alignCenter: '<svg viewBox="0 0 24 24"><line x1="21" y1="6" x2="3" y2="6"/><line x1="17" y1="12" x2="7" y2="12"/><line x1="19" y1="18" x2="5" y2="18"/></svg>',
    alignRight: '<svg viewBox="0 0 24 24"><line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="12" x2="9" y2="12"/><line x1="21" y1="18" x2="7" y2="18"/></svg>',
    alignJustify: '<svg viewBox="0 0 24 24"><line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="12" x2="3" y2="12"/><line x1="21" y1="18" x2="3" y2="18"/></svg>',
    hr: '<svg viewBox="0 0 24 24"><line x1="3" y1="12" x2="21" y2="12"/></svg>',
    clear: '<svg viewBox="0 0 24 24"><path d="M4 7V4h16v3"/><path d="M5 20h6"/><path d="M13 4 8 20"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="21" y1="15" x2="15" y2="21"/></svg>',
    play: '<svg viewBox="0 0 24 24"><polygon points="6 4 20 12 6 20 6 4"/></svg>',
    pause: '<svg viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>',
    volOn: '<svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>',
    volMute: '<svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>'
  };

  /* ========================================================================
     1. PaxsRichEditor.create(host, opts)
     ======================================================================== */
  function createRichEditor(host, opts) {
    var o = opts || {};
    var listeners = [];
    var savedRange = null;
    var readOnly = !!o.readOnly;
    var activePopovers = [];

    var wrap = document.createElement('div');
    wrap.className = 'paxs-rte' + (readOnly ? ' paxs-rte-readonly' : '');

    var toolbar = document.createElement('div');
    toolbar.className = 'paxs-rte-toolbar';
    toolbar.setAttribute('role', 'toolbar');
    toolbar.setAttribute('aria-label', o.ariaLabel || 'Text formatting toolbar');

    var body = document.createElement('div');
    body.className = 'paxs-rte-body';
    body.contentEditable = readOnly ? 'false' : 'true';
    body.setAttribute('role', 'textbox');
    body.setAttribute('aria-multiline', 'true');
    body.setAttribute('aria-label', o.ariaLabel || 'Report text editor');
    body.setAttribute('data-placeholder', o.placeholder || 'Enter clinical text…');
    if (o.minHeight) body.style.minHeight = typeof o.minHeight === 'number' ? o.minHeight + 'px' : o.minHeight;
    if (o.maxHeight) body.style.maxHeight = typeof o.maxHeight === 'number' ? o.maxHeight + 'px' : o.maxHeight;

    var foot = document.createElement('div');
    foot.className = 'paxs-rte-foot';
    foot.innerHTML =
      '<span class="paxs-rte-stats">' +
        '<span>Words: <b data-stat="words">0</b></span>' +
        '<span>Chars: <b data-stat="chars">0</b></span>' +
      '</span>' +
      '<span data-stat="dir">LTR</span>';

    wrap.appendChild(toolbar);
    wrap.appendChild(body);
    wrap.appendChild(foot);
    host.replaceChildren(wrap);

    function saveSelection() {
      var sel = window.getSelection();
      if (sel && sel.rangeCount > 0) {
        var r = sel.getRangeAt(0);
        if (body.contains(r.commonAncestorContainer)) {
          savedRange = r.cloneRange();
        }
      }
    }

    function restoreSelection() {
      body.focus();
      if (!savedRange) return;
      try {
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(savedRange);
      } catch (e) { /* range detached */ }
    }

    function closePopovers(except) {
      for (var i = 0; i < activePopovers.length; i++) {
        if (activePopovers[i] !== except) activePopovers[i].hidden = true;
      }
    }

    function applyInlineStyle(prop, value) {
      restoreSelection();
      var sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return;
      var range = sel.getRangeAt(0);

      if (range.collapsed) {
        if (prop === 'fontSize' || prop === 'fontFamily') {
          var span = document.createElement('span');
          span.style[prop] = value;
          span.appendChild(document.createTextNode('\u200B'));
          range.insertNode(span);
          var newRange = document.createRange();
          newRange.setStart(span.firstChild, 1);
          newRange.collapse(true);
          sel.removeAllRanges();
          sel.addRange(newRange);
          saveSelection();
          notifyChange();
          return;
        }
        return;
      }

      // Use execCommand fontName marker trick so multi-block selections split cleanly
      var marker = '__paxs_style_marker__';
      try {
        document.execCommand('styleWithCSS', false, false);
        document.execCommand('fontName', false, marker);
        var fonts = body.querySelectorAll('font[face="' + marker + '"]');
        for (var i = 0; i < fonts.length; i++) {
          var s = document.createElement('span');
          if (value) s.style[prop] = value;
          while (fonts[i].firstChild) s.appendChild(fonts[i].firstChild);
          fonts[i].parentNode.replaceChild(s, fonts[i]);
        }
      } catch (e) {
        // Fallback: wrap extracted contents
        var fallbackSpan = document.createElement('span');
        if (value) fallbackSpan.style[prop] = value;
        try {
          fallbackSpan.appendChild(range.extractContents());
          range.insertNode(fallbackSpan);
        } catch (err) { /* noop */ }
      }
      saveSelection();
      notifyChange();
    }

    function exec(cmd, value) {
      if (readOnly) return;
      restoreSelection();
      try {
        document.execCommand('styleWithCSS', false, true);
      } catch (e) { /* older engines */ }
      try {
        document.execCommand(cmd, false, value === undefined ? null : value);
      } catch (e) { /* noop */ }
      saveSelection();
      updateToolbarState();
      notifyChange();
    }

    function makeGroup() {
      var g = document.createElement('div');
      g.className = 'paxs-rte-group';
      toolbar.appendChild(g);
      return g;
    }

    var trackedButtons = [];

    function addButton(group, cfg) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'paxs-rte-btn';
      btn.title = cfg.title;
      btn.setAttribute('aria-label', cfg.title);
      if (cfg.stateCmd) btn.setAttribute('aria-pressed', 'false');
      btn.innerHTML = cfg.icon || cfg.label;
      btn.addEventListener('mousedown', function (ev) {
        ev.preventDefault(); // preserve editor selection
      });
      btn.addEventListener('click', function () {
        closePopovers();
        if (cfg.onClick) cfg.onClick(btn);
        else exec(cfg.cmd, cfg.value);
      });
      group.appendChild(btn);
      if (cfg.stateCmd) trackedButtons.push({ btn: btn, cmd: cfg.stateCmd });
      return btn;
    }

    function addSelect(group, options, title, extraCls, onChange) {
      var sel = document.createElement('select');
      sel.className = 'paxs-rte-select' + (extraCls ? ' ' + extraCls : '');
      sel.title = title;
      sel.setAttribute('aria-label', title);
      for (var i = 0; i < options.length; i++) {
        var opt = document.createElement('option');
        opt.value = options[i].value;
        opt.textContent = options[i].label;
        sel.appendChild(opt);
      }
      sel.addEventListener('mousedown', saveSelection);
      sel.addEventListener('change', function () {
        onChange(sel.value, sel);
      });
      group.appendChild(sel);
      return sel;
    }

    function addColorPicker(group, cfg) {
      var holder = document.createElement('div');
      holder.className = 'paxs-rte-color-wrap';

      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'paxs-rte-btn paxs-rte-color-btn';
      btn.title = cfg.title;
      btn.setAttribute('aria-label', cfg.title);
      btn.innerHTML =
        '<span class="paxs-rte-color-btn-inner">' +
          '<span style="font-size:12px;font-weight:800;">' + cfg.glyph + '</span>' +
          '<span class="paxs-rte-color-bar" style="background:' + cfg.defaultBar + '"></span>' +
        '</span>';

      var bar = btn.querySelector('.paxs-rte-color-bar');
      var pop = document.createElement('div');
      pop.className = 'paxs-rte-popover';
      pop.hidden = true;
      activePopovers.push(pop);

      var heading = document.createElement('div');
      heading.className = 'paxs-rte-popover-title';
      heading.textContent = cfg.title;
      pop.appendChild(heading);

      var grid = document.createElement('div');
      grid.className = 'paxs-rte-swatches';
      for (var i = 0; i < cfg.swatches.length; i++) {
        (function (sw) {
          var sbtn = document.createElement('button');
          sbtn.type = 'button';
          sbtn.className = 'paxs-rte-swatch' + (!sw.value ? ' paxs-rte-swatch-clear' : '');
          if (sw.value) sbtn.style.background = sw.value;
          sbtn.title = sw.label;
          sbtn.setAttribute('aria-label', sw.label);
          sbtn.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
          sbtn.addEventListener('click', function () {
            pop.hidden = true;
            bar.style.background = sw.value || cfg.defaultBar;
            cfg.onPick(sw.value);
          });
          grid.appendChild(sbtn);
        })(cfg.swatches[i]);
      }
      pop.appendChild(grid);

      var customRow = document.createElement('label');
      customRow.className = 'paxs-rte-popover-custom';
      customRow.innerHTML = '<span>Custom color</span>';
      var colorInput = document.createElement('input');
      colorInput.type = 'color';
      colorInput.value = cfg.defaultBar;
      colorInput.addEventListener('input', function () {
        bar.style.background = colorInput.value;
        cfg.onPick(colorInput.value);
      });
      customRow.appendChild(colorInput);
      pop.appendChild(customRow);

      btn.addEventListener('mousedown', function (ev) {
        ev.preventDefault();
        saveSelection();
      });
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var willOpen = pop.hidden;
        closePopovers(pop);
        pop.hidden = !willOpen;
      });

      holder.appendChild(btn);
      holder.appendChild(pop);
      group.appendChild(holder);
    }

    // Group 1: History
    var gHistory = makeGroup();
    addButton(gHistory, { title: 'Undo (Ctrl+Z)', icon: ICONS.undo, cmd: 'undo' });
    addButton(gHistory, { title: 'Redo (Ctrl+Y)', icon: ICONS.redo, cmd: 'redo' });

    // Group 2: Typography (Block style, Font family, Font size)
    var gType = makeGroup();
    var blockSelect = addSelect(gType, BLOCK_FORMATS, 'Paragraph / Heading style', '', function (val) {
      exec('formatBlock', '<' + val + '>');
    });
    var fontSelect = addSelect(gType, FONT_FAMILIES, 'Font family', '', function (val) {
      if (!val) { applyInlineStyle('fontFamily', ''); return; }
      applyInlineStyle('fontFamily', val);
    });
    var sizeSelect = addSelect(gType, FONT_SIZES, 'Font size', 'paxs-rte-select-sm', function (val) {
      if (!val) return;
      applyInlineStyle('fontSize', val);
      sizeSelect.value = '';
    });

    // Group 3: Inline Formatting
    var gInline = makeGroup();
    addButton(gInline, { title: 'Bold (Ctrl+B)', icon: ICONS.bold, cmd: 'bold', stateCmd: 'bold' });
    addButton(gInline, { title: 'Italic (Ctrl+I)', icon: ICONS.italic, cmd: 'italic', stateCmd: 'italic' });
    addButton(gInline, { title: 'Underline (Ctrl+U)', icon: ICONS.underline, cmd: 'underline', stateCmd: 'underline' });
    addButton(gInline, { title: 'Strikethrough', icon: ICONS.strike, cmd: 'strikeThrough', stateCmd: 'strikeThrough' });
    addButton(gInline, { title: 'Superscript (e.g. cm³)', label: 'x<sup>2</sup>', cmd: 'superscript', stateCmd: 'superscript' });
    addButton(gInline, { title: 'Subscript (e.g. T₂)', label: 'x<sub>2</sub>', cmd: 'subscript', stateCmd: 'subscript' });

    // Group 4: Text Color & Highlight
    var gColor = makeGroup();
    addColorPicker(gColor, {
      title: 'Text color',
      glyph: 'A',
      defaultBar: '#22d3ee',
      swatches: TEXT_COLORS,
      onPick: function (color) {
        if (!color) {
          applyInlineStyle('color', 'inherit');
        } else {
          exec('foreColor', color);
        }
      }
    });
    addColorPicker(gColor, {
      title: 'Highlight color',
      glyph: 'H',
      defaultBar: '#fef08a',
      swatches: HIGHLIGHT_COLORS,
      onPick: function (color) {
        exec('hiliteColor', color || 'transparent');
      }
    });

    // Group 5: Lists (pointing / numbering) & Indentation
    var gLists = makeGroup();
    addButton(gLists, { title: 'Bullet list (pointing)', icon: ICONS.ul, cmd: 'insertUnorderedList', stateCmd: 'insertUnorderedList' });
    addButton(gLists, { title: 'Numbered list (numbering)', icon: ICONS.ol, cmd: 'insertOrderedList', stateCmd: 'insertOrderedList' });
    addButton(gLists, { title: 'Decrease indent', icon: ICONS.outdent, cmd: 'outdent' });
    addButton(gLists, { title: 'Increase indent', icon: ICONS.indent, cmd: 'indent' });

    // Group 6: Alignment & Direction
    var gAlign = makeGroup();
    addButton(gAlign, { title: 'Align left', icon: ICONS.alignLeft, cmd: 'justifyLeft', stateCmd: 'justifyLeft' });
    addButton(gAlign, { title: 'Align center', icon: ICONS.alignCenter, cmd: 'justifyCenter', stateCmd: 'justifyCenter' });
    addButton(gAlign, { title: 'Align right', icon: ICONS.alignRight, cmd: 'justifyRight', stateCmd: 'justifyRight' });
    addButton(gAlign, { title: 'Justify', icon: ICONS.alignJustify, cmd: 'justifyFull', stateCmd: 'justifyFull' });
    var dirBtn = addButton(gAlign, {
      title: 'Toggle text direction (LTR / RTL)',
      label: 'LTR',
      onClick: function () {
        var next = body.getAttribute('dir') === 'rtl' ? 'ltr' : 'rtl';
        body.setAttribute('dir', next);
        dirBtn.textContent = next.toUpperCase();
        dirBtn.classList.toggle('is-active', next === 'rtl');
        var statDir = foot.querySelector('[data-stat="dir"]');
        if (statDir) statDir.textContent = next.toUpperCase();
        notifyChange();
      }
    });

    // Group 7: Divider & Clear Formatting
    var gUtil = makeGroup();
    addButton(gUtil, { title: 'Insert horizontal divider', icon: ICONS.hr, cmd: 'insertHorizontalRule' });
    addButton(gUtil, {
      title: 'Clear formatting',
      icon: ICONS.clear,
      onClick: function () {
        exec('removeFormat');
        exec('formatBlock', '<P>');
      }
    });

    function updateStats() {
      var text = (body.innerText || body.textContent || '').replace(/\u200B/g, '').trim();
      var words = text ? text.split(/\s+/).filter(Boolean).length : 0;
      var chars = text.length;
      var wEl = foot.querySelector('[data-stat="words"]');
      var cEl = foot.querySelector('[data-stat="chars"]');
      if (wEl) wEl.textContent = String(words);
      if (cEl) cEl.textContent = String(chars);
    }

    function updateToolbarState() {
      if (readOnly) return;
      for (var i = 0; i < trackedButtons.length; i++) {
        var active = false;
        try {
          active = document.queryCommandState(trackedButtons[i].cmd);
        } catch (e) { /* noop */ }
        trackedButtons[i].btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      }
      try {
        var block = String(document.queryCommandValue('formatBlock') || '').toUpperCase().replace(/[<>]/g, '');
        if (block && /^(?:P|H1|H2|H3|BLOCKQUOTE)$/.test(block)) {
          blockSelect.value = block;
        } else {
          blockSelect.value = 'P';
        }
      } catch (e) { /* noop */ }
    }

    function notifyChange() {
      updateStats();
      var html = getHtml();
      var plain = getPlainText();
      for (var i = 0; i < listeners.length; i++) {
        try { listeners[i](html, plain); } catch (e) { /* noop */ }
      }
    }

    function getHtml() {
      var raw = body.innerHTML.replace(/\u200B/g, '');
      var cleaned = sanitizeHtml(raw);
      if (!toPlainText(cleaned) && cleaned.indexOf('<hr') === -1) return '';
      if (body.getAttribute('dir') === 'rtl' && cleaned && !/^<(?:div|p)[^>]*dir="rtl"/i.test(cleaned)) {
        return '<div dir="rtl">' + cleaned + '</div>';
      }
      return cleaned;
    }

    function getPlainText() {
      return (body.innerText || body.textContent || '').replace(/\u200B/g, '').replace(/\u00a0/g, ' ').trim();
    }

    function setValue(raw) {
      var html = fromValue(raw);
      body.innerHTML = html;
      var isRtl = /dir=["']rtl["']/i.test(html);
      body.setAttribute('dir', isRtl ? 'rtl' : 'ltr');
      dirBtn.textContent = isRtl ? 'RTL' : 'LTR';
      dirBtn.classList.toggle('is-active', isRtl);
      var statDir = foot.querySelector('[data-stat="dir"]');
      if (statDir) statDir.textContent = isRtl ? 'RTL' : 'LTR';
      updateStats();
    }

    function insertText(text) {
      if (readOnly) return;
      restoreSelection();
      try {
        document.execCommand('insertText', false, text);
      } catch (e) {
        body.appendChild(document.createTextNode(text));
      }
      saveSelection();
      notifyChange();
    }

    function setReadOnly(flag) {
      readOnly = !!flag;
      body.contentEditable = readOnly ? 'false' : 'true';
      wrap.classList.toggle('paxs-rte-readonly', readOnly);
    }

    // Clean paste handler: preserve safe formatting, strip scripts/junk styles
    body.addEventListener('paste', function (ev) {
      if (readOnly) return;
      var clip = ev.clipboardData || window.clipboardData;
      if (!clip) return;
      var htmlData = clip.getData('text/html');
      var textData = clip.getData('text/plain');
      ev.preventDefault();
      if (htmlData) {
        var clean = sanitizeHtml(htmlData);
        try {
          document.execCommand('insertHTML', false, clean);
        } catch (e) {
          document.execCommand('insertText', false, textData || '');
        }
      } else {
        document.execCommand('insertText', false, textData || '');
      }
      notifyChange();
    });

    body.addEventListener('input', notifyChange);
    body.addEventListener('keyup', function () { saveSelection(); updateToolbarState(); });
    body.addEventListener('mouseup', function () { saveSelection(); updateToolbarState(); });
    body.addEventListener('blur', saveSelection);

    var onDocClick = function (ev) {
      if (!wrap.contains(ev.target)) closePopovers();
    };
    document.addEventListener('click', onDocClick);

    if (o.initialValue) setValue(o.initialValue);
    else updateStats();

    return {
      root: wrap,
      body: body,
      getHtml: getHtml,
      getPlainText: getPlainText,
      setValue: setValue,
      insertText: insertText,
      setReadOnly: setReadOnly,
      setInvalid: function (flag) { wrap.classList.toggle('paxs-rte-invalid', !!flag); },
      focus: function () { body.focus(); },
      onChange: function (fn) { if (typeof fn === 'function') listeners.push(fn); },
      destroy: function () {
        document.removeEventListener('click', onDocClick);
        listeners = [];
      }
    };
  }

  /* ========================================================================
     2. PaxsVoicePlayer.mount(host, opts)
     ======================================================================== */
  var SPEEDS = [0.75, 1, 1.25, 1.5, 2];
  var BAR_COUNT = 44;

  function fmtClock(sec) {
    if (!isFinite(sec) || sec < 0) return '00:00';
    var total = Math.floor(sec);
    var m = Math.floor(total / 60);
    var s = total % 60;
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  function waveformHeights(seedStr) {
    var seed = 2166136261;
    var str = String(seedStr || 'paxs');
    for (var i = 0; i < str.length; i++) {
      seed ^= str.charCodeAt(i);
      seed = Math.imul(seed, 16777619);
    }
    var heights = [];
    for (var b = 0; b < BAR_COUNT; b++) {
      seed ^= seed << 13;
      seed ^= seed >>> 17;
      seed ^= seed << 5;
      var norm = ((seed >>> 0) % 1000) / 1000;
      // Shape like natural spoken dictation phrases
      var envelope = 0.45 + 0.55 * Math.abs(Math.sin((b / BAR_COUNT) * Math.PI * 3.2 + norm));
      var pct = Math.round(20 + norm * envelope * 78);
      heights.push(Math.max(18, Math.min(96, pct)));
    }
    return heights;
  }

  function mountVoicePlayer(host, opts) {
    var o = opts || {};
    var uid = o.uid || '';
    var endpoint = o.endpoint || ('/api/v1/studies/' + encodeURIComponent(uid) + '/dictations');
    var fetchFn = o.fetch || window.fetch.bind(window);
    var hideWhenEmpty = !!o.hideWhenEmpty;
    var enableShortcuts = !!o.enableShortcuts;
    var controller = new AbortController();

    var audio = document.createElement('audio');
    audio.preload = 'metadata';

    var speedIdx = 1; // 1.0x
    var destroyed = false;
    var currentTrackId = '';
    var tracksData = [];
    var bars = [];
    var scrubbing = false;

    var card = document.createElement('div');
    card.className = 'paxs-vp' + (o.compact ? ' paxs-vp-compact' : '');

    var statusMsg = document.createElement('p');
    statusMsg.className = 'paxs-vp-status';
    statusMsg.setAttribute('role', 'status');

    if (!hideWhenEmpty) {
      statusMsg.textContent = 'Loading voice dictations…';
      card.appendChild(statusMsg);
      host.replaceChildren(card);
    }

    function buildDeck(tracks) {
      tracksData = tracks;
      card.replaceChildren();

      // Top row: indicator + track count + track picker
      var top = document.createElement('div');
      top.className = 'paxs-vp-top';

      var brand = document.createElement('div');
      brand.className = 'paxs-vp-brand';
      brand.innerHTML =
        '<span class="paxs-vp-dot" aria-hidden="true"></span>' +
        '<span>Voice Dictation</span>' +
        '<span class="paxs-vp-count-pill">' + tracks.length + (tracks.length === 1 ? ' clip' : ' clips') + '</span>';

      var trackWrap = document.createElement('div');
      trackWrap.className = 'paxs-vp-track-wrap';
      var trackSelect = document.createElement('select');
      trackSelect.className = 'paxs-vp-track-select';
      trackSelect.setAttribute('aria-label', 'Dictation recording');
      for (var i = 0; i < tracks.length; i++) {
        var tr = tracks[i];
        var opt = document.createElement('option');
        opt.value = tr.id;
        var dt = tr.created_at ? new Date(tr.created_at).toLocaleString([], {
          month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
        }) : '';
        opt.textContent = (tr.recorded_by || 'Radiologist') + (dt ? ' · ' + dt : '');
        trackSelect.appendChild(opt);
      }
      trackWrap.appendChild(trackSelect);
      top.appendChild(brand);
      top.appendChild(trackWrap);

      // Middle deck: transport + waveform scrubber
      var deck = document.createElement('div');
      deck.className = 'paxs-vp-deck';

      var transport = document.createElement('div');
      transport.className = 'paxs-vp-transport';

      var backBtn = document.createElement('button');
      backBtn.type = 'button';
      backBtn.className = 'paxs-vp-skip';
      backBtn.title = 'Rewind 5 seconds' + (enableShortcuts ? ' (Alt+←)' : '');
      backBtn.setAttribute('aria-label', 'Rewind 5 seconds');
      backBtn.textContent = '-5s';

      var playBtn = document.createElement('button');
      playBtn.type = 'button';
      playBtn.className = 'paxs-vp-play';
      playBtn.title = 'Play / Pause' + (enableShortcuts ? ' (Alt+Space)' : '');
      playBtn.setAttribute('aria-label', 'Play dictation');
      playBtn.innerHTML = ICONS.play;

      var fwdBtn = document.createElement('button');
      fwdBtn.type = 'button';
      fwdBtn.className = 'paxs-vp-skip';
      fwdBtn.title = 'Forward 5 seconds' + (enableShortcuts ? ' (Alt+→)' : '');
      fwdBtn.setAttribute('aria-label', 'Forward 5 seconds');
      fwdBtn.textContent = '+5s';

      transport.appendChild(backBtn);
      transport.appendChild(playBtn);
      transport.appendChild(fwdBtn);

      var waveWrap = document.createElement('div');
      waveWrap.className = 'paxs-vp-wave-wrap';

      var wave = document.createElement('div');
      wave.className = 'paxs-vp-wave';
      wave.setAttribute('role', 'slider');
      wave.setAttribute('tabindex', '0');
      wave.setAttribute('aria-label', 'Seek dictation audio');
      wave.setAttribute('aria-valuemin', '0');
      wave.setAttribute('aria-valuemax', '100');
      wave.setAttribute('aria-valuenow', '0');

      var timeRow = document.createElement('div');
      timeRow.className = 'paxs-vp-time-row';
      var curEl = document.createElement('span');
      curEl.textContent = '00:00';
      var durEl = document.createElement('span');
      durEl.textContent = '--:--';
      timeRow.appendChild(curEl);
      timeRow.appendChild(durEl);

      waveWrap.appendChild(wave);
      waveWrap.appendChild(timeRow);

      deck.appendChild(transport);
      deck.appendChild(waveWrap);

      // Bottom controls: speed, volume, mute, shortcut hint
      var controls = document.createElement('div');
      controls.className = 'paxs-vp-controls';

      var cLeft = document.createElement('div');
      cLeft.className = 'paxs-vp-controls-left';

      var speedBtn = document.createElement('button');
      speedBtn.type = 'button';
      speedBtn.className = 'paxs-vp-speed';
      speedBtn.title = 'Playback speed';
      speedBtn.textContent = '1×';

      var muteBtn = document.createElement('button');
      muteBtn.type = 'button';
      muteBtn.className = 'paxs-vp-mute';
      muteBtn.title = 'Mute / Unmute';
      muteBtn.setAttribute('aria-label', 'Mute audio');
      muteBtn.innerHTML = ICONS.volOn;

      var volRange = document.createElement('input');
      volRange.type = 'range';
      volRange.className = 'paxs-vp-vol';
      volRange.min = '0';
      volRange.max = '1';
      volRange.step = '0.05';
      volRange.value = '1';
      volRange.setAttribute('aria-label', 'Volume');

      cLeft.appendChild(speedBtn);
      cLeft.appendChild(muteBtn);
      cLeft.appendChild(volRange);

      var cRight = document.createElement('div');
      cRight.className = 'paxs-vp-controls-right';
      if (enableShortcuts) {
        cRight.innerHTML =
          '<span class="paxs-vp-kbd-hint"><kbd>Alt</kbd>+<kbd>Space</kbd> play · <kbd>Alt</kbd>+<kbd>←/→</kbd> ±5s</span>';
      }

      controls.appendChild(cLeft);
      controls.appendChild(cRight);

      statusMsg.textContent = '';
      statusMsg.className = 'paxs-vp-status';
      statusMsg.hidden = true;

      card.appendChild(top);
      card.appendChild(deck);
      card.appendChild(controls);
      card.appendChild(statusMsg);

      function renderWaveBars(seed) {
        wave.replaceChildren();
        bars = [];
        var heights = waveformHeights(seed);
        for (var i = 0; i < heights.length; i++) {
          var bar = document.createElement('span');
          bar.className = 'paxs-vp-bar';
          bar.style.height = heights[i] + '%';
          wave.appendChild(bar);
          bars.push(bar);
        }
      }

      function paintProgress(hoverIdx) {
        var dur = isFinite(audio.duration) && audio.duration > 0 ? audio.duration : 0;
        var ratio = dur ? Math.max(0, Math.min(1, audio.currentTime / dur)) : 0;
        var playedCount = Math.round(ratio * bars.length);
        for (var i = 0; i < bars.length; i++) {
          bars[i].classList.toggle('is-played', i < playedCount);
          bars[i].classList.toggle('is-preview', hoverIdx >= 0 && i >= playedCount && i <= hoverIdx);
        }
        curEl.textContent = fmtClock(audio.currentTime);
        durEl.textContent = dur ? fmtClock(dur) : '--:--';
        wave.setAttribute('aria-valuenow', String(Math.round(ratio * 100)));
      }

      function seekFromClientX(clientX) {
        var rect = wave.getBoundingClientRect();
        if (!rect.width || !isFinite(audio.duration) || audio.duration <= 0) return;
        var ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
        audio.currentTime = ratio * audio.duration;
        paintProgress(-1);
      }

      function togglePlay() {
        if (audio.paused) {
          var p = audio.play();
          if (p && p.catch) {
            p.catch(function () {
              statusMsg.textContent = 'Could not start playback. Check browser audio permissions.';
              statusMsg.className = 'paxs-vp-status is-error';
              statusMsg.hidden = false;
            });
          }
        } else {
          audio.pause();
        }
      }

      function skipBy(delta) {
        if (!isFinite(audio.duration) || audio.duration <= 0) return;
        audio.currentTime = Math.max(0, Math.min(audio.duration, audio.currentTime + delta));
        paintProgress(-1);
      }

      function loadTrack(id, resumeSec) {
        currentTrackId = id;
        audio.pause();
        statusMsg.hidden = true;
        renderWaveBars(uid + ':' + id);
        audio.src = endpoint + '/' + encodeURIComponent(id) + '/audio';
        audio.playbackRate = SPEEDS[speedIdx];
        audio.onloadedmetadata = function () {
          if (resumeSec > 0 && isFinite(audio.duration)) {
            audio.currentTime = Math.min(resumeSec, Math.max(0, audio.duration - 0.5));
          }
          paintProgress(-1);
        };
        paintProgress(-1);
      }

      playBtn.addEventListener('click', togglePlay);
      backBtn.addEventListener('click', function () { skipBy(-5); });
      fwdBtn.addEventListener('click', function () { skipBy(5); });

      speedBtn.addEventListener('click', function () {
        speedIdx = (speedIdx + 1) % SPEEDS.length;
        var rate = SPEEDS[speedIdx];
        audio.playbackRate = rate;
        speedBtn.textContent = rate + '×';
      });

      muteBtn.addEventListener('click', function () {
        audio.muted = !audio.muted;
        muteBtn.innerHTML = audio.muted ? ICONS.volMute : ICONS.volOn;
      });

      volRange.addEventListener('input', function () {
        var v = Number(volRange.value);
        audio.volume = v;
        audio.muted = v === 0;
        muteBtn.innerHTML = audio.muted ? ICONS.volMute : ICONS.volOn;
      });

      wave.addEventListener('pointerdown', function (ev) {
        scrubbing = true;
        try { wave.setPointerCapture(ev.pointerId); } catch (e) { /* noop */ }
        seekFromClientX(ev.clientX);
      });
      wave.addEventListener('pointermove', function (ev) {
        var rect = wave.getBoundingClientRect();
        if (!rect.width) return;
        if (scrubbing) {
          seekFromClientX(ev.clientX);
        } else {
          var ratio = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
          paintProgress(Math.floor(ratio * bars.length));
        }
      });
      wave.addEventListener('pointerup', function () { scrubbing = false; });
      wave.addEventListener('pointerleave', function () {
        scrubbing = false;
        paintProgress(-1);
      });
      wave.addEventListener('keydown', function (ev) {
        if (ev.key === 'ArrowLeft') { ev.preventDefault(); skipBy(-5); }
        else if (ev.key === 'ArrowRight') { ev.preventDefault(); skipBy(5); }
        else if (ev.key === ' ' || ev.key === 'Enter') { ev.preventDefault(); togglePlay(); }
      });

      audio.addEventListener('play', function () {
        card.classList.add('is-playing');
        playBtn.innerHTML = ICONS.pause;
        playBtn.setAttribute('aria-label', 'Pause dictation');
      });
      audio.addEventListener('pause', function () {
        card.classList.remove('is-playing');
        playBtn.innerHTML = ICONS.play;
        playBtn.setAttribute('aria-label', 'Play dictation');
      });
      audio.addEventListener('ended', function () {
        card.classList.remove('is-playing');
        playBtn.innerHTML = ICONS.play;
        paintProgress(-1);
      });
      audio.addEventListener('timeupdate', function () { paintProgress(-1); });
      audio.addEventListener('error', function () {
        statusMsg.textContent = 'Could not play this dictation recording.';
        statusMsg.className = 'paxs-vp-status is-error';
        statusMsg.hidden = false;
      });

      trackSelect.addEventListener('change', function () {
        loadTrack(trackSelect.value, 0);
      });

      var resumeState = o.resumeState;
      var canResume = resumeState && resumeState.uid === uid &&
        tracks.some(function (t) { return t.id === resumeState.id; });
      if (canResume) trackSelect.value = resumeState.id;
      host.hidden = false;
      host.replaceChildren(card);
      loadTrack(trackSelect.value, canResume ? resumeState.position : 0);

      // Hands-free transcription shortcuts while writing in the report editor
      if (enableShortcuts) {
        onKeyDown = function (ev) {
          if (destroyed || !tracksData.length) return;
          if (!ev.altKey || ev.ctrlKey || ev.metaKey) return;
          if (ev.code === 'Space') {
            ev.preventDefault();
            togglePlay();
          } else if (ev.key === 'ArrowLeft') {
            ev.preventDefault();
            skipBy(-5);
          } else if (ev.key === 'ArrowRight') {
            ev.preventDefault();
            skipBy(5);
          }
        };
        window.addEventListener('keydown', onKeyDown);
      }
    }

    var onKeyDown = null;

    fetchFn(endpoint, { signal: controller.signal })
      .then(function (res) {
        if (hideWhenEmpty && (res.status === 403 || res.status === 404)) return [];
        if (!res.ok) throw new Error('Dictation list unavailable');
        return res.json();
      })
      .then(function (tracks) {
        if (controller.signal.aborted || destroyed) return;
        if (!tracks || !tracks.length) {
          if (hideWhenEmpty) {
            host.hidden = true;
            return;
          }
          statusMsg.textContent = 'No voice recordings for this study yet.';
          card.replaceChildren(statusMsg);
          host.replaceChildren(card);
          return;
        }
        buildDeck(tracks);
      })
      .catch(function () {
        if (controller.signal.aborted || destroyed) return;
        if (hideWhenEmpty) {
          host.hidden = true;
          return;
        }
        statusMsg.textContent = 'Could not load study dictations.';
        statusMsg.className = 'paxs-vp-status is-error';
        card.replaceChildren(statusMsg);
        host.hidden = false;
        host.replaceChildren(card);
      });

    return function unmount() {
      destroyed = true;
      controller.abort();
      if (onKeyDown) window.removeEventListener('keydown', onKeyDown);
      if (currentTrackId && typeof o.onSaveResume === 'function') {
        o.onSaveResume({ uid: uid, id: currentTrackId, position: audio.currentTime || 0 });
      }
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    };
  }

  root.PaxsRichEditor = {
    create: createRichEditor,
    sanitizeHtml: sanitizeHtml,
    looksLikeHtml: looksLikeHtml,
    fromValue: fromValue,
    toPlainText: toPlainText,
    applyPlaceholders: applyPlaceholders,
    escapeHtml: escHtml
  };

  root.PaxsVoicePlayer = {
    mount: mountVoicePlayer
  };
}(typeof window !== 'undefined' ? window : this));
