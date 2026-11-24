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
  // Re-entry guard FIRST: everything below (including the dialog observer)
  // used to install a second time when this script was loaded twice, which
  // doubled every MutationObserver callback.
  if (window.__paxsStudyEnhanceLoaded) return;
  window.__paxsStudyEnhanceLoaded = true;

  // -------------------------------------------------------------------------
  // Viewer scope gate — CRITICAL
  //
  // This file is loaded by BOTH the viewer and the Study List. Every popup,
  // toast and observer added below must be viewer-only: the Study List keeps
  // its native dialogs and window.alert() behaviour untouched.
  //
  // The dialog engine (paxs-dialog-engine.js) applies the same predicate and
  // returns before installing anything outside the viewer, so `PaxsDialog`
  // simply does not exist on the study table.
  // -------------------------------------------------------------------------
  function isPaxsViewerActive() {
    var search = window.location.search || '';
    var path = window.location.pathname || '';
    return search.indexOf('StudyInstanceUIDs') !== -1 ||
      search.indexOf('study') !== -1 ||
      path.indexOf('/viewer/viewer') !== -1;
  }

  /** The viewer's dialog engine, or null on the Study List. */
  function paxsDialogs() {
    return isPaxsViewerActive() && window.PaxsDialog ? window.PaxsDialog : null;
  }

  /** The viewer's toast stack, or null on the Study List. */
  function paxsToasts() {
    return isPaxsViewerActive() && window.PaxsToast ? window.PaxsToast : null;
  }

  /**
   * Report an outcome to the user.
   *
   * In the viewer this is a non-blocking toast (an alert() steals focus from
   * the study and freezes the render loop). On the Study List — where the
   * dialog engine is deliberately absent — the old alert() stays, because that
   * page is out of scope for this redesign.
   */
  function notify(variant, title, message) {
    var toasts = paxsToasts();
    if (toasts) {
      toasts.show({ variant: variant, title: title, message: message });
      return;
    }
    alert(message ? title + '\n\n' + message : title);
  }

  // -------------------------------------------------------------------------
  // Terms & Conditions — a real dialog in the viewer, alert() elsewhere
  // -------------------------------------------------------------------------
  function termsBodyHtml() {
    return '' +
      '<ol class="paxs-terms">' +
        '<li class="paxs-term">' +
          '<span class="paxs-term-n">1</span>' +
          '<div><div class="paxs-term-t">Diagnostic disclaimer</div>' +
          '<div class="paxs-term-d">This system is engineered for DICOM medical image ' +
          'archiving and web visualisation. Findings produced here are not a ' +
          'diagnosis on their own; the signed report is.</div></div>' +
        '</li>' +
        '<li class="paxs-term">' +
          '<span class="paxs-term-n">2</span>' +
          '<div><div class="paxs-term-t">Data integrity</div>' +
          '<div class="paxs-term-d">Every imaging and reporting operation is written to ' +
          'a tamper-evident audit trail with the acting user, the study and a ' +
          'content hash.</div></div>' +
        '</li>' +
        '<li class="paxs-term">' +
          '<span class="paxs-term-n">3</span>' +
          '<div><div class="paxs-term-t">Privacy &amp; PHI</div>' +
          '<div class="paxs-term-d">Data is protected in transit by TLS and at rest by ' +
          'AES encryption. Access is confined to your institution\'s studies.</div></div>' +
        '</li>' +
      '</ol>' +
      '<div class="paxs-terms-foot">Developed by Mohamed O. Elsamani — ' +
        '<span class="paxs-mono">elsamani.gt.tc</span></div>';
  }

  function openTermsDialog() {
    var dialogs = paxsDialogs();
    if (!dialogs) {
      // Study List: out of scope, keep the native alert.
      alert('PAXS Archive Terms & Conditions:\n\n1. Diagnostic Disclaimer: This system is engineered for DICOM medical image archiving and web visualization.\n2. Data Integrity: All imaging operations are logged into a tamper-evident audit trail.\n3. Privacy & PHI: Data is protected via AES-TLS encryption.\n\nDeveloped by Mohamed O. Elsamani (elsamani.gt.tc)');
      return;
    }

    var footer = dialogs.el('div', 'paxs-dlg-actions');
    var ok = dialogs.el('button', 'paxs-btn paxs-btn-primary', 'Understood');
    ok.type = 'button';
    ok.setAttribute('data-paxs-autofocus', '');
    footer.appendChild(ok);

    var dlg = dialogs.open({
      name: 'terms',
      title: 'PAXS Archive — Terms & Conditions',
      description: 'Conditions of use for diagnostic imaging on this workstation.',
      icon: dialogs.icons.shield,
      tone: 'info',
      size: 'md',
      scene: 'volume',
      body: termsBodyHtml(),
      footer: footer
    });
    ok.addEventListener('click', function () { dlg.close('accept'); });
  }

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
  // Automatically injects hidden title and description nodes into dialogs
  // lacking them. This keeps Radix DialogContent valid for legacy controls
  // that only provide a visible heading through a service wrapper.
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
        if (!d.querySelector('[data-radix-dialog-description], [id*="radix-"][id*="description"]')) {
          var hiddenDescription = document.createElement('p');
          var descriptionId = 'paxs-dialog-description-' + i;
          hiddenDescription.id = descriptionId;
          hiddenDescription.textContent = 'PAXS dialog';
          hiddenDescription.setAttribute('data-radix-dialog-description', '1');
          hiddenDescription.style.cssText = 'position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border-width:0;';
          d.appendChild(hiddenDescription);
          d.setAttribute('aria-describedby', descriptionId);
        }
      }
    }
    var mo = new MutationObserver(fixDialogs);
    mo.observe(document.body, { childList: true, subtree: true });
    fixDialogs();
  })();

  var MOD_COLORS = {
    CT: '#38bdf8', MR: '#a78bfa', DX: '#fb923c', CR: '#fb923c',
    US: '#34d399', PT: '#f472b6', NM: '#f472b6', XA: '#fbbf24',
    RF: '#fbbf24', MG: '#f87171', OT: '#2dd4bf'
  };

  // Thumbnail transport, the bounded blob LRU and the 4-wide request queue
  // live in paxs-thumb-engine.js (loaded first). This file owns the study
  // rows, the sidebar and the viewer chrome.
  var PaxsThumb = window.PaxsThumb;

  var studies = [];               // QIDO /studies response (row order = render order)
  var studyPairs = [];            // [key, study] built once, so matching rebuilds nothing
  var studiesRequested = false;

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

  // `textContent`, not `innerText`: innerText flushes layout, and this ran for
  // every row on every mutation pass.
  function rowKey(row) {
    return (row.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80);
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

  // SB-4 — one QIDO call, indexed once.
  //
  // matchStudy() used to rebuild a "NAME DESCRIPTION" key for all 200 studies
  // for every row, on every mutation pass, and rowKey() read `innerText` (which
  // forces layout). Both are now a single normalised key per study and a
  // `textContent` read per row.
  function rebuildStudyIndex() {
    studyPairs = [];
    for (var i = 0; i < studies.length; i++) {
      var key = studyKey(studies[i]);
      if (!key) continue;
      studyPairs.push([key, studies[i]]);
    }
  }

  function loadStudies() {
    if (studiesRequested || !PaxsThumb) return;
    studiesRequested = true;
    // paxsFetch carries the bearer token and refreshes once on a 401; the old
    // bare fetch() returned [] to an expired session and the tiles vanished.
    paxsFetch('/dicom-web/studies?limit=200&includefield=0020000D,00080061,00100010,00081030')
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (arr) {
        studies = Array.isArray(arr) ? arr : [];
        rebuildStudyIndex();
        scheduleEnhance();       // paint the tiles now that we know the UIDs
      })
      .catch(function () { studiesRequested = false; });   // allow one retry
  }

  function matchStudy(row, i) {
    if (!studies.length) return null;
    var rk = rowKey(row);
    if (rk) {
      for (var j = 0; j < studyPairs.length; j++) {
        if (rk.indexOf(studyPairs[j][0]) !== -1) return studyPairs[j][1];
      }
    }
    return i < studies.length ? studies[i] : null;
  }

  // SB-2 — mount one tile per row and let the engine decide when to fetch.
  //
  // The old version called thumbFor(uid) for every row on every pass, which
  // fired three requests per study (series, instances, thumbnail) for all 200
  // rows at once. Now the tile is created synchronously and the engine's
  // IntersectionObserver + 4-wide queue schedule the network work.
  function enhanceRow(row, i) {
    if (!PaxsThumb) return;

    // Cheapest possible guard first: a row that already carries a mounted tile
    // needs no study match at all. The old order ran the 200x200 name match
    // (and an innerText layout read) for every row on every mutation pass.
    var cell = row.cells && row.cells[0];
    if (!cell) return;
    var existing = cell.querySelector(':scope > .paxs-thumb');
    if (existing && existing.getAttribute('data-uid')) return;

    var study = matchStudy(row, i);
    if (!study) return;

    var uid = pick(study, '0020000D');
    if (!uid) return;
    row.setAttribute('data-paxs-study', uid);

    var mod = firstModality(study);
    PaxsThumb.mount(cell, {
      uid: uid,
      modality: mod || 'IMG',
      initials: patientInitials(study),
      color: MOD_COLORS[mod] || '#2dd4bf'
    });

    var nameBlock = cell.querySelector(':scope > .truncate');
    if (nameBlock && !nameBlock.getAttribute('title')) {
      nameBlock.setAttribute('title', (nameBlock.textContent || '').trim());
    }
  }

  function enhanceStudyList() {
    var sl = document.querySelector('.paxs-sl');
    if (!sl) return false;
    var rows = sl.querySelectorAll('tbody tr[class*="cursor-pointer"]');
    if (!rows.length) return false;
    loadStudies();
    for (var i = 0; i < rows.length; i++) enhanceRow(rows[i], i);
    return true;
  }

  // -------------------------------------------------------------------------
  // Replace and Style Top-Left Logo Image in PAXS Viewer Header
  // -------------------------------------------------------------------------
  function updateViewerLogo() {
    var imgs = document.querySelectorAll('header img, div[class*="header"] img, nav img');
    for (var i = 0; i < imgs.length; i++) {
      var img = imgs[i];
      if (img.hasAttribute('data-paxs-theme-logo')) continue;
      if (img.src.indexOf('logo') !== -1 || img.alt.toLowerCase().indexOf('logo') !== -1 || img.closest('header')) {
        if (img.src.indexOf('/static/logo/light-viewer.png') === -1) {
          img.src = '/static/logo/light-viewer.png';
        }
        img.style.height = '32px';
        img.style.width = 'auto';
        img.style.objectFit = 'contain';
      }
    }
  }

  setTimeout(updateViewerLogo, 300);

  // -------------------------------------------------------------------------
  // Intercept Top-Left Logo Button in Paxs Viewer
  // Open the archive study list instead of switching into PAXs's internal worklist route.
  // -------------------------------------------------------------------------
  function archiveStudyListUrl() {
    var prefix = window.PAXS_BASE_PATH;
    if (typeof prefix !== 'string') {
      var match = String(window.PUBLIC_URL || '').match(/^(.*)\/viewer\/?$/);
      prefix = match ? match[1] : '';
    }
    return prefix.replace(/\/+$/, '') + '/studies-ui';
  }

  function handleStudyListReturn(e) {
    var target = e.target;
    if (!target) return;
    // Match specific top-left header logo image or brand links
    var logoElem = target.closest(
      'header a[href="/"], header a[href="/viewer"], header a[href="/viewer/"], ' +
      'img[src*="PaxsViewer-logo"], img[src*="logomain"], ' +
      'button[data-cy="header-study-list-button"], [data-cy="return-to-work-list"]'
    );
    if (logoElem) {
      var isViewingStudy = window.location.search.indexOf('StudyInstanceUIDs') !== -1 ||
                           window.location.search.indexOf('study') !== -1;
      if (isViewingStudy) {
        e.preventDefault();
        e.stopPropagation();
        window.location.href = archiveStudyListUrl();
        return false;
      }
    }
  }
  document.addEventListener('click', handleStudyListReturn, true);

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
        '<a class="foot-link" href="https://elsamani.gt.tc" target="_blank" rel="noopener">' +
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14 21 3"/></svg>' +
          'elsamani.gt.tc' +
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
        openTermsDialog();
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

  // Colorize toolbar icons on load and keep active
  setTimeout(colorizeToolbarIcons, 500);

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
    // The viewer rail has a dedicated enhancer with the complete semantic
    // category map. Do not let this study-list compatibility pass overwrite
    // its classes after the OHIF toolbar has been tagged.
    if (btn.closest('.paxs-toolbar')) return;
    // Never restyle modal or toast chrome as a tool. This sweep matches
    // `header button[aria-label]`, and the dialog engine's close button
    // (aria-label="Close dialog") plus the toast dismiss button both match —
    // tagging them applied tool padding/colour/glow to our own dialogs. It also
    // caught Shepherd's tour buttons, which live inside a <dialog>.
    if (btn.closest('dialog, .paxs-toast-region, .paxs-dlg-root')) return;
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
  // Report Editor — Upgraded Clinical Rich-Text Popup (Study List & Viewer)
  // -------------------------------------------------------------------------
  function paxsReadCsrfToken() {
    try {
      var parts = document.cookie.split(';');
      for (var i = 0; i < parts.length; i++) {
        var part = parts[i].trim();
        if (part.indexOf('paxs_csrf=') === 0) return part.slice('paxs_csrf='.length);
      }
    } catch (e) { /* noop */ }
    return '';
  }

  function paxsEnsureRichAssets() {
    if (!document.querySelector('link[href*="/static/paxs-rich-editor.css"]')) {
      var link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = '/static/paxs-rich-editor.css?v=1';
      document.head.appendChild(link);
    }
  }

  function openLegacyReportModal(studyUid) {
    var match = (studyUid ? studyUid : '') ||
                (window.location.search.match(/StudyInstanceUIDs=([^&]+)/) ||
                 window.location.search.match(/study=([^&]+)/) || [])[1];
    if (!match) {
      notify('warning', 'No study selected', 'Please open or select a study to write or edit a report.');
      return;
    }
    var uid = decodeURIComponent(match);
    paxsEnsureRichAssets();

    var modalId = 'paxs-report-modal-wrap';
    var existing = document.getElementById(modalId);
    if (existing) {
      existing.dispatchEvent(new Event('paxs-report-close'));
      existing.remove();
    }

    var RTE = window.PaxsRichEditor;
    var wrap = document.createElement('div');
    wrap.id = modalId;
    wrap.className = 'paxs-rpt-backdrop';
    wrap.setAttribute('role', 'dialog');
    wrap.setAttribute('aria-modal', 'true');
    wrap.setAttribute('aria-labelledby', 'paxs-rpt-dialog-title');

    wrap.innerHTML =
      '<div class="paxs-rpt-modal">' +
        '<div class="paxs-rpt-header">' +
          '<div class="paxs-rpt-title-wrap">' +
            '<span class="paxs-rpt-icon" aria-hidden="true">' +
              '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/></svg>' +
            '</span>' +
            '<div>' +
              '<div class="paxs-rpt-heading">' +
                '<h3 id="paxs-rpt-dialog-title">Diagnostic Report Editor</h3>' +
                '<span class="paxs-rpt-status-pill" id="paxs-rpt-status-pill" data-status="NEW">Loading</span>' +
              '</div>' +
              '<div class="paxs-rpt-sub" id="paxs-rpt-sub">Clinical radiology reporting &amp; dictation workspace</div>' +
            '</div>' +
          '</div>' +
          '<button type="button" class="paxs-rpt-close" id="paxs-rpt-close" aria-label="Close report editor" title="Close (Esc)">' +
            '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
          '</button>' +
        '</div>' +

        '<div class="paxs-rpt-scroll">' +
          '<div class="paxs-rpt-loading-box" id="paxs-rpt-loading">' +
            '<span class="paxs-rpt-spin" aria-hidden="true"></span>' +
            '<span>Loading study DICOM metadata &amp; report…</span>' +
          '</div>' +

          '<div id="paxs-rpt-form-body" style="display:none;flex-direction:column;gap:14px;">' +
            '<div class="paxs-rpt-meta">' +
              '<div class="paxs-rpt-meta-item"><span class="paxs-rpt-meta-k">Patient</span><span class="paxs-rpt-meta-v" id="paxs-meta-patient">—</span></div>' +
              '<div class="paxs-rpt-meta-item"><span class="paxs-rpt-meta-k">Patient ID</span><span class="paxs-rpt-meta-v"><code id="paxs-meta-pid">—</code></span></div>' +
              '<div class="paxs-rpt-meta-item"><span class="paxs-rpt-meta-k">Age / Sex</span><span class="paxs-rpt-meta-v" id="paxs-meta-demo">—</span></div>' +
              '<div class="paxs-rpt-meta-item"><span class="paxs-rpt-meta-k">Modality</span><span class="paxs-rpt-meta-v"><span class="paxs-rpt-mod-badge" id="paxs-meta-mod">—</span></span></div>' +
              '<div class="paxs-rpt-meta-item"><span class="paxs-rpt-meta-k">Accession #</span><span class="paxs-rpt-meta-v" id="paxs-meta-acc">—</span></div>' +
              '<div class="paxs-rpt-meta-item"><span class="paxs-rpt-meta-k">Study Date</span><span class="paxs-rpt-meta-v" id="paxs-meta-date">—</span></div>' +
              '<div class="paxs-rpt-meta-item is-wide"><span class="paxs-rpt-meta-k">Study Description</span><span class="paxs-rpt-meta-v" id="paxs-meta-desc">—</span></div>' +
            '</div>' +

            '<div id="paxs-rpt-dictation-slot" hidden></div>' +

            '<div class="paxs-rpt-signed-banner" id="paxs-rpt-signed-banner" hidden>' +
              '<span id="paxs-rpt-signed-label">Signed &amp; Finalized — report body is locked</span>' +
              '<span style="font-family:var(--paxs-re-font-mono);font-size:11px;opacity:0.85;">IMMUTABLE RECORD</span>' +
            '</div>' +

            '<div class="paxs-rpt-tmpl-bar" id="paxs-rpt-tmpl-bar">' +
              '<label class="paxs-rpt-tmpl-label" for="paxs-rpt-tmpl-select">Template</label>' +
              '<select class="paxs-rpt-tmpl-select" id="paxs-rpt-tmpl-select">' +
                '<option value="">— Select a structured report template —</option>' +
              '</select>' +
              '<button type="button" class="paxs-rpt-tmpl-apply" id="paxs-rpt-tmpl-apply" disabled>Apply template</button>' +
            '</div>' +

            '<div class="paxs-rpt-section">' +
              '<div class="paxs-rpt-section-head">' +
                '<span class="paxs-rpt-section-title">Diagnostic Findings</span>' +
              '</div>' +
              '<div id="paxs-rpt-findings-host"></div>' +
            '</div>' +

            '<div class="paxs-rpt-section is-impression">' +
              '<div class="paxs-rpt-section-head">' +
                '<span class="paxs-rpt-section-title">Impression &amp; Conclusion</span>' +
              '</div>' +
              '<div id="paxs-rpt-impression-host"></div>' +
            '</div>' +

            '<div class="paxs-rpt-addenda-wrap" id="paxs-rpt-addenda" hidden>' +
              '<div id="paxs-rpt-chain"></div>' +
              '<div id="paxs-rpt-addendum-form"></div>' +
            '</div>' +

            '<div class="paxs-rpt-notice" id="paxs-rpt-notice" role="status" hidden></div>' +
            '<div class="paxs-rpt-notice is-warn" id="paxs-rpt-confirm-bar" role="alert" hidden>' +
              '<span>Finalize &amp; sign this report? Once signed it becomes read-only and is attributed to your account.</span>' +
              '<div class="paxs-rpt-confirm-actions">' +
                '<button type="button" class="paxs-rpt-btn" id="paxs-rpt-confirm-cancel">Keep editing</button>' +
                '<button type="button" class="paxs-rpt-btn paxs-rpt-btn-warn" id="paxs-rpt-confirm-ok">Confirm &amp; Sign</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<div class="paxs-rpt-footer">' +
          '<div class="paxs-rpt-footer-left">' +
            '<button type="button" class="paxs-rpt-btn paxs-rpt-btn-print" id="paxs-rpt-print-btn">' +
              '<svg viewBox="0 0 24 24"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>' +
              'Print' +
            '</button>' +
            '<button type="button" class="paxs-rpt-btn paxs-rpt-btn-print" id="paxs-rpt-pdf-btn">' +
              '<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>' +
              'Export PDF' +
            '</button>' +
            '<span class="paxs-rpt-saved-stamp" id="paxs-rpt-saved-stamp">Ctrl+S to save draft</span>' +
          '</div>' +
          '<div class="paxs-rpt-footer-right">' +
            '<button type="button" class="paxs-rpt-btn" id="paxs-rpt-draft-btn">' +
              '<svg viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>' +
              'Save Draft' +
            '</button>' +
            '<button type="button" class="paxs-rpt-btn paxs-rpt-btn-primary" id="paxs-rpt-final-btn">' +
              '<svg viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>' +
              'Finalize &amp; Sign' +
            '</button>' +
          '</div>' +
        '</div>' +
      '</div>';

    document.body.appendChild(wrap);

    var findingsHost = wrap.querySelector('#paxs-rpt-findings-host');
    var impressionHost = wrap.querySelector('#paxs-rpt-impression-host');
    var findingsEd = RTE ? RTE.create(findingsHost, {
      ariaLabel: 'Diagnostic findings editor',
      placeholder: 'Describe imaging technique, anatomical observations, and diagnostic findings…',
      minHeight: 190,
      maxHeight: 380
    }) : null;
    var impressionEd = RTE ? RTE.create(impressionHost, {
      ariaLabel: 'Impression and conclusion editor',
      placeholder: 'Summarize primary clinical impression and recommendations…',
      minHeight: 115,
      maxHeight: 240
    }) : null;

    if (!findingsEd) {
      findingsHost.innerHTML = '<textarea id="paxs-rpt-findings" style="width:100%;min-height:170px;padding:12px;border-radius:10px;border:1px solid var(--paxs-re-border-strong);background:var(--paxs-re-surface);color:var(--paxs-re-text);"></textarea>';
      impressionHost.innerHTML = '<textarea id="paxs-rpt-impression" style="width:100%;min-height:105px;padding:12px;border-radius:10px;border:1px solid var(--paxs-re-border-strong);background:var(--paxs-re-surface);color:var(--paxs-re-text);"></textarea>';
    }

    function getFindingsHtml() {
      return findingsEd ? findingsEd.getHtml() : (wrap.querySelector('#paxs-rpt-findings').value || '');
    }
    function getFindingsPlain() {
      return findingsEd ? findingsEd.getPlainText() : (wrap.querySelector('#paxs-rpt-findings').value || '').trim();
    }
    function getImpressionHtml() {
      return impressionEd ? impressionEd.getHtml() : (wrap.querySelector('#paxs-rpt-impression').value || '');
    }
    function setFindingsValue(val) {
      if (findingsEd) findingsEd.setValue(val || '');
      else wrap.querySelector('#paxs-rpt-findings').value = val || '';
    }
    function setImpressionValue(val) {
      if (impressionEd) impressionEd.setValue(val || '');
      else wrap.querySelector('#paxs-rpt-impression').value = val || '';
    }
    function setEditorsReadOnly(ro) {
      if (findingsEd) findingsEd.setReadOnly(ro);
      else wrap.querySelector('#paxs-rpt-findings').readOnly = ro;
      if (impressionEd) impressionEd.setReadOnly(ro);
      else wrap.querySelector('#paxs-rpt-impression').readOnly = ro;
    }

    var noticeEl = wrap.querySelector('#paxs-rpt-notice');
    var confirmBar = wrap.querySelector('#paxs-rpt-confirm-bar');
    var statusPill = wrap.querySelector('#paxs-rpt-status-pill');
    var savedStamp = wrap.querySelector('#paxs-rpt-saved-stamp');
    var draftBtn = wrap.querySelector('#paxs-rpt-draft-btn');
    var finalBtn = wrap.querySelector('#paxs-rpt-final-btn');
    var printBtn = wrap.querySelector('#paxs-rpt-print-btn');
    var pdfBtn = wrap.querySelector('#paxs-rpt-pdf-btn');
    var tmplSelect = wrap.querySelector('#paxs-rpt-tmpl-select');
    var tmplApply = wrap.querySelector('#paxs-rpt-tmpl-apply');
    var tmplBar = wrap.querySelector('#paxs-rpt-tmpl-bar');
    var signedBanner = wrap.querySelector('#paxs-rpt-signed-banner');
    var addendaWrap = wrap.querySelector('#paxs-rpt-addenda');

    function showNotice(kind, text) {
      noticeEl.className = 'paxs-rpt-notice is-' + kind;
      noticeEl.textContent = text;
      noticeEl.hidden = false;
    }
    function clearNotice() {
      noticeEl.hidden = true;
      noticeEl.textContent = '';
      if (findingsEd) findingsEd.setInvalid(false);
    }

    var dictationSlot = wrap.querySelector('#paxs-rpt-dictation-slot');
    var stopDictation = mountDictationPlayer(dictationSlot, uid, true, { enableShortcuts: true });

    var currentMeta = null;
    var isFinalized = false;
    var busy = false;

    function closeModal() {
      wrap.dispatchEvent(new Event('paxs-report-close'));
      wrap.remove();
    }

    function onModalKeydown(ev) {
      if (ev.key === 'Escape') {
        if (!confirmBar.hidden) { confirmBar.hidden = true; return; }
        closeModal();
      } else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') {
        ev.preventDefault();
        if (!isFinalized && !busy) saveRpt('DRAFT');
      }
    }
    window.addEventListener('keydown', onModalKeydown);

    wrap.addEventListener('paxs-report-close', function () {
      window.removeEventListener('keydown', onModalKeydown);
      if (stopDictation) stopDictation();
      if (findingsEd) findingsEd.destroy();
      if (impressionEd) impressionEd.destroy();
    }, { once: true });

    wrap.addEventListener('click', function (ev) {
      if (ev.target === wrap) closeModal();
    });
    wrap.querySelector('#paxs-rpt-close').onclick = closeModal;

    printBtn.onclick = function () {
      window.open('/api/v1/reports/print/' + encodeURIComponent(uid), '_blank');
    };

    pdfBtn.onclick = function () {
      var prevText = pdfBtn.innerHTML;
      pdfBtn.disabled = true;
      pdfBtn.textContent = 'Exporting…';
      var headers = {};
      try {
        var tok = localStorage.getItem('paxs_token');
        if (tok) headers['Authorization'] = 'Bearer ' + tok;
      } catch (e) { /* noop */ }
      paxsFetch('/api/v1/reports/pdf/' + encodeURIComponent(uid), {
        credentials: 'same-origin',
        headers: headers
      })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          var disp = res.headers.get('Content-Disposition') || '';
          var m = disp.match(/filename="([^"]+)"/i);
          var fname = m ? m[1] : ('PAXS_Report_' + uid.slice(0, 16) + '.pdf');
          return res.blob().then(function (blob) { return { blob: blob, fname: fname }; });
        })
        .then(function (item) {
          var url = URL.createObjectURL(item.blob);
          var a = document.createElement('a');
          a.href = url;
          a.download = item.fname;
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
          showNotice('ok', 'PDF exported: ' + item.fname);
        })
        .catch(function (err) {
          showNotice('error', 'Could not export PDF: ' + err.message);
        })
        .then(function () {
          pdfBtn.disabled = false;
          pdfBtn.innerHTML = prevText;
        });
    };

    function applySignedState(report) {
      isFinalized = true;
      setEditorsReadOnly(true);
      statusPill.textContent = 'FINALIZED';
      statusPill.setAttribute('data-status', 'FINALIZED');
      draftBtn.hidden = true;
      finalBtn.hidden = true;
      tmplBar.hidden = true;
      confirmBar.hidden = true;
      signedBanner.hidden = false;
      var signedLabel = wrap.querySelector('#paxs-rpt-signed-label');
      var when = report.finalized_at ? String(report.finalized_at).replace('T', ' ').replace(/\.\d+/, '') : '';
      signedLabel.textContent = 'Signed by ' + (report.radiologist || 'Authorized Radiologist') + (when ? ' · ' + when : '');
      savedStamp.textContent = 'Finalized report · read-only';

      addendaWrap.hidden = false;
      refreshAddendumChain(report.addenda);
      var api = window.PaxsReportAddenda;
      if (api && report.id && api.canAuthor(paxsUser && paxsUser.role)) {
        mountAddendumForm(report, uid);
      }
    }

    function saveRpt(status) {
      if (busy) return;
      clearNotice();
      var findings = getFindingsHtml();
      var impression = getImpressionHtml();

      if (status === 'FINALIZED' && !getFindingsPlain()) {
        if (findingsEd) { findingsEd.setInvalid(true); findingsEd.focus(); }
        showNotice('error', 'Findings cannot be empty before finalizing and signing.');
        return;
      }

      busy = true;
      draftBtn.disabled = true;
      finalBtn.disabled = true;
      savedStamp.textContent = status === 'FINALIZED' ? 'Signing report…' : 'Saving draft…';

      postReport(uid, status, findings, impression)
        .then(function (res) {
          busy = false;
          draftBtn.disabled = false;
          var canFin = !!paxsUser && (paxsUser.role === 'ADMIN' || paxsUser.role === 'RADIOLOGIST');
          finalBtn.disabled = !canFin;
          confirmBar.hidden = true;

          if (!res.ok) {
            var msg = (res.body && res.body.detail) ? res.body.detail : 'HTTP ' + res.code;
            showNotice('error', 'Could not save report: ' + msg);
            savedStamp.textContent = 'Save failed';
            return;
          }

          var nowStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
          if (status === 'FINALIZED') {
            showNotice('ok', 'Report finalized, signed, and locked.');
            fetchReportMeta(uid)
              .then(function (fresh) {
                if (fresh && fresh.report) applySignedState(fresh.report);
              })
              .catch(function () {
                applySignedState(res.body || { status: 'FINALIZED', radiologist: paxsUser && paxsUser.username });
              });
          } else {
            statusPill.textContent = 'DRAFT';
            statusPill.setAttribute('data-status', 'DRAFT');
            savedStamp.textContent = 'Draft saved at ' + nowStr;
            showNotice('ok', 'Draft saved successfully at ' + nowStr + '.');
          }
        })
        .catch(function (err) {
          busy = false;
          draftBtn.disabled = false;
          finalBtn.disabled = false;
          showNotice('error', 'Network error while saving report: ' + err);
          savedStamp.textContent = 'Save failed';
        });
    }

    paxsFetch('/api/v1/reports/study/' + encodeURIComponent(uid), { headers: paxsAuthHeaders() })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (meta) {
        currentMeta = meta;
        wrap.querySelector('#paxs-rpt-loading').style.display = 'none';
        wrap.querySelector('#paxs-rpt-form-body').style.display = 'flex';

        wrap.querySelector('#paxs-meta-patient').textContent = (meta.patient_name || 'UNKNOWN').replace(/\^+/g, ' ').trim();
        wrap.querySelector('#paxs-meta-pid').textContent = meta.patient_id || '--';
        wrap.querySelector('#paxs-meta-demo').textContent =
          [meta.patient_age || '--', meta.patient_sex || '--'].join(' / ');
        wrap.querySelector('#paxs-meta-mod').textContent = meta.modality || 'CT';
        wrap.querySelector('#paxs-meta-acc').textContent = meta.accession_number || '--';
        wrap.querySelector('#paxs-meta-date').textContent = meta.study_date || '--';
        wrap.querySelector('#paxs-meta-desc').textContent = meta.study_description || '--';

        if (meta.report) {
          setFindingsValue(meta.report.findings || '');
          setImpressionValue(meta.report.impression || '');
          var st = String(meta.report.status || 'DRAFT').toUpperCase();
          statusPill.textContent = st;
          statusPill.setAttribute('data-status', st);
          if (isSignedReport(meta.report)) {
            applySignedState(meta.report);
          }
        } else {
          statusPill.textContent = 'NEW';
          statusPill.setAttribute('data-status', 'NEW');
        }

        // Load templates for this modality
        var loadedTemplates = [];
        paxsFetch('/api/v1/templates?modality=' + encodeURIComponent(meta.modality || ''), { headers: paxsAuthHeaders() })
          .then(function (tr) { return tr.ok ? tr.json() : []; })
          .then(function (tmpls) {
            loadedTemplates = tmpls || [];
            for (var i = 0; i < loadedTemplates.length; i++) {
              var t = loadedTemplates[i];
              var opt = document.createElement('option');
              opt.value = String(i);
              opt.textContent = t.title + (t.modality ? ' [' + t.modality + ']' : '') + (t.is_default ? ' ★' : '');
              tmplSelect.appendChild(opt);
            }
          })
          .catch(function () { /* templates optional */ });

        tmplSelect.onchange = function () {
          tmplApply.disabled = tmplSelect.value === '';
        };

        tmplApply.onclick = function () {
          if (isFinalized) return;
          var idx = tmplSelect.value;
          if (idx === '' || !loadedTemplates[idx]) return;
          var t = loadedTemplates[idx];
          var fRaw = t.body_findings_template || '';
          var iRaw = t.default_impression_template || '';
          if (RTE && currentMeta) {
            fRaw = RTE.applyPlaceholders(fRaw, currentMeta);
            iRaw = RTE.applyPlaceholders(iRaw, currentMeta);
          }
          if (fRaw) setFindingsValue(fRaw);
          if (iRaw) setImpressionValue(iRaw);
          showNotice('ok', 'Template "' + t.title + '" applied.');
          if (findingsEd) findingsEd.focus();
        };

        draftBtn.onclick = function () { saveRpt('DRAFT'); };

        var paxsCanFinalize = !!paxsUser &&
          (paxsUser.role === 'ADMIN' || paxsUser.role === 'RADIOLOGIST');
        if (!paxsCanFinalize) {
          finalBtn.disabled = true;
          finalBtn.title = 'Finalizing a report requires a radiologist or admin role';
        }
        finalBtn.onclick = function () {
          if (!paxsCanFinalize || isFinalized) return;
          if (!getFindingsPlain()) {
            if (findingsEd) { findingsEd.setInvalid(true); findingsEd.focus(); }
            showNotice('error', 'Findings cannot be empty before finalizing and signing.');
            return;
          }
          clearNotice();
          confirmBar.hidden = false;
          confirmBar.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        };
        wrap.querySelector('#paxs-rpt-confirm-cancel').onclick = function () {
          confirmBar.hidden = true;
        };
        wrap.querySelector('#paxs-rpt-confirm-ok').onclick = function () {
          saveRpt('FINALIZED');
        };

        if (!isFinalized && findingsEd) {
          try { findingsEd.focus(); } catch (e) { /* noop */ }
        }
      })
      .catch(function (e) {
        var loading = wrap.querySelector('#paxs-rpt-loading');
        loading.innerHTML = '<span style="color:var(--paxs-re-danger);">Could not load study DICOM metadata: ' +
          (RTE ? RTE.escapeHtml(String(e)) : String(e)) + '</span>';
      });
  }

  // -------------------------------------------------------------------------
  // Report Editor — viewer implementation (dialog engine)
  //
  // Same endpoints, same permission model as the Study List version; the
  // difference is the shell: real header/body/footer regions, focus trap,
  // Escape, inline field errors instead of alert(), and a confirmation before
  // a signature that cannot be undone.
  // -------------------------------------------------------------------------
  function reportUidFrom(studyUid) {
    var match = (studyUid ? studyUid : '') ||
      (window.location.search.match(/StudyInstanceUIDs=([^&]+)/) ||
       window.location.search.match(/study=([^&]+)/) || [])[1];
    return match ? decodeURIComponent(match) : '';
  }

  function fetchReportMeta(uid) {
    return paxsFetch('/api/v1/reports/study/' + encodeURIComponent(uid), {
      headers: paxsAuthHeaders()
    }).then(function (r) { return r.json(); });
  }

  function postReport(uid, status, findings, impression) {
    return paxsFetch('/api/v1/reports/save', {
      method: 'POST',
      headers: paxsAuthHeaders(),
      body: JSON.stringify({
        study_instance_uid: uid,
        findings: findings,
        impression: impression,
        status: status
      })
    }).then(function (sr) {
      return sr.json().catch(function () { return {}; }).then(function (body) {
        return { ok: sr.ok, code: sr.status, body: body };
      });
    });
  }

  function reportFormHtml() {
    return '' +
      '<div class="paxs-meta-grid">' +
        '<div class="paxs-meta-cell"><span class="paxs-meta-k">Patient</span>' +
          '<span class="paxs-meta-v" id="paxs-meta-patient">—</span></div>' +
        '<div class="paxs-meta-cell"><span class="paxs-meta-k">Patient ID</span>' +
          '<span class="paxs-meta-v"><code id="paxs-meta-pid">—</code></span></div>' +
        '<div class="paxs-meta-cell"><span class="paxs-meta-k">Modality</span>' +
          '<span class="paxs-meta-v" id="paxs-meta-mod">—</span></div>' +
        '<div class="paxs-meta-cell"><span class="paxs-meta-k">Accession #</span>' +
          '<span class="paxs-meta-v" id="paxs-meta-acc">—</span></div>' +
        '<div class="paxs-meta-cell paxs-wide"><span class="paxs-meta-k">Description</span>' +
          '<span class="paxs-meta-v" id="paxs-meta-desc">—</span></div>' +
      '</div>' +
      '<div class="paxs-field">' +
        '<label class="paxs-field-label" for="paxs-rpt-tmpl-select">Standard template' +
          '<span class="paxs-field-hint">optional</span></label>' +
        '<select class="paxs-select" id="paxs-rpt-tmpl-select">' +
          '<option value="">— Choose a template —</option>' +
        '</select>' +
      '</div>' +
      '<div class="paxs-field">' +
        '<label class="paxs-field-label" for="paxs-rpt-findings">Findings' +
          '<span class="paxs-counter" id="paxs-rpt-findings-count">0</span></label>' +
        '<textarea class="paxs-textarea" id="paxs-rpt-findings" rows="6" ' +
          'placeholder="Describe the findings for this study…"></textarea>' +
      '</div>' +
      '<div class="paxs-field">' +
        '<label class="paxs-field-label" for="paxs-rpt-impression">Impression' +
          '<span class="paxs-counter" id="paxs-rpt-impression-count">0</span></label>' +
        '<textarea class="paxs-textarea" id="paxs-rpt-impression" rows="4" ' +
          'placeholder="Conclusion and recommendation…"></textarea>' +
      '</div>' +
      // C-19: a finalized report is immutable; its addendum chain and the
      // append control live below the editor fields and only appear once the
      // loaded report says it is signed.
      '<div class="paxs-addenda" id="paxs-rpt-addenda" hidden>' +
        '<div class="paxs-rpt-signed">' +
          '<div class="paxs-rpt-signed-k">Signed findings</div>' +
          '<p class="paxs-rpt-signed-v" id="paxs-rpt-signed-findings"></p>' +
          '<div class="paxs-rpt-signed-k">Signed impression</div>' +
          '<p class="paxs-rpt-signed-v" id="paxs-rpt-signed-impression"></p>' +
        '</div>' +
        '<div id="paxs-rpt-chain"></div>' +
        '<div id="paxs-rpt-addendum-form"></div>' +
      '</div>' +
      '<div class="paxs-field-error" id="paxs-rpt-error" role="alert" hidden></div>';
  }

  function openReportDialog(uid) {
    var D = paxsDialogs();
    if (!D) return;

    var body = D.el('div', 'paxs-report');
    body.innerHTML =
      '<div class="paxs-rpt-loading" id="paxs-rpt-loading">' +
        '<span class="paxs-spinner" aria-hidden="true"></span>' +
        '<span>Loading study metadata…</span>' +
      '</div>' +
      '<div id="paxs-rpt-form" hidden>' + reportFormHtml() + '</div>';

    var footer = D.el('div', 'paxs-dlg-actions');
    var printBtn = D.el('button', 'paxs-btn paxs-btn-quiet paxs-btn-start', 'Print report');
    printBtn.type = 'button';
    var draftBtn = D.el('button', 'paxs-btn paxs-btn-ghost', 'Save draft');
    draftBtn.type = 'button';
    var signBtn = D.el('button', 'paxs-btn paxs-btn-primary', 'Finalize & sign');
    signBtn.type = 'button';
    footer.appendChild(printBtn);
    footer.appendChild(draftBtn);
    footer.appendChild(signBtn);

    var dlg = D.open({
      name: 'report',
      title: 'Diagnostic Report',
      description: 'Findings and impression are stored against this study and audited.',
      icon: D.icons.report,
      tone: 'info',
      size: 'lg',
      scene: 'volume',
      body: body,
      footer: footer
    });

    var loading = body.querySelector('#paxs-rpt-loading');
    var form = body.querySelector('#paxs-rpt-form');
    var errBox = body.querySelector('#paxs-rpt-error');
    var findingsEl = body.querySelector('#paxs-rpt-findings');
    var impressionEl = body.querySelector('#paxs-rpt-impression');
    var findingsCount = body.querySelector('#paxs-rpt-findings-count');
    var impressionCount = body.querySelector('#paxs-rpt-impression-count');

    function showError(message) {
      errBox.textContent = message;
      errBox.hidden = false;
    }
    function clearError() {
      errBox.hidden = true;
      errBox.textContent = '';
      findingsEl.classList.remove('paxs-invalid');
    }

    function count(input, out) {
      var update = function () {
        out.textContent = input.value.length;
        out.setAttribute('data-over', input.value.length > 4000 ? '1' : '0');
      };
      input.addEventListener('input', update);
      update();
    }
    count(findingsEl, findingsCount);
    count(impressionEl, impressionCount);

    printBtn.addEventListener('click', function () {
      window.open('/api/v1/reports/print/' + encodeURIComponent(uid), '_blank');
    });

    function save(status) {
      var findings = findingsEl.value;
      var impression = impressionEl.value;

      if (status === 'FINALIZED' && !findings.trim()) {
        findingsEl.classList.add('paxs-invalid');
        findingsEl.focus();
        showError('Findings cannot be empty before signing.');
        return;
      }

      var run = function () {
        clearError();
        dlg.setBusy(true);
        postReport(uid, status, findings, impression)
          .then(function (res) {
            dlg.setBusy(false);
            if (!res.ok) {
              var msg = (res.body && res.body.detail) ? res.body.detail : 'HTTP ' + res.code;
              showError(msg);
              notify('error',
                status === 'FINALIZED' ? 'Could not sign the report' : 'Could not save the draft',
                msg);
              return;
            }
            if (status === 'FINALIZED') {
              notify('success', 'Report signed',
                'The report is finalised, attributed to you and locked.');
              dlg.close('signed');
            } else {
              notify('success', 'Draft saved', 'You can keep editing this report.');
            }
          })
          .catch(function (err) {
            dlg.setBusy(false);
            showError('Network error — ' + err);
            notify('error', 'Save failed', String(err));
          });
      };

      if (status === 'FINALIZED') {
        D.confirm({
          name: 'report-finalize',
          title: 'Sign and finalize this report?',
          message: 'The report becomes read-only and is attributed to you. ' +
            'This cannot be undone from the viewer.',
          confirmText: 'Sign & finalize',
          cancelText: 'Keep editing',
          tone: 'warn',
          scene: 'pulse'
        }).then(function (confirmed) { if (confirmed) run(); });
        return;
      }
      run();
    }

    draftBtn.addEventListener('click', function () { save('DRAFT'); });
    signBtn.addEventListener('click', function () {
      // C06: signing is privileged. The server enforces it; the UI must not
      // offer a button that will be refused.
      var canFinalize = !!paxsUser &&
        (paxsUser.role === 'ADMIN' || paxsUser.role === 'RADIOLOGIST');
      if (!canFinalize) {
        notify('warning', 'Not permitted',
          'Finalizing a report requires a radiologist or admin role.');
        return;
      }
      save('FINALIZED');
    });

    fetchReportMeta(uid)
      .then(function (meta) {
        loading.hidden = true;
        form.hidden = false;

        // The dialog focused itself while the body was a spinner; now that the
        // editor exists, put the caret where the radiologist is going to type.
        if (document.activeElement === dlg.root || document.activeElement === document.body) {
          try { findingsEl.focus(); } catch (e) { /* noop */ }
        }
        body.querySelector('#paxs-meta-patient').textContent = meta.patient_name || 'UNKNOWN';
        body.querySelector('#paxs-meta-pid').textContent = meta.patient_id || '--';
        body.querySelector('#paxs-meta-mod').textContent = meta.modality || 'CT';
        body.querySelector('#paxs-meta-acc').textContent = meta.accession_number || '--';
        body.querySelector('#paxs-meta-desc').textContent = meta.study_description || '--';

        if (meta.report) {
          findingsEl.value = meta.report.findings || '';
          impressionEl.value = meta.report.impression || '';
          findingsEl.dispatchEvent(new Event('input'));
          impressionEl.dispatchEvent(new Event('input'));
        }

        if (!paxsUser || (paxsUser.role !== 'ADMIN' && paxsUser.role !== 'RADIOLOGIST')) {
          signBtn.disabled = true;
          signBtn.setAttribute('data-paxs-locked', '1');
          signBtn.title = 'Finalizing a report requires a radiologist or admin role';
        }

        if (isSignedReport(meta.report)) {
          showSignedReport(meta.report, uid, {
            body: body,
            findingsEl: findingsEl,
            impressionEl: impressionEl,
            draftBtn: draftBtn,
            signBtn: signBtn
          });
        }

        fetch('/api/v1/templates?modality=' + encodeURIComponent(meta.modality || ''),
          { headers: paxsAuthHeaders() })
          .then(function (tr) { return tr.json(); })
          .then(function (tmpls) {
            var sel = body.querySelector('#paxs-rpt-tmpl-select');
            if (!tmpls || !tmpls.length) return;
            for (var i = 0; i < tmpls.length; i++) {
              var opt = document.createElement('option');
              opt.value = i;
              opt.textContent = tmpls[i].title;
              sel.appendChild(opt);
            }
            sel.addEventListener('change', function () {
              var idx = sel.value;
              if (idx === '' || !tmpls[idx]) return;
              var t = tmpls[idx];
              if (t.body_findings_template) findingsEl.value = t.body_findings_template;
              if (t.default_impression_template) impressionEl.value = t.default_impression_template;
              findingsEl.dispatchEvent(new Event('input'));
              impressionEl.dispatchEvent(new Event('input'));
            });
          })
          .catch(function () { /* templates are optional */ });
      })
      .catch(function (e) {
        loading.hidden = true;
        form.hidden = false;
        showError('Could not load this study\'s metadata — ' + e);
      });
  }

  // -------------------------------------------------------------------------
  // C-19 — the addendum chain of a FINALIZED report
  //
  // A signed report is immutable: the server refuses a content change (409) and
  // the only legal follow-up is an append-only addendum, attributed to the
  // authenticated author, audited and delivered to the EMR as an amended ORU.
  // The dialog therefore stops being an editor once the report is signed: the
  // signature is shown read-only, the append control appears only for a role the
  // server will accept, and the chain is re-read from the API after each append
  // so the modal shows the persisted author and timestamp, not a local guess.
  // -------------------------------------------------------------------------
  function isSignedReport(report) {
    return !!report && String(report.status || '').toUpperCase() === 'FINALIZED';
  }

  function refreshAddendumChain(addenda) {
    var host = document.getElementById('paxs-rpt-chain');
    if (!host) return;
    var api = window.PaxsReportAddenda;
    host.innerHTML = api ? api.chainHtml(addenda || []) : '';
  }

  function showSignedReport(report, uid, ui) {
    ui.findingsEl.readOnly = true;
    ui.impressionEl.readOnly = true;
    ui.draftBtn.hidden = true;
    ui.signBtn.hidden = true;
    var templateSelect = ui.body.querySelector('#paxs-rpt-tmpl-select');
    if (templateSelect) templateSelect.disabled = true;

    ui.body.querySelector('#paxs-rpt-signed-findings').textContent = report.findings || '—';
    ui.body.querySelector('#paxs-rpt-signed-impression').textContent = report.impression || '—';
    var block = ui.body.querySelector('#paxs-rpt-addenda');
    if (block) block.hidden = false;

    refreshAddendumChain(report.addenda);

    var api = window.PaxsReportAddenda;
    if (api && report.id && api.canAuthor(paxsUser && paxsUser.role)) {
      mountAddendumForm(report, uid);
    }
  }

  function mountAddendumForm(report, uid) {
    var host = document.getElementById('paxs-rpt-addendum-form');
    var api = window.PaxsReportAddenda;
    if (!host || !api) return;
    host.innerHTML = api.formHtml({
      prefix: 'paxs-rpt-addendum',
      placeholder: 'Correction, clarification or follow-up to the signed report…',
      submitLabel: 'Append addendum'
    });

    var form = host.querySelector('form');
    var input = host.querySelector('#paxs-rpt-addendum-text');
    var errorBox = host.querySelector('#paxs-rpt-addendum-error');
    var submit = host.querySelector('#paxs-rpt-addendum-submit');

    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var text = (input.value || '').trim();
      if (!text) {
        errorBox.textContent = 'An addendum needs text.';
        errorBox.hidden = false;
        input.focus();
        return;
      }
      errorBox.hidden = true;
      submit.disabled = true;
      postAddendum(report.id, text)
        .then(function (res) {
          submit.disabled = false;
          if (!res.ok) {
            var msg = (res.body && res.body.detail) ? res.body.detail : 'HTTP ' + res.code;
            errorBox.textContent = msg;
            errorBox.hidden = false;
            notify('error', 'Could not append the addendum', msg);
            return;
          }
          input.value = '';
          notify('success', 'Addendum appended',
            'The signed text is unchanged; the addendum is stored, attributed and audited.');
          // Re-read the report: the authoritative author/timestamp ordering of
          // the chain belongs to the server, not to this dialog.
          fetchReportMeta(uid)
            .then(function (meta) {
              if (meta && meta.report) refreshAddendumChain(meta.report.addenda);
            })
            .catch(function () { /* appended; the chain refreshes on reopen */ });
        })
        .catch(function (err) {
          submit.disabled = false;
          errorBox.textContent = 'Network error — ' + err;
          errorBox.hidden = false;
        });
    });
  }

  function postAddendum(reportId, text) {
    return paxsFetch('/api/v1/reports/' + encodeURIComponent(reportId) + '/addendum', {
      method: 'POST',
      headers: paxsAuthHeaders(),
      body: JSON.stringify({ text: text })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        return { ok: res.ok, code: res.status, body: body };
      });
    });
  }

  function openReportModal(studyUid) {
    var dialogs = paxsDialogs();
    if (!dialogs) {
      openLegacyReportModal(studyUid);
      return;
    }
    var uid = reportUidFrom(studyUid);
    if (!uid) {
      notify('warning', 'No study selected',
        'Open a study in the viewer before writing a report.');
      return;
    }
    openReportDialog(uid);
  }

  var lastDictation = null;

  function mountDictationPlayer(host, uid, hideWhenEmpty, extraOpts) {
    var prefix = archiveStudyListUrl().slice(0, -'/studies-ui'.length);
    var endpoint = prefix + '/api/v1/studies/' + encodeURIComponent(uid) + '/dictations';
    var opts = extraOpts || {};

    if (window.PaxsVoicePlayer && typeof window.PaxsVoicePlayer.mount === 'function') {
      paxsEnsureRichAssets();
      return window.PaxsVoicePlayer.mount(host, {
        uid: uid,
        endpoint: endpoint,
        fetch: paxsFetch,
        hideWhenEmpty: !!hideWhenEmpty,
        compact: !opts.enableShortcuts,
        enableShortcuts: !!opts.enableShortcuts,
        resumeState: lastDictation,
        onSaveResume: function (state) { lastDictation = state; }
      });
    }

    var controller = new AbortController();
    var audio = document.createElement('audio');
    audio.controls = true;
    audio.preload = 'metadata';
    audio.style.cssText = 'display:block;width:100%;height:38px;margin-top:6px';
    audio.setAttribute('aria-label', 'Dictation playback');
    var trackList = document.createElement('select');
    trackList.setAttribute('aria-label', 'Dictation track');
    trackList.style.cssText = 'display:block;width:100%;color-scheme:light dark';
    var message = document.createElement('p');
    message.setAttribute('role', 'status');
    message.style.margin = '4px 0';
    if (!hideWhenEmpty) {
      message.textContent = 'Loading dictations…';
      host.append(message);
    }

    function loadTrack(id, resume) {
      audio.src = endpoint + '/' + encodeURIComponent(id) + '/audio';
      audio.onloadedmetadata = function () {
        if (resume > 0) audio.currentTime = resume;
      };
      message.textContent = '';
    }

    audio.onerror = function () { message.textContent = 'Could not play this recording.'; };
    trackList.onchange = function () { loadTrack(trackList.value, 0); };

    paxsFetch(endpoint, { signal: controller.signal })
      .then(function (res) {
        if (hideWhenEmpty && (res.status === 403 || res.status === 404)) return [];
        if (!res.ok) throw new Error('Dictation list unavailable');
        return res.json();
      })
      .then(function (tracks) {
        if (controller.signal.aborted) return;
        if (!tracks.length) {
          message.textContent = 'No recordings for this study.';
          return;
        }
        tracks.forEach(function (track) {
          var option = document.createElement('option');
          option.value = track.id;
          option.textContent = track.recorded_by + ' · ' + new Date(track.created_at).toLocaleString();
          trackList.append(option);
        });
        var resume = lastDictation && lastDictation.uid === uid &&
          tracks.some(function (track) { return track.id === lastDictation.id; });
        if (resume) trackList.value = lastDictation.id;
        host.hidden = false;
        host.replaceChildren(trackList, audio, message);
        loadTrack(trackList.value, resume ? lastDictation.position : 0);
      })
      .catch(function () {
        if (controller.signal.aborted) return;
        host.hidden = false;
        message.textContent = 'Could not load dictations.';
        host.replaceChildren(message);
      });

    return function () {
      controller.abort();
      if (trackList.value) {
        lastDictation = { uid: uid, id: trackList.value, position: audio.currentTime };
      }
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    };
  }

  window.paxsMountDictationPlayer = mountDictationPlayer;
  window.paxsOpenReportModal = openReportModal;

  // -------------------------------------------------------------------------
  // Left panel (viewerLayoutResizableLeftPanel) — study isolation + tabs
  //
  // SB-1  The sidebar is locked to the study in the URL. The All / Recent tabs
  //       are the leak: they re-point the browser at every study the hanging
  //       protocol knows about, which is how another patient's series ended up
  //       in this patient's viewer. Primary is forced active and its siblings
  //       are removed from the tab order.
  // SB-2  Activation is rate-limited. Clicking a Radix tab fires a state update
  //       that mutates the DOM, which re-enters the observer — the old code
  //       re-clicked on every pass and produced a click storm.
  // SB-5  Nothing React owns is ever `.remove()`d or `innerHTML`-rewritten.
  //       The old study filter called block.remove() on React nodes (which
  //       throws NotFoundError inside reconciliation) and overwrote the
  //       modality span's innerHTML (which desynced the text node React keeps).
  //       Every visual change here is an attribute or a class, so React's diff
  //       stays valid and the change survives a re-render by being re-applied.
  // -------------------------------------------------------------------------

  var TAB_SELECTOR = '[role="tab"]';
  var PANEL_SELECTORS = [
    '#viewerLayoutResizableLeftPanel',
    'div[data-cy="viewerLayoutResizableLeftPanel"]',
    'div[class*="viewerLayoutResizableLeftPanel"]',
    'div[data-cy="study-browser"]',
    'div[class*="study-browser"]'
  ];
  var STUDY_UID_ATTRS = ['data-study-uid', 'data-studyinstanceuid', 'data-study-instance-uid', 'data-study'];

  function findLeftPanel() {
    for (var i = 0; i < PANEL_SELECTORS.length; i++) {
      var panel = document.querySelector(PANEL_SELECTORS[i]);
      if (panel) return panel;
    }
    return null;
  }

  /**
   * Classify one tab of a study-browser tab bar.
   *
   * Radix TabsTrigger renders `id="${baseId}-trigger-${value}"` and
   * `aria-controls="${baseId}-content-${value}"`, so the `value` ("primary",
   * "recent", "all") is in the id even when the visible label is localised —
   * which is why the old text-only match failed in Arabic.
   *
   * Returns '' when the tab is not one of the study-browser modes, so callers
   * can leave unrelated tab bars (Measurements / Segments) alone.
   */
  function tabKind(btn) {
    if (!btn || !btn.getAttribute) return '';
    var signals = [
      btn.id,
      btn.getAttribute('aria-controls'),
      btn.getAttribute('value'),
      btn.getAttribute('data-value'),
      btn.getAttribute('data-cy'),
      btn.getAttribute('name')
    ].join(' ').toLowerCase();

    if (signals.indexOf('primary') !== -1) return 'primary';
    if (signals.indexOf('recent') !== -1) return 'recent';
    if (/(^|[\s\-_])all([\s\-_]|$)/.test(signals)) return 'all';

    var text = (btn.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (!text) return '';
    if (text === 'primary' || text.indexOf('primary') !== -1 || text === 'أولي') return 'primary';
    if (text === 'recent' || text.indexOf('recent') !== -1 || text === 'حديث') return 'recent';
    if (text === 'all' || text === 'الكل' || text === 'todos' || text === 'все') return 'all';
    return '';
  }

  var tabLock = { lastAttempt: 0, attempts: 0, max: 30, timer: null };

  /**
   * SB-2 — retry once the cooldown has elapsed.
   *
   * The rate limit exists to stop a click storm, but dropping the request
   * outright loses the lock when the cooldown swallows the only mutation React
   * produced. A single timer (never a loop) guarantees convergence: one retry,
   * bounded by tabLock.max total attempts.
   */
  function scheduleRelock(btn) {
    if (tabLock.timer || tabLock.attempts >= tabLock.max) return;
    var wait = Math.max(80, 700 - (Date.now() - tabLock.lastAttempt) + 20);
    tabLock.timer = setTimeout(function () {
      tabLock.timer = null;
      if (!btn || !btn.isConnected) return;
      if (btn.getAttribute('data-state') === 'active') return;
      tabLock.lastAttempt = Date.now();
      tabLock.attempts++;
      forceActivateTab(btn);
    }, wait);
  }

  /** Dispatch the full pointer sequence Radix listens for. */
  function forceActivateTab(btn) {
    if (!btn) return;
    var opts = { bubbles: true, cancelable: true, view: window, button: 0, buttons: 1 };
    try { btn.focus({ preventScroll: true }); } catch (e) { /* older engines */ }
    try { btn.dispatchEvent(new PointerEvent('pointerdown', opts)); } catch (e) { /* no PointerEvent */ }
    try { btn.dispatchEvent(new MouseEvent('mousedown', opts)); } catch (e) { /* ignore */ }
    try { btn.dispatchEvent(new PointerEvent('pointerup', opts)); } catch (e) { /* ignore */ }
    try { btn.dispatchEvent(new MouseEvent('mouseup', opts)); } catch (e) { /* ignore */ }
    try { btn.click(); } catch (e) { /* ignore */ }
  }

  function hideTab(btn) {
    if (btn.getAttribute('data-paxs-tab-hidden') === '1') return;
    btn.setAttribute('data-paxs-tab-hidden', '1');
    btn.setAttribute('aria-hidden', 'true');
    btn.setAttribute('tabindex', '-1');
    // CSS owns the hiding (display/pointer-events). The attribute + the
    // property below are belt and braces for anything that ignores the sheet.
    btn.style.setProperty('display', 'none', 'important');
    btn.style.setProperty('pointer-events', 'none', 'important');
    try { btn.disabled = true; } catch (e) { /* not a button */ }
  }

  function lockTablist(list) {
    var tabs = list.querySelectorAll(TAB_SELECTOR);
    if (tabs.length < 2) return false;

    var primary = null;
    for (var i = 0; i < tabs.length; i++) {
      if (tabKind(tabs[i]) === 'primary') { primary = tabs[i]; break; }
    }
    if (!primary) return false;   // not a study-browser tab bar — leave it alone

    for (var j = 0; j < tabs.length; j++) {
      if (tabs[j] !== primary) hideTab(tabs[j]);
    }

    primary.setAttribute('data-paxs-primary-locked', '1');
    if (primary.getAttribute('aria-selected') === 'true' ||
        primary.getAttribute('data-state') === 'active') {
      return true;                 // already the active mode: nothing to click
    }

    var now = Date.now();
    if (now - tabLock.lastAttempt < 700) {   // SB-2
      scheduleRelock(primary);
      return false;
    }
    if (tabLock.attempts >= tabLock.max) return false;
    tabLock.lastAttempt = now;
    tabLock.attempts++;
    forceActivateTab(primary);
    return true;
  }

  function lockStudyTabs(panel) {
    var lists = panel.querySelectorAll('[role="tablist"]');
    if (lists.length) {
      for (var i = 0; i < lists.length; i++) lockTablist(lists[i]);
      return;
    }
    // No tablist wrapper: treat the panel's tabs as a single group.
    var tabs = panel.querySelectorAll(TAB_SELECTOR);
    if (tabs.length && tabs[0].parentElement) lockTablist(tabs[0].parentElement);
  }

  /**
   * SB-1 — hide any study block whose UID is not the one in the URL.
   *
   * Attribute-only: the block is never removed, so React can still unmount it.
   */
  function isolateStudyBlocks(panel, allowed) {
    if (!allowed.length) return 0;
    var nodes = panel.querySelectorAll(
      '[data-study-uid],[data-studyinstanceuid],[data-study-instance-uid],[data-study]'
    );
    var blocked = 0;
    for (var i = 0; i < nodes.length; i++) {
      var uid = '';
      for (var a = 0; a < STUDY_UID_ATTRS.length && !uid; a++) {
        uid = nodes[i].getAttribute(STUDY_UID_ATTRS[a]) || '';
      }
      if (!uid) continue;

      var isBlocked = !PaxsThumb.studyUidAllowed(uid, allowed);
      var flag = isBlocked ? '1' : '0';
      if (nodes[i].getAttribute('data-paxs-study-blocked') === flag) continue;

      nodes[i].setAttribute('data-paxs-study-blocked', flag);
      if (isBlocked) {
        nodes[i].setAttribute('aria-hidden', 'true');
        blocked++;
      } else {
        nodes[i].removeAttribute('aria-hidden');
      }
    }
    return blocked;
  }

  // A blocked study must not be openable either: a click that survives the CSS
  // would navigate the viewer to another patient's study.
  var panelClickGuardBound = false;
  function bindPanelClickGuard(panel) {
    if (panelClickGuardBound) return;
    panelClickGuardBound = true;
    panel.addEventListener('click', function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      if (!target.closest('[data-paxs-study-blocked="1"]')) return;
      event.preventDefault();
      event.stopPropagation();
    }, true);
  }

  var boundImages = typeof WeakSet === 'function' ? new WeakSet() : null;

  /** Track the native thumbnail's load state for the shimmer skeleton. */
  function bindCardImage(card) {
    var img = card.querySelector('img');
    if (!img) {
      if (card.getAttribute('data-paxs-thumb') !== 'pending') {
        card.setAttribute('data-paxs-thumb', 'pending');
      }
      return;
    }
    if (boundImages && boundImages.has(img)) return;
    if (boundImages) boundImages.add(img);

    var mark = function (state) {
      if (card.isConnected) card.setAttribute('data-paxs-thumb', state);
    };
    img.addEventListener('load', function () { mark('ready'); });
    img.addEventListener('error', function () { mark('error'); });
    if (img.complete) mark(img.naturalWidth ? 'ready' : 'error');
    else mark('loading');
  }

  /** Instant selection feedback that outlives a React re-render. */
  function bindCardClick(card, panel) {
    if (card.getAttribute('data-paxs-click-bound') === '1') return;
    card.setAttribute('data-paxs-click-bound', '1');
    card.addEventListener('pointerdown', function () {
      panel.setAttribute('data-paxs-selected-series', card.getAttribute('data-series') || '');
      var siblings = panel.querySelectorAll('.paxs-active-series');
      for (var i = 0; i < siblings.length; i++) {
        if (siblings[i] !== card) siblings[i].classList.remove('paxs-active-series');
      }
      card.classList.add('paxs-active-series');
    }, { passive: true });
  }

  /** Match MPR's supported multi-slice stack rule for a truthful card badge. */
  function seriesViewKind(modality, instanceCount) {
    var mod = String(modality || '').trim().toUpperCase();
    var count = Number.parseInt(String(instanceCount || '').replace(/[^0-9]/g, ''), 10) || 0;
    return /^(CT|MR|PT|NM|US)$/.test(mod) && count > 5 ? 'volume' : 'plane';
  }

  /**
   * SB-5 — decorate the series cards.
   *
   * PAXs's Thumbnail renders `[data-cy="study-browser-thumbnail"]` with
   * `data-series` (a displaySetInstanceUID), a `[data-cy="series-modality-label"]`
   * and a `[data-cy="series-description-label"]`. We read those, tag the card
   * with the modality, and let paxs-sidebar-theme.css draw the pill, the
   * skeleton and the active glow. No node is created inside the card.
   */
  function decorateSeriesCards(panel) {
    var cards = panel.querySelectorAll(
      '[data-cy="study-browser-thumbnail"],[data-cy="study-browser-thumbnail-no-image"]'
    );
    var selected = panel.getAttribute('data-paxs-selected-series') || '';

    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var label = card.querySelector('[data-cy="series-modality-label"]');
      var modality = label ? (label.textContent || '').trim().toUpperCase() : '';
      if (!modality) modality = card.getAttribute('data-paxs-mod') || '';

      if (modality && card.getAttribute('data-paxs-mod') !== modality) {
        card.setAttribute('data-paxs-mod', modality);
        card.setAttribute('data-paxs-mod-key', modality.toLowerCase());
      }

      var countLabel = card.querySelector('[data-cy="thumbnail-detail-InstanceCount"]');
      var countParts = countLabel ? (countLabel.textContent || '').match(/[0-9]+/g) : null;
      var imageCount = countParts ? countParts[countParts.length - 1] : 0;
      var viewKind = seriesViewKind(modality, imageCount);
      if (card.getAttribute('data-paxs-view-kind') !== viewKind) {
        card.setAttribute('data-paxs-view-kind', viewKind);
        card.setAttribute('aria-description', viewKind === 'volume'
          ? 'Multi-slice series; available for 3D MPR review.'
          : '2D image series.');
      }

      var pill = label && label.parentElement;
      if (pill && !pill.classList.contains('paxs-mod-pill')) pill.classList.add('paxs-mod-pill');

      // React marks the active series by swapping the highlight class on the
      // status dot; the click we recorded covers the frame before it re-renders.
      var isActive = !!card.querySelector('[class*="bg-highlight"]') ||
                     (!!selected && card.getAttribute('data-series') === selected);
      if (isActive !== card.classList.contains('paxs-active-series')) {
        card.classList.toggle('paxs-active-series', isActive);
      }

      bindCardImage(card);
      bindCardClick(card, panel);
    }
    return cards.length;
  }

  function enhanceLeftSidebarSeries() {
    if (!PaxsThumb) return false;

    var allowed = PaxsThumb.parseActiveStudyUIDs(window.location.search);
    if (!allowed.length) return false;        // study list: no single study in scope

    var panel = findLeftPanel();
    if (!panel) return false;

    // Stable CSS hook: paxs-sidebar-theme.css scopes on
    // :is(#viewerLayoutResizableLeftPanel, [data-paxs-panel]), so the skin
    // still applies if the layout changes the generated panel id.
    if (panel.getAttribute('data-paxs-panel') !== '1') {
      panel.setAttribute('data-paxs-panel', '1');
    }

    // SB-1 at the transport layer: the engine refuses to resolve or fetch any
    // study outside this scope, so a stale card cannot pull pixels either.
    PaxsThumb.setAllowed(allowed);

    lockStudyTabs(panel);
    isolateStudyBlocks(panel, allowed);
    bindPanelClickGuard(panel);
    decorateSeriesCards(panel);
    return true;
  }

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
    if (/^\/(api\/|dicom-web\/)/.test(url)) {
      url = archiveStudyListUrl().slice(0, -'/studies-ui'.length) + url;
    }
    var isRefreshCall = String(url).indexOf('/api/v1/auth/refresh') !== -1;

    function attempt(retried) {
      var o = {};
      for (var k in opts) { if (Object.prototype.hasOwnProperty.call(opts, k)) o[k] = opts[k]; }
      o.credentials = 'same-origin';
      o.headers = {};
      var base = opts.headers || {};
      for (var h in base) { if (Object.prototype.hasOwnProperty.call(base, h)) o.headers[h] = base[h]; }
      if (!/^(GET|HEAD|OPTIONS|TRACE)$/.test(String(o.method || 'GET').toUpperCase())) {
        var csrf = document.cookie.match(/(?:^|;\s*)paxs_csrf=([^;]+)/);
        if (csrf) o.headers['X-CSRF-Token'] = csrf[1];
      }
      try {
        var t = localStorage.getItem('paxs_token');
        if (t) o.headers['Authorization'] = 'Bearer ' + t;
      } catch (e) {}

      return fetch(url, o).then(function (res) {
        if (res.status !== 401 || retried || isRefreshCall) return res;
        return paxsRefreshToken().then(function (tok) {
          if (!tok) { window.dispatchEvent(new Event('paxs-auth-expired')); return res; }
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
  var PAXS_AUTH_RETRY_PATHS = ['/dicom-web/', '/api/v1/volume/', '/api/v1/reports/', '/api/v1/priors/'];

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
          if (!tok) { window.dispatchEvent(new Event('paxs-auth-expired')); return res; }
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
          if (!tok) { window.dispatchEvent(new Event('paxs-auth-expired')); return; }
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
    if (paxsRefreshDone) return paxsRefreshInFlight || Promise.resolve(null);
    paxsRefreshDone = true;
    return paxsRefreshToken();
  }

  function paxsAuthHeaders() {
    var h = { 'Content-Type': 'application/json' };
    try {
      var t = localStorage.getItem('paxs_token');
      if (t) {
        h['Authorization'] = 'Bearer ' + t;
      } else {
        var csrf = paxsReadCsrfToken();
        if (csrf) h['X-CSRF-Token'] = csrf;
      }
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
    if (isPaxsViewerActive()) return;
    if (!paxsUser) return;
    if (document.querySelector('[data-paxs-react-user-chip]')) return; // handled natively by React
    // This chip belongs to the Study List app bar (studies-ui). The deep-linked
    // OHIF viewer has no app bar at all, and Shepherd's onboarding tour renders
    // the only <header> on that page — inside a fixed, z-index 9999 <dialog>.
    // Matching it used to mount the chip inside a tooltip, where it vanished the
    // moment the tour was dismissed. So: require a real page-chrome header and
    // bail out on the viewer instead of inventing one.
    var header = document.querySelector('header.h-14, header[class*="bg-slate-900"]');
    if (!header) {
      var candidates = document.querySelectorAll('header');
      for (var h = 0; h < candidates.length; h++) {
        if (candidates[h].closest('.shepherd-element') || candidates[h].closest('dialog')) continue;
        header = candidates[h];
        break;
      }
    }
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

  // -------------------------------------------------------------------------
  // Boot — one coalesced pass for every enhancer
  //
  // The file used to run TWO body observers that both called enhance(), plus a
  // 400ms poll: every mutation ran the study list, the sidebar, the footer and
  // the toolbar sweep, twice. There is now one observer, one queued pass, and
  // a re-entrancy flag.
  //
  // The observer is DISCONNECTED for the duration of the pass, so the writes
  // this file makes (classes, data-paxs-* attributes, img.src) cannot feed
  // themselves back in. It is re-attached even when a pass throws, because a
  // dead observer silently freezes every enhancement for the rest of the
  // session.
  // -------------------------------------------------------------------------

  var OBSERVE_OPTS = {
    childList: true,
    subtree: true,
    // Only the attributes React actually flips: the tab value and the series
    // identity. Watching `class` or `src` would observe our own writes.
    attributes: true,
    attributeOldValue: true,
    attributeFilter: ['data-state', 'aria-selected', 'data-series']
  };

  // S5 (plans/09): a stack scroll rewrites the slice overlay and re-sets toolbar
  // attributes to the same values on every step. None of that is ours to enhance,
  // so those records no longer trigger a full-document pass.
  function hasRelevantMutation(records) {
    for (var i = 0; i < records.length; i++) {
      var record = records[i];
      var node = record.target.nodeType === 1 ? record.target : record.target.parentNode;
      if (node && node.closest && node.closest('[data-cy="viewport-grid"]')) continue;
      if (record.type === 'attributes' &&
          record.oldValue === record.target.getAttribute(record.attributeName)) continue;
      return true;
    }
    return false;
  }

  function enhanceAll() {
    if (PaxsThumb) enhanceStudyList();   // study list rows (no-op elsewhere)
    updateViewerLogo();
    injectViewerFooter();
    colorizeToolbarIcons();
    enhanceLeftSidebarSeries();          // viewer sidebar (no-op on the list)
    injectUserChip();
  }

  var enhanceQueued = false;
  var enhanceRafId = 0;
  var enhanceTimerId = 0;
  var enhanceObserver = new MutationObserver(function (records) {
    if (hasRelevantMutation(records)) scheduleEnhance();
  });

  /**
   * Run one coalesced pass. Whichever of the frame callback / timer fires
   * first wins; the other is cancelled.
   */
  function runEnhancePass() {
    if (!enhanceQueued) return;
    enhanceQueued = false;
    if (enhanceRafId) { cancelAnimationFrame(enhanceRafId); enhanceRafId = 0; }
    if (enhanceTimerId) { clearTimeout(enhanceTimerId); enhanceTimerId = 0; }
    if (!enhanceObserver) return;   // script torn down mid-frame
    enhanceObserver.disconnect();
    try {
      enhanceAll();
    } catch (e) {
      // One bad pass (a half-mounted panel, a detached node) must not stop
      // the loop for the rest of the session.
    }
    enhanceObserver.observe(document.body, OBSERVE_OPTS);
  }

  function scheduleEnhance() {
    if (enhanceQueued) return;
    enhanceQueued = true;
    enhanceRafId = requestAnimationFrame(runEnhancePass);
    // A browser pauses requestAnimationFrame in a hidden tab. Without this
    // fallback the pass is never run, enhanceQueued stays true forever, and
    // every later scheduleEnhance() returns early — the enhancer dies silently
    // until the tab is shown again.
    enhanceTimerId = setTimeout(runEnhancePass, 250);
  }

  // V1-1: install the transport guard BEFORE anything can fetch an image, so a
  // 401 on a WADO-RS frame triggers a refresh + replay instead of a black viewport.
  paxsInstallAuthGuards();
  // Refresh can revoke the previous session token; do not race it with /auth/me.
  paxsSilentRefresh().then(fetchPaxsUser);

  // Hand the engine the authenticated transport. Every QIDO/WADO thumbnail
  // request now rides the same single-flight refresh as the rest of the app.
  if (PaxsThumb) {
    PaxsThumb.configure({ request: paxsFetch });
    PaxsThumb.setAllowed(PaxsThumb.parseActiveStudyUIDs(window.location.search));
  }

  enhanceObserver.observe(document.body, OBSERVE_OPTS);

  // Bounded start-up pulse. The study list marks its root `.paxs-sl` with a
  // class write (not observed here), and the viewer mounts the sidebar several
  // frames after this script runs — so kick the pass a few times, then stop.
  var bootTicks = 0;
  (function bootPulse() {
    scheduleEnhance();
    if (++bootTicks < 6) setTimeout(bootPulse, 400 * bootTicks);
  })();

  // Expose for diagnostics / re-installation if another script swaps window.fetch.
  window.__paxsInstallAuthGuards = paxsInstallAuthGuards;
  window.__paxsEnhance = scheduleEnhance;

  // Expose for diagnostics / re-installation if another script swaps window.fetch.
  window.__paxsInstallAuthGuards = paxsInstallAuthGuards;

})();


