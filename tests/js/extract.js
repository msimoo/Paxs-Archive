/**
 * Helpers for executing the *shipped* viewer code in a Node sandbox.
 *
 * `paxs-mpr.js` is an un-bundled classic script (an IIFE with no module
 * exports), so the only way to exercise its internals without a browser is to
 * slice the wanted functions out of the real source and run them in a `vm`
 * context alongside the globals they close over.
 *
 * Test-only helper — never loaded by the viewer.
 */

'use strict';

const fs = require('node:fs');
const path = require('node:path');

const VIEWER_DIR = path.join(__dirname, '..', '..', 'app', 'static', 'viewer');

/** Absolute path of a viewer asset. */
function viewerFile(name) {
  return path.join(VIEWER_DIR, name);
}

/** Raw source of paxs-mpr.js. */
function readViewerSource() {
  return fs.readFileSync(viewerFile('paxs-mpr.js'), 'utf8');
}

/** Whitespace-normalised source, for structural (contract) assertions. */
function normalisedSource() {
  return readViewerSource().replace(/\s+/g, ' ');
}

/**
 * Slice a top-level `function name(...) { ... }` declaration out of `source`
 * by brace matching.
 *
 * The matcher skips string literals and comments, because the viewer builds
 * Web Worker source inside string arrays (e.g. `'self.onmessage = function(e) {'`)
 * — a naive counter stops at the first brace inside those strings and returns
 * a truncated function. Throws when the function is missing or unbalanced.
 */
function extractFunction(source, name) {
  const start = source.indexOf('function ' + name + '(');
  if (start < 0) {
    throw new Error(`function ${name} not found in the viewer source`);
  }
  const open = source.indexOf('{', start);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;

  for (let i = open; i < source.length; i++) {
    const ch = source[i];
    const next = source[i + 1];

    if (lineComment) {
      if (ch === '\n') lineComment = false;
      continue;
    }
    if (blockComment) {
      if (ch === '*' && next === '/') { blockComment = false; i++; }
      continue;
    }
    if (quote) {
      if (ch === '\\') { i++; continue; }   // escaped character inside a string
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '/' && next === '/') { lineComment = true; i++; continue; }
    if (ch === '/' && next === '*') { blockComment = true; i++; continue; }
    if (ch === "'" || ch === '"' || ch === '`') { quote = ch; continue; }

    if (ch === '{') {
      depth++;
    } else if (ch === '}') {
      depth--;
      if (depth === 0) {
        return source.slice(start, i + 1);
      }
    }
  }
  throw new Error(`unbalanced braces while extracting ${name}`);
}

/**
 * Build a vm context pre-populated with `globals`, run `code`, and return the
 * context so the caller can read results and mutated globals back out.
 */
function runInSandbox(code, globals) {
  const vm = require('node:vm');
  const ctx = Object.assign({}, globals);
  vm.createContext(ctx);
  vm.runInContext(code, ctx);
  return ctx;
}

module.exports = {
  VIEWER_DIR,
  viewerFile,
  readViewerSource,
  normalisedSource,
  extractFunction,
  runInSandbox,
};
