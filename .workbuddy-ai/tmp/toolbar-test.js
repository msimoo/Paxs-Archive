/**
 * Behavioral verification for app/static/viewer/paxs-toolbar-enhance.js
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const SRC = fs.readFileSync(
  path.resolve('D:/Paxs Archive/app/static/viewer/paxs-toolbar-enhance.js'), 'utf8');

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? '  -> ' + extra : '')); }
}

const TOOLBAR_HTML = `
  <header>
    <div id="brand">PAXS</div>
    <div id="tb">
      <button aria-label="Window Level"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" stroke="currentColor" fill="none"/></svg></button>
      <button aria-label="Zoom"><svg viewBox="0 0 24 24"><circle cx="10" cy="10" r="6" stroke="currentColor" fill="none"/></svg></button>
      <button aria-label="Pan" data-state="on"><svg viewBox="0 0 24 24"><path d="M4 12h16" stroke="currentColor"/></svg></button>
      <button aria-label="Length"><svg viewBox="0 0 24 24"><path d="M3 20 21 4" stroke="currentColor"/></svg></button>
      <button aria-label="Cobb Angle" role="radio" aria-checked="true" data-state="on"><svg viewBox="0 0 24 24"><path d="M4 20 20 20" stroke="currentColor"/></svg></button>
      <button aria-label="Reset"><svg viewBox="0 0 24 24"><path d="M12 4v16" stroke="currentColor"/></svg></button>
    </div>
    <span>CT</span>
  </header>`;

function boot(url, telemetry) {
  const vc = new VirtualConsole();
  const dom = new JSDOM(`<!doctype html><html><head></head><body>${TOOLBAR_HTML}</body></html>`,
    { runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole: vc, url });
  const w = dom.window;
  if (telemetry) w.__getPaxsMprState = () => telemetry;
  w.eval(SRC);
  return w;
}

const VIEWER_URL = 'http://localhost/viewer/viewer?StudyInstanceUIDs=1.2.840.1';

console.log('\n[1] Toolbar container tagging (derived at runtime, not from hashed classes)');
{
  const w = boot(VIEWER_URL);
  ok('toolbar container tagged .paxs-toolbar',
     w.document.getElementById('tb').classList.contains('paxs-toolbar'));
}

console.log('\n[2] Per-tool category tagging');
{
  const w = boot(VIEWER_URL);
  const get = (l) => w.document.querySelector(`button[aria-label="${l}"]`);
  ok('Window Level -> paxs-tool-wl',        get('Window Level').classList.contains('paxs-tool-wl'));
  ok('Zoom -> paxs-tool-nav',               get('Zoom').classList.contains('paxs-tool-nav'));
  ok('Pan -> paxs-tool-nav',                get('Pan').classList.contains('paxs-tool-nav'));
  ok('Length -> paxs-tool-measure',         get('Length').classList.contains('paxs-tool-measure'));
  ok('Cobb Angle -> paxs-tool-measure',     get('Cobb Angle').classList.contains('paxs-tool-measure'));
  ok('Reset -> paxs-tool-nav',              get('Reset').classList.contains('paxs-tool-nav'));
  ok('every tool carries .paxs-tool',
     w.document.querySelectorAll('#tb button.paxs-tool').length === 6);
}

console.log('\n[3] ACTIVE-state detection (the previously-dead selector)');
{
  const w = boot(VIEWER_URL);
  const get = (l) => w.document.querySelector(`button[aria-label="${l}"]`);
  ok('data-state="on" -> paxs-tool-active',        get('Pan').classList.contains('paxs-tool-active'));
  ok('aria-checked="true" -> paxs-tool-active',    get('Cobb Angle').classList.contains('paxs-tool-active'));
  ok('inactive tool NOT flagged',                  !get('Zoom').classList.contains('paxs-tool-active'));
  ok('exactly two active tools',                   w.document.querySelectorAll('.paxs-tool-active').length === 2);

  // aria-pressed variant
  const dom = new JSDOM(`<!doctype html><body><header><div id="tb">
    <button aria-label="Zoom"><svg/></button>
    <button aria-label="Pan" aria-pressed="true"><svg/></button>
  </div></header></body>`, { runScripts: 'outside-only', pretendToBeVisual: true,
    virtualConsole: new VirtualConsole(), url: VIEWER_URL });
  dom.window.eval(SRC);
  ok('aria-pressed="true" -> paxs-tool-active',
     dom.window.document.querySelector('button[aria-label="Pan"]').classList.contains('paxs-tool-active'));
}

console.log('\n[4] Icon refinement hook');
{
  const w = boot(VIEWER_URL);
  const svgs = w.document.querySelectorAll('#tb svg');
  let all = true;
  svgs.forEach((s) => { if (!s.classList.contains('paxs-icon')) all = false; });
  ok('all toolbar svgs tagged .paxs-icon', all, 'count=' + svgs.length);
}

console.log('\n[5] Live telemetry pill');
{
  // idle (no bridge)
  const w = boot(VIEWER_URL);
  let pill = w.document.querySelector('header .paxs-telemetry');
  ok('pill injected into header', !!pill);
  ok('idle state by default', pill.classList.contains('paxs-mode-idle'), pill.className);

  // client mode via bridge
  w.__getPaxsMprState = () => ({
    effectiveMode: 'client', rttMs: 37, clientScore: 81,
    capabilities: { webgl: true, webgl2: true }
  });
  w.__paxsToolbarEnhance();
  pill = w.document.querySelector('header .paxs-telemetry');
  ok('client mode class', pill.classList.contains('paxs-mode-client'), pill.className);
  ok('label reads GPU Client', /GPU Client/.test(pill.querySelector('.paxs-tel-label').textContent));
  ok('tooltip carries RTT', /37 ms/.test(pill.title), pill.title);
  ok('tooltip carries score', /81 \/ 100/.test(pill.title));
  ok('tooltip carries GPU', /WebGL2/.test(pill.title));

  // server mode
  w.__getPaxsMprState = () => ({ effectiveMode: 'server', rttMs: 210, clientScore: 12, capabilities: { webgl: true, webgl2: false } });
  w.__paxsToolbarEnhance();
  pill = w.document.querySelector('header .paxs-telemetry');
  ok('server mode class', pill.classList.contains('paxs-mode-server'), pill.className);
  ok('label reads Server', /Server/.test(pill.querySelector('.paxs-tel-label').textContent));

  // split mode
  w.__getPaxsMprState = () => ({ effectiveMode: 'split', rttMs: 90, clientScore: 44, capabilities: { webgl: true, webgl2: true } });
  w.__paxsToolbarEnhance();
  pill = w.document.querySelector('header .paxs-telemetry');
  ok('split mode class', pill.classList.contains('paxs-mode-split'), pill.className);
  ok('label reads Hybrid', /Hybrid/.test(pill.querySelector('.paxs-tel-label').textContent));
}

console.log('\n[6] Idempotency + non-viewer no-op');
{
  const w = boot(VIEWER_URL);
  w.__paxsToolbarEnhance();
  w.__paxsToolbarEnhance();
  ok('exactly one telemetry pill', w.document.querySelectorAll('.paxs-telemetry').length === 1);
  ok('toolbar tagged once', w.document.querySelectorAll('.paxs-toolbar').length === 1);

  const w2 = boot('http://localhost/viewer'); // study list, not the viewer
  ok('no toolbar tagging on study list', w2.document.querySelectorAll('.paxs-toolbar').length === 0);
  ok('no telemetry pill on study list', w2.document.querySelectorAll('.paxs-telemetry').length === 0);
}

console.log('\n[7] Modality badge tagging');
{
  const w = boot(VIEWER_URL);
  ok('CT span tagged .paxs-modality-badge',
     !!w.document.querySelector('header span.paxs-modality-badge'));
}

console.log('\n===============================');
console.log('  PASS: ' + pass + '   FAIL: ' + fail);
console.log('===============================\n');
process.exit(fail ? 1 : 0);
