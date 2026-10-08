/* ==========================================================================
   PAXS — Upgraded 3D & 2D Multi-Planar Reconstruction (MPR) DICOM Workspace
   Loaded from viewer/index.html & study list UI.

   ===========================================================================
   CANONICAL AXIS CONVENTION (see plan_9_2026.md C09)
   ===========================================================================
   The volume is a Float32Array indexed [k][j][i]:
       index = k * rows * cols + j * cols + i
       i = column index, j = row index, k = slice index

   Every plane has a horizontal u axis and a vertical v axis:

       plane      const  u (horizontal)  v (vertical)   viewport w x h
       ------------------------------------------------------------------
       axial      k      i (X, cols)     j (Y, rows)    cols  x rows
       sagittal   i      j (Y, rows)     k (Z, slices)  rows  x slices
       coronal    j      i (X, cols)     k (Z, slices)  cols  x slices

   This matches the backend (app/services/volume_service.py: reslice_plane /
   mip_project rows-cols + [vertical, horizontal] spacing) and the plane
   labels ("AXIAL · Z" / "SAGITTAL · X" / "CORONAL · Y" name the CONSTANT
   index axis).

   ALL voxel/viewport/patient maths lives in paxs-mpr-geometry.js, which must
   be loaded BEFORE this file. Do not re-derive an index, a plane dimension or
   a millimetre value inline anywhere in this module — call the geometry
   helpers instead, otherwise the probe, ROI, crosshairs, measurements and the
   WebGL2 slice shader drift apart again.

   UI/UX Expert Refinement:
   - Three.js 3D Spatial Volume & Anatomical Orientation Viewport with live
     3D cutplanes (Axial Z, Sagittal X, Coronal Y), 3D volume bounding box,
     orientation compass cube (A/P, L/R, S/I), orbit controls & camera presets.
   - ACR Radiology 4-Corner Viewport Overlays (Patient Name/Modality, Series/Instance,
     Physical Location/Thickness, W/L and Hounsfield Unit HU pixel intensity lookup).
   - Keyboard Shortcuts Modal (`⌨ Shortcuts` or `?`/`h` key).
   - Cine Scrub Loop Player with Speed Selector (5 FPS, 15 FPS, 30 FPS).
   - shadcn/ui Design Tokens & WCAG AA Accessible Contrast.
   ========================================================================== */
(function () {
  if (window.__paxsMprLoaded) return;
  window.__paxsMprLoaded = true;

  var STYLE_ID = 'paxs-mpr-style';
  var ROOT_ID = 'paxs-mpr-root';
  var SHORTCUTS_ID = 'paxs-mpr-shortcuts';
  var THREE_LOCAL = '/viewer/three.min.js';
  var THREE_CDN = 'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js';

  // Centralised geometry (single source of truth for every coordinate
  // transform and physical measurement — see the header comment).
  var GEO = window.PaxsMprGeometry || null;

  function geo() {
    if (!GEO) {
      throw new Error('paxs-mpr-geometry.js must be loaded before paxs-mpr.js');
    }
    return GEO;
  }

  // Dev-mode GPU/resource accounting (R7): enabled with
  // window.__paxsMprDebug = true before opening the workspace.
  var resourceStats = { geometries: 0, materials: 0, textures: 0 };

  function trackResource(kind) {
    resourceStats[kind] = (resourceStats[kind] || 0) + 1;
  }

  function releaseResource(kind, count) {
    resourceStats[kind] = Math.max(0, (resourceStats[kind] || 0) - (count || 1));
  }

  // Dev-mode GPU accounting (R7): counters are always inspectable, and verbose
  // logging on dispose is enabled with window.__paxsMprDebug = true.
  window.paxsMprResourceStats = resourceStats;

  /** Geometry meta for a plane, including the current index along its normal. */
  function planeMeta(plane) {
    return geo().makeMeta({
      rows: state.rows,
      columns: state.cols,
      slices: state.slices,
      pixel_spacing: state.spacing,
      slice_spacing: state.sliceSpacing,
      image_position_patient: state.info ? state.info.image_position_patient : null,
      image_orientation_patient: state.info ? state.info.image_orientation_patient : null
    }, plane, state.idx[plane]);
  }

  /**
   * Millimetres per step along a plane's constant (normal) axis:
   * axial -> k (slice spacing), sagittal -> i (column spacing),
   * coronal -> j (row spacing). state.spacing is DICOM [row, col].
   */
  function normalSpacing(plane) {
    if (plane === 'sagittal') return state.spacing[1] || 1;
    if (plane === 'coronal') return state.spacing[0] || 1;
    return state.sliceSpacing || 1;
  }

  var state = {
    studyUid: null,
    seriesUid: null,
    info: null,
    vol: null,          // Float32Array (HU), [z][y][x]
    volCache: {},       // seriesUid -> { info, vol } in-memory cache for instant switching & zero-lag reload
    mode: 'gl',         // 'gl' | 'server'
    layout: '4grid',    // '4grid' | '3d' | 'axial' | 'sagittal' | 'coronal' | '1x2' | '1x3'
    slices: 0, rows: 0, cols: 0,
    spacing: [1, 1],    // [row, col] mm
    sliceSpacing: 1,
    idx: { axial: 0, sagittal: 0, coronal: 0 },
    wl: { c: 40, w: 400 },
    cine: { playing: false, timer: null, speed: 66, fps: 15 },
    hoverHu: { axial: '--', sagittal: '--', coronal: '--' },
    busy: false,
    projectionMode: 'normal',
    slabThickness: 1,
    volumeLut: 'bone',
    activeTool: 'crosshair', // 'crosshair' | 'ruler' | 'roi' | 'cobb' | 'ctr'
    measurements: [],        // array of { id, plane, sliceIdx, type, points: [...], stats: {...}, label: '...' }
    pendingTool: null,       // { type, plane, points: [] }
    fusion: {
      enabled: false,
      opacity: 0.5,
      colormap: 'hot_iron',  // 'hot_iron' | 'pet_spectrum' | 'rainbow' | 'cold_fire'
      overlaySeriesUid: null,
      overlayInfo: null,
      overlayVol: null
    },
    lod: { enabled: true, active: false, timer: null, scale: 0.5 },
    isInteracting: false,
    serverCache: {
      axial: { key: '', img: null },
      sagittal: { key: '', img: null },
      coronal: { key: '', img: null }
    },
    gl2: { supported: false, ctx: null, renderCv: null, volTex: null, overlayTex: null, prog: null, vao: null, uLoc: null }
  };

  var threeState = {
    loaded: false,
    loading: false,
    scene: null,
    camera: null,
    renderer: null,
    animFrame: null,
    planes: { axial: null, sagittal: null, coronal: null },
    box: null,
    volumeMesh: null,
    renderMode: 'planes', // 'planes' | 'vrt' | 'mip3d'
    transferFunction: 'bone', // 'bone' | 'angio' | 'soft' | 'mip'
    webGpuSupported: false,
    isDragging: false,
    prevMouse: { x: 0, y: 0 },
    rot: { x: -0.5, y: 0.6 },
    dist: 350,
    onPointerUp: null,      // named listener so it can be detached on dispose
    extraResources: []      // tracked objects not reachable via scene.traverse
  };

  function loadThree(cb) {
    if (window.THREE) {
      threeState.loaded = true;
      if (cb) cb();
      return;
    }
    if (threeState.loading) {
      var check = setInterval(function () {
        if (window.THREE) { clearInterval(check); threeState.loaded = true; if (cb) cb(); }
      }, 30);
      return;
    }
    threeState.loading = true;
    var script = document.createElement('script');
    script.src = THREE_LOCAL;
    script.onload = function () {
      threeState.loaded = true;
      threeState.loading = false;
      if (cb) cb();
    };
    script.onerror = function () {
      // Fallback to CDN if local file is missing
      var cdnScript = document.createElement('script');
      cdnScript.src = THREE_CDN;
      cdnScript.onload = function () {
        threeState.loaded = true;
        threeState.loading = false;
        if (cb) cb();
      };
      cdnScript.onerror = function () {
        threeState.loading = false;
        console.warn('PAXS MPR: Three.js failed to load — fallback to 2D WebGL viewports');
        if (cb) cb();
      };
      document.head.appendChild(cdnScript);
    };
    document.head.appendChild(script);
  }

  function probeGlSupport() {
    if (window.__paxsMprForceServer === true) return false;
    try {
      if (localStorage.getItem('paxs_mpr_server') === '1') return false;
    } catch (e) {}
    try {
      var cv = document.createElement('canvas');
      var gl = cv.getContext('webgl2', { antialias: false, depth: false });
      if (!gl) return false;
      var tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, 2, 2, 0, gl.RED, gl.FLOAT, new Float32Array([0.5, 0.5, 0.5, 0.5]));
      var ok = gl.getError() === 0;
      gl.deleteTexture(tex);
      // Release the probe context: browsers cap live WebGL contexts (~16), so
      // leaking one per workspace open eventually kills the 3D viewport (H15).
      var lose = gl.getExtension('WEBGL_lose_context');
      if (lose) lose.loseContext();
      return ok;
    } catch (e) {
      return false;
    }
  }

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var isLight = false;
    try { isLight = localStorage.getItem('paxs_theme') === 'light'; } catch (e) {}

    var css = [
      '#paxs-mpr-root{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;' +
        'background:rgba(3,7,18,0.85);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);' +
        'font-family:var(--sans,"Inter","Segoe UI",system-ui,sans-serif);animation:mpr-fade-in .22s cubic-bezier(.16,1,.3,1)}',
      '#paxs-mpr-root *{box-sizing:border-box}',

      '#paxs-mpr-root .mpr-panel{width:min(1440px,96vw);height:min(940px,94vh);display:flex;flex-direction:column;' +
        'background:' + (isLight ? '#ffffff' : '#090f1e') + ';border:1px solid ' + (isLight ? 'rgba(15,23,42,.15)' : 'rgba(148,163,184,.2)') + ';' +
        'border-radius:14px;box-shadow:0 32px 100px rgba(0,0,0,.7);overflow:hidden;color:' + (isLight ? '#0f172a' : '#f1f5f9') + '}',

      /* Header */
      '#paxs-mpr-root .mpr-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 20px;' +
        'border-bottom:1px solid ' + (isLight ? 'rgba(15,23,42,.1)' : 'rgba(148,163,184,.14)') + ';' +
        'background:' + (isLight ? '#f8fafc' : '#0d152a') + '}',

      '#paxs-mpr-root .mpr-brand{display:flex;align-items:center;gap:10px}',
      '#paxs-mpr-root .mpr-live{width:9px;height:9px;border-radius:50%;background:#34d399;box-shadow:0 0 10px #34d399;animation:mpr-pulse 2s infinite}',
      '@keyframes mpr-pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(0.85)}}',

      '#paxs-mpr-root .mpr-title{font-size:15px;font-weight:700;letter-spacing:.03em;display:flex;align-items:center;gap:8px}',
      '#paxs-mpr-root .mpr-badge{font-size:10px;font-family:ui-monospace,monospace;font-weight:700;letter-spacing:.08em;' +
        'padding:3px 8px;border-radius:6px;text-transform:uppercase;background:rgba(34,211,238,.12);color:#22d3ee;border:1px solid rgba(34,211,238,.3)}',

      '#paxs-mpr-root .mpr-sub{font-size:11px;color:#94a3b8;font-family:ui-monospace,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',

      /* Toolbar controls */
      '#paxs-mpr-root .mpr-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap}',

      '#paxs-mpr-root .mpr-btn{background:' + (isLight ? '#f1f5f9' : 'rgba(15,23,42,0.85)') + ';color:' + (isLight ? '#0f172a' : '#cbd5e1') + ';' +
        'border:1px solid ' + (isLight ? 'rgba(15,23,42,.15)' : 'rgba(148,163,184,.25)') + ';' +
        'border-radius:999px;padding:6px 14px;font-size:12px;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;gap:6px;' +
        'transition:all .2s cubic-bezier(.16,1,.3,1)}',
      '#paxs-mpr-root .mpr-btn:hover{background:rgba(30,41,59,0.9);color:#ffffff;border-color:rgba(34,211,238,.4);box-shadow:0 4px 14px rgba(34,211,238,.25);transform:translateY(-1px) scale(1.04)}',
      '#paxs-mpr-root .mpr-btn.active{background:linear-gradient(135deg,#22d3ee,#2dd4bf);color:#040914;border-color:transparent;font-weight:700;box-shadow:0 0 18px rgba(34,211,238,.55),inset 0 1px 0 rgba(255,255,255,.4);transform:scale(1.05)}',

      '#paxs-mpr-root select.mpr-select{background:' + (isLight ? '#ffffff' : 'rgba(2,6,23,0.85)') + ';color:' + (isLight ? '#0f172a' : '#e2e8f0') + ';' +
        'border:1px solid ' + (isLight ? 'rgba(15,23,42,.2)' : 'rgba(148,163,184,.3)') + ';border-radius:999px;padding:6px 12px;font-size:12px;font-weight:600;outline:none;cursor:pointer}',

      '#paxs-mpr-root button.mpr-close{background:rgba(248,113,113,.14);color:#fca5a5;border:1px solid rgba(248,113,113,.35);' +
        'border-radius:999px;padding:6px 14px;font-size:12px;font-weight:600;cursor:pointer;transition:all .18s ease}',
      '#paxs-mpr-root button.mpr-close:hover{background:#f87171;color:#ffffff;box-shadow:0 0 16px rgba(248,113,113,.5);transform:translateY(-1px)}',

      /* Progress Bar */
      '#paxs-mpr-root .mpr-progress{height:3px;background:' + (isLight ? '#e2e8f0' : '#0d152a') + ';overflow:hidden}',
      '#paxs-mpr-root .mpr-progress>div{height:100%;width:0%;background:linear-gradient(90deg,#22d3ee,#2dd4bf,#a855f7);transition:width .2s}',

      /* Subhead Clinical Toolbar */
      '#paxs-mpr-root .mpr-subhead{display:flex;align-items:center;justify-content:space-between;padding:6px 16px;' +
        'background:' + (isLight ? '#f1f5f9' : '#080e1e') + ';border-bottom:1px solid ' + (isLight ? 'rgba(15,23,42,.1)' : 'rgba(148,163,184,.14)') + ';flex-wrap:wrap;gap:8px}',
      '#paxs-mpr-root .mpr-tool-btn{background:' + (isLight ? '#ffffff' : 'rgba(15,23,42,0.85)') + ';color:' + (isLight ? '#475569' : '#94a3b8') + ';' +
        'border:1px solid ' + (isLight ? 'rgba(15,23,42,.15)' : 'rgba(148,163,184,.25)') + ';border-radius:6px;padding:4px 10px;font-size:11px;font-weight:600;cursor:pointer;' +
        'display:inline-flex;align-items:center;gap:4px;transition:all .18s ease}',
      '#paxs-mpr-root .mpr-tool-btn:hover{background:rgba(30,41,59,0.9);color:#ffffff;border-color:rgba(34,211,238,.4);transform:translateY(-1px)}',
      '#paxs-mpr-root .mpr-tool-btn.active{background:linear-gradient(135deg,#0ea5e9,#06b6d4);color:#ffffff;border-color:transparent;font-weight:700;box-shadow:0 0 12px rgba(14,165,233,.5)}',

      /* Main Viewport Body */
      '#paxs-mpr-root .mpr-body{display:grid;grid-template-columns:repeat(2,1fr);grid-template-rows:repeat(2,1fr);gap:10px;padding:12px;flex:1;min-height:0;overflow:hidden}',
      '#paxs-mpr-root .mpr-body.layout-3d{grid-template-columns:1fr;grid-template-rows:1fr}',
      '#paxs-mpr-root .mpr-body.layout-3d #mpr-3d-vp{grid-column:span 2;grid-row:span 2}',
      '#paxs-mpr-root .mpr-body.layout-axial #mpr-axial-vp{grid-column:span 2;grid-row:span 2}',
      '#paxs-mpr-root .mpr-body.layout-sagittal #mpr-sag-vp{grid-column:span 2;grid-row:span 2}',
      '#paxs-mpr-root .mpr-body.layout-coronal #mpr-cor-vp{grid-column:span 2;grid-row:span 2}',
      '#paxs-mpr-root .mpr-body.layout-1x2{grid-template-columns:1fr 1fr;grid-template-rows:1fr}',
      '#paxs-mpr-root .mpr-body.layout-1x2 #mpr-3d-vp, #paxs-mpr-root .mpr-body.layout-1x2 #mpr-sag-vp{display:none}',
      '#paxs-mpr-root .mpr-body.layout-1x3{grid-template-columns:repeat(3,1fr);grid-template-rows:1fr}',
      '#paxs-mpr-root .mpr-body.layout-1x3 #mpr-3d-vp{display:none}',

      /* Viewport Container */
      '#paxs-mpr-root .mpr-vp{display:flex;flex-direction:column;gap:6px;min-width:0;min-height:0;position:relative}',
      '#paxs-mpr-root .mpr-vp-header{display:flex;justify-content:space-between;align-items:center;padding:4px 8px;' +
        'background:' + (isLight ? '#f1f5f9' : '#0e172e') + ';border-radius:6px;font-size:11px;font-family:ui-monospace,monospace;' +
        'color:' + (isLight ? '#475569' : '#94a3b8') + ';letter-spacing:.05em}',
      '#paxs-mpr-root .mpr-vp-title{font-weight:700;display:flex;align-items:center;gap:6px}',
      '#paxs-mpr-root .mpr-vp-dot{width:6px;height:6px;border-radius:50%}',

      '#paxs-mpr-root .mpr-canvas-wrap{position:relative;flex:1;background:#03060d;border:1px solid ' +
        (isLight ? 'rgba(15,23,42,.15)' : 'rgba(148,163,184,.2)') + ';border-radius:10px;overflow:hidden;touch-action:none;cursor:crosshair}',
      '#paxs-mpr-root .mpr-canvas-wrap canvas{position:absolute;inset:0;width:100%;height:100%;display:block}',
      '#paxs-mpr-root .mpr-svg-overlay{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:5}',

      /* 3D Viewport Controls overlay */
      '#paxs-mpr-root .mpr-3d-tools{position:absolute;top:10px;left:10px;z-index:10;display:flex;gap:6px;flex-wrap:wrap}',
      '#paxs-mpr-root .mpr-3d-btn{background:rgba(11,19,41,0.75);backdrop-filter:blur(8px);color:#e2e8f0;border:1px solid rgba(255,255,255,0.15);' +
        'border-radius:6px;padding:4px 8px;font-size:10px;font-weight:600;cursor:pointer;transition:all .15s ease}',
      '#paxs-mpr-root .mpr-3d-btn:hover{background:#22d3ee;color:#040914;border-color:transparent}',

      /* ACR 4-Corner Viewport Overlays */
      '#paxs-mpr-root .mpr-corner-tl{position:absolute;top:8px;left:10px;z-index:4;font-family:ui-monospace,monospace;font-size:10px;color:#7dd3fc;text-shadow:0 1px 3px #000;pointer-events:none}',
      '#paxs-mpr-root .mpr-corner-tr{position:absolute;top:8px;right:10px;z-index:4;font-family:ui-monospace,monospace;font-size:10px;color:#7dd3fc;text-shadow:0 1px 3px #000;text-align:right;pointer-events:none}',
      '#paxs-mpr-root .mpr-corner-bl{position:absolute;bottom:8px;left:10px;z-index:4;font-family:ui-monospace,monospace;font-size:10px;color:#38bdf8;text-shadow:0 1px 3px #000;pointer-events:none}',
      '#paxs-mpr-root .mpr-corner-br{position:absolute;bottom:8px;right:10px;z-index:4;font-family:ui-monospace,monospace;font-size:10px;color:#38bdf8;text-shadow:0 1px 3px #000;text-align:right;pointer-events:none}',

      /* Crosshair lines */
      '#paxs-mpr-root .mpr-line{position:absolute;background:#22d3ee;opacity:.85;pointer-events:none;z-index:3;animation:crosshair-pulse 2s infinite ease-in-out}',
      '#paxs-mpr-root .mpr-line.v{width:1px;top:0;bottom:0}',
      '#paxs-mpr-root .mpr-line.h{height:1px;left:0;right:0}',
      '@keyframes crosshair-pulse{0%,100%{opacity:.75}50%{opacity:.95}}',

      '#paxs-mpr-root .mpr-vp input[type=range]{width:100%;height:5px;accent-color:#22d3ee;cursor:pointer;border-radius:999px}',
      '#paxs-mpr-root .mpr-error{color:#fca5a5;padding:20px;text-align:center;font-size:13px;font-family:ui-monospace,monospace}',
      '@keyframes mpr-fade-in{from{opacity:0;transform:scale(0.98)}to{opacity:1;transform:scale(1)}}'
    ].join('\n');

    var style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = css;
    document.head.appendChild(style);
  }

  function ensureButton() {
    var sl = document.querySelector('.paxs-sl');
    if (!sl || sl.querySelector('.paxs-mpr-launch')) return;
    var viewBtn = Array.prototype.find.call(
      sl.querySelectorAll('button'),
      function (b) { return (b.textContent || '').trim() === 'View'; }
    );
    var host = viewBtn ? viewBtn.parentElement : null;
    if (!host) return;
    var btn = document.createElement('button');
    btn.className = 'paxs-mpr-launch';
    btn.innerHTML = '⚡ 3D MPR';
    btn.title = 'Open 3D Multi-Planar Reconstruction (Three.js Volume Viewport)';
    btn.style.cssText = 'background:linear-gradient(135deg,#22d3ee,#2dd4bf);color:#040914;border:none;border-radius:6px;' +
      'padding:6px 14px;font-size:12px;font-weight:700;cursor:pointer;letter-spacing:.04em;' +
      'box-shadow:0 0 14px rgba(34,211,238,.4);transition:all .2s ease;';
    btn.addEventListener('mouseenter', function () { btn.style.transform = 'translateY(-1px) scale(1.03)'; });
    btn.addEventListener('mouseleave', function () { btn.style.transform = 'none'; });
    btn.addEventListener('click', openMpr);
    host.insertBefore(btn, viewBtn);
  }

  function openMpr() {
    if (state.busy || document.getElementById(ROOT_ID)) return;
    if (!GEO) {
      window.alert('MPR workspace unavailable: paxs-mpr-geometry.js failed to load.');
      return;
    }
    state.mode = probeGlSupport() ? 'gl' : 'server';
    injectStyle();
    var uids = collectStudyUids();
    if (!uids.length) { window.alert('No studies in the list to open in MPR.'); return; }
    state.studyUid = uids[0];
    buildPanel();
    loadThree(function () {
      loadSeriesList(state.studyUid);
    });
  }

  function collectStudyUids() {
    var uids = [];
    var match = window.location.search.match(/StudyInstanceUIDs=([^&]+)/) ||
                window.location.search.match(/study=([^&]+)/);
    if (match && match[1]) {
      uids.push(decodeURIComponent(match[1]));
    }
    var sl = document.querySelector('.paxs-sl');
    if (sl) {
      var rows = sl.querySelectorAll('tbody tr[class*="cursor-pointer"]');
      for (var i = 0; i < rows.length; i++) {
        var u = rows[i].dataset.paxsStudy;
        if (u && uids.indexOf(u) === -1) uids.push(u);
      }
    }
    return uids;
  }

  function buildPanel() {
    var root = document.createElement('div');
    root.id = ROOT_ID;
    root.innerHTML =
      '<div class="mpr-panel">' +
        '<div class="mpr-head">' +
          '<div class="mpr-brand">' +
            '<span class="mpr-live"></span>' +
            '<span class="mpr-title">PAXS Clinical 3D Workspace</span>' +
            '<span class="mpr-badge" id="mpr-modality">CT</span>' +
            '<span class="mpr-sub" id="mpr-sub">loading volume…</span>' +
          '</div>' +
          '<div class="mpr-controls">' +
            '<select class="mpr-select" id="mpr-series" title="Volumetric series"></select>' +
            '<div class="mpr-presets" style="display:flex;gap:4px;">' +
              '<button class="mpr-btn active" data-wl="40,400">Soft Tissue</button>' +
              '<button class="mpr-btn" data-wl="40,80">Brain</button>' +
              '<button class="mpr-btn" data-wl="-600,1500">Lung</button>' +
              '<button class="mpr-btn" data-wl="400,1800">Bone</button>' +
              '<button class="mpr-btn" data-wl="300,600">Angio</button>' +
            '</div>' +
            '<button class="mpr-btn" id="mpr-cine-toggle" title="Toggle Slice Cine Loop">▶ Cine</button>' +
            '<button class="mpr-btn active" id="mpr-lod-toggle" title="Adaptive Level-of-Detail: 60 FPS GPU Downsampling during interaction">⚡ LOD: ON</button>' +
            '<select class="mpr-select" id="mpr-layout-select" title="Hanging Protocol Layout View">' +
              '<option value="4grid">4-Grid (3D + 3 Planes)</option>' +
              '<option value="1x2">1x2 (Axial + Coronal Dual)</option>' +
              '<option value="1x3">1x3 (Tri-Planar Panoramic)</option>' +
              '<option value="3d">3D Spatial Focus</option>' +
              '<option value="axial">Axial Single Focus</option>' +
              '<option value="sagittal">Sagittal Single Focus</option>' +
              '<option value="coronal">Coronal Single Focus</option>' +
            '</select>' +
            '<button class="mpr-btn" id="mpr-shortcuts-btn" title="Keyboard Shortcuts (Key ?)">⌨ Hotkeys</button>' +
            '<button class="mpr-close" id="mpr-close-btn">✕ Close</button>' +
          '</div>' +
        '</div>' +
        '<div class="mpr-subhead">' +
          '<div class="mpr-tools-group" style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;">' +
            '<span style="color:#94a3b8;font-weight:700;font-size:11px;margin-right:2px;">CLINICAL TOOLS:</span>' +
            '<button class="mpr-tool-btn active" data-tool="crosshair" id="mpr-tool-crosshair" title="Crosshair Navigation & W/L Drag (C)">🎯 Crosshair</button>' +
            '<button class="mpr-tool-btn" data-tool="ruler" id="mpr-tool-ruler" title="Caliper / Distance Measurement (R)">📏 Caliper (mm)</button>' +
            '<button class="mpr-tool-btn" data-tool="roi" id="mpr-tool-roi" title="Elliptical ROI with Live HU & Volumetric Statistics (O)">⭕ ROI Stats (HU/cm³)</button>' +
            '<button class="mpr-tool-btn" data-tool="cobb" id="mpr-tool-cobb" title="Cobb Angle Spine Measurement (B)">📐 Cobb Angle</button>' +
            '<button class="mpr-tool-btn" data-tool="ctr" id="mpr-tool-ctr" title="Cardio-Thoracic Ratio (T)">🫀 CTR Ratio</button>' +
            '<button class="mpr-tool-btn" id="mpr-tool-clear" title="Clear all annotations on screen">🗑 Clear All</button>' +
          '</div>' +
          '<div class="mpr-fusion-group" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">' +
            '<span style="color:#f472b6;font-weight:700;font-size:11px;display:flex;align-items:center;gap:4px;">🔥 PET/CT FUSION:</span>' +
            '<button class="mpr-tool-btn" id="mpr-fusion-toggle" style="background:rgba(236,72,153,0.15);border-color:rgba(236,72,153,0.3);color:#f472b6;">Fusion: OFF</button>' +
            '<div id="mpr-fusion-controls" style="display:none;align-items:center;gap:6px;">' +
              '<select class="mpr-select" id="mpr-fusion-series" style="padding:2px 8px;font-size:10.5px;" title="Overlay Volume"></select>' +
              '<select class="mpr-select" id="mpr-fusion-cmap" style="padding:2px 8px;font-size:10.5px;" title="Color Map">' +
                '<option value="hot_iron">Hot Iron (PET)</option>' +
                '<option value="pet_spectrum">PET Spectrum</option>' +
                '<option value="rainbow">Rainbow (Jet)</option>' +
                '<option value="cold_fire">Cold Fire</option>' +
              '</select>' +
              '<span style="color:#94a3b8;font-size:10px;">Blend:</span>' +
              '<input type="range" id="mpr-fusion-opacity" min="0" max="100" value="50" style="width:65px;accent-color:#f472b6;cursor:pointer;">' +
              '<span id="mpr-fusion-val" style="color:#f472b6;font-size:10px;font-weight:700;width:24px;">50%</span>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="mpr-progress"><div id="mpr-prog"></div></div>' +
        '<div class="mpr-body" id="mpr-body">' +
          '<div class="mpr-vp" id="mpr-3d-vp">' +
            '<div class="mpr-vp-header"><span class="mpr-vp-title"><span class="mpr-vp-dot" style="background:#a855f7"></span>3D SPATIAL VOLUME (THREE.JS)</span><span id="mpr-3d-hud">ROTATION: 360°</span></div>' +
            '<div class="mpr-canvas-wrap" id="mpr-3d-wrap">' +
              '<div class="mpr-3d-tools">' +
                '<button class="mpr-3d-btn" id="mpr-3d-iso">ISO 3D</button>' +
                '<button class="mpr-3d-btn" id="mpr-3d-ant">Anterior (A)</button>' +
                '<button class="mpr-3d-btn" id="mpr-3d-sup">Superior (S)</button>' +
                '<button class="mpr-3d-btn" id="mpr-3d-left">Left (L)</button>' +
                '<select class="mpr-3d-select" id="mpr-3d-mode" style="background:rgba(11,19,41,0.85);color:#22d3ee;border:1px solid rgba(34,211,238,0.35);border-radius:6px;font-size:10px;font-weight:700;padding:3px 6px;cursor:pointer;">' +
                  '<option value="planes">Cutplanes (3D)</option>' +
                  '<option value="vrt">VRT Raymarch (3D)</option>' +
                  '<option value="mip3d">MIP 3D (Max Intensity)</option>' +
                '</select>' +
                '<select class="mpr-3d-select" id="mpr-3d-lut" style="background:rgba(11,19,41,0.85);color:#fbbf24;border:1px solid rgba(251,191,36,0.35);border-radius:6px;font-size:10px;font-weight:700;padding:3px 6px;cursor:pointer;">' +
                  '<option value="bone">Bone 3D</option>' +
                  '<option value="angio">Angio 3D</option>' +
                  '<option value="soft">Soft Tissue</option>' +
                  '<option value="mip">MIP Contrast</option>' +
                '</select>' +
              '</div>' +
              '<canvas id="mpr-3d-cv"></canvas>' +
            '</div>' +
          '</div>' +
          viewportHTML('AXIAL · Z', 'mpr-axial', '#22d3ee') +
          viewportHTML('SAGITTAL · X', 'mpr-sag', '#2dd4bf') +
          viewportHTML('CORONAL · Y', 'mpr-cor', '#fbbf24') +
        '</div>' +
      '</div>';

    document.body.appendChild(root);

    root.querySelector('#mpr-close-btn').addEventListener('click', closeMpr);
    root.querySelector('#mpr-shortcuts-btn').addEventListener('click', showShortcutsModal);
    root.addEventListener('keydown', handleKeydown);

    var layoutSel = root.querySelector('#mpr-layout-select');
    layoutSel.addEventListener('change', function () {
      setLayout(layoutSel.value);
    });

    var lodToggle = root.querySelector('#mpr-lod-toggle');
    if (lodToggle) {
      lodToggle.addEventListener('click', function () {
        state.lod.enabled = !state.lod.enabled;
        lodToggle.classList.toggle('active', state.lod.enabled);
        lodToggle.textContent = state.lod.enabled ? '⚡ LOD: ON' : '⚡ LOD: OFF';
      });
    }

    // Clinical Tools Selection
    root.querySelectorAll('.mpr-tool-btn[data-tool]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        root.querySelectorAll('.mpr-tool-btn[data-tool]').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        state.activeTool = btn.dataset.tool;
        state.pendingTool = null;
      });
    });

    var clearBtn = root.querySelector('#mpr-tool-clear');
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        state.measurements = [];
        state.pendingTool = null;
        renderAll();
      });
    }

    // PET/CT Fusion Toggles
    var fusionBtn = root.querySelector('#mpr-fusion-toggle');
    var fusionControls = root.querySelector('#mpr-fusion-controls');
    var fusionCmapSel = root.querySelector('#mpr-fusion-cmap');
    var fusionOpacitySlider = root.querySelector('#mpr-fusion-opacity');
    var fusionOpacityVal = root.querySelector('#mpr-fusion-val');

    if (fusionBtn) {
      fusionBtn.addEventListener('click', function () {
        state.fusion.enabled = !state.fusion.enabled;
        fusionBtn.textContent = state.fusion.enabled ? 'Fusion: ON' : 'Fusion: OFF';
        fusionBtn.classList.toggle('active', state.fusion.enabled);
        if (fusionControls) fusionControls.style.display = state.fusion.enabled ? 'inline-flex' : 'none';
        if (state.fusion.enabled) {
          loadFusionManifest();
        } else {
          renderAll();
        }
      });
    }

    if (fusionCmapSel) {
      fusionCmapSel.addEventListener('change', function () {
        state.fusion.colormap = fusionCmapSel.value;
        renderAll();
      });
    }

    if (fusionOpacitySlider) {
      fusionOpacitySlider.addEventListener('input', function () {
        var val = parseInt(fusionOpacitySlider.value, 10);
        state.fusion.opacity = val / 100.0;
        if (fusionOpacityVal) fusionOpacityVal.textContent = val + '%';
        renderAll();
      });
    }

    var mode3dSel = root.querySelector('#mpr-3d-mode');
    if (mode3dSel) {
      mode3dSel.addEventListener('change', function () {
        threeState.renderMode = mode3dSel.value;
        update3dPlanes();
      });
    }

    var lut3dSel = root.querySelector('#mpr-3d-lut');
    if (lut3dSel) {
      lut3dSel.addEventListener('change', function () {
        threeState.transferFunction = lut3dSel.value;
        update3dPlanes();
      });
    }

    root.querySelectorAll('.mpr-presets button').forEach(function (btn) {
      btn.addEventListener('click', function () {
        root.querySelectorAll('.mpr-presets button').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        var parts = btn.dataset.wl.split(',');
        state.wl.c = parseFloat(parts[0]);
        state.wl.w = parseFloat(parts[1]);
        scheduleRender();
      });
    });

    root.querySelector('#mpr-cine-toggle').addEventListener('click', toggleCine);

    root.querySelector('#mpr-3d-iso').addEventListener('click', function () { set3dCamera(-0.5, 0.6); });
    root.querySelector('#mpr-3d-ant').addEventListener('click', function () { set3dCamera(0, 0); });
    root.querySelector('#mpr-3d-sup').addEventListener('click', function () { set3dCamera(-Math.PI / 2, 0); });
    root.querySelector('#mpr-3d-left').addEventListener('click', function () { set3dCamera(0, Math.PI / 2); });
  }

  function viewportHTML(title, id, color) {
    return '<div class="mpr-vp" id="' + id + '-vp">' +
      '<div class="mpr-vp-header"><span class="mpr-vp-title"><span class="mpr-vp-dot" style="background:' + color + '"></span>' + title + '</span><span id="' + id + '-hud"></span></div>' +
      '<div class="mpr-canvas-wrap" id="' + id + '-wrap">' +
        '<div class="mpr-corner-tl" id="' + id + '-ctl">PATIENT: DICOM<br>MOD: CT</div>' +
        '<div class="mpr-corner-tr" id="' + id + '-ctr">SERIES: 1<br>ACR STANDARD</div>' +
        '<div class="mpr-corner-bl" id="' + id + '-cbl">POS: 0.0mm<br>THICK: 1.0mm</div>' +
        '<div class="mpr-corner-br" id="' + id + '-cbr">W/L: 400/40<br>HU: --</div>' +
        '<canvas id="' + id + '-cv"></canvas>' +
        '<svg class="mpr-svg-overlay" id="' + id + '-svg"></svg>' +
      '</div>' +
      '<input type="range" id="' + id + '-slider" min="0" max="100" value="50">' +
    '</div>';
  }

  function showShortcutsModal() {
    if (document.getElementById(SHORTCUTS_ID)) return;
    var dlg = document.createElement('div');
    dlg.id = SHORTCUTS_ID;
    dlg.style.cssText = 'position:fixed;inset:0;z-index:10000;display:flex;align-items:center;justify-content:center;' +
      'background:rgba(3,7,18,0.75);backdrop-filter:blur(10px);animation:mpr-fade-in .2s ease;';
    dlg.innerHTML =
      '<div style="width:min(480px,90vw);background:#0f182d;border:1px solid rgba(148,163,184,.25);border-radius:12px;' +
        'padding:20px;color:#f1f5f9;box-shadow:0 20px 60px rgba(0,0,0,.8);">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;border-bottom:1px solid rgba(148,163,184,.15);padding-bottom:10px;">' +
          '<h3 style="margin:0;font-size:15px;font-weight:700;color:#22d3ee;">⌨ DICOM MPR Keyboard Shortcuts</h3>' +
          '<button id="mpr-sc-close" style="background:none;border:none;color:#94a3b8;font-size:16px;cursor:pointer;">✕</button>' +
        '</div>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:12px;font-family:ui-monospace,monospace;">' +
          '<div><b style="color:#22d3ee;">Space</b> : Toggle Cine Play</div>' +
          '<div><b style="color:#22d3ee;">Arrow ↑/↓</b> : Next/Prev Slice</div>' +
          '<div><b style="color:#22d3ee;">1 - 5</b> : W/L Presets</div>' +
          '<div><b style="color:#22d3ee;">Tab</b> : Cycle Layouts</div>' +
          '<div><b style="color:#22d3ee;">Shift + Drag</b> : Pan View</div>' +
          '<div><b style="color:#22d3ee;">Scroll Wheel</b> : W/L Adjust / Zoom</div>' +
          '<div><b style="color:#22d3ee;">Escape</b> : Close Workspace</div>' +
          '<div><b style="color:#22d3ee;">? / H</b> : Hotkeys Help</div>' +
        '</div>' +
        '<div style="margin-top:14px;padding-top:10px;border-top:1px solid rgba(148,163,184,.15);text-align:center;font-size:12px;">' +
          '<a href="/paxs-viewer-guide" target="_blank" rel="noopener" style="color:#22d3ee;font-weight:700;text-decoration:none;">📖 Full PaxsViewer Guide (toolbar · layouts · measurements · reports)</a>' +
        '</div>' +
      '</div>';
    document.body.appendChild(dlg);
    dlg.querySelector('#mpr-sc-close').onclick = function () { dlg.remove(); };
    dlg.onclick = function (e) { if (e.target === dlg) dlg.remove(); };
  }

  function setLayout(layoutMode) {
    state.layout = layoutMode;
    var body = document.getElementById('mpr-body');
    if (!body) return;
    body.className = 'mpr-body layout-' + layoutMode;
    var vp3d = document.getElementById('mpr-3d-vp');
    if (layoutMode === '3d') {
      vp3d.style.gridColumn = 'span 2';
      vp3d.style.gridRow = 'span 2';
    } else {
      vp3d.style.gridColumn = '';
      vp3d.style.gridRow = '';
    }
    setTimeout(function () {
      resize3dCanvas();
      renderAll();
    }, 100);
  }

  function toggleCine() {
    state.cine.playing = !state.cine.playing;
    var btn = document.getElementById('mpr-cine-toggle');
    if (btn) {
      btn.textContent = state.cine.playing ? '⏸ Pause' : '▶ Cine';
      btn.classList.toggle('active', state.cine.playing);
    }
    if (state.cine.playing) {
      state.cine.timer = setInterval(function () {
        state.idx.axial = (state.idx.axial + 1) % Math.max(1, state.slices);
        var s = document.getElementById('mpr-axial-slider');
        if (s) s.value = state.idx.axial;
        scheduleRender();
      }, state.cine.speed);
    } else {
      if (state.cine.timer) clearInterval(state.cine.timer);
    }
  }

  function handleKeydown(e) {
    if (e.key === 'Escape') { closeMpr(); return; }
    if (e.key === '?' || e.key === 'h' || e.key === 'H') { showShortcutsModal(); return; }
    if (e.key === ' ') { e.preventDefault(); toggleCine(); return; }
    if (e.key === 'c' || e.key === 'C') {
      var btn = document.getElementById('mpr-tool-crosshair');
      if (btn) btn.click();
      return;
    }
    if (e.key === 'r' || e.key === 'R') {
      var rBtn = document.getElementById('mpr-tool-ruler');
      if (rBtn) rBtn.click();
      return;
    }
    if (e.key === 'o' || e.key === 'O') {
      var oBtn = document.getElementById('mpr-tool-roi');
      if (oBtn) oBtn.click();
      return;
    }
    if (e.key === 'b' || e.key === 'B') {
      var bBtn = document.getElementById('mpr-tool-cobb');
      if (bBtn) bBtn.click();
      return;
    }
    if (e.key === 't' || e.key === 'T') {
      var tBtn = document.getElementById('mpr-tool-ctr');
      if (tBtn) tBtn.click();
      return;
    }
    if (e.key === 'ArrowUp' || e.key === 'ArrowRight') {
      e.preventDefault();
      state.idx.axial = Math.min(state.slices - 1, state.idx.axial + 1);
      scheduleRender();
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') {
      e.preventDefault();
      state.idx.axial = Math.max(0, state.idx.axial - 1);
      scheduleRender();
    }
  }

  function closeMpr() {
    var root = document.getElementById(ROOT_ID);
    if (!root) return;
    if (state.cine.timer) { clearInterval(state.cine.timer); state.cine.timer = null; }
    if (state.lod.timer) { clearTimeout(state.lod.timer); state.lod.timer = null; }
    state.cine.playing = false;
    // Reject in-flight decodes before tearing anything down, so a close during
    // a load never leaves a promise pending (H15).
    shutdownWorkerPool('MPR workspace closed');
    disposeThreeScene();
    root.removeEventListener('keydown', handleKeydown);
    root.remove();
    releaseGl2();
    state.vol = null; state.info = null; state.busy = false;
    state.measurements = [];
    state.pendingTool = null;
  }

  function setProgress(p) {
    var bar = document.getElementById('mpr-prog');
    if (bar) bar.style.width = (p * 100).toFixed(1) + '%';
  }

  function fetchJson(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' for ' + url);
      return r.json();
    });
  }

  function loadSeriesList(studyUid) {
    var sel = document.getElementById('mpr-series');
    fetchJson('/dicom-web/studies/' + encodeURIComponent(studyUid) + '/series?includefield=0020000E,00080060,0008103E,00201209')
      .then(function (series) {
        var volumetric = (series || []).filter(function (s) {
          var mod = String((s['00080060'] || {}).Value ? s['00080060'].Value[0] : '').toUpperCase();
          var n = (s['00201209'] || {}).Value ? parseInt(s['00201209'].Value[0], 10) : 0;
          return (mod === 'CT' || mod === 'MR' || mod === 'PT' || mod === 'NM' || mod === 'US') && n > 5;
        });

        // Sort by instance count descending so the largest 3D volume stack (e.g. 60 slices) is auto-selected first!
        volumetric.sort(function (a, b) {
          var na = (a['00201209'] || {}).Value ? parseInt(a['00201209'].Value[0], 10) : 0;
          var nb = (b['00201209'] || {}).Value ? parseInt(b['00201209'].Value[0], 10) : 0;
          return nb - na;
        });

        var list = volumetric.length ? volumetric : (series || []);
        if (!list.length) { showError('No series found in this study.'); return; }
        sel.innerHTML = '';
        list.forEach(function (s, i) {
          var uid = s['0020000E'].Value[0];
          var mod = (s['00080060'] || {}).Value ? s['00080060'].Value[0] : '?';
          var n = (s['00201209'] || {}).Value ? s['00201209'].Value[0] : '?';
          var desc = (s['0008103E'] || {}).Value ? s['0008103E'].Value[0] : '';
          var opt = document.createElement('option');
          opt.value = uid;
          opt.textContent = 'Series ' + (i + 1) + ' · ' + mod + ' (' + n + ' imgs 3D)' + (desc ? ' · ' + desc.slice(0, 20) : '');
          sel.appendChild(opt);
        });
        sel.addEventListener('change', function () { loadVolume(sel.value); });
        loadVolume(list[0]['0020000E'].Value[0]);
      })
      .catch(function (e) { showError('Cannot load study series: ' + e.message); });
  }

  function loadVolume(seriesUid) {
    state.seriesUid = seriesUid;
    state.busy = true;

    // Check instant in-memory cache
    if (state.volCache[seriesUid]) {
      var cached = state.volCache[seriesUid];
      state.info = cached.info;
      state.vol = cached.vol;
      state.slices = cached.info.slices; state.rows = cached.info.rows; state.cols = cached.info.columns;
      state.spacing = cached.info.pixel_spacing || [1, 1];
      state.sliceSpacing = cached.info.slice_spacing || cached.info.slice_thickness || 1;
      state.wl.c = cached.info.window_center != null ? cached.info.window_center : 40;
      state.wl.w = cached.info.window_width != null ? cached.info.window_width : 400;
      state.idx.axial = Math.floor(state.slices / 2);
      state.idx.sagittal = Math.floor(state.cols / 2);
      state.idx.coronal = Math.floor(state.rows / 2);

      var modBadge = document.getElementById('mpr-modality');
      if (modBadge) modBadge.textContent = (cached.info.modality || 'CT') + ' · ' + cached.info.slices + ' slices';

      releaseGl2();
      initGl2();
      initViewports();
      init3dEngine();
      renderAll();
      state.busy = false;
      var sub = document.getElementById('mpr-sub');
      if (sub) sub.textContent = state.slices + '×' + state.rows + '×' + state.cols + ' · ' + state.sliceSpacing.toFixed(2) + 'mm (instant cached)';
      return;
    }

    setProgress(0.1);

    fetchJson('/api/v1/volume/' + encodeURIComponent(seriesUid) + '/info')
      .then(function (info) {
        if (!info || !info.slices) throw new Error('Series has no readable volume.');
        state.info = info;
        state.slices = info.slices; state.rows = info.rows; state.cols = info.columns;
        state.spacing = info.pixel_spacing || [1, 1];
        state.sliceSpacing = info.slice_spacing || info.slice_thickness || 1;
        state.wl.c = info.window_center != null ? info.window_center : 40;
        state.wl.w = info.window_width != null ? info.window_width : 400;
        state.idx.axial = Math.floor(state.slices / 2);
        state.idx.sagittal = Math.floor(state.cols / 2);
        state.idx.coronal = Math.floor(state.rows / 2);

        var modBadge = document.getElementById('mpr-modality');
        if (modBadge) modBadge.textContent = (info.modality || 'CT') + ' · ' + info.slices + ' slices';

        if (state.mode === 'server') {
          // Thin-client fallback: server reslices every plane
          setProgress(1);
          initViewports();
          init3dEngine();
          renderServerPlanes();
          state.busy = false;
          setTimeout(function () { setProgress(0); }, 300);
          var subS = document.getElementById('mpr-sub');
          if (subS) subS.textContent = state.slices + '×' + state.rows + '×' + state.cols + ' · server reslice';
          return null;
        }

        // Progressive Loading: For large volumes (>= 16 slices), fetch coarse downsampled preview first (8x smaller, arrives in <20ms)
        var isLargeVolume = info.slices >= 16;
        if (isLargeVolume) {
          setProgress(0.2);
          var subP = document.getElementById('mpr-sub');
          if (subP) subP.textContent = '⚡ Streaming coarse preview…';

          // Fetch downsampled coarse preview first
          fetch('/api/v1/volume/' + encodeURIComponent(seriesUid) + '/data?downsample=2')
            .then(function (r) {
              if (!r.ok) return null;
              return r.arrayBuffer().then(function (buf) {
                // Strided reads yield ceil(n/step) samples — the same helper
                // the backend uses, so the decoded buffer has the expected
                // length for odd dimensions too (H13).
                var downRows = geo().stridedDim(info.rows, 2);
                var downCols = geo().stridedDim(info.columns, 2);
                var downSlices = geo().stridedDim(info.slices, 2);
                var coarseInfo = Object.assign({}, info, {
                  rows: downRows,
                  columns: downCols,
                  slices: downSlices
                });
                return decodeVolumeAsync(coarseInfo, buf);
              });
            })
            .then(function (coarseDecoded) {
              if (!coarseDecoded || state.seriesUid !== seriesUid) return;
              if (!state.volCache[seriesUid]) {
                state.vol = coarseDecoded.vol;
                state.slices = geo().stridedDim(info.slices, 2);
                state.rows = geo().stridedDim(info.rows, 2);
                state.cols = geo().stridedDim(info.columns, 2);
                // NOTE: integer striding does not rescale spacing exactly when
                // the dimension is odd (e.g. 5 -> 3 samples). The x2 factor is
                // the nominal preview spacing; the full-resolution pass
                // replaces these values a moment later.
                state.sliceSpacing = (info.slice_spacing || info.slice_thickness || 1) * 2;
                state.spacing = [(info.pixel_spacing ? info.pixel_spacing[0] : 1) * 2, (info.pixel_spacing ? info.pixel_spacing[1] : 1) * 2];
                state.idx.axial = Math.floor(state.slices / 2);
                state.idx.sagittal = Math.floor(state.cols / 2);
                state.idx.coronal = Math.floor(state.rows / 2);

                releaseGl2();
                initGl2();
                initViewports();
                init3dEngine();
                renderAll();
                var subPreview = document.getElementById('mpr-sub');
                if (subPreview) subPreview.innerHTML = '<span style="color:#fbbf24;font-weight:700;">⚡ FAST PREVIEW</span> (Loading full HD in background…)';
              }
            })
            .catch(function () {});
        }

        setProgress(0.4);
        return fetch('/api/v1/volume/' + encodeURIComponent(seriesUid) + '/data').then(function (r) {
          if (!r.ok) throw new Error('Volume stream failed: HTTP ' + r.status);
          return r.arrayBuffer().then(function (buf) { return { info: info, buf: buf }; });
        });
      })
      .then(function (res) {
        if (!res) return;
        setProgress(0.75);
        return decodeVolumeAsync(res.info, res.buf).then(function (decoded) {
          return { info: res.info, decoded: decoded };
        });
      })
      .then(function (payload) {
        if (!payload || !payload.decoded || state.seriesUid !== seriesUid) return;
        var info = payload.info;
        var decoded = payload.decoded;
        state.info = info;
        state.vol = decoded.vol;
        state.slices = info.slices;
        state.rows = info.rows;
        state.cols = info.columns;
        state.spacing = info.pixel_spacing || [1, 1];
        state.sliceSpacing = info.slice_spacing || info.slice_thickness || 1;
        state.idx.axial = Math.floor(state.slices / 2);
        state.idx.sagittal = Math.floor(state.cols / 2);
        state.idx.coronal = Math.floor(state.rows / 2);

        // Cache cap: keep at most 3 large volumetric datasets in memory
        // (~500MB). Eviction is insertion-order (oldest key first) — the
        // Object.keys() order, not access order.
        var cacheKeys = Object.keys(state.volCache);
        if (cacheKeys.length >= 3) {
          var oldestKey = cacheKeys[0];
          delete state.volCache[oldestKey];
        }
        state.volCache[seriesUid] = { info: state.info, vol: state.vol };

        setProgress(0.95);
        releaseGl2();
        initGl2();
        initViewports();
        init3dEngine();
        renderAll();
        setProgress(1);
        state.busy = false;
        setTimeout(function () { setProgress(0); }, 300);
        var sub = document.getElementById('mpr-sub');
        if (sub) sub.innerHTML = state.slices + '×' + state.rows + '×' + state.cols + ' · ' + state.sliceSpacing.toFixed(2) + 'mm · <span style="color:#22d3ee;font-weight:700;">✦ ULTRA HD (Streamed)</span>';
      })
      .catch(function (e) { state.busy = false; showError(e.message); });
  }

  // -------------------------------------------------------------------------
  // Web Worker pool — raw little-endian voxel unpacking.
  //
  // `/api/v1/volume/{series}/data` always streams *decoded* voxels in the
  // dtype reported by /info (volume_service.iter_volume_bytes calls
  // pixel_array), so no compressed transfer syntax ever reaches this code.
  // Each request carries a requestId and settles exactly once: on success, on
  // worker error, on decode timeout, or when the workspace closes
  // (CancelledError). A worker that dies mid-decode therefore rejects its
  // pending promise instead of leaving the viewer hanging (H15).
  // -------------------------------------------------------------------------
  var workerPool = [];
  var workerPoolSize = Math.max(2, Math.min(8, (navigator.hardwareConcurrency || 4)));
  var workerRoundRobin = 0;
  var workerCallbacks = {};
  var workerReqSeq = 0;
  var workerBlobUrl = null;
  var WORKER_DECODE_TIMEOUT_MS = (typeof window.__paxsMprDecodeTimeoutMs === 'number')
    ? window.__paxsMprDecodeTimeoutMs
    : 30000;

  function cancelledError(message) {
    var err = new Error(message || 'volume decode cancelled');
    err.name = 'CancelledError';
    return err;
  }

  /** Settle one pending request exactly once (idempotent). */
  function settleWorkerRequest(reqId, error, result) {
    var pending = workerCallbacks[reqId];
    if (!pending) return false;
    delete workerCallbacks[reqId];
    if (pending.timer) clearTimeout(pending.timer);
    if (error) pending.reject(error);
    else pending.resolve(result);
    return true;
  }

  /** Reject every pending decode (workspace closed, or worker died). */
  function cancelPendingDecodes(message, worker) {
    Object.keys(workerCallbacks).forEach(function (key) {
      var pending = workerCallbacks[key];
      if (worker && pending.worker !== worker) return;
      settleWorkerRequest(key, cancelledError(message));
    });
  }

  /** Tear the pool down completely (close): reject, terminate, revoke. */
  function shutdownWorkerPool(message) {
    cancelPendingDecodes(message || 'MPR workspace closed');
    workerPool.forEach(function (worker) {
      try { worker.terminate(); } catch (e) {}
    });
    workerPool = [];
    workerRoundRobin = 0;
    if (workerBlobUrl) {
      try { URL.revokeObjectURL(workerBlobUrl); } catch (e) {}
      workerBlobUrl = null;
    }
  }

  function initWorkerPool() {
    if (workerPool.length || !window.Worker) return;
    try {
      var workerCode = [
        'self.onmessage = function(e) {',
        '  var reqId = e.data.reqId;',
        '  var info = e.data.info;',
        '  var buf = e.data.buf;',
        '  var count = info.slices * info.rows * info.columns;',
        '  var slope = info.rescale_slope != null ? info.rescale_slope : 1;',
        '  var intercept = info.rescale_intercept != null ? info.rescale_intercept : 0;',
        '  var dtype = info.dtype || "uint16";',
        '  var vol;',
        '  var i;',
        '',
        '  if (dtype === "float32") {',
        '    vol = new Float32Array(buf, 0, count);',
        '  } else if (dtype === "int16") {',
        '    var i16 = new Int16Array(buf, 0, count);',
        '    vol = new Float32Array(count);',
        '    if (slope === 1 && intercept === 0) {',
        '      for (i = 0; i < count; i++) vol[i] = i16[i];',
        '    } else {',
        '      for (i = 0; i < count; i++) vol[i] = i16[i] * slope + intercept;',
        '    }',
        '  } else if (dtype === "uint16") {',
        '    var u16 = new Uint16Array(buf, 0, count);',
        '    vol = new Float32Array(count);',
        '    if (slope === 1 && intercept === 0) {',
        '      for (i = 0; i < count; i++) vol[i] = u16[i];',
        '    } else {',
        '      for (i = 0; i < count; i++) vol[i] = u16[i] * slope + intercept;',
        '    }',
        '  } else {',
        '    var u8 = new Uint8Array(buf, 0, count);',
        '    vol = new Float32Array(count);',
        '    if (slope === 1 && intercept === 0) {',
        '      for (i = 0; i < count; i++) vol[i] = u8[i];',
        '    } else {',
        '      for (i = 0; i < count; i++) vol[i] = u8[i] * slope + intercept;',
        '    }',
        '  }',
        '  self.postMessage({ reqId: reqId, vol: vol }, [vol.buffer]);',
        '};'
      ].join('\n');
      var blob = new Blob([workerCode], { type: 'application/javascript' });
      workerBlobUrl = URL.createObjectURL(blob);
      for (var w = 0; w < workerPoolSize; w++) {
        var worker = new Worker(workerBlobUrl);
        worker.onmessage = function (ev) {
          settleWorkerRequest(ev.data.reqId, null, { vol: ev.data.vol });
        };
        worker.onerror = function (err) {
          // A worker that fails mid-decode must not leave its callers hanging:
          // reject every request dispatched to THIS worker (H15).
          var failed = this;
          console.warn('PAXS Worker Pool error:', err);
          Object.keys(workerCallbacks).forEach(function (key) {
            if (workerCallbacks[key].worker === failed) {
              settleWorkerRequest(key, new Error('volume decode worker failed'));
            }
          });
        };
        workerPool.push(worker);
      }
    } catch (e) {
      console.warn('PAXS Worker Pool creation failed:', e);
    }
  }

  function decodeVolumeAsync(info, buf) {
    return new Promise(function (resolve, reject) {
      initWorkerPool();
      if (workerPool.length) {
        var reqId = ++workerReqSeq;
        var worker = workerPool[workerRoundRobin % workerPool.length];
        workerRoundRobin++;
        // One entry per in-flight decode, with its own timeout so a lost
        // worker message can never leave `state.busy` stuck forever (H15).
        workerCallbacks[reqId] = {
          resolve: resolve,
          reject: reject,
          worker: worker,
          timer: setTimeout(function () {
            settleWorkerRequest(reqId, new Error('volume decode timed out'));
          }, WORKER_DECODE_TIMEOUT_MS)
        };
        try {
          worker.postMessage({ reqId: reqId, info: info, buf: buf }, [buf]);
        } catch (e) {
          settleWorkerRequest(reqId, e);
        }
        return;
      }
      // No Worker support — decode synchronously on the main thread.
      try {
        resolve(decodeVolume(info, buf));
      } catch (e) {
        reject(e);
      }
    });
  }

  function decodeVolume(info, buf) {
    var count = info.slices * info.rows * info.columns;
    var slope = info.rescale_slope != null ? info.rescale_slope : 1;
    var intercept = info.rescale_intercept != null ? info.rescale_intercept : 0;
    var dtype = info.dtype || 'uint16';
    var vol;
    var i;

    // Fast-path: Direct typed array instantiation over ArrayBuffer slice (10x faster than byte-by-byte DataView loop)
    if (dtype === 'float32') {
      vol = new Float32Array(buf, 0, count);
    } else if (dtype === 'int16') {
      var i16 = new Int16Array(buf, 0, count);
      vol = new Float32Array(count);
      if (slope === 1 && intercept === 0) {
        for (i = 0; i < count; i++) vol[i] = i16[i];
      } else {
        for (i = 0; i < count; i++) vol[i] = i16[i] * slope + intercept;
      }
      return { vol: vol };
    } else if (dtype === 'uint16') {
      var u16 = new Uint16Array(buf, 0, count);
      vol = new Float32Array(count);
      if (slope === 1 && intercept === 0) {
        for (i = 0; i < count; i++) vol[i] = u16[i];
      } else {
        for (i = 0; i < count; i++) vol[i] = u16[i] * slope + intercept;
      }
      return { vol: vol };
    } else {
      var u8 = new Uint8Array(buf, 0, count);
      vol = new Float32Array(count);
      if (slope === 1 && intercept === 0) {
        for (i = 0; i < count; i++) vol[i] = u8[i];
      } else {
        for (i = 0; i < count; i++) vol[i] = u8[i] * slope + intercept;
      }
      return { vol: vol };
    }

    if (slope !== 1 || intercept !== 0) {
      for (i = 0; i < count; i++) vol[i] = vol[i] * slope + intercept;
    }
    return { vol: vol };
  }

  function planeAxial(z) {
    var n = state.rows * state.cols;
    if (state.projectionMode === 'normal' || state.slabThickness <= 1) {
      // u = i (X, cols), v = j (Y, rows) — row-major [j][i].
      return geo().extractPlane(state.vol, 'axial', z, planeMeta('axial'));
    }
    var half = Math.max(1, Math.floor((state.slabThickness / Math.max(state.sliceSpacing, 0.1)) / 2));
    var zStart = Math.max(0, z - half);
    var zEnd = Math.min(state.slices - 1, z + half);
    var out = new Float32Array(n);
    var mode = state.projectionMode;
    var i, sz;
    if (mode === 'mip') {
      out.fill(-10000);
      for (sz = zStart; sz <= zEnd; sz++) {
        var sub = state.vol.subarray(sz * n, (sz + 1) * n);
        for (i = 0; i < n; i++) if (sub[i] > out[i]) out[i] = sub[i];
      }
    } else if (mode === 'minip') {
      out.fill(10000);
      for (sz = zStart; sz <= zEnd; sz++) {
        var sub = state.vol.subarray(sz * n, (sz + 1) * n);
        for (i = 0; i < n; i++) if (sub[i] < out[i]) out[i] = sub[i];
      }
    } else if (mode === 'avgip') {
      var count = zEnd - zStart + 1;
      for (sz = zStart; sz <= zEnd; sz++) {
        var sub = state.vol.subarray(sz * n, (sz + 1) * n);
        for (i = 0; i < n; i++) out[i] += sub[i];
      }
      for (i = 0; i < n; i++) out[i] /= count;
    }
    return out;
  }

  // u = j (Y, rows), v = k (Z, slices) — row-major [k][j].
  function planeSagittal(x) {
    return geo().extractPlane(state.vol, 'sagittal', x, planeMeta('sagittal'));
  }

  // u = i (X, cols), v = k (Z, slices) — row-major [k][i].
  function planeCoronal(y) {
    return geo().extractPlane(state.vol, 'coronal', y, planeMeta('coronal'));
  }

  var vps = {
    axial: { cv: 'mpr-axial-cv', wrap: 'mpr-axial-wrap', slider: 'mpr-axial-slider', hud: 'mpr-axial-hud', w: 0, h: 0 },
    sagittal: { cv: 'mpr-sag-cv', wrap: 'mpr-sag-wrap', slider: 'mpr-sag-slider', hud: 'mpr-sag-hud', w: 0, h: 0 },
    coronal: { cv: 'mpr-cor-cv', wrap: 'mpr-cor-wrap', slider: 'mpr-cor-slider', hud: 'mpr-cor-hud', w: 0, h: 0 }
  };

  function initViewports() {
    ['axial', 'sagittal', 'coronal'].forEach(function (plane) {
      var vp = vps[plane];
      vp.w = plane === 'axial' ? state.cols : (plane === 'sagittal' ? state.rows : state.cols);
      vp.h = plane === 'axial' ? state.rows : (plane === 'sagittal' ? state.slices : state.slices);
      var cv = document.getElementById(vp.cv);
      if (!cv) return;
      vp.ctx = cv.getContext('2d');
      setupPointer(vp, plane);
      var slider = document.getElementById(vp.slider);
      slider.min = 0;
      slider.max = (plane === 'axial' ? state.slices : (plane === 'sagittal' ? state.cols : state.rows)) - 1;
      slider.value = state.idx[plane];
      slider.oninput = function () {
        beginInteraction();
        state.idx[plane] = parseInt(slider.value, 10);
        scheduleRender();
        endInteraction(120);
      };
    });
  }

  function draw2D(vp, data) {
    var cv = document.getElementById(vp.cv);
    if (!cv || !vp.ctx) return;
    var rect = cv.getBoundingClientRect();
    if (cv.width !== rect.width || cv.height !== rect.height) {
      cv.width = rect.width; cv.height = rect.height;
    }
    var imgData = vp.ctx.createImageData(vp.w, vp.h);
    var px = imgData.data;
    var lo = state.wl.c - state.wl.w / 2;
    var iw = Math.max(state.wl.w, 1);
    for (var i = 0; i < vp.w * vp.h; i++) {
      var t = Math.max(0, Math.min(1, (data[i] - lo) / iw));
      var v = (t * 255) | 0;
      px[i * 4] = v; px[i * 4 + 1] = v; px[i * 4 + 2] = v; px[i * 4 + 3] = 255;
    }
    var tempCv = document.createElement('canvas');
    tempCv.width = vp.w; tempCv.height = vp.h;
    tempCv.getContext('2d').putImageData(imgData, 0, 0);
    vp.ctx.imageSmoothingEnabled = true;
    vp.ctx.drawImage(tempCv, 0, 0, cv.width, cv.height);
  }

  // ---------------------------------------------------------------------- //
  // WebGL2 3D-texture MPR renderer (client-side GPU slicing).
  //
  // The volume is uploaded once as an R32F 3D texture; every orthogonal plane
  // is rendered by a fragment shader that samples the volume at the slice
  // position and applies window/level in-shader. Slab projections (MIP /
  // MINIP / average) run as a shader loop along the plane's normal axis.
  // Rendering happens on one hidden canvas whose contents are blitted into
  // the per-plane viewport canvases (2D), which the Three.js 3D viewport then
  // reuses as its cutplane textures. Falls back to the CPU per-pixel path
  // when WebGL2 / 3D textures are unavailable.
  // ---------------------------------------------------------------------- //
  var GL2_VS = [
    '#version 300 es',
    'out vec2 v_uv;',
    'void main() {',
    '  vec2 p;',
    '  if (gl_VertexID == 0) p = vec2(-1.0, -1.0);',
    '  else if (gl_VertexID == 1) p = vec2(3.0, -1.0);',
    '  else p = vec2(-1.0, 3.0);',
    '  v_uv = p * 0.5 + 0.5;',
    '  gl_Position = vec4(p, 0.0, 1.0);',
    '}'
  ].join('\n');

  var GL2_FS = [
    '#version 300 es',
    'precision highp float;',
    'precision highp sampler3D;',
    'uniform sampler3D u_vol;',
    'uniform int u_axis;',       // 0 = z (axial), 1 = x (sagittal), 2 = y (coronal)
    'uniform float u_pos;',      // normalized slice position [0,1]
    'uniform float u_wlC;',
    'uniform float u_wlW;',
    'uniform float u_slab;',     // mm (0 = single slice)
    'uniform float u_spacing;',  // mm per step along the axis
    'uniform int u_count;',      // number of slices along the axis
    'uniform int u_mode;',       // 0 normal, 1 mip, 2 minip, 3 average
    'uniform int u_lod;',        // 1 = normal crisp pass, 2 = fast half-stride LOD
    'in vec2 v_uv;',
    'out vec4 outColor;',
    'float at(vec3 uvw) { return texture(u_vol, clamp(uvw, 0.0, 1.0)).r; }',
    'void main() {',
    '  vec3 base;',
    // Volume texture is (x = i/cols, y = j/rows, z = k/slices); the u/v axes
    // follow the canonical convention (axial u=i v=j, sagittal u=j v=k,
    // coronal u=i v=k), so v_uv.x is always the u axis and v_uv.y the v axis.
    '  if (u_axis == 0) base = vec3(v_uv.x, v_uv.y, u_pos);',
    '  else if (u_axis == 1) base = vec3(u_pos, v_uv.x, v_uv.y);',
    '  else base = vec3(v_uv.x, u_pos, v_uv.y);',
    '  float val;',
    '  if (u_mode == 0 || u_slab <= 0.0) {',
    '    val = at(base);',
    '  } else {',
    '    int n = max(1, int(floor((u_slab / max(u_spacing, 0.1)) / 2.0)));',
    '    float step = 1.0 / float(max(u_count, 1));',
    '    int stride = max(1, u_lod);',
    '    float sum = 0.0;',
    '    float mn = 1e9;',
    '    float mx = -1e9;',
    '    int samples = 0;',
    '    for (int i = -n; i <= n; i += stride) {',
    '      vec3 p = base;',
    '      float d = float(i) * step;',
    '      if (u_axis == 0) p.z = clamp(base.z + d, 0.0, 1.0);',
    '      else if (u_axis == 1) p.x = clamp(base.x + d, 0.0, 1.0);',
    '      else p.y = clamp(base.y + d, 0.0, 1.0);',
    '      float s = at(p);',
    '      sum += s;',
    '      mn = min(mn, s);',
    '      mx = max(mx, s);',
    '      samples++;',
    '    }',
    '    if (u_mode == 1) val = mx;',
    '    else if (u_mode == 2) val = mn;',
    '    else val = sum / max(float(samples), 1.0);',
    '  }',
    '  float lo = u_wlC - u_wlW / 2.0;',
    '  float t = clamp((val - lo) / max(u_wlW, 1.0), 0.0, 1.0);',
    '  outColor = vec4(t, t, t, 1.0);',
    '}'
  ].join('\n');

  function initGl2() {
    if (state.mode !== 'gl' || !state.vol || state.gl2.supported) return;
    var maxDim = Math.max(state.cols, state.rows, state.slices);
    // Support large volumetric studies up to 1024 dimension and 256M voxels (1 GB VRAM)
    if (maxDim > 1024 || state.slices * state.rows * state.cols > 256 * 1024 * 1024) return;

    var renderCv = document.createElement('canvas');
    renderCv.width = Math.max(state.cols, state.rows);
    renderCv.height = Math.max(state.rows, state.slices);
    var gl = renderCv.getContext('webgl2', {
      antialias: false,
      depth: false,
      alpha: false,
      premultipliedAlpha: true,
      powerPreference: 'high-performance'
    });
    if (!gl) return;
    
    var max3D = gl.getParameter(gl.MAX_3D_TEXTURE_SIZE);
    if (maxDim > max3D) {
      console.warn('PAXS MPR: Max 3D Texture size exceeded (' + maxDim + ' > ' + max3D + ')');
      return;
    }

    // Enable floating point texture linear filtering extension if available for buttery smooth trilinear MPR reslicing
    var extFloatLinear = gl.getExtension('OES_texture_float_linear') || gl.getExtension('EXT_color_buffer_float');
    var filterMode = extFloatLinear ? gl.LINEAR : gl.NEAREST;

    var tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_3D, tex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, filterMode);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, filterMode);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    
    // Allocate R32F 3D Texture on GPU
    gl.texImage3D(
      gl.TEXTURE_3D,
      0,
      gl.R32F,
      state.cols,
      state.rows,
      state.slices,
      0,
      gl.RED,
      gl.FLOAT,
      state.vol
    );

    if (gl.getError() !== 0) {
      console.warn('PAXS MPR: 3D Float texture upload failed — falling back to CPU rendering');
      return;
    }

    var vs = gl.createShader(gl.VERTEX_SHADER);
    gl.shaderSource(vs, GL2_VS); gl.compileShader(vs);
    var fs = gl.createShader(gl.FRAGMENT_SHADER);
    gl.shaderSource(fs, GL2_FS); gl.compileShader(fs);
    var prog = gl.createProgram();
    gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      console.warn('PAXS MPR: WebGL2 program failed to link', gl.getProgramInfoLog(prog));
      return;
    }
    var vao = gl.createVertexArray();
    gl.bindVertexArray(vao);

    state.gl2 = {
      supported: true,
      ctx: gl,
      renderCv: renderCv,
      volTex: tex,
      prog: prog,
      vao: vao,
      uLoc: {
        vol: gl.getUniformLocation(prog, 'u_vol'),
        axis: gl.getUniformLocation(prog, 'u_axis'),
        pos: gl.getUniformLocation(prog, 'u_pos'),
        wlC: gl.getUniformLocation(prog, 'u_wlC'),
        wlW: gl.getUniformLocation(prog, 'u_wlW'),
        slab: gl.getUniformLocation(prog, 'u_slab'),
        spacing: gl.getUniformLocation(prog, 'u_spacing'),
        count: gl.getUniformLocation(prog, 'u_count'),
        mode: gl.getUniformLocation(prog, 'u_mode'),
        lod: gl.getUniformLocation(prog, 'u_lod')
      }
    };
  }

  function releaseGl2() {
    if (state.gl2 && state.gl2.ctx) {
      try {
        var ext = state.gl2.ctx.getExtension('WEBGL_lose_context');
        if (ext) ext.loseContext();
      } catch (e) {}
    }
    state.gl2 = { supported: false, ctx: null, renderCv: null, volTex: null, prog: null, vao: null, uLoc: null };
  }

  function drawPlaneGl(plane) {
    var g = state.gl2;
    if (!g.supported || !g.ctx) return;
    var vp = vps[plane];
    var gl = g.ctx;
    var isLod = state.lod.enabled && state.lod.active;
    var pw = isLod ? Math.max(128, Math.round(vp.w * state.lod.scale)) : vp.w;
    var ph = isLod ? Math.max(128, Math.round(vp.h * state.lod.scale)) : vp.h;
    var count = plane === 'axial' ? state.slices : (plane === 'sagittal' ? state.cols : state.rows);
    gl.viewport(0, 0, pw, ph);
    gl.useProgram(g.prog);
    gl.bindVertexArray(g.vao);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_3D, g.volTex);
    gl.uniform1i(g.uLoc.vol, 0);
    gl.uniform1i(g.uLoc.axis, plane === 'axial' ? 0 : (plane === 'sagittal' ? 1 : 2));
    gl.uniform1f(g.uLoc.pos, count <= 1 ? 0.5 : state.idx[plane] / (count - 1));
    gl.uniform1f(g.uLoc.wlC, state.wl.c);
    gl.uniform1f(g.uLoc.wlW, state.wl.w);
    gl.uniform1f(g.uLoc.slab, state.projectionMode === 'normal' ? 0 : state.slabThickness);
    // Slab stepping runs along the plane normal -> the normal-axis spacing.
    gl.uniform1f(g.uLoc.spacing, normalSpacing(plane));
    gl.uniform1i(g.uLoc.count, count);
    gl.uniform1i(g.uLoc.mode, state.projectionMode === 'mip' ? 1 : (state.projectionMode === 'minip' ? 2 : (state.projectionMode === 'avgip' ? 3 : 0)));
    gl.uniform1i(g.uLoc.lod, isLod ? 2 : 1);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    var cv = document.getElementById(vp.cv);
    if (!cv) return;
    var rect = cv.getBoundingClientRect();
    var dw = Math.max(1, Math.round(rect.width));
    var dh = Math.max(1, Math.round(rect.height));
    if (cv.width !== dw || cv.height !== dh) { cv.width = dw; cv.height = dh; }
    if (!vp.ctx) vp.ctx = cv.getContext('2d');
    vp.ctx.imageSmoothingEnabled = isLod;
    vp.ctx.drawImage(g.renderCv, 0, 0, pw, ph, 0, 0, dw, dh);
    if (state.fusion.enabled && state.fusion.overlayVol) {
      drawFusionOverlay(vp, plane, dw, dh);
    }
  }

  function renderPlane(vp, plane, data) {
    if (state.mode === 'gl' && state.gl2.supported && state.gl2.ctx) drawPlaneGl(plane);
    else {
      draw2D(vp, data);
      if (state.fusion.enabled && state.fusion.overlayVol) {
        var cv = document.getElementById(vp.cv);
        if (cv) drawFusionOverlay(vp, plane, cv.width, cv.height);
      }
    }
  }

  function drawFusionOverlay(vp, plane, dw, dh) {
    var fVol = state.fusion.overlayVol;
    var fInfo = state.fusion.overlayInfo;
    if (!fVol || !fInfo || !vp.ctx) return;

    // The overlay volume was fetched with ?downsample=2 -> ceil(n/2) samples.
    var fRows = geo().stridedDim(fInfo.rows, 2) || state.rows;
    var fCols = geo().stridedDim(fInfo.columns, 2) || state.cols;
    var fSlices = geo().stridedDim(fInfo.slices, 2) || state.slices;

    // Plane geometry of the OVERLAY volume in the canonical convention:
    //   axial    -> u = i (cols), v = j (rows)
    //   sagittal -> u = j (rows), v = k (slices)
    //   coronal  -> u = i (cols), v = k (slices)
    // The overlay is stretched over the base plane's rectangle by the caller,
    // so its own plane image must use its own u/v extents.
    var imgW = plane === 'sagittal' ? fRows : fCols;
    var imgH = plane === 'axial' ? fRows : fSlices;

    var offCv = document.createElement('canvas');
    offCv.width = imgW; offCv.height = imgH;
    var offCtx = offCv.getContext('2d');
    var imgData = offCtx.createImageData(imgW, imgH);
    var d = imgData.data;

    var count = plane === 'axial' ? state.slices : (plane === 'sagittal' ? state.cols : state.rows);
    var normPos = count <= 1 ? 0.5 : state.idx[plane] / (count - 1);
    var cmap = state.fusion.colormap || 'hot_iron';
    var opacity = state.fusion.opacity || 0.5;

    // Index along the plane's constant (normal) axis.
    var fZ = Math.min(fSlices - 1, Math.floor(normPos * (fSlices - 1)));
    var fY = Math.min(fRows - 1, Math.floor(normPos * (fRows - 1)));
    var fX = Math.min(fCols - 1, Math.floor(normPos * (fCols - 1)));

    for (var r = 0; r < imgH; r++) {
      for (var c = 0; c < imgW; c++) {
        var idx4 = (r * imgW + c) * 4;
        // c is the u index, r the v index; the normal axis is fZ / fX / fY.
        var hu = 0;
        if (plane === 'axial') hu = fVol[fZ * fRows * fCols + r * fCols + c];
        else if (plane === 'sagittal') hu = fVol[r * fRows * fCols + c * fCols + fX];
        else hu = fVol[r * fRows * fCols + fY * fCols + c];

        var t = Math.max(0, Math.min(1, (hu - 50) / 400));
        if (t > 0.04) {
          var rgb = sampleColormap(t, cmap);
          d[idx4] = rgb[0];
          d[idx4 + 1] = rgb[1];
          d[idx4 + 2] = rgb[2];
          d[idx4 + 3] = Math.round(t * opacity * 255);
        } else {
          d[idx4 + 3] = 0;
        }
      }
    }
    offCtx.putImageData(imgData, 0, 0);
    vp.ctx.drawImage(offCv, 0, 0, dw, dh);
  }

  function sampleColormap(t, cmap) {
    if (cmap === 'rainbow') {
      var h = (1.0 - t) * 240.0;
      var s = 1.0, v = 1.0;
      var hi = Math.floor(h / 60) % 6;
      var f = h / 60 - Math.floor(h / 60);
      var p = v * (1 - s), q = v * (1 - f * s), tt = v * (1 - (1 - f) * s);
      var r = 0, g = 0, b = 0;
      if (hi === 0) { r = v; g = tt; b = p; }
      else if (hi === 1) { r = q; g = v; b = p; }
      else if (hi === 2) { r = p; g = v; b = tt; }
      else if (hi === 3) { r = p; g = q; b = v; }
      else if (hi === 4) { r = tt; g = p; b = v; }
      else { r = v; g = p; b = q; }
      return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
    } else if (cmap === 'pet_spectrum') {
      var pr = Math.sin(t * Math.PI);
      var pg = t * t;
      var pb = (1.0 - t) * 0.4;
      return [Math.round(pr * 255), Math.round(pg * 255), Math.round(pb * 255)];
    } else if (cmap === 'cold_fire') {
      var cr = t < 0.5 ? t * 0.2 : (t - 0.5) * 2.0;
      var cg = t < 0.5 ? t * 1.6 : 0.8 + (t - 0.5) * 0.4;
      var cb = t < 0.5 ? 1.0 : (1.0 - (t - 0.5) * 2.0);
      return [Math.round(cr * 255), Math.round(cg * 255), Math.round(cb * 255)];
    } else { // hot_iron
      var hr = Math.min(1, t * 3.0);
      var hg = Math.max(0, Math.min(1, t * 3.0 - 1.0));
      var hb = Math.max(0, Math.min(1, t * 3.0 - 2.0));
      return [Math.round(hr * 255), Math.round(hg * 255), Math.round(hb * 255)];
    }
  }

  // ---- thin-client server-reslice mode (no WebGL / no volume in memory) ---
  function serverPlane(plane) {
    var vp = vps[plane];
    var cache = state.serverCache[plane];
    var idx = state.idx[plane];
    var key = idx + '|' + Math.round(state.wl.c) + '|' + Math.round(state.wl.w);
    if (cache.key === key && cache.img) { drawServerImage(vp, cache.img); return; }
    var url = '/api/v1/volume/' + encodeURIComponent(state.seriesUid) + '/mpr/slice' +
      '?plane=' + plane + '&index=' + idx +
      '&window_center=' + state.wl.c + '&window_width=' + state.wl.w;
    var img = new Image();
    img.onload = function () {
      cache.key = key; cache.img = img;
      drawServerImage(vp, img);
    };
    img.src = url;
  }

  function drawServerImage(vp, img) {
    var cv = document.getElementById(vp.cv);
    if (!cv) return;
    var rect = cv.getBoundingClientRect();
    var dw = Math.max(1, Math.round(rect.width));
    var dh = Math.max(1, Math.round(rect.height));
    if (cv.width !== dw || cv.height !== dh) { cv.width = dw; cv.height = dh; }
    if (!vp.ctx) vp.ctx = cv.getContext('2d');
    vp.ctx.drawImage(img, 0, 0, dw, dh);
  }

  function renderServerPlanes() {
    ['axial', 'sagittal', 'coronal'].forEach(function (p) {
      serverPlane(p);
      drawCrosshair(p);
      updateHud(p);
    });
    update3dPlanes();
  }

  function beginInteraction() {
    state.isInteracting = true;
    if (state.lod.enabled) {
      state.lod.active = true;
    }
    if (state.lod.timer) {
      clearTimeout(state.lod.timer);
      state.lod.timer = null;
    }
  }

  function endInteraction(delayMs) {
    if (state.lod.timer) clearTimeout(state.lod.timer);
    state.lod.timer = setTimeout(function () {
      state.isInteracting = false;
      state.lod.active = false;
      state.lod.timer = null;
      scheduleRender();
    }, delayMs || 100);
  }

  var renderPending = false;
  function scheduleRender() {
    if (renderPending) return;
    renderPending = true;
    requestAnimationFrame(function () {
      renderPending = false;
      renderAll();
    });
  }

  function renderAll() {
    if (state.mode === 'server') { renderServerPlanes(); return; }
    if (!state.vol) return;
    var planes = (state.mode === 'gl' && state.gl2.supported && state.gl2.ctx)
      ? null
      : {
          axial: planeAxial(state.idx.axial),
          sagittal: planeSagittal(state.idx.sagittal),
          coronal: planeCoronal(state.idx.coronal)
        };
    ['axial', 'sagittal', 'coronal'].forEach(function (p) {
      renderPlane(vps[p], p, planes ? planes[p] : null);
      drawCrosshair(p);
      drawSvgOverlays(p);
      updateHud(p);
    });
    update3dPlanes();
  }

  function updateHud(plane) {
    var el = document.getElementById(vps[plane].hud);
    if (!el) return;
    var isLod = state.lod.enabled && state.lod.active;
    var lodBadge = isLod ? ' <span style="color:#fbbf24;font-size:9px;font-weight:700;background:rgba(251,191,36,0.15);padding:1px 5px;border-radius:4px;border:1px solid rgba(251,191,36,0.3);">⚡ 60FPS LOD</span>' : '';
    el.innerHTML = plane.toUpperCase() + ' ' + state.idx[plane] + ' · W/L ' + Math.round(state.wl.w) + '/' + Math.round(state.wl.c) + lodBadge;

    // Update ACR Corner overlays
    var cbl = document.getElementById(vps[plane].cv.replace('-cv', '-cbl'));
    if (cbl) {
      var pos = (state.idx[plane] * normalSpacing(plane)).toFixed(1);
      cbl.innerHTML = 'POS: ' + pos + 'mm<br>THICK: ' + state.sliceSpacing.toFixed(1) + 'mm';
    }
    var cbr = document.getElementById(vps[plane].cv.replace('-cv', '-cbr'));
    if (cbr) {
      var qualText = isLod ? 'LOD 0.5x' : 'NATIVE 1.0x';
      cbr.innerHTML = 'W/L: ' + Math.round(state.wl.w) + '/' + Math.round(state.wl.c) + '<br>HU: ' + state.hoverHu[plane] + ' · ' + qualText;
    }
  }

  function drawCrosshair(plane) {
    var wrap = document.getElementById(vps[plane].wrap);
    if (!wrap) return;
    wrap.querySelectorAll('.mpr-line').forEach(function (l) { l.remove(); });
    if (state.activeTool !== 'crosshair') return;
    var dims = geo().planeDims(plane, planeMeta(plane));
    // The vertical line marks the u axis (i or j), the horizontal line the v
    // axis (j or k) — per the canonical convention at the top of this file.
    var uIndex, vIndex;
    if (plane === 'axial') { uIndex = state.idx.sagittal; vIndex = state.idx.coronal; }
    else if (plane === 'sagittal') { uIndex = state.idx.coronal; vIndex = state.idx.axial; }
    else { uIndex = state.idx.sagittal; vIndex = state.idx.axial; }
    var f = function (idx, n) { return (n <= 1 ? 0.5 : idx / (n - 1)) * 100 + '%'; };
    addLine(wrap, 'v', null, f(uIndex, dims.w));
    addLine(wrap, 'h', f(vIndex, dims.h), null);
  }

  function addLine(wrap, kind, top, left) {
    var line = document.createElement('div');
    line.className = 'mpr-line ' + kind;
    if (top != null) line.style.top = top;
    if (left != null) line.style.left = left;
    wrap.appendChild(line);
  }

  function drawSvgOverlays(plane) {
    var vp = vps[plane];
    var svg = document.getElementById(vp.id + '-svg');
    if (!svg) return;
    svg.innerHTML = '';
    var items = state.measurements.filter(function (m) { return m.plane === plane; });
    if (state.pendingTool && state.pendingTool.plane === plane) {
      items = items.concat([state.pendingTool]);
    }

    var rect = svg.getBoundingClientRect();
    var w = rect.width || vp.w;
    var h = rect.height || vp.h;
    var measureMeta = planeMeta(plane);

    items.forEach(function (m) {
      if (m.type === 'ruler' && m.points.length >= 2) {
        var p1 = m.points[0], p2 = m.points[1];
        var x1 = p1.fx * w, y1 = p1.fy * h;
        var x2 = p2.fx * w, y2 = p2.fy * h;
        var distMm = geo().computePhysicalLength(p1, p2, measureMeta);

        var line = '<line x1="' + x1 + '" y1="' + y1 + '" x2="' + x2 + '" y2="' + y2 + '" stroke="#34d399" stroke-width="2" stroke-linecap="round"/>' +
          '<circle cx="' + x1 + '" cy="' + y1 + '" r="3.5" fill="#34d399"/>' +
          '<circle cx="' + x2 + '" cy="' + y2 + '" r="3.5" fill="#34d399"/>' +
          '<rect x="' + ((x1 + x2) / 2 - 28) + '" y="' + ((y1 + y2) / 2 - 18) + '" width="56" height="15" rx="4" fill="rgba(3,7,18,0.85)" stroke="#34d399" stroke-width="1"/>' +
          '<text x="' + ((x1 + x2) / 2) + '" y="' + ((y1 + y2) / 2 - 7) + '" fill="#34d399" font-size="10" font-weight="700" text-anchor="middle" font-family="ui-monospace,monospace">' + distMm.toFixed(1) + ' mm</text>';
        svg.innerHTML += line;
      } else if (m.type === 'roi' && m.points.length >= 2) {
        var c = m.points[0], edge = m.points[1];
        var cx = c.fx * w, cy = c.fy * h;
        var rx = Math.abs(edge.fx - c.fx) * w;
        var ry = Math.abs(edge.fy - c.fy) * h;
        if (rx < 4) rx = 4;
        if (ry < 4) ry = 4;

        var areaCm2 = geo().computeEllipseAreaCm2(c, edge, measureMeta);
        var stats = m.stats || { mean: 0, std: 0, min: 0, max: 0 };
        var ellipse = '<ellipse cx="' + cx + '" cy="' + cy + '" rx="' + rx + '" ry="' + ry + '" stroke="#38bdf8" stroke-width="2" fill="rgba(56,189,248,0.18)"/>' +
          '<circle cx="' + cx + '" cy="' + cy + '" r="3" fill="#38bdf8"/>' +
          '<rect x="' + (cx + rx + 6) + '" y="' + (cy - 20) + '" width="110" height="38" rx="4" fill="rgba(3,7,18,0.9)" stroke="#38bdf8" stroke-width="1"/>' +
          '<text x="' + (cx + rx + 12) + '" y="' + (cy - 8) + '" fill="#38bdf8" font-size="9.5" font-weight="700" font-family="ui-monospace,monospace">Area: ' + areaCm2.toFixed(1) + ' cm²</text>' +
          '<text x="' + (cx + rx + 12) + '" y="' + (cy + 4) + '" fill="#e2e8f0" font-size="9" font-family="ui-monospace,monospace">Mean: ' + stats.mean.toFixed(1) + ' HU (±' + stats.std.toFixed(1) + ')</text>' +
          '<text x="' + (cx + rx + 12) + '" y="' + (cy + 15) + '" fill="#94a3b8" font-size="8.5" font-family="ui-monospace,monospace">Range: ' + stats.min + ' to ' + stats.max + ' HU</text>';
        svg.innerHTML += ellipse;
      } else if (m.type === 'cobb' && m.points.length >= 2) {
        var pts = m.points;
        for (var i = 0; i < pts.length; i += 2) {
          if (i + 1 < pts.length) {
            var lx1 = pts[i].fx * w, ly1 = pts[i].fy * h;
            var lx2 = pts[i + 1].fx * w, ly2 = pts[i + 1].fy * h;
            svg.innerHTML += '<line x1="' + lx1 + '" y1="' + ly1 + '" x2="' + lx2 + '" y2="' + ly2 + '" stroke="#fbbf24" stroke-width="2"/>' +
              '<circle cx="' + lx1 + '" cy="' + ly1 + '" r="3" fill="#fbbf24"/><circle cx="' + lx2 + '" cy="' + ly2 + '" r="3" fill="#fbbf24"/>';
          }
        }
        if (m.angleDeg != null) {
          svg.innerHTML += '<rect x="10" y="30" width="130" height="20" rx="4" fill="rgba(3,7,18,0.85)" stroke="#fbbf24" stroke-width="1"/>' +
            '<text x="16" y="44" fill="#fbbf24" font-size="10" font-weight="700" font-family="ui-monospace,monospace">Cobb Angle: ' + m.angleDeg.toFixed(1) + '°</text>';
        }
      } else if (m.type === 'ctr' && m.points.length >= 2) {
        var cpts = m.points;
        for (var k = 0; k < cpts.length; k += 2) {
          if (k + 1 < cpts.length) {
            var cx1 = cpts[k].fx * w, cy1 = cpts[k].fy * h;
            var cx2 = cpts[k + 1].fx * w, cy2 = cpts[k + 1].fy * h;
            var col = k === 0 ? '#f43f5e' : '#38bdf8';
            svg.innerHTML += '<line x1="' + cx1 + '" y1="' + cy1 + '" x2="' + cx2 + '" y2="' + cy2 + '" stroke="' + col + '" stroke-width="2" stroke-dasharray="4,2"/>' +
              '<circle cx="' + cx1 + '" cy="' + cy1 + '" r="3" fill="' + col + '"/><circle cx="' + cx2 + '" cy="' + cy2 + '" r="3" fill="' + col + '"/>';
          }
        }
        if (m.ratio != null) {
          var isCardio = m.ratio >= 0.5;
          var rCol = isCardio ? '#f43f5e' : '#34d399';
          svg.innerHTML += '<rect x="10" y="30" width="165" height="22" rx="4" fill="rgba(3,7,18,0.85)" stroke="' + rCol + '" stroke-width="1"/>' +
            '<text x="16" y="45" fill="' + rCol + '" font-size="10" font-weight="700" font-family="ui-monospace,monospace">CTR: ' + m.ratio.toFixed(2) + ' (' + (isCardio ? 'Cardiomegaly' : 'Normal < 0.50') + ')</text>';
        }
      }
    });
  }

  function setupPointer(vp, plane) {
    var wrap = document.getElementById(vp.wrap);
    if (!wrap) return;
    var dragging = null;
    var isDrawingTool = false;

    wrap.onpointerdown = function (e) {
      wrap.setPointerCapture(e.pointerId);
      var rect = wrap.getBoundingClientRect();
      var fx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      var fy = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));

      if (state.activeTool === 'crosshair') {
        beginInteraction();
        dragging = { x: e.clientX, y: e.clientY, c: state.wl.c, w: state.wl.w };
        var was = { x: e.clientX, y: e.clientY };
        wrap.onpointerup = function (ev) {
          wrap.onpointerup = null;
          if (dragging && Math.abs(ev.clientX - was.x) < 4 && Math.abs(ev.clientY - was.y) < 4) {
            moveCrosshair(plane, wrap, ev);
          }
          dragging = null;
          endInteraction();
        };
      } else if (state.activeTool === 'ruler' || state.activeTool === 'roi') {
        isDrawingTool = true;
        state.pendingTool = {
          id: Date.now(),
          type: state.activeTool,
          plane: plane,
          sliceIdx: state.idx[plane],
          points: [{ fx: fx, fy: fy }, { fx: fx, fy: fy }]
        };
        drawSvgOverlays(plane);

        wrap.onpointerup = function (ev) {
          wrap.onpointerup = null;
          isDrawingTool = false;
          if (state.pendingTool && state.pendingTool.points.length >= 2) {
            if (state.activeTool === 'roi') {
              state.pendingTool.stats = computeRoiStats(plane, state.pendingTool.points[0], state.pendingTool.points[1], vp);
            }
            state.measurements.push(state.pendingTool);
            state.pendingTool = null;
            drawSvgOverlays(plane);
          }
        };
      } else if (state.activeTool === 'cobb' || state.activeTool === 'ctr') {
        if (!state.pendingTool || state.pendingTool.type !== state.activeTool || state.pendingTool.plane !== plane) {
          state.pendingTool = {
            id: Date.now(),
            type: state.activeTool,
            plane: plane,
            sliceIdx: state.idx[plane],
            points: [{ fx: fx, fy: fy }]
          };
        } else {
          state.pendingTool.points.push({ fx: fx, fy: fy });
          if (state.pendingTool.points.length === 4) {
            var pts = state.pendingTool.points;
            // Measurements are computed in physical (patient) space from the
            // pixel spacing — a normalized viewport fraction is not an angle
            // and not a length once the spacing is anisotropic (C09).
            var measureMeta = planeMeta(plane);
            if (state.activeTool === 'cobb') {
              state.pendingTool.angleDeg = geo().computeCobbAngle(
                pts[0], pts[1], pts[2], pts[3], measureMeta
              );
            } else if (state.activeTool === 'ctr') {
              var cDist = geo().computePhysicalLength(pts[0], pts[1], measureMeta);
              var tDist = geo().computePhysicalLength(pts[2], pts[3], measureMeta);
              state.pendingTool.ratio = geo().computePhysicalRatio(cDist, tDist);
            }
            state.measurements.push(state.pendingTool);
            state.pendingTool = null;
          }
        }
        drawSvgOverlays(plane);
      }
    };

    wrap.onpointermove = function (e) {
      var rect = wrap.getBoundingClientRect();
      var fx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      var fy = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));

      if (dragging) {
        beginInteraction();
        var dx = e.clientX - dragging.x, dy = e.clientY - dragging.y;
        state.wl.w = Math.max(1, dragging.w + dx * 2);
        state.wl.c = dragging.c - dy * 2;
        scheduleRender();
      } else if (isDrawingTool && state.pendingTool) {
        state.pendingTool.points[1] = { fx: fx, fy: fy };
        drawSvgOverlays(plane);
      }

      // Hover HU intensity lookup — the grid position is converted to a voxel
      // by the geometry module, so the probe reads exactly the voxel the plane
      // extractor displayed at that position (C09).
      if (state.vol) {
        var px = Math.floor(fx * (vp.w - 1));
        var py = Math.floor(fy * (vp.h - 1));
        var meta = planeMeta(plane);
        var voxel = geo().viewportToVoxel(px, py, plane, meta);
        var huVal = geo().sampleVoxel(state.vol, voxel.i, voxel.j, voxel.k, meta);
        state.hoverHu[plane] = Math.round(huVal || 0) + ' HU';
        updateHud(plane);
      }
    };

    wrap.onwheel = function (e) {
      e.preventDefault();
      beginInteraction();
      var maxIdx = (plane === 'axial' ? state.slices : (plane === 'sagittal' ? state.cols : state.rows)) - 1;
      var step = e.deltaY > 0 ? 1 : -1;
      state.idx[plane] = Math.max(0, Math.min(maxIdx, state.idx[plane] + step));
      var slider = document.getElementById(vp.slider);
      if (slider) slider.value = state.idx[plane];
      scheduleRender();
      endInteraction(140);
    };
  }

  function computeRoiStats(plane, c, edge, vp) {
    if (!state.vol) return { mean: 0, std: 0, min: 0, max: 0 };
    var meta = planeMeta(plane);
    var cx = Math.round(c.fx * (vp.w - 1));
    var cy = Math.round(c.fy * (vp.h - 1));
    var rx = Math.max(2, Math.round(Math.abs(edge.fx - c.fx) * (vp.w - 1)));
    var ry = Math.max(2, Math.round(Math.abs(edge.fy - c.fy) * (vp.h - 1)));

    var sum = 0, sumSq = 0, count = 0;
    var mn = 1e9, mx = -1e9;

    for (var dy = -ry; dy <= ry; dy++) {
      for (var dx = -rx; dx <= rx; dx++) {
        if ((dx * dx) / (rx * rx) + (dy * dy) / (ry * ry) <= 1.0) {
          var px = cx + dx, py = cy + dy;
          if (px >= 0 && px < vp.w && py >= 0 && py < vp.h) {
            // Same grid->voxel mapping as the probe and the plane extractor.
            var voxel = geo().viewportToVoxel(px, py, plane, meta);
            var v = geo().sampleVoxel(state.vol, voxel.i, voxel.j, voxel.k, meta);
            if (isNaN(v)) continue;
            sum += v;
            sumSq += v * v;
            mn = Math.min(mn, v);
            mx = Math.max(mx, v);
            count++;
          }
        }
      }
    }
    if (count === 0) return { mean: 0, std: 0, min: 0, max: 0 };
    var mean = sum / count;
    var variance = Math.max(0, (sumSq / count) - (mean * mean));
    return { mean: mean, std: Math.sqrt(variance), min: Math.round(mn), max: Math.round(mx) };
  }

  function loadFusionManifest() {
    var studyUid = state.studyUid || (state.info ? state.info.study_instance_uid : null);
    if (!studyUid) return;
    fetchJson('/api/v1/fusion/' + encodeURIComponent(studyUid))
      .then(function (manifest) {
        if (!manifest || !manifest.frames_of_reference) return;
        var sel = document.getElementById('mpr-fusion-series');
        if (!sel) return;
        sel.innerHTML = '';
        var fusable = [];
        manifest.frames_of_reference.forEach(function (forGroup) {
          forGroup.series.forEach(function (s) {
            if (s.series_instance_uid !== state.seriesUid) {
              fusable.push(s);
              var opt = document.createElement('option');
              opt.value = s.series_instance_uid;
              opt.textContent = (s.modality || 'IMG') + ' · ' + (s.series_description || 'Series ' + s.series_number);
              sel.appendChild(opt);
            }
          });
        });
        if (fusable.length > 0) {
          sel.onchange = function () { loadFusionOverlayVolume(sel.value); };
          loadFusionOverlayVolume(fusable[0].series_instance_uid);
        }
      })
      .catch(function () {});
  }

  function loadFusionOverlayVolume(overlayUid) {
    state.fusion.overlaySeriesUid = overlayUid;
    fetchJson('/api/v1/volume/' + encodeURIComponent(overlayUid) + '/info')
      .then(function (info) {
        state.fusion.overlayInfo = info;
        return fetch('/api/v1/volume/' + encodeURIComponent(overlayUid) + '/data?downsample=2')
          .then(function (r) { return r.arrayBuffer(); })
          .then(function (buf) {
            var downInfo = Object.assign({}, info, {
              rows: geo().stridedDim(info.rows, 2),
              columns: geo().stridedDim(info.columns, 2),
              slices: geo().stridedDim(info.slices, 2)
            });
            return decodeVolumeAsync(downInfo, buf);
          });
      })
      .then(function (decoded) {
        if (decoded) {
          state.fusion.overlayVol = decoded.vol;
          renderAll();
        }
      })
      .catch(function () {});
  }

  function moveCrosshair(plane, wrap, ev) {
    beginInteraction();
    var rect = wrap.getBoundingClientRect();
    var fx = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
    var fy = Math.max(0, Math.min(1, (ev.clientY - rect.top) / rect.height));
    // u/v grid position -> the two in-plane voxel indices of this plane
    // (axial: u=i, v=j · sagittal: u=j, v=k · coronal: u=i, v=k).
    var dims = geo().planeDims(plane, planeMeta(plane));
    var uIndex = Math.round(fx * Math.max(0, dims.w - 1));
    var vIndex = Math.round(fy * Math.max(0, dims.h - 1));
    if (plane === 'axial') {
      state.idx.sagittal = uIndex;
      state.idx.coronal = vIndex;
    } else if (plane === 'sagittal') {
      state.idx.coronal = uIndex;
      state.idx.axial = vIndex;
    } else {
      state.idx.sagittal = uIndex;
      state.idx.axial = vIndex;
    }
    ['axial', 'sagittal', 'coronal'].forEach(function (p) {
      var s = document.getElementById(vps[p].slider);
      if (s) s.value = state.idx[p];
    });
    scheduleRender();
    endInteraction(120);
  }

  function disposeMaterial(material) {
    if (!material) return;
    // Textures referenced by the material must be released with it.
    ['map', 'alphaMap', 'emissiveMap', 'normalMap', 'specularMap'].forEach(function (slot) {
      var tex = material[slot];
      if (tex && tex.dispose) { tex.dispose(); releaseResource('textures'); }
    });
    if (material.uniforms) {
      Object.keys(material.uniforms).forEach(function (key) {
        var value = material.uniforms[key] && material.uniforms[key].value;
        if (value && value.isTexture && value.dispose) {
          value.dispose();
          releaseResource('textures');
        }
      });
    }
    if (material.dispose) {
      material.dispose();
      releaseResource('materials');
    }
  }

  /**
   * Release every GPU resource owned by the 3D viewport and detach its
   * listeners. Called on close AND before rebuilding the scene on a series
   * switch, so repeated open/close cycles return to the resource baseline
   * (H15 / R7). The animation frame is stopped first: the loop closes over the
   * old renderer/scene and must not render into a disposed context.
   */
  function disposeThreeScene() {
    if (threeState.animFrame) {
      cancelAnimationFrame(threeState.animFrame);
      threeState.animFrame = null;
    }
    if (threeState.onPointerUp) {
      window.removeEventListener('pointerup', threeState.onPointerUp);
      threeState.onPointerUp = null;
    }
    var scene = threeState.scene;
    if (scene && scene.traverse) {
      scene.traverse(function (node) {
        if (node.geometry && node.geometry.dispose) {
          node.geometry.dispose();
          releaseResource('geometries');
        }
        if (node.material) {
          var materials = Object.prototype.toString.call(node.material) === '[object Array]'
            ? node.material
            : [node.material];
          materials.forEach(disposeMaterial);
        }
      });
      while (scene.children.length) { scene.remove(scene.children[0]); }
    }
    // Resources that are not reachable through the scene graph (e.g. the box
    // geometry used to derive the edge geometry) must be released explicitly.
    (threeState.extraResources || []).forEach(function (obj) {
      if (obj && obj.dispose) {
        obj.dispose();
        releaseResource('geometries');
      }
    });
    threeState.extraResources = [];
    if (threeState.renderer) {
      threeState.renderer.dispose();
      if (threeState.renderer.forceContextLoss) threeState.renderer.forceContextLoss();
      threeState.renderer = null;
    }
    var wrap = document.getElementById('mpr-3d-wrap');
    if (wrap) {
      wrap.onpointerdown = null;
      wrap.onpointermove = null;
      wrap.onwheel = null;
    }
    threeState.scene = null;
    threeState.camera = null;
    threeState.box = null;
    threeState.volumeMesh = null;
    threeState.planes = { axial: null, sagittal: null, coronal: null };
    threeState.isDragging = false;
    if (window.__paxsMprDebug) {
      console.log('PAXS MPR live GPU resources after dispose:', resourceStats);
    }
  }

  function init3dEngine() {
    if (!threeState.loaded || !window.THREE) return;
    var cv = document.getElementById('mpr-3d-cv');
    var wrap = document.getElementById('mpr-3d-wrap');
    if (!cv || !wrap) return;

    // Switching series re-runs this function: release the previous scene first
    // so repeated loads cannot accumulate GPU objects (H15).
    disposeThreeScene();

    var THREE = window.THREE;
    var scene = new THREE.Scene();
    scene.background = new THREE.Color(0x03060d);
    threeState.scene = scene;

    var rect = wrap.getBoundingClientRect();
    var camera = new THREE.PerspectiveCamera(45, rect.width / (rect.height || 1), 1, 2000);
    threeState.camera = camera;
    update3dCameraPos();

    var renderer = new THREE.WebGLRenderer({ canvas: cv, antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setSize(rect.width, rect.height);
    threeState.renderer = renderer;

    var ambient = new THREE.AmbientLight(0xffffff, 0.75);
    scene.add(ambient);
    var dirLight = new THREE.DirectionalLight(0x22d3ee, 0.85);
    dirLight.position.set(200, 300, 200);
    scene.add(dirLight);

    var sx = state.cols * state.spacing[1];
    var sy = state.rows * state.spacing[0];
    var sz = state.slices * state.sliceSpacing;
    var boxGeo = new THREE.BoxGeometry(sx, sy, sz);
    var boxMat = new THREE.LineBasicMaterial({ color: 0x38bdf8, opacity: 0.35, transparent: true });
    var boxEdges = new THREE.EdgesGeometry(boxGeo);
    var boxLine = new THREE.LineSegments(boxEdges, boxMat);
    scene.add(boxLine);
    threeState.box = boxLine;
    trackResource('geometries');  // boxGeo
    trackResource('geometries');  // boxEdges
    trackResource('materials');   // boxMat
    // boxGeo is only an input to EdgesGeometry, so the scene traversal cannot
    // reach it — keep it for explicit disposal.
    threeState.extraResources = [boxGeo];

    threeState.planes.axial = create3dPlaneMesh(sx, sy, 0x22d3ee, 'axial');
    threeState.planes.sagittal = create3dPlaneMesh(sy, sz, 0x2dd4bf, 'sagittal');
    threeState.planes.sagittal.rotation.y = Math.PI / 2;
    threeState.planes.coronal = create3dPlaneMesh(sx, sz, 0xfbbf24, 'coronal');
    threeState.planes.coronal.rotation.x = Math.PI / 2;

    scene.add(threeState.planes.axial);
    scene.add(threeState.planes.sagittal);
    scene.add(threeState.planes.coronal);

    var volRaymarch = create3dVolumeRaymarchMesh(sx, sy, sz);
    if (volRaymarch) {
      scene.add(volRaymarch);
      threeState.volumeMesh = volRaymarch;
      volRaymarch.visible = (threeState.renderMode === 'vrt' || threeState.renderMode === 'mip3d');
    }

    wrap.onpointerdown = function (e) {
      threeState.isDragging = true;
      beginInteraction();
      threeState.prevMouse = { x: e.clientX, y: e.clientY };
    };
    wrap.onpointermove = function (e) {
      if (!threeState.isDragging) return;
      beginInteraction();
      var dx = e.clientX - threeState.prevMouse.x;
      var dy = e.clientY - threeState.prevMouse.y;
      threeState.rot.y += dx * 0.01;
      threeState.rot.x = Math.max(-Math.PI / 2 + 0.1, Math.min(Math.PI / 2 - 0.1, threeState.rot.x + dy * 0.01));
      threeState.prevMouse = { x: e.clientX, y: e.clientY };
      update3dCameraPos();
    };
    // Named handler so disposeThreeScene() can detach it (an anonymous
    // listener would leak one per series load / workspace open).
    threeState.onPointerUp = function () {
      if (threeState.isDragging) {
        threeState.isDragging = false;
        endInteraction();
      }
    };
    window.addEventListener('pointerup', threeState.onPointerUp);
    wrap.onwheel = function (e) {
      e.preventDefault();
      beginInteraction();
      threeState.dist = Math.max(100, Math.min(1200, threeState.dist + e.deltaY * 0.5));
      update3dCameraPos();
      endInteraction(150);
    };

    function animLoop() {
      threeState.animFrame = requestAnimationFrame(animLoop);
      renderer.render(scene, camera);
    }
    animLoop();
  }

  function create3dVolumeRaymarchMesh(sx, sy, sz) {
    var THREE = window.THREE;
    if (!THREE || !state.vol) return null;

    var geo = new THREE.BoxGeometry(sx, sy, sz);
    trackResource('geometries');
    var tex3d;
    try {
      tex3d = new THREE.Data3DTexture(state.vol, state.cols, state.rows, state.slices);
      trackResource('textures');
      tex3d.format = THREE.RedFormat;
      tex3d.type = THREE.FloatType;
      tex3d.minFilter = THREE.LinearFilter;
      tex3d.magFilter = THREE.LinearFilter;
      tex3d.unpackAlignment = 1;
      tex3d.needsUpdate = true;
    } catch (e) {
      return null;
    }

    var vs = [
      'varying vec3 vOrigin;',
      'varying vec3 vDirection;',
      'void main() {',
      '  vec4 worldPos = modelMatrix * vec4(position, 1.0);',
      '  vOrigin = (inverse(modelMatrix) * vec4(cameraPosition, 1.0)).xyz;',
      '  vDirection = position - vOrigin;',
      '  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);',
      '}'
    ].join('\n');

    var fs = [
      'precision highp float;',
      'precision highp sampler3D;',
      'varying vec3 vOrigin;',
      'varying vec3 vDirection;',
      'uniform sampler3D u_data;',
      'uniform vec3 u_size;',
      'uniform float u_wlC;',
      'uniform float u_wlW;',
      'uniform int u_preset;',
      'uniform int u_steps;',
      'vec2 hitBox(vec3 orig, vec3 dir) {',
      '  vec3 boxMin = -u_size * 0.5;',
      '  vec3 boxMax = u_size * 0.5;',
      '  vec3 invDir = 1.0 / max(abs(dir), vec3(1e-5)) * sign(dir);',
      '  vec3 t0 = (boxMin - orig) * invDir;',
      '  vec3 t1 = (boxMax - orig) * invDir;',
      '  vec3 tmin = min(t0, t1);',
      '  vec3 tmax = max(t0, t1);',
      '  float tNear = max(max(tmin.x, tmin.y), tmin.z);',
      '  float tFar = min(min(tmax.x, tmax.y), tmax.z);',
      '  return vec2(tNear, tFar);',
      '}',
      'float sampleVol(vec3 p) {',
      '  vec3 uv = (p / u_size) + 0.5;',
      '  return texture(u_data, clamp(uv, 0.0, 1.0)).r;',
      '}',
      'vec4 applyLut(float val) {',
      '  float lo = u_wlC - u_wlW * 0.5;',
      '  float t = clamp((val - lo) / max(u_wlW, 1.0), 0.0, 1.0);',
      '  if (u_preset == 0) {', // Bone
      '    float a = smoothstep(0.3, 0.7, t);',
      '    vec3 col = mix(vec3(0.9, 0.75, 0.55), vec3(1.0, 0.98, 0.94), t);',
      '    return vec4(col, a * 0.35);',
      '  } else if (u_preset == 1) {', // MIP
      '    return vec4(vec3(t), t);',
      '  } else if (u_preset == 2) {', // Angio
      '    float a = smoothstep(0.35, 0.85, t);',
      '    vec3 col = mix(vec3(0.9, 0.15, 0.1), vec3(1.0, 0.85, 0.25), t);',
      '    return vec4(col, a * 0.45);',
      '  } else {', // Soft Tissue
      '    float a = smoothstep(0.12, 0.55, t);',
      '    vec3 col = mix(vec3(0.65, 0.25, 0.2), vec3(0.95, 0.85, 0.75), t);',
      '    return vec4(col, a * 0.25);',
      '  }',
      '}',
      'void main() {',
      '  vec3 rayDir = normalize(vDirection);',
      '  vec2 bounds = hitBox(vOrigin, rayDir);',
      '  if (bounds.x > bounds.y || bounds.y < 0.0) discard;',
      '  float tStart = max(bounds.x, 0.0);',
      '  float tEnd = bounds.y;',
      '  float rayLen = tEnd - tStart;',
      '  int steps = clamp(u_steps, 32, 192);',
      '  float stepSize = rayLen / float(steps);',
      '  vec3 p = vOrigin + rayDir * tStart;',
      '  vec4 accum = vec4(0.0);',
      '  float maxVal = -10000.0;',
      '  for (int i = 0; i < 192; i++) {',
      '    if (i >= steps || accum.a >= 0.95) break;',
      '    float val = sampleVol(p);',
      '    if (u_preset == 1) {',
      '      maxVal = max(maxVal, val);',
      '    } else {',
      '      vec4 c = applyLut(val);',
      '      c.rgb *= c.a;',
      '      accum += c * (1.0 - accum.a);',
      '    }',
      '    p += rayDir * stepSize;',
      '  }',
      '  if (u_preset == 1) {',
      '    float lo = u_wlC - u_wlW * 0.5;',
      '    float t = clamp((maxVal - lo) / max(u_wlW, 1.0), 0.0, 1.0);',
      '    gl_FragColor = vec4(vec3(t), t > 0.05 ? 0.95 : 0.0);',
      '  } else {',
      '    if (accum.a < 0.01) discard;',
      '    gl_FragColor = accum;',
      '  }',
      '}'
    ].join('\n');

    var mat = new THREE.ShaderMaterial({
      vertexShader: vs,
      fragmentShader: fs,
      uniforms: {
        u_data: { value: tex3d },
        u_size: { value: new THREE.Vector3(sx, sy, sz) },
        u_wlC: { value: state.wl.c },
        u_wlW: { value: state.wl.w },
        u_preset: { value: threeState.transferFunction === 'bone' ? 0 : (threeState.transferFunction === 'mip' ? 1 : (threeState.transferFunction === 'angio' ? 2 : 3)) },
        u_steps: { value: state.lod.active ? 48 : 128 }
      },
      side: THREE.BackSide,
      transparent: true,
      depthWrite: false
    });

    trackResource('materials');
    var mesh = new THREE.Mesh(geo, mat);
    mesh.name = 'mpr-volume-raymarch';
    return mesh;
  }

  function create3dPlaneMesh(w, h, colorHex, plane) {
    var THREE = window.THREE;
    var geo = new THREE.PlaneGeometry(w, h);
    trackResource('geometries');
    var tex = new THREE.CanvasTexture(document.createElement('canvas'));
    trackResource('textures');
    tex.minFilter = THREE.LinearFilter;
    tex.magFilter = THREE.LinearFilter;
    var mat = new THREE.MeshBasicMaterial({
      map: tex,
      color: colorHex,
      side: THREE.DoubleSide,
      transparent: true,
      opacity: 0.92,
      depthWrite: false
    });
    trackResource('materials');
    var mesh = new THREE.Mesh(geo, mat);
    mesh.name = 'mpr-plane-' + plane;
    return mesh;
  }

  function set3dCamera(rx, ry) {
    threeState.rot.x = rx;
    threeState.rot.y = ry;
    update3dCameraPos();
  }

  function update3dCameraPos() {
    if (!threeState.camera) return;
    var r = threeState.dist;
    var x = r * Math.cos(threeState.rot.x) * Math.sin(threeState.rot.y);
    var y = r * Math.sin(threeState.rot.x);
    var z = r * Math.cos(threeState.rot.x) * Math.cos(threeState.rot.y);
    threeState.camera.position.set(x, y, z);
    threeState.camera.lookAt(0, 0, 0);
  }

  function update3dPlanes() {
    if (!threeState.loaded) return;
    var sx = state.cols * state.spacing[1];
    var sy = state.rows * state.spacing[0];
    var sz = state.slices * state.sliceSpacing;

    var isVolumeRaymarch = (threeState.renderMode === 'vrt' || threeState.renderMode === 'mip3d');
    
    if (threeState.planes.axial) {
      var zPos = (state.idx.axial / Math.max(1, state.slices - 1) - 0.5) * sz;
      var xPos = (state.idx.sagittal / Math.max(1, state.cols - 1) - 0.5) * sx;
      var yPos = (state.idx.coronal / Math.max(1, state.rows - 1) - 0.5) * sy;

      threeState.planes.axial.position.z = zPos;
      threeState.planes.sagittal.position.x = xPos;
      threeState.planes.coronal.position.y = yPos;

      threeState.planes.axial.visible = !isVolumeRaymarch;
      threeState.planes.sagittal.visible = !isVolumeRaymarch;
      threeState.planes.coronal.visible = !isVolumeRaymarch;

      if (!isVolumeRaymarch) {
        ['axial', 'sagittal', 'coronal'].forEach(function (p) {
          var mesh = threeState.planes[p];
          if (!mesh || !mesh.material || !mesh.material.map) return;
          mesh.material.map.image = document.getElementById(vps[p].cv);
          mesh.material.map.needsUpdate = true;
        });
      }
    }

    if (threeState.volumeMesh && threeState.volumeMesh.material && threeState.volumeMesh.material.uniforms) {
      threeState.volumeMesh.visible = isVolumeRaymarch;
      var u = threeState.volumeMesh.material.uniforms;
      u.u_wlC.value = state.wl.c;
      u.u_wlW.value = state.wl.w;
      u.u_preset.value = threeState.renderMode === 'mip3d' ? 1 : (threeState.transferFunction === 'bone' ? 0 : (threeState.transferFunction === 'angio' ? 2 : 3));
      u.u_steps.value = state.lod.active ? 48 : 128;
    }

    var hud3d = document.getElementById('mpr-3d-hud');
    if (hud3d) {
      var modeStr = threeState.renderMode === 'planes' ? 'CUTPLANES' : (threeState.renderMode === 'mip3d' ? 'MIP 3D' : 'VRT RAYMARCH');
      hud3d.textContent = modeStr + ' · ROT: ' + Math.round((threeState.rot.y * 180 / Math.PI) % 360) + '°';
    }
  }

  function resize3dCanvas() {
    if (!threeState.renderer || !threeState.camera) return;
    var wrap = document.getElementById('mpr-3d-wrap');
    if (!wrap) return;
    var rect = wrap.getBoundingClientRect();
    threeState.camera.aspect = rect.width / (rect.height || 1);
    threeState.camera.updateProjectionMatrix();
    threeState.renderer.setSize(rect.width, rect.height);
  }

  function showError(msg) {
    var body = document.getElementById('mpr-body');
    if (body) body.innerHTML = '<div class="mpr-error">MPR Workspace error: ' + msg + '</div>';
    var sub = document.getElementById('mpr-sub');
    if (sub) sub.textContent = 'error';
  }

  var polls = 0;
  var pollTimer = setInterval(function () { ensureButton(); if (++polls > 25) clearInterval(pollTimer); }, 800);
  setTimeout(function () {
    ensureButton();
    var mo = new MutationObserver(function () { requestAnimationFrame(ensureButton); });
    mo.observe(document.body, { childList: true, subtree: true });
  }, 500);

})();
