/**
 * Behavioural tests for the plane extractors shipped in
 * app/static/viewer/paxs-mpr.js.
 *
 * Rather than re-implementing the maths, this test extracts the real
 * `planeAxial` / `planeSagittal` / `planeCoronal` functions out of the shipped
 * file and executes them against a synthetic phantom. It therefore fails on
 * the pre-fix commit and passes afterwards — the C09 evidence for the viewer.
 *
 *   node --test "tests/js/*.test.js"
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
const vm = require('node:vm');

const { readViewerSource, extractFunction } = require('./extract.js');
const phantom = require('./phantom.js');

const source = readViewerSource();

/**
 * Run one extractor against a synthetic state and return the plane array.
 *
 * The real extractor delegates to `planeMeta()` and to the real geometry
 * module, so both are injected into the sandbox — this keeps it an integration
 * test of the shipped code path rather than a re-implementation.
 */
function runExtractor(name, state, index) {
  const geometry = require(path.join(
    __dirname, '..', '..', 'app', 'static', 'viewer', 'paxs-mpr-geometry.js'
  ));
  const ctx = {
    state,
    __index: index,
    Float32Array,
    Math,
    geo: () => geometry,
  };
  vm.createContext(ctx);
  vm.runInContext(
    [
      extractFunction(source, 'planeMeta'),
      extractFunction(source, name),
      `__result = ${name}(__index);`,
    ].join('\n'),
    ctx
  );
  return Array.from(ctx.__result);
}

function makeState() {
  const { vol, meta } = phantom.makeVolume({ rows: 4, cols: 6, slices: 5 });
  return {
    vol,
    rows: meta.rows,
    cols: meta.cols,
    slices: meta.slices,
    spacing: [meta.spacing.row, meta.spacing.col],
    sliceSpacing: meta.spacing.slice,
    projectionMode: 'normal',
    slabThickness: 1,
    idx: { axial: 2, sagittal: 3, coronal: 1 },
  };
}

test('test_axial_extractor_is_row_major_columns_then_rows', () => {
  // C09: axial out[j * cols + i] = vol[k][j][i] (u = i, v = j).
  const state = makeState();
  const plane = runExtractor('planeAxial', state, 2);
  assert.strictEqual(plane.length, state.rows * state.cols);
  for (let j = 0; j < state.rows; j++) {
    for (let i = 0; i < state.cols; i++) {
      assert.strictEqual(plane[j * state.cols + i], phantom.encodeVoxel(i, j, 2), `axial j=${j} i=${i}`);
    }
  }
});

test('test_coronal_extractor_is_slices_then_columns', () => {
  // C09: coronal out[k * cols + i] = vol[k][j][i] (u = i, v = k).
  const state = makeState();
  const plane = runExtractor('planeCoronal', state, 1);
  assert.strictEqual(plane.length, state.slices * state.cols, 'coronal height must be slices');
  for (let k = 0; k < state.slices; k++) {
    for (let i = 0; i < state.cols; i++) {
      assert.strictEqual(plane[k * state.cols + i], phantom.encodeVoxel(i, 1, k), `coronal k=${k} i=${i}`);
    }
  }
});

test('test_sagittal_extractor_is_slices_then_rows', () => {
  // C09: sagittal out[k * rows + j] = vol[k][j][x] (u = j, v = k).
  // The pre-fix implementation emitted out[j * slices + k] (transposed), which
  // did not match the viewport it is drawn into.
  const state = makeState();
  const plane = runExtractor('planeSagittal', state, 3);
  assert.strictEqual(plane.length, state.rows * state.slices, 'sagittal size = rows x slices');
  for (let k = 0; k < state.slices; k++) {
    for (let j = 0; j < state.rows; j++) {
      assert.strictEqual(
        plane[k * state.rows + j], phantom.encodeVoxel(3, j, k),
        `sagittal must be [k * rows + j] (k=${k}, j=${j})`
      );
    }
  }
});
