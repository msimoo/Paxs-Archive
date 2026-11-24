/**
 * PAXS — the report addendum chain (C-19).
 *
 * A FINALIZED report is immutable. Its `findings` / `impression` are the signed
 * record and the API refuses to rewrite them (409); follow-up observations are
 * appended as addenda, which are append-only, attributed to the authenticated
 * author, audited, and delivered to the EMR as an amended ORU. The backend
 * returns them on `GET /api/v1/reports/{id}` and `/reports/study/{uid}`.
 *
 * This is the single renderer for that chain, shared by the Study List
 * (`app.js`) and the viewer (`paxs-study-list-enhance.js`), because the one
 * thing the two surfaces must not disagree about is escaping: an addendum body
 * is free text typed by a clinician, so every interpolated value goes through
 * the caller's `esc` and a `<` in a body must reach the DOM as text.
 *
 * Rendering contract:
 *   * chronological order (created_at, then id — two addenda written in the
 *     same second must not reorder between refreshes),
 *   * each entry shows its author, its timestamp and its escaped text,
 *   * the chain renders *below* the signed sections and says so, so the record
 *     reads "what was signed" then "what was added later" and never implies the
 *     original text changed,
 *   * no chain, no markup — a DRAFT has no addenda and must not grow an empty
 *     "Addenda" heading.
 *
 * Loaded as a classic script (no build step), like the rest of the static UI.
 */
(function (root) {
  'use strict';

  if (root.PaxsReportAddenda) return;

  /** Roles the server accepts on POST /reports/{id}/addendum. */
  var AUTHOR_ROLES = ['ADMIN', 'RADIOLOGIST'];

  /** Fallback escaper, used only when a caller forgets to pass one. */
  function plainEscape(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function canAuthor(role) {
    return AUTHOR_ROLES.indexOf(String(role || '').toUpperCase()) !== -1;
  }

  /** Numeric id, or 0 — an id is only ever used as a data attribute. */
  function idNumber(item) {
    var n = Number(item && item.id);
    return isFinite(n) ? n : 0;
  }

  function stampMs(item) {
    var raw = item && item.created_at;
    if (!raw) return 0;
    var ms = Date.parse(raw);
    return isFinite(ms) ? ms : 0;
  }

  /** Chronological copy: created_at ascending, id as the deterministic tie-break. */
  function sort(addenda) {
    var list = (addenda || []).slice();
    list.sort(function (a, b) {
      var at = stampMs(a);
      var bt = stampMs(b);
      if (at !== bt) return at - bt;
      return idNumber(a) - idNumber(b);
    });
    return list;
  }

  /**
   * `2026-10-21T10:00:00` — the server's ISO timestamp, to the second.
   *
   * Deliberately not `toLocaleString()`: the same addendum must render the same
   * way for the radiologist who wrote it and for the auditor reading it later,
   * and a timezone-shifted string in a clinical audit trail reads like an edit.
   */
  function stampText(item) {
    var raw = item && item.created_at;
    if (!raw) return '';
    return String(raw).replace('T', ' ').replace(/\.\d+/, '').replace(/Z$/, '');
  }

  /**
   * The addendum chain as HTML, or '' when there is none.
   *
   * @param {Array} addenda  entries shaped {id, author, text, created_at}
   * @param {Function} esc   the caller's escaper (one per surface, no second copy)
   */
  function chainHtml(addenda, esc) {
    var e = typeof esc === 'function' ? esc : plainEscape;
    var chain = sort(addenda);
    if (!chain.length) return '';

    var html = '<section class="addenda" aria-labelledby="report-addenda-title">' +
      '<div class="addenda-head">' +
        '<b id="report-addenda-title">Addenda</b>' +
        '<span class="sub">' + chain.length +
          (chain.length === 1 ? ' amendment' : ' amendments') +
          ' · the signed text above is unchanged</span>' +
      '</div>';

    for (var i = 0; i < chain.length; i++) {
      var item = chain[i] || {};
      var when = stampText(item);
      html += '<article class="addendum" data-addendum-id="' + e(String(idNumber(item))) + '">' +
        '<div class="addendum-meta">' +
          '<span class="addendum-n">Addendum ' + (i + 1) + '</span>' +
          '<span class="addendum-author">' + e(item.author || 'UNKNOWN') + '</span>' +
          (when ? '<time class="addendum-time" datetime="' + e(item.created_at) + '">' + e(when) + '</time>' : '') +
        '</div>' +
        '<p class="addendum-text">' + e(item.text || '') + '</p>' +
      '</article>';
    }

    return html + '</section>';
  }

  /**
   * The "Add addendum" control, or '' when the signed-in role may not append.
   *
   * The form is inert markup: the caller wires submit -> POST
   * `/api/v1/reports/{id}/addendum` and re-renders the chain on success. The
   * server is the authority on both the role and the report state, so a stale
   * UI cannot append to a DRAFT.
   *
   * @param {Object} opts {prefix, maxLength, placeholder, submitLabel}
   */
  function formHtml(opts) {
    var o = opts || {};
    var e = plainEscape;
    var prefix = e(o.prefix || 'report-addendum');
    var maxLength = Number(o.maxLength) > 0 ? Math.floor(Number(o.maxLength)) : 20000;
    var placeholder = e(o.placeholder || 'Correction, clarification or follow-up…');
    var submitLabel = e(o.submitLabel || 'Add addendum');

    return '<form class="addendum-form" id="' + prefix + '-form" novalidate>' +
      '<label class="addendum-label" for="' + prefix + '-text">Add addendum</label>' +
      '<textarea class="addendum-input" id="' + prefix + '-text" rows="3" maxlength="' +
        maxLength + '" placeholder="' + placeholder + '"></textarea>' +
      '<div class="addendum-error" id="' + prefix + '-error" role="alert" hidden></div>' +
      '<div class="addendum-actions">' +
        '<button class="addendum-submit" id="' + prefix + '-submit" type="submit">' +
          submitLabel + '</button>' +
      '</div>' +
    '</form>';
  }

  root.PaxsReportAddenda = {
    authorRoles: AUTHOR_ROLES,
    canAuthor: canAuthor,
    sort: sort,
    chainHtml: chainHtml,
    formHtml: formHtml,
    escape: plainEscape
  };
}(typeof window !== 'undefined' ? window : this));
