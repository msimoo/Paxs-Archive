/* ==========================================================================
   PAXS Study List — thumbnails + modality icons
   Loaded from viewer/index.html alongside paxs-study-list-theme.css.

   For every study row in the study list it:
   1. reads the study UID + modalities from the QIDO /studies response
   2. resolves the first series -> first instance (cached per study)
   3. requests the small PNG from the /thumbnail WADO endpoint (cached)
   4. injects a modality-colored thumbnail tile into the row

   The tile is inserted INSIDE the <tr>, so sorting / pagination / React
   re-renders keep it attached to the right row (data-paxs-study key).
   ========================================================================== */
(function () {
  // -------------------------------------------------------------------------
  // H-11: the global HTMLCanvasElement.prototype.getContext patch that forced
  // `preserveDrawingBuffer: true` (and `desynchronized`/`powerPreference`) on
  // EVERY WebGL/WebGL2 context was removed. It inflated GPU VRAM and slowed
  // rendering across Cornerstone/OHIF viewports. The one canvas that is actually
  // read back — the MPR GPU renderer — now requests preserveDrawingBuffer on its
  // own getContext call (see paxs-mpr.js drawGl2). A duplicate of this patch in
  // viewer/index.html was removed at the same time; it ran first and was the one
  // that actually took effect.
  // -------------------------------------------------------------------------

  // -------------------------------------------------------------------------
  // Radix UI / Shadcn DialogContent Accessibility Fix
  // Automatically injects a hidden DialogTitle into dialogs lacking one,
  // resolving: "`DialogContent` requires a `DialogTitle` for the component to be accessible"
  // -------------------------------------------------------------------------
  (function installRadixDialogA11yFix() {
    function fixDialogs() {
      var dialogs = document.querySelectorAll('div[role="dialog"], div[data-radix-dialog-content], div[aria-modal="true"]');
      for (var i = 0; i < dialogs.length; i++) {
        var d = dialogs[i];
        if (!d.querySelector('h1, h2, h3, h4, h5, h6, [id*="radix-"][id*="title"], [data-radix-dialog-title]')) {
          var hiddenTitle = document.createElement('h2');
          hiddenTitle.textContent = d.getAttribute('aria-label') || 'Dialog';
          hiddenTitle.style.cssText = 'position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border-width:0;';
          hiddenTitle.setAttribute('data-radix-dialog-title', '1');
          d.insertBefore(hiddenTitle, d.firstChild);
        }
      }
    }
    setInterval(fixDialogs, 1000);
    var mo = new MutationObserver(fixDialogs);
    mo.observe(document.body, { childList: true, subtree: true });
  })();

  if (window.__paxsStudyEnhanceLoaded) return;
  window.__paxsStudyEnhanceLoaded = true;

  var MOD_COLORS = {
    CT: '#38bdf8', MR: '#a78bfa', DX: '#fb923c', CR: '#fb923c',
    US: '#34d399', PT: '#f472b6', NM: '#f472b6', XA: '#fbbf24',
    RF: '#fbbf24', MG: '#f87171', OT: '#2dd4bf'
  };

  var studies = [];      // QIDO /studies response (row order = render order)
  var thumbCache = {};   // studyUid -> object URL ('' = known-unavailable)
  var resolved = {};     // studyUid -> { series, sop } | null
  var resolving = {};    // studyUid -> in-flight promise

  // H-12 — bound the number of live thumbnail blob URLs. The list renders up to
  // 200 studies and the cache used to hold one un-revoked object URL per study
  // for the lifetime of the page. It is now an LRU: the oldest URL is revoked
  // once the cap is exceeded, and every cached URL is revoked on pagehide.
  var THUMB_CACHE_MAX = 64;
  var thumbOrder = [];   // LRU order of cached uids (oldest first)

  function _releaseThumb(uid) {
    var u = thumbCache[uid];
    if (u) { try { URL.revokeObjectURL(u); } catch (e) {} }
    delete thumbCache[uid];
    var i = thumbOrder.indexOf(uid);
    if (i !== -1) thumbOrder.splice(i, 1);
  }

  function _cacheThumb(uid, url) {
    if (thumbCache[uid] !== undefined) _releaseThumb(uid);
    thumbCache[uid] = url;
    thumbOrder.push(uid);
    while (thumbOrder.length > THUMB_CACHE_MAX) _releaseThumb(thumbOrder[0]);
  }

  function releaseAllThumbs() {
    thumbOrder.slice().forEach(_releaseThumb);
    thumbCache = {};
    thumbOrder = [];
  }

  // Revoke every cached blob URL when the page is being discarded. A bfcache
  // restore re-runs this script, so this only ever frees memory.
  window.addEventListener('pagehide', releaseAllThumbs);

  function pick(obj, tag) {
    try {
      var el = obj && obj[tag];
      if (!el || !el.Value || !el.Value.length) return '';
      var v = el.Value[0];
      if (el.vr === 'PN' && v && typeof v === 'object') return v.Alphabetic || '';
      if (Array.isArray(v)) return v[0];
      return String(v);
    } catch (e) { return ''; }
  }

  function studyKey(s) {
    return (pick(s, '00100010') + ' ' + pick(s, '00081030'))
      .replace(/\s+/g, ' ').trim().slice(0, 80);
  }

  function rowKey(row) {
    return (row.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  }

  function firstModality(s) {
    var m = String(pick(s, '00080061') || '').split(/[\^/]/)[0].trim().toUpperCase();
    return m || '';
  }

  // DICOM PN is Family^Given^Middle^Prefix^Suffix — derive "GF"-style initials
  // (Given first letter + Family first letter). Space-separated plain names
  // (first two words) are handled too; unparseable names fall back to '?'.
  function patientInitials(study) {
    var name = String(pick(study, '00100010') || '').trim();
    var first = function (s) { return s ? s.charAt(0).toUpperCase() : ''; };
    if (name.indexOf('^') !== -1) {
      var parts = name.split('^').map(function (p) { return p.trim(); }).filter(Boolean);
      if (parts.length) {
        var given = parts.length > 1 ? parts[1] : parts[0];
        return (first(given) + first(parts[0])) || '?';
      }
    }
    var words = name.split(/\s+/).filter(Boolean);
    if (words.length) {
      return (first(words[0]) + (words.length > 1 ? first(words[1]) : '')) || '?';
    }
    return '?';
  }

  function loadStudies() {
    fetch('/dicom-web/studies?limit=200&includefield=0020000D,00080061,00100010,00081030')
      .then(function (r) { return r.json(); })
      .then(function (arr) { studies = Array.isArray(arr) ? arr : []; })
      .catch(function () {});
  }

  function ensureResolved(uid) {
    if (uid in resolved) return Promise.resolve(resolved[uid]);
    if (resolving[uid]) return resolving[uid];
    resolving[uid] = fetch('/dicom-web/studies/' + encodeURIComponent(uid) +
        '/series?limit=1&includefield=0020000E,00080060')
      .then(function (r) { return r.json(); })
      .then(function (series) {
        if (!series || !series.length) { resolved[uid] = null; return null; }
        var suid = pick(series[0], '0020000E');
        if (!suid) { resolved[uid] = null; return null; }
        return fetch('/dicom-web/studies/' + encodeURIComponent(uid) +
            '/series/' + encodeURIComponent(suid) +
            '/instances?limit=1&includefield=00080018')
          .then(function (r) { return r.json(); })
          .then(function (inst) {
            var sop = inst && inst.length ? pick(inst[0], '00080018') : '';
            resolved[uid] = (suid && sop) ? { series: suid, sop: sop } : null;
            return resolved[uid];
          });
      })
      .catch(function () { resolved[uid] = null; return null; });
    return resolving[uid];
  }

  function thumbFor(uid) {
    if (uid in thumbCache) return Promise.resolve(thumbCache[uid]);
    return ensureResolved(uid).then(function (ref) {
      if (!ref) { thumbCache[uid] = ''; return ''; }
      var url = '/dicom-web/studies/' + encodeURIComponent(uid) +
        '/series/' + encodeURIComponent(ref.series) +
        '/instances/' + encodeURIComponent(ref.sop) + '/thumbnail';
      return fetch(url)
        .then(function (r) {
          if (!r.ok) { thumbCache[uid] = ''; return ''; }
          return r.blob().then(function (b) {
            var u = URL.createObjectURL(b);
            // H-12: cache through the LRU so the oldest blob URL is revoked
            // once the cap is reached (and never the one just created).
            _cacheThumb(uid, u);
            return u;
          });
        })
        .catch(function () { thumbCache[uid] = ''; return ''; });
    });
  }

  function matchStudy(row, i) {
    if (!studies.length) return null;
    var rk = rowKey(row);
    if (rk) {
      for (var j = 0; j < studies.length; j++) {
        var sk = studyKey(studies[j]);
        if (sk && rk.indexOf(sk) !== -1) return studies[j];
      }
    }
    return i < studies.length ? studies[i] : null;
  }

  function enhanceRow(row, i) {
    var study = matchStudy(row, i);
    var uid = study ? pick(study, '0020000D') : '';
    var cell = row.cells[0];
    if (!uid || !cell) return;
    // Drop stray tiles that ended up in other cells (React re-renders can
    // reuse a cell that once held a tile).
    for (var k = 1; k < row.cells.length; k++) {
      row.cells[k].querySelectorAll('.paxs-thumb').forEach(function (t) { t.remove(); });
    }
    // Re-inject when the td was replaced by a re-render (tile missing);
    // otherwise keep the existing tile attached to the row.
    if (row.dataset.paxsStudy === uid && cell.querySelector('.paxs-thumb')) return;
    row.dataset.paxsStudy = uid;

    var mod = firstModality(study);
    var color = MOD_COLORS[mod] || '#2dd4bf';

    var wrap = document.createElement('div');
    wrap.className = 'paxs-thumb';
    wrap.style.setProperty('--paxs-mod', color);

    var badge = document.createElement('span');
    badge.className = 'paxs-mod';
    badge.textContent = mod || 'IMG';
    wrap.appendChild(badge);

    // Graceful no-thumbnail fallback: patient-initials avatar in the modality
    // color. Visible by default; hidden once a real thumbnail loads (paxs-loaded)
    // or the img is hidden via inline display:none on error / no-instance.
    var avatar = document.createElement('span');
    avatar.className = 'paxs-avatar';
    avatar.textContent = patientInitials(study);
    avatar.setAttribute('aria-hidden', 'true');
    wrap.appendChild(avatar);

    // Native tooltip with the full patient name for capped/truncated cells
    // (re-set after React re-renders replace the cell contents).
    var nameBlock = cell.querySelector(':scope > .truncate');
    if (nameBlock && !nameBlock.getAttribute('title')) {
      nameBlock.setAttribute('title', (nameBlock.textContent || '').trim());
    }

    var img = document.createElement('img');
    img.alt = '';
    img.loading = 'lazy';
    img.onerror = function () {
      img.style.display = 'none';
      wrap.classList.add('paxs-nothumb');
    };
    wrap.appendChild(img);

    cell.insertBefore(wrap, cell.firstChild);

    thumbFor(uid).then(function (u) {
      if (u) {
        img.onload = function () { wrap.classList.add('paxs-loaded'); };
        img.src = u;
      } else {
        img.style.display = 'none';
        wrap.classList.add('paxs-nothumb');
      }
    });
  }

  function enhance() {
    var sl = document.querySelector('.paxs-sl');
    if (!sl) return;
    var rows = sl.querySelectorAll('tbody tr[class*="cursor-pointer"]');
    for (var i = 0; i < rows.length; i++) enhanceRow(rows[i], i);
  }

  loadStudies();
  // Poll briefly (cheap + idempotent via the data-paxs-study guard) so the
  // tiles appear even when the list mounts after the first timeout; the
  // observer then keeps them in sync across SPA re-renders.
  var polls = 0;
  var pollTimer = setInterval(function () {
    enhance();
    if (++polls > 20) clearInterval(pollTimer);
  }, 800);
  setTimeout(function () {
    enhance();
    var mo = new MutationObserver(function () { requestAnimationFrame(enhance); });
    mo.observe(document.body, { childList: true, subtree: true });
  }, 400);

  // -------------------------------------------------------------------------
  // Replace and Style Top-Left Logo Image in PAXS Viewer Header
  // -------------------------------------------------------------------------
  function updateViewerLogo() {
    var imgs = document.querySelectorAll('header img, div[class*="header"] img, nav img');
    for (var i = 0; i < imgs.length; i++) {
      var img = imgs[i];
      if (img.src.indexOf('logo') !== -1 || img.alt.toLowerCase().indexOf('logo') !== -1 || img.closest('header')) {
        if (img.src.indexOf('/static/logo/logo.png') === -1) {
          img.src = '/static/logo/logo.png';
        }
        img.style.height = '32px';
        img.style.width = 'auto';
        img.style.objectFit = 'contain';
      }
    }
  }

  setInterval(updateViewerLogo, 1000);
  setTimeout(updateViewerLogo, 300);

  // -------------------------------------------------------------------------
  // Intercept Top-Left Logo Button in Paxs Viewer
  // Forces clicking the top-left logo/brand link to open our upgraded PACS Study List UI at /viewer
  // -------------------------------------------------------------------------
  document.addEventListener('click', function (e) {
    var target = e.target;
    if (!target) return;
    // Match specific top-left header logo image or brand links
    var logoElem = target.closest(
      'header a[href="/"], header a[href="/viewer"], header a[href="/viewer/"], ' +
      'img[src*="PaxsViewer-logo"], img[src*="logomain"], ' +
      'button[data-cy="header-study-list-button"]'
    );
    if (logoElem) {
      var isViewingStudy = window.location.search.indexOf('StudyInstanceUIDs') !== -1 ||
                           window.location.search.indexOf('study') !== -1;
      if (isViewingStudy) {
        e.preventDefault();
        e.stopPropagation();
        window.location.href = '/viewer';
        return false;
      }
    }
  }, true);

  // -------------------------------------------------------------------------
  // Inject PAXS Archive Site Footer inside DICOM Image Viewer
  // -------------------------------------------------------------------------
  function injectViewerFooter() {
    var isViewingStudy = window.location.search.indexOf('StudyInstanceUIDs') !== -1 ||
                         window.location.search.indexOf('study') !== -1 ||
                         window.location.pathname.indexOf('/viewer/viewer') !== -1;
    if (!isViewingStudy) return;
    if (document.querySelector('.paxs-viewer-foot')) return;

    var foot = document.createElement('footer');
    foot.className = 'paxs-viewer-foot';
    foot.innerHTML = 
      '<div class="foot-glow" aria-hidden="true"></div>' +
      '<div class="foot-brand">' +
        '<svg width="16" height="16" viewBox="0 0 32 32" fill="none" aria-hidden="true">' +
          '<rect x="4" y="6" width="24" height="20" rx="4" stroke="#22d3ee" stroke-width="2"/>' +
          '<path d="M4 12h24M4 18h24M10 6v20M22 6v20" stroke="#2dd4bf" stroke-width="1.5"/>' +
        '</svg>' +
        '<span>PAXS Archive · clinical imaging platform</span>' +
      '</div>' +
      '<div class="foot-copy">' +
        '<span class="foot-heart" aria-hidden="true">♥</span>' +
        '<span>© 2026 <b class="foot-name">Mohamed O. Elsamani</b></span>' +
        '<span class="foot-sep" aria-hidden="true"></span>' +
        '<a class="foot-link" href="https://elsamanii.rf.gd" target="_blank" rel="noopener">' +
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14 21 3"/></svg>' +
          'elsamanii.rf.gd' +
        '</a>' +
        '<span class="foot-sep" aria-hidden="true"></span>' +
        '<button class="foot-link terms-link" id="paxs-foot-terms" type="button" title="Terms & Conditions and Privacy Policy" style="background:none;border:none;cursor:pointer;padding:0;">' +
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6M9 17h6"/></svg>' +
          'Terms & Conditions' +
        '</button>' +
        '<span class="foot-sep" aria-hidden="true"></span>' +
        '<button class="foot-link report-link" id="paxs-foot-report" type="button" title="Prepare or Print Diagnostic Report" style="background:none;border:none;cursor:pointer;padding:0;color:#34d399;">' +
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/></svg>' +
          '📝 Diagnostic Report' +
        '</button>' +
        '<span class="foot-sep" aria-hidden="true"></span>' +
        '<a class="foot-link guide-link" id="paxs-foot-guide" href="/paxs-viewer-guide" target="_blank" rel="noopener" title="Open the PaxsViewer guide (toolbar, layouts, measurements, reports)" style="background:none;border:none;cursor:pointer;padding:0;color:#22d3ee;text-decoration:none;">' +
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>' +
          '📖 Viewer Guide' +
        '</a>' +
      '</div>' +
      '<div class="foot-pulse" aria-hidden="true"><span></span><span></span><span></span><span></span><span></span></div>';

    document.body.appendChild(foot);

    var termsBtn = document.getElementById('paxs-foot-terms');
    if (termsBtn) {
      termsBtn.addEventListener('click', function (e) {
        e.preventDefault();
        alert('PAXS Archive Terms & Conditions:\n\n1. Diagnostic Disclaimer: This system is engineered for DICOM medical image archiving and web visualization.\n2. Data Integrity: All imaging operations are logged into a tamper-evident audit trail.\n3. Privacy & PHI: Data is protected via AES-TLS encryption.\n\nDeveloped by Mohamed O. Elsamani (elsamanii.rf.gd)');
      });
    }

    var reportBtn = document.getElementById('paxs-foot-report');
    if (reportBtn) {
      reportBtn.addEventListener('click', function (e) {
        e.preventDefault();
        openReportModal();
      });
    }

    // Auto-reveal footer when mouse approaches bottom 30px of the screen
    window.addEventListener('mousemove', function (e) {
      if (foot) {
        var threshold = window.innerHeight - 30;
        if (e.clientY >= threshold) {
          foot.classList.add('paxs-foot-visible');
        } else if (e.clientY < window.innerHeight - 60) {
          foot.classList.remove('paxs-foot-visible');
        }
      }
    });
  }

  // Inject footer on load and keep active
  setTimeout(injectViewerFooter, 500);
  setInterval(injectViewerFooter, 1500);

  // Colorize toolbar icons on load and keep active
  setTimeout(colorizeToolbarIcons, 500);
  setInterval(colorizeToolbarIcons, 2000);

  // -------------------------------------------------------------------------
  // Colorize Paxs Toolbar Icons by Tool Category
  // Applies colored CSS classes and SVG icon colors to toolbar buttons
  // based on their aria-label classification.
  // -------------------------------------------------------------------------
  var NAV_LABELS = ['Zoom', 'Pan', 'Stack Scroll', 'Magnify', 'Flip', 'Rotate', 'Reset', 'Invert', 'Cine'];
  var MEASURE_LABELS = ['Length', 'Bidirectional', 'Ellipse', 'Circle', 'Rectangle', 'Angle', 'Cobb', 'Probe', 'Spline', 'LiveWire', 'Freehand', 'Planar Freehand', 'Area'];
  var WL_LABELS = ['Window Level', 'W/L', 'Contrast', 'Threshold', 'Window', 'Level'];
  var ANNOT_LABELS = ['Arrow', 'Text', 'Annotation', 'Eraser', 'Calibration', 'Crosshairs', 'Reference', 'Bookmark', 'Key Image', 'SR', 'Report'];

  var TOOL_COLORS = {
    nav:     '#38bdf8',
    measure: '#34d399',
    wl:      '#fbbf24',
    annot:   '#a78bfa',
    def:     '#94a3b8'
  };

  var TOOL_CLASSES = {
    nav:     'paxs-tool-nav',
    measure: 'paxs-tool-measure',
    wl:      'paxs-tool-wl',
    annot:   'paxs-tool-annot',
    def:     'paxs-tool-default'
  };

  var ALL_TOOL_CLASSES = [
    TOOL_CLASSES.nav, TOOL_CLASSES.measure, TOOL_CLASSES.wl,
    TOOL_CLASSES.annot, TOOL_CLASSES.def
  ];

  function matchesAny(label, list) {
    if (!label) return false;
    var lower = label.toLowerCase();
    for (var i = 0; i < list.length; i++) {
      if (lower.indexOf(list[i].toLowerCase()) !== -1) return true;
    }
    return false;
  }

  function classifyLabel(label) {
    if (!label) return 'def';
    if (matchesAny(label, NAV_LABELS))     return 'nav';
    if (matchesAny(label, MEASURE_LABELS)) return 'measure';
    if (matchesAny(label, WL_LABELS))      return 'wl';
    if (matchesAny(label, ANNOT_LABELS))   return 'annot';
    return 'def';
  }

  function removeToolClasses(el) {
    if (!el) return;
    for (var i = 0; i < ALL_TOOL_CLASSES.length; i++) {
      el.classList.remove(ALL_TOOL_CLASSES[i]);
    }
  }

  function applyToolColor(btn) {
    if (!btn) return;
    var label = btn.getAttribute('aria-label');
    var cat = classifyLabel(label);
    var cls = TOOL_CLASSES[cat];

    // Remove any previous tool class, then add the new one. Presentation
    // (icon size, padding, colour, active glow) is owned entirely by
    // paxs-viewer-theme.css + paxs-toolbar-enhance.js — this function must NOT
    // write inline styles on the SVG, or it would fight the stylesheet on the
    // 2s interval below and win (inline beats class rules).
    removeToolClasses(btn);
    btn.classList.add(cls);
    btn.classList.add('paxs-tool');
  }

  function colorizeToolbarIcons() {
    // Only run inside the viewer (not on the study list)
    var isViewingStudy = window.location.search.indexOf('StudyInstanceUIDs') !== -1 ||
                         window.location.search.indexOf('study') !== -1 ||
                         window.location.pathname.indexOf('/viewer/viewer') !== -1;
    if (!isViewingStudy) return;

    // Strategy 1: header toolbar buttons with aria-label
    var headerBtns = document.querySelectorAll('header button[aria-label]');
    if (headerBtns && headerBtns.length) {
      for (var i = 0; i < headerBtns.length; i++) {
        applyToolColor(headerBtns[i]);
      }
    }

    // Strategy 2: buttons inside Paxs's relative-flex toolbar containers
    var containers = document.querySelectorAll('div[class*="relative flex justify-center"]');
    if (containers && containers.length) {
      for (var c = 0; c < containers.length; c++) {
        if (!containers[c]) continue;
        var btns = containers[c].querySelectorAll('button[aria-label]');
        if (btns && btns.length) {
          for (var j = 0; j < btns.length; j++) {
            applyToolColor(btns[j]);
          }
        }
      }
    }
  }

  // -------------------------------------------------------------------------
  // Report Editor Modal Engine inside DICOM Viewer & Study List
  // -------------------------------------------------------------------------
  function openReportModal(studyUid) {
    var match = (studyUid ? studyUid : '') ||
                (window.location.search.match(/StudyInstanceUIDs=([^&]+)/) ||
                 window.location.search.match(/study=([^&]+)/) || [])[1];
    if (!match) {
      alert('Please open or select a study to write/edit a report.');
      return;
    }
    var uid = decodeURIComponent(match);

    var modalId = 'paxs-report-modal-wrap';
    var existing = document.getElementById(modalId);
    if (existing) existing.remove();

    var wrap = document.createElement('div');
    wrap.id = modalId;
    wrap.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(2,6,23,0.85);backdrop-filter:blur(10px);' +
      'display:flex;align-items:center;justify-content:center;font-family:"Inter",system-ui,sans-serif;color:#f8fafc;';

    wrap.innerHTML = 
      '<div style="background:#0f172a;border:1px solid rgba(51,65,85,0.8);border-radius:12px;padding:24px;width:720px;max-width:92vw;max-height:90vh;overflow-y:auto;box-shadow:0 25px 60px rgba(0,0,0,0.8);">' +
        '<div style="display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid rgba(51,65,85,0.6);padding-bottom:12px;margin-bottom:16px;">' +
          '<h3 style="margin:0;font-size:16px;font-weight:700;color:#38bdf8;display:flex;align-items:center;gap:8px;">📝 Diagnostic Report Editor</h3>' +
          '<button id="paxs-rpt-close" style="background:none;border:none;color:#94a3b8;font-size:20px;cursor:pointer;">✕</button>' +
        '</div>' +
        '<div id="paxs-rpt-loading" style="padding:20px;text-align:center;color:#94a3b8;font-family:monospace;">Loading study DICOM metadata...</div>' +
        '<div id="paxs-rpt-form-body" style="display:none;">' +
          '<div style="background:#1e293b;border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:12.5px;display:grid;grid-template-columns:1fr 1fr;gap:10px;">' +
            '<div><b>Patient:</b> <span id="paxs-meta-patient">--</span></div>' +
            '<div><b>Patient ID:</b> <code id="paxs-meta-pid">--</code></div>' +
            '<div><b>Modality:</b> <span id="paxs-meta-mod">--</span></div>' +
            '<div><b>Accession #:</b> <span id="paxs-meta-acc">--</span></div>' +
            '<div style="grid-column:span 2;"><b>Description:</b> <span id="paxs-meta-desc">--</span></div>' +
          '</div>' +
          '<div style="margin-bottom:14px;">' +
            '<label style="display:block;font-size:12px;font-weight:600;color:#94a3b8;margin-bottom:4px;">Insert Standard Report Template</label>' +
            '<select id="paxs-rpt-tmpl-select" style="width:100%;padding:8px;background:#1e293b;border:1px solid #334155;border-radius:6px;color:#fff;font-size:13px;">' +
              '<option value="">-- Choose Template --</option>' +
            '</select>' +
          '</div>' +
          '<div style="margin-bottom:14px;">' +
            '<label style="display:block;font-size:12px;font-weight:600;color:#94a3b8;margin-bottom:4px;">Diagnostic Findings</label>' +
            '<textarea id="paxs-rpt-findings" style="width:100%;height:130px;padding:10px;background:#1e293b;border:1px solid #334155;border-radius:6px;color:#fff;font-family:monospace;font-size:12.5px;resize:vertical;" placeholder="Enter diagnostic findings..."></textarea>' +
          '</div>' +
          '<div style="margin-bottom:16px;">' +
            '<label style="display:block;font-size:12px;font-weight:600;color:#94a3b8;margin-bottom:4px;">Impression & Conclusion</label>' +
            '<textarea id="paxs-rpt-impression" style="width:100%;height:80px;padding:10px;background:#1e293b;border:1px solid #334155;border-radius:6px;color:#fff;font-family:monospace;font-size:12.5px;resize:vertical;" placeholder="Enter impression..."></textarea>' +
          '</div>' +
          '<div style="display:flex;align-items:center;justify-content:space-between;border-top:1px solid rgba(51,65,85,0.6);padding-top:14px;">' +
            '<button id="paxs-rpt-print-btn" style="background:#0284c7;color:#fff;border:none;border-radius:6px;padding:8px 16px;font-weight:700;font-size:12.5px;cursor:pointer;">🖨 Print Report</button>' +
            '<div style="display:flex;gap:10px;">' +
              '<button id="paxs-rpt-draft-btn" style="background:#334155;color:#fff;border:none;border-radius:6px;padding:8px 16px;font-weight:600;font-size:12.5px;cursor:pointer;">💾 Save Draft</button>' +
              '<button id="paxs-rpt-final-btn" style="background:linear-gradient(135deg,#10b981,#059669);color:#fff;border:none;border-radius:6px;padding:8px 18px;font-weight:700;font-size:12.5px;cursor:pointer;">✅ Finalize & Sign</button>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>';

    document.body.appendChild(wrap);

    document.getElementById('paxs-rpt-close').onclick = function () { wrap.remove(); };

    paxsFetch('/api/v1/reports/study/' + encodeURIComponent(uid), { headers: paxsAuthHeaders() })
      .then(function (r) { return r.json(); })
      .then(function (meta) {
        document.getElementById('paxs-rpt-loading').style.display = 'none';
        document.getElementById('paxs-rpt-form-body').style.display = 'block';

        document.getElementById('paxs-meta-patient').textContent = meta.patient_name || 'UNKNOWN';
        document.getElementById('paxs-meta-pid').textContent = meta.patient_id || '--';
        document.getElementById('paxs-meta-mod').textContent = meta.modality || 'CT';
        document.getElementById('paxs-meta-acc').textContent = meta.accession_number || '--';
        document.getElementById('paxs-meta-desc').textContent = meta.study_description || '--';

        if (meta.report) {
          document.getElementById('paxs-rpt-findings').value = meta.report.findings || '';
          document.getElementById('paxs-rpt-impression').value = meta.report.impression || '';
        }

        fetch('/api/v1/templates?modality=' + encodeURIComponent(meta.modality || ''), { headers: paxsAuthHeaders() })
          .then(function (tr) { return tr.json(); })
          .then(function (tmpls) {
            var sel = document.getElementById('paxs-rpt-tmpl-select');
            if (tmpls && tmpls.length) {
              for (var i = 0; i < tmpls.length; i++) {
                var opt = document.createElement('option');
                opt.value = i;
                opt.textContent = tmpls[i].title;
                sel.appendChild(opt);
              }
              sel.onchange = function () {
                var idx = sel.value;
                if (idx !== '' && tmpls[idx]) {
                  var t = tmpls[idx];
                  if (t.body_findings_template) document.getElementById('paxs-rpt-findings').value = t.body_findings_template;
                  if (t.default_impression_template) document.getElementById('paxs-rpt-impression').value = t.default_impression_template;
                }
              };
            }
          }).catch(function () {});

        document.getElementById('paxs-rpt-draft-btn').onclick = function () { saveRpt('DRAFT'); };
        // C06: finalizing/signing a report is a privileged action. The server
        // enforces it; the UI must not offer a button that will be refused.
        var paxsCanFinalize = !!paxsUser &&
          (paxsUser.role === 'ADMIN' || paxsUser.role === 'RADIOLOGIST');
        var paxsFinalBtn = document.getElementById('paxs-rpt-final-btn');
        if (!paxsCanFinalize) {
          paxsFinalBtn.disabled = true;
          paxsFinalBtn.title = 'Finalizing a report requires a radiologist or admin role';
          paxsFinalBtn.style.opacity = '0.45';
          paxsFinalBtn.style.cursor = 'not-allowed';
        }
        paxsFinalBtn.onclick = function () {
          if (!paxsCanFinalize) { return; }
          saveRpt('FINALIZED');
        };
        document.getElementById('paxs-rpt-print-btn').onclick = function () {
          window.open('/api/v1/reports/print/' + encodeURIComponent(uid), '_blank');
        };

        function saveRpt(status) {
          var payload = {
            study_instance_uid: uid,
            findings: document.getElementById('paxs-rpt-findings').value,
            impression: document.getElementById('paxs-rpt-impression').value,
            status: status
          };
          paxsFetch('/api/v1/reports/save', {
            method: 'POST',
            headers: paxsAuthHeaders(),
            body: JSON.stringify(payload)
          })
          .then(function (sr) {
            return sr.json().catch(function () { return {}; }).then(function (body) {
              return { ok: sr.ok, code: sr.status, body: body };
            });
          })
          .then(function (res) {
            if (!res.ok) {
              // e.g. 403 for a non-radiologist, or 409 when the report is already
              // finalized — surface the server's reason instead of "success".
              alert('Could not save the report: ' +
                ((res.body && res.body.detail) ? res.body.detail : 'HTTP ' + res.code));
              return;
            }
            alert('Report ' + (status === 'FINALIZED' ? 'finalized and signed successfully!' : 'saved as draft!'));
            if (status === 'FINALIZED') wrap.remove();
          })
          .catch(function (err) { alert('Error saving report: ' + err); });
        }
      })
      .catch(function (e) {
        document.getElementById('paxs-rpt-loading').textContent = 'Error loading study DICOM metadata: ' + e;
      });
  }

  window.paxsOpenReportModal = openReportModal;

  // -------------------------------------------------------------------------
  // Left Panel (viewerLayoutResizableLeftPanel) Live UX Enhancer
  // Enriches series thumbnail cards with modality icons, 3D volume badges,
  // and smooth hover dynamics.
  // -------------------------------------------------------------------------
  var MODALITY_ICONS = {
    CT: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/></svg>',
    MR: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M6 3v18M18 3v18M6 12h12M10 7l4 10"/></svg>',
    DX: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9l6 6M15 9l-6 6"/></svg>',
    US: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M2 12h20M6 8c3 0 5 8 8 8s5-8 8-8"/></svg>',
    PT: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="8"/><path d="M12 2v20M2 12h20"/></svg>'
  };

  function enhanceLeftSidebarSeries() {
    var match = window.location.search.match(/StudyInstanceUIDs=([^&]+)/) ||
                window.location.search.match(/study=([^&]+)/);
    var isViewingStudy = Boolean(match && match[1]);
    if (!isViewingStudy) return;
    var currentStudyUid = decodeURIComponent(match[1]);

    var panel = document.getElementById('viewerLayoutResizableLeftPanel') ||
                document.querySelector('div[class*="viewerLayoutResizableLeftPanel"]') ||
                document.querySelector('div[data-cy="study-browser"]') ||
                document.querySelector('div[class*="study-browser"]');
    if (!panel) return;

    // Strict Study Isolation: Hide any other patient/study drawers if present
    // so only the active selected study's series and instances are displayed
    var allStudyBlocks = panel.querySelectorAll('div[data-cy*="study-item"], div[class*="study-item"], div[class*="StudyItem"]');
    for (var b = 0; b < allStudyBlocks.length; b++) {
      var block = allStudyBlocks[b];
      var blockUid = block.getAttribute('data-study-uid') || block.getAttribute('data-uid') || '';
      if (blockUid && blockUid !== currentStudyUid) {
        block.style.display = 'none';
      }
    }

    // Target the study browser top bar: div.w-100.bg-background.flex.h-[48px]
    // Automatically switch and lock to "Primary" mode so only the loaded study's series appear
    var topBars = panel.querySelectorAll('div[class*="h-[48px]"], div[class*="justify-center"][class*="gap-"], div[data-cy*="study-browser-tabs"]');
    for (var tb = 0; tb < topBars.length; tb++) {
      var barBtns = topBars[tb].querySelectorAll('button, div[role="button"], div[role="tab"]');
      for (var bb = 0; bb < barBtns.length; bb++) {
        var btnTxt = (barBtns[bb].textContent || '').trim().toLowerCase();
        if (btnTxt === 'primary' || btnTxt.indexOf('primary') !== -1) {
          if (!barBtns[bb].hasAttribute('data-paxs-primary-active')) {
            barBtns[bb].setAttribute('data-paxs-primary-active', '1');
            barBtns[bb].click();
          }
          break;
        }
      }
    }

    // Auto-select "Primary" tab across all tab/button groups inside the panel
    var allTabs = panel.querySelectorAll('button[role="tab"], button[data-cy*="primary"], button[value="primary"]');
    for (var t = 0; t < allTabs.length; t++) {
      var tabTxt = (allTabs[t].textContent || '').trim().toLowerCase();
      if (tabTxt === 'primary' || tabTxt.indexOf('primary') !== -1 || allTabs[t].getAttribute('value') === 'primary') {
        if (!allTabs[t].hasAttribute('data-paxs-default-selected')) {
          allTabs[t].setAttribute('data-paxs-default-selected', '1');
          allTabs[t].click();
        }
        break;
      }
    }

    // Enhance all thumbnail containers & default primary series selection
    var cards = panel.querySelectorAll('div[class*="cursor-pointer"], button[class*="cursor-pointer"], div[data-cy*="study-browser-thumbnail"]');
    var hasActive = false;
    for (var a = 0; a < cards.length; a++) {
      if (cards[a].classList.contains('paxs-active-series') || cards[a].getAttribute('data-state') === 'selected' || cards[a].getAttribute('aria-selected') === 'true') {
        hasActive = true;
        break;
      }
    }

    // Default select the first primary series if none is highlighted yet
    if (!hasActive && cards.length > 0) {
      cards[0].classList.add('paxs-active-series');
      cards[0].setAttribute('data-state', 'selected');
    }

    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];

      // Add click handler to toggle active state instantly
      if (!card.getAttribute('data-paxs-click-bound')) {
        card.setAttribute('data-paxs-click-bound', '1');
        card.addEventListener('click', function () {
          for (var k = 0; k < cards.length; k++) {
            cards[k].classList.remove('paxs-active-series');
            cards[k].removeAttribute('data-state');
          }
          this.classList.add('paxs-active-series');
          this.setAttribute('data-state', 'selected');
        });
      }

      if (card.getAttribute('data-paxs-enhanced')) continue;
      card.setAttribute('data-paxs-enhanced', '1');

      // Find modality label
      var spans = card.querySelectorAll('span, div');
      for (var s = 0; s < spans.length; s++) {
        var txt = (spans[s].textContent || '').trim().toUpperCase();
        if (txt === 'CT' || txt === 'MR' || txt === 'DX' || txt === 'US' || txt === 'PT' || txt === 'CR' || txt === 'NM') {
          spans[s].classList.add('paxs-modality-pill');
          if (MODALITY_ICONS[txt] && !spans[s].querySelector('svg')) {
            spans[s].innerHTML = MODALITY_ICONS[txt] + ' ' + txt;
          }
          break;
        }
      }
    }
  }

  setInterval(enhanceLeftSidebarSeries, 1000);
  setTimeout(enhanceLeftSidebarSeries, 400);

  // -------------------------------------------------------------------------
  // Signed-in user state + Sign out in the Study List header
  //
  // Fetches /api/v1/auth/me (the HttpOnly session cookie rides along on the
  // same-origin fetch) and renders an avatar + name + role chip with a Sign
  // out button at the right of the header. Sign out POSTs to
  // /api/v1/auth/logout — which revokes the server-side session row and
  // clears the cookie — then bounces to /login; the auth gate returns the
  // user to /viewer on their next sign-in.
  //
  // Styling is inline (this page does not load paxs-study-list-theme.css),
  // matching the footer and report-modal blocks above.
  // -------------------------------------------------------------------------
  var PAXS_USER_STYLE = document.createElement('style');
  PAXS_USER_STYLE.textContent =
    '.paxs-user-chip{display:inline-flex;align-items:center;gap:8px;margin-left:10px;' +
    'padding:4px 6px 4px 4px;background:rgba(17,28,48,.9);border:1px solid rgba(148,163,184,.28);' +
    'border-radius:999px;flex:none;box-shadow:0 4px 14px rgba(0,0,0,.25)}' +
    '.paxs-user-chip .paxs-user-avatar{width:26px;height:26px;border-radius:50%;display:grid;' +
    'place-items:center;font-weight:800;font-size:12px;color:#04121f;' +
    'box-shadow:inset 0 0 0 1px rgba(255,255,255,.28)}' +
    '.paxs-user-chip .paxs-user-id{display:flex;flex-direction:column;line-height:1.1;min-width:0}' +
    '.paxs-user-chip .paxs-user-name{font-size:11.5px;font-weight:700;color:#e2e8f0;' +
    'white-space:nowrap;max-width:130px;overflow:hidden;text-overflow:ellipsis}' +
    '.paxs-user-chip .paxs-user-role{font-size:9px;font-weight:700;letter-spacing:.08em;' +
    'text-transform:uppercase;color:#22d3ee}' +
    '.paxs-user-chip .paxs-user-signout{margin-left:2px;padding:4px 10px;background:transparent;' +
    'border:1px solid rgba(148,163,184,.28);color:#8b9bb4;border-radius:999px;' +
    'font-size:10.5px;font-weight:600;cursor:pointer;transition:color .18s,border-color .18s}' +
    '.paxs-user-chip .paxs-user-signout:hover{color:#f87171;border-color:rgba(248,113,113,.5)}' +
    '.paxs-user-chip .paxs-user-signout:disabled{opacity:.55;cursor:wait}' +
    ':root[data-theme="light"] .paxs-user-chip{background:#fff;border-color:rgba(15,23,42,.12)}' +
    ':root[data-theme="light"] .paxs-user-chip .paxs-user-name{color:#0f172a}'; 
  document.head.appendChild(PAXS_USER_STYLE);

  var PAXS_AVATAR_PALETTE = ['#22d3ee', '#a78bfa', '#34d399', '#fbbf24', '#fb7185', '#38bdf8'];
  var paxsUser = null;
  var paxsUserFetched = false;

  function paxsAvatarColor(name) {
    var h = 0;
    for (var i = 0; i < String(name || '?').length; i++) h = (h * 31 + String(name).charCodeAt(i)) >>> 0;
    return PAXS_AVATAR_PALETTE[h % PAXS_AVATAR_PALETTE.length];
  }

  function paxsEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // -------------------------------------------------------------------------
  // Silent refresh on page load
  //
  // The access token is short-lived (15 min) but the HttpOnly refresh cookie
  // lives 7 days. On every study-list / viewer page load we rotate it once
  // (POST /api/v1/auth/refresh) and store the fresh access token so the
  // report modal and any bearer-protected calls start with a valid token
  // instead of waiting for a 401. Guarded so a revoked/expired session or a
  // public share link (no cookie) never triggers a redirect loop.
  // -------------------------------------------------------------------------
  var paxsRefreshDone = false;

  // H14: the session cookie is HttpOnly, so `document.cookie` can never see it
  // — the previous guard therefore skipped refresh unconditionally. Refresh is
  // now driven by the response (401) rather than by a JS-visible cookie.
  var paxsRefreshInFlight = null;

  function paxsRefreshToken() {
    // Single flight: concurrent 401s share one refresh call.
    if (!paxsRefreshInFlight) {
      paxsRefreshInFlight = fetch('/api/v1/auth/refresh', {
        method: 'POST',
        credentials: 'same-origin',
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (d && d.access_token) {
            try { localStorage.setItem('paxs_token', d.access_token); } catch (e) {}
            return d.access_token;
          }
          return null;
        })
        .catch(function () { return null; })
        .then(function (tok) {
          paxsRefreshInFlight = null;
          return tok;
        });
    }
    return paxsRefreshInFlight;
  }

  // Centralized authenticated fetch:
  //   * attaches the bearer token
  //   * on 401, refreshes once and retries the original request exactly once
  //   * never retries the refresh endpoint itself (loop protection)
  function paxsFetch(url, opts) {
    opts = opts || {};
    var isRefreshCall = String(url).indexOf('/api/v1/auth/refresh') !== -1;

    function attempt(retried) {
      var o = {};
      for (var k in opts) { if (Object.prototype.hasOwnProperty.call(opts, k)) o[k] = opts[k]; }
      o.credentials = 'same-origin';
      o.headers = {};
      var base = opts.headers || {};
      for (var h in base) { if (Object.prototype.hasOwnProperty.call(base, h)) o.headers[h] = base[h]; }
      try {
        var t = localStorage.getItem('paxs_token');
        if (t) o.headers['Authorization'] = 'Bearer ' + t;
      } catch (e) {}

      return fetch(url, o).then(function (res) {
        if (res.status !== 401 || retried || isRefreshCall) return res;
        return paxsRefreshToken().then(function (tok) {
          if (!tok) return res;      // refresh failed: surface the original 401
          return attempt(true);      // exactly one retry
        });
      });
    }

    return attempt(false);
  }

  // -------------------------------------------------------------------------
  // V1-1 — transport-level refresh-and-retry for IMAGE requests.
  //
  // paxsFetch() above is a module-private helper with only three call sites
  // (the report modal and /auth/me). Cornerstone reaches the network through
  // dicomweb-client, which uses its OWN fetch/XHR — so every WADO-RS
  // /metadata, /frames/{n} and /rendered request bypassed the refresh path
  // entirely. A single transient 401 was therefore terminal for that frame:
  // the loader marked the image failed and the viewport stayed black with no
  // in-page recovery.
  //
  // We install the same single-flight refresh at the transport layer, scoped to
  // the endpoints the viewer actually loads images from. paxsRefreshToken() is
  // single-flight, so a burst of concurrent 401s triggers ONE /auth/refresh and
  // then one replay each — no thundering herd against the auth endpoint.
  // -------------------------------------------------------------------------
  var PAXS_AUTH_RETRY_PATHS = ['/dicom-web/', '/api/v1/volume/', '/api/v1/reports/'];

  function paxsNeedsAuthRetry(url) {
    var u = String(url == null ? '' : url);
    for (var i = 0; i < PAXS_AUTH_RETRY_PATHS.length; i++) {
      if (u.indexOf(PAXS_AUTH_RETRY_PATHS[i]) !== -1) return true;
    }
    return false;
  }

  function paxsInstallFetchGuard() {
    if (!window.fetch || window.__paxsFetchGuard) return;
    window.__paxsFetchGuard = true;
    var origFetch = window.fetch;
    window.fetch = function (input, init) {
      var url = (typeof input === 'string') ? input
              : (input && input.url) ? input.url : '';
      var p = origFetch.apply(this, arguments);
      if (!paxsNeedsAuthRetry(url)) return p;
      return p.then(function (res) {
        if (!res || res.status !== 401) return res;
        return paxsRefreshToken().then(function (tok) {
          if (!tok) return res;            // refresh failed: surface the 401
          // Replay the ORIGINAL request exactly once, with the fresh token.
          var retryInit = {};
          for (var k in (init || {})) {
            if (Object.prototype.hasOwnProperty.call(init, k)) retryInit[k] = init[k];
          }
          retryInit.credentials = 'same-origin';
          retryInit.headers = {};
          var base = (init && init.headers) || {};
          for (var h in base) {
            if (Object.prototype.hasOwnProperty.call(base, h)) retryInit.headers[h] = base[h];
          }
          retryInit.headers['Authorization'] = 'Bearer ' + tok;
          return origFetch.call(window, input, retryInit);
        });
      });
    };
  }

  function paxsInstallXhrGuard() {
    if (!window.XMLHttpRequest || window.__paxsXhrGuard) return;
    window.__paxsXhrGuard = true;
    var XHR = window.XMLHttpRequest;
    var origOpen = XHR.prototype.open;
    var origSend = XHR.prototype.send;
    var origSetHeader = XHR.prototype.setRequestHeader;
    XHR.prototype.open = function (method, url) {
      // Record the target so send() can decide whether this is an image read.
      this.__paxsUrl = url;
      this.__paxsRetried = false;
      return origOpen.apply(this, arguments);
    };
    XHR.prototype.send = function () {
      var xhr = this;
      var args = arguments;
      if (!paxsNeedsAuthRetry(xhr.__paxsUrl)) return origSend.apply(xhr, args);
      xhr.addEventListener('load', function () {
        if (xhr.status !== 401 || xhr.__paxsRetried) return;
        xhr.__paxsRetried = true;          // exactly one replay, ever
        paxsRefreshToken().then(function (tok) {
          if (!tok) return;
          try {
            origSetHeader.call(xhr, 'Authorization', 'Bearer ' + tok);
          } catch (e) { /* headers already flushed — nothing we can do */ }
          origSend.apply(xhr, args);
        });
      });
      return origSend.apply(xhr, args);
    };
  }

  function paxsInstallAuthGuards() {
    paxsInstallFetchGuard();
    paxsInstallXhrGuard();
  }

  function paxsSilentRefresh() {
    // Page-load optimization: rotate the token once so the first authenticated
    // call starts valid. Refresh-on-401 (paxsFetch) is the real safety net.
    if (paxsRefreshDone) return;
    paxsRefreshDone = true;
    paxsRefreshToken();
  }

  function paxsAuthHeaders() {
    var h = { 'Content-Type': 'application/json' };
    try {
      var t = localStorage.getItem('paxs_token');
      if (t) h['Authorization'] = 'Bearer ' + t;
    } catch (e) {}
    return h;
  }

  function fetchPaxsUser() {
    if (paxsUserFetched) return;
    paxsUserFetched = true;
    paxsFetch('/api/v1/auth/me', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (u) { paxsUser = u; })
      .catch(function () { paxsUser = null; });
  }

  function injectUserChip() {
    if (!paxsUser) return;
    if (document.querySelector('[data-paxs-react-user-chip]')) return; // handled natively by React
    // Study-list app bar (studies-ui SPA) — the upgraded shadcn header. In
    // the deep-linked OHIF viewer the page has no such bar, so fall back to
    // any top-level header element (the PaxsViewer toolbar area).
    var header = document.querySelector('header.h-14, header[class*="bg-slate-900"]')
               || document.querySelector('header');
    if (!header) return;
    if (header.querySelector('.paxs-user-chip')) return; // already injected

    var chip = document.createElement('div');
    chip.className = 'paxs-user-chip';
    var name = paxsUser.full_name || paxsUser.username;
    var color = paxsAvatarColor(paxsUser.username);
    chip.innerHTML =
      '<span class="paxs-user-avatar" style="background:' + color + '">' +
        paxsEsc((paxsUser.username || '?').charAt(0).toUpperCase()) +
      '</span>' +
      '<span class="paxs-user-id">' +
        '<span class="paxs-user-name">' + paxsEsc(name) + '</span>' +
        '<span class="paxs-user-role">' + paxsEsc(paxsUser.role || '') + '</span>' +
      '</span>' +
      '<button type="button" class="paxs-user-signout" title="Sign out">Sign out</button>';

    // Study list: land the chip inside the header's right-side action
    // cluster (next to "Launch 3D PACS Viewer") so it survives React
    // re-renders of the row area. OHIF viewer: fall back to the header's
    // far right.
    var cluster = null;
    var btns = header.querySelectorAll('header button, header a, nav button');
    for (var i = 0; i < btns.length; i++) {
      var txt = (btns[i].textContent || '').replace(/\s+/g, ' ').trim();
      if (txt === 'Launch 3D PACS Viewer' || txt === 'Upload DICOM' || txt === 'Nodes' || txt === 'Schedule') {
        cluster = btns[i].parentElement;
        break;
      }
    }
    if (cluster) cluster.appendChild(chip);
    else header.appendChild(chip);

    var signout = chip.querySelector('.paxs-user-signout');
    signout.addEventListener('click', function () {
      signout.disabled = true;
      signout.textContent = 'Signing out…';
      fetch('/api/v1/auth/logout', { method: 'POST', credentials: 'same-origin' })
        .catch(function () {})
        .then(function () {
          try { localStorage.removeItem('paxs_token'); } catch (e) {}
          window.location.href = '/login';
        });
    });
  }

  // V1-1: install the transport guard BEFORE anything can fetch an image, so a
  // 401 on a WADO-RS frame triggers a refresh + replay instead of a black viewport.
  paxsInstallAuthGuards();
  paxsSilentRefresh();   // fire once on load — fresh token before any API call
  fetchPaxsUser();
  setTimeout(injectUserChip, 600);
  // Keep the chip attached across React re-renders of the header
  setInterval(function () { injectUserChip(); }, 2000);

  // Expose for diagnostics / re-installation if another script swaps window.fetch.
  window.__paxsInstallAuthGuards = paxsInstallAuthGuards;

})();


