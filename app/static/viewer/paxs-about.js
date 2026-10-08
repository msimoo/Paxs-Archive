/**
 * PAXS Viewer — modernized "About PAXS Viewer" modal (diagnostic UI + telemetry).
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * The About entry point lives inside the prebuilt OHIF fork bundle
 * (app.bundle.*.js), which opens the stock OHIF AboutModal via the modal
 * service. That bundle is immutable at runtime (app-config.js declares
 * `extensions: []` / `modes: []`, so no customization can be registered
 * without a full rebuild).
 *
 * So we take the same vanilla-override route already proven by
 * paxs-study-list-enhance.js: intercept the header menu item in the CAPTURE
 * phase, suppress the native modal, and render our own clinical-workstation
 * dialog instead.
 *
 * CONTRACT
 * --------
 *   • Intercepts `[role="menuitem"]` whose label is "About" / "حول"
 *     (viewer header AND study-list header) and calls preventDefault() +
 *     stopPropagation() so the stock modal never renders.
 *   • Traps pointer / wheel / keyboard events at the capture phase over the
 *     overlay, so no pan / zoom / W/L / scroll reaches the DICOM viewports
 *     underneath while the dialog is open.
 *   • Dismisses on Escape, backdrop click, and the explicit close button.
 *   • Reads live telemetry from window.__getPaxsMprState() (exposed by
 *     paxs-mpr.js). Outside an active volume session it falls back to an
 *     independent WebGL2 capability probe.
 *
 * No build step: plain ES5-compatible vanilla JS, matching the rest of the
 * /viewer override scripts.
 */
(function () {
  'use strict';

  if (window.__paxsAboutInstalled) return;
  window.__paxsAboutInstalled = true;

  var OVERLAY_ID = 'paxs-about-overlay';
  var OPEN_CLASS = 'paxs-about-open';
  var TRANSITION_MS = 150;

  var PLATFORM_VERSION = 'v2026.1';
  var LEAD_ARCHITECT = 'Mohamed O. Elsamani';

  // ---------------------------------------------------------------------------
  // Styles — clinical dark workstation. Injected once; light-theme overrides
  // mirror the `:root[data-theme="light"]` convention used elsewhere.
  // ---------------------------------------------------------------------------
  function injectStyles() {
    if (document.getElementById('paxs-about-style')) return;
    var st = document.createElement('style');
    st.id = 'paxs-about-style';
    st.textContent =
      // ---- overlay / backdrop -------------------------------------------
      '#' + OVERLAY_ID + '{position:fixed;inset:0;z-index:2147483000;display:flex;' +
      'align-items:center;justify-content:center;padding:24px;' +
      'background:radial-gradient(120% 120% at 50% 0%,rgba(8,12,22,.72),rgba(4,6,12,.86));' +
      'backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);' +
      'opacity:0;transition:opacity ' + TRANSITION_MS + 'ms ease;' +
      'font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}' +
      '#' + OVERLAY_ID + '.' + OPEN_CLASS + '{opacity:1}' +

      // ---- dialog --------------------------------------------------------
      '#' + OVERLAY_ID + ' .paxs-about-dialog{position:relative;width:min(660px,100%);' +
      'max-height:calc(100vh - 48px);overflow:auto;overscroll-behavior:contain;' +
      'background:linear-gradient(160deg,rgba(15,23,42,.94),rgba(9,13,22,.97));' +
      'border:1px solid #1e293b;border-radius:18px;' +
      'box-shadow:0 32px 90px rgba(0,0,0,.66),0 0 0 1px rgba(34,211,238,.06),' +
      'inset 0 1px 0 rgba(255,255,255,.05);' +
      'backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);' +
      'transform:scale(.96);opacity:0;' +
      'transition:transform ' + TRANSITION_MS + 'ms cubic-bezier(.2,.8,.3,1),opacity ' + TRANSITION_MS + 'ms ease}' +
      '#' + OVERLAY_ID + '.' + OPEN_CLASS + ' .paxs-about-dialog{transform:scale(1);opacity:1}' +

      // accent hairline along the top edge
      '#' + OVERLAY_ID + ' .paxs-about-dialog::before{content:"";position:absolute;' +
      'top:0;left:12%;right:12%;height:1px;' +
      'background:linear-gradient(90deg,transparent,rgba(34,211,238,.7),rgba(52,211,153,.55),transparent)}' +

      // ---- header --------------------------------------------------------
      '#' + OVERLAY_ID + ' .pa-head{display:flex;align-items:center;gap:14px;' +
      'padding:22px 24px 18px;border-bottom:1px solid #1e293b}' +
      '#' + OVERLAY_ID + ' .pa-mark{position:relative;flex:none;width:44px;height:44px;' +
      'display:grid;place-items:center;border-radius:12px;' +
      'background:linear-gradient(150deg,rgba(34,211,238,.16),rgba(52,211,153,.08));' +
      'border:1px solid rgba(34,211,238,.28);' +
      'box-shadow:0 0 24px rgba(34,211,238,.18),inset 0 0 14px rgba(34,211,238,.08)}' +
      '#' + OVERLAY_ID + ' .pa-mark svg{display:block}' +
      '#' + OVERLAY_ID + ' .pa-mark .pa-dot{position:absolute;right:-2px;bottom:-2px;width:11px;height:11px;' +
      'border-radius:50%;background:#34d399;border:2px solid #0f172a;' +
      'box-shadow:0 0 10px rgba(52,211,153,.9);animation:paPulse 2.2s ease-in-out infinite}' +
      '@keyframes paPulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.55;transform:scale(.82)}}' +
      '#' + OVERLAY_ID + ' .pa-titles{flex:1;min-width:0}' +
      '#' + OVERLAY_ID + ' .pa-title{font-size:17px;font-weight:800;letter-spacing:.01em;' +
      'color:#e8eef7;line-height:1.25}' +
      '#' + OVERLAY_ID + ' .pa-title span{background:linear-gradient(90deg,#22d3ee,#34d399);' +
      '-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}' +
      '#' + OVERLAY_ID + ' .pa-sub{margin-top:3px;font-size:11.5px;color:#8b9bb4;line-height:1.5}' +
      '#' + OVERLAY_ID + ' .pa-sub b{color:#c7d3e2;font-weight:600}' +
      '#' + OVERLAY_ID + ' .pa-close{flex:none;width:32px;height:32px;border-radius:9px;' +
      'display:grid;place-items:center;cursor:pointer;background:rgba(148,163,184,.08);' +
      'border:1px solid rgba(148,163,184,.22);color:#94a3b8;' +
      'transition:color .15s,border-color .15s,background .15s}' +
      '#' + OVERLAY_ID + ' .pa-close:hover{color:#f1f5f9;border-color:rgba(34,211,238,.5);' +
      'background:rgba(34,211,238,.12)}' +
      '#' + OVERLAY_ID + ' .pa-close:focus-visible{outline:2px solid #22d3ee;outline-offset:2px}' +

      // ---- body ----------------------------------------------------------
      '#' + OVERLAY_ID + ' .pa-body{padding:18px 24px 8px}' +
      '#' + OVERLAY_ID + ' .pa-section{margin-bottom:18px}' +
      '#' + OVERLAY_ID + ' .pa-sec-h{display:flex;align-items:center;gap:8px;margin:0 0 10px;' +
      'font-size:10px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:#64748b}' +
      '#' + OVERLAY_ID + ' .pa-sec-h::after{content:"";flex:1;height:1px;' +
      'background:linear-gradient(90deg,#1e293b,transparent)}' +

      // telemetry grid
      '#' + OVERLAY_ID + ' .pa-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}' +
      '#' + OVERLAY_ID + ' .pa-cell{display:flex;flex-direction:column;gap:4px;padding:10px 12px;' +
      'background:rgba(15,23,42,.6);border:1px solid #1e293b;border-radius:10px;min-width:0}' +
      '#' + OVERLAY_ID + ' .pa-cell.pa-wide{grid-column:1/-1}' +
      '#' + OVERLAY_ID + ' .pa-k{font-size:9.5px;font-weight:700;letter-spacing:.1em;' +
      'text-transform:uppercase;color:#64748b}' +
      '#' + OVERLAY_ID + ' .pa-v{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;' +
      'font-size:12px;color:#dbe4f0;word-break:break-all;line-height:1.45}' +
      '#' + OVERLAY_ID + ' .pa-v.pa-muted{color:#7c8aa0}' +
      '#' + OVERLAY_ID + ' .pa-v.pa-ok{color:#34d399}' +
      '#' + OVERLAY_ID + ' .pa-v.pa-warn{color:#fbbf24}' +

      // mode badge
      '#' + OVERLAY_ID + ' .pa-mode{display:inline-flex;align-items:center;gap:7px;' +
      'padding:4px 11px;border-radius:999px;font-size:11px;font-weight:800;' +
      'letter-spacing:.06em;text-transform:uppercase;' +
      'font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}' +
      '#' + OVERLAY_ID + ' .pa-mode::before{content:"";width:7px;height:7px;border-radius:50%;' +
      'background:currentColor;box-shadow:0 0 8px currentColor}' +
      '#' + OVERLAY_ID + ' .pa-mode.pa-server{color:#38bdf8;background:rgba(56,189,248,.12);' +
      'border:1px solid rgba(56,189,248,.35)}' +
      '#' + OVERLAY_ID + ' .pa-mode.pa-client{color:#34d399;background:rgba(52,211,153,.12);' +
      'border:1px solid rgba(52,211,153,.35)}' +
      '#' + OVERLAY_ID + ' .pa-mode.pa-split{color:#a78bfa;background:rgba(167,139,250,.12);' +
      'border:1px solid rgba(167,139,250,.35)}' +
      '#' + OVERLAY_ID + ' .pa-mode.pa-idle{color:#94a3b8;background:rgba(148,163,184,.1);' +
      'border:1px solid rgba(148,163,184,.28)}' +

      // compliance badges
      '#' + OVERLAY_ID + ' .pa-badges{display:flex;flex-wrap:wrap;gap:7px}' +
      '#' + OVERLAY_ID + ' .pa-badge{padding:4px 10px;border-radius:7px;font-size:10.5px;font-weight:700;' +
      'letter-spacing:.03em;color:#7dd3fc;background:rgba(56,189,248,.08);' +
      'border:1px solid rgba(56,189,248,.22);' +
      'font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}' +

      // shortcuts
      '#' + OVERLAY_ID + ' .pa-keys{display:grid;grid-template-columns:1fr 1fr;gap:7px 16px}' +
      '#' + OVERLAY_ID + ' .pa-key{display:flex;align-items:center;justify-content:space-between;gap:10px;' +
      'font-size:11.5px;color:#a9b6c8}' +
      '#' + OVERLAY_ID + ' .pa-key kbd{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;' +
      'font-size:10.5px;font-weight:700;color:#e2e8f0;padding:2px 8px;border-radius:6px;' +
      'background:rgba(148,163,184,.12);border:1px solid rgba(148,163,184,.24);' +
      'box-shadow:0 1px 0 rgba(0,0,0,.4)}' +

      // ---- footer --------------------------------------------------------
      '#' + OVERLAY_ID + ' .pa-foot{padding:14px 24px 20px;border-top:1px solid #1e293b}' +
      '#' + OVERLAY_ID + ' .pa-disc{font-size:10.5px;line-height:1.6;color:#5c6b80}' +
      '#' + OVERLAY_ID + ' .pa-disc b{color:#8b9bb4;font-weight:600}' +
      '#' + OVERLAY_ID + ' .pa-sig{margin-top:9px;display:flex;align-items:center;gap:8px;' +
      'font-size:10.5px;color:#5c6b80}' +
      '#' + OVERLAY_ID + ' .pa-sig b{color:#94a3b8;font-weight:700}' +
      '#' + OVERLAY_ID + ' .pa-sig .pa-sep{width:3px;height:3px;border-radius:50%;' +
      'background:#334155;flex:none}' +

      // ---- light theme ---------------------------------------------------
      ':root[data-theme="light"] #' + OVERLAY_ID + '{' +
      'background:radial-gradient(120% 120% at 50% 0%,rgba(226,232,240,.7),rgba(203,213,225,.85))}' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .paxs-about-dialog{' +
      'background:linear-gradient(160deg,rgba(255,255,255,.97),rgba(241,245,249,.99));' +
      'border-color:rgba(15,23,42,.12);box-shadow:0 32px 80px rgba(15,23,42,.28)}' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .pa-title{color:#0f172a}' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .pa-sub{color:#64748b}' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .pa-sub b{color:#334155}' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .pa-head,' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .pa-foot{border-color:rgba(15,23,42,.1)}' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .pa-cell{background:rgba(255,255,255,.75);' +
      'border-color:rgba(15,23,42,.1)}' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .pa-v{color:#1e293b}' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .pa-v.pa-muted{color:#64748b}' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .pa-key{color:#475569}' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .pa-key kbd{color:#0f172a;' +
      'background:rgba(15,23,42,.06);border-color:rgba(15,23,42,.14)}' +
      ':root[data-theme="light"] #' + OVERLAY_ID + ' .pa-mark{background:linear-gradient(150deg,rgba(14,165,233,.12),rgba(16,185,129,.06))}';

    (document.head || document.documentElement).appendChild(st);
  }

  // ---------------------------------------------------------------------------
  // Capability probe — used when no MPR volume session is active.
  // ---------------------------------------------------------------------------
  function probeCapabilities() {
    var out = { webgl: false, webgl2: false, renderer: '', vendor: '', vramMb: 0 };
    try {
      var cv = document.createElement('canvas');
      var gl = cv.getContext('webgl2');
      out.webgl2 = !!gl;
      if (!gl) gl = cv.getContext('webgl') || cv.getContext('experimental-webgl');
      if (!gl) return out;
      out.webgl = true;
      var dbg = gl.getExtension('WEBGL_debug_renderer_info');
      if (dbg) {
        out.renderer = String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || '');
        out.vendor = String(gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) || '');
      } else {
        out.renderer = String(gl.getParameter(gl.RENDERER) || '');
      }
      // Release the probe context (browsers cap live WebGL contexts).
      var lose = gl.getExtension('WEBGL_lose_context');
      if (lose) lose.loseContext();
    } catch (e) { /* probing is best-effort */ }
    return out;
  }

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function shortUid(uid) {
    if (!uid) return '—';
    var s = String(uid);
    // Keep it readable but unambiguous: head…tail.
    return s.length > 34 ? s.slice(0, 20) + '…' + s.slice(-10) : s;
  }

  function modeInfo(effective) {
    var m = String(effective || '').toLowerCase();
    if (m === 'server' || m === 'rendered') {
      return { cls: 'pa-server', label: 'Server', note: 'thin client · server-rendered frames' };
    }
    if (m === 'client') {
      return { cls: 'pa-client', label: 'Client (GPU)', note: 'local WebGL2 volume pipeline' };
    }
    if (m === 'split') {
      return { cls: 'pa-split', label: 'Hybrid [Split]', note: 'local preview + server-refined slices' };
    }
    return { cls: 'pa-idle', label: 'Idle', note: 'no active session — awaiting negotiation' };
  }

  // ---------------------------------------------------------------------------
  // Modal construction
  // ---------------------------------------------------------------------------
  function buildBody(tele, caps) {
    var mode = modeInfo(tele && tele.effectiveMode);
    var configured = tele && tele.configuredMode ? String(tele.configuredMode) : 'hybrid';

    // --- Processing mode -----------------------------------------------------
    var scoreTxt = tele && typeof tele.clientScore === 'number'
      ? tele.clientScore + ' / 100' : '—';
    var rttTxt = tele && typeof tele.rttMs === 'number'
      ? tele.rttMs + ' ms' : '—';

    // --- GPU -----------------------------------------------------------------
    var renderer = caps.renderer || 'unavailable';
    var gpuCls = caps.webgl2 ? 'pa-v pa-ok' : (caps.webgl ? 'pa-v pa-warn' : 'pa-v pa-warn');
    var gpuLabel = caps.webgl2 ? 'WebGL2 · hardware accelerated'
      : (caps.webgl ? 'WebGL1 only · degraded' : 'no WebGL context');

    // --- Active dataset ------------------------------------------------------
    var hasVol = !!(tele && tele.dims);
    var dimsTxt = hasVol
      ? (tele.dims.cols + ' × ' + tele.dims.rows + ' × ' + tele.dims.slices + ' voxels')
      : 'no volume loaded';
    var spacingTxt = (hasVol && tele.spacing)
      ? (tele.spacing.row + ' / ' + tele.spacing.col + ' / ' + tele.spacing.slice + ' mm')
      : '—';

    return '' +
      '<div class="pa-section">' +
        '<div class="pa-sec-h">Processing telemetry</div>' +
        '<div class="pa-grid">' +
          '<div class="pa-cell pa-wide">' +
            '<span class="pa-k">Active processing mode</span>' +
            '<span class="pa-mode ' + mode.cls + '">' + esc(mode.label) + '</span>' +
            '<span class="pa-v pa-muted" style="margin-top:2px">' + esc(mode.note) +
              ' · configured: ' + esc(configured) + '</span>' +
          '</div>' +
          '<div class="pa-cell"><span class="pa-k">Client capability score</span>' +
            '<span class="pa-v">' + esc(scoreTxt) + '</span></div>' +
          '<div class="pa-cell"><span class="pa-k">Network RTT</span>' +
            '<span class="pa-v">' + esc(rttTxt) + '</span></div>' +
        '</div>' +
      '</div>' +

      '<div class="pa-section">' +
        '<div class="pa-sec-h">Hardware acceleration</div>' +
        '<div class="pa-grid">' +
          '<div class="pa-cell pa-wide"><span class="pa-k">GPU renderer</span>' +
            '<span class="' + gpuCls + '">' + esc(renderer) + '</span>' +
            '<span class="pa-v pa-muted" style="margin-top:2px">' + esc(gpuLabel) + '</span></div>' +
        '</div>' +
      '</div>' +

      '<div class="pa-section">' +
        '<div class="pa-sec-h">Active dataset</div>' +
        '<div class="pa-grid">' +
          '<div class="pa-cell pa-wide"><span class="pa-k">StudyInstanceUID</span>' +
            '<span class="pa-v' + (tele && tele.studyUid ? '' : ' pa-muted') + '">' +
            esc(tele && tele.studyUid ? shortUid(tele.studyUid) : '—') + '</span></div>' +
          '<div class="pa-cell pa-wide"><span class="pa-k">SeriesInstanceUID</span>' +
            '<span class="pa-v' + (tele && tele.seriesUid ? '' : ' pa-muted') + '">' +
            esc(tele && tele.seriesUid ? shortUid(tele.seriesUid) : '—') + '</span></div>' +
          '<div class="pa-cell"><span class="pa-k">Voxel matrix</span>' +
            '<span class="pa-v' + (hasVol ? '' : ' pa-muted') + '">' + esc(dimsTxt) + '</span></div>' +
          '<div class="pa-cell"><span class="pa-k">Voxel spacing</span>' +
            '<span class="pa-v' + (hasVol ? '' : ' pa-muted') + '">' + esc(spacingTxt) + '</span></div>' +
        '</div>' +
      '</div>' +

      '<div class="pa-section">' +
        '<div class="pa-sec-h">Standards compliance</div>' +
        '<div class="pa-badges">' +
          '<span class="pa-badge">DICOM 3.0</span>' +
          '<span class="pa-badge">WADO-RS</span>' +
          '<span class="pa-badge">WebGL2 MPR</span>' +
          '<span class="pa-badge">HTJ2K Lossless</span>' +
        '</div>' +
      '</div>' +

      '<div class="pa-section">' +
        '<div class="pa-sec-h">Keyboard &amp; mouse reference</div>' +
        '<div class="pa-keys">' +
          '<div class="pa-key"><span>Window / Level</span><kbd>Left drag</kbd></div>' +
          '<div class="pa-key"><span>Zoom</span><kbd>Right drag</kbd></div>' +
          '<div class="pa-key"><span>Scroll slices</span><kbd>Wheel</kbd></div>' +
          '<div class="pa-key"><span>Pan</span><kbd>Shift + drag</kbd></div>' +
        '</div>' +
      '</div>';
  }

  function buildModal() {
    var overlay = document.createElement('div');
    overlay.id = OVERLAY_ID;
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'About PAXS Viewer');

    var tele = null;
    try { tele = window.__getPaxsMprState ? window.__getPaxsMprState() : null; } catch (e) { tele = null; }
    var caps = (tele && tele.capabilities)
      ? { webgl: !!tele.capabilities.webgl, webgl2: !!tele.capabilities.webgl2,
          renderer: '', vendor: '' }
      : probeCapabilities();
    // Always resolve a renderer string — the MPR bridge reports flags only.
    if (!caps.renderer) {
      var p = probeCapabilities();
      caps.renderer = p.renderer;
      if (!caps.webgl && p.webgl) { caps.webgl = true; caps.webgl2 = p.webgl2; }
    }

    var dialog = document.createElement('div');
    dialog.className = 'paxs-about-dialog';

    dialog.innerHTML =
      '<div class="pa-head">' +
        '<div class="pa-mark" aria-hidden="true">' +
          '<svg width="24" height="24" viewBox="0 0 32 32" fill="none">' +
            '<rect x="3.5" y="6" width="25" height="20" rx="4.5" stroke="#22d3ee" stroke-width="1.8"/>' +
            '<path d="M3.5 12.5h25M3.5 19.5h25M11 6v20M21 6v20" stroke="#2dd4bf" stroke-width="1.2"/>' +
            '<circle cx="16" cy="16" r="2.4" fill="#34d399"/>' +
          '</svg>' +
          '<span class="pa-dot"></span>' +
        '</div>' +
        '<div class="pa-titles">' +
          '<div class="pa-title"><span>PAXS Viewer</span></div>' +
          '<div class="pa-sub">PAXS Archive Platform · <b>' + esc(PLATFORM_VERSION) + '</b>' +
            ' &nbsp;|&nbsp; Lead Architect: <b>' + esc(LEAD_ARCHITECT) + '</b></div>' +
        '</div>' +
        '<button class="pa-close" type="button" aria-label="Close" title="Close (Esc)">' +
          '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
            'stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>' +
        '</button>' +
      '</div>' +
      '<div class="pa-body">' + buildBody(tele, caps) + '</div>' +
      '<div class="pa-foot">' +
        '<div class="pa-disc"><b>Clinical notice.</b> This workstation is intended for secondary ' +
          'diagnostic display and clinical review of DICOM studies. Diagnostic interpretation ' +
          'must be performed on a calibrated, primary-diagnostic display. Measurements and ' +
          'rendered reconstructions are subject to acquisition parameters and are not a ' +
          'substitute for professional medical judgement.</div>' +
        '<div class="pa-sig">' +
          '<span>PAXS Archive · clinical imaging platform</span>' +
          '<span class="pa-sep" aria-hidden="true"></span>' +
          '<span>© 2026 <b>' + esc(LEAD_ARCHITECT) + '</b></span>' +
        '</div>' +
      '</div>';

    overlay.appendChild(dialog);
    return overlay;
  }

  // ---------------------------------------------------------------------------
  // Event trapping
  //
  // Listeners are bound on the overlay in the BUBBLE phase. The overlay is a
  // fixed, full-viewport node above the DICOM viewports, so:
  //   • no pointer event can ever target a viewport canvas underneath, and
  //   • stopping propagation at the overlay keeps events from reaching any
  //     document/body-level OHIF handler (tool hotkeys, wheel, drag).
  // Bubble (not capture) matters: it lets the dialog's own controls — chiefly
  // the close button — run their handlers before we swallow the event.
  // preventDefault() is deliberately NOT called, so native scrolling inside
  // the dialog (overflow:auto) keeps working.
  // ---------------------------------------------------------------------------
  var TRAPPED = ['pointerdown', 'pointerup', 'pointermove', 'mousedown', 'mouseup',
                 'mousemove', 'click', 'dblclick', 'wheel', 'contextmenu',
                 'touchstart', 'touchmove'];

  function trap(overlay, evt) {
    overlay.addEventListener(evt, function (e) {
      // Backdrop click dismisses; anything inside the dialog is swallowed.
      if (e.type === 'click' && e.target === overlay) {
        e.preventDefault();
        e.stopPropagation();
        closeAbout();
        return;
      }
      e.stopPropagation();
    }, false);
  }

  var activeOverlay = null;
  var keyHandler = null;
  var prevFocus = null;

  function openAbout() {
    if (activeOverlay) return;
    injectStyles();

    var overlay = buildModal();
    document.body.appendChild(overlay);
    activeOverlay = overlay;

    // Trap pointer / wheel / touch over the whole overlay (capture phase).
    for (var i = 0; i < TRAPPED.length; i++) trap(overlay, TRAPPED[i]);

    // Escape closes — capture phase so OHIF hotkeys never see it.
    keyHandler = function (e) {
      if (e.key === 'Escape' || e.keyCode === 27) {
        e.preventDefault();
        e.stopPropagation();
        closeAbout();
      } else if (e.key === 'Tab') {
        // Keep focus inside the dialog.
        e.preventDefault();
        var btn = overlay.querySelector('.pa-close');
        if (btn) btn.focus();
      }
    };
    document.addEventListener('keydown', keyHandler, true);

    var closeBtn = overlay.querySelector('.pa-close');
    if (closeBtn) closeBtn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      closeAbout();
    });

    // Animate in on the next frame so the transition actually runs.
    // Guarded on `activeOverlay === overlay` so an immediate close (Escape
    // pressed before the frame fires) can never re-add the open class.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        if (overlay && activeOverlay === overlay) overlay.classList.add(OPEN_CLASS);
      });
    });

    // The About entry lives inside a Radix dropdown menu. Because we swallowed
    // the click (to suppress the stock modal), the menu never got its own
    // dismissal signal — so nudge any open dismissable layer closed with a
    // synthetic outside pointerdown. Harmless when no menu is open.
    try {
      if (typeof window.PointerEvent === 'function') {
        document.body.dispatchEvent(new window.PointerEvent('pointerdown', {
          bubbles: true, cancelable: true, composed: true
        }));
      }
    } catch (e) { /* best-effort menu dismissal */ }

    prevFocus = document.activeElement;
    if (closeBtn) closeBtn.focus();
  }

  function closeAbout() {
    var overlay = activeOverlay;
    if (!overlay) return;
    activeOverlay = null;

    if (keyHandler) {
      document.removeEventListener('keydown', keyHandler, true);
      keyHandler = null;
    }

    overlay.classList.remove(OPEN_CLASS);
    setTimeout(function () {
      if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }, TRANSITION_MS);

    // Restore focus to wherever it was before the dialog opened.
    try { if (prevFocus && prevFocus.focus) prevFocus.focus(); } catch (e) {}
    prevFocus = null;
  }

  // ---------------------------------------------------------------------------
  // Interception — suppress the stock OHIF About modal.
  // ---------------------------------------------------------------------------
  var ABOUT_LABELS = ['about', 'حول', 'حول التطبيق', 'acerca de', 'über', 'à propos', 'sobre'];

  function isAboutMenuItem(el) {
    if (!el) return false;
    // data-cy hints (OHIF stamps many menu items).
    var cy = (el.getAttribute && (el.getAttribute('data-cy') || '')) || '';
    if (/about/i.test(cy)) return true;

    var text = (el.textContent || '').trim();
    if (!text || text.length > 24) return false;
    var lower = text.toLowerCase();
    for (var i = 0; i < ABOUT_LABELS.length; i++) {
      if (lower === ABOUT_LABELS[i] || lower.indexOf(ABOUT_LABELS[i]) === 0) return true;
    }
    return false;
  }

  document.addEventListener('click', function (e) {
    var target = e.target;
    if (!target || !target.closest) return;
    var item = target.closest('[role="menuitem"], [data-cy*="about"]');
    if (!item) return;
    if (!isAboutMenuItem(item)) return;

    // Suppress the native modal entirely.
    e.preventDefault();
    e.stopPropagation();
    if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    openAbout();
  }, true);

  // ---------------------------------------------------------------------------
  // Expose a small API (used by the footer link and for diagnostics).
  // ---------------------------------------------------------------------------
  window.paxsOpenAbout = openAbout;
  window.paxsCloseAbout = closeAbout;

})();
