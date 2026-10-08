/**
 * Wiring contracts for the shipped MPR viewer.
 *
 * These assert the *structural* properties of app/static/viewer/paxs-mpr.js
 * that cannot be exercised without a browser: that every coordinate/measurement
 * calculation is delegated to paxs-mpr-geometry.js (C09 — no duplicated maths),
 * and that the worker + GPU lifecycles are fully cancellable/disposable (H15).
 *
 *   node --test "tests/js/*.test.js"
 *
 * On the pre-fix commit this file fails on every group, which is the intended
 * "fails before, passes after" evidence for the viewer half of the repair.
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..');
const MPR = path.join(ROOT, 'app', 'static', 'viewer', 'paxs-mpr.js');
const GEOMETRY = path.join(ROOT, 'app', 'static', 'viewer', 'paxs-mpr-geometry.js');
const VIEWER_HTML = path.join(ROOT, 'app', 'static', 'viewer', 'index.html');
const MAIN_PY = path.join(ROOT, 'app', 'main.py');

/** Whitespace-normalised source, so multi-line expressions match. */
const source = fs.readFileSync(MPR, 'utf8').replace(/\s+/g, ' ');

function assertContains(needle, message) {
  assert.ok(source.includes(needle), `${message || 'missing'}: expected to find \`${needle}\``);
}

function assertAbsent(needle, message) {
  assert.ok(!source.includes(needle), `${message || 'leftover'}: must NOT contain \`${needle}\``);
}

/* -------------------------------------------------------------------------- */
/* C09 — single source of truth for coordinate maths                          */
/* -------------------------------------------------------------------------- */

test('test_c09_geometry_module_is_used_for_every_transform', () => {
  assertContains('window.PaxsMprGeometry', 'geometry module accessor');
  for (const call of [
    'geo().makeMeta(',
    'geo().extractPlane(',
    'geo().viewportToVoxel(',
    'geo().sampleVoxel(',
    'geo().planeDims(',
    'geo().stridedDim(',
    'geo().computePhysicalLength(',
    'geo().computeCobbAngle(',
    'geo().computePhysicalRatio(',
    'geo().computeEllipseAreaCm2('
  ]) {
    assertContains(call, 'delegated geometry call');
  }
});

test('test_c09_no_duplicated_index_or_measurement_math', () => {
  // The exact expressions that produced the wrong coronal/sagittal values and
  // the spacing-blind measurements before the fix.
  assertAbsent('px * state.rows * state.cols', 'inline sagittal/coronal probe index');
  assertAbsent('state.idx.coronal * state.cols + py', 'inline coronal probe index');
  assertAbsent('Math.atan((m2 - m1)', 'normalized-space Cobb angle');
  assertAbsent('vp.w * sx', 'grid-space ruler length');
  assertAbsent('plane === \'axial\' ? state.sliceSpacing : state.spacing[0]', 'inline normal-axis spacing');
  // H13 consumers: floor(n/2) must be the shared stridedDim helper.
  assertAbsent('Math.floor(info.rows / 2)', 'floor-based preview rows');
  assertAbsent('Math.floor(info.columns / 2)', 'floor-based preview columns');
  assertAbsent('Math.ceil(info.slices / 2)', 'ad-hoc ceil for preview slices');
  assertAbsent('Math.floor(fInfo.rows / 2)', 'floor-based fusion rows');
});

test('test_c09_plane_extractors_delegate_and_use_canonical_order', () => {
  assertContains("function planeSagittal(x) { return geo().extractPlane(state.vol, 'sagittal', x, planeMeta('sagittal')); }");
  assertContains("function planeCoronal(y) { return geo().extractPlane(state.vol, 'coronal', y, planeMeta('coronal')); }");
  assertContains("geo().extractPlane(state.vol, 'axial', z, planeMeta('axial'))");
});

test('test_c09_webgl2_slice_shader_uses_canonical_axis_mapping', () => {
  // axis 0 (axial) u=i v=j, axis 1 (sagittal) u=j v=k, axis 2 (coronal) u=i v=k
  assertContains("' if (u_axis == 0) base = vec3(v_uv.x, v_uv.y, u_pos);'");
  assertContains("' else if (u_axis == 1) base = vec3(u_pos, v_uv.x, v_uv.y);'");
  assertContains("' else base = vec3(v_uv.x, u_pos, v_uv.y);'");
  assertAbsent("vec3(u_pos, v_uv.y, v_uv.x)", 'transposed sagittal shader mapping');
});

test('test_c09_crosshair_and_fusion_follow_the_convention', () => {
  // Crosshair: vertical line = u axis, horizontal line = v axis.
  assertContains('if (plane === \'axial\') { uIndex = state.idx.sagittal; vIndex = state.idx.coronal; }');
  assertContains('else if (plane === \'sagittal\') { uIndex = state.idx.coronal; vIndex = state.idx.axial; }');
  // Fusion overlay: per-plane image extents (sagittal is rows x slices).
  assertContains("var imgW = plane === 'sagittal' ? fRows : fCols;");
  assertContains("var imgH = plane === 'axial' ? fRows : fSlices;");
  assertContains('fVol[r * fRows * fCols + c * fCols + fX]');
  assertContains('fVol[r * fRows * fCols + fY * fCols + c]');
});

test('test_c09_geometry_module_is_loadable_and_served_uncached', () => {
  assert.ok(fs.existsSync(GEOMETRY), 'paxs-mpr-geometry.js must exist');

  // Load order: the geometry module must come before the viewer module.
  const html = fs.readFileSync(VIEWER_HTML, 'utf8');
  const geometryAt = html.indexOf('/viewer/paxs-mpr-geometry.js');
  const viewerAt = html.indexOf('/viewer/paxs-mpr.js');
  assert.ok(geometryAt >= 0, 'viewer/index.html must load paxs-mpr-geometry.js');
  assert.ok(viewerAt >= 0, 'viewer/index.html must load paxs-mpr.js');
  assert.ok(geometryAt < viewerAt, 'geometry module must be loaded before paxs-mpr.js');

  // Cache policy: entry-point scripts must revalidate, not be cached for a day.
  const mainPy = fs.readFileSync(MAIN_PY, 'utf8');
  assert.ok(mainPy.includes('"paxs-mpr-geometry.js"'), 'app/main.py _NO_CACHE_NAMES must list paxs-mpr-geometry.js');
});

/* -------------------------------------------------------------------------- */
/* H15 — worker promise lifecycle                                             */
/* -------------------------------------------------------------------------- */

test('test_h15_worker_requests_have_timeout_and_settle_once', () => {
  assertContains('WORKER_DECODE_TIMEOUT_MS', 'configurable decode timeout');
  assertContains('function settleWorkerRequest(', 'single settle helper');
  assertContains('clearTimeout(pending.timer)', 'timeout cleanup');
  assertContains('volume decode timed out', 'timeout rejection message');
  // The pending map entry carries its own worker so errors can be attributed.
  assertContains('worker: worker,');
});

test('test_h15_worker_error_rejects_pending_decodes', () => {
  assertContains('worker.onerror = function (err) {', 'worker error handler');
  assertContains('if (workerCallbacks[key].worker === failed)', 'per-worker attribution');
  assertContains("settleWorkerRequest(key, new Error('volume decode worker failed'));");
});

test('test_h15_close_rejects_pending_and_terminates_workers', () => {
  assertContains('function cancelPendingDecodes(', 'cancellation helper');
  assertContains('function shutdownWorkerPool(', 'pool teardown helper');
  assertContains('CancelledError', 'cancellation error type');
  assertContains('worker.terminate()', 'worker termination');
  assertContains('URL.revokeObjectURL(workerBlobUrl)', 'blob URL release');
  // closeMpr must go through the teardown path.
  assertContains("shutdownWorkerPool('MPR workspace closed')");
});

test('test_h15_dead_rle_branch_is_removed', () => {
  // /data always streams decoded voxels, so no transfer syntax is involved.
  assertAbsent('decodeRle', 'dead RLE decoder');
  assertAbsent('transferSyntax', 'unused transfer syntax plumbing');
  assertContains('function decodeVolumeAsync(info, buf)');
});

/* -------------------------------------------------------------------------- */
/* H15 — GPU / listener lifecycle                                             */
/* -------------------------------------------------------------------------- */

test('test_h15_scene_disposal_is_complete', () => {
  assertContains('function disposeThreeScene(', 'scene disposal helper');
  assertContains('function disposeMaterial(', 'material disposal helper');
  assertContains('node.geometry.dispose()', 'geometry disposal');
  assertContains('material.dispose()', 'material disposal');
  assertContains('tex.dispose()', 'texture disposal');
  assertContains('threeState.renderer.dispose()', 'renderer disposal');
  assertContains('threeState.renderer.forceContextLoss()', 'context loss');
  assertContains("window.removeEventListener('pointerup', threeState.onPointerUp)", 'listener detach');
  assertContains("threeState.onPointerUp = null;", 'listener reference cleared');
});

test('test_h15_disposal_runs_on_close_and_on_reload', () => {
  // Called by closeMpr (workspace close) ...
  const closeBody = source.slice(source.indexOf('function closeMpr()'), source.indexOf('function setProgress('));
  assert.ok(closeBody.includes('disposeThreeScene();'), 'closeMpr must dispose the 3D scene');
  assert.ok(closeBody.includes('shutdownWorkerPool('), 'closeMpr must shut the worker pool down');
  // ... and at the top of init3dEngine (series switch rebuilds the scene).
  const initBody = source.slice(source.indexOf('function init3dEngine()'), source.indexOf('function create3dVolumeRaymarchMesh('));
  assert.ok(initBody.includes('disposeThreeScene();'), 'init3dEngine must dispose the previous scene');
});

test('test_h15_gpu_probe_context_is_released_and_counters_exposed', () => {
  assertContains('WEBGL_lose_context', 'probe context release');
  assertContains('window.paxsMprResourceStats', 'dev-mode resource counters');
  assertContains("releaseResource('geometries')");
  assertContains("releaseResource('materials')");
  assertContains("releaseResource('textures')");
  assertContains("trackResource('geometries')");
  assertContains("trackResource('materials')");
  assertContains("trackResource('textures')");
});

test('test_h15_animation_frame_is_cancelled_before_dispose', () => {
  assertContains('if (threeState.animFrame) { cancelAnimationFrame(threeState.animFrame); threeState.animFrame = null; }');
});

test('test_h15_off_graph_geometries_are_disposed', () => {
  // boxGeo only feeds EdgesGeometry, so scene.traverse cannot reach it: it must
  // be tracked separately or every open/close leaks one BoxGeometry.
  assertContains('threeState.extraResources = [boxGeo]');
  assertContains('(threeState.extraResources || []).forEach(function (obj) {');
});

/* -------------------------------------------------------------------------- */
/* hygiene                                                                    */
/* -------------------------------------------------------------------------- */

test('test_no_todo_fixme_hack_markers', () => {
  for (const marker of ['TODO', 'FIXME', 'HACK']) {
    assertAbsent(marker, `no ${marker} markers may be introduced`);
  }
});
