/**
 * V1-1 verification: the transport-level 401 refresh-and-retry guard in
 * paxs-study-list-enhance.js.
 *
 * Regression context: paxsFetch() only wrapped three report/auth calls, so
 * Cornerstone's WADO-RS image requests never got a refresh+retry. A single
 * transient 401 was terminal for that frame -> black viewport.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const SRC = fs.readFileSync(
  path.resolve('D:/Paxs Archive/app/static/viewer/paxs-study-list-enhance.js'), 'utf8');

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? '  -> ' + extra : '')); }
}

const VIEWER_URL = 'http://localhost/viewer/viewer?StudyInstanceUIDs=1.2.840.1';

function mkRes(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: () => Promise.resolve(body === undefined ? {} : body),
    text: () => Promise.resolve(typeof body === 'string' ? body : ''),
  };
}

/**
 * A controllable XMLHttpRequest. jsdom's real XHR would hit the network and
 * bypass our fetch mock, so the guard can only be tested against a fake whose
 * status we own.
 */
function makeFakeXhr() {
  function FakeXHR() {
    this.status = 0;
    this.__headers = {};
    this.__listeners = {};
    this.__sendCount = 0;
  }
  FakeXHR.prototype.open = function (method, url) {
    this.__method = method;
    this.__url = url;
  };
  FakeXHR.prototype.setRequestHeader = function (k, v) { this.__headers[k] = v; };
  FakeXHR.prototype.addEventListener = function (ev, fn) {
    (this.__listeners[ev] = this.__listeners[ev] || []).push(fn);
  };
  FakeXHR.prototype.send = function () {
    const self = this;
    self.__sendCount++;
    const auth = self.__headers['Authorization'];
    setTimeout(function () {
      self.status = auth ? 200 : 401;
      (self.__listeners['load'] || []).forEach((fn) => fn.call(self));
    }, 0);
  };
  return FakeXHR;
}

/** Boot the script with an instrumented fetch + fake XHR. */
function boot(opts) {
  opts = opts || {};
  const dom = new JSDOM(
    '<!doctype html><html><body><header></header></body></html>',
    { runScripts: 'outside-only', pretendToBeVisual: true,
      virtualConsole: new VirtualConsole(), url: VIEWER_URL });
  const w = dom.window;

  const log = { calls: [], refreshes: 0, retriedWithAuth: 0 };
  w.__log = log;

  // Instrumented fetch, captured before the script wraps it.
  w.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const auth = (init && init.headers && init.headers['Authorization']) || null;
    log.calls.push({ url, auth });

    if (url.indexOf('/api/v1/auth/refresh') !== -1) {
      log.refreshes++;
      if (opts.refreshFails) return Promise.resolve(mkRes(401));
      return Promise.resolve(mkRes(200, { access_token: 'fresh-token-1' }));
    }
    if (url.indexOf('/api/v1/auth/me') !== -1) return Promise.resolve(mkRes(200, {}));

    // `alwaysUnauthorized` simulates a server that keeps 401-ing even WITH a
    // valid token — used to prove there is no retry loop.
    if (opts.alwaysUnauthorized) return Promise.resolve(mkRes(401));
    if (!auth) return Promise.resolve(mkRes(401));
    if (url.indexOf('/dicom-web/') !== -1 || url.indexOf('/api/v1/volume/') !== -1) {
      log.retriedWithAuth++;
      return Promise.resolve(mkRes(200, 'OK'));
    }
    return Promise.resolve(mkRes(200, 'OK'));
  };

  w.XMLHttpRequest = makeFakeXhr();
  w.eval(SRC);
  return w;
}

const settle = () => new Promise((r) => setTimeout(r, 5));

(async function main() {
  console.log('\n[1] A 401 on a WADO-RS frame is refreshed and replayed');
  {
    const w = boot();
    await settle();
    w.__log.refreshes = 0;              // ignore the page-load silent refresh
    w.__log.calls.length = 0;

    const res = await w.fetch('/dicom-web/studies/1/series/2/instances/3/frames/1');
    await settle();

    ok('original 401 recovered to 200', res.status === 200, res.status);
    ok('exactly one refresh fired', w.__log.refreshes === 1, w.__log.refreshes);
    ok('replay carried a bearer token', w.__log.retriedWithAuth === 1, w.__log.retriedWithAuth);
    const replayed = w.__log.calls.filter((c) => c.url.indexOf('/frames/1') !== -1);
    ok('frame was requested twice (original + replay)', replayed.length === 2, replayed.length);
    ok('original request had no Authorization',
       replayed[0] && replayed[0].auth === null, replayed[0] && replayed[0].auth);
    ok('replay had Authorization: Bearer …',
       replayed[1] && /^Bearer /.test(replayed[1].auth || ''), replayed[1] && replayed[1].auth);
  }

  console.log('\n[2] Volume + report endpoints are covered too');
  {
    for (const [label, url] of [
      ['volume data', '/api/v1/volume/1.2.3/data?downsample=2'],
      ['mpr slice', '/api/v1/volume/1.2.3/mpr/slice?plane=axial&index=5'],
      ['report', '/api/v1/reports/study/1.2.3'],
    ]) {
      const w = boot();
      await settle();
      w.__log.refreshes = 0;
      const res = await w.fetch(url);
      await settle();
      ok(`${label} recovers from 401`, res.status === 200, res.status);
      ok(`${label} refreshed once`, w.__log.refreshes === 1, w.__log.refreshes);
    }
  }

  console.log('\n[3] Single-flight: a burst of 401s triggers ONE refresh');
  {
    const w = boot();
    await settle();
    w.__log.refreshes = 0;
    w.__log.retriedWithAuth = 0;

    const results = await Promise.all([
      w.fetch('/dicom-web/a/frames/1'),
      w.fetch('/dicom-web/a/frames/2'),
      w.fetch('/dicom-web/a/frames/3'),
      w.fetch('/dicom-web/a/frames/4'),
      w.fetch('/dicom-web/a/frames/5'),
    ]);
    await settle();

    ok('all five frames recovered', results.every((r) => r.status === 200),
       results.map((r) => r.status).join(','));
    ok('exactly ONE refresh for the burst', w.__log.refreshes === 1, w.__log.refreshes);
    ok('all five replayed with auth', w.__log.retriedWithAuth === 5, w.__log.retriedWithAuth);
  }

  console.log('\n[4] No infinite retry: exactly one replay per request');
  {
    const w = boot({ alwaysUnauthorized: true });
    await settle();
    w.__log.refreshes = 0;
    w.__log.calls.length = 0;

    const res = await w.fetch('/dicom-web/a/frames/9');
    await settle();

    ok('a permanently-401 server still surfaces 401', res.status === 401, res.status);
    ok('refreshed at most once', w.__log.refreshes <= 1, w.__log.refreshes);
    const n = w.__log.calls.filter((c) => c.url.indexOf('/frames/9') !== -1).length;
    ok('frame requested at most twice (no loop)', n <= 2, n);
  }

  console.log('\n[5] Unrelated endpoints are NOT intercepted (no behaviour change)');
  {
    const w = boot();
    await settle();
    w.__log.refreshes = 0;
    w.__log.calls.length = 0;

    const res = await w.fetch('/api/v1/studies?page=1');
    await settle();
    ok('studies list still 401s through', res.status === 401, res.status);
    ok('no refresh triggered for an unrelated path', w.__log.refreshes === 0, w.__log.refreshes);
    ok('request issued exactly once', w.__log.calls.length === 1, w.__log.calls.length);
  }

  console.log('\n[6] Refresh failure surfaces the original 401 (no crash)');
  {
    const w = boot({ refreshFails: true });
    await settle();
    const res = await w.fetch('/dicom-web/a/frames/1');
    await settle();
    ok('original 401 is surfaced', res.status === 401, res.status);
  }

  console.log('\n[7] Guard is idempotent and does not double-wrap');
  {
    const w = boot();
    await settle();
    w.__log.refreshes = 0;
    w.__paxsInstallAuthGuards();       // must be a no-op (__paxsFetchGuard)
    w.__paxsInstallAuthGuards();
    await w.fetch('/dicom-web/a/frames/7');
    await settle();
    ok('still exactly one refresh after re-install', w.__log.refreshes === 1, w.__log.refreshes);
    const n = w.__log.calls.filter((c) => c.url.indexOf('/frames/7') !== -1).length;
    ok('frame requested exactly twice (not 4x)', n === 2, n);
  }

  console.log('\n[8] XMLHttpRequest path is guarded as well');
  {
    const w = boot();
    await settle();
    w.__log.refreshes = 0;

    const outcome = await new Promise((resolve) => {
      const xhr = new w.XMLHttpRequest();
      const statuses = [];
      xhr.open('GET', '/dicom-web/a/frames/1');
      xhr.addEventListener('load', function () {
        statuses.push(xhr.status);
        // Resolve once the replay has also completed.
        if (statuses.length >= 2) resolve({ statuses, sends: xhr.__sendCount });
      });
      xhr.send();
    });
    await settle();

    ok('XHR guard installed', w.__paxsXhrGuard === true, w.__paxsXhrGuard);
    ok('XHR was sent twice (original + replay)', outcome.sends === 2, outcome.sends);
    ok('first attempt 401, replay 200',
       outcome.statuses[0] === 401 && outcome.statuses[1] === 200,
       outcome.statuses.join(','));
    ok('XHR refreshed once', w.__log.refreshes === 1, w.__log.refreshes);
  }

  console.log('\n[9] XHR replay does not loop against a permanently-401 server');
  {
    const w = boot({ alwaysUnauthorized: true });
    await settle();
    w.__log.refreshes = 0;
    // Re-install a fake XHR that ALWAYS 401s, then drive one request.
    w.XMLHttpRequest = (function () {
      function F() { this.status = 0; this.__headers = {}; this.__listeners = {}; this.__sendCount = 0; }
      F.prototype.open = function () {};
      F.prototype.setRequestHeader = function (k, v) { this.__headers[k] = v; };
      F.prototype.addEventListener = function (ev, fn) {
        (this.__listeners[ev] = this.__listeners[ev] || []).push(fn);
      };
      F.prototype.send = function () {
        const self = this;
        self.__sendCount++;
        setTimeout(function () {
          self.status = 401;
          (self.__listeners['load'] || []).forEach((fn) => fn.call(self));
        }, 0);
      };
      return F;
    })();
    w.__paxsXhrGuard = false;          // force a fresh install against the new XHR
    w.__paxsInstallAuthGuards();
    await settle();

    const sends = await new Promise((resolve) => {
      const xhr = new w.XMLHttpRequest();
      xhr.open('GET', '/dicom-web/a/frames/2');
      xhr.send();
      setTimeout(() => resolve(xhr.__sendCount), 30);
    });
    ok('XHR replayed at most once', sends <= 2, sends);
    ok('XHR refreshed at most once', w.__log.refreshes <= 1, w.__log.refreshes);
  }

  console.log('\n===============================');
  console.log('  PASS: ' + pass + '   FAIL: ' + fail);
  console.log('===============================\n');
  process.exit(fail ? 1 : 0);
})();
