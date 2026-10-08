/**
 * Behavioural tests for the MPR decode-worker lifecycle (H15 / AC5).
 *
 * The real `cancelledError`, `settleWorkerRequest`, `cancelPendingDecodes`,
 * `shutdownWorkerPool` and `decodeVolumeAsync` functions are extracted from
 * the shipped app/static/viewer/paxs-mpr.js and executed in a vm sandbox
 * against fake workers. Only the browser boundary is replaced
 * (`initWorkerPool`, which does `new Worker(blobURL)`, and the `Worker`
 * constructor); every promise-settlement rule under test is the real code.
 *
 *   node --test "tests/js/*.test.js"
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { readViewerSource, extractFunction, runInSandbox } = require('./extract.js');

const SOURCE = readViewerSource();

/** Short default so a leaked timer cannot stall the test process. */
const TIMEOUT_MS = 60;

function fakeWorker(name) {
  return {
    name,
    posted: [],
    terminated: false,
    onmessage: null,
    onerror: null,
    postMessage(message, transfer) {
      this.posted.push({ message, transfer });
    },
    terminate() {
      this.terminated = true;
    },
  };
}

/**
 * Sandbox with the REAL lifecycle functions — including `initWorkerPool`, so
 * the onmessage/onerror wiring under test is the shipped code. Only the
 * browser APIs it needs (`Worker`, `Blob`, `URL`, `navigator`) are faked, and
 * every fake worker created by the real code is captured in
 * `ctx.createdWorkers`.
 */
function makeSandbox(options) {
  const opts = options || {};
  const revoked = [];
  const created = [];
  // The viewer both guards on `window.Worker` and constructs a bare `Worker`
  // (a browser global), so the fake must be reachable under both names.
  const WorkerCtor = opts.noWorkerSupport
    ? undefined
    : function FakeWorker() {
        const worker = fakeWorker('w' + created.length);
        created.push(worker);
        return worker;
      };
  const ctx = runInSandbox(
    [
      extractFunction(SOURCE, 'cancelledError'),
      extractFunction(SOURCE, 'settleWorkerRequest'),
      extractFunction(SOURCE, 'cancelPendingDecodes'),
      extractFunction(SOURCE, 'shutdownWorkerPool'),
      extractFunction(SOURCE, 'initWorkerPool'),
      extractFunction(SOURCE, 'decodeVolumeAsync'),
    ].join('\n'),
    {
      workerPool: [],
      workerPoolSize: opts.poolSize == null ? 1 : opts.poolSize,
      workerRoundRobin: 0,
      workerCallbacks: {},
      workerReqSeq: 0,
      workerBlobUrl: null,
      WORKER_DECODE_TIMEOUT_MS: TIMEOUT_MS,
      Worker: WorkerCtor,
      window: {
        Worker: WorkerCtor,
        __paxsMprDecodeTimeoutMs: undefined,
      },
      Blob: function FakeBlob() {},
      URL: {
        createObjectURL: () => 'blob:fake',
        revokeObjectURL: (url) => revoked.push(url),
      },
      navigator: { hardwareConcurrency: 2 },
      decodeVolume: () => ({ vol: 'sync-fallback' }),
      setTimeout,
      clearTimeout,
      Error,
      Object,
      console,
    }
  );
  ctx.revoked = revoked;
  ctx.createdWorkers = created;
  // Create the pool through the real initialiser.
  ctx.initWorkerPool();
  return ctx;
}

/** The single fake worker the real initWorkerPool() created. */
function poolWorker(ctx, index) {
  const worker = ctx.createdWorkers[index || 0];
  assert.ok(worker, 'initWorkerPool did not create the expected worker');
  return worker;
}

function requestIdOf(worker, index) {
  const posted = worker.posted[index || 0];
  assert.ok(posted, `${worker.name} was never posted to`);
  return posted.message.reqId;
}

function pendingCount(ctx) {
  return Object.keys(ctx.workerCallbacks).length;
}

/** Swallow a rejection so it can be asserted on later without racing. */
function capture(promise) {
  return promise.then(
    (value) => ({ status: 'fulfilled', value }),
    (reason) => ({ status: 'rejected', reason })
  );
}

/* -------------------------------------------------------------------------- */

test('test_worker_error_rejects_pending_decodes', async () => {
  const ctx = makeSandbox({ poolSize: 1 });
  const worker = poolWorker(ctx);
  const promise = ctx.decodeVolumeAsync({ slices: 1, rows: 1, columns: 1 }, new ArrayBuffer(2));
  assert.strictEqual(pendingCount(ctx), 1, 'request must be registered while in flight');

  worker.onerror.call(worker, new Error('worker crashed mid-decode'));

  await assert.rejects(promise, /volume decode worker failed/);
  assert.strictEqual(pendingCount(ctx), 0, 'failed request must free its slot');
});

test('test_worker_timeout_rejects_pending_decode', async () => {
  const ctx = makeSandbox({ poolSize: 1 });
  // The worker never answers: only the timeout can settle this request.
  const promise = ctx.decodeVolumeAsync({ slices: 1, rows: 1, columns: 1 }, new ArrayBuffer(2));

  await assert.rejects(promise, /volume decode timed out/);
  assert.strictEqual(pendingCount(ctx), 0, 'timed-out request must free its slot');
});

test('test_shutdown_rejects_every_in_flight_decode_with_cancelled_error', async () => {
  const ctx = makeSandbox({ poolSize: 1 });
  const worker = poolWorker(ctx);

  const promises = [0, 1, 2].map(() =>
    capture(ctx.decodeVolumeAsync({ slices: 1, rows: 1, columns: 1 }, new ArrayBuffer(2)))
  );
  assert.strictEqual(pendingCount(ctx), 3, 'three decodes must be in flight');

  ctx.shutdownWorkerPool('MPR workspace closed');

  const results = await Promise.all(promises);
  assert.strictEqual(results.length, 3);
  for (const result of results) {
    assert.strictEqual(result.status, 'rejected', 'every in-flight decode must reject');
    assert.strictEqual(result.reason.name, 'CancelledError');
    assert.match(result.reason.message, /MPR workspace closed/);
  }
  assert.strictEqual(pendingCount(ctx), 0, 'no callback slots may survive shutdown');
  assert.strictEqual(worker.terminated, true, 'the worker must be terminated');
  assert.strictEqual(ctx.workerPool.length, 0, 'the pool must be emptied');
  assert.deepStrictEqual(ctx.revoked, ['blob:fake'], 'the blob URL must be revoked');
  assert.strictEqual(ctx.workerBlobUrl, null, 'the blob URL reference must be cleared');
});

test('test_worker_message_resolves_and_clears_the_slot', async () => {
  const ctx = makeSandbox({ poolSize: 1 });
  const worker = poolWorker(ctx);
  const promise = ctx.decodeVolumeAsync({ slices: 1, rows: 1, columns: 1 }, new ArrayBuffer(2));
  const reqId = requestIdOf(worker);

  worker.onmessage({ data: { reqId: reqId, vol: 'decoded-volume' } });

  // Field comparison (not deepStrictEqual): the resolved object is created
  // inside the vm realm, so its prototype differs from this realm's Object.
  const resolved = await promise;
  assert.strictEqual(resolved.vol, 'decoded-volume');
  assert.strictEqual(pendingCount(ctx), 0);
});

test('test_settle_is_idempotent', async () => {
  const ctx = makeSandbox({ poolSize: 1 });
  const worker = poolWorker(ctx);
  const promise = ctx.decodeVolumeAsync({ slices: 1, rows: 1, columns: 1 }, new ArrayBuffer(2));
  const reqId = requestIdOf(worker);

  worker.onmessage({ data: { reqId: reqId, vol: 'first' } });
  assert.strictEqual((await promise).vol, 'first');

  // A late error (e.g. the worker dying after it already answered) must be a
  // no-op, not a second settlement.
  assert.strictEqual(ctx.settleWorkerRequest(reqId, new Error('late failure')), false);
});

test('test_cancel_is_scoped_to_one_worker', async () => {
  const ctx = makeSandbox({ poolSize: 2 });
  const workerA = poolWorker(ctx, 0);
  const workerB = poolWorker(ctx, 1);

  const first = capture(ctx.decodeVolumeAsync({ slices: 1, rows: 1, columns: 1 }, new ArrayBuffer(2)));
  const second = capture(ctx.decodeVolumeAsync({ slices: 1, rows: 1, columns: 1 }, new ArrayBuffer(2)));
  assert.strictEqual(pendingCount(ctx), 2);
  assert.notStrictEqual(workerA, workerB, 'the pool must round-robin across workers');

  ctx.cancelPendingDecodes('worker A died', workerA);

  assert.strictEqual((await first).status, 'rejected', "the dead worker's request must reject");
  assert.strictEqual(pendingCount(ctx), 1, "the other worker's request must survive");

  // Settle the survivor so no timer outlives the test.
  workerB.onmessage({ data: { reqId: requestIdOf(workerB), vol: 'ok' } });
  assert.strictEqual((await second).status, 'fulfilled');
});

test('test_without_worker_support_decodes_synchronously', async () => {
  const ctx = makeSandbox({ noWorkerSupport: true });
  assert.strictEqual(ctx.workerPool.length, 0, 'no pool without Worker support');
  assert.deepStrictEqual(
    await ctx.decodeVolumeAsync({ slices: 1, rows: 1, columns: 1 }, new ArrayBuffer(2)),
    { vol: 'sync-fallback' }
  );
  assert.strictEqual(pendingCount(ctx), 0);
});

test('test_post_message_failure_rejects_instead_of_hanging', async () => {
  const ctx = makeSandbox({ poolSize: 1 });
  const worker = poolWorker(ctx);
  worker.postMessage = () => { throw new Error('DataCloneError'); };

  await assert.rejects(
    ctx.decodeVolumeAsync({ slices: 1, rows: 1, columns: 1 }, new ArrayBuffer(2)),
    /DataCloneError/
  );
  assert.strictEqual(pendingCount(ctx), 0, 'a failed dispatch must not leak a slot');
});

test('test_every_request_gets_a_unique_id', async () => {
  const ctx = makeSandbox({ poolSize: 1 });
  const worker = poolWorker(ctx);
  // Capture both promises: a rejection with no handler would surface as an
  // unhandled rejection and fail the file.
  const first = capture(ctx.decodeVolumeAsync({ slices: 1, rows: 1, columns: 1 }, new ArrayBuffer(2)));
  const second = capture(ctx.decodeVolumeAsync({ slices: 1, rows: 1, columns: 1 }, new ArrayBuffer(2)));
  const ids = worker.posted.map((p) => p.message.reqId);
  assert.strictEqual(new Set(ids).size, 2, `request ids must be unique, got ${ids.join(',')}`);

  ctx.cancelPendingDecodes('cleanup');
  assert.strictEqual((await first).reason.name, 'CancelledError');
  assert.strictEqual((await second).reason.name, 'CancelledError');
  assert.strictEqual(pendingCount(ctx), 0);
});
