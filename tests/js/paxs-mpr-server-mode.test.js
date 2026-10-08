/**
 * Behavioural tests for the MPR viewer's server-mode pipeline (Phase 2).
 *
 * The real `serverKey`, `drawServerImage`, `serverPlane`, `resetServerCache`,
 * `serverRawKey`, `serverRoiStats` and `releaseServerResources` functions are
 * extracted from the shipped app/static/viewer/paxs-mpr.js and executed in a
 * vm sandbox against a fake DOM. Only the browser boundary is replaced
 * (`document`, `Image`, `fetch`, `URL`); every staleness / geometry / pending
 * rule under test is the real shipped code.
 *
 *   node --test "tests/js/*.test.js"
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { readViewerSource, extractFunction, runInSandbox } = require('./extract.js');

const SOURCE = readViewerSource();

const SERVER_FUNCS = [
  'serverKey',
  'resetServerCache',
  'serverPlane',
  'drawServerImage',
  'serverRawKey',
  'fetchServerRawPlane',
  'serverRoiStats',
  'releaseServerResources',
  'drawSvgOverlays',
];

/** Minimal fake canvas 2D context recording every drawImage call. */
function fakeContext() {
  return {
    draws: [],
    imageSmoothingEnabled: true,
    fillStyle: '',
    fillRect() {},
    drawImage(img, x, y, w, h) {
      this.draws.push({ img, x, y, w, h });
    },
  };
}

/**
 * Sandbox with the real server-mode functions.
 *
 * `state` is a real plain object shaped like the viewer's; `vps` mimics the
 * viewport descriptors (canvas + context + HUD ids). `Image` is a fake whose
 * `src` setter records the request and lets the test fire `onload` manually,
 * which is what makes stale-response ordering testable.
 *
 * `fetch` is a fake returning a canned raw plane when `opts.rawPlane` is set,
 * so the real `fetchServerRawPlane` geometry validation is exercised.
 */
function makeSandbox(options) {
  const opts = options || {};
  const requested = [];
  const createdImages = [];
  const revoked = [];
  const fetched = [];
  const timers = [];

  const ctx2d = fakeContext();
  const cv = { width: 640, height: 480, getContext: () => ctx2d, getBoundingClientRect: () => ({ width: 640, height: 480 }) };

  const hudEl = { innerHTML: '' };
  const svgEl = { innerHTML: '', getBoundingClientRect: () => ({ width: 32, height: 32 }) };

  const doc = {
    getElementById(id) {
      if (id === 'mpr-axial-cv') return cv;
      if (id === 'mpr-axial-hud') return hudEl;
      if (id === 'mpr-axial-svg') return svgEl;
      return null;
    },
  };

  function FakeImage() {
    const self = this;
    createdImages.push(self);
    self.width = opts.imgWidth == null ? 32 : opts.imgWidth;
    self.height = opts.imgHeight == null ? 32 : opts.imgHeight;
    self.onload = null;
    self.onerror = null;
    let _src = '';
    Object.defineProperty(self, 'src', {
      get() { return _src; },
      set(v) {
        _src = v;
        requested.push(v);
        if (opts.autoLoad) { self.onload && self.onload(); }
      },
    });
  }

  /** Fake fetch: answers raw-plane requests when `rawPlane` is configured. */
  function fakeFetch(url) {
    fetched.push(url);
    if (opts.rawPlane && String(url).includes('format=raw')) {
      const plane = opts.rawPlane;
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: (h) => (h === 'X-MPR-Rows' ? String(plane.rows) : h === 'X-MPR-Cols' ? String(plane.cols) : null) },
        arrayBuffer: () => Promise.resolve(plane.data.buffer.slice(0)),
      });
    }
    return Promise.resolve({ ok: false, status: 404, headers: { get: () => null } });
  }

  const scope = {
    state: opts.state,
    vps: opts.vps,
    document: doc,
    Image: FakeImage,
    fetch: fakeFetch,
    console: { warn() {}, log() {} },
    // Capture timers instead of running them, so a debounce under test cannot
    // fire asynchronously and turn an assertion into a race.
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    // `serverWlTimer` is a real viewer-level variable the extracted functions
    // read and write; give the sandbox its own binding.
    serverWlTimer: null,
    // Geometry helpers the server pipeline delegates to. `planeMeta` supplies
    // the plane dimensions the ROI sampler maps viewport -> plane pixels with;
    // `geo().planeDims` mirrors paxs-mpr-geometry.js for a 10x32x32 volume.
    planeMeta: () => ({ slices: 10, rows: 32, cols: 32, spacing: [1, 1], sliceSpacing: 1 }),
    geo: () => ({
      planeDims: (plane) => {
        if (plane === 'axial') return { w: 32, h: 32 };
        if (plane === 'sagittal') return { w: 32, h: 10 };
        return { w: 32, h: 10 };
      },
    }),
    Promise,
    Float32Array,
    Math,
    isNaN,
    Number,
    String,
    Object,
    Array,
    parseInt,
    URL: {
      createObjectURL: () => 'blob:fake-' + createdImages.length,
      revokeObjectURL: (u) => { revoked.push(u); },
    },
  };

  const ctx = runInSandbox(
    SERVER_FUNCS.map((name) => extractFunction(SOURCE, name)).join('\n'),
    scope
  );
  return { ctx, requested, createdImages, revoked, fetched, timers, ctx2d, hudEl, svgEl };
}

/** Viewer-shaped state with only the fields the server pipeline touches. */
function baseState(overrides) {
  const state = {
    mode: 'server',
    seriesUid: '1.2.3.SERIES_A',
    info: { slices: 10, rows: 32, columns: 32 },
    slices: 10,
    rows: 32,
    cols: 32,
    idx: { axial: 4, sagittal: 0, coronal: 0 },
    wl: { c: 40, w: 400 },
    hoverHu: { axial: '--', sagittal: '--', coronal: '--' },
    measurements: [],
    pendingTool: null,
    serverTouched: false,
    serverCache: {
      axial: { key: '', img: null, imgKey: '', seq: 0, prevKey: '', prevImg: null },
      sagittal: { key: '', img: null, imgKey: '', seq: 0, prevKey: '', prevImg: null },
      coronal: { key: '', img: null, imgKey: '', seq: 0, prevKey: '', prevImg: null },
    },
    serverRaw: {
      axial: { key: '', rows: 0, cols: 0, data: null },
      sagittal: { key: '', rows: 0, cols: 0, data: null },
      coronal: { key: '', rows: 0, cols: 0, data: null },
    },
    serverFrames: {},
  };
  return Object.assign(state, overrides || {});
}

function baseVps() {
  return {
    axial: { cv: 'mpr-axial-cv', wrap: 'mpr-axial-wrap', slider: 'mpr-axial-slider', hud: 'mpr-axial-hud', w: 32, h: 32 },
    sagittal: { cv: 'mpr-sag-cv', wrap: 'mpr-sag-wrap', slider: 'mpr-sag-slider', hud: 'mpr-sag-hud', w: 32, h: 32 },
    coronal: { cv: 'mpr-cor-cv', wrap: 'mpr-cor-wrap', slider: 'mpr-cor-slider', hud: 'mpr-cor-hud', w: 32, h: 32 },
  };
}

/* -------------------------------------------------------------------------- */
/* Request identity — a series switch must never repaint the old study        */
/* -------------------------------------------------------------------------- */

test('test_server_request_includes_series_plane_index_and_window', () => {
  const s = makeSandbox({ state: baseState(), vps: baseVps() });
  const key = s.ctx.serverKey('axial', 7);
  assert.strictEqual(key, '1.2.3.SERIES_A|axial|7|40|400');
});

test('test_server_key_changes_with_series_and_slice_and_window', () => {
  const s = makeSandbox({ state: baseState(), vps: baseVps() });
  const base = s.ctx.serverKey('axial', 7);
  assert.notStrictEqual(base, s.ctx.serverKey('axial', 8), 'slice must be part of identity');
  assert.notStrictEqual(base, s.ctx.serverKey('sagittal', 7), 'plane must be part of identity');
  s.ctx.state.seriesUid = '9.9.9.OTHER';
  assert.notStrictEqual(base, s.ctx.serverKey('axial', 7), 'series must be part of identity');
});

test('test_server_plane_requests_png_with_window', () => {
  const s = makeSandbox({ state: baseState(), vps: baseVps() });
  s.ctx.serverPlane('axial');

  assert.strictEqual(s.requested.length, 1);
  const url = s.requested[0];
  assert.ok(url.includes('/api/v1/volume/1.2.3.SERIES_A/mpr/slice'), url);
  assert.ok(url.includes('plane=axial'), url);
  assert.ok(url.includes('index=4'), url);
  assert.ok(url.includes('format=png'), url);
  assert.ok(url.includes('window_center=40'), url);
  assert.ok(url.includes('window_width=400'), url);
});

test('test_server_plane_marks_the_session_as_server_touched', () => {
  const s = makeSandbox({ state: baseState(), vps: baseVps() });
  assert.strictEqual(s.ctx.state.serverTouched, false);
  s.ctx.serverPlane('axial');
  assert.strictEqual(s.ctx.state.serverTouched, true);
});

test('test_cached_frame_is_reused_without_a_second_request', () => {
  const state = baseState();
  const s = makeSandbox({ state, vps: baseVps() });

  s.ctx.serverPlane('axial');
  assert.strictEqual(s.requested.length, 1);

  // Resolve the in-flight request, then render again: the cache must serve it.
  s.createdImages[0].onload();
  s.ctx.serverPlane('axial');
  assert.strictEqual(s.requested.length, 1, 'a cached slice must not re-request');
});

test('test_scrolling_back_reuses_the_previously_fetched_frame', () => {
  const state = baseState();
  const s = makeSandbox({ state, vps: baseVps() });

  s.ctx.serverPlane('axial');          // slice 4
  s.createdImages[0].onload();
  state.idx.axial = 5;
  s.ctx.serverPlane('axial');          // slice 5
  s.createdImages[1].onload();
  const before = s.requested.length;
  assert.strictEqual(before, 2);

  state.idx.axial = 4;                 // scroll back
  s.ctx.serverPlane('axial');
  assert.strictEqual(s.requested.length, before, 'scroll-back must hit the retained frame');
  // The retained frame is now the current one, so a further re-render of the
  // same slice is still free.
  s.ctx.serverPlane('axial');
  assert.strictEqual(s.requested.length, before, 'repeat render of the same slice is cached');
});

test('test_slow_response_is_dropped_when_a_newer_request_wins', () => {
  const state = baseState();
  const s = makeSandbox({ state, vps: baseVps() });

  // Request slice 4, then change to slice 5 before either resolves.
  s.ctx.serverPlane('axial');
  const staleImg = s.createdImages[0];
  state.idx.axial = 5;
  s.ctx.serverPlane('axial');
  const freshImg = s.createdImages[1];

  // The stale response lands LAST — it must not paint over slice 5.
  freshImg.onload();
  const drawsAfterFresh = s.ctx2d.draws.length;
  staleImg.onload();

  assert.strictEqual(s.ctx2d.draws.length, drawsAfterFresh, 'a superseded frame must not paint');
  assert.strictEqual(state.serverCache.axial.img, freshImg, 'cache holds the newest frame');
});

test('test_response_is_dropped_when_mode_left_server', () => {
  const state = baseState();
  const s = makeSandbox({ state, vps: baseVps() });

  s.ctx.serverPlane('axial');
  state.mode = 'gl';                   // negotiate/fallback switched mid-flight
  s.createdImages[0].onload();

  assert.strictEqual(s.ctx2d.draws.length, 0, 'no paint after leaving server mode');
});

test('test_reset_server_cache_clears_frames_and_windows', () => {
  const state = baseState();
  const s = makeSandbox({ state, vps: baseVps() });

  s.ctx.serverPlane('axial');
  s.createdImages[0].onload();
  assert.notStrictEqual(state.serverCache.axial.img, null);

  s.ctx.resetServerCache();

  assert.strictEqual(state.serverCache.axial.img, null);
  assert.strictEqual(state.serverCache.axial.key, '');
  assert.strictEqual(state.serverRaw.axial.data, null);
});

/* -------------------------------------------------------------------------- */
/* Canvas 2D blit — aspect preservation                                       */
/* -------------------------------------------------------------------------- */

test('test_draw_server_image_letterboxes_a_square_plane_into_a_wide_canvas', () => {
  const state = baseState();
  const s = makeSandbox({ state, vps: baseVps(), imgWidth: 32, imgHeight: 32 });

  s.ctx.drawServerImage(s.ctx.vps.axial, { width: 32, height: 32 });

  const draw = s.ctx2d.draws[s.ctx2d.draws.length - 1];
  assert.ok(draw, 'drawImage must be called');
  // Canvas is 640x480; a square plane must be drawn square, centred.
  assert.strictEqual(draw.w, draw.h, 'a square plane must stay square');
  assert.strictEqual(draw.x, (640 - draw.w) / 2, 'horizontally centred');
  assert.strictEqual(draw.y, (480 - draw.h) / 2, 'vertically centred');
});

test('test_draw_server_image_records_the_image_rect_for_overlays', () => {
  const state = baseState();
  const s = makeSandbox({ state, vps: baseVps() });
  s.ctx.drawServerImage(s.ctx.vps.axial, { width: 32, height: 64 });

  const rect = s.ctx.vps.axial.imgRect;
  assert.ok(rect, 'the drawn rectangle must be published for SVG overlays');
  assert.strictEqual(rect.w, 240, '32:64 into 640x480 scales by height');
  assert.strictEqual(rect.h, 480);
  assert.strictEqual(rect.x, 200, 'letterbox offset is shared with the pointer mapping');
});

/* -------------------------------------------------------------------------- */
/* Measurement sampling — pending until the raw plane arrives                 */
/* -------------------------------------------------------------------------- */

test('test_server_raw_key_ignores_window_level', () => {
  const state = baseState();
  const s = makeSandbox({ state, vps: baseVps() });

  const key = s.ctx.serverRawKey('axial', 3);
  state.wl.c = 999; state.wl.w = 1234;   // HU is window-independent
  assert.strictEqual(s.ctx.serverRawKey('axial', 3), key, 'W/L must not invalidate HU data');
});

test('test_server_roi_stats_are_pending_without_fetched_voxels', () => {
  const state = baseState();
  const s = makeSandbox({ state, vps: baseVps() });

  const stats = s.ctx.serverRoiStats('axial', 10, 10, 3, 3, {});
  assert.strictEqual(stats.pending, true, 'must not fabricate statistics before data arrives');
  assert.ok(s.fetched.length >= 1, 'a raw plane must be requested on demand');
  assert.ok(s.fetched[0].includes('format=raw'), 'measurements use the raw endpoint');
  assert.ok(s.fetched[0].includes('index=4'), 'raw plane is for the displayed slice');
});

test('test_server_roi_stats_resolve_after_the_raw_plane_arrives', async () => {
  const state = baseState();
  // 32x32 plane of 100 HU with a 200 HU block covering the sampled ROI. The
  // block must span the whole region the sampler touches (the 2px radius about
  // the centre plus the rounding fringe), otherwise the assertion measures the
  // block edge rather than "the ROI reads the bright tissue".
  const data = new Float32Array(32 * 32).fill(100);
  for (let v = 12; v < 21; v++) for (let u = 12; u < 21; u++) data[v * 32 + u] = 200;
  const s = makeSandbox({
    state, vps: baseVps(), rawPlane: { rows: 32, cols: 32, data },
  });

  // First call is pending and kicks off the fetch...
  const pending = s.ctx.serverRoiStats('axial', 16, 16, 2, 2, { w: 32, h: 32 });
  assert.strictEqual(pending.pending, true);

  // ...then the fake fetch settles, populating the raw cache.
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(state.serverRaw.axial.rows, 32, 'raw plane must be cached');

  // A second call now computes real statistics from the float32 plane.
  const stats = s.ctx.serverRoiStats('axial', 16, 16, 2, 2, { w: 32, h: 32 });
  assert.strictEqual(stats.pending, undefined, 'resolved stats are not pending');
  assert.strictEqual(stats.mean, 200, 'the ROI must read the bright tissue, got ' + stats.mean);
  assert.strictEqual(stats.max, 200);
  assert.strictEqual(stats.min, 200, 'the whole ROI is inside the block');
});

test('test_server_roi_stats_do_not_apply_rescale_again', async () => {
  const state = baseState();
  // The server returns HU; a client-side slope/intercept would double-apply.
  const data = new Float32Array(32 * 32).fill(-1000);
  const s = makeSandbox({
    state, vps: baseVps(), rawPlane: { rows: 32, cols: 32, data },
  });

  s.ctx.serverRoiStats('axial', 16, 16, 1, 1, { w: 32, h: 32 });
  await new Promise((r) => setImmediate(r));

  const stats = s.ctx.serverRoiStats('axial', 16, 16, 1, 1, { w: 32, h: 32 });
  assert.strictEqual(Math.round(stats.mean), -1000, 'HU values must pass through untouched');
});

test('test_raw_plane_geometry_is_validated_before_use', async () => {
  const state = baseState();
  // Header advertises 32x32 float32 (4096 bytes) but the body is short: the
  // client must refuse it rather than index out of bounds.
  const s = makeSandbox({
    state, vps: baseVps(), rawPlane: { rows: 32, cols: 32, data: new Float32Array(16) },
  });

  s.ctx.serverRoiStats('axial', 16, 16, 2, 2, { w: 32, h: 32 });
  await new Promise((r) => setImmediate(r));

  assert.strictEqual(state.serverRaw.axial.data, null, 'a mis-sized plane must not be cached');
});

test('test_roi_mapping_reaches_the_last_plane_column', async () => {
  const state = baseState();
  // Every voxel is 0 except the LAST column, which is 500. Sampling the ROI at
  // the far right edge must see it. The old `dims / (vp-1)` scaling pushed that
  // index to 32 (past the end of a 32-wide plane) and the bounds check dropped
  // it, so the edge always read as background.
  const data = new Float32Array(32 * 32).fill(0);
  for (let v = 0; v < 32; v++) data[v * 32 + 31] = 500;
  const s = makeSandbox({
    state, vps: baseVps(), rawPlane: { rows: 32, cols: 32, data },
  });

  s.ctx.serverRoiStats('axial', 31, 16, 1, 1, { w: 32, h: 32 });
  await new Promise((r) => setImmediate(r));

  const stats = s.ctx.serverRoiStats('axial', 31, 16, 1, 1, { w: 32, h: 32 });
  assert.strictEqual(stats.max, 500, 'the last plane column must be reachable, got ' + stats.max);
});

test('test_roi_mapping_is_the_identity_for_a_full_resolution_plane', async () => {
  const state = baseState();
  // axial plane is 32x32 and the viewport is 32x32, so viewport pixel k must
  // land on plane pixel k exactly. Mark a single voxel and sample it.
  const data = new Float32Array(32 * 32).fill(0);
  data[16 * 32 + 16] = 777;
  const s = makeSandbox({
    state, vps: baseVps(), rawPlane: { rows: 32, cols: 32, data },
  });

  s.ctx.serverRoiStats('axial', 16, 16, 1, 1, { w: 32, h: 32 });
  await new Promise((r) => setImmediate(r));

  // cx=16,cy=16 with rx=ry=1 samples the plus-shape about (16,16); the marked
  // voxel is the centre and must be the maximum.
  const stats = s.ctx.serverRoiStats('axial', 16, 16, 1, 1, { w: 32, h: 32 });
  assert.strictEqual(stats.max, 777, 'centre pixel must map to itself, got ' + stats.max);
});

/* -------------------------------------------------------------------------- */
/* Teardown                                                                   */
/* -------------------------------------------------------------------------- */

test('test_release_server_resources_revokes_object_urls', () => {
  const state = baseState();
  const s = makeSandbox({ state, vps: baseVps() });

  state.serverFrames = { axial: { 3: { _paxsObjectUrl: 'blob:a' }, 4: { _paxsObjectUrl: 'blob:b' } } };
  state.serverRaw.axial = { key: 'k', rows: 32, cols: 32, data: new Float32Array(4) };

  s.ctx.releaseServerResources();

  assert.deepStrictEqual(s.revoked.sort(), ['blob:a', 'blob:b'], 'blob URLs must be revoked');
  // NOTE: the viewer reassigns `serverFrames` from inside the vm realm, so its
  // replacement object carries the sandbox's Object.prototype. `deepStrictEqual`
  // is prototype-sensitive across realms and would fail here for a correct
  // implementation, so assert on the observable shape instead.
  assert.deepStrictEqual(Object.keys(state.serverFrames), [], 'frame cache must be dropped');
  assert.strictEqual(state.serverRaw.axial.data, null, 'raw plane must be dropped');
});

test('test_release_server_resources_clears_the_series_cache', () => {
  const state = baseState();
  const s = makeSandbox({ state, vps: baseVps() });
  s.ctx.serverPlane('axial');
  s.createdImages[0].onload();

  s.ctx.releaseServerResources();

  assert.strictEqual(state.serverCache.axial.img, null);
  assert.strictEqual(state.serverCache.axial.key, '');
});

/* -------------------------------------------------------------------------- */
/* Structural contracts — the GPU/worker pipeline stays bypassed              */
/* -------------------------------------------------------------------------- */

const norm = SOURCE.replace(/\s+/g, ' ');

test('test_server_branch_skips_worker_and_gl_initialisation', () => {
  // The server branch must not create workers or upload a 3D texture.
  const marker = norm.indexOf('if (state.mode === \'server\') {');
  assert.ok(marker > 0, 'server branch must exist');
  const branch = norm.slice(marker, marker + 900);
  assert.ok(!branch.includes('initGl2()'), 'server branch must not initialise WebGL2');
  assert.ok(!branch.includes('initWorkerPool'), 'server branch must not start the worker pool');
  assert.ok(!branch.includes('/data'), 'server branch must not request the raw volume stream');
  assert.ok(!branch.includes('init3dEngine()'), 'server branch must not build a Three.js scene');
});

test('test_server_mode_never_restores_a_gpu_volume_from_cache', () => {
  assert.ok(
    norm.includes('if (state.mode !== \'server\' && state.volCache[seriesUid])'),
    'the in-memory volume cache must be bypassed in server mode'
  );
});

test('test_server_window_level_is_debounced_at_200ms', () => {
  assert.ok(norm.includes('SERVER_WL_DEBOUNCE_MS = 200'), 'debounce constant must be 200ms');
  assert.ok(norm.includes('serverWlTimer = setTimeout('), 'window changes must go through a timer');
  assert.ok(
    norm.includes('scheduleRender({ window: true })'),
    'W/L interactions must be distinguishable from slice changes'
  );
});

test('test_measurement_overlay_resolves_the_real_svg_id', () => {
  assert.ok(
    norm.includes("document.getElementById(vp.cv.replace('-cv', '-svg'))"),
    'SVG overlay must be resolved from the canvas id (vps has no `id`)'
  );
  assert.ok(!norm.includes("document.getElementById(vp.id + '-svg')"), 'the broken id lookup must be gone');
});

test('test_measurements_are_scoped_to_their_slice', () => {
  assert.ok(
    norm.includes('m.sliceIdx == null || m.sliceIdx === sliceNow'),
    'annotations must not bleed across slices'
  );
});

test('test_server_mode_never_reports_a_fabricated_zero_hu_mean', () => {
  assert.ok(norm.includes('Mean: sampling'), 'pending ROI stats must be labelled, not zeroed');
  assert.ok(norm.includes('roi.pending = !!stats.pending'), 'pending state must be captured on the measurement');
});

test('test_negotiation_reports_the_real_capability_score', () => {
  // A bare negotiate call resolves hybrid -> server, moving strong clients off
  // WebGL; the viewer must therefore always report its probed score.
  assert.ok(norm.includes('webgl_score='), 'negotiate must report a webgl score');
  assert.ok(norm.includes('probeClientScore'), 'the score must come from a real capability probe');
  assert.ok(
    norm.includes('if (!result) return false;'),
    'a failed negotiation must leave the locally chosen mode intact'
  );
});
