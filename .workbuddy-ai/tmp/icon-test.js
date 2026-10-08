/**
 * Behavioral verification for the icon-injection map in paxs-toolbar-enhance.js
 *
 * Task E contract under test:
 *   - PAXS_ICONS geometry is authored on a 24x24 grid, stroked 1.75px
 *   - the EXISTING <svg> node is re-attributed in place (never replaced)
 *   - each upgraded svg is stamped data-paxs-icon="<icon_id>" (the icon key)
 *   - the modern glyph class paxs-modern-svg is applied
 *   - matching is word-boundary based, so "Rectangle"/"Freehand" don't misfire
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

const VIEWER_URL = 'http://localhost/viewer/viewer?StudyInstanceUIDs=1.2.840.1';

// Stock glyphs deliberately mimic OHIF's FILLED silhouettes + a chevron.
const FILLED = '<path fill="currentColor" d="M2 2h20v20H2z"/>';
const btn = (label, extra) =>
  `<button aria-label="${label}" ${extra || ''}><svg viewBox="0 0 25 25">${FILLED}</svg></button>`;

const HTML = `<header><div id="tb">
  ${btn('Window Level')}
  ${btn('Zoom')}
  ${btn('Pan', 'data-state="on"')}
  ${btn('Stack Scroll')}
  ${btn('Cine')}
  ${btn('Length')}
  ${btn('Bidirectional')}
  ${btn('Angle')}
  ${btn('Cobb Angle')}
  ${btn('Probe')}
  ${btn('MPR')}
  ${btn('Layout')}
  ${btn('Invert')}
  ${btn('Rotate Right')}
  ${btn('Reset')}
  ${btn('Flip Horizontally')}
  ${btn('Ellipse')}
  ${btn('Rectangle')}
  ${btn('Freehand')}
  ${btn('Eraser')}
  ${btn('Arrow Annotate')}
  ${btn('Text')}
  ${btn('Capture')}
  ${btn('Undo')}
  ${btn('Redo')}
  ${btn('Segmentation')}
  ${btn('Measurements')}
  ${btn('Some Unknown Tool')}
  <button aria-label="Layout" id="dd"><svg viewBox="0 0 24 24">${FILLED}</svg><svg class="chevron" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg></button>
</div></header>`;

function boot(html) {
  const dom = new JSDOM(`<!doctype html><html><head></head><body>${html || HTML}</body></html>`,
    { runScripts: 'outside-only', pretendToBeVisual: true,
      virtualConsole: new VirtualConsole(), url: VIEWER_URL });
  dom.window.eval(SRC);
  return dom.window;
}

const g = (w, l) => w.document.querySelector(`button[aria-label="${l}"]`);
const iconId = (w, l) => g(w, l).querySelector('svg').getAttribute('data-paxs-icon');
const inner = (w, l) => g(w, l).querySelector('svg').innerHTML;

console.log('\n[1] Injection re-attributes the EXISTING <svg>, stamps the icon id');
{
  const w = boot();
  const b = g(w, 'Length');
  const svg = b.querySelector('svg');
  ok('viewBox normalised to 0 0 24 24', svg.getAttribute('viewBox') === '0 0 24 24');
  ok('fill="none"',   svg.getAttribute('fill') === 'none');
  ok('stroke="currentColor"', svg.getAttribute('stroke') === 'currentColor');
  ok('stroke-width="1.75"',   svg.getAttribute('stroke-width') === '1.75');
  ok('stroke-linecap="round"',   svg.getAttribute('stroke-linecap') === 'round');
  ok('stroke-linejoin="round"',  svg.getAttribute('stroke-linejoin') === 'round');
  ok('aria-hidden="true"', svg.getAttribute('aria-hidden') === 'true');
  ok('stock silhouette path removed', !/M2 2h20v20H2z/.test(svg.innerHTML));
  ok('caliper geometry injected', /m3 21 18-18/.test(svg.innerHTML), svg.innerHTML);
  // The caliper deliberately keeps solid end-anchor dots — those carry their own
  // fill="currentColor" and must survive (they are NOT a stock silhouette).
  ok('intentional anchor dots kept', (svg.innerHTML.match(/fill="currentColor"/g) || []).length === 2);
  ok('modern glyph class applied', svg.classList.contains('paxs-modern-svg'));
  ok('legacy .paxs-icon class applied', svg.classList.contains('paxs-icon'));
  ok('stamped data-paxs-icon="length"', svg.getAttribute('data-paxs-icon') === 'length');
  ok('same <svg> node identity (not replaced)', b.querySelector('svg') === svg);
}

console.log('\n[2] Correct glyph per tool (incl. the tricky label overlaps)');
{
  const w = boot();
  const has = (label, re) => re.test(inner(w, label));
  ok('Window Level -> contrast dial',  has('Window Level', /M12 3v18/));
  ok('Zoom -> magnifier +',            has('Zoom', /m20 20-3\.5-3\.5/));
  ok('Pan -> 4-way arrows',            has('Pan', /M12 2v20M2 12h20/));
  ok('Stack Scroll -> layers',         has('Stack Scroll', /M4 8\.6 12 4\.2l8 4\.4/));
  ok('Cine -> play triangle',          has('Cine', /<polygon points="7 4 19 12 7 20 7 4"/));
  ok('Length -> caliper',              has('Length', /m3 21 18-18/));
  ok('Bidirectional -> bidi caliper',  has('Bidirectional', /M3 21h4M3 21v-4M21 3h-4M21 3v4/));
  ok('Cobb Angle -> cobb (NOT angle)', has('Cobb Angle', /M3 18\.5h8M13 18\.5h8/));
  ok('Angle -> goniometer arc',        has('Angle', /M5 19h14M5 19l11-13/));
  ok('Probe -> crosshair',             has('Probe', /M12 2v4M12 18v4M2 12h4M18 12h4/));
  ok('MPR -> iso cube',                has('MPR', /m12 3 8 4\.5v9L12 21l-8-4\.5v-9L12 3z/));
  ok('Layout -> 2x2 grid',             has('Layout', /<rect x="3" y="3" width="7\.5" height="7\.5"/));
  ok('Layout -> exactly 4 panes',      (inner(w, 'Layout').match(/<rect/g) || []).length === 4);
  ok('Invert -> contrast flip',        has('Invert', /M12 3a9 9 0 1 0 9 9/));
  ok('Reset -> ccw refresh arc',       has('Reset', /M3 12a9 9 0 1 0 2\.64-6\.36L3 8M3 3v5h5/));
  ok('Rotate Right -> cw rotate arc',  has('Rotate Right', /M21 12a9 9 0 1 1-2\.64-6\.36L21 8M21 3v5h-5/));
  ok('Flip -> mirror axis',            has('Flip Horizontally', /M9 6\.5 3\.5 12 9 17\.5z/));
  ok('Ellipse -> contour',             has('Ellipse', /<ellipse cx="12" cy="12" rx="8\.6" ry="6"/));
  ok('Rectangle -> ROI box',           has('Rectangle', /<rect x="4" y="6\.5" width="16" height="11"/));
  ok('Freehand -> ROI pen',            has('Freehand', /m15 5 4 4L7 21H3v-4L15 5z/));
  ok('Eraser -> clear glyph',          has('Eraser', /M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6/));
  ok('Arrow Annotate -> arrow',        has('Arrow Annotate', /M5 19 19 5M19 5h-7M19 5v7/));
  ok('Text -> T',                      has('Text', /M5 6\.2h14M12 6\.2v12\.6/));
  ok('Capture -> download',            has('Capture', /M12 3\.5v11\.5/));
  ok('Undo -> history arrow',          has('Undo', /M4 12h9\.5a4\.5 4\.5 0 0 1 0 9H10/));
  ok('Redo -> history arrow',          has('Redo', /M20 12h-9\.5a4\.5 4\.5 0 0 0 0 9H14/));
  ok('Segmentation -> layers',         has('Segmentation', /M12 3\.4 20\.6 8/));
  ok('Measurements -> report doc',     has('Measurements', /M14 3\.2H7\.2a2 2 0/));
}

console.log('\n[2b] Icon-id mapping (the renamed keys)');
{
  const w = boot();
  ok('Window Level -> windowLevel', iconId(w, 'Window Level') === 'windowLevel');
  ok('Length -> length',            iconId(w, 'Length') === 'length');
  ok('Freehand -> roi',             iconId(w, 'Freehand') === 'roi');
  ok('Eraser -> clear',             iconId(w, 'Eraser') === 'clear');
  ok('Arrow Annotate -> arrow',     iconId(w, 'Arrow Annotate') === 'arrow');
  ok('Rotate Right -> rotate',      iconId(w, 'Rotate Right') === 'rotate');
  ok('Reset -> reset',              iconId(w, 'Reset') === 'reset');
  ok('Rectangle -> rectangle (NOT angle)',
     iconId(w, 'Rectangle') === 'rectangle', iconId(w, 'Rectangle'));
}

console.log('\n[3] Safety: attributes, listeners, unknown labels, chevrons');
{
  const w = boot();
  const pan = g(w, 'Pan');
  ok('data-state preserved', pan.getAttribute('data-state') === 'on');

  // Listener attached BEFORE injection must still fire.
  let fired = 0;
  const zoom = g(w, 'Zoom');
  zoom.addEventListener('click', () => { fired++; });
  // Re-run enhancement (as the MutationObserver would) then click.
  w.__paxsToolbarEnhance();
  zoom.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  ok('click listener survives re-enhancement', fired === 1, 'fired=' + fired);

  const unknown = g(w, 'Some Unknown Tool');
  ok('unmatched label left untouched',
     unknown.querySelector('svg').getAttribute('data-paxs-icon') === null &&
     /fill="currentColor"/.test(unknown.querySelector('svg').innerHTML));

  const dd = w.document.getElementById('dd');
  const chev = dd.querySelector('svg.chevron');
  ok('chevron svg NOT replaced', /m6 9 6 6 6-6/.test(chev.innerHTML));
  ok('chevron not flagged', chev.getAttribute('data-paxs-icon') === null);
  ok('chevron keeps no modern class', !chev.classList.contains('paxs-modern-svg'));
  ok('tool icon (first svg) WAS replaced',
     /<rect x="3" y="3" width="7\.5" height="7\.5"/.test(dd.querySelector('svg').innerHTML));
}

console.log('\n[4] Idempotency');
{
  const w = boot();
  const svg = g(w, 'Length').querySelector('svg');
  const before = svg.innerHTML;
  w.__paxsToolbarEnhance();
  w.__paxsToolbarEnhance();
  w.__paxsToolbarEnhance();
  ok('markup unchanged after repeat runs', svg.innerHTML === before);
  ok('icon id still stamped once', svg.getAttribute('data-paxs-icon') === 'length');
  ok('exactly one modern class', (svg.getAttribute('class') || '').split(/\s+/)
      .filter(c => c === 'paxs-modern-svg').length === 1, svg.getAttribute('class'));
  ok('only one svg in the button (no duplicates)', g(w, 'Length').querySelectorAll('svg').length === 1);
}

console.log('\n[5] Active state still driven by OHIF attributes (untouched by injection)');
{
  const w = boot();
  const pan = g(w, 'Pan');
  ok('active class present', pan.classList.contains('paxs-tool-active'));
  // Simulate OHIF de-activating the tool.
  pan.setAttribute('data-state', 'off');
  pan.removeAttribute('aria-checked');
  w.__paxsToolbarEnhance();
  ok('de-activation reflected', !pan.classList.contains('paxs-tool-active'));
  // And re-activating.
  pan.setAttribute('aria-checked', 'true');
  w.__paxsToolbarEnhance();
  ok('re-activation reflected', pan.classList.contains('paxs-tool-active'));
}

console.log('\n[6] Zero infinite mutation loops (debounced observer)');
{
  const w = boot();
  const svg = g(w, 'Zoom').querySelector('svg');
  const before = svg.innerHTML;
  const flag = svg.getAttribute('data-paxs-icon');

  // Simulate a burst of mutations the way React would emit them.
  let mutations = 0;
  const mo = new w.MutationObserver(() => { mutations++; });
  mo.observe(w.document.body, { childList: true, subtree: true, attributes: true });
  for (let i = 0; i < 5; i++) w.__paxsToolbarEnhance();
  w.document.body.appendChild(w.document.createElement('div'));

  ok('markup stable through a mutation burst', svg.innerHTML === before);
  ok('flag stable through a mutation burst', svg.getAttribute('data-paxs-icon') === flag);
  ok('no duplicate modern class after burst', (svg.getAttribute('class') || '')
      .split(/\s+/).filter(c => c === 'paxs-modern-svg').length === 1);
}

console.log('\n===============================');
console.log('  PASS: ' + pass + '   FAIL: ' + fail);
console.log('===============================\n');
process.exit(fail ? 1 : 0);
