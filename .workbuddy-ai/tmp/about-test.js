/**
 * Behavioral verification harness for app/static/viewer/paxs-about.js
 * Runs in jsdom — real DOM, real event dispatch.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const ABOUT_SRC = fs.readFileSync(
  path.resolve('D:/Paxs Archive/app/static/viewer/paxs-about.js'), 'utf8');

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? '  -> ' + extra : '')); }
}

function makeDom() {
  const vc = new VirtualConsole(); // swallow jsdom "not implemented" noise
  const dom = new JSDOM(`<!doctype html><html><head></head><body>
    <header>
      <button role="menuitem" id="mi-about">About</button>
      <button role="menuitem" id="mi-prefs">Preferences</button>
    </header>
    <div id="viewport"><canvas id="cv" width="200" height="200"></canvas></div>
  </body></html>`, { runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole: vc });
  return dom;
}

function boot(dom, telemetry) {
  const w = dom.window;
  if (telemetry) w.__getPaxsMprState = () => telemetry;
  // Simulate the OHIF "native" About modal handler bound in the bubble phase.
  w.__nativeOpened = false;
  const item = w.document.getElementById('mi-about');
  item.addEventListener('click', () => { w.__nativeOpened = true; });
  w.eval(ABOUT_SRC);
  return w;
}

function click(w, el) {
  const ev = new w.MouseEvent('click', { bubbles: true, cancelable: true });
  el.dispatchEvent(ev);
  return ev;
}

// ---------------------------------------------------------------------------
console.log('\n[1] Interception of the header "About" menu item');
{
  const dom = makeDom(); const w = boot(dom);
  const ev = click(w, w.document.getElementById('mi-about'));
  const overlay = w.document.getElementById('paxs-about-overlay');
  ok('custom modal rendered', !!overlay);
  ok('native OHIF About modal suppressed', w.__nativeOpened === false);
  ok('event defaultPrevented', ev.defaultPrevented === true);
  ok('modal has dialog role', overlay && overlay.getAttribute('role') === 'dialog');
  ok('branding present', overlay && /PAXS Viewer/.test(overlay.textContent));
  ok('platform version present', overlay && /v2026\.1/.test(overlay.textContent));
  ok('lead architect present', overlay && /Mohamed O\. Elsamani/.test(overlay.textContent));
  ok('compliance badges present',
     overlay && /DICOM 3\.0/.test(overlay.textContent) && /HTJ2K Lossless/.test(overlay.textContent));
  ok('shortcut cheatsheet present',
     overlay && /Window \/ Level/.test(overlay.textContent) && /Shift \+ drag/.test(overlay.textContent));
  ok('clinical disclaimer present', overlay && /Clinical notice/.test(overlay.textContent));
}

console.log('\n[2] Non-About menu items are NOT intercepted');
{
  const dom = makeDom(); const w = boot(dom);
  w.__prefsFired = false;
  const prefs = w.document.getElementById('mi-prefs');
  prefs.addEventListener('click', () => { w.__prefsFired = true; });
  const ev = click(w, prefs);
  ok('Preferences click reaches its handler', w.__prefsFired === true);
  ok('no modal opened for Preferences', !w.document.getElementById('paxs-about-overlay'));
  ok('default not prevented', ev.defaultPrevented === false);
}

console.log('\n[3] Telemetry — live MPR bridge (active volume session)');
{
  const dom = makeDom();
  const w = boot(dom, {
    active: true, studyUid: '1.2.840.113619.2.55.3.604688.1',
    seriesUid: '1.2.840.113619.2.55.3.604688.1.2',
    dims: { slices: 512, rows: 512, cols: 512 },
    spacing: { row: 0.7, col: 0.7, slice: 1.0 },
    configuredMode: 'hybrid', effectiveMode: 'client', clientScore: 78, rttMs: 42,
    capabilities: { webgl: true, webgl2: true, vramMb: 8192, workers: 16, memoryGb: 32, total: 78 },
    serverLoad: { active: 3, max: 20, cacheMb: 1024, budgetMb: 4096, overloaded: false },
    fallbackMode: 'client'
  });
  click(w, w.document.getElementById('mi-about'));
  const t = w.document.getElementById('paxs-about-overlay').textContent;
  ok('mode badge shows Client (GPU)', /Client \(GPU\)/.test(t));
  ok('configured mode echoed', /configured: hybrid/.test(t));
  ok('client score shown', /78 \/ 100/.test(t));
  ok('rtt shown', /42 ms/.test(t));
  ok('study uid shown (truncated)', /1\.2\.840\.113619\.2\.5/.test(t));
  ok('voxel matrix shown', /512 × 512 × 512 voxels/.test(t));
  ok('voxel spacing shown', /0\.7 \/ 0\.7 \/ 1 mm/.test(t));
}

console.log('\n[4] Telemetry — split mode mapping');
{
  const dom = makeDom();
  const w = boot(dom, {
    active: true, studyUid: '1.2.3', seriesUid: '1.2.3.4',
    dims: { slices: 100, rows: 256, cols: 256 }, spacing: { row: 1, col: 1, slice: 2 },
    configuredMode: 'hybrid', effectiveMode: 'split', clientScore: 30, rttMs: 210,
    capabilities: { webgl: true, webgl2: true, total: 30 }
  });
  click(w, w.document.getElementById('mi-about'));
  const t = w.document.getElementById('paxs-about-overlay').textContent;
  ok('mode badge shows Hybrid [Split]', /Hybrid \[Split\]/.test(t));
}

console.log('\n[5] Telemetry — server mode + graceful fallback (no session)');
{
  const dom = makeDom();
  const w = boot(dom, {
    active: true, studyUid: '1.2.3', seriesUid: '1.2.3.4', dims: null, spacing: null,
    configuredMode: 'server', effectiveMode: 'server', clientScore: null, rttMs: null,
    capabilities: null
  });
  click(w, w.document.getElementById('mi-about'));
  let t = w.document.getElementById('paxs-about-overlay').textContent;
  ok('mode badge shows Server', /Server/.test(t));
  ok('no volume loaded fallback text', /no volume loaded/.test(t));
  ok('independent probe fallback (no WebGL in jsdom)', /no WebGL context|unavailable/.test(t));

  // Bridge absent entirely (study-list header) -> must still open and probe.
  const dom2 = makeDom(); const w2 = boot(dom2, null);
  click(w2, w2.document.getElementById('mi-about'));
  ok('opens with no bridge at all (study-list header)',
     !!w2.document.getElementById('paxs-about-overlay'));
}

console.log('\n[6] Dismissal — Escape, backdrop, close button');
{
  // Escape
  const dom = makeDom(); const w = boot(dom);
  click(w, w.document.getElementById('mi-about'));
  const esc = new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
  w.document.dispatchEvent(esc);
  ok('Escape marks modal closing (open class removed)',
     !w.document.getElementById('paxs-about-overlay').classList.contains('paxs-about-open'));
  ok('Escape default prevented', esc.defaultPrevented === true);

  // Backdrop
  const dom2 = makeDom(); const w2 = boot(dom2);
  click(w2, w2.document.getElementById('mi-about'));
  const overlay = w2.document.getElementById('paxs-about-overlay');
  click(w2, overlay);
  ok('backdrop click closes',
     !overlay.classList.contains('paxs-about-open'));

  // Close button
  const dom3 = makeDom(); const w3 = boot(dom3);
  click(w3, w3.document.getElementById('mi-about'));
  const ov3 = w3.document.getElementById('paxs-about-overlay');
  const btn = ov3.querySelector('.pa-close');
  ok('close button exists', !!btn);
  click(w3, btn);
  ok('close button closes', !ov3.classList.contains('paxs-about-open'));
}

console.log('\n[7] Event trapping — no bleed-through to document-level tool handlers');
{
  const dom = makeDom(); const w = boot(dom);
  click(w, w.document.getElementById('mi-about'));
  const overlay = w.document.getElementById('paxs-about-overlay');
  const dialog = overlay.querySelector('.paxs-about-dialog');

  const leaks = { wheel: 0, pointerdown: 0, mousedown: 0, keydownDoc: 0 };
  ['wheel', 'pointerdown', 'mousedown'].forEach((type) => {
    w.document.addEventListener(type, () => { leaks[type]++; }, false);
  });

  // Dispatch inside the dialog — must be swallowed before reaching document.
  ['wheel', 'pointerdown', 'mousedown'].forEach((type) => {
    dialog.dispatchEvent(new w.Event(type, { bubbles: true, cancelable: true }));
  });

  ok('wheel did not reach document', leaks.wheel === 0, 'leaks=' + leaks.wheel);
  ok('pointerdown did not reach document', leaks.pointerdown === 0, 'leaks=' + leaks.pointerdown);
  ok('mousedown did not reach document', leaks.mousedown === 0, 'leaks=' + leaks.mousedown);

  // Sanity: with the modal CLOSED, the same wheel DOES reach document.
  const dom2 = makeDom(); const w2 = boot(dom2);
  let reach = 0;
  w2.document.addEventListener('wheel', () => { reach++; }, false);
  w2.document.getElementById('viewport').dispatchEvent(
    new w2.Event('wheel', { bubbles: true, cancelable: true }));
  ok('control: wheel reaches document when modal closed', reach === 1);
}

console.log('\n[8] Re-entrancy — double click opens a single modal');
{
  const dom = makeDom(); const w = boot(dom);
  const item = w.document.getElementById('mi-about');
  click(w, item); click(w, item);
  const n = w.document.querySelectorAll('#paxs-about-overlay').length;
  ok('exactly one overlay instance', n === 1, 'count=' + n);
}

console.log('\n===============================');
console.log('  PASS: ' + pass + '   FAIL: ' + fail);
console.log('===============================\n');
process.exit(fail ? 1 : 0);
