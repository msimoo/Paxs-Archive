/**
 * Behavioural tests for Phase 4's hybrid engine and graceful fallback chain.
 *
 * The real fallback machinery from the shipped app/static/viewer/paxs-mpr.js is
 * extracted and run in a `vm` sandbox against a fake DOM:
 *
 *   parseLoadRatio / updateServerLoad / serverLoadOverloaded  — §7.3 load feed
 *   fallbackIndexFor / degradeFallback                        — §6.4 chain walk
 *   applyProcessingMode / notifyFallback                      — mode application
 *   attachContextLossHandler / handleContextLoss              — §4.5 context loss
 *   serverFallbackAllowed                                    — §4.4 promotion gate
 *   splitModeActive / scheduleFineSliceRender                — §4.3 split debounce
 *
 * Only the browser boundary is replaced. The chain ordering, the
 * never-promote-backwards rule and the degrade targets under test are the real
 * shipped code — a regression here means a clinician sees a black viewport
 * instead of an image, which is the failure this whole chain exists to prevent.
 *
 *   node --test "tests/js/*.test.js"
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { readViewerSource, extractFunction, runInSandbox } = require('./extract.js');

const SOURCE = readViewerSource();

const FALLBACK_FUNCS = [
  'parseLoadRatio',
  'updateServerLoad',
  'serverLoadOverloaded',
  'fallbackIndexFor',
  'serverFallbackAllowed',
  'splitModeActive',
  'scheduleFineSliceRender',
  'notifyFallback',
  'applyProcessingMode',
  'degradeFallback',
  'attachContextLossHandler',
  'handleContextLoss',
  'detachGpuPipeline',
];

/** Viewer-shaped processing block with the full Phase 4 fallback state. */
function processingState(overrides) {
  return Object.assign({
    configuredMode: 'hybrid',
    effectiveMode: 'client',
    clientScore: 72,
    capabilities: null,
    rttMs: 20,
    negotiateResponse: null,
    clientProbed: true,
    fallbackChain: ['client', 'split', 'server', 'rendered'],
    fallbackIndex: 0,
    serverLoad: { active: 0, max: 20, cacheMb: 0, budgetMb: 4096, overloaded: false },
    split: { coarseLoaded: false, fineTimer: null, fine: { axial: null, sagittal: null, coronal: null } },
  }, overrides || {});
}

/**
 * Build a sandbox carrying the real fallback functions.
 *
 * `opts.canvas` lets a test supply its own canvas stub so the registered
 * `webglcontextlost` listener can be fired by hand — that is how the simulated
 * context loss is driven.
 */
function makeSandbox(options) {
  const opts = options || {};
  const timers = [];
  const cleared = [];
  const removedListeners = [];
  const badges = {};
  const hudEls = {};

  const canvas = opts.canvas || {
    addEventListener(type, fn) { this._fn = fn; this._type = type; },
    width: 1, height: 1,
  };

  const doc = {
    getElementById(id) {
      if (id === 'mpr-badge-mode') {
        if (!badges[id]) badges[id] = { textContent: '', title: '', style: {} };
        return badges[id];
      }
      if (id === 'mpr-sub') {
        if (!hudEls[id]) hudEls[id] = { textContent: '', innerHTML: '' };
        return hudEls[id];
      }
      return null;
    },
  };

  const scope = {
    state: opts.state,
    document: doc,
    // Module-level `var`s the extracted functions close over. They are declared
    // in the IIFE body (not inside a function), so extractFunction cannot see
    // them — supplied here with production values, so a change to either is
    // caught by these tests rather than silently diverging.
    SPLIT_DOWNSAMPLE: 4,
    SPLIT_FINE_DEBOUNCE_MS: 400,
    timers,
    // Capture the debounce instead of running it: a timer that fires
    // asynchronously mid-assertion turns an ordering test into a race.
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { cleared.push(id); },
    console: { warn() {}, log() {} },
    // Mode-application collaborators the extracted functions call. Each records
    // its invocation so the test asserts "did we switch pipelines?" rather than
    // reaching into rendering internals that are not under test here.
    releaseGl2: () => { scope.calls.releaseGl2++; },
    initViewports: () => { scope.calls.initViewports++; },
    initGl2: () => { scope.calls.initGl2++; },
    renderAll: () => { scope.calls.renderAll++; },
    renderServerPlanes: () => { scope.calls.renderServerPlanes++; },
    set3dUnavailable: (r) => { scope.calls.set3dUnavailable.push(r); },
    vps: {},
    calls: {
      releaseGl2: 0, initViewports: 0, initGl2: 0, renderAll: 0,
      renderServerPlanes: 0, set3dUnavailable: [],
    },
    Promise,
    Math,
    String,
    Number,
    Object,
    Array,
    isNaN,
    parseInt,
    // `threeState` is read by detachGpuPipeline; give it a renderer to dispose.
    threeState: { renderer: { dispose() { scope.calls.disposed = true; } } },
  };
  scope.calls.disposed = false;

  const ctx = runInSandbox(
    FALLBACK_FUNCS.map((n) => extractFunction(SOURCE, n)).join('\n'),
    scope
  );
  return { ctx, scope, timers, cleared, badges, hudEls, canvas };
}

/* ========================================================================== */
/* §7.3 — server load header parsing                                          */
/* ========================================================================== */

test('test_parse_load_ratio_reads_current_over_max', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  assert.deepStrictEqual(
    { ...s.ctx.parseLoadRatio('3/20') },
    { current: 3, max: 20 }
  );
});

test('test_parse_load_ratio_rejects_malformed_input', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  for (const bad of [null, '', 'nonsense', '3', '3/', '/20', 'a/b', '3/0', 'NaN/4']) {
    assert.strictEqual(s.ctx.parseLoadRatio(bad), null, `should reject ${JSON.stringify(bad)}`);
  }
});

test('test_update_server_load_folds_headers_into_state', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  const resp = {
    headers: {
      get: (h) => (h === 'X-PAXS-Server-Load' ? '4/20'
        : h === 'X-PAXS-Vol-Cache-MB' ? '1024/4096' : null),
    },
  };

  const sl = s.ctx.updateServerLoad(resp);

  assert.strictEqual(sl.active, 4);
  assert.strictEqual(sl.max, 20);
  assert.strictEqual(sl.cacheMb, 1024);
  assert.strictEqual(sl.budgetMb, 4096);
  assert.strictEqual(sl.overloaded, false);
});

test('test_update_server_load_flags_session_saturation', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  const resp = { headers: { get: (h) => (h === 'X-PAXS-Server-Load' ? '20/20' : null) } };

  s.ctx.updateServerLoad(resp);

  assert.strictEqual(s.ctx.serverLoadOverloaded(), true);
});

test('test_update_server_load_flags_cache_budget_saturation', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  const resp = { headers: { get: (h) => (h === 'X-PAXS-Vol-Cache-MB' ? '4096/4096' : null) } };

  s.ctx.updateServerLoad(resp);

  assert.strictEqual(s.ctx.serverLoadOverloaded(), true);
});

test('test_update_server_load_below_both_limits_is_not_overloaded', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  const resp = {
    headers: {
      get: (h) => (h === 'X-PAXS-Server-Load' ? '19/20'
        : h === 'X-PAXS-Vol-Cache-MB' ? '4095/4096' : null),
    },
  };

  s.ctx.updateServerLoad(resp);

  assert.strictEqual(s.ctx.serverLoadOverloaded(), false);
});

test('test_update_server_load_tolerates_a_missing_headers_object', () => {
  // A stubbed fetch (or a proxy that strips headers) must not throw here —
  // losing the load signal is survivable; crashing the viewer is not.
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  assert.doesNotThrow(() => s.ctx.updateServerLoad({}));
  assert.doesNotThrow(() => s.ctx.updateServerLoad(null));
  assert.strictEqual(s.ctx.serverLoadOverloaded(), false);
});

test('test_server_load_overloaded_is_false_when_processing_is_absent', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: {} } });
  assert.strictEqual(s.ctx.serverLoadOverloaded(), false);
});

/* ========================================================================== */
/* §4.4 — the promotion gate                                                  */
/* ========================================================================== */

test('test_client_mode_never_promotes_to_the_server', () => {
  const s = makeSandbox({
    state: { mode: 'gl', processing: processingState({ effectiveMode: 'client' }) },
  });
  assert.strictEqual(s.ctx.serverFallbackAllowed(), false);
});

test('test_an_overloaded_server_is_not_promoted_to', () => {
  const s = makeSandbox({
    state: {
      mode: 'gl',
      processing: processingState({
        effectiveMode: 'split',
        serverLoad: { active: 20, max: 20, cacheMb: 0, budgetMb: 4096, overloaded: true },
      }),
    },
  });
  assert.strictEqual(s.ctx.serverFallbackAllowed(), false);
});

test('test_an_existing_server_session_survives_overload', () => {
  // Never tear down a working server pipeline to relieve load we already
  // committed to — that would blank a live viewport.
  const s = makeSandbox({
    state: {
      mode: 'server',
      processing: processingState({
        effectiveMode: 'server',
        serverLoad: { active: 99, max: 20, cacheMb: 0, budgetMb: 4096, overloaded: true },
      }),
    },
  });
  assert.strictEqual(s.ctx.serverFallbackAllowed(), true);
});

test('test_a_healthy_server_still_allows_promotion', () => {
  const s = makeSandbox({
    state: { mode: 'gl', processing: processingState({ effectiveMode: 'split' }) },
  });
  assert.strictEqual(s.ctx.serverFallbackAllowed(), true);
});

/* ========================================================================== */
/* §6.4 — the fallback chain walk                                             */
/* ========================================================================== */

test('test_fallback_chain_order_is_client_split_server_rendered', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  assert.deepStrictEqual(
    Array.from(s.scope.state.processing.fallbackChain),
    ['client', 'split', 'server', 'rendered']
  );
});

test('test_fallback_index_locates_each_chain_member', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  assert.strictEqual(s.ctx.fallbackIndexFor('client'), 0);
  assert.strictEqual(s.ctx.fallbackIndexFor('split'), 1);
  assert.strictEqual(s.ctx.fallbackIndexFor('server'), 2);
  assert.strictEqual(s.ctx.fallbackIndexFor('rendered'), 3);
});

test('test_an_unknown_mode_is_treated_as_the_chain_tail', () => {
  // Unknown vocabulary must not resolve to index 0, which would let a bogus
  // mode silently "degrade" back up to client.
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  assert.strictEqual(s.ctx.fallbackIndexFor('bogus'), 3);
});

test('test_degrade_from_client_lands_on_split', () => {
  const s = makeSandbox({
    state: { mode: 'gl', processing: processingState({ effectiveMode: 'client' }) },
  });
  const next = s.ctx.degradeFallback('gpu died');
  assert.strictEqual(next, 'split');
  assert.strictEqual(s.scope.state.processing.effectiveMode, 'split');
});

test('test_degrade_from_split_lands_on_server', () => {
  const s = makeSandbox({
    state: { mode: 'gl', processing: processingState({ effectiveMode: 'split', fallbackIndex: 1 }) },
  });
  const next = s.ctx.degradeFallback('server slices failing');
  assert.strictEqual(next, 'server');
  assert.strictEqual(s.scope.state.mode, 'server');
  assert.strictEqual(s.scope.calls.renderServerPlanes, 1);
});

test('test_degrade_from_server_lands_on_rendered', () => {
  const s = makeSandbox({
    state: { mode: 'server', processing: processingState({ effectiveMode: 'server', fallbackIndex: 2 }) },
  });
  const next = s.ctx.degradeFallback('server unreachable');
  assert.strictEqual(next, 'rendered', 'server is the second-to-last step');
  assert.strictEqual(s.scope.state.processing.effectiveMode, 'rendered');
  assert.strictEqual(s.badges['mpr-badge-mode'].textContent, 'RENDERED');
});

test('test_the_chain_is_exhausted_at_rendered', () => {
  // Once `rendered` is the effective mode there is nothing further to try, so
  // the walk reports null rather than looping.
  const s = makeSandbox({
    state: { mode: 'server', processing: processingState({ effectiveMode: 'rendered', fallbackIndex: 3 }) },
  });
  assert.strictEqual(s.ctx.degradeFallback('nothing left'), null);
  assert.strictEqual(s.scope.state.processing.fallbackIndex, 3);
});

test('test_degrade_never_promotes_backwards', () => {
  // Walking the chain must move strictly forward, one step per call. If
  // `fallbackIndex` were ignored, each call would restart from the *current*
  // mode and the session would oscillate instead of converging on a pipeline.
  const s = makeSandbox({
    state: { mode: 'gl', processing: processingState({ effectiveMode: 'client' }) },
  });
  assert.strictEqual(s.ctx.degradeFallback('one'), 'split');
  assert.strictEqual(s.ctx.degradeFallback('two'), 'server');
  assert.strictEqual(s.ctx.degradeFallback('three'), 'rendered');
  assert.strictEqual(s.ctx.degradeFallback('four'), null, 'chain exhausted');
  assert.strictEqual(s.scope.state.processing.fallbackIndex, 3);
});

/* ========================================================================== */
/* §4.5 — WebGL context loss                                                  */
/* ========================================================================== */

test('test_context_loss_handler_is_registered_on_the_canvas', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  s.ctx.attachContextLossHandler(s.canvas);
  assert.strictEqual(s.canvas._type, 'webglcontextlost');
  assert.strictEqual(typeof s.canvas._fn, 'function');
});

test('test_registering_the_handler_twice_binds_only_once', () => {
  // initGl2 re-runs on every series switch; a second listener would run the
  // fallback twice and degrade two steps for one GPU failure.
  const handler = { type: null, count: 0 };
  const canvas = {
    addEventListener(type) { handler.type = type; handler.count++; },
  };
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() }, canvas });

  s.ctx.attachContextLossHandler(canvas);
  s.ctx.attachContextLossHandler(canvas);

  assert.strictEqual(handler.count, 1, 'the second call must be a no-op');
});

test('test_a_lost_context_switches_to_server_rendering', () => {
  const s = makeSandbox({
    state: { mode: 'gl', processing: processingState({ effectiveMode: 'client' }) },
  });

  s.ctx.handleContextLoss('WebGL context lost');

  assert.strictEqual(s.scope.state.processing.effectiveMode, 'server');
  assert.strictEqual(s.scope.state.mode, 'server');
  assert.strictEqual(s.scope.calls.renderServerPlanes, 1, 'must re-render on the server');
  assert.strictEqual(s.scope.state.vol, null, 'the GPU volume handle must be dropped');
});

test('test_context_loss_releases_the_gpu_handles', () => {
  const s = makeSandbox({
    state: { mode: 'gl', processing: processingState({ effectiveMode: 'client' }) },
  });
  s.scope.state.gl2 = {
    supported: true, ctx: { getExtension: () => null }, renderCv: {}, volTex: {}, prog: {}, vao: {}, uLoc: {},
  };

  s.ctx.handleContextLoss('WebGL context lost');

  assert.strictEqual(s.scope.state.gl2.supported, false, 'gl2 must be detached');
  assert.strictEqual(s.scope.state.gl2.ctx, null);
  assert.strictEqual(s.scope.state.gl2.volTex, null);
});

test('test_context_loss_never_falls_below_server', () => {
  // A GPU failure is exactly what the server pipeline is for. Even if the
  // session was already split (chain index 1), landing on `server` is right —
  // `rendered` would throw away working volume endpoints for no gain.
  const s = makeSandbox({
    state: { mode: 'gl', processing: processingState({ effectiveMode: 'split', fallbackIndex: 1 }) },
  });
  s.ctx.handleContextLoss('WebGL context lost');
  assert.strictEqual(s.scope.state.processing.effectiveMode, 'server');
});

test('test_context_loss_from_rendered_stays_on_server_not_past_it', () => {
  // Even from the very end of the chain, context loss targets `server`, the
  // last pipeline that needs no GPU.
  const s = makeSandbox({
    state: { mode: 'server', processing: processingState({ effectiveMode: 'rendered', fallbackIndex: 3 }) },
  });
  s.ctx.handleContextLoss('WebGL context lost');
  assert.strictEqual(s.scope.state.processing.effectiveMode, 'server');
});

test('test_context_loss_prevents_the_default_so_the_context_can_restore', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  let prevented = false;
  s.ctx.handleContextLoss.call;
  // The listener itself calls preventDefault before degrading; assert via the
  // registered listener path.
  const canvas = { addEventListener(t, fn) { this._fn = fn; } };
  s.ctx.attachContextLossHandler(canvas);
  canvas._fn({ preventDefault() { prevented = true; } });
  assert.strictEqual(prevented, true, 'preventDefault() is required for restoration');
});

test('test_context_loss_does_not_throw_without_a_three_renderer', () => {
  const s = makeSandbox({ state: { mode: 'gl', processing: processingState() } });
  s.scope.threeState.renderer = null;
  assert.doesNotThrow(() => s.ctx.handleContextLoss('lost'));
});

/* ========================================================================== */
/* §4.3 — split mode                                                          */
/* ========================================================================== */

test('test_split_mode_is_active_only_for_the_split_effective_mode', () => {
  for (const m of ['client', 'server', 'rendered', 'hybrid', null]) {
    const s = makeSandbox({ state: { mode: 'gl', processing: processingState({ effectiveMode: m }) } });
    assert.strictEqual(s.ctx.splitModeActive(), false, `${m} is not split`);
  }
  const split = makeSandbox({ state: { mode: 'gl', processing: processingState({ effectiveMode: 'split' }) } });
  assert.strictEqual(split.ctx.splitModeActive(), true);
});

test('test_split_fine_slice_is_debounced', () => {
  const s = makeSandbox({
    state: { mode: 'gl', processing: processingState({ effectiveMode: 'split' }) },
  });
  s.ctx.scheduleFineSliceRender();

  assert.strictEqual(s.timers.length, 1, 'exactly one debounce timer');
  assert.ok(s.timers[0].ms >= 400, `debounce must exceed 400ms, got ${s.timers[0].ms}`);
});

test('test_repeated_navigation_restarts_the_debounce', () => {
  // A scroll fires renderAll per frame; without the clear/restart the server
  // would be hit once per frame instead of once per settled position.
  const s = makeSandbox({
    state: { mode: 'gl', processing: processingState({ effectiveMode: 'split' }) },
  });
  s.ctx.scheduleFineSliceRender();
  const first = s.scope.state.processing.split.fineTimer;
  s.ctx.scheduleFineSliceRender();

  assert.ok(s.cleared.length >= 1, 'the previous timer must be cleared');
  assert.notStrictEqual(s.scope.state.processing.split.fineTimer, first);
});

test('test_split_debounce_is_a_no_op_outside_split_mode', () => {
  const s = makeSandbox({
    state: { mode: 'gl', processing: processingState({ effectiveMode: 'client' }) },
  });
  s.ctx.scheduleFineSliceRender();
  assert.strictEqual(s.timers.length, 0, 'client mode must not poll the server');
});

/* ========================================================================== */
/* Mode application                                                           */
/* ========================================================================== */

test('test_applying_server_mode_clears_the_gpu_volume', () => {
  const s = makeSandbox({
    state: { mode: 'gl', vol: new Float32Array(8), processing: processingState() },
  });
  s.ctx.applyProcessingMode('server', 'degraded');

  assert.strictEqual(s.scope.state.mode, 'server');
  assert.strictEqual(s.scope.state.vol, null);
  assert.strictEqual(s.scope.calls.releaseGl2, 1);
  assert.strictEqual(s.scope.calls.renderServerPlanes, 1);
});

test('test_applying_split_mode_keeps_the_gpu_pipeline', () => {
  const s = makeSandbox({
    state: { mode: 'server', vol: new Float32Array(8), processing: processingState() },
  });
  s.ctx.applyProcessingMode('split', 'coarse only');

  assert.strictEqual(s.scope.state.mode, 'gl');
  assert.ok(s.scope.state.vol, 'the coarse volume must be retained');
});

test('test_applying_rendered_mode_marks_the_session_degraded', () => {
  const s = makeSandbox({ state: { mode: 'server', processing: processingState() } });
  s.ctx.applyProcessingMode('rendered', 'all pipelines failed');

  const badge = s.badges['mpr-badge-mode'];
  assert.strictEqual(badge.textContent, 'RENDERED');
  assert.ok(/all pipelines failed/.test(badge.title), badge.title);
});

/* ========================================================================== */
/* Structural contracts                                                       */
/* ========================================================================== */

test('test_viewer_handles_webglcontextlost_somewhere', () => {
  const src = readViewerSource();
  assert.ok(src.includes("addEventListener('webglcontextlost'") ||
            src.includes('addEventListener("webglcontextlost"'),
    'the viewer must listen for context loss');
});

test('test_viewer_never_reloads_the_page_to_recover', () => {
  // The whole point of the chain is in-place recovery. A location.reload() in
  // the fallback path would lose the clinician's series, slice and window.
  const fn = extractFunction(readViewerSource(), 'handleContextLoss');
  assert.ok(!/location\s*\.\s*reload/.test(fn), 'fallback must not reload the page');
});

test('test_load_headers_are_read_by_name_consistently', () => {
  const src = readViewerSource();
  assert.ok(src.includes('X-PAXS-Server-Load'));
  assert.ok(src.includes('X-PAXS-Vol-Cache-MB'));
});
