/**
 * Behavioural tests for MPR GPU-resource disposal (H15 / AC6).
 *
 * The real `trackResource`, `releaseResource`, `disposeMaterial` and
 * `disposeThreeScene` functions are extracted from the shipped
 * app/static/viewer/paxs-mpr.js and executed against a fake Three.js scene
 * whose objects record their own `dispose()` calls. Only the browser boundary
 * (THREE, canvas, DOM) is faked.
 *
 * This is the unit-level evidence for "open + close 20 times and the live
 * object count returns to baseline"; the Chrome Memory profiler run remains a
 * manual step.
 *
 *   node --test "tests/js/*.test.js"
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { readViewerSource, extractFunction, runInSandbox } = require('./extract.js');

const SOURCE = readViewerSource();

/* -------------------------------------------------------------------------- */
/* fake THREE objects                                                         */
/* -------------------------------------------------------------------------- */

function fakeGeometry(log, name) {
  return { name: name, dispose: () => log.push('geometry:' + name) };
}

function fakeTexture(log, name) {
  return { name: name, isTexture: true, dispose: () => log.push('texture:' + name) };
}

function fakeMaterial(log, name, options) {
  const opts = options || {};
  const material = { name: name, dispose: () => log.push('material:' + name) };
  if (opts.map) { material.map = fakeTexture(log, name + '.map'); }
  if (opts.uniformTexture) { material.uniforms = { u_data: { value: fakeTexture(log, name + '.u_data') } }; }
  return material;
}

function fakeNode(geometry, material) {
  return { geometry: geometry, material: material, children: [] };
}

function fakeScene(nodes) {
  return {
    children: nodes.slice(),
    traverse(visit) {
      const walk = (node) => {
        visit(node);
        (node.children || []).forEach(walk);
      };
      this.children.forEach(walk);
    },
    remove(node) {
      this.children = this.children.filter((child) => child !== node);
    },
  };
}

function fakeRenderer(log) {
  return {
    dispose: () => log.push('renderer:dispose'),
    forceContextLoss: () => log.push('renderer:forceContextLoss'),
  };
}

/* -------------------------------------------------------------------------- */
/* sandbox                                                                    */
/* -------------------------------------------------------------------------- */

function makeSandbox() {
  const log = [];
  const removedListeners = [];
  let cancelledFrames = 0;

  const ctx = runInSandbox(
    [
      extractFunction(SOURCE, 'trackResource'),
      extractFunction(SOURCE, 'releaseResource'),
      extractFunction(SOURCE, 'disposeMaterial'),
      extractFunction(SOURCE, 'disposeThreeScene'),
    ].join('\n'),
    {
      resourceStats: { geometries: 0, materials: 0, textures: 0 },
      threeState: {
        scene: null,
        camera: null,
        renderer: null,
        animFrame: null,
        planes: { axial: null, sagittal: null, coronal: null },
        box: null,
        volumeMesh: null,
        isDragging: false,
        onPointerUp: null,
        extraResources: [],
      },
      window: {
        __paxsMprDebug: false,
        removeEventListener: (type, handler) => removedListeners.push([type, handler]),
      },
      document: { getElementById: () => null },
      cancelAnimationFrame: () => { cancelledFrames += 1; },
      Object,
      console,
    }
  );

  ctx.log = log;
  ctx.removedListeners = removedListeners;
  ctx.cancelledFrameCount = () => cancelledFrames;
  return ctx;
}

/**
 * Mirror exactly what init3dEngine() creates and tracks: a bounding box line
 * (boxGeo -> boxEdges, boxMat), three cutplane meshes (geometry + material +
 * canvas texture) and the raymarch volume mesh (geometry + shader material
 * carrying a Data3DTexture), plus the renderer.
 *
 * Returns the number of tracked objects.
 */
function buildScene(ctx) {
  const log = ctx.log;
  const boxGeo = fakeGeometry(log, 'boxGeo');
  const boxEdges = fakeGeometry(log, 'boxEdges');
  const boxMat = fakeMaterial(log, 'boxMat');
  const boxLine = fakeNode(boxEdges, boxMat);

  const planeNodes = ['axial', 'sagittal', 'coronal'].map((plane) => {
    const geometry = fakeGeometry(log, 'planeGeo-' + plane);
    const material = fakeMaterial(log, 'planeMat-' + plane, { map: true });
    return fakeNode(geometry, material);
  });

  const volumeGeo = fakeGeometry(log, 'volumeGeo');
  const volumeMat = fakeMaterial(log, 'volumeMat', { uniformTexture: true });
  const volumeNode = fakeNode(volumeGeo, volumeMat);

  const nodes = [boxLine].concat(planeNodes, [volumeNode]);
  ctx.threeState.scene = fakeScene(nodes);
  ctx.threeState.renderer = fakeRenderer(log);
  ctx.threeState.box = boxLine;
  ctx.threeState.volumeMesh = volumeNode;
  ctx.threeState.planes = {
    axial: planeNodes[0],
    sagittal: planeNodes[1],
    coronal: planeNodes[2],
  };
  ctx.threeState.extraResources = [boxGeo];
  ctx.threeState.animFrame = 42;
  ctx.threeState.onPointerUp = function onPointerUp() {};

  // Same accounting as init3dEngine(): 6 geometries, 5 materials, 4 textures.
  for (let i = 0; i < 6; i++) { ctx.trackResource('geometries'); }
  for (let i = 0; i < 5; i++) { ctx.trackResource('materials'); }
  for (let i = 0; i < 4; i++) { ctx.trackResource('textures'); }
  return { boxGeo: boxGeo, totalTracked: 15 };
}

/* -------------------------------------------------------------------------- */
/* tests                                                                      */
/* -------------------------------------------------------------------------- */

test('test_dispose_releases_every_tracked_resource', () => {
  const ctx = makeSandbox();
  const built = buildScene(ctx);
  assert.deepStrictEqual(
    { ...ctx.resourceStats },
    { geometries: 6, materials: 5, textures: 4 },
    'precondition: resources tracked as init3dEngine would'
  );

  ctx.disposeThreeScene();

  // Every created object was disposed exactly once.
  const disposed = ctx.log.slice().sort();
  assert.deepStrictEqual(disposed, [
    'geometry:boxEdges', 'geometry:boxGeo', 'geometry:planeGeo-axial',
    'geometry:planeGeo-coronal', 'geometry:planeGeo-sagittal', 'geometry:volumeGeo',
    'material:boxMat', 'material:planeMat-axial', 'material:planeMat-coronal',
    'material:planeMat-sagittal', 'material:volumeMat',
    'renderer:dispose', 'renderer:forceContextLoss',
    'texture:planeMat-axial.map', 'texture:planeMat-coronal.map',
    'texture:planeMat-sagittal.map', 'texture:volumeMat.u_data',
  ].sort());
  assert.ok(ctx.log.includes('geometry:boxGeo'), 'off-graph boxGeo must be disposed');

  // Counters return to the baseline.
  assert.deepStrictEqual({ ...ctx.resourceStats }, { geometries: 0, materials: 0, textures: 0 });
  assert.strictEqual(built.totalTracked, 15, 'sanity: 6 geometries + 5 materials + 4 textures');
});

test('test_dispose_clears_scene_state_and_listeners', () => {
  const ctx = makeSandbox();
  buildScene(ctx);

  ctx.disposeThreeScene();

  assert.strictEqual(ctx.threeState.scene, null);
  assert.strictEqual(ctx.threeState.camera, null);
  assert.strictEqual(ctx.threeState.renderer, null);
  assert.strictEqual(ctx.threeState.box, null);
  assert.strictEqual(ctx.threeState.volumeMesh, null);
  assert.deepStrictEqual(
    { ...ctx.threeState.planes },
    { axial: null, sagittal: null, coronal: null }
  );
  assert.strictEqual(ctx.threeState.isDragging, false);
  // Length check, not deepStrictEqual: the array is created in the vm realm,
  // so its prototype is not this realm's Array.prototype.
  assert.strictEqual(ctx.threeState.extraResources.length, 0);
  assert.strictEqual(ctx.threeState.onPointerUp, null, 'listener reference cleared');
  assert.strictEqual(ctx.removedListeners.length, 1, 'exactly one listener must be detached');
  assert.strictEqual(ctx.removedListeners[0][0], 'pointerup');
  assert.strictEqual(typeof ctx.removedListeners[0][1], 'function', 'the named handler was detached');
  assert.strictEqual(ctx.cancelledFrameCount(), 1, 'the animation frame must be cancelled');
  assert.strictEqual(ctx.threeState.animFrame, null);
});

test('test_twenty_open_close_cycles_return_to_baseline', () => {
  const ctx = makeSandbox();

  for (let cycle = 1; cycle <= 20; cycle++) {
    buildScene(ctx);
    assert.strictEqual(ctx.resourceStats.geometries, 6, `cycle ${cycle}: tracked geometries`);
    ctx.disposeThreeScene();
    assert.deepStrictEqual(
      { ...ctx.resourceStats },
      { geometries: 0, materials: 0, textures: 0 },
      `cycle ${cycle}: resources must return to baseline`
    );
  }

  // 20 cycles x 15 objects, each disposed exactly once, no growth.
  const geometryDisposals = ctx.log.filter((entry) => entry.startsWith('geometry:')).length;
  const materialDisposals = ctx.log.filter((entry) => entry.startsWith('material:')).length;
  const textureDisposals = ctx.log.filter((entry) => entry.startsWith('texture:')).length;
  assert.strictEqual(geometryDisposals, 20 * 6);
  assert.strictEqual(materialDisposals, 20 * 5);
  assert.strictEqual(textureDisposals, 20 * 4);
  assert.strictEqual(ctx.cancelledFrameCount(), 20, 'one cancelled frame per close');
});

test('test_dispose_handles_material_arrays', () => {
  const ctx = makeSandbox();
  const log = ctx.log;
  const shared = fakeMaterial(log, 'sharedMat', { map: true });
  ctx.threeState.scene = fakeScene([fakeNode(fakeGeometry(log, 'multiGeo'), [shared, fakeMaterial(log, 'secondMat')])]);
  ctx.trackResource('geometries');
  ctx.trackResource('materials');
  ctx.trackResource('materials');
  ctx.trackResource('textures');

  ctx.disposeThreeScene();

  assert.ok(log.includes('geometry:multiGeo'));
  assert.ok(log.includes('material:sharedMat'));
  assert.ok(log.includes('material:secondMat'));
  assert.ok(log.includes('texture:sharedMat.map'));
  assert.deepStrictEqual({ ...ctx.resourceStats }, { geometries: 0, materials: 0, textures: 0 });
});

test('test_dispose_is_safe_when_nothing_was_built', () => {
  const ctx = makeSandbox();
  // A close before any series loaded, or a second close, must not throw.
  ctx.disposeThreeScene();
  ctx.disposeThreeScene();
  assert.deepStrictEqual({ ...ctx.resourceStats }, { geometries: 0, materials: 0, textures: 0 });
  assert.strictEqual(ctx.log.length, 0);
});

test('test_dispose_survives_a_partial_scene', () => {
  const ctx = makeSandbox();
  const log = ctx.log;
  // Scene exists but the renderer was never created (WebGL context failure).
  ctx.threeState.scene = fakeScene([fakeNode(fakeGeometry(log, 'lonelyGeo'), null)]);
  ctx.trackResource('geometries');

  ctx.disposeThreeScene();

  assert.ok(log.includes('geometry:lonelyGeo'));
  assert.strictEqual(ctx.resourceStats.geometries, 0);
  assert.strictEqual(ctx.threeState.scene, null);
});
