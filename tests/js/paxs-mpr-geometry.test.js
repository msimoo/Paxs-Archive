/**
 * Unit tests for app/static/viewer/paxs-mpr-geometry.js
 *
 * Run with the Node built-in test runner (no extra dependencies):
 *   node --test "tests/js/*.test.js"
 *
 * These cover C09 (MPR geometry / physical measurements): the plane axis
 * convention, voxel<->patient transforms, spacing-aware length/angle/ratio,
 * and the requirement that a plane's displayed voxel equals the voxel the
 * probe reads at the same screen position.
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const G = require(path.join(__dirname, '..', '..', 'app', 'static', 'viewer', 'paxs-mpr-geometry.js'));
const phantom = require('./phantom.js');

const CLOSE = 1e-6;

function approx(actual, expected, tolerance, message) {
  const tol = tolerance == null ? 1e-9 : tolerance;
  assert.ok(
    Math.abs(actual - expected) <= tol,
    `${message || 'value'}: expected ${expected} +/- ${tol}, got ${actual}`
  );
}

/* -------------------------------------------------------------------------- */
/* plane convention                                                           */
/* -------------------------------------------------------------------------- */

test('test_plane_axis_conventions', () => {
  // C09: the canonical convention shared with volume_service.reslice_plane.
  assert.deepStrictEqual(G.planeAxes('axial'), { u: 'i', v: 'j', normal: 'k' });
  assert.deepStrictEqual(G.planeAxes('sagittal'), { u: 'j', v: 'k', normal: 'i' });
  assert.deepStrictEqual(G.planeAxes('coronal'), { u: 'i', v: 'k', normal: 'j' });

  const meta = G.makeMeta({ rows: 6, columns: 8, slices: 5, pixel_spacing: [2, 1], slice_spacing: 2.5 });
  assert.deepStrictEqual(G.planeDims('axial', meta), { w: 8, h: 6 });
  assert.deepStrictEqual(G.planeDims('sagittal', meta), { w: 6, h: 5 });
  assert.deepStrictEqual(G.planeDims('coronal', meta), { w: 8, h: 5 });

  // Spacing follows the u/v axes, not the raw DICOM order.
  assert.deepStrictEqual(G.planeSpacing('axial', meta), { u: 1, v: 2 });
  assert.deepStrictEqual(G.planeSpacing('sagittal', meta), { u: 2, v: 2.5 });
  assert.deepStrictEqual(G.planeSpacing('coronal', meta), { u: 1, v: 2.5 });

  // Index ranges along each plane's constant axis.
  assert.strictEqual(G.planeIndexCount('axial', meta), 5);
  assert.strictEqual(G.planeIndexCount('sagittal', meta), 8);
  assert.strictEqual(G.planeIndexCount('coronal', meta), 6);
});

test('test_make_meta_accepts_api_and_canonical_forms', () => {
  const fromApi = G.makeMeta({
    rows: 5, columns: 7, slices: 4, pixel_spacing: [2, 1],
    slice_thickness: 3, image_position_patient: [1, 2, 3],
    image_orientation_patient: [1, 0, 0, 0, 1, 0],
  }, 'coronal', 2);
  assert.strictEqual(fromApi.rows, 5);
  assert.strictEqual(fromApi.cols, 7);
  assert.strictEqual(fromApi.slices, 4);
  assert.deepStrictEqual(fromApi.spacing, { row: 2, col: 1, slice: 3 });
  assert.deepStrictEqual(fromApi.origin, [1, 2, 3]);
  assert.strictEqual(fromApi.plane, 'coronal');
  assert.strictEqual(fromApi.planeIndex, 2);

  // Missing spacing/orientation degrades to 1 mm isotropic, identity axes.
  const bare = G.makeMeta({ rows: 2, columns: 2, slices: 2 });
  assert.deepStrictEqual(bare.spacing, { row: 1, col: 1, slice: 1 });
  assert.strictEqual(bare.origin, null);
  assert.strictEqual(bare.orientation, null);
});

test('test_strided_dim_matches_backend_ceil', () => {
  // H13: mirrors volume_service.strided_dim (ceil, never floor).
  assert.strictEqual(G.stridedDim(5, 2), 3);
  assert.strictEqual(G.stridedDim(7, 2), 4);
  assert.strictEqual(G.stridedDim(6, 2), 3);
  assert.strictEqual(G.stridedDim(5, 1), 5);
  assert.strictEqual(G.stridedDim(5, 0), 5);
  assert.strictEqual(G.stridedDim(0, 3), 0);
  for (let n = 1; n <= 40; n++) {
    for (let step = 1; step <= 8; step++) {
      let expected = 0;
      for (let x = 0; x < n; x += step) { expected++; }
      assert.strictEqual(G.stridedDim(n, step), expected, `n=${n} step=${step}`);
    }
  }
});

/* -------------------------------------------------------------------------- */
/* voxel <-> patient                                                          */
/* -------------------------------------------------------------------------- */

test('test_voxel_to_patient_anisotropic', () => {
  // row 2 mm (patient +x), col 1 mm (patient +y), slice 2.5 mm (patient +z)
  const meta = G.makeMeta({
    rows: 6, columns: 8, slices: 5,
    pixel_spacing: [2, 1], slice_spacing: 2.5,
    image_position_patient: [10, 20, 30],
    image_orientation_patient: [1, 0, 0, 0, 1, 0],
  });
  const p = G.voxelToPatient({ i: 3, j: 2, k: 4 }, meta);
  // i -> col direction (+y) * col spacing(1) ; j -> row direction (+x) * row spacing(2)
  approx(p[0], 10 + 2 * 2, CLOSE, 'x');   // 14
  approx(p[1], 20 + 3 * 1, CLOSE, 'y');   // 23
  approx(p[2], 30 + 4 * 2.5, CLOSE, 'z'); // 40
  approx(p[0], 14, CLOSE, 'x exact');
  approx(p[1], 23, CLOSE, 'y exact');
  approx(p[2], 40, CLOSE, 'z exact');
});

test('test_patient_to_voxel_roundtrip', () => {
  const meta = G.makeMeta({
    rows: 6, columns: 8, slices: 5,
    pixel_spacing: [2, 1], slice_spacing: 2.5,
    image_position_patient: [10, 20, 30],
    image_orientation_patient: [1, 0, 0, 0, 1, 0],
  });
  const voxels = [{ i: 0, j: 0, k: 0 }, { i: 3, j: 2, k: 4 }, { i: 7, j: 5, k: 1 }, { i: 1.5, j: 2.25, k: 3.75 }];
  for (const v of voxels) {
    const p = G.voxelToPatient(v, meta);
    const back = G.patientToVoxel({ x: p[0], y: p[1], z: p[2] }, meta);
    approx(back.i, v.i, 1e-9, `i roundtrip ${JSON.stringify(v)}`);
    approx(back.j, v.j, 1e-9, `j roundtrip ${JSON.stringify(v)}`);
    approx(back.k, v.k, 1e-9, `k roundtrip ${JSON.stringify(v)}`);
  }

  // A one-step shift along each index axis moves exactly one spacing.
  const base = G.voxelToPatient({ i: 0, j: 0, k: 0 }, meta);
  const stepI = G.voxelToPatient({ i: 1, j: 0, k: 0 }, meta);
  const stepJ = G.voxelToPatient({ i: 0, j: 1, k: 0 }, meta);
  const stepK = G.voxelToPatient({ i: 0, j: 0, k: 1 }, meta);
  approx(Math.hypot(stepI[0] - base[0], stepI[1] - base[1], stepI[2] - base[2]), 1.0, CLOSE, 'i step = col spacing');
  approx(Math.hypot(stepJ[0] - base[0], stepJ[1] - base[1], stepJ[2] - base[2]), 2.0, CLOSE, 'j step = row spacing');
  approx(Math.hypot(stepK[0] - base[0], stepK[1] - base[1], stepK[2] - base[2]), 2.5, CLOSE, 'k step = slice spacing');
});

test('test_voxel_index_and_bounds', () => {
  const meta = G.makeMeta({ rows: 6, columns: 8, slices: 5 });
  assert.strictEqual(G.voxelIndex(0, 0, 0, meta), 0);
  assert.strictEqual(G.voxelIndex(1, 0, 0, meta), 1);
  assert.strictEqual(G.voxelIndex(0, 1, 0, meta), 8);
  assert.strictEqual(G.voxelIndex(0, 0, 1, meta), 48);
  assert.strictEqual(G.voxelIndex(8, 0, 0, meta), -1);
  assert.strictEqual(G.voxelIndex(0, 6, 0, meta), -1);
  assert.strictEqual(G.voxelIndex(0, 0, 5, meta), -1);
  assert.strictEqual(G.voxelIndex(-1, 0, 0, meta), -1);

  const { vol, meta: m } = phantom.makeVolume({ rows: 6, cols: 8, slices: 5 });
  assert.strictEqual(G.sampleVoxel(vol, 3, 2, 4, m), phantom.encodeVoxel(3, 2, 4));
  assert.ok(Number.isNaN(G.sampleVoxel(vol, 99, 0, 0, m)));

  // Round-trip a real flat buffer index (not a phantom value).
  const flat = G.voxelIndex(3, 2, 4, meta);
  assert.strictEqual(flat, 4 * meta.rows * meta.cols + 2 * meta.cols + 3);
  assert.deepStrictEqual(G.indexToVoxel(flat, meta), { i: 3, j: 2, k: 4 });
  assert.deepStrictEqual(
    G.indexToVoxel(G.voxelIndex(0, 5, 4, meta), meta),
    { i: 0, j: 5, k: 4 }
  );
});

/* -------------------------------------------------------------------------- */
/* plane extraction and probe consistency                                     */
/* -------------------------------------------------------------------------- */

test('test_sagittal_ordering', () => {
  // C09: sagittal is u = j (Y, rows) horizontally, v = k (Z, slices) vertically.
  const { vol, meta } = phantom.makeVolume({ rows: 6, cols: 8, slices: 5 });
  const index = 3;
  const plane = G.extractPlane(vol, 'sagittal', index, meta);
  const dims = G.planeDims('sagittal', meta);

  assert.strictEqual(dims.w, 6, 'sagittal width must be rows');
  assert.strictEqual(dims.h, 5, 'sagittal height must be slices');
  assert.strictEqual(plane.length, 6 * 5);

  for (let k = 0; k < meta.slices; k++) {
    for (let j = 0; j < meta.rows; j++) {
      const expected = phantom.encodeVoxel(index, j, k);
      assert.strictEqual(
        plane[k * meta.rows + j], expected,
        `sagittal out[k*rows + j] must be vol[k][j][${index}] (k=${k}, j=${j})`
      );
    }
  }

  // The transposed layout (out[j*slices + k]) must NOT match.
  let transposedMatches = 0;
  for (let k = 0; k < meta.slices; k++) {
    for (let j = 0; j < meta.rows; j++) {
      if (plane[j * meta.slices + k] === phantom.encodeVoxel(index, j, k)) { transposedMatches++; }
    }
  }
  assert.ok(transposedMatches < 30, 'sagittal must not be stored transposed');
});

test('test_coronal_probe_matches_display', () => {
  // AC1/C09: displayed voxel == probe voxel for every coronal grid position.
  const { vol, meta } = phantom.makeVolume({ rows: 6, cols: 8, slices: 5, plane: 'coronal', planeIndex: 2 });
  const plane = G.extractPlane(vol, 'coronal', meta.planeIndex, meta);
  const dims = G.planeDims('coronal', meta);

  assert.deepStrictEqual(dims, { w: 8, h: 5 });
  for (let v = 0; v < dims.h; v++) {
    for (let u = 0; u < dims.w; u++) {
      const displayed = plane[v * dims.w + u];
      const voxel = G.viewportToVoxel(u, v, 'coronal', meta);
      assert.deepStrictEqual(voxel, { i: u, j: meta.planeIndex, k: v }, `coronal voxel at u=${u},v=${v}`);
      const probed = G.sampleVoxel(vol, voxel.i, voxel.j, voxel.k, meta);
      assert.strictEqual(displayed, probed, `coronal mismatch at u=${u},v=${v}`);
      assert.strictEqual(displayed, phantom.encodeVoxel(u, meta.planeIndex, v));
    }
  }
});

test('test_axial_and_sagittal_probe_match_display', () => {
  // AC1/AC2: same invariant for axial and sagittal.
  for (const spec of [
    { plane: 'axial', planeIndex: 3 },
    { plane: 'sagittal', planeIndex: 5 },
  ]) {
    const { vol, meta } = phantom.makeVolume({ rows: 6, cols: 8, slices: 5, plane: spec.plane, planeIndex: spec.planeIndex });
    const plane = G.extractPlane(vol, spec.plane, spec.planeIndex, meta);
    const dims = G.planeDims(spec.plane, meta);
    for (let v = 0; v < dims.h; v++) {
      for (let u = 0; u < dims.w; u++) {
        const voxel = G.viewportToVoxel(u, v, spec.plane, meta);
        assert.strictEqual(
          plane[v * dims.w + u],
          G.sampleVoxel(vol, voxel.i, voxel.j, voxel.k, meta),
          `${spec.plane} mismatch at u=${u},v=${v}`
        );
      }
    }
  }
});

test('test_labelled_voxels_round_trip_through_every_plane', () => {
  // AC1/AC2: three labelled voxels must be reachable and identical in all planes.
  const { vol, meta } = phantom.makeVolume({ rows: 6, cols: 8, slices: 5 });
  const planeSpecs = [
    { plane: 'axial', normal: 'k' },
    { plane: 'sagittal', normal: 'i' },
    { plane: 'coronal', normal: 'j' },
  ];
  for (const labelled of phantom.LABELLED_VOXELS) {
    for (const spec of planeSpecs) {
      const planeIndex = labelled[spec.normal];
      const withIndex = G.makeMeta(meta, spec.plane, planeIndex);
      const plane = G.extractPlane(vol, spec.plane, planeIndex, withIndex);
      const dims = G.planeDims(spec.plane, withIndex);
      const grid = G.voxelToViewport(labelled.i, labelled.j, labelled.k, spec.plane, withIndex);
      const displayed = plane[grid.v * dims.w + grid.u];
      assert.strictEqual(
        displayed, phantom.encodeVoxel(labelled.i, labelled.j, labelled.k),
        `${spec.plane} must display the labelled voxel ${JSON.stringify(labelled)}`
      );
      // And the inverse: the grid position maps back to that same voxel.
      const back = G.viewportToVoxel(grid.u, grid.v, spec.plane, withIndex);
      assert.deepStrictEqual(back, { i: labelled.i, j: labelled.j, k: labelled.k });
    }
  }
});

/* -------------------------------------------------------------------------- */
/* physical measurements                                                      */
/* -------------------------------------------------------------------------- */

test('test_physical_length_2mm_x_1mm', () => {
  // AC3: 3 grid steps across (1 mm) + 2 grid steps down (2 mm) = 5 mm.
  const meta = G.makeMeta({
    rows: 8, columns: 8, slices: 4,
    pixel_spacing: [2, 1], slice_spacing: 2.5, plane: 'axial', planeIndex: 1,
  });
  const mm = G.computePhysicalLength({ u: 0, v: 0 }, { u: 3, v: 2 }, meta);
  approx(mm, 5.0, 1e-9, '3-4-5 triangle in mm');

  // The same displacement in raw grid units would be sqrt(9+4) ~= 3.606 — the
  // spacing-unaware result this fix removes.
  assert.ok(Math.abs(mm - Math.sqrt(13)) > 1, 'must not return the grid-space distance');

  // 100 mm along the u axis (1 mm per step) -> exactly 100 mm.
  const hundred = G.computePhysicalLength({ u: 0, v: 0 }, { u: 100, v: 0 }, meta);
  approx(hundred, 100, 1e-9, '100 mm ruler');

  // ...and along the v axis (2 mm per step) -> 50 steps.
  const hundredV = G.computePhysicalLength({ u: 0, v: 0 }, { u: 0, v: 50 }, meta);
  approx(hundredV, 100, 1e-9, '100 mm ruler on v');
});

test('test_physical_length_accepts_fraction_points', () => {
  // The viewer stores {fx, fy} fractions; those must resolve to the same mm.
  const meta = G.makeMeta({
    rows: 11, columns: 11, slices: 3,
    pixel_spacing: [1, 1], slice_spacing: 1, plane: 'axial', planeIndex: 1,
  });
  const fromGrid = G.computePhysicalLength({ u: 0, v: 0 }, { u: 10, v: 0 }, meta);
  const fromFractions = G.computePhysicalLength({ fx: 0, fy: 0 }, { fx: 1, fy: 0 }, meta);
  approx(fromGrid, 10, 1e-9, 'grid');
  approx(fromFractions, 10, 1e-9, 'fractions');
});

test('test_cobb_angle_45deg_anisotropic', () => {
  // AC3: with 1 mm (u) x 2 mm (v) spacing, 2 steps across + 1 step down is 45°.
  const meta = G.makeMeta({
    rows: 8, columns: 8, slices: 4,
    pixel_spacing: [2, 1], slice_spacing: 2.5, plane: 'axial', planeIndex: 1,
  });
  const { p1, p2, expectedDeg } = phantom.points45(meta);
  approx(p2.u, 2, CLOSE, 'phantom du');
  approx(p2.v, 1, CLOSE, 'phantom dv');

  const cobb = G.computeCobbAngle(p1, p2, { u: 0, v: 4 }, { u: 2, v: 4 }, meta);
  approx(cobb, expectedDeg, 0.5, 'Cobb angle in physical space');

  // The normalized-space (spacing-unaware) answer would be ~26.565 degrees.
  approx(Math.atan2(1, 2) * (180 / Math.PI), 26.56505117707799, 1e-9, 'grid-space angle for contrast');
  assert.ok(Math.abs(cobb - 26.56505117707799) > 10, 'must not return the grid-space angle');

  // A 3-point angle at the middle point behaves the same way.
  const angle = G.computePhysicalAngle(p1, { u: 0, v: 1 }, p2, meta);
  approx(angle, 90, 1e-6, 'right angle at the shared vertex');

  // Parallel lines -> 0 degrees; perpendicular -> 90 degrees. Angles are
  // recovered through acos, so the tolerance is 1e-3 deg (well inside the
  // +/- 0.5 deg clinical tolerance required by AC3).
  approx(G.computeCobbAngle({ u: 0, v: 0 }, { u: 2, v: 1 }, { u: 0, v: 3 }, { u: 2, v: 4 }, meta), 0, 1e-3, 'parallel');
  // +45 deg (du=2, dv=+1 -> 2 mm, 2 mm) against -45 deg (du=2, dv=-1) = 90 deg.
  approx(G.computeCobbAngle({ u: 0, v: 0 }, { u: 2, v: 1 }, { u: 0, v: 0 }, { u: 2, v: -1 }, meta), 90, 1e-3, 'perpendicular');
});

test('test_cobb_angle_is_acute', () => {
  // Cobb is conventionally reported as the acute angle between the endplates.
  const meta = G.makeMeta({
    rows: 8, columns: 8, slices: 4,
    pixel_spacing: [1, 1], slice_spacing: 1, plane: 'axial', planeIndex: 1,
  });
  const angle = G.computeCobbAngle({ u: 0, v: 0 }, { u: 1, v: 1 }, { u: 0, v: 2 }, { u: 1, v: 1 }, meta);
  assert.ok(angle <= 90, `expected an acute angle, got ${angle}`);
});

test('test_physical_ratio', () => {
  // AC3: ratio of 2:1 physical lengths.
  assert.strictEqual(G.computePhysicalRatio(100, 50), 2);
  assert.strictEqual(G.computePhysicalRatio(50, 100), 0.5);
  assert.strictEqual(G.computePhysicalRatio(10, 0), 0);

  // The ratio must be taken on physical lengths, not grid counts.
  const meta = G.makeMeta({
    rows: 8, columns: 8, slices: 4,
    pixel_spacing: [2, 1], slice_spacing: 2.5, plane: 'axial', planeIndex: 1,
  });
  const heart = G.computePhysicalLength({ u: 0, v: 0 }, { u: 0, v: 25 }, meta); // 50 mm
  const thorax = G.computePhysicalLength({ u: 0, v: 0 }, { u: 0, v: 50 }, meta); // 100 mm
  approx(G.computePhysicalRatio(heart, thorax), 0.5, 1e-9, 'CTR ratio');
});

test('test_ellipse_area_anisotropic', () => {
  // ROI area in cm2 from semi-axes measured in mm along u and v.
  const meta = G.makeMeta({
    rows: 40, columns: 40, slices: 3,
    pixel_spacing: [2, 1], slice_spacing: 1, plane: 'axial', planeIndex: 1,
  });
  // semi-axes: 10 steps u (10 mm) and 5 steps v (10 mm) -> 1 cm each
  const area = G.computeEllipseAreaCm2({ u: 0, v: 0 }, { u: 10, v: 5 }, meta);
  approx(area, Math.PI * 1 * 1, 1e-9, 'area cm2');

  // Ignoring spacing (10 x 5 grid) would give a different answer.
  assert.ok(Math.abs(area - (Math.PI * 10 * 5) / 100) > 0.1, 'must be spacing-aware');
});

/* -------------------------------------------------------------------------- */
/* plane extraction vs. backend reslice convention                            */
/* -------------------------------------------------------------------------- */

test('test_extract_plane_matches_backend_convention', () => {
  // C09: same data[k, j] / data[k, i] / data[j, i] mapping the server returns.
  const { vol, meta } = phantom.makeVolume({ rows: 4, cols: 6, slices: 5 });

  const axial = G.extractPlane(vol, 'axial', 2, meta);
  assert.strictEqual(axial.length, meta.rows * meta.cols);
  for (let j = 0; j < meta.rows; j++) {
    for (let i = 0; i < meta.cols; i++) {
      assert.strictEqual(axial[j * meta.cols + i], phantom.encodeVoxel(i, j, 2));
    }
  }

  const sagittal = G.extractPlane(vol, 'sagittal', 4, meta);
  assert.strictEqual(sagittal.length, meta.slices * meta.rows);
  for (let k = 0; k < meta.slices; k++) {
    for (let j = 0; j < meta.rows; j++) {
      assert.strictEqual(sagittal[k * meta.rows + j], phantom.encodeVoxel(4, j, k));
    }
  }

  const coronal = G.extractPlane(vol, 'coronal', 1, meta);
  assert.strictEqual(coronal.length, meta.slices * meta.cols);
  for (let k = 0; k < meta.slices; k++) {
    for (let i = 0; i < meta.cols; i++) {
      assert.strictEqual(coronal[k * meta.cols + i], phantom.encodeVoxel(i, 1, k));
    }
  }
});

test('test_extract_plane_rejects_out_of_range_index', () => {
  const { vol, meta } = phantom.makeVolume({ rows: 4, cols: 6, slices: 5 });
  assert.throws(() => G.extractPlane(vol, 'sagittal', 6, meta), /out of range/);
  assert.throws(() => G.extractPlane(vol, 'axial', -1, meta), /out of range/);
  assert.throws(() => G.planeAxes('oblique'), /unknown plane/);
});

test('test_phantom_helpers_are_self_consistent', () => {
  // Guard the phantoms themselves: a broken phantom would hide a real defect.
  const { meta } = phantom.makeVolume(phantom.ODD_5x7);
  assert.strictEqual(meta.rows, 5);
  assert.strictEqual(meta.cols, 7);
  assert.strictEqual(meta.slices, 5);

  const anisotropic = G.makeMeta({
    rows: 8, columns: 8, slices: 4,
    pixel_spacing: [phantom.ANISOTROPIC.rowSpacing, phantom.ANISOTROPIC.colSpacing],
    slice_spacing: phantom.ANISOTROPIC.sliceSpacing,
    plane: 'axial', planeIndex: 1,
  });
  const hundred = phantom.points100mm(anisotropic);
  approx(G.computePhysicalLength(hundred.p1, hundred.p2, anisotropic), hundred.expectedMm, 1e-9, '100 mm phantom');

  for (const voxel of phantom.LABELLED_VOXELS) {
    const encoded = phantom.encodeVoxel(voxel.i, voxel.j, voxel.k);
    assert.deepStrictEqual(phantom.decodeVoxel(encoded), voxel);
  }
});
