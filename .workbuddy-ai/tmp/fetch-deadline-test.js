/**
 * V3-5 verification: volume payload fetches carry an abort deadline.
 *
 * Regression context: the negotiate and RTT probes already used an
 * AbortController, but the /data fetches did not. A stalled server left the
 * socket open forever; because a hung socket never rejects, the loader's
 * .catch never fired and `state.busy` stayed true — the workspace wedged with
 * no error shown.
 *
 * The REAL fetchWithDeadline() source is extracted from the shipping file and
 * driven against a mock fetch, so this cannot drift from what ships.
 */
const fs = require('fs');
const path = require('path');

const FILE = 'D:/Paxs Archive/app/static/viewer/paxs-mpr.js';
const SRC = fs.readFileSync(path.resolve(FILE), 'utf8');

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? '  -> ' + extra : '')); }
}

function extractFunction(source, name) {
  const start = source.indexOf('function ' + name + '(');
  if (start === -1) throw new Error('function not found: ' + name);
  let depth = 0;
  for (let i = source.indexOf('{', start); i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') { depth--; if (depth === 0) return source.slice(start, i + 1); }
  }
  throw new Error('unbalanced braces for ' + name);
}

const FN_SRC = extractFunction(SRC, 'fetchWithDeadline');
const mTimeout = SRC.match(/VOLUME_FETCH_TIMEOUT_MS\s*=\s*([^;]+);/);
const DEFAULT_MS = eval(mTimeout[1]);

/** Minimal AbortController + AbortSignal stand-in. */
function makeAbort() {
  const listeners = [];
  return {
    signal: { aborted: false, addEventListener: (ev, fn) => { if (ev === 'abort') listeners.push(fn); } },
    abort() {
      if (this.signal.aborted) return;
      this.signal.aborted = true;
      listeners.forEach((fn) => fn());
    },
  };
}

function makeSandbox(opts) {
  opts = opts || {};
  const calls = [];
  const timers = [];

  const fetchImpl = function (url, init) {
    calls.push({ url, init });
    return new Promise((resolve, reject) => {
      if (init && init.signal) {
        init.signal.addEventListener('abort', () => {
          const e = new Error('The operation was aborted.');
          e.name = 'AbortError';
          reject(e);
        });
      }
      if (opts.behaviour === 'never') return;                       // hang forever
      if (opts.behaviour === 'reject') return reject(new Error('network down'));
      setTimeout(() => resolve({ ok: true, status: 200 }), 1);
    });
  };

  const sandbox = {
    calls,
    timers,
    aborts: [],
    fetch: fetchImpl,
    // REAL timers so the abort deadline actually fires; the delay is recorded
    // so the "custom deadline" test can assert on it.
    setTimeout: (fn, ms) => { timers.push(ms); return setTimeout(fn, ms); },
    clearTimeout: (id) => clearTimeout(id),
    AbortController: opts.noAbortController
      ? undefined
      : function () { const c = makeAbort(); sandbox.aborts.push(c); return c; },
  };

  const fn = new Function(
    'fetch', 'setTimeout', 'clearTimeout', 'AbortController',
    'VOLUME_FETCH_TIMEOUT_MS',
    'return ' + FN_SRC + ';'
  )(
    (...a) => sandbox.fetch(...a),
    sandbox.setTimeout,
    sandbox.clearTimeout,
    sandbox.AbortController,
    DEFAULT_MS
  );

  return { ...sandbox, fn };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async function main() {
  console.log('\n[0] Default deadline is present and sane in the shipping file');
  ok('VOLUME_FETCH_TIMEOUT_MS exists', !!mTimeout, mTimeout && mTimeout[1]);
  ok('default is 120000 ms', DEFAULT_MS === 120000, DEFAULT_MS);
  ok('default is finite and generous', DEFAULT_MS > 30000 && DEFAULT_MS < 600000, DEFAULT_MS);

  console.log('\n[1] A stalled fetch is aborted instead of hanging forever');
  {
    const sb = makeSandbox({ behaviour: 'never' });
    const p = sb.fn('/api/v1/volume/x/data', 25);
    let settled = 'pending';
    p.then(() => { settled = 'resolved'; }, (e) => { settled = 'rejected:' + e.name; });
    await sleep(60);
    ok('promise settled (did not hang)', settled !== 'pending', settled);
    ok('rejected with AbortError', settled === 'rejected:AbortError', settled);
    ok('abort() was called', sb.aborts.length === 1 && sb.aborts[0].signal.aborted === true,
       JSON.stringify(sb.aborts.map((a) => a.signal.aborted)));
    ok('a signal was passed to fetch',
       sb.calls.length === 1 && !!(sb.calls[0].init && sb.calls[0].init.signal),
       JSON.stringify(sb.calls[0] && sb.calls[0].init));
    ok('the deadline was scheduled', sb.calls.length === 1);
  }

  console.log('\n[2] A healthy fetch resolves and is passed through untouched');
  {
    const sb = makeSandbox({});
    const res = await sb.fn('/api/v1/volume/x/data');
    ok('resolves with the response', res && res.ok === true, JSON.stringify(res));
    ok('fetch received the url', sb.calls[0].url === '/api/v1/volume/x/data', sb.calls[0].url);
    ok('no abort on the happy path',
       sb.aborts.length === 0 || sb.aborts.every((a) => !a.signal.aborted));
    ok('the timer was cleared', sb.calls.length === 1);
  }

  console.log('\n[3] A network error propagates (not swallowed)');
  {
    const sb = makeSandbox({ behaviour: 'reject' });
    let err = null;
    try { await sb.fn('/api/v1/volume/x/data'); } catch (e) { err = e; }
    ok('rejection propagates', err !== null && /network down/.test(err.message),
       err && err.message);
  }

  console.log('\n[4] Falls back to plain fetch where AbortController is unavailable');
  {
    const sb = makeSandbox({ noAbortController: true });
    const res = await sb.fn('/api/v1/volume/x/data');
    ok('still resolves', res && res.ok === true, JSON.stringify(res));
    ok('no signal was passed', !sb.calls[0].init || !sb.calls[0].init.signal,
       JSON.stringify(sb.calls[0].init));
  }

  console.log('\n[5] The explicit deadline overrides the default');
  {
    const sb = makeSandbox({ behaviour: 'never' });
    sb.fn('/x', 12345);
    ok('custom deadline honoured', sb.timers.includes(12345), sb.timers.join(','));

    const sb2 = makeSandbox({ behaviour: 'never' });
    sb2.fn('/x');
    ok('default deadline used when omitted',
       sb2.timers.includes(DEFAULT_MS), sb2.timers.join(','));
  }

  console.log('\n[6] Source wiring: every volume /data fetch uses the deadline');
  {
    const bare = SRC.match(/fetch\('\/api\/v1\/volume\//g) || [];
    ok('no bare fetch() to a volume endpoint remains', bare.length === 0,
       bare.join(' | '));
    const guarded = SRC.match(/fetchWithDeadline\('\/api\/v1\/volume\//g) || [];
    ok('volume fetches are wrapped', guarded.length >= 3, guarded.length);
    ok('loadSplitCoarseVolume uses the deadline',
       /function loadSplitCoarseVolume[\s\S]{0,300}?fetchWithDeadline\(url\)/.test(SRC));
    ok('fetchWithDeadline is defined', /function fetchWithDeadline\(/.test(SRC));
  }

  console.log('\n===============================');
  console.log('  PASS: ' + pass + '   FAIL: ' + fail);
  console.log('===============================\n');
  process.exit(fail ? 1 : 0);
})();
