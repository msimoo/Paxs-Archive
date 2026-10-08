/**
 * PAXS Viewer memory / WebGL regression harness (H-10, H-11, H-12, L-8).
 *
 * Extracted-source harness: the real function text is pulled out of the
 * shipping file with brace matching and driven against mocks, so the test
 * cannot drift from what actually ships.
 *
 *   H-10  prefetchServerRange must evict frames outside a sliding window and
 *         revoke their object URLs.
 *   H-11  no HTMLCanvasElement.prototype.getContext override anywhere; the one
 *         canvas that is read back requests preserveDrawingBuffer itself.
 *   H-12  thumbnail object URLs are bounded by an LRU and revoked on pagehide.
 *   L-8   closeMpr releases the fusion volume, resets processing state and
 *         clears the split fine timer.
 */
const fs = require('fs');
const path = require('path');

const ROOT = 'D:/Paxs Archive/app/static/viewer';
const MPR = fs.readFileSync(path.join(ROOT, 'paxs-mpr.js'), 'utf8');
const ENH = fs.readFileSync(path.join(ROOT, 'paxs-study-list-enhance.js'), 'utf8');
const HTML = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? '  -> ' + extra : '')); }
}

/** Pull `function <name>(...) { ... }` out of src by brace matching. */
function extractFunction(src, name) {
  const idx = src.indexOf('function ' + name + '(');
  if (idx === -1) throw new Error('function not found: ' + name);
  let i = src.indexOf('{', idx);
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    const c = src[j];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return src.slice(idx, j + 1); }
  }
  throw new Error('unbalanced braces extracting: ' + name);
}

// ---------------------------------------------------------------------------
console.log('\n[1] H-10 — sliding-window eviction of prefetched frames');

ok('SERVER_FRAME_KEEP_RADIUS is defined', /var SERVER_FRAME_KEEP_RADIUS\s*=\s*\d+/.test(MPR));
ok('evictServerFrames is called from prefetchServerRange',
   extractFunction(MPR, 'prefetchServerRange').includes('evictServerFrames(plane, center)'));

const evictSrc = extractFunction(MPR, 'evictServerFrames');
const radius = Number((MPR.match(/var SERVER_FRAME_KEEP_RADIUS\s*=\s*(\d+)/) || [])[1]);
ok('keep radius parsed', Number.isFinite(radius) && radius > 0, 'got ' + radius);

function makeFrames(lo, hi) {
  const frames = {};
  for (let i = lo; i <= hi; i++) {
    frames[i] = { _paxsObjectUrl: 'blob:' + i, src: 'blob:' + i, onload: () => {}, onerror: () => {} };
  }
  return frames;
}

const revoked = [];
const mockURL = { revokeObjectURL: (u) => revoked.push(u) };
const mockState = { serverFrames: { axial: makeFrames(0, 30) } };
const evict = new Function('state', 'SERVER_FRAME_KEEP_RADIUS', 'URL',
  evictSrc + '\nreturn evictServerFrames;')(mockState, radius, mockURL);

evict('axial', 15);

const keys = Object.keys(mockState.serverFrames.axial).map(Number).sort((a, b) => a - b);
const expectLo = 15 - radius, expectHi = 15 + radius;
ok('in-window frames kept',
   keys.every((k) => k >= expectLo && k <= expectHi),
   'keys=' + keys.join(','));
ok('exactly the window survives',
   keys.length === (expectHi - expectLo + 1) && keys[0] === expectLo && keys[keys.length - 1] === expectHi,
   'got ' + keys.length + ' keys');
ok('every out-of-window object URL revoked',
   revoked.length === 31 - keys.length, 'revoked ' + revoked.length + ' expected ' + (31 - keys.length));
ok('a high out-of-window URL was revoked', revoked.indexOf('blob:30') !== -1);
ok('a low out-of-window URL was revoked', revoked.indexOf('blob:0') !== -1);
ok('in-window URLs were NOT revoked', revoked.indexOf('blob:15') === -1);
ok('an in-window frame keeps its src',
   mockState.serverFrames.axial[15].src === 'blob:15');
ok('evicted frame has no lingering object URL',
   Object.keys(mockState.serverFrames.axial).every((k) => mockState.serverFrames.axial[k]._paxsObjectUrl));

// Scrolling far away must leave the window bounded, not accumulate.
for (let c = 0; c <= 30; c++) evict('axial', c);
ok('window stays bounded after a full sweep',
   Object.keys(mockState.serverFrames.axial).length <= (2 * radius + 1),
   'got ' + Object.keys(mockState.serverFrames.axial).length);

// ---------------------------------------------------------------------------
console.log('\n[2] H-11 — no global getContext prototype override');

const FILES = ['paxs-mpr.js', 'paxs-study-list-enhance.js', 'paxs-about.js',
               'paxs-toolbar-enhance.js', 'index.html'];
for (const f of FILES) {
  const src = fs.readFileSync(path.join(ROOT, f), 'utf8');
  // Assignment to the prototype (not a mention in a comment).
  const patched = /HTMLCanvasElement\.prototype\.getContext\s*=/.test(src);
  ok(f + ' does not patch HTMLCanvasElement.prototype.getContext', !patched);
}
ok('index.html no longer installs the VTK guard',
   !/origGetContext/.test(HTML) && !/__paxsVtkGuardInstalled\s*=\s*true/.test(HTML));
// Precise: no *assignment* of the flag anywhere (the H-11 comment names it).
ok('index.html no longer forces preserveDrawingBuffer',
   !/preserveDrawingBuffer\s*:\s*true/.test(HTML));

const renderCvCall = MPR.slice(MPR.indexOf("renderCv.getContext('webgl2'"));
const renderCvOpts = renderCvCall.slice(0, renderCvCall.indexOf('});'));
ok('the MPR read-back canvas requests preserveDrawingBuffer itself',
   renderCvOpts.includes('preserveDrawingBuffer: true'));

// ---------------------------------------------------------------------------
console.log('\n[3] H-12 — bounded thumbnail object-URL LRU + pagehide revocation');

const maxMatch = ENH.match(/var THUMB_CACHE_MAX\s*=\s*(\d+)/);
ok('THUMB_CACHE_MAX is defined', !!maxMatch, 'missing');
const THUMB_MAX = maxMatch ? Number(maxMatch[1]) : 0;
ok('THUMB_CACHE_MAX is bounded and sane', THUMB_MAX > 0 && THUMB_MAX <= 200, 'got ' + THUMB_MAX);
ok('pagehide revokes all cached thumbnails',
   /addEventListener\('pagehide',\s*releaseAllThumbs\)/.test(ENH));

const thumbFactory = new Function('URL', [
  'var thumbCache = {};',
  'var thumbOrder = [];',
  'var THUMB_CACHE_MAX = ' + THUMB_MAX + ';',
  extractFunction(ENH, '_releaseThumb'),
  extractFunction(ENH, '_cacheThumb'),
  extractFunction(ENH, 'releaseAllThumbs'),
  'return {',
  '  add: _cacheThumb,',
  '  releaseAll: releaseAllThumbs,',
  '  size: function () { return Object.keys(thumbCache).length; },',
  '  order: function () { return thumbOrder.length; },',
  '  has: function (u) { return Object.prototype.hasOwnProperty.call(thumbCache, u); }',
  '};',
].join('\n'));

const thumbRevoked = [];
const thumb = thumbFactory({ revokeObjectURL: (u) => thumbRevoked.push(u) });

for (let i = 0; i < 100; i++) thumb.add('study-' + i, 'blob:t' + i);
ok('cache never exceeds the cap', thumb.size() <= THUMB_MAX, 'size ' + thumb.size());
ok('LRU order stays in step with the cache', thumb.order() === thumb.size());
ok('overflow URLs were revoked', thumbRevoked.length === 100 - thumb.size(),
   'revoked ' + thumbRevoked.length);
ok('the newest entry survives', thumb.has('study-99'));
ok('the oldest entry was evicted', !thumb.has('study-0'));
ok('the newest entry was not revoked', thumbRevoked.indexOf('blob:t99') === -1);

thumb.releaseAll();
ok('releaseAll empties the cache', thumb.size() === 0 && thumb.order() === 0);
ok('releaseAll revoked every remaining URL', thumbRevoked.length === 100,
   'revoked ' + thumbRevoked.length + '/100');

// ---------------------------------------------------------------------------
console.log('\n[4] L-8 — closeMpr releases the fusion volume and resets state');

const closeSrc = extractFunction(MPR, 'closeMpr');
ok('closeMpr nulls the fusion overlay volume', /state\.fusion\.overlayVol\s*=\s*null/.test(closeSrc));
ok('closeMpr nulls the fusion overlay info', /state\.fusion\.overlayInfo\s*=\s*null/.test(closeSrc));
ok('closeMpr disables fusion', /state\.fusion\.enabled\s*=\s*false/.test(closeSrc));
ok('closeMpr clears the split fine timer',
   /clearTimeout\(state\.processing\.split\.fineTimer\)/.test(closeSrc));
ok('closeMpr resets the fallback index', /state\.processing\.fallbackIndex\s*=\s*0/.test(closeSrc));
ok('closeMpr resets effectiveMode', /state\.processing\.effectiveMode\s*=\s*null/.test(closeSrc));
ok('closeMpr resets the split fine plane cache',
   /state\.processing\.split\.fine\s*=\s*\{\s*axial:\s*null/.test(closeSrc));

console.log('\n===============================');
console.log('  PASS: ' + pass + '   FAIL: ' + fail);
console.log('===============================\n');
process.exit(fail ? 1 : 0);
