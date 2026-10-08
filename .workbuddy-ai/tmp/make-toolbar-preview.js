/**
 * Builds a static preview of the modernized header toolbar using the REAL
 * paxs-viewer-theme.css and the REAL icons injected by paxs-toolbar-enhance.js
 * (run through jsdom), so the preview cannot drift from the shipping code.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const css = fs.readFileSync(
  path.resolve('D:/Paxs Archive/app/static/viewer/paxs-viewer-theme.css'), 'utf8');
const src = fs.readFileSync(
  path.resolve('D:/Paxs Archive/app/static/viewer/paxs-toolbar-enhance.js'), 'utf8');

// Tools to render: [aria-label, category, active]
const TOOLS = [
  ['Window Level', 'wl', true],
  ['Zoom', 'nav', false],
  ['Pan', 'nav', false],
  ['Stack Scroll', 'nav', false],
  ['Length', 'measure', false],
  ['Angle', 'measure', false],
  ['Cobb Angle', 'measure', false],
  ['Bidirectional', 'measure', false],
  ['Probe', 'annot', false],
  ['MPR', 'def', false],
  ['Layout', 'def', false],
  ['Invert', 'nav', false],
  ['Rotate Right', 'nav', false],
  ['Reset', 'nav', false],
  ['Cine', 'nav', false],
  ['Arrow Annotate', 'annot', false],
  ['Eraser', 'annot', false],
  ['Measurements', 'annot', false]
];

const buttonsHtml = TOOLS.map(([label, , active]) =>
  `<button aria-label="${label}"${active ? ' data-state="on"' : ''}><svg viewBox="0 0 25 25"><path fill="currentColor" d="M2 2h20v20H2z"/></svg></button>`
).join('\n');

const dom = new JSDOM(
  `<!doctype html><html><head></head><body><header><div id="tb">${buttonsHtml}</div></header></body></html>`,
  { runScripts: 'outside-only', pretendToBeVisual: true,
    virtualConsole: new VirtualConsole(),
    url: 'http://localhost/viewer/viewer?StudyInstanceUIDs=1.2.840' });

const w = dom.window;
w.eval(src);

// Pull the *injected* svg markup straight out of the DOM the script produced.
// Keep the real class list (paxs-icon paxs-modern-svg) so the preview exercises
// the actual 19px / 1.75px cascade rather than a stripped-down copy.
const rendered = TOOLS.map(([label, cat, active]) => {
  const btn = w.document.querySelector(`button[aria-label="${label}"]`);
  const svg = btn.querySelector('svg');
  return `<button class="paxs-tool paxs-tool-${cat}${active ? ' paxs-tool-active' : ''}" ` +
    `aria-label="${label}" title="${label}"${active ? ' data-state="on"' : ''}>` +
    svg.outerHTML +
    `</button>`;
}).join('\n    ');

const html = `<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PAXS Viewer — toolbar icons preview</title>
<style>
${css}
/* ---- preview scaffolding (not part of the shipped theme) ---- */
html,body{height:100%}
body{background:#05070d;margin:0;display:flex;flex-direction:column;overflow:hidden}
.pv-note{color:#64748b;font:600 11px/1.6 ui-monospace,monospace;padding:14px 18px;border-bottom:1px solid rgba(51,65,85,.35)}
.pv-note b{color:#94a3b8}
.pv-stage{flex:1;background:radial-gradient(circle at 50% 40%,#0b1220,#04060c);display:flex;align-items:center;justify-content:center;color:#334155;font:700 12px ui-monospace,monospace;letter-spacing:.2em}
.pv-zoom{display:flex;gap:26px;justify-content:center;padding:26px;border-bottom:1px solid rgba(51,65,85,.35);flex-wrap:wrap}
.pv-zoom figure{margin:0;text-align:center}
.pv-zoom figcaption{color:#64748b;font:600 9.5px ui-monospace,monospace;margin-top:8px;letter-spacing:.06em}
.pv-zoom .big{display:grid;place-items:center;width:64px;height:64px;border-radius:12px;background:rgba(30,41,59,.5);border:1px solid rgba(51,65,85,.6)}
.pv-zoom .big svg{width:38px!important;height:38px!important;min-width:38px!important;min-height:38px!important}
</style>
</head>
<body>
<div class="pv-note"><b>PAXS Viewer — injected medical icon set.</b> Every glyph below is the real output of <code>paxs-toolbar-enhance.js</code>, rendered with the shipping <code>paxs-viewer-theme.css</code>.</div>

<header>
  <a href="/viewer" class="paxs-brand">PAXS</a>
  <div class="paxs-toolbar">
    ${rendered}
  </div>
  <span class="paxs-modality-badge">CT</span>
  <div class="paxs-telemetry paxs-mode-split" role="status">
    <span class="paxs-tel-dot"></span>
    <span class="paxs-tel-icon">⚡</span>
    <span class="paxs-tel-label">Hybrid</span>
    <span class="paxs-tel-tip">Processing mode: Hybrid  ·  Network RTT: 88 ms  ·  Client score: 46 / 100  ·  GPU: WebGL2</span>
  </div>
</header>

<div class="pv-zoom">
  <figure><div class="big">${w.document.querySelector('button[aria-label="Window Level"] svg').outerHTML}</div><figcaption>W/L</figcaption></figure>
  <figure><div class="big">${w.document.querySelector('button[aria-label="Zoom"] svg').outerHTML}</div><figcaption>ZOOM</figcaption></figure>
  <figure><div class="big">${w.document.querySelector('button[aria-label="Pan"] svg').outerHTML}</div><figcaption>PAN</figcaption></figure>
  <figure><div class="big">${w.document.querySelector('button[aria-label="Length"] svg').outerHTML}</div><figcaption>LENGTH</figcaption></figure>
  <figure><div class="big">${w.document.querySelector('button[aria-label="Angle"] svg').outerHTML}</div><figcaption>ANGLE</figcaption></figure>
  <figure><div class="big">${w.document.querySelector('button[aria-label="Cobb Angle"] svg').outerHTML}</div><figcaption>COBB</figcaption></figure>
  <figure><div class="big">${w.document.querySelector('button[aria-label="Probe"] svg').outerHTML}</div><figcaption>PROBE</figcaption></figure>
  <figure><div class="big">${w.document.querySelector('button[aria-label="MPR"] svg').outerHTML}</div><figcaption>MPR/3D</figcaption></figure>
  <figure><div class="big">${w.document.querySelector('button[aria-label="Layout"] svg').outerHTML}</div><figcaption>LAYOUT</figcaption></figure>
  <figure><div class="big">${w.document.querySelector('button[aria-label="Invert"] svg').outerHTML}</div><figcaption>INVERT</figcaption></figure>
  <figure><div class="big">${w.document.querySelector('button[aria-label="Rotate Right"] svg').outerHTML}</div><figcaption>ROTATE</figcaption></figure>
  <figure><div class="big">${w.document.querySelector('button[aria-label="Cine"] svg').outerHTML}</div><figcaption>CINE</figcaption></figure>
</div>

<div class="pv-stage">DICOM VIEWPORT AREA</div>
</body></html>`;

const out = path.resolve('D:/Paxs Archive/.workbuddy-ai/tmp/toolbar-preview.html');
fs.writeFileSync(out, html, 'utf8');
console.log('written: ' + out + '  (' + html.length + ' bytes)');
console.log('tools rendered: ' + TOOLS.length);

// jsdom's pretendToBeVisual keeps requestAnimationFrame timers alive, so the
// event loop never drains on its own — exit explicitly.
process.exit(0);
