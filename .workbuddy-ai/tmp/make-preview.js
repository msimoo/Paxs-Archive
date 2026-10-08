/**
 * Renders the real paxs-about.js modal into a standalone preview HTML,
 * using jsdom so the preview is generated from the shipping code (no drift).
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const ABOUT_SRC = fs.readFileSync(
  path.resolve('D:/Paxs Archive/app/static/viewer/paxs-about.js'), 'utf8');

const vc = new VirtualConsole();
const dom = new JSDOM(`<!doctype html><html data-theme="dark"><head></head><body>
  <header><button role="menuitem" id="mi-about">About</button></header>
</body></html>`, { runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole: vc });

const w = dom.window;
w.__getPaxsMprState = () => ({
  active: true,
  studyUid: '1.2.840.113619.2.55.3.604688.20260925.114233',
  seriesUid: '1.2.840.113619.2.55.3.604688.20260925.114233.17',
  dims: { slices: 512, rows: 512, cols: 512 },
  spacing: { row: 0.703125, col: 0.703125, slice: 1.0 },
  layout: '4grid',
  configuredMode: 'hybrid',
  effectiveMode: 'split',
  clientScore: 46,
  rttMs: 88,
  capabilities: { webgl: true, webgl2: true, vramMb: 8192, workers: 16, memoryGb: 32, total: 46 },
  serverLoad: { active: 7, max: 20, cacheMb: 2048, budgetMb: 4096, overloaded: false },
  fallbackMode: 'split'
});

w.eval(ABOUT_SRC);
w.document.getElementById('mi-about').dispatchEvent(
  new w.MouseEvent('click', { bubbles: true, cancelable: true }));

const overlay = w.document.getElementById('paxs-about-overlay');
overlay.classList.add('paxs-about-open');           // skip the entrance animation
const styleEl = w.document.getElementById('paxs-about-style');

// Patch the GPU renderer line for a realistic preview (jsdom has no WebGL).
overlay.innerHTML = overlay.innerHTML.replace('unavailable',
  'NVIDIA RTX A4000/PCIe/SSE2 · ANGLE (NVIDIA)');

const html = `<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>About PAXS Viewer — preview</title>
${styleEl.outerHTML}
<style>
  html,body{margin:0;height:100%}
  body{background:#05070d;display:grid;place-items:center;overflow:hidden}
  /* Preview-only: keep the overlay inside the viewport frame, not position:fixed */
  #paxs-about-overlay{position:absolute !important;opacity:1 !important}
</style>
</head>
<body>
${overlay.outerHTML}
</body></html>`;

const out = path.resolve('D:/Paxs Archive/.workbuddy-ai/tmp/about-preview.html');
fs.writeFileSync(out, html, 'utf8');
console.log('written: ' + out + '  (' + html.length + ' bytes)');
