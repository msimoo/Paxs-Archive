/**
 * V3-2 verification: progressive/chunked WebGL2 3D texture upload.
 *
 * Regression context: initGl2() uploaded the whole Float32Array in ONE
 * synchronous gl.texImage3D call. A 512x512x500 CT is ~500 MB, so the main
 * thread blocked for seconds (the reported UI freeze on large studies).
 *
 * This harness extracts the REAL uploadVolumeTexture() source from the shipping
 * file and drives it against a mock GL context, so the test cannot drift from
 * what actually ships.
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

/** Extract a top-level `function NAME(...) { ... }` by brace matching. */
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

const FN_SRC = extractFunction(SRC, 'uploadVolumeTexture');
const mDirect = SRC.match(/PAXS_TEX_DIRECT_MAX_BYTES\s*=\s*([^;]+);/);
const mChunk = SRC.match(/PAXS_TEX_CHUNK_BYTES\s*=\s*([^;]+);/);
const DIRECT_MAX = eval(mDirect[1]);
const CHUNK_BYTES = eval(mChunk[1]);

console.log('\n[0] Thresholds present and sane in the shipping file');
ok('PAXS_TEX_DIRECT_MAX_BYTES exists', !!mDirect, mDirect && mDirect[1]);
ok('PAXS_TEX_CHUNK_BYTES exists', !!mChunk, mChunk && mChunk[1]);
ok('direct threshold is 64 MB', DIRECT_MAX === 64 * 1024 * 1024, DIRECT_MAX);
ok('chunk size is 8 MB', CHUNK_BYTES === 8 * 1024 * 1024, CHUNK_BYTES);
ok('chunk size < direct threshold', CHUNK_BYTES < DIRECT_MAX);

/**
 * Build a sandbox whose GL_UPLOAD_GEN is a real mutable closure variable, exactly
 * like the shipping module, plus a controllable requestAnimationFrame queue.
 */
function makeSandbox(opts) {
  opts = opts || {};
  const gl = {
    TEXTURE_3D: 'TEXTURE_3D', R32F: 'R32F', RED: 'RED', FLOAT: 'FLOAT',
    calls: [],
    failAllocation: !!opts.failAllocation,
    failAtSlice: opts.failAtSlice,
    lost: !!opts.contextLost,
  };
  gl.texImage3D = function (target, level, ifmt, w, h, d, border, fmt, type, pixels) {
    gl.calls.push({ op: 'texImage3D', w, h, d, hasPixels: pixels != null });
  };
  gl.texStorage3D = function (target, levels, ifmt, w, h, d) {
    gl.calls.push({ op: 'texStorage3D', levels, w, h, d });
  };
  gl.texSubImage3D = function (target, level, x, y, z, w, h, d, fmt, type, pixels) {
    gl.calls.push({ op: 'texSubImage3D', x, y, z, w, h, d, pixelsLen: pixels && pixels.length });
  };
  gl.getError = function () {
    if (gl.failAllocation) { gl.failAllocation = false; return 1285; }
    if (gl.failAtSlice != null &&
        gl.calls.some((c) => c.op === 'texSubImage3D' && c.z >= gl.failAtSlice)) {
      gl.failAtSlice = null;
      return 1285;
    }
    return 0;
  };
  gl.isContextLost = function () { return gl.lost; };

  const cols = opts.cols || 8, rows = opts.rows || 8, slices = opts.slices || 4;
  const state = {
    cols, rows, slices,
    vol: new Float32Array(cols * rows * slices),
    texUpload: null,
    gl2: { supported: true },
  };

  const rafQueue = [];
  const raf = function (fn) { rafQueue.push(fn); return rafQueue.length; };

  const api = new Function(
    'state', 'console', 'requestAnimationFrame',
    'PAXS_TEX_DIRECT_MAX_BYTES', 'PAXS_TEX_CHUNK_BYTES',
    'var GL_UPLOAD_GEN = 0;\n' +
    FN_SRC + '\n' +
    'return {' +
    '  upload: uploadVolumeTexture,' +
    '  bumpGen: function () { GL_UPLOAD_GEN++; },' +
    '  gen: function () { return GL_UPLOAD_GEN; }' +
    '};'
  )(state, console, raf, DIRECT_MAX, CHUNK_BYTES);

  return {
    state, gl, rafQueue, api,
    flush(maxFrames) {
      let n = 0;
      while (rafQueue.length && n < (maxFrames || 100000)) { rafQueue.shift()(); n++; }
      return n;
    },
  };
}

console.log('\n[1] Small volumes keep the single-call path (zero regression)');
{
  const sb = makeSandbox({ cols: 512, rows: 512, slices: 40 });   // ~42 MB
  const bytes = 512 * 512 * 40 * 4;
  ok('volume is under the direct threshold', bytes <= DIRECT_MAX, bytes);
  ok('returns true', sb.api.upload(sb.gl, { id: 'tex' }, bytes) === true);
  ok('used texImage3D exactly once',
     sb.gl.calls.filter((c) => c.op === 'texImage3D').length === 1);
  ok('did NOT use texStorage3D',
     sb.gl.calls.filter((c) => c.op === 'texStorage3D').length === 0);
  ok('did NOT use texSubImage3D',
     sb.gl.calls.filter((c) => c.op === 'texSubImage3D').length === 0);
  ok('no frames scheduled (synchronous)', sb.rafQueue.length === 0, sb.rafQueue.length);
  ok('upload marked complete', sb.state.texUpload.done === true);
}

console.log('\n[2] Large volumes use texStorage3D + chunked texSubImage3D');
{
  const cols = 512, rows = 512, slices = 600;
  const bytes = cols * rows * slices * 4;                          // ~629 MB
  const sb = makeSandbox({ cols, rows, slices });
  ok('volume is over the direct threshold', bytes > DIRECT_MAX, bytes);

  ok('returns true (upload scheduled)', sb.api.upload(sb.gl, { id: 'tex' }, bytes) === true);
  ok('used texStorage3D once',
     sb.gl.calls.filter((c) => c.op === 'texStorage3D').length === 1);
  ok('allocated immutable storage at full size',
     sb.gl.calls.some((c) => c.op === 'texStorage3D' && c.w === cols && c.h === rows && c.d === slices));
  ok('no monolithic texImage3D with pixels',
     sb.gl.calls.filter((c) => c.op === 'texImage3D' && c.hasPixels).length === 0);
  ok('the first frame did NOT upload everything (proves chunking)',
     sb.gl.calls.filter((c) => c.op === 'texSubImage3D').length < slices,
     sb.gl.calls.filter((c) => c.op === 'texSubImage3D').length);

  const frames = sb.flush();
  ok('yielded across MULTIPLE animation frames', frames > 1, frames);

  const subs = sb.gl.calls.filter((c) => c.op === 'texSubImage3D');
  ok('uploaded every slice exactly once', subs.length === slices, subs.length);
  ok('slices are contiguous 0..N-1', subs.every((c, i) => c.z === i),
     subs.slice(0, 3).map((c) => c.z).join(','));
  ok('each slab is 1 slice deep', subs.every((c) => c.d === 1));
  ok('each slab carries the right voxel count',
     subs.every((c) => c.pixelsLen === rows * cols), subs[0] && subs[0].pixelsLen);
  ok('upload marked complete', sb.state.texUpload.done === true);
  ok('progress reached the slice count',
     sb.state.texUpload.uploaded === slices, sb.state.texUpload.uploaded);

  const perChunk = Math.max(1, Math.floor(CHUNK_BYTES / (rows * cols * 4)));
  ok('slices-per-frame derives from PAXS_TEX_CHUNK_BYTES',
     perChunk >= 1 && perChunk < slices, 'slices/frame=' + perChunk);
  // The FIRST chunk runs synchronously (so the viewport gets data on the very
  // first frame); only the remaining chunks are scheduled on rAF.
  const expectedFrames = Math.ceil(slices / perChunk) - 1;
  ok('frames scheduled == chunks - 1 (first chunk is synchronous)',
     frames === expectedFrames, frames + ' vs ' + expectedFrames);
}

console.log('\n[3] Generation bump (releaseGl2) aborts a pending upload');
{
  const cols = 512, rows = 512, slices = 600;
  const sb = makeSandbox({ cols, rows, slices });
  sb.api.upload(sb.gl, { id: 'tex' }, cols * rows * slices * 4);
  const before = sb.gl.calls.filter((c) => c.op === 'texSubImage3D').length;
  ok('some slices uploaded before the abort', before > 0, before);

  sb.api.bumpGen();                       // <- what releaseGl2() now does
  const framesAfter = sb.flush();
  const after = sb.gl.calls.filter((c) => c.op === 'texSubImage3D').length;
  ok('no further slices were written after the bump', after === before,
     `${before} -> ${after}`);
  ok('the loop stopped immediately (no frames ran)', framesAfter <= 1, framesAfter);
}

console.log('\n[4] Context loss aborts the upload loop');
{
  const cols = 512, rows = 512, slices = 600;
  const sb = makeSandbox({ cols, rows, slices });
  sb.api.upload(sb.gl, { id: 'tex' }, cols * rows * slices * 4);
  const before = sb.gl.calls.filter((c) => c.op === 'texSubImage3D').length;
  sb.gl.lost = true;                      // GPU went away mid-upload
  sb.flush();
  const after = sb.gl.calls.filter((c) => c.op === 'texSubImage3D').length;
  ok('the loop stopped promptly', after - before <= 1, 'extra=' + (after - before));
}

console.log('\n[5] Allocation failure falls back instead of uploading garbage');
{
  const cols = 512, rows = 512, slices = 600;
  const sb = makeSandbox({ cols, rows, slices, failAllocation: true });
  const res = sb.api.upload(sb.gl, { id: 'tex' }, cols * rows * slices * 4);
  ok('returns false so initGl2 can fall back to CPU rendering', res === false, res);
  ok('no slices were uploaded',
     sb.gl.calls.filter((c) => c.op === 'texSubImage3D').length === 0);
  ok('failure recorded in state.texUpload',
     sb.state.texUpload && sb.state.texUpload.failed === true, sb.state.texUpload);
}

console.log('\n[6] Mid-upload driver error stops the loop and reports it');
{
  const cols = 512, rows = 512, slices = 600;
  const perChunk = Math.max(1, Math.floor(CHUNK_BYTES / (cols * rows * 4)));
  const sb = makeSandbox({ cols, rows, slices, failAtSlice: perChunk * 2 });
  sb.api.upload(sb.gl, { id: 'tex' }, cols * rows * slices * 4);
  sb.flush();
  const uploaded = sb.gl.calls.filter((c) => c.op === 'texSubImage3D').length;
  ok('stopped before uploading all slices', uploaded < slices, uploaded);
  ok('failure recorded', sb.state.texUpload.failed === true, sb.state.texUpload);
}

console.log('\n[7] Source-level wiring checks (initGl2 / releaseGl2)');
{
  ok('initGl2 no longer calls texImage3D directly with state.vol',
     !/gl\.texImage3D\([\s\S]{0,200}?state\.vol\s*\)/.test(SRC));
  ok('initGl2 delegates to uploadVolumeTexture',
     /uploadVolumeTexture\(gl, tex, texBytes\)/.test(SRC));
  ok('releaseGl2 bumps GL_UPLOAD_GEN',
     /function releaseGl2\(\)\s*\{[\s\S]{0,300}?GL_UPLOAD_GEN\+\+/.test(SRC));
  ok('releaseGl2 clears state.texUpload',
     /function releaseGl2\(\)\s*\{[\s\S]{0,300}?state\.texUpload = null/.test(SRC));
  ok('state declares texUpload',
     /texUpload:\s*null/.test(SRC));
}

console.log('\n===============================');
console.log('  PASS: ' + pass + '   FAIL: ' + fail);
console.log('===============================\n');
process.exit(fail ? 1 : 0);
