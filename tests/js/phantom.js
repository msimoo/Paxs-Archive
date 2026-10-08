/**
 * Non-clinical synthetic phantoms for the MPR geometry tests.
 *
 * Test-only helper (never shipped, never loaded by the viewer). Every voxel
 * value uniquely encodes its own (i, j, k) so a wrong index, a transposed
 * plane or an off-by-one is impossible to miss. No patient data is involved.
 */

'use strict';

/** Unique, human-readable voxel value for a coordinate. */
function encodeVoxel(i, j, k) {
  return k * 1000000 + j * 1000 + i;
}

function decodeVoxel(value) {
  const k = Math.floor(value / 1000000);
  const rest = value - k * 1000000;
  const j = Math.floor(rest / 1000);
  return { i: rest - j * 1000, j, k };
}

/**
 * Build a volume whose every voxel encodes its own coordinate.
 * @returns {{meta: Object, vol: Float32Array}}
 */
function makeVolume(options) {
  const opts = options || {};
  const rows = opts.rows != null ? opts.rows : 6;
  const cols = opts.cols != null ? opts.cols : 8;
  const slices = opts.slices != null ? opts.slices : 5;
  const meta = {
    rows,
    cols,
    slices,
    spacing: {
      row: opts.rowSpacing != null ? opts.rowSpacing : 1.0,
      col: opts.colSpacing != null ? opts.colSpacing : 1.0,
      slice: opts.sliceSpacing != null ? opts.sliceSpacing : 2.5,
    },
    origin: opts.origin || [10, 20, 30],
    orientation: opts.orientation || [1, 0, 0, 0, 1, 0],
    plane: opts.plane || 'axial',
    planeIndex: opts.planeIndex != null ? opts.planeIndex : Math.floor(slices / 2),
  };
  const vol = new Float32Array(rows * cols * slices);
  for (let k = 0; k < slices; k++) {
    for (let j = 0; j < rows; j++) {
      for (let i = 0; i < cols; i++) {
        vol[k * rows * cols + j * cols + i] = encodeVoxel(i, j, k);
      }
    }
  }
  return { meta, vol };
}

/** Anisotropic phantom: 1.0 mm in-plane (col), 2.0 mm (row), 2.5 mm slices. */
const ANISOTROPIC = { rowSpacing: 2.0, colSpacing: 1.0, sliceSpacing: 2.5 };

/** The odd 5x7 shape used by the H13 dimension contract. */
const ODD_5x7 = { rows: 5, cols: 7, slices: 5 };

/** Three labelled voxels at known positions (used by the consistency tests). */
const LABELLED_VOXELS = [
  { i: 1, j: 2, k: 3 },
  { i: 4, j: 0, k: 1 },
  { i: 0, j: 5, k: 4 },
];

/**
 * Two points on a plane whose PHYSICAL displacement is exactly 45 degrees,
 * for a plane whose u spacing is 1 mm and v spacing is 2 mm:
 *   du = 2 grid steps -> 2 mm across
 *   dv = 1 grid step  -> 2 mm down
 * Returns {p1, p2, expectedDeg}.
 */
function points45(meta) {
  const spacing = meta.spacing;
  const uSpacing = meta.plane === 'axial' ? spacing.col : spacing.row;
  const vSpacing = meta.plane === 'axial' ? spacing.row : spacing.slice;
  // du * uSpacing must equal dv * vSpacing
  const dv = 1;
  const du = (dv * vSpacing) / uSpacing;
  return { p1: { u: 0, v: 0 }, p2: { u: du, v: dv }, expectedDeg: 45 };
}

/** Two points 100 mm apart along the plane's u axis. */
function points100mm(meta) {
  const spacing = meta.spacing;
  const uSpacing = meta.plane === 'axial' ? spacing.col : spacing.row;
  const steps = 100 / uSpacing;
  return { p1: { u: 0, v: 0 }, p2: { u: steps, v: 0 }, expectedMm: 100 };
}

module.exports = {
  encodeVoxel,
  decodeVoxel,
  makeVolume,
  ANISOTROPIC,
  ODD_5x7,
  LABELLED_VOXELS,
  points45,
  points100mm,
};
