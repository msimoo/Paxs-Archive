/**
 * Cascade verification: the shipped CSS must resolve the injected icon to
 * 19px / 1.75px, NOT the generic 18px from `.paxs-tool svg`.
 *
 * `.paxs-tool svg` has specificity (0,1,1); a bare `.paxs-modern-svg` only has
 * (0,1,0) and would LOSE. The fix adds `.paxs-tool .paxs-modern-svg` (0,2,0)
 * and `.paxs-toolbar button .paxs-modern-svg` (0,2,1). This test locks that in.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const css = fs.readFileSync(
  path.resolve('D:/Paxs Archive/app/static/viewer/paxs-viewer-theme.css'), 'utf8');

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? '  -> ' + extra : '')); }
}

const html = `<!doctype html><html data-theme="dark"><head><style>${css}</style></head><body>
<header class="paxs-header">
  <div class="paxs-toolbar">
    <button class="paxs-tool paxs-tool-wl paxs-tool-active" data-state="on" aria-label="Window Level">
      <svg class="paxs-icon paxs-modern-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.75" width="24" height="24"><circle cx="12" cy="12" r="9"/></svg>
    </button>
  </div>
</header></body></html>`;

const dom = new JSDOM(html, { pretendToBeVisual: true, virtualConsole: new VirtualConsole() });
const w = dom.window;
const svg = w.document.querySelector('svg.paxs-modern-svg');
const cs = w.getComputedStyle(svg);

console.log('\n[1] Resolved geometry of the injected icon');
ok('width resolves to 19px', cs.width === '19px', 'got ' + cs.width);
ok('height resolves to 19px', cs.height === '19px', 'got ' + cs.height);
// jsdom normalises the `none` keyword to a fully-transparent colour, so accept
// either spelling — both mean "no fill".
ok('fill resolves to none / transparent',
   cs.fill === 'none' || /rgba\(0,\s*0,\s*0,\s*0\)/.test(cs.fill), 'got ' + cs.fill);
ok('stroke follows currentColor (resolves to a colour)',
   cs.stroke && cs.stroke !== '' && cs.stroke !== 'none', 'got ' + cs.stroke);

console.log('\n[2] Guard: the generic 18px rule must NOT win');
ok('width is not the generic 18px', cs.width !== '18px', 'got ' + cs.width);

console.log('\n===============================');
console.log('  PASS: ' + pass + '   FAIL: ' + fail);
console.log('===============================\n');
process.exit(fail ? 1 : 0);
