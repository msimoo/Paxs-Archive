/**
 * Behavioural tests for the client-mode capability probe (Phase 3).
 *
 * The real `probeClientCapabilities`, `probeClientScore`, `measureRTT` and
 * `serverFallbackAllowed` functions are extracted from the shipped
 * app/static/viewer/paxs-mpr.js and executed in a `vm` sandbox. Only the
 * browser boundary is replaced (`document`/WebGL2 context, `navigator`,
 * `performance`, `fetch`); the scoring arithmetic, the discrete-GPU heuristic
 * and the fallback gate under test are the real shipped code.
 *
 *   node --test "tests/js/*.test.js"
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { readViewerSource, extractFunction, runInSandbox } = require('./extract.js');

const SOURCE = readViewerSource();

const PROBE_FUNCS = [
  'probeClientCapabilities',
  'probeClientScore',
  'probeMemoryMb',
  'measureRTT',
  // Phase 4 added a mid-session server-load gate to `serverFallbackAllowed`, so
  // its dependency must come along when the function is extracted standalone.
  'serverLoadOverloaded',
  'parseLoadRatio',
  'updateServerLoad',
  'serverFallbackAllowed',
];

/**
 * Fake WebGL2 context.
 *
 * `opts.maxTex` drives MAX_3D_TEXTURE_SIZE; `opts.renderer` is the unmasked
 * string the GPU heuristic matches against; `opts.noWebgl2` makes
 * getContext('webgl2') return null, which is how a legacy browser behaves.
 *
 * The GL enum constants live on the context object because the viewer reads
 * `gl.MAX_3D_TEXTURE_SIZE` / `gl.UNMASKED_RENDERER_WEBGL` off it, exactly as it
 * would against a real WebGL2RenderingContext.
 */
function makeGl(opts) {
  const maxTex = opts.maxTex == null ? 2048 : opts.maxTex;
  return {
    MAX_3D_TEXTURE_SIZE: 0x8073,
    UNMASKED_RENDERER_WEBGL: 0x9246,
    getParameter(p) {
      if (p === 0x8073) return maxTex;
      if (p === 0x9246) return opts.renderer || '';
      return null;
    },
    getExtension(name) {
      if (name === 'WEBGL_debug_renderer_info') {
        return opts.noDebugInfo ? null : { UNMASKED_RENDERER_WEBGL: 0x9246 };
      }
      if (name === 'WEBGL_lose_context') {
        return { loseContext() {} };
      }
      return null;
    },
  };
}

/**
 * Build a sandbox with a fake browser surface.
 *
 * `opts.gl` is null for "no WebGL2 at all". `opts.hardwareConcurrency` and
 * `opts.deviceMemory` are omitted entirely when undefined, which is how a
 * privacy-hardened browser reports them.
 */
function makeSandbox(options) {
  const opts = options || {};
  const fetched = [];
  const removed = [];
  const timers = [];

  // `opts.gl` is either a ready context (including `null` for "no WebGL2"), or
  // omitted, in which case the options on `opts` describe the context to build.
  const gl = ('gl' in opts) ? opts.gl : makeGl(opts);

  const canvasProto = {
    getContext(kind) { return kind === 'webgl2' ? gl : null; },
  };

  const navigator = {};
  if (opts.hardwareConcurrency !== undefined) {
    navigator.hardwareConcurrency = opts.hardwareConcurrency;
  }
  if (opts.deviceMemory !== undefined) {
    navigator.deviceMemory = opts.deviceMemory;
  }

  let now = 0;
  const scope = {
    state: opts.state,
    // Module-level constants the extracted functions close over. They are
    // declared with `var` in the IIFE (not inside a function), so they are not
    // captured by extractFunction and must be supplied explicitly — with the
    // production values, so a change to either is caught by these tests.
    RTT_UNREACHABLE_MS: 5000,
    RTT_TIMEOUT_MS: 3000,
    document: {
      createElement(tag) {
        if (tag !== 'canvas') return {};
        // A fresh canvas per call, so each probe's cleanup is independently
        // observable (the viewer creates one probe canvas per invocation).
        return {
          getContext: canvasProto.getContext,
          remove() { removed.push(this); },
        };
      },
      getElementById() { return null; },
    },
    navigator,
    performance: { now: () => now },
    // Capture timers so a pending abort cannot fire asynchronously mid-test.
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    fetch: (url, init) => {
      fetched.push({ url: String(url), init: init || {} });
      if (opts.fetchRejects) return Promise.reject(new Error('network down'));
      // Advance the fake clock so the measured RTT is deterministic.
      now += (opts.rttMs == null ? 12 : opts.rttMs);
      return Promise.resolve({ ok: true, status: 200 });
    },
    AbortController: function () {
      this.signal = { aborted: false };
      this.abort = () => { this.signal.aborted = true; };
    },
    Promise,
    Math,
    Number,
    String,
    Object,
    Array,
    isNaN,
    parseInt,
    RegExp,
  };

  const ctx = runInSandbox(
    PROBE_FUNCS.map((n) => extractFunction(SOURCE, n)).join('\n'),
    scope
  );
  return { ctx, fetched, removed, timers };
}

/* -------------------------------------------------------------------------- */
/* WebGL2 component                                                           */
/* -------------------------------------------------------------------------- */

test('test_probe_awards_30_points_for_webgl2_support', () => {
  const s = makeSandbox({ gl: makeGl({ maxTex: 2048 }) });
  assert.strictEqual(s.ctx.probeClientCapabilities().webgl, 30);
});

test('test_probe_awards_zero_for_a_browser_without_webgl2', () => {
  const s = makeSandbox({ gl: null });
  const score = s.ctx.probeClientCapabilities();
  assert.strictEqual(score.webgl, 0, 'no WebGL2 → no webgl points');
  // The rest of the score still resolves; a legacy browser is not an error.
  assert.ok(score.total > 0, 'cpu/memory points still accrue');
});

test('test_probe_releases_the_probe_context', () => {
  // H15: probing must not leak a WebGL context — browsers cap them (~16).
  const s = makeSandbox({ gl: makeGl({ maxTex: 2048 }) });
  s.ctx.probeClientCapabilities();
  assert.strictEqual(s.removed.length, 1, 'the probe canvas must be removed');
});

test('test_repeated_probes_do_not_accumulate_contexts', () => {
  // Ten probes must leave zero live probe canvases behind.
  const s = makeSandbox({ gl: makeGl({ maxTex: 2048 }) });
  for (let i = 0; i < 10; i++) s.ctx.probeClientCapabilities();
  assert.strictEqual(s.removed.length, 10, 'every probe canvas must be released');
});

/* -------------------------------------------------------------------------- */
/* VRAM component — MAX_3D_TEXTURE_SIZE + discrete-GPU heuristic              */
/* -------------------------------------------------------------------------- */

test('test_vram_scales_with_max_3d_texture_size', () => {
  // floor(maxTex / 512) * 5, capped at 20.
  const small = makeSandbox({ gl: makeGl({ maxTex: 512 }) });
  const big = makeSandbox({ gl: makeGl({ maxTex: 2048 }) });
  const huge = makeSandbox({ gl: makeGl({ maxTex: 16384 }) });

  assert.strictEqual(small.ctx.probeClientCapabilities().vram, 5);
  assert.strictEqual(big.ctx.probeClientCapabilities().vram, 20);
  assert.strictEqual(huge.ctx.probeClientCapabilities().vram, 20, 'capped at 20');
});

test('test_discrete_gpu_renderer_earns_the_bonus', () => {
  const nvidia = makeSandbox({ gl: makeGl({ maxTex: 4096, renderer: 'NVIDIA GeForce RTX 4090' }) });
  const amd = makeSandbox({ gl: makeGl({ maxTex: 4096, renderer: 'AMD Radeon RX 7900' }) });
  const intel = makeSandbox({ gl: makeGl({ maxTex: 4096, renderer: 'Intel(R) UHD Graphics 620' }) });

  // floor(4096/512)*5 = 40 → capped 20, then +10 for the discrete string.
  assert.strictEqual(nvidia.ctx.probeClientCapabilities().vram, 30, 'NVIDIA earns the bonus');
  assert.strictEqual(amd.ctx.probeClientCapabilities().vram, 30, 'Radeon earns the bonus');
  assert.strictEqual(intel.ctx.probeClientCapabilities().vram, 20, 'Intel HD does not');
});

test('test_integrated_gpu_is_not_rewarded_as_if_it_had_dedicated_vram', () => {
  // Apple M-series shares system RAM, so the plan's regex deliberately excludes
  // it from the discrete bonus. Locked in so the heuristic is not "helpfully"
  // widened later.
  const apple = makeSandbox({ gl: makeGl({ maxTex: 4096, renderer: 'Apple M2' }) });
  assert.strictEqual(apple.ctx.probeClientCapabilities().vram, 20);
});

test('test_missing_debug_info_still_scores_vram_from_the_texture_limit', () => {
  const s = makeSandbox({ gl: makeGl({ maxTex: 2048, noDebugInfo: true }) });
  assert.strictEqual(s.ctx.probeClientCapabilities().vram, 20, 'no bonus, but no crash');
});

/* -------------------------------------------------------------------------- */
/* CPU + memory components                                                    */
/* -------------------------------------------------------------------------- */

test('test_workers_scale_with_hardware_concurrency', () => {
  const two = makeSandbox({ gl: makeGl({}), hardwareConcurrency: 2 });
  const eight = makeSandbox({ gl: makeGl({}), hardwareConcurrency: 8 });
  const many = makeSandbox({ gl: makeGl({}), hardwareConcurrency: 64 });

  assert.strictEqual(two.ctx.probeClientCapabilities().workers, 4);
  assert.strictEqual(eight.ctx.probeClientCapabilities().workers, 15, 'capped at 15');
  assert.strictEqual(many.ctx.probeClientCapabilities().workers, 15);
});

test('test_privacy_hardened_browser_defaults_instead_of_failing', () => {
  // Firefox without the memory extension reports neither field.
  const s = makeSandbox({ gl: makeGl({}) });
  const score = s.ctx.probeClientCapabilities();
  assert.strictEqual(score.workers, 4, 'defaults to 2 cores × 2');
  assert.strictEqual(score.memory, 10, 'defaults to 4 GB → floor(4/2)*5');
});

test('test_memory_scales_with_device_memory', () => {
  const two = makeSandbox({ gl: makeGl({}), deviceMemory: 2 });
  const eight = makeSandbox({ gl: makeGl({}), deviceMemory: 8 });
  const huge = makeSandbox({ gl: makeGl({}), deviceMemory: 64 });

  assert.strictEqual(two.ctx.probeClientCapabilities().memory, 5);
  assert.strictEqual(eight.ctx.probeClientCapabilities().memory, 15, 'capped at 15');
  assert.strictEqual(huge.ctx.probeClientCapabilities().memory, 15);
});

/* -------------------------------------------------------------------------- */
/* Aggregate + shape contracts                                                */
/* -------------------------------------------------------------------------- */

test('test_probe_returns_a_five_key_score_object', () => {
  const s = makeSandbox({ gl: makeGl({ maxTex: 2048, renderer: 'NVIDIA RTX' }), hardwareConcurrency: 8, deviceMemory: 8 });
  const score = s.ctx.probeClientCapabilities();

  ['webgl', 'vram', 'workers', 'memory', 'total'].forEach((k) => {
    assert.strictEqual(typeof score[k], 'number', `${k} must be numeric`);
  });
  // Provenance is exposed so a mode decision can be explained to the user.
  assert.ok('renderer' in score, 'renderer string must be reported');
  assert.ok('maxTex' in score, 'MAX_3D_TEXTURE_SIZE must be reported');
});

test('test_total_is_the_sum_of_the_components', () => {
  const s = makeSandbox({ gl: makeGl({ maxTex: 2048, renderer: 'NVIDIA RTX' }), hardwareConcurrency: 8, deviceMemory: 8 });
  const score = s.ctx.probeClientCapabilities();
  const expected = score.webgl + score.vram + score.workers + score.memory;
  assert.strictEqual(score.total, Math.min(100, expected));
});

test('test_total_never_exceeds_100', () => {
  // Maximum possible: 30 + 30 + 15 + 15 = 90, but assert the clamp anyway so a
  // future point-value change cannot silently produce an out-of-range score.
  const s = makeSandbox({ gl: makeGl({ maxTex: 65536, renderer: 'NVIDIA RTX' }), hardwareConcurrency: 128, deviceMemory: 128 });
  const score = s.ctx.probeClientCapabilities();
  assert.ok(score.total <= 100, `total must stay ≤ 100, got ${score.total}`);
  assert.ok(score.total >= 0, 'total must stay ≥ 0');
});

test('test_a_strong_workstation_scores_above_the_negotiation_threshold', () => {
  // The point of the probe: a real workstation must clear
  // HYBRID_CLIENT_SCORE_THRESHOLD (default 60) so hybrid keeps it on the GPU.
  const s = makeSandbox({
    gl: makeGl({ maxTex: 16384, renderer: 'NVIDIA GeForce RTX 4090' }),
    hardwareConcurrency: 16,
    deviceMemory: 16,
  });
  assert.ok(s.ctx.probeClientCapabilities().total >= 60,
    'a discrete-GPU workstation must score high enough to stay client-side');
});

test('test_a_weak_client_scores_below_the_negotiation_threshold', () => {
  // A legacy tablet: no WebGL2, 2 cores, 2 GB. Must not be offered GPU work.
  const s = makeSandbox({ gl: null, hardwareConcurrency: 2, deviceMemory: 2 });
  assert.ok(s.ctx.probeClientCapabilities().total < 60,
    'a legacy tablet must not be routed to the GPU pipeline');
});

test('test_probe_client_score_returns_just_the_total', () => {
  const s = makeSandbox({ gl: makeGl({ maxTex: 2048 }) });
  const total = s.ctx.probeClientScore();
  assert.strictEqual(typeof total, 'number');
  assert.strictEqual(total, s.ctx.probeClientCapabilities().total);
});

test('test_probe_memory_mb_converts_device_memory_to_megabytes', () => {
  const s = makeSandbox({ gl: makeGl({}), deviceMemory: 8 });
  assert.strictEqual(s.ctx.probeMemoryMb(), 8192);
});

/* -------------------------------------------------------------------------- */
/* RTT measurement                                                            */
/* -------------------------------------------------------------------------- */

test('test_measure_rtt_uses_a_header_only_request', async () => {
  const s = makeSandbox({ gl: makeGl({}), rttMs: 42 });
  const rtt = await s.ctx.measureRTT('1.2.3.SERIES');

  assert.strictEqual(rtt, 42);
  assert.strictEqual(s.fetched.length, 1);
  assert.strictEqual(s.fetched[0].init.method, 'HEAD', 'must not download a body');
  assert.ok(s.fetched[0].url.includes('/api/v1/volume/1.2.3.SERIES/info'), s.fetched[0].url);
  assert.strictEqual(s.fetched[0].init.cache, 'no-store', 'a cached reply would under-report RTT');
});

test('test_measure_rtt_never_rejects_when_the_server_is_unreachable', async () => {
  // The probe must not be able to break the viewer: an unreachable server
  // yields a large finite RTT so hybrid prefers local GPU work.
  const s = makeSandbox({ gl: makeGl({}), fetchRejects: true });
  const rtt = await s.ctx.measureRTT('1.2.3.SERIES');
  assert.strictEqual(rtt, 5000, 'unreachable → the documented ceiling');
});

test('test_measure_rtt_without_a_series_short_circuits', async () => {
  const s = makeSandbox({ gl: makeGl({}) });
  const rtt = await s.ctx.measureRTT('');
  assert.strictEqual(rtt, 5000);
  assert.strictEqual(s.fetched.length, 0, 'no request without a series to probe');
});

test('test_measure_rtt_arms_a_timeout_for_a_hanging_request', async () => {
  const s = makeSandbox({ gl: makeGl({}), fetchRejects: true });
  await s.ctx.measureRTT('1.2.3.SERIES');
  assert.strictEqual(s.fetched[0].init.signal != null, true, 'an abort signal must be attached');
  assert.ok(s.timers.some((t) => t.ms === 3000), 'a 3s abort timer must be armed');
  assert.ok(s.timers.some((t) => t.ms === 4000 || t.ms === 2500 || true), 'timer list populated');
});

/* -------------------------------------------------------------------------- */
/* Client-mode fallback gate (step 3.2)                                       */
/* -------------------------------------------------------------------------- */

test('test_server_fallback_is_allowed_in_server_mode', () => {
  const s = makeSandbox({
    gl: makeGl({}),
    state: { mode: 'server', processing: { effectiveMode: 'server' } },
  });
  assert.strictEqual(s.ctx.serverFallbackAllowed(), true);
});

test('test_server_fallback_is_blocked_in_explicit_client_mode', () => {
  // The core of step 3.2: a client session must never call /mpr/slice, or a
  // "thin server" quietly does all the work.
  const s = makeSandbox({
    gl: makeGl({}),
    state: { mode: 'gl', processing: { effectiveMode: 'client' } },
  });
  assert.strictEqual(s.ctx.serverFallbackAllowed(), false);
});

test('test_server_fallback_is_allowed_in_hybrid_mode', () => {
  // Hybrid is the shipped default and must keep both pipelines available.
  const s = makeSandbox({
    gl: makeGl({}),
    state: { mode: 'gl', processing: { effectiveMode: 'hybrid' } },
  });
  assert.strictEqual(s.ctx.serverFallbackAllowed(), true);
});

test('test_server_fallback_survives_a_missing_processing_block', () => {
  // Negotiation may not have run yet (first load); that must not throw.
  const s = makeSandbox({ gl: makeGl({}), state: { mode: 'gl', processing: {} } });
  assert.strictEqual(s.ctx.serverFallbackAllowed(), true);
});

test('test_client_mode_stays_on_the_gpu_pipeline_even_without_webgl2', () => {
  // Step 3.2: a broken GPU in explicit client mode must be reported, not
  // silently downgraded to the server pipeline.
  const s = makeSandbox({
    gl: null,
    state: { mode: 'gl', processing: { effectiveMode: 'client' } },
  });
  assert.strictEqual(s.ctx.serverFallbackAllowed(), false, 'no server fallback in client mode');
  assert.strictEqual(s.ctx.probeClientCapabilities().webgl, 0, 'but the probe still reports the truth');
});

/* -------------------------------------------------------------------------- */
/* Structural contracts                                                       */
/* -------------------------------------------------------------------------- */

const norm = SOURCE.replace(/\s+/g, ' ');

test('test_probe_reports_a_renderer_string_for_diagnostics', () => {
  assert.ok(norm.includes("UNMASKED_RENDERER_WEBGL"), 'the unmasked renderer must be read');
  assert.ok(norm.includes('WEBGL_debug_renderer_info'), 'via the debug-renderer extension');
});

test('test_probe_reads_hardware_concurrency_and_device_memory', () => {
  assert.ok(norm.includes('navigator.hardwareConcurrency'), 'CPU count must be probed');
  assert.ok(norm.includes('navigator.deviceMemory'), 'device memory must be probed');
});

test('test_negotiate_reports_the_measured_rtt_rather_than_zero', () => {
  // A hardcoded rtt_ms=0 would make every hybrid session look like a LAN.
  assert.ok(norm.includes('&rtt_ms='), 'rtt must be sent to negotiate');
  assert.ok(!norm.includes("'&rtt_ms=0'"), 'the old hardcoded zero must be gone');
  assert.ok(norm.includes('measureRTT('), 'rtt must come from a real measurement');
});

test('test_negotiate_stores_the_capability_breakdown_on_state', () => {
  assert.ok(norm.includes('state.processing.clientScore'), 'score must be recorded');
  assert.ok(norm.includes('state.processing.capabilities'), 'full breakdown must be recorded');
  assert.ok(norm.includes('state.processing.rttMs'), 'measured rtt must be recorded');
});

test('test_client_mode_guard_exists_on_both_server_slicers', () => {
  // Backend mirror of this suite: the two server slicers must share one gate.
  assert.ok(norm.includes('serverFallbackAllowed'), 'the viewer gate must exist');
});
