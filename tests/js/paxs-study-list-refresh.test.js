/**
 * H14 — centralized single-flight 401 refresh (study-list enhancement).
 *
 * `app/static/viewer/paxs-study-list-enhance.js` is an un-bundled classic
 * script, so the refresh wrapper is exercised by slicing the real functions out
 * of the shipped source and running them in a Node `vm` sandbox with stubbed
 * `fetch` / `localStorage` — the same approach the paxs-mpr tests use.
 *
 *   node --test "tests/js/*.test.js"
 *
 * Before the fix the guard read a cookie JavaScript cannot see (the session
 * cookie is HttpOnly), so the silent refresh never ran and a 15-minute access
 * token was simply never renewed. These tests pin the replacement contract:
 *
 *   * one refresh call for N concurrent 401s (single flight),
 *   * exactly one retry per request after a successful refresh,
 *   * the refresh endpoint itself is never retried (no loop),
 *   * a failed refresh surfaces the original 401 instead of retrying.
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');

const { viewerFile, extractFunction, runInSandbox } = require('./extract');

const REFRESH_PATH = '/api/v1/auth/refresh';
const source = fs.readFileSync(viewerFile('paxs-study-list-enhance.js'), 'utf8');
/** Whitespace-normalised source, so multi-line expressions match. */
const flat = source.replace(/\s+/g, ' ');

/** Drop block and whole-line comments (trailing comments are inert here). */
function stripComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/^\s*\/\/.*$/gm, ' ');
}

const isRefresh = (call) => call.url.includes(REFRESH_PATH);
const refreshCount = (calls) => calls.filter(isRefresh).length;
const apiCalls = (calls) => calls.filter((call) => !isRefresh(call));

/**
 * Run the *shipped* `paxsRefreshToken` + `paxsFetch` in a sandbox.
 *
 * `handler(url, opts)` returns the plan for one request:
 * `{ status, body, delay }` — `status` defaults to 200, `delay` to 1ms so the
 * three concurrent calls below are genuinely in flight together.
 */
function harness(handler) {
  const calls = [];
  const store = new Map();

  const fetchStub = (url, opts) => {
    const plan = handler(String(url), opts || {}) || {};
    calls.push({ url: String(url), opts: opts || {} });
    const status = plan.status === undefined ? 200 : plan.status;
    return new Promise((resolve) => {
      setTimeout(
        () =>
          resolve({
            ok: status >= 200 && status < 300,
            status,
            json: () => Promise.resolve(plan.body || {}),
          }),
        plan.delay === undefined ? 1 : plan.delay
      );
    });
  };

  const localStorageStub = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
  };

  const code = [
    'var paxsRefreshInFlight = null;', // hoisted by the IIFE in the real file
    extractFunction(source, 'paxsRefreshToken'),
    extractFunction(source, 'paxsFetch'),
  ].join('\n');

  const ctx = runInSandbox(code, { fetch: fetchStub, localStorage: localStorageStub });
  return { ctx, calls, store };
}

/* -------------------------------------------------------------------------- */
/* Single flight + exactly-one retry                                          */
/* -------------------------------------------------------------------------- */

test('test_h14_concurrent_401s_share_a_single_refresh_call', async () => {
  const token = 'refreshed-access-token';
  const { ctx, calls, store } = harness((url, opts) => {
    if (url.includes(REFRESH_PATH)) return { status: 200, body: { access_token: token } };
    // Only the retry carries the fresh token, so a 401 here means the wrapper
    // did not use the refreshed credential.
    const bearer = (opts.headers || {}).Authorization;
    return bearer === `Bearer ${token}` ? { status: 200, body: { ok: true } } : { status: 401 };
  });

  const results = await Promise.all([
    ctx.paxsFetch('/api/v1/studies'),
    ctx.paxsFetch('/api/v1/studies'),
    ctx.paxsFetch('/api/v1/studies'),
  ]);

  assert.deepStrictEqual(
    results.map((r) => r.status),
    [200, 200, 200],
    'every request must complete after the shared refresh'
  );
  assert.strictEqual(refreshCount(calls), 1, 'concurrent 401s must share one refresh call');
  assert.strictEqual(apiCalls(calls).length, 6, 'three requests = three attempts + three retries');
  assert.strictEqual(store.get('paxs_token'), token, 'the rotated token must be stored');
  for (const call of calls) {
    assert.strictEqual(call.opts.credentials, 'same-origin', 'the refresh cookie must ride along');
  }
});

test('test_h14_request_is_retried_at_most_once', async () => {
  const { ctx, calls } = harness((url) =>
    url.includes(REFRESH_PATH) ? { status: 200, body: { access_token: 'tok' } } : { status: 401 }
  );

  const res = await ctx.paxsFetch('/api/v1/studies');

  assert.strictEqual(res.status, 401, 'the original 401 is surfaced when the retry also fails');
  assert.strictEqual(apiCalls(calls).length, 2, 'initial attempt + exactly one retry');
  assert.strictEqual(refreshCount(calls), 1, 'a single refresh, not one per attempt');
});

test('test_h14_failed_refresh_surfaces_the_original_401', async () => {
  const { ctx, calls } = harness(() => ({ status: 401 }));

  const res = await ctx.paxsFetch('/api/v1/studies');

  assert.strictEqual(res.status, 401);
  assert.strictEqual(apiCalls(calls).length, 1, 'no retry without a fresh token');
  assert.strictEqual(refreshCount(calls), 1);
});

test('test_h14_refresh_endpoint_is_never_retried', async () => {
  const { ctx, calls } = harness(() => ({ status: 401 }));

  const res = await ctx.paxsFetch(REFRESH_PATH, { method: 'POST' });

  assert.strictEqual(res.status, 401);
  assert.strictEqual(calls.length, 1, 'the refresh call must not trigger a refresh loop');
});

/* -------------------------------------------------------------------------- */
/* Source contracts (anti-regression)                                         */
/* -------------------------------------------------------------------------- */

test('test_h14_httponly_cookie_guard_is_gone', () => {
  assert.ok(
    !flat.includes('paxsHasSessionCookie'),
    'the unreachable HttpOnly cookie guard must be removed'
  );
  assert.ok(
    !stripComments(source).includes('document.cookie'),
    'refresh must never be gated on a cookie JavaScript cannot read'
  );
});

test('test_h14_single_flight_and_loop_protection_are_structural', () => {
  assert.ok(flat.includes('if (!paxsRefreshInFlight)'), 'single-flight guard');
  assert.ok(flat.includes('return paxsRefreshInFlight;'), 'pending refresh is shared');
  assert.ok(
    flat.includes("String(url).indexOf('/api/v1/auth/refresh') !== -1"),
    'the refresh endpoint is recognised before retrying'
  );
  assert.ok(
    flat.includes('if (res.status !== 401 || retried || isRefreshCall) return res;'),
    'loop protection: 401 only, never twice, never the refresh call'
  );
  assert.ok(flat.includes('return attempt(true);'), 'retry helper is invoked once');
  assert.ok(flat.includes('paxsSilentRefresh()'), 'page-load silent refresh is still wired');
});
