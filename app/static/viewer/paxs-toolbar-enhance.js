/**
 * PAXS Viewer — Header toolbar modernization, medical icon injection
 * and live telemetry pill.
 *
 * WHY A DOM-ENHANCEMENT SCRIPT (not CSS alone)
 * --------------------------------------------
 * The header toolbar lives inside the prebuilt OHIF fork bundle. Its wrapper
 * classes are hashed Tailwind utilities that change between builds, so a CSS
 * selector written against them rots silently. This script instead *derives*
 * the toolbar container at runtime from the tool buttons themselves, then tags
 * it with a stable class (`.paxs-toolbar`) that the stylesheet can rely on.
 *
 * WHY WE INJECT ICON PATHS (not stroke-width)
 * -------------------------------------------
 * OHIF's icon set mixes stroked Lucide glyphs with filled silhouettes
 * (`<path fill="currentColor">`). A CSS `stroke-width` rule can only thicken a
 * silhouette — it has no outline to widen, so the chunky geometry never
 * changes. We therefore replace the *contents* of the existing `<svg>` with our
 * own 24x24 line-art geometry from `PAXS_ICONS`.
 *
 * ZERO TOOL DISRUPTION (hard constraints)
 * ---------------------------------------
 *   • `<button>` elements are NEVER detached, cloned or recreated — doing so
 *     destroys Cornerstone/OHIF pointer & drag bindings.
 *   • Only child `<svg>` nodes inside `button[aria-label]` are touched, and even
 *     then the element itself is re-attributed in place — never replaced.
 *   • Every upgraded `<svg>` is stamped `data-paxs-icon="<icon_id>"`; the
 *     attribute is checked first, so re-runs can never loop or thrash.
 *   • No event listener, command, tool group or cornerstone state is read or
 *     written. Tool activation stays 100% OHIF-owned.
 *
 * ACTIVE-STATE CONTRACT (verified in the bundle)
 * ----------------------------------------------
 * Tool buttons are Radix Toggle / ToggleGroup items, so the pressed state is
 * exposed as:
 *     data-state="on"            (Toggle + ToggleGroupItem)
 *     aria-checked="true"        (ToggleGroup type="single" → role="radio")
 *     aria-pressed="true"        (bare Toggle)
 * The legacy selector `[data-state="active"]` never matches — it is kept in
 * the stylesheet only for backwards compatibility.
 */
(function () {
  'use strict';

  if (window.__paxsToolbarInstalled) return;
  window.__paxsToolbarInstalled = true;

  // ---------------------------------------------------------------------------
  // Tool classification (drives the category accent colour).
  // ---------------------------------------------------------------------------
  var CATEGORY = {
    nav:     'paxs-tool-nav',
    measure: 'paxs-tool-measure',
    wl:      'paxs-tool-wl',
    annot:   'paxs-tool-annot',
    def:     'paxs-tool-default'
  };
  var ALL_CATEGORY = [CATEGORY.nav, CATEGORY.measure, CATEGORY.wl, CATEGORY.annot, CATEGORY.def];

  var LABELS = {
    nav:     ['Zoom', 'Pan', 'Stack Scroll', 'Scroll', 'Magnify', 'Flip', 'Rotate', 'Reset', 'Invert', 'Cine', 'Capture', 'Download'],
    measure: ['Length', 'Bidirectional', 'Ellipse', 'Circle', 'Rectangle', 'Angle', 'Cobb', 'Probe', 'Spline', 'LiveWire', 'Freehand', 'Area', 'Calibration'],
    wl:      ['Window Level', 'W/L', 'Contrast', 'Threshold', 'Window', 'Level', 'Preset'],
    annot:   ['Arrow', 'Text', 'Annotation', 'Eraser', 'Crosshair', 'Reference', 'Bookmark', 'Key Image', 'Report', 'Segment', 'Label']
  };

  function classify(label) {
    if (!label) return 'def';
    var lower = String(label).toLowerCase();
    var keys = ['nav', 'measure', 'wl', 'annot'];
    for (var k = 0; k < keys.length; k++) {
      var list = LABELS[keys[k]];
      for (var i = 0; i < list.length; i++) {
        if (lower.indexOf(list[i].toLowerCase()) !== -1) return keys[k];
      }
    }
    return 'def';
  }

  // ---------------------------------------------------------------------------
  // PAXS_ICONS — modern medical vector set.
  //
  // Every glyph is authored on a 24x24 grid for
  //     fill="none" stroke="currentColor" stroke-width="1.75"
  //     stroke-linecap="round" stroke-linejoin="round"
  // A handful of glyphs carry small solid anchor dots (`fill="currentColor"` on
  // an inner element). That is deliberate and survives the stylesheet's
  // `fill: none !important`, because that rule targets only the ROOT `.svg` —
  // an inner element's own fill attribute still wins over the inherited value.
  // ---------------------------------------------------------------------------
  var PAXS_ICONS = {

    // Window / Level — contrast dial: split aperture with a brightness ramp.
    windowLevel:
      '<circle cx="12" cy="12" r="9"/>' +
      '<path d="M12 3v18"/>' +
      '<path d="M12 7h5.5M12 11h7M12 15h6M12 19h4"/>',

    // Zoom — minimalist diagnostic magnifier.
    zoom:
      '<circle cx="11" cy="11" r="7"/>' +
      '<path d="m20 20-3.5-3.5"/>' +
      '<path d="M11 8v6M8 11h6"/>',

    // Pan — precision 4-directional cross.
    pan:
      '<path d="M12 2v20M2 12h20"/>' +
      '<path d="M12 2l-3 3M12 2l3 3M12 22l-3-3M12 22l3-3"/>' +
      '<path d="M2 12l3-3M2 12l3 3M22 12l-3-3M22 12l3 3"/>',

    // Length — measurement caliper with end anchors.
    length:
      '<path d="m3 21 18-18"/>' +
      '<path d="M7.5 12.5l2 2M11 9l2 2M14.5 5.5l2 2"/>' +
      '<circle cx="3" cy="21" r="1.5" fill="currentColor"/>' +
      '<circle cx="21" cy="3" r="1.5" fill="currentColor"/>',

    // Bidirectional — double-ended caliper.
    bidi:
      '<path d="m3 21 18-18"/>' +
      '<path d="M3 21h4M3 21v-4M21 3h-4M21 3v4"/>' +
      '<path d="M8.5 15.5l7-7"/>',

    // Angle — geometric goniometer arc.
    angle:
      '<path d="M5 19h14M5 19l11-13"/>' +
      '<path d="M10 19a5 5 0 0 0-1.8-3.8"/>' +
      '<circle cx="5" cy="19" r="1.5" fill="currentColor"/>',

    // Cobb Angle — two endplates with a tilt arc.
    cobb:
      '<path d="M3 18.5h8M13 18.5h8"/>' +
      '<path d="M10.4 18.5 14.2 6.2"/>' +
      '<path d="M6.8 18.5a3.6 3.6 0 0 1 3.3-3.6"/>',

    // Probe — focal HU crosshair probe.
    probe:
      '<circle cx="12" cy="12" r="8"/>' +
      '<path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>' +
      '<circle cx="12" cy="12" r="1.5" fill="currentColor"/>',

    // MPR / 3D — isometric orthogonal slice cube.
    mpr:
      '<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3z"/>' +
      '<path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>' +
      '<path d="M8 5.25 16 9.75M16 14.25v-4.5"/>',

    // Layout — 2x2 matrix viewport grid.
    layout:
      '<rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/>' +
      '<rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/>' +
      '<rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/>' +
      '<rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/>',

    // Invert — contrast inversion circle with a filled quadrant.
    invert:
      '<path d="M12 3a9 9 0 1 0 9 9 9 9 0 0 0-9-9zm0 15a6 6 0 1 1 6-6 6 6 0 0 1-6 6z"/>' +
      '<path d="M12 6a6 6 0 0 1 6 6h-6z" fill="currentColor" fill-opacity="0.7"/>',

    // Reset — circular refresh arc (counter-clockwise).
    reset:
      '<path d="M3 12a9 9 0 1 0 2.64-6.36L3 8M3 3v5h5"/>',

    // Rotate — 90-degree orthogonal rotate arc.
    rotate:
      '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8M21 3v5h-5"/>',

    // Cine — minimalist playback trigger.
    cine:
      '<polygon points="7 4 19 12 7 20 7 4"/>',

    // Stack Scroll — layered slices with a step indicator.
    scroll:
      '<path d="M4 8.6 12 4.2l8 4.4-8 4.4z"/>' +
      '<path d="M4 12.4 12 16.8l8-4.4"/>' +
      '<path d="M4 16.2 12 20.6l8-4.4"/>',

    // Flip — mirror about a vertical axis.
    flip:
      '<path d="M12 3v18"/>' +
      '<path d="M9 6.5 3.5 12 9 17.5z"/>' +
      '<path d="M15 6.5 20.5 12 15 17.5z"/>',

    // Ellipse — closed contour with a diameter line.
    ellipse:
      '<ellipse cx="12" cy="12" rx="8.6" ry="6"/>' +
      '<path d="M3.4 12h17.2"/>',

    // Rectangle — closed ROI box.
    rectangle:
      '<rect x="4" y="6.5" width="16" height="11" rx="1.5"/>',

    // Freehand / ROI — diagnostic contour pen.
    roi:
      '<path d="m15 5 4 4L7 21H3v-4L15 5z"/>' +
      '<path d="M13 7l4 4"/>',

    // Arrow — diagnostic annotation arrow.
    arrow:
      '<path d="M5 19 19 5M19 5h-7M19 5v7"/>',

    // Clear — measurement eraser.
    clear:
      '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>' +
      '<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>' +
      '<path d="M10 11v6M14 11v6"/>',

    // Text / Label annotation.
    text:
      '<path d="M5 6.2h14M12 6.2v12.6"/>',

    // Capture / Download.
    capture:
      '<path d="M12 3.5v11.5"/>' +
      '<path d="M8.2 11.2 12 15l3.8-3.8"/>' +
      '<path d="M4.5 19.5h15"/>',

    // Undo / Redo — curved history arrows.
    undo:
      '<path d="M4 12h9.5a4.5 4.5 0 0 1 0 9H10"/>' +
      '<path d="M8 7.6 3.6 12 8 16.4"/>',
    redo:
      '<path d="M20 12h-9.5a4.5 4.5 0 0 0 0 9H14"/>' +
      '<path d="M16 7.6 20.4 12 16 16.4"/>',

    // Segmentation — stacked layers.
    segmentation:
      '<path d="M12 3.4 20.6 8 12 12.6 3.4 8z"/>' +
      '<path d="M3.4 12.6 12 17.2l8.6-4.6"/>' +
      '<path d="M3.4 16.6 12 21.2l8.6-4.6"/>',

    // Measurements / Report — document with findings lines.
    report:
      '<path d="M14 3.2H7.2a2 2 0 0 0-2 2v13.6a2 2 0 0 0 2 2h9.6a2 2 0 0 0 2-2V8z"/>' +
      '<path d="M14 3.2V8h4.8"/>' +
      '<path d="M9 13h6M9 16.6h4"/>',

    // Overflow / More.
    more:
      '<circle cx="6" cy="12" r="1.5"/>' +
      '<circle cx="12" cy="12" r="1.5"/>' +
      '<circle cx="18" cy="12" r="1.5"/>'
  };

  // ---------------------------------------------------------------------------
  // Ordered matcher: first rule that hits wins, so more specific labels must
  // come first (e.g. "Cobb Angle" before "Angle", "Bidirectional" before
  // "Length").
  //
  // Matching is WORD-BOUNDARY based, not substring based. Substring matching
  // produced two real bugs: "Rectangle" contains "angle", and "Freehand"
  // contains "hand" — both silently received the wrong glyph. `\b…s?\b` keeps
  // plurals working ("Measurements") while refusing those false hits.
  // ---------------------------------------------------------------------------
  var ICON_RULES = [
    { key: 'cobb',         words: ['cobb'] },
    { key: 'bidi',         words: ['bidirectional'] },
    { key: 'windowLevel',  words: ['window level', 'w/l', 'contrast', 'threshold', 'window', 'level', 'preset'] },
    { key: 'zoom',         words: ['zoom', 'magnify'] },
    { key: 'pan',          words: ['pan', 'hand'] },
    { key: 'scroll',       words: ['stack scroll', 'scroll'] },
    { key: 'cine',         words: ['cine', 'play', 'stop'] },
    { key: 'rectangle',    words: ['rectangle', 'box'] },
    { key: 'angle',        words: ['angle'] },
    { key: 'length',       words: ['length', 'ruler', 'caliper'] },
    { key: 'probe',        words: ['probe', 'crosshair', 'reference'] },
    { key: 'mpr',          words: ['mpr', '3d', 'volume', 'reformat'] },
    { key: 'layout',       words: ['layout', 'grid'] },
    { key: 'invert',       words: ['invert'] },
    { key: 'flip',         words: ['flip'] },
    { key: 'reset',        words: ['reset'] },
    { key: 'rotate',       words: ['rotate', 'reload'] },
    { key: 'ellipse',      words: ['ellipse', 'circle'] },
    { key: 'roi',          words: ['freehand', 'spline', 'livewire', 'brush', 'roi'] },
    { key: 'clear',        words: ['eraser', 'clear'] },
    { key: 'arrow',        words: ['arrow', 'annotate', 'annotation'] },
    { key: 'text',         words: ['text'] },
    { key: 'capture',      words: ['capture', 'download', 'export', 'save'] },
    { key: 'undo',         words: ['undo'] },
    { key: 'redo',         words: ['redo'] },
    { key: 'segmentation', words: ['segmentation', 'segment'] },
    { key: 'report',       words: ['measurement', 'report'] },
    { key: 'more',         words: ['more', 'overflow', 'options'] }
  ];

  function escapeRe(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  // Compiled once — the enhancement sweep must not rebuild regexes.
  var RULE_RE = ICON_RULES.map(function (rule) {
    return new RegExp('\\b' + rule.words.map(escapeRe).join('s?\\b|\\b') + 's?\\b', 'i');
  });

  function matchIconKey(label) {
    if (!label) return null;
    var text = String(label).trim();
    for (var i = 0; i < ICON_RULES.length; i++) {
      if (RULE_RE[i].test(text)) return ICON_RULES[i].key;
    }
    return null;
  }

  // ---------------------------------------------------------------------------
  // Icon injection
  // ---------------------------------------------------------------------------
  var ICON_FLAG = 'data-paxs-icon';
  var ICON_CLASS = 'paxs-modern-svg';

  // The label a tool exposes can live on any of these attributes depending on
  // the OHIF version / extension, so resolve all three.
  function resolveLabel(el) {
    if (!el || !el.getAttribute) return '';
    return (el.getAttribute('aria-label') ||
            el.getAttribute('data-cy') ||
            el.getAttribute('title') || '').trim();
  }

  function isChevron(svg) {
    if (!svg) return false;
    var cls = typeof svg.className === 'string' ? svg.className
      : (svg.getAttribute && svg.getAttribute('class')) || '';
    if (cls.indexOf('chevron') !== -1) return true;
    if (svg.getAttribute && svg.getAttribute('data-icon') === 'chevron') return true;
    // A dropdown caret is a single short polyline — treat it as decorative.
    var kids = svg.children;
    if (kids && kids.length === 1) {
      var d = kids[0].getAttribute && (kids[0].getAttribute('d') || '');
      if (/^M[\s\d.]*[lL]/.test(d) && d.length < 40) return true;
    }
    return false;
  }

  function toolIconSvg(btn) {
    var svgs = btn.querySelectorAll('svg');
    for (var i = 0; i < svgs.length; i++) {
      if (!isChevron(svgs[i])) return svgs[i];
    }
    return svgs.length ? svgs[0] : null;
  }

  function setSvgMarkup(svg, markup) {
    if (typeof svg.innerHTML === 'string') {
      svg.innerHTML = markup;
      return;
    }
    // Very old engines: namespace-aware fallback.
    var doc = new DOMParser().parseFromString(
      '<svg xmlns="http://www.w3.org/2000/svg">' + markup + '</svg>', 'image/svg+xml');
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var src = doc.documentElement;
    while (src.firstChild) svg.appendChild(document.importNode(src.firstChild, true));
  }

  /**
   * Swap the stock glyph for our modern line-art icon.
   *
   * Idempotency: the resolved icon id is stamped on the <svg> itself as
   * `data-paxs-icon="<id>"`. If the svg already carries that exact id we do
   * nothing — so repeated sweeps cannot loop. If the label changed (React
   * re-rendered the same svg for a different tool) the id differs and we
   * re-inject, which is the correct behaviour.
   */
  function injectIcon(btn) {
    if (!btn) return false;
    var key = matchIconKey(resolveLabel(btn));
    if (!key || !PAXS_ICONS[key]) return false;

    var svg = toolIconSvg(btn);
    if (!svg) return false;
    if (svg.getAttribute(ICON_FLAG) === key) return false; // already upgraded

    // Re-attribute the EXISTING svg node — never replace the element, so the
    // button's listeners, data-state and aria-checked survive untouched.
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.75');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    svg.classList.add('paxs-icon');
    svg.classList.add(ICON_CLASS);

    setSvgMarkup(svg, PAXS_ICONS[key]);

    svg.setAttribute(ICON_FLAG, key);
    return true;
  }

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------
  function isViewer() {
    return window.location.search.indexOf('StudyInstanceUIDs') !== -1 ||
           window.location.search.indexOf('study=') !== -1 ||
           window.location.pathname.indexOf('/viewer/viewer') !== -1;
  }

  function toolButtons() {
    var header = document.querySelector('header');
    if (!header) return [];
    var btns = header.querySelectorAll('button[aria-label], button[data-cy], button[title]');
    var out = [];
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      if (!resolveLabel(b)) continue;
      if (!b.querySelector('svg, img')) continue;
      out.push(b);
    }
    return out;
  }

  function isPressed(btn) {
    if (btn.getAttribute('data-state') === 'on') return true;
    if (btn.getAttribute('aria-checked') === 'true') return true;
    if (btn.getAttribute('aria-pressed') === 'true') return true;
    if (btn.getAttribute('data-active') === 'true') return true;
    var cls = typeof btn.className === 'string' ? btn.className : '';
    // Tailwind token OHIF uses for the pressed tool.
    if (cls.indexOf('bg-highlight') !== -1) return true;
    return false;
  }

  // ---------------------------------------------------------------------------
  // Tag the toolbar container: the lowest common ancestor of the tool buttons
  // that is still inside the header. Falls back to the buttons' direct parent.
  // ---------------------------------------------------------------------------
  function commonAncestor(nodes, stopAt) {
    if (!nodes.length) return null;
    var chain = [];
    var p = nodes[0].parentElement;
    while (p && p !== stopAt) { chain.push(p); p = p.parentElement; }
    for (var i = 0; i < chain.length; i++) {
      var cand = chain[i];
      var all = true;
      for (var j = 1; j < nodes.length; j++) {
        if (!cand.contains(nodes[j])) { all = false; break; }
      }
      if (all) return cand;
    }
    return nodes[0].parentElement;
  }

  function tagToolbar() {
    var header = document.querySelector('header');
    if (!header) return;
    var btns = toolButtons();
    if (!btns.length) return;

    // Group buttons by their nearest toolbar-ish ancestor: walk up from each
    // button until the ancestor contains >1 tool button.
    var groups = [];
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      var p = b.parentElement;
      var host = null;
      while (p && p !== header) {
        var count = 0;
        for (var j = 0; j < btns.length; j++) if (p.contains(btns[j])) count++;
        if (count > 1) { host = p; break; }
        p = p.parentElement;
      }
      if (!host) host = commonAncestor(btns, header);
      if (!host) continue;
      var found = false;
      for (var g = 0; g < groups.length; g++) if (groups[g].host === host) { found = true; break; }
      if (!found) groups.push({ host: host });
    }

    for (var gi = 0; gi < groups.length; gi++) {
      var host = groups[gi].host;
      host.classList.add('paxs-toolbar');
      var members = host.querySelectorAll('button[aria-label], button[data-cy], button[title]');
      for (var m = 0; m < members.length; m++) {
        var btn = members[m];
        if (!btn.querySelector('svg, img')) continue;

        var label = resolveLabel(btn);
        btn.classList.add('paxs-tool');
        // Category
        var cat = classify(label);
        for (var c = 0; c < ALL_CATEGORY.length; c++) btn.classList.remove(ALL_CATEGORY[c]);
        btn.classList.add(CATEGORY[cat]);
        // Active state (presentation only — OHIF still owns the real state)
        btn.classList.toggle('paxs-tool-active', isPressed(btn));
        // Swap the stock glyph for our modern line-art icon (idempotent).
        injectIcon(btn);
        // Icon refinement hook (also covers a chevron that we left in place).
        var svgs = btn.querySelectorAll('svg');
        for (var s = 0; s < svgs.length; s++) svgs[s].classList.add('paxs-icon');
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Live telemetry pill (right header section).
  //
  // Reads the read-only bridge exposed by paxs-mpr.js. Outside an active MPR
  // session the bridge returns null, so we fall back to the negotiated mode
  // reported by the volume endpoint (captured opportunistically) and finally
  // to an "idle" pill. The pill is decorative.
  // ---------------------------------------------------------------------------
  var MODE_STYLE = {
    server:  { cls: 'paxs-mode-server', icon: '☁', label: 'Server' },
    client:  { cls: 'paxs-mode-client', icon: '🖥', label: 'GPU Client' },
    split:   { cls: 'paxs-mode-split',  icon: '⚡', label: 'Hybrid' },
    hybrid:  { cls: 'paxs-mode-split',  icon: '⚡', label: 'Hybrid' },
    rendered:{ cls: 'paxs-mode-server', icon: '☁', label: 'Server' },
    idle:    { cls: 'paxs-mode-idle',   icon: '○', label: 'Idle' }
  };

  // Sniff the negotiate response header set by /volume/{uid}/negotiate.
  var lastNegotiated = null;
  (function sniffNegotiate() {
    if (!window.fetch || window.__paxsFetchSniffed) return;
    window.__paxsFetchSniffed = true;
    var orig = window.fetch;
    window.fetch = function (input, init) {
      var url = (typeof input === 'string') ? input : (input && input.url) || '';
      var p = orig.apply(this, arguments);
      if (url.indexOf('/volume/') !== -1 && url.indexOf('/negotiate') !== -1) {
        p.then(function (res) {
          try {
            var h = res.headers && res.headers.get && res.headers.get('X-PAXS-Processing-Mode');
            if (h) lastNegotiated = h;
          } catch (e) { /* opaque response */ }
        }).catch(function () {});
      }
      return p;
    };
  })();

  function telemetrySnapshot() {
    var t = null;
    try { t = window.__getPaxsMprState ? window.__getPaxsMprState() : null; } catch (e) { t = null; }
    var mode = (t && t.effectiveMode) || lastNegotiated || 'idle';
    var rtt = t && typeof t.rttMs === 'number' ? t.rttMs : null;
    var score = t && typeof t.clientScore === 'number' ? t.clientScore : null;
    var gpu = null;
    if (t && t.capabilities) gpu = t.capabilities.webgl2 ? 'WebGL2' : (t.capabilities.webgl ? 'WebGL1' : 'none');
    return { mode: mode, rtt: rtt, score: score, gpu: gpu };
  }

  function buildPill() {
    var el = document.createElement('div');
    el.className = 'paxs-telemetry';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.innerHTML =
      '<span class="paxs-tel-dot" aria-hidden="true"></span>' +
      '<span class="paxs-tel-icon" aria-hidden="true"></span>' +
      '<span class="paxs-tel-label"></span>' +
      '<span class="paxs-tel-tip" role="tooltip"></span>';
    return el;
  }

  function refreshPill() {
    var header = document.querySelector('header');
    if (!header) return;

    var pill = header.querySelector('.paxs-telemetry');
    if (!pill) {
      pill = buildPill();
      // Append as the last header child; CSS pins it to the right edge.
      header.appendChild(pill);
    }

    var snap = telemetrySnapshot();
    var st = MODE_STYLE[snap.mode] || MODE_STYLE.idle;

    pill.className = 'paxs-telemetry ' + st.cls;
    pill.querySelector('.paxs-tel-icon').textContent = st.icon;
    pill.querySelector('.paxs-tel-label').textContent = st.label;

    var tip = [];
    tip.push('Processing mode: ' + st.label);
    tip.push('Network RTT: ' + (snap.rtt == null ? 'not measured' : snap.rtt + ' ms'));
    tip.push('Client score: ' + (snap.score == null ? 'n/a' : snap.score + ' / 100'));
    tip.push('GPU: ' + (snap.gpu || 'not probed'));
    pill.querySelector('.paxs-tel-tip').textContent = tip.join('  ·  ');
    pill.title = tip.join('\n');
  }

  // ---------------------------------------------------------------------------
  // Patient / Study info header polish.
  //
  // Conditional by design: OHIF renders patient metadata in different places
  // depending on the layout. We only *tag* elements that already exist — we
  // never invent or move DOM, so nothing can regress if the layout changes.
  // ---------------------------------------------------------------------------
  var PATIENT_SELECTORS = [
    '[data-cy="patient-info"]',
    '[data-cy*="patientInfo"]',
    '[class*="patient-info"]',
    '[class*="PatientInfo"]',
    '[aria-label*="Patient"]'
  ];

  function tagPatientInfo() {
    for (var i = 0; i < PATIENT_SELECTORS.length; i++) {
      var nodes = document.querySelectorAll(PATIENT_SELECTORS[i]);
      for (var j = 0; j < nodes.length; j++) nodes[j].classList.add('paxs-patient-info');
    }
    // Modality badges rendered as short all-caps text nodes in the header.
    var spans = document.querySelectorAll('header span');
    for (var s = 0; s < spans.length; s++) {
      var txt = (spans[s].textContent || '').trim();
      if (/^(CT|MR|US|CR|DX|MG|PT|NM|XA|RF|OT|SR|SEG)$/.test(txt)) {
        spans[s].classList.add('paxs-modality-badge');
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Boot: tag once, then keep up with React re-renders.
  //
  // The observer is DEBOUNCED (~100ms). Toolbar state flips fire a burst of
  // mutations; without coalescing, each one would schedule its own sweep and
  // the page would thrash.
  // ---------------------------------------------------------------------------
  function enhance() {
    if (!isViewer()) return;
    tagToolbar();
    tagPatientInfo();
    refreshPill();
  }

  enhance();
  setTimeout(enhance, 400);
  setTimeout(enhance, 1200);

  var OBSERVER_DEBOUNCE_MS = 100;
  var pending = null;

  function scheduleEnhance() {
    if (pending) return;                 // already coalescing this burst
    pending = setTimeout(function () {
      pending = null;
      enhance();
    }, OBSERVER_DEBOUNCE_MS);
  }

  var mo = new MutationObserver(scheduleEnhance);
  mo.observe(document.body, { childList: true, subtree: true });

  // Low-frequency safety net for attribute-only changes (data-state flips) that
  // the MutationObserver above would otherwise miss on deep subtrees.
  setInterval(enhance, 1500);

  // Expose for diagnostics.
  window.__paxsToolbarEnhance = enhance;
})();
