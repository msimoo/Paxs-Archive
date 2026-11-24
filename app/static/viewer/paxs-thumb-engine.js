/* ==========================================================================
   PAXS Thumbnail Engine — bounded, study-scoped, lazy series thumbnails

   Loaded before paxs-study-list-enhance.js, which injects the request
   function and mounts the tiles. Transport + caching only: no product copy,
   no layout policy.

   Defects this closes:
   SB-1  Study scope. Every resolve/fetch is gated on the StudyInstanceUID(s)
         of the page, so a mis-rendered sidebar cannot pull another patient's
         pixels over the wire.
   SB-2  Fan-out. enhanceRow() used to call thumbFor() for every row on every
         pass — 200 studies x 3 round trips at once. Requests now go through a
         queue capped at THUMB_CONCURRENCY, only for tiles near the viewport.
   SB-3  Cache thrash. The LRU revoked the oldest blob even when that tile was
         still on screen, flickering it back to a placeholder. Eviction now
         leaves visible tiles alone and resets only off-screen ones.

   The pure helpers at the top are sliced out by
   tests/js/paxs-thumb-engine.test.js and run in a Node vm sandbox — keep them
   free of DOM access and module-level state.
   ========================================================================== */
(function () {
  'use strict';

  if (window.PaxsThumb) return;

  var THUMB_CACHE_MAX = 64;        // SB-3 — live blob URLs
  var THUMB_CONCURRENCY = 4;       // SB-2 — parallel QIDO/WADO resolutions
  var MISS_CACHE_MAX = 256;        // bounded negative cache
  var RESOLVED_MAX = 512;          // bounded study -> {series, sop} memo
  var PREFETCH_MARGIN = '150px';   // start loading this far outside the viewport

  // --- SB-1 — study scope ---

  var STUDY_QUERY_KEYS = ['StudyInstanceUIDs', 'study', 'studyInstanceUid', 'StudyInstanceUID'];

  /**
   * Read the study UIDs the page was opened for. OHIF appends
   * `StudyInstanceUIDs` once per study; this fork's deep link also accepts
   * `study=`. Comma lists are split and the result is de-duplicated.
   *
   * Returns [] when the page is not scoped to a study (the study list), which
   * callers must treat as "no restriction", not as "nothing allowed".
   */
  function parseActiveStudyUIDs(search) {
    var out = [];
    var raw = String(search == null ? '' : search);
    var query = raw.charAt(0) === '?' ? raw.slice(1) : raw;
    if (!query) return out;

    var pairs = query.split('&');
    for (var i = 0; i < pairs.length; i++) {
      var pair = pairs[i];
      if (!pair) continue;
      var eq = pair.indexOf('=');
      if (eq < 1) continue;

      var key = pair.slice(0, eq);
      var value = pair.slice(eq + 1);
      try { key = decodeURIComponent(key.replace(/\+/g, ' ')); } catch (e) { /* keep raw */ }
      try { value = decodeURIComponent(value.replace(/\+/g, ' ')); } catch (e) { /* keep raw */ }

      var wanted = false;
      for (var k = 0; k < STUDY_QUERY_KEYS.length; k++) {
        if (key === STUDY_QUERY_KEYS[k]) { wanted = true; break; }
      }
      if (!wanted) continue;

      var parts = value.split(',');
      for (var p = 0; p < parts.length; p++) {
        var uid = parts[p].trim();
        if (uid && out.indexOf(uid) === -1) out.push(uid);
      }
    }
    return out;
  }

  /** True when `uid` may be resolved. An empty scope means "unrestricted". */
  function studyUidAllowed(uid, allowed) {
    if (!uid) return false;
    if (!allowed || !allowed.length) return true;
    return allowed.indexOf(uid) !== -1;
  }

  // --- SB-3 — LRU of blob URLs ---

  /**
   * Bounded LRU keyed by study UID. `onEvict(key, value)` runs once per evicted
   * entry and is where the caller revokes the object URL.
   */
  function createLruCache(max, onEvict) {
    var limit = Math.max(1, max | 0);
    var order = new Map();

    function evict(key) {
      var value = order.get(key);
      order['delete'](key);
      if (!onEvict) return;
      try { onEvict(key, value); } catch (e) { /* an eviction must never throw */ }
    }

    return {
      has: function (key) { return order.has(key); },
      /** Read + mark most-recently-used. */
      get: function (key) {
        if (!order.has(key)) return undefined;
        var value = order.get(key);
        order['delete'](key);
        order.set(key, value);
        return value;
      },
      /** Read without touching recency. */
      peek: function (key) { return order.get(key); },
      set: function (key, value) {
        if (order.has(key)) {
          var previous = order.get(key);
          order['delete'](key);
          if (previous !== value) {
            try { if (onEvict) onEvict(key, previous); } catch (e) { /* ignore */ }
          }
        }
        order.set(key, value);
        while (order.size > limit) evict(order.keys().next().value);
      },
      'delete': function (key) { order['delete'](key); },
      size: function () { return order.size; },
      keys: function () { return Array.from(order.keys()); },
      clear: function () { order.clear(); }
    };
  }

  // --- SB-2 — concurrency-limited queue ---

  /**
   * Run at most `limit` tasks at a time, at most one per uid.
   *
   * `cancel(uid)` only removes a task that has not started; a running request
   * is allowed to finish, because its bytes still warm the cache. That is what
   * the scroll-out path needs: skip what has not started, never discard bytes
   * already in flight.
   */
  function createRequestQueue(limit, runner) {
    var max = Math.max(1, limit | 0);
    var waiting = [];
    var inflight = Object.create(null);
    var active = 0;

    function settle(item) {
      active--;
      delete inflight[item.uid];
      pump();
    }

    function start(item) {
      active++;
      var promise;
      try {
        promise = runner(item);
      } catch (e) {
        promise = Promise.reject(e);
      }
      Promise.resolve(promise).then(
        function () { settle(item); },
        function () { settle(item); }
      );
    }

    function pump() {
      while (active < max && waiting.length) start(waiting.shift());
    }

    return {
      /** @returns {boolean} false when this uid is already queued or running */
      push: function (uid, payload) {
        if (inflight[uid]) return false;
        inflight[uid] = true;
        waiting.push({ uid: uid, payload: payload });
        pump();
        return true;
      },
      /** @returns {boolean} true only when a not-yet-started task was dropped */
      cancel: function (uid) {
        if (!inflight[uid]) return false;
        for (var i = 0; i < waiting.length; i++) {
          if (waiting[i].uid === uid) {
            waiting.splice(i, 1);
            delete inflight[uid];
            return true;
          }
        }
        return false;
      },
      pending: function () { return waiting.length; },
      active: function () { return active; },
      clear: function () { waiting.length = 0; inflight = Object.create(null); }
    };
  }

  // --- DICOM JSON helpers ---

  function pick(tagMap, tag) {
    try {
      var el = tagMap && tagMap[tag];
      if (!el || !el.Value || !el.Value.length) return '';
      var value = el.Value[0];
      if (el.vr === 'PN' && value && typeof value === 'object') return value.Alphabetic || '';
      if (Array.isArray(value)) return value[0];
      return String(value);
    } catch (e) {
      return '';
    }
  }

  /** Escape a value for use inside a quoted CSS attribute selector. */
  function attrValue(value) {
    return String(value == null ? '' : value).replace(/["\\]/g, '\\$&');
  }

  function tilesFor(uid) {
    return document.querySelectorAll('.paxs-thumb[data-uid="' + attrValue(uid) + '"]');
  }

  // --- Module state ---

  var state = {
    request: null,
    allowed: [],
    cache: null,
    misses: new Map(),
    resolved: new Map(),
    resolving: new Map(),
    queue: null,
    observer: null,
    visible: typeof WeakSet === 'function' ? new WeakSet() : null,
    configured: false
  };

  // --- Tile painting ---

  /** Paint a tile from a resolved URL. '' means "known unavailable". */
  function paint(wrap, url) {
    var img = wrap.querySelector('img');
    if (!img) return;

    if (!url) {
      wrap.classList.remove('paxs-loaded');
      wrap.classList.add('paxs-nothumb');
      wrap.setAttribute('data-thumb-state', 'none');
      img.onload = null;
      img.onerror = null;
      img.removeAttribute('src');
      return;
    }

    // SB-3: a painted, visible tile is a cache hit — keep it near the MRU end
    // so eviction takes an off-screen study instead of this one.
    state.cache.get(wrap.getAttribute('data-uid'));

    wrap.classList.remove('paxs-nothumb');
    if (wrap.classList.contains('paxs-loaded')) {
      wrap.setAttribute('data-thumb-state', 'done');
      if (img.getAttribute('src') !== url) img.src = url;
      return;
    }

    wrap.setAttribute('data-thumb-state', 'loading');
    img.decoding = 'async';            // decode off the main thread
    img.onload = function () {
      wrap.classList.add('paxs-loaded');
      wrap.setAttribute('data-thumb-state', 'done');
      stopObserving(wrap);
    };
    img.onerror = function () {
      wrap.classList.remove('paxs-loaded');
      wrap.classList.add('paxs-nothumb');
      wrap.setAttribute('data-thumb-state', 'none');
      stopObserving(wrap);
    };
    img.src = url;
    // A blob URL decoded earlier in this session is complete synchronously;
    // trusting that avoids the one-frame placeholder flash on React re-render.
    if (img.complete && img.naturalWidth > 0) {
      wrap.classList.add('paxs-loaded');
      wrap.setAttribute('data-thumb-state', 'done');
      stopObserving(wrap);
    }
  }

  /** A settled tile needs no further intersection callbacks. */
  function stopObserving(wrap) {
    if (state.observer) state.observer.unobserve(wrap);
  }

  /** Drop the pixels but keep the tile, so it can be re-fetched on re-entry. */
  function reset(wrap) {
    var img = wrap.querySelector('img');
    wrap.classList.remove('paxs-loaded');
    wrap.classList.add('paxs-nothumb');
    wrap.setAttribute('data-thumb-state', 'idle');
    if (img) {
      img.onload = null;
      img.onerror = null;
      img.removeAttribute('src');
    }
    // It is off-screen and out of the cache: watch it again so scrolling back
    // re-queues the request.
    if (state.observer) state.observer.observe(wrap);
  }

  function paintAll(uid, url) {
    var nodes = tilesFor(uid);
    for (var i = 0; i < nodes.length; i++) paint(nodes[i], url);
  }

  function rememberMiss(uid) {
    state.misses.set(uid, true);
    while (state.misses.size > MISS_CACHE_MAX) {
      state.misses['delete'](state.misses.keys().next().value);
    }
  }

  /**
   * Memoise study -> {series, sop}. Every structure that outlives a single
   * request is bounded: dropping the oldest entry only costs two extra QIDO
   * calls if that study is thumbnailed again, so memory stays flat on a list
   * of thousands of studies. A `null` value is a real answer ("no series"),
   * not a missing entry, so it is stored rather than skipped.
   */
  function rememberResolution(uid, ref) {
    state.resolved['delete'](uid);          // re-insert at the MRU end
    state.resolved.set(uid, ref);
    while (state.resolved.size > RESOLVED_MAX) {
      state.resolved['delete'](state.resolved.keys().next().value);
    }
  }

  // --- Resolution: study -> first series -> first instance -> /thumbnail ---

  function ensureResolved(uid) {
    if (state.resolved.has(uid)) return Promise.resolve(state.resolved.get(uid));
    if (state.resolving.has(uid)) return state.resolving.get(uid);

    var pending = state.request(
      '/dicom-web/studies/' + encodeURIComponent(uid) +
      '/series?limit=1&includefield=0020000E,00080060',
      { credentials: 'same-origin' }
    )
      .then(function (res) { return res && res.ok ? res.json() : []; })
      .then(function (series) {
        if (!series || !series.length) { rememberResolution(uid, null); return null; }
        var seriesUid = pick(series[0], '0020000E');
        if (!seriesUid) { rememberResolution(uid, null); return null; }
        return state.request(
          '/dicom-web/studies/' + encodeURIComponent(uid) +
          '/series/' + encodeURIComponent(seriesUid) +
          '/instances?limit=1&includefield=00080018',
          { credentials: 'same-origin' }
        )
          .then(function (res) { return res && res.ok ? res.json() : []; })
          .then(function (instances) {
            var sop = instances && instances.length ? pick(instances[0], '00080018') : '';
            var ref = seriesUid && sop ? { series: seriesUid, sop: sop } : null;
            rememberResolution(uid, ref);
            return ref;
          });
      })
      .catch(function () { rememberResolution(uid, null); return null; })
      .then(function (ref) {
        state.resolving['delete'](uid);
        return ref;
      });

    state.resolving.set(uid, pending);
    return pending;
  }

  function thumbFor(uid) {
    if (!studyUidAllowed(uid, state.allowed)) return Promise.resolve('');   // SB-1
    if (state.cache.has(uid)) return Promise.resolve(state.cache.get(uid));
    if (state.misses.has(uid)) return Promise.resolve('');

    return ensureResolved(uid).then(function (ref) {
      if (!ref) { rememberMiss(uid); return ''; }

      var url = '/dicom-web/studies/' + encodeURIComponent(uid) +
        '/series/' + encodeURIComponent(ref.series) +
        '/instances/' + encodeURIComponent(ref.sop) + '/thumbnail';

      return state.request(url, { credentials: 'same-origin' })
        .then(function (res) {
          if (!res || !res.ok) { rememberMiss(uid); return ''; }
          return res.blob().then(function (blob) {
            if (!blob || !blob.size) { rememberMiss(uid); return ''; }
            var objectUrl = URL.createObjectURL(blob);
            state.cache.set(uid, objectUrl);
            return objectUrl;
          });
        })
        .catch(function () { rememberMiss(uid); return ''; });
    });
  }

  // --- Queue runner + IntersectionObserver ---

  function runRequest(item) {
    var wrap = item.payload && item.payload.wrap;
    if (!wrap || !wrap.isConnected) return Promise.resolve('');
    if (wrap.getAttribute('data-uid') !== item.uid) return Promise.resolve('');

    wrap.setAttribute('data-thumb-state', 'loading');
    return thumbFor(item.uid).then(function (url) {
      // Paint every tile for this study, not just the one that asked: a React
      // re-render may have replaced the row while the request was in flight.
      paintAll(item.uid, url);
      return url;
    });
  }

  function request(wrap) {
    var uid = wrap.getAttribute('data-uid');
    if (!uid) return;

    var current = wrap.getAttribute('data-thumb-state');
    if (current === 'queued' || current === 'loading' || current === 'done') return;
    if (!studyUidAllowed(uid, state.allowed)) return;            // SB-1
    if (state.misses.has(uid)) { paint(wrap, ''); return; }

    if (state.cache.has(uid)) { paint(wrap, state.cache.get(uid)); return; }

    wrap.setAttribute('data-thumb-state', 'queued');
    // A duplicate uid is fine: the in-flight request paints all of its tiles.
    state.queue.push(uid, { wrap: wrap });
  }

  function onEvict(uid, objectUrl) {
    if (objectUrl) {
      try { URL.revokeObjectURL(objectUrl); } catch (e) { /* already gone */ }
    }
    // SB-3: a tile that is on screen keeps its decoded frame — revoking the
    // URL does not repaint it. Only off-screen tiles are reset, so scrolling
    // back re-fetches instead of showing a dead blob.
    var nodes = tilesFor(uid);
    for (var i = 0; i < nodes.length; i++) {
      if (state.visible && state.visible.has(nodes[i])) continue;
      reset(nodes[i]);
    }
  }

  function ensureObserver() {
    if (state.observer || typeof IntersectionObserver !== 'function') return state.observer;

    state.observer = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        var wrap = entries[i].target;
        var uid = wrap.getAttribute('data-uid');
        if (!uid) continue;

        if (entries[i].isIntersecting) {
          if (state.visible) state.visible.add(wrap);
          request(wrap);
          continue;
        }

        if (state.visible) state.visible['delete'](wrap);
        // Scrolled away before its turn in the queue: drop it rather than
        // spend a round trip on a row nobody is looking at.
        if (wrap.getAttribute('data-thumb-state') === 'queued') {
          if (state.queue.cancel(uid)) wrap.setAttribute('data-thumb-state', 'idle');
        }
        // Its blob may have been evicted while it was visible.
        if (!state.cache.has(uid) && wrap.getAttribute('data-thumb-state') === 'done') {
          reset(wrap);
        }
      }
    }, { root: null, rootMargin: PREFETCH_MARGIN, threshold: 0.01 });

    return state.observer;
  }

  function attach(wrap) {
    var uid = wrap.getAttribute('data-uid');
    if (!uid) return;
    if (!studyUidAllowed(uid, state.allowed)) return;            // SB-1

    if (state.cache.has(uid)) { paint(wrap, state.cache.get(uid)); return; }
    if (state.misses.has(uid)) { paint(wrap, ''); return; }

    var observer = ensureObserver();
    if (observer) {
      wrap.setAttribute('data-thumb-state', 'idle');
      observer.observe(wrap);
      return;
    }
    request(wrap);   // no IntersectionObserver: load immediately, still queued
  }

  // --- Tile factory — the markup paxs-study-list-theme.css styles ---

  function mountTile(host, options) {
    if (!host || !options || !options.uid) return null;
    var uid = options.uid;
    if (!studyUidAllowed(uid, state.allowed)) return null;       // SB-1

    var existing = host.querySelector(':scope > .paxs-thumb');
    if (existing && existing.getAttribute('data-uid') === uid) {
      attach(existing);
      return existing;
    }
    if (existing) existing.remove();

    var wrap = document.createElement('div');
    wrap.className = 'paxs-thumb';
    wrap.setAttribute('data-uid', uid);
    wrap.setAttribute('data-thumb-state', 'idle');
    if (options.color) wrap.style.setProperty('--paxs-mod', options.color);

    var badge = document.createElement('span');
    badge.className = 'paxs-mod';
    badge.textContent = options.modality || 'IMG';
    wrap.appendChild(badge);

    if (options.initials) {
      var avatar = document.createElement('span');
      avatar.className = 'paxs-avatar';
      avatar.textContent = options.initials;
      avatar.setAttribute('aria-hidden', 'true');
      wrap.appendChild(avatar);
    }

    var img = document.createElement('img');
    img.alt = '';
    img.decoding = 'async';
    img.width = 48;
    img.height = 48;
    wrap.appendChild(img);

    host.insertBefore(wrap, host.firstChild);
    attach(wrap);
    return wrap;
  }

  // --- Public surface ---

  function releaseAll() {
    var keys = state.cache.keys();
    for (var i = 0; i < keys.length; i++) {
      var objectUrl = state.cache.get(keys[i]);
      if (objectUrl) {
        try { URL.revokeObjectURL(objectUrl); } catch (e) { /* ignore */ }
      }
    }
    state.cache.clear();
    state.misses.clear();
  }

  function setAllowed(list) {
    var next = Array.isArray(list) ? list.slice() : [];
    if (next.join('|') === state.allowed.join('|')) return false;
    state.allowed = next;
    // SB-1: a scope change invalidates every pixel and resolution we hold.
    releaseAll();
    state.resolved.clear();
    state.resolving.clear();
    state.queue.clear();
    return true;
  }

  function configure(options) {
    var opts = options || {};
    if (typeof opts.request === 'function') state.request = opts.request;
    if (typeof opts.allowed !== 'undefined') setAllowed(opts.allowed);
    state.configured = true;
    return state.configured;
  }

  /** Re-attach every mounted tile (used after a scope change). */
  function refresh(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var nodes = scope.querySelectorAll('.paxs-thumb[data-uid]');
    for (var i = 0; i < nodes.length; i++) attach(nodes[i]);
    return nodes.length;
  }

  function stats() {
    return {
      allowed: state.allowed.length,
      cached: state.cache.size(),
      misses: state.misses.size,
      resolved: state.resolved.size,
      queued: state.queue.pending(),
      active: state.queue.active(),
      visible: !!state.observer
    };
  }

  state.cache = createLruCache(THUMB_CACHE_MAX, onEvict);
  state.queue = createRequestQueue(THUMB_CONCURRENCY, runRequest);
  state.request = function (url, init) { return window.fetch(url, init); };

  // Free every blob when the document is discarded. A bfcache restore re-runs
  // this script, so the only effect is to release memory.
  window.addEventListener('pagehide', function () {    releaseAll();
    state.queue.clear();
  });

  window.PaxsThumb = {
    configure: configure,
    setAllowed: setAllowed,
    mount: mountTile,
    refresh: refresh,
    releaseAll: releaseAll,
    stats: stats,
    parseActiveStudyUIDs: parseActiveStudyUIDs,
    studyUidAllowed: studyUidAllowed,
    createLruCache: createLruCache,
    createRequestQueue: createRequestQueue,
    LIMITS: { cache: THUMB_CACHE_MAX, concurrency: THUMB_CONCURRENCY }
  };
})();
