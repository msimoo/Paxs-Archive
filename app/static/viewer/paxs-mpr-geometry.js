/* ==========================================================================
   PAXS MPR geometry — single source of truth for voxel / viewport / patient
   coordinate transforms and physical measurements.

   Loaded as a classic script BEFORE paxs-mpr.js (viewer/index.html) and also
   requireable from Node for unit tests:
     <script src="/viewer/paxs-mpr-geometry.js?v=37"></script>
     const G = require('.../paxs-mpr-geometry.js')

   ---------------------------------------------------------------------------
   CANONICAL AXIS CONVENTION  (must match the backend, see
   app/services/volume_service.py — reslice_plane / mip_project)

   The volume buffer is a Float32Array indexed [k][j][i]:

       index = k * rows * cols + j * cols + i
       i = column index (patient X for an axial acquisition)
       j = row    index (patient Y for an axial acquisition)
       k = slice  index (patient Z for an axial acquisition)

   Each orthogonal plane has a horizontal u axis and a vertical v axis:

       plane     const   u (horizontal)   v (vertical)    grid width x height
       -----------------------------------------------------------------------
       axial     k       i (X, cols)      j (Y, rows)     cols x rows
       sagittal  i       j (Y, rows)      k (Z, slices)   rows x slices
       coronal   j       i (X, cols)      k (Z, slices)   cols x slices

   This is the same convention the backend declares (ResliceResult rows/cols
   and its [vertical, horizontal] mm spacing), and the same one the plane
   labels in the UI use ("AXIAL · Z", "SAGITTAL · X", "CORONAL · Y" = the
   constant index axis).

   All previously inline maths (probe/ROI voxel lookups, crosshairs, ruler,
   Cobb angle, CTR ratio, ROI area, plane extractors, WebGL2 slice shader)
   must call these helpers — do not re-derive indices anywhere else.
   ========================================================================== */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) { module.exports = api; }
  if (root) { root.PaxsMprGeometry = api; }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // plane -> u/v/normal voxel axes and the meta fields holding their sizes
  var PLANES = {
    axial: { u: 'i', v: 'j', normal: 'k', uDim: 'cols', vDim: 'rows', uSpacing: 'col', vSpacing: 'row' },
    sagittal: { u: 'j', v: 'k', normal: 'i', uDim: 'rows', vDim: 'slices', uSpacing: 'row', vSpacing: 'slice' },
    coronal: { u: 'i', v: 'k', normal: 'j', uDim: 'cols', vDim: 'slices', uSpacing: 'col', vSpacing: 'slice' }
  };

  function fail(message) {
    throw new Error('paxs-mpr-geometry: ' + message);
  }

  function num(value, fallback) {
    var n = Number(value);
    return isFinite(n) ? n : fallback;
  }

  function isArray(value) {
    return Object.prototype.toString.call(value) === '[object Array]';
  }

  /* ------------------------------------------------------------------------ */
  /* meta                                                                     */
  /* ------------------------------------------------------------------------ */

  /**
   * Build the canonical meta object from a `/api/v1/volume/{series}/info`
   * payload (or from an equivalent object built in tests).
   *
   * @param {Object} info   volume info (rows, columns, slices, pixel_spacing,
   *                        slice_spacing|slice_thickness, image_position_patient,
   *                        image_orientation_patient)
   * @param {string} plane  active plane (axial|sagittal|coronal)
   * @param {number} planeIndex current index along the plane's normal axis
   */
  function makeMeta(info, plane, planeIndex) {
    if (!info) { fail('makeMeta requires volume info'); }
    var spacing = info.spacing;
    var rowSpacing;
    var colSpacing;
    var sliceSpacing;
    if (spacing && !isArray(spacing)) {
      rowSpacing = num(spacing.row, 1);
      colSpacing = num(spacing.col, 1);
      sliceSpacing = num(spacing.slice, 1);
    } else {
      // API form: pixel_spacing = [row, col] (DICOM order)
      var ps = isArray(spacing) ? spacing : info.pixel_spacing;
      rowSpacing = num(ps && ps[0], 1);
      colSpacing = num(ps && ps[1], 1);
      sliceSpacing = num(info.slice_spacing != null ? info.slice_spacing : info.slice_thickness, 1);
    }
    var rows = Math.round(num(info.rows, 0));
    var cols = Math.round(num(info.columns != null ? info.columns : info.cols, 0));
    var slices = Math.round(num(info.slices, 0));
    return {
      rows: rows,
      cols: cols,
      slices: slices,
      spacing: { row: rowSpacing, col: colSpacing, slice: sliceSpacing },
      origin: info.image_position_patient || info.origin || null,
      orientation: info.image_orientation_patient || info.orientation || null,
      plane: plane || info.plane || null,
      planeIndex: Math.round(num(planeIndex != null ? planeIndex : info.planeIndex, 0))
    };
  }

  /** Accept either canonical meta or the raw API payload. */
  function toMeta(meta) {
    if (!meta) { fail('meta is required'); }
    if (meta.spacing && !isArray(meta.spacing) && meta.rows != null && meta.cols != null) {
      return meta;
    }
    return makeMeta(meta, meta.plane, meta.planeIndex);
  }

  function planeOf(plane, meta) {
    var name = plane || (meta && meta.plane);
    var table = PLANES[name];
    if (!table) { fail('unknown plane "' + name + '"'); }
    return table;
  }

  /* ------------------------------------------------------------------------ */
  /* plane geometry                                                           */
  /* ------------------------------------------------------------------------ */

  /** Voxel axis names of a plane: {u, v, normal} (e.g. sagittal -> j, k, i). */
  function planeAxes(plane) {
    var t = planeOf(plane, null);
    return { u: t.u, v: t.v, normal: t.normal };
  }

  /** Data-grid size of a plane: {w, h} = number of u samples, v samples. */
  function planeDims(plane, meta) {
    var m = toMeta(meta);
    var t = planeOf(plane, m);
    return { w: m[t.uDim], h: m[t.vDim] };
  }

  /** Physical mm per data-grid step on a plane: {u, v}. */
  function planeSpacing(plane, meta) {
    var m = toMeta(meta);
    var t = planeOf(plane, m);
    return { u: m.spacing[t.uSpacing], v: m.spacing[t.vSpacing] };
  }

  /** Index range of the plane's constant (normal) axis. */
  function planeIndexCount(plane, meta) {
    var m = toMeta(meta);
    var t = planeOf(plane, m);
    return t.normal === 'i' ? m.cols : (t.normal === 'j' ? m.rows : m.slices);
  }

  /** Number of samples a strided read yields (ceil) — mirrors the backend
   *  `volume_service.strided_dim` so declared and sampled geometry agree. */
  function stridedDim(n, step) {
    var count = Math.round(num(n, 0));
    var s = Math.max(1, Math.round(num(step, 1)) || 1);
    if (count <= 0) { return 0; }
    return Math.ceil(count / s);
  }

  /* ------------------------------------------------------------------------ */
  /* voxel indexing                                                           */
  /* ------------------------------------------------------------------------ */

  /** Flat buffer index for a voxel, or -1 when outside the volume. */
  function voxelIndex(i, j, k, meta) {
    var m = toMeta(meta);
    if (i < 0 || j < 0 || k < 0 || i >= m.cols || j >= m.rows || k >= m.slices) { return -1; }
    return k * m.rows * m.cols + j * m.cols + i;
  }

  /** Voxel coordinates for a flat buffer index. */
  function indexToVoxel(index, meta) {
    var m = toMeta(meta);
    var plane = m.rows * m.cols;
    var k = Math.floor(index / plane);
    var rem = index - k * plane;
    var j = Math.floor(rem / m.cols);
    return { i: rem - j * m.cols, j: j, k: k };
  }

  /** Read one voxel through the volume buffer, or NaN when out of bounds. */
  function sampleVoxel(vol, i, j, k, meta) {
    var index = voxelIndex(i, j, k, meta);
    return index < 0 ? NaN : vol[index];
  }

  /* ------------------------------------------------------------------------ */
  /* viewport <-> voxel                                                       */
  /* ------------------------------------------------------------------------ */

  /**
   * Data-grid point -> voxel.
   * `u`/`v` are data-grid coordinates along the plane's u/v axes (fractional
   * allowed); the normal axis comes from `meta.planeIndex`.
   */
  function viewportToVoxel(u, v, plane, meta) {
    var m = toMeta(meta);
    var t = planeOf(plane, m);
    var voxel = { i: 0, j: 0, k: 0 };
    voxel[t.normal] = m.planeIndex;
    voxel[t.u] = u;
    voxel[t.v] = v;
    return voxel;
  }

  /** Voxel -> data-grid point on a plane: {u, v}. */
  function voxelToViewport(i, j, k, plane, meta) {
    var m = toMeta(meta);
    var t = planeOf(plane, m);
    var voxel = { i: i, j: j, k: k };
    return { u: voxel[t.u], v: voxel[t.v] };
  }

  /**
   * A measurement point -> data-grid {u, v}.
   * Accepts `{u, v}` (data grid) or `{fx, fy}` (fractions of the plane rect).
   */
  function pointToGrid(point, plane, meta) {
    if (!point) { fail('measurement point is required'); }
    if (point.u != null || point.v != null) {
      return { u: num(point.u, 0), v: num(point.v, 0) };
    }
    var dims = planeDims(plane, meta);
    var spanU = dims.w > 1 ? dims.w - 1 : 1;
    var spanV = dims.h > 1 ? dims.h - 1 : 1;
    return { u: num(point.fx, 0) * spanU, v: num(point.fy, 0) * spanV };
  }

  /** Measurement point -> voxel on the plane (normal axis = meta.planeIndex). */
  function pointToVoxel(point, plane, meta) {
    var grid = pointToGrid(point, plane, meta);
    return viewportToVoxel(grid.u, grid.v, plane, meta);
  }

  /* ------------------------------------------------------------------------ */
  /* patient space                                                            */
  /* ------------------------------------------------------------------------ */

  function normalOf(orientation) {
    if (!orientation || orientation.length < 6) { return [0, 0, 1]; }
    var rx = num(orientation[0], 1), ry = num(orientation[1], 0), rz = num(orientation[2], 0);
    var cx = num(orientation[3], 0), cy = num(orientation[4], 1), cz = num(orientation[5], 0);
    var nx = ry * cz - rz * cy;
    var ny = rz * cx - rx * cz;
    var nz = rx * cy - ry * cx;
    var norm = Math.sqrt(nx * nx + ny * ny + nz * nz) || 1;
    return [nx / norm, ny / norm, nz / norm];
  }

  /**
   * 3x3 basis of the volume: columns are the physical direction of one step
   * along i (column direction), j (row direction) and k (slice normal).
   * Falls back to the index axes when the orientation is unavailable.
   */
  function basisOf(meta) {
    var m = toMeta(meta);
    var orientation = m.orientation;
    var rowVec = orientation && orientation.length >= 6
      ? [num(orientation[0], 1), num(orientation[1], 0), num(orientation[2], 0)]
      : [1, 0, 0];
    var colVec = orientation && orientation.length >= 6
      ? [num(orientation[3], 0), num(orientation[4], 1), num(orientation[5], 0)]
      : [0, 1, 0];
    var normal = normalOf(orientation);
    var s = m.spacing;
    // column 0 -> i, column 1 -> j, column 2 -> k
    return [
      [colVec[0] * s.col, rowVec[0] * s.row, normal[0] * s.slice],
      [colVec[1] * s.col, rowVec[1] * s.row, normal[1] * s.slice],
      [colVec[2] * s.col, rowVec[2] * s.row, normal[2] * s.slice]
    ];
  }

  function originOf(meta) {
    var m = toMeta(meta);
    var o = m.origin;
    if (!o || o.length < 3) { return [0, 0, 0]; }
    return [num(o[0], 0), num(o[1], 0), num(o[2], 0)];
  }

  function applyBasis(basis, i, j, k, origin) {
    return [
      origin[0] + basis[0][0] * i + basis[0][1] * j + basis[0][2] * k,
      origin[1] + basis[1][0] * i + basis[1][1] * j + basis[1][2] * k,
      origin[2] + basis[2][0] * i + basis[2][1] * j + basis[2][2] * k
    ];
  }

  function invert3(m) {
    var a = m[0][0], b = m[0][1], c = m[0][2];
    var d = m[1][0], e = m[1][1], f = m[1][2];
    var g = m[2][0], h = m[2][1], i = m[2][2];
    var det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
    if (!det) { fail('volume basis is singular (degenerate orientation/spacing)'); }
    var inv = [
      [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
      [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
      [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]
    ];
    return inv;
  }

  /** Voxel (fractional allowed) -> patient coordinates in mm. */
  function voxelToPatient(voxel, meta) {
    var m = toMeta(meta);
    var v = voxel || {};
    return applyBasis(
      basisOf(m),
      num(v.i, 0), num(v.j, 0), num(v.k, 0),
      originOf(m)
    );
  }

  /** Patient coordinates in mm -> voxel (fractional). */
  function patientToVoxel(patient, meta) {
    var m = toMeta(meta);
    var p = patient || {};
    var basis = basisOf(m);
    var origin = originOf(m);
    var inv = invert3(basis);
    var dx = num(p.x, 0) - origin[0];
    var dy = num(p.y, 0) - origin[1];
    var dz = num(p.z, 0) - origin[2];
    return {
      i: inv[0][0] * dx + inv[0][1] * dy + inv[0][2] * dz,
      j: inv[1][0] * dx + inv[1][1] * dy + inv[1][2] * dz,
      k: inv[2][0] * dx + inv[2][1] * dy + inv[2][2] * dz
    };
  }

  /* ------------------------------------------------------------------------ */
  /* physical measurements                                                    */
  /* ------------------------------------------------------------------------ */

  function distance(a, b) {
    var dx = a[0] - b[0], dy = a[1] - b[1], dz = a[2] - b[2];
    return Math.sqrt(dx * dx + dy * dy + dz * dz);
  }

  /**
   * Physical distance between two measurement points, in mm.
   * Points may be {u, v} data-grid or {fx, fy} fractions; the normal axis is
   * `meta.planeIndex`. Computed through patient space, so anisotropic spacing
   * and oblique orientations are handled exactly.
   */
  function computePhysicalLength(p1, p2, meta) {
    var m = toMeta(meta);
    var plane = m.plane;
    var a = voxelToPatient(pointToVoxel(p1, plane, m), m);
    var b = voxelToPatient(pointToVoxel(p2, plane, m), m);
    return distance(a, b);
  }

  /** Angle in degrees at p2 between (p2 -> p1) and (p2 -> p3). */
  function computePhysicalAngle(p1, p2, p3, meta) {
    var m = toMeta(meta);
    var plane = m.plane;
    var a = voxelToPatient(pointToVoxel(p1, plane, m), m);
    var b = voxelToPatient(pointToVoxel(p2, plane, m), m);
    var c = voxelToPatient(pointToVoxel(p3, plane, m), m);
    return angleBetween(b, a, b, c);
  }

  function angleBetween(originA, pointA, originB, pointB) {
    var u = [pointA[0] - originA[0], pointA[1] - originA[1], pointA[2] - originA[2]];
    var v = [pointB[0] - originB[0], pointB[1] - originB[1], pointB[2] - originB[2]];
    var nu = Math.sqrt(u[0] * u[0] + u[1] * u[1] + u[2] * u[2]);
    var nv = Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
    if (nu === 0 || nv === 0) { return 0; }
    var dot = (u[0] * v[0] + u[1] * v[1] + u[2] * v[2]) / (nu * nv);
    var clamped = Math.max(-1, Math.min(1, dot));
    return Math.acos(clamped) * (180 / Math.PI);
  }

  /**
   * Cobb angle in degrees between two lines (p1->p2 and p3->p4), measured in
   * physical space — the acute angle between the two line directions.
   */
  function computeCobbAngle(p1, p2, p3, p4, meta) {
    var m = toMeta(meta);
    var plane = m.plane;
    var a1 = voxelToPatient(pointToVoxel(p1, plane, m), m);
    var a2 = voxelToPatient(pointToVoxel(p2, plane, m), m);
    var b1 = voxelToPatient(pointToVoxel(p3, plane, m), m);
    var b2 = voxelToPatient(pointToVoxel(p4, plane, m), m);
    var angle = angleBetween(a1, a2, b1, b2);
    return angle > 90 ? 180 - angle : angle;
  }

  /** Unitless ratio of two physical lengths (0 when the denominator is 0). */
  function computePhysicalRatio(length1, length2) {
    var a = num(length1, 0);
    var b = num(length2, 0);
    if (!b) { return 0; }
    return a / b;
  }

  /**
   * Ellipse area in cm2 from a centre point and a point on the ellipse edge
   * (both data-grid or fraction points). Semi-axes are measured in mm along the
   * plane's u/v axes, so anisotropic spacing is honoured.
   */
  function computeEllipseAreaCm2(center, edge, meta) {
    var m = toMeta(meta);
    var plane = m.plane;
    var sp = planeSpacing(plane, m);
    var c = pointToGrid(center, plane, m);
    var e = pointToGrid(edge, plane, m);
    var semiU = Math.abs(e.u - c.u) * sp.u;
    var semiV = Math.abs(e.v - c.v) * sp.v;
    return (Math.PI * semiU * semiV) / 100;
  }

  /* ------------------------------------------------------------------------ */
  /* plane extraction (mirrors the backend reslice_plane output)              */
  /* ------------------------------------------------------------------------ */

  /**
   * Extract one orthogonal plane from the volume buffer.
   * Output layout is row-major with width = planeDims().w, height = .h:
   *   axial    out[j * cols + i]      = vol[k, j, i]
   *   sagittal out[k * rows + j]      = vol[k, j, i]
   *   coronal  out[k * cols + i]      = vol[k, j, i]
   * Returns a Float32Array of length w * h.
   */
  function extractPlane(vol, plane, index, meta) {
    var m = toMeta(meta);
    var t = planeOf(plane, m);
    var dims = planeDims(plane, m);
    var out = new Float32Array(dims.w * dims.h);
    var stride = m.rows * m.cols;
    var count = t.normal === 'i' ? m.cols : (t.normal === 'j' ? m.rows : m.slices);
    if (index < 0 || index >= count) { fail(plane + ' index ' + index + ' out of range 0-' + (count - 1)); }
    var u;
    var v;
    var write = 0;
    for (v = 0; v < dims.h; v++) {
      for (u = 0; u < dims.w; u++) {
        var voxel = { i: 0, j: 0, k: 0 };
        voxel[t.normal] = index;
        voxel[t.u] = u;
        voxel[t.v] = v;
        out[write++] = vol[voxel.k * stride + voxel.j * m.cols + voxel.i];
      }
    }
    return out;
  }

  return {
    PLANES: PLANES,
    makeMeta: makeMeta,
    planeAxes: planeAxes,
    planeDims: planeDims,
    planeSpacing: planeSpacing,
    planeIndexCount: planeIndexCount,
    stridedDim: stridedDim,
    voxelIndex: voxelIndex,
    indexToVoxel: indexToVoxel,
    sampleVoxel: sampleVoxel,
    viewportToVoxel: viewportToVoxel,
    voxelToViewport: voxelToViewport,
    pointToGrid: pointToGrid,
    pointToVoxel: pointToVoxel,
    voxelToPatient: voxelToPatient,
    patientToVoxel: patientToVoxel,
    computePhysicalLength: computePhysicalLength,
    computePhysicalAngle: computePhysicalAngle,
    computeCobbAngle: computeCobbAngle,
    computePhysicalRatio: computePhysicalRatio,
    computeEllipseAreaCm2: computeEllipseAreaCm2,
    extractPlane: extractPlane
  };
});
