/* ============================================================
   PAXS Archive — Transfer jobs workbench (#nodes -> #transfer-board)
   ------------------------------------------------------------
   Upgrades the Transfer jobs panel on #nodes with:
     · live poll pause/resume + configurable interval (3s / 5s / 15s / 30s / Off)
     · 6-tile operational telemetry strip (Queued, Running, Completed,
       Needs attention, Success rate with 95% Wilson CI, Throughput & median duration)
     · per-peer AE breakdown pills (click to filter)
     · search, status/direction/transport/node filters, sorting, Cards vs Table layout
     · "Load older" offset pagination + page limit (30 / 50 / 100)
     · multi-select bulk Retry / Cancel / CSV Export
     · slide-over Job Inspector (timing breakdown, error diagnostics, UID list with
       copy/view/single-UID retry, live auto-refresh while job is active)
     · slide-over New Transfer Composer (SEND batch up to 100 studies with live local
       study search + DICOM UID validator + peer C-ECHO ping; RETRIEVE DIMSE/DICOMweb)

   Depends on globals from app.js: api, toast, esc, state, viewerUrl, openFind.
   Exposes window.PaxsTransfers = { load, clearHistory, stopPoll, openComposer, openInspector }.
   ============================================================ */
(function () {
  "use strict";

  var $id = function (id) { return document.getElementById(id); };
  var qsa = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var H = function (s) {
    return (typeof esc === "function") ? esc(s) : String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
    });
  };
  var notify = function (msg, kind) { if (typeof toast === "function") toast(msg, kind || ""); };
  var call = function (path, opts) { return api(path, opts || {}); };
  var signedIn = function () { return typeof state !== "undefined" && !!state.token; };
  var isAdmin = function () { return typeof state !== "undefined" && state.role === "ADMIN"; };
  var studyHref = function (uid) { return typeof viewerUrl === "function" ? viewerUrl(uid) : "/viewer?study=" + encodeURIComponent(uid); };

  var UID_RE = /^[0-9]+(?:\.[0-9]+)*$/;
  var PLACEHOLDER_UIDS = new Set(["1.2.3", "1.2.3.4", "1.2.3.4.5", "0.0.0.0"]);
  var AE_BAD = /[\\\x00-\x1f]/;
  var PREF_KEY = "paxs_tx_v1";

  var DEFAULT_PREFS = {
    pollSec: 5,
    paused: false,
    limit: 50,
    layout: "cards",
    status: "",
    dir: "",
    transport: "",
    node: "",
    sort: "newest",
    q: "",
    ourAe: "PAXS_SCU"
  };

  function loadPrefs() {
    var p = Object.assign({}, DEFAULT_PREFS);
    try {
      var raw = JSON.parse(localStorage.getItem(PREF_KEY) || "null");
      if (raw && typeof raw === "object") Object.assign(p, raw);
    } catch (_) { /* ignore */ }
    return p;
  }
  var P = loadPrefs();
  function savePrefs() { try { localStorage.setItem(PREF_KEY, JSON.stringify(P)); } catch (_) { /* ignore */ } }

  var S = {
    jobs: [],
    nodes: [],
    loading: false,
    hasMore: false,
    loadedAt: null,
    error: null,
    sel: new Set(),
    pollTimer: null,
    inspectTimer: null,
    inspectId: null,
    composerOpen: false,
    batchUids: []
  };

  /* ------------------------------------------------------------ time & stats helpers */
  function isoDate(s) {
    if (!s) return null;
    var str = String(s);
    if (!/[zZ]|[+-]\d\d:?\d\d$/.test(str)) str += "Z";
    var d = new Date(str);
    return isNaN(d) ? null : d;
  }
  function fmtDurSec(sec) {
    if (sec == null || !isFinite(sec) || sec < 0) return "—";
    if (sec < 1) return Math.round(sec * 1000) + " ms";
    if (sec < 60) return sec.toFixed(sec < 10 ? 1 : 0) + " s";
    if (sec < 3600) return Math.floor(sec / 60) + "m " + Math.round(sec % 60) + "s";
    return (sec / 3600).toFixed(1) + " h";
  }
  function fmtRel(d) {
    if (!d) return "—";
    var sec = (Date.now() - d.getTime()) / 1000;
    if (sec < 5) return "just now";
    if (sec < 60) return Math.round(sec) + "s ago";
    if (sec < 3600) return Math.round(sec / 60) + "m ago";
    if (sec < 86400) return Math.round(sec / 3600) + "h ago";
    return d.toLocaleDateString();
  }
  function wilson95(k, n) {
    if (!n) return null;
    var z = 1.95996, p = k / n, z2 = z * z, den = 1 + z2 / n;
    var c = (p + z2 / (2 * n)) / den;
    var h = z * Math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / den;
    return { p: p, lo: Math.max(0, c - h), hi: Math.min(1, c + h) };
  }
  function median(arr) {
    if (!arr.length) return NaN;
    var s = arr.slice().sort(function (a, b) { return a - b; });
    var m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }

  function enrichJob(j) {
    var created = isoDate(j.created_at), started = isoDate(j.started_at), finished = isoDate(j.finished_at);
    var waitSec = (created && started) ? Math.max(0, (started - created) / 1000) : null;
    var durSec = (started && finished) ? Math.max(0, (finished - started) / 1000) : (started && j.status === "RUNNING" ? Math.max(0, (Date.now() - started) / 1000) : null);
    var total = Number(j.total_items) || 0, completed = Number(j.completed_items) || 0, failed = Number(j.failed_items) || 0;
    var rate = (durSec && durSec > 0 && completed > 0 && finished) ? (completed / durSec) : null;
    j._created = created; j._started = started; j._finished = finished;
    j._waitSec = waitSec; j._durSec = durSec; j._rate = rate;
    j._total = total; j._completed = completed; j._failed = failed;
    j._uids = Array.isArray(j.study_uids) ? j.study_uids : [];
    j._status = String(j.status || "").toUpperCase();
    return j;
  }

  function isValidUid(uid) {
    var u = String(uid || "").trim();
    return !!u && u.length <= 64 && UID_RE.test(u) && !PLACEHOLDER_UIDS.has(u.toLowerCase());
  }

  function diagnosticHint(job) {
    var code = String(job.error_code || "").toUpperCase();
    var msg = String(job.error_message || "").toLowerCase();
    if (code === "NO_MATCHES") return "Peer returned 0 matching instances for the requested Study UID. Confirm the Study UID exists on the peer and that Query/Retrieve (C-GET / WADO-RS) is enabled for our AE title.";
    if (code === "NO_SUCCESSFUL_INSTANCES") return "Instances were matched, but none transferred. Check disk space, storage permissions, or SOP Class / Transfer Syntax negotiation with the peer.";
    if (/refused|econnrefused|unreachable|connect/.test(msg)) return "TCP connection was refused or unreachable. Verify the peer host:port, firewall rules, and run C-ECHO Ping in Registered nodes.";
    if (/timeout|timed out/.test(msg)) return "Network or DIMSE association timed out. Check peer responsiveness, large-study timeout settings, or try DICOMweb transport if available.";
    if (/rejected|called ae|calling ae|unauthorized|401|403/.test(msg)) return "Peer rejected the association or HTTP credentials. Check Calling AE Title (our_ae_title), peer whitelist, or DICOMweb Basic auth credentials.";
    if (/tls|cert|ssl/.test(msg)) return "TLS handshake or certificate pinning failed. Verify the peer's trust_cert PEM or global DICOM_TLS_CA.";
    if (job.status === "PARTIAL") return "Some instances succeeded while others failed. Inspect peer logs or re-run the job to transmit missing instances.";
    return null;
  }

  function progressText(job) {
    var c = job._completed, f = job._failed;
    if (job._status === "PENDING") return "Waiting in queue · starts automatically when a worker is free";
    if (job._status === "RUNNING") return job.direction === "RETRIEVE"
      ? c + " received & indexed" + (f ? " · " + f + " failed" : "")
      : c + " confirmed sent" + (f ? " · " + f + " failed" : "");
    if (job._status === "COMPLETED") return job.direction === "RETRIEVE"
      ? c + " image" + (c === 1 ? "" : "s") + " saved & indexed"
      : c + " image" + (c === 1 ? "" : "s") + " confirmed by " + job.node_ae_title;
    if (job._status === "PARTIAL") return c + " completed · " + f + " failed";
    if (job._status === "CANCELLED") return "Cancelled before worker start";
    if (job.error_code === "NO_MATCHES") return "Remote peer returned no matching images";
    if (job.error_code === "NO_SUCCESSFUL_INSTANCES") return "Matched images failed to transfer";
    return "Failed" + (job.error_code ? " · " + job.error_code : "");
  }

  /* ------------------------------------------------------------ data loading */
  async function ensureNodes() {
    if (Array.isArray(window.PAXS_NODES) && window.PAXS_NODES.length) {
      S.nodes = window.PAXS_NODES;
      return S.nodes;
    }
    try {
      var res = await call("/api/v1/nodes");
      if (res.ok) {
        S.nodes = await res.json();
        window.PAXS_NODES = S.nodes;
      }
    } catch (_) { /* ignore */ }
    return S.nodes;
  }

  async function load(opts) {
    var list = $id("transfer-list");
    if (!list || !signedIn()) return;
    if (S.loading && !(opts && opts.force)) return;
    S.loading = true;
    var rfBtn = $id("transfer-refresh");
    if (rfBtn) rfBtn.classList.add("is-busy");
    var limit = Math.min(100, Math.max(10, +P.limit || 50));
    try {
      var res = await call("/api/v1/transfers?limit=" + limit + "&offset=0");
      if (!res.ok) {
        S.error = res.status === 403
          ? "Your account role cannot view transfer jobs."
          : "Could not load transfer jobs (HTTP " + res.status + ").";
      } else {
        var rows = await res.json();
        S.error = null;
        S.jobs = (Array.isArray(rows) ? rows : []).map(enrichJob);
        S.hasMore = S.jobs.length >= limit;
        S.loadedAt = new Date();
      }
    } catch (_) {
      S.error = "Transfer jobs service could not be reached.";
    } finally {
      S.loading = false;
      if (rfBtn) rfBtn.classList.remove("is-busy");
    }
    if (!S.nodes.length) await ensureNodes();
    renderAll();
    startPoll();
  }

  async function loadMore() {
    if (S.loading) return;
    var limit = Math.min(100, Math.max(10, +P.limit || 50));
    var offset = S.jobs.length;
    var btn = $id("tx-load-more");
    if (btn) { btn.disabled = true; btn.textContent = "Loading…"; }
    try {
      var res = await call("/api/v1/transfers?limit=" + limit + "&offset=" + offset);
      if (res.ok) {
        var extra = await res.json();
        var seen = new Set(S.jobs.map(function (j) { return j.id; }));
        extra.forEach(function (j) { if (!seen.has(j.id)) S.jobs.push(enrichJob(j)); });
        S.hasMore = extra.length >= limit;
        renderAll();
      }
    } catch (_) {
      notify("Could not load older jobs", "err");
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Load older jobs…"; }
    }
  }

  function startPoll() {
    stopPoll();
    var view = $id("view-nodes");
    if (!view || !view.classList.contains("active")) return;
    if (P.paused || !(P.pollSec > 0)) return;
    S.pollTimer = setInterval(function () {
      var v = $id("view-nodes");
      if (v && v.classList.contains("active") && !document.hidden && !S.loading) load();
    }, P.pollSec * 1000);
  }
  function stopPoll() {
    if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
  }

  /* ------------------------------------------------------------ filtering & sorting */
  function matchesSearch(j, q) {
    if (!q) return true;
    var hay = [
      "#" + j.id,
      String(j.id),
      j.node_ae_title || "",
      j.direction || "",
      j.transport || "",
      j.status || "",
      j.error_code || "",
      j.error_message || "",
      j._uids.join(" ")
    ].join(" ").toLowerCase();
    return q.toLowerCase().split(/\s+/).every(function (tok) { return hay.indexOf(tok) >= 0; });
  }

  function filteredJobs(ignoreStatus) {
    var list = S.jobs.filter(function (j) {
      if (!ignoreStatus && P.status) {
        if (P.status === "ISSUE") {
          if (j._status !== "FAILED" && j._status !== "PARTIAL") return false;
        } else if (j._status !== P.status) return false;
      }
      if (P.dir && j.direction !== P.dir) return false;
      if (P.transport && j.transport !== P.transport) return false;
      if (P.node && j.node_ae_title !== P.node) return false;
      return matchesSearch(j, P.q);
    });
    var prio = { RUNNING: 0, PENDING: 1, FAILED: 2, PARTIAL: 3, COMPLETED: 4, CANCELLED: 5 };
    var cmp = {
      newest: function (a, b) { return b.id - a.id; },
      oldest: function (a, b) { return a.id - b.id; },
      status: function (a, b) { return (prio[a._status] ?? 9) - (prio[b._status] ?? 9) || b.id - a.id; },
      images: function (a, b) { return (b._completed + b._failed) - (a._completed + a._failed) || b.id - a.id; },
      duration: function (a, b) { return (b._durSec || -1) - (a._durSec || -1) || b.id - a.id; }
    }[P.sort] || function (a, b) { return b.id - a.id; };
    return list.sort(cmp);
  }

  /* ------------------------------------------------------------ rendering */
  function renderHeaderMeta() {
    var badge = $id("tx-total-badge");
    if (badge) badge.textContent = S.jobs.length ? String(S.jobs.length) : "0";
    var liveBtn = $id("tx-live");
    if (liveBtn) {
      var active = !P.paused && P.pollSec > 0;
      liveBtn.classList.toggle("is-paused", !active);
      liveBtn.setAttribute("aria-pressed", String(active));
      liveBtn.querySelector("span").textContent = active ? "live" : "paused";
    }
    var upd = $id("tx-updated");
    if (upd) {
      upd.classList.toggle("is-err", !!S.error);
      upd.textContent = S.error ? "⚠ " + S.error : S.loadedAt ? "Updated " + S.loadedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
    }
    var clearBtn = $id("transfer-clear-history");
    if (clearBtn) {
      var hasFinished = S.jobs.some(function (j) { return j._status !== "PENDING" && j._status !== "RUNNING"; });
      clearBtn.disabled = !hasFinished;
    }
  }

  function renderStats() {
    var stats = $id("transfer-stats");
    var strip = $id("tx-node-strip");
    if (!stats) return;
    if (!S.jobs.length) {
      stats.hidden = true;
      if (strip) strip.hidden = true;
      return;
    }
    var t = { PENDING: 0, RUNNING: 0, COMPLETED: 0, FAILED: 0, PARTIAL: 0, CANCELLED: 0 };
    var activeDone = 0, activeTot = 0, totalImgs = 0, durs = [], rates = [], byNode = {};
    S.jobs.forEach(function (j) {
      t[j._status] = (t[j._status] || 0) + 1;
      totalImgs += j._completed;
      if (j._status === "RUNNING") { activeDone += j._completed; activeTot += j._total; }
      if (j._status === "COMPLETED" && j._durSec != null) durs.push(j._durSec);
      if (j._rate != null) rates.push(j._rate);
      var ae = j.node_ae_title || "UNKNOWN";
      if (!byNode[ae]) byNode[ae] = { ae: ae, total: 0, ok: 0, issue: 0, active: 0 };
      byNode[ae].total++;
      if (j._status === "COMPLETED") byNode[ae].ok++;
      if (j._status === "FAILED" || j._status === "PARTIAL") byNode[ae].issue++;
      if (j._status === "PENDING" || j._status === "RUNNING") byNode[ae].active++;
    });
    var finishedOutcome = t.COMPLETED + t.FAILED + t.PARTIAL;
    var w = wilson95(t.COMPLETED, finishedOutcome);
    var medDur = median(durs), medRate = median(rates);

    var tile = function (filterVal, cls, val, label, sub) {
      var active = P.status === filterVal && filterVal !== "__NONE__" ? " is-active" : "";
      return '<button type="button" class="transfer-stat ' + cls + active + '" data-tx-stat="' + filterVal + '"><b>' + val + "</b><span>" + label + "</span><small>" + (sub || "") + "</small></button>";
    };
    stats.innerHTML =
      tile("PENDING", "is-queued", t.PENDING, "Queued", t.PENDING ? "click to filter" : "queue clear") +
      tile("RUNNING", "is-running", t.RUNNING, "Running", activeTot ? activeDone + "/" + activeTot + " img" : (t.RUNNING ? "active association" : "idle")) +
      tile("COMPLETED", "is-done", t.COMPLETED, "Completed", totalImgs + " images moved") +
      tile("ISSUE", "is-issue", t.FAILED + t.PARTIAL, "Needs attention", t.FAILED + " failed · " + t.PARTIAL + " partial") +
      tile("__NONE__", "is-rate", w ? (w.p * 100).toFixed(0) + "%" : "—", "Success rate", w ? "95% CI " + Math.round(w.lo * 100) + "–" + Math.round(w.hi * 100) + "%" : "no finished jobs") +
      tile("__NONE__", "is-speed", isFinite(medDur) ? fmtDurSec(medDur) : "—", "Median duration", isFinite(medRate) ? medRate.toFixed(1) + " img/s" : (durs.length ? "n=" + durs.length : "no timed jobs"));
    stats.hidden = false;

    if (strip) {
      var aes = Object.keys(byNode).sort();
      if (aes.length >= 1) {
        strip.hidden = false;
        strip.innerHTML = '<span class="tx-strip-lbl">Peers:</span>' + aes.map(function (ae) {
          var b = byNode[ae];
          var cls = P.node === ae ? " is-active" : "";
          var tone = b.issue ? " is-issue" : b.active ? " is-run" : "";
          return '<button type="button" class="tx-node-chip' + cls + tone + '" data-tx-node="' + H(ae) + '" title="Filter by ' + H(ae) + '">' +
            '<b class="mono">' + H(ae) + "</b><i>" + b.ok + "/" + b.total + "</i></button>";
        }).join("");
      } else {
        strip.hidden = true;
      }
    }
  }

  function refreshFacets() {
    var base = filteredJobs(true);
    var c = { "": base.length, PENDING: 0, RUNNING: 0, COMPLETED: 0, ISSUE: 0, CANCELLED: 0 };
    base.forEach(function (j) {
      if (j._status === "FAILED" || j._status === "PARTIAL") c.ISSUE++;
      else if (c[j._status] != null) c[j._status]++;
    });
    qsa("#tx-status [data-txc]").forEach(function (el) { el.textContent = c[el.dataset.txc] || 0; });

    var nodeSel = $id("tx-node");
    if (nodeSel) {
      var aes = Array.from(new Set(S.jobs.map(function (j) { return j.node_ae_title; }).concat(S.nodes.map(function (n) { return n.ae_title; })).filter(Boolean))).sort();
      nodeSel.innerHTML = ['<option value="">All peers</option>'].concat(aes.map(function (ae) {
        return '<option value="' + H(ae) + '"' + (ae === P.node ? " selected" : "") + ">" + H(ae) + "</option>";
      })).join("");
      nodeSel.value = P.node || "";
    }
    var clr = $id("tx-clear-filters");
    if (clr) clr.hidden = !(P.q || P.status || P.dir || P.transport || P.node);
  }

  function renderJobActions(j) {
    var btns = [];
    if (j._status === "PENDING") btns.push('<button type="button" class="tx-act danger" data-tx-cancel="' + j.id + '" title="Cancel queued job">✕</button>');
    if (j._status === "FAILED" || j._status === "PARTIAL" || j._status === "CANCELLED") {
      var canRetry = j.direction === "SEND" || isAdmin();
      if (canRetry) btns.push('<button type="button" class="tx-act ok" data-tx-retry="' + j.id + '" title="Retry job">↻</button>');
    }
    if (j._uids.length === 1 && (j.direction === "SEND" || j._status === "COMPLETED")) {
      btns.push('<a class="tx-act" href="' + H(studyHref(j._uids[0])) + '" target="_blank" rel="noopener" title="Open study in viewer">◉</a>');
    }
    btns.push('<button type="button" class="tx-act" data-tx-copy="' + j.id + '" title="Copy Study UID(s)">⧉</button>');
    btns.push('<button type="button" class="tx-act" data-tx-inspect="' + j.id + '" title="Inspect job details">ⓘ</button>');
    return btns.join("");
  }

  function renderList() {
    var list = $id("transfer-list");
    if (!list) return;
    refreshFacets();
    if (S.error && !S.jobs.length) {
      list.innerHTML = '<div class="wf-request-error" role="alert"><span aria-hidden="true">!</span><p>' + H(S.error) + '</p><button class="btn sm" type="button" data-tx-reload>Retry</button></div>';
      updateFooter(0);
      updateBulk();
      return;
    }
    if (!S.jobs.length) {
      list.innerHTML = '<div class="transfer-empty"><b>No transfer jobs yet</b><span>Click <b>＋ New transfer</b> above to send local studies or retrieve from a peer, or queue from the Studies / Find views.</span></div>';
      updateFooter(0);
      updateBulk();
      return;
    }

    var rows = filteredJobs(false);
    if (!rows.length) {
      list.innerHTML = '<div class="transfer-empty"><b>No matching jobs</b><span>Loosen or clear the active search and status filters.</span></div>';
      updateFooter(0);
      updateBulk();
      return;
    }

    if (P.layout === "table") {
      var head = '<div class="tx-trow tx-thead">' +
        '<label class="tx-sel" title="Select all visible"><input type="checkbox" id="tx-selall"' + (rows.every(function (j) { return S.sel.has(j.id); }) ? " checked" : "") + " /></label>" +
        "<div>Job</div><div>Direction &amp; Peer</div><div>Transport</div><div>Progress</div><div>Duration</div><div>Created</div><div>Actions</div></div>";
      var body = rows.map(function (j) {
        var sel = S.sel.has(j.id);
        var pct = j._total > 0 ? Math.min(100, Math.round((j._completed / j._total) * 100)) : 0;
        var progTxt = j._total > 0 ? (j._completed + "/" + j._total + " (" + pct + "%)") : (j._completed + " img");
        return '<div class="tx-trow' + (sel ? " is-sel" : "") + '" data-job-id="' + j.id + '">' +
          '<label class="tx-sel"><input type="checkbox" data-tx-sel="' + j.id + '"' + (sel ? " checked" : "") + " /></label>" +
          '<div class="mono"><b>#' + j.id + "</b> <span class=\"transfer-status " + H(j._status.toLowerCase()) + '">' + H(j._status) + "</span></div>" +
          '<div class="tx-tpeer"><span class="tx-dir-tag ' + (j.direction === "RETRIEVE" ? "ret" : "snd") + '">' + (j.direction === "RETRIEVE" ? "↓ RETRIEVE" : "↑ SEND") + '</span> <b class="mono">' + H(j.node_ae_title) + "</b><small>" + j._uids.length + " " + (j._uids.length === 1 ? "study" : "studies") + "</small></div>" +
          '<div class="mono">' + H(j.transport) + "</div>" +
          '<div class="tx-tprog"><span class="mono">' + H(progTxt) + (j._failed ? ' <i class="tx-fail-n">(' + j._failed + " err)</i>" : "") + '</span><div class="transfer-progress-track ' + (j._total > 0 ? "determinate" : j._status === "RUNNING" ? "indeterminate" : "empty") + '"><span style="width:' + (j._total > 0 ? pct : 0) + '%"></span></div></div>' +
          '<div class="mono">' + fmtDurSec(j._durSec) + (j._rate ? "<small class=\"tx-dim\"> " + j._rate.toFixed(1) + "/s</small>" : "") + "</div>" +
          '<div class="tx-dim" title="' + H(j._created ? j._created.toLocaleString() : "") + '">' + fmtRel(j._created) + "</div>" +
          '<div class="tx-row-acts">' + renderJobActions(j) + "</div></div>";
      }).join("");
      list.innerHTML = '<div class="tx-table">' + head + body + "</div>";
    } else {
      list.innerHTML = rows.map(function (j) {
        var sel = S.sel.has(j.id);
        var active = j._status === "RUNNING";
        var knownTotal = j._total > 0;
        var pct = knownTotal ? Math.min(100, Math.round((j._completed / j._total) * 100)) : 0;
        var progressLabel = knownTotal ? pct + "%" : active ? "Running" : j._status === "PENDING" ? "Queued" : "—";
        var isRet = j.direction === "RETRIEVE";
        var uidPreview = j._uids.length === 1 ? j._uids[0] : (j._uids.length + " studies (" + (j._uids[0] || "").slice(0, 24) + "…)");
        var timeMeta = j._finished
          ? "Finished " + fmtRel(j._finished) + (j._durSec != null ? " · took " + fmtDurSec(j._durSec) : "") + (j._rate ? " (" + j._rate.toFixed(1) + " img/s)" : "")
          : j._started
            ? "Started " + fmtRel(j._started) + (j._waitSec != null ? " · waited " + fmtDurSec(j._waitSec) : "")
            : "Queued " + fmtRel(j._created);
        return '<article class="transfer-job ' + (active ? "is-running " : "") + (sel ? "is-sel" : "") + '" data-status="' + H(j._status.toLowerCase()) + '" data-job-id="' + j.id + '">' +
          '<div class="transfer-job-top">' +
            '<div class="transfer-job-title">' +
              '<label class="tx-sel" title="Select job"><input type="checkbox" data-tx-sel="' + j.id + '"' + (sel ? " checked" : "") + " /></label>" +
              '<span class="transfer-direction ' + (isRet ? "retrieve" : "send") + '" aria-hidden="true">' + (isRet ? "↓" : "↑") + "</span>" +
              "<div>" +
                '<div class="name">' + (isRet ? "Retrieve from" : "Send to") + " " + H(j.node_ae_title) + "</div>" +
                '<div class="sub mono">' + H(j.transport) + " · job #" + j.id + " · " + H(uidPreview) + "</div>" +
              "</div>" +
            "</div>" +
            '<div class="transfer-job-tags">' +
              '<span class="transfer-status ' + H(j._status.toLowerCase()) + '">' + H(j._status) + "</span>" +
              renderJobActions(j) +
            "</div>" +
          "</div>" +
          '<div class="transfer-progress-head"><span>' + H(progressText(j)) + "</span><strong>" + progressLabel + "</strong></div>" +
          '<div class="transfer-progress-track ' + (knownTotal ? "determinate" : active ? "indeterminate" : "empty") + '" role="progressbar"><span style="width:' + (knownTotal ? pct : 0) + '%"></span></div>' +
          '<div class="transfer-job-meta"><span>' + (knownTotal ? (j._completed + j._failed) + " of " + j._total + " images processed" : active ? j._completed + " moved · " + j._failed + " failed" : j._completed + " completed · " + j._failed + " failed") + "</span><span>" + H(timeMeta) + "</span></div>" +
          (j.error_message ? '<div class="transfer-error">' + H(j.error_message) + "</div>" : "") +
          "</article>";
      }).join("");
    }
    updateFooter(rows.length);
    updateBulk();
  }

  function updateFooter(shown) {
    var f = $id("tx-footer"), cnt = $id("tx-footer-count"), more = $id("tx-load-more");
    if (!f) return;
    if (!S.jobs.length) { f.hidden = true; return; }
    f.hidden = false;
    if (cnt) cnt.textContent = "Showing " + shown + " of " + S.jobs.length + " loaded jobs";
    if (more) more.hidden = !S.hasMore;
  }

  function updateBulk() {
    var bar = $id("tx-bulk"); if (!bar) return;
    var valid = new Set(S.jobs.map(function (j) { return j.id; }));
    S.sel.forEach(function (id) { if (!valid.has(id)) S.sel.delete(id); });
    bar.hidden = !S.sel.size;
    var n = $id("tx-bulk-n"); if (n) n.textContent = S.sel.size + " selected";
  }

  function renderAll() {
    renderHeaderMeta();
    renderStats();
    renderList();
  }

  /* ------------------------------------------------------------ actions */
  async function cancelJob(jobId, skipConfirm) {
    var j = S.jobs.find(function (r) { return r.id === jobId; });
    if (!j || j._status !== "PENDING") return false;
    if (!skipConfirm && !window.confirm("Cancel queued " + j.direction.toLowerCase() + " job #" + jobId + "?")) return false;
    var res = await call("/api/v1/transfers/" + jobId + "/cancel", { method: "POST" });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) { notify(data.detail || "Could not cancel job #" + jobId, "err"); return false; }
    if (!skipConfirm) { notify("Transfer #" + jobId + " cancelled", "ok"); load(); }
    return true;
  }

  async function enqueueJob(payload, quiet) {
    var res = await call("/api/v1/transfers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) {
      if (!quiet) notify(data.detail || "Could not queue transfer (" + res.status + ")", "err");
      return null;
    }
    if (!quiet) {
      notify(payload.direction + " queued for " + payload.node_ae_title + " · job #" + data.id, "ok");
      load();
    }
    return data;
  }

  async function retryJob(jobId, overrides) {
    var j = S.jobs.find(function (r) { return r.id === jobId; });
    if (!j || !j._uids.length) return null;
    var payload = Object.assign({
      direction: j.direction,
      transport: j.transport || "DIMSE",
      node_ae_title: j.node_ae_title,
      study_uids: j._uids.slice(0, 100),
      our_ae_title: P.ourAe || "PAXS_SCU"
    }, overrides || {});
    return enqueueJob(payload, !!(overrides && overrides.quiet));
  }

  async function clearHistory() {
    var btn = $id("transfer-clear-history");
    if (!btn || btn.disabled) return;
    if (!window.confirm("Clear up to 100 finished transfer records (completed, partial, failed, cancelled)? Active jobs and stored images are kept.")) return;
    btn.disabled = true;
    try {
      var res = await call("/api/v1/transfers/history?limit=100", { method: "DELETE" });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok) { notify(data.detail || "Could not clear history (" + res.status + ")", "err"); return; }
      notify(data.deleted ? data.deleted + " finished job records cleared" : "No finished records to clear", "ok");
      await load();
    } catch (_) {
      notify("Transfer service could not be reached", "err");
    } finally {
      btn.disabled = false;
    }
  }

  async function copyText(txt, okMsg) {
    try {
      await navigator.clipboard.writeText(txt);
      notify(okMsg || "Copied to clipboard", "ok");
    } catch (_) {
      notify("Clipboard copy blocked by browser", "err");
    }
  }

  /* ------------------------------------------------------------ drawer: inspector & composer */
  function openDrawer(eyebrow, title, html) {
    $id("tx-drawer-eyebrow").innerHTML = '<i aria-hidden="true"></i>' + H(eyebrow);
    $id("tx-drawer-title").textContent = title;
    $id("tx-drawer-body").innerHTML = html;
    $id("tx-scrim").hidden = false;
    var dr = $id("tx-drawer");
    dr.setAttribute("aria-hidden", "false");
    dr.classList.add("is-open");
  }
  function closeDrawer() {
    S.inspectId = null;
    S.composerOpen = false;
    if (S.inspectTimer) { clearInterval(S.inspectTimer); S.inspectTimer = null; }
    $id("tx-scrim").hidden = true;
    var dr = $id("tx-drawer");
    dr.setAttribute("aria-hidden", "true");
    dr.classList.remove("is-open");
  }

  async function openInspector(jobId) {
    S.composerOpen = false;
    S.inspectId = jobId;
    var j = S.jobs.find(function (r) { return r.id === jobId; });
    try {
      var res = await call("/api/v1/transfers/" + jobId);
      if (res.ok) {
        j = enrichJob(await res.json());
        var idx = S.jobs.findIndex(function (r) { return r.id === jobId; });
        if (idx >= 0) S.jobs[idx] = j;
      }
    } catch (_) { /* use cached */ }
    if (!j) return;
    await ensureNodes();
    var node = S.nodes.find(function (n) { return n.ae_title === j.node_ae_title; });
    var hint = diagnosticHint(j);
    var knownTotal = j._total > 0;
    var pct = knownTotal ? Math.min(100, Math.round((j._completed / j._total) * 100)) : 0;

    var actBtns = [];
    if (j._status === "PENDING") actBtns.push('<button type="button" class="btn sm danger" id="tx-i-cancel">✕ Cancel job</button>');
    if (j._status === "FAILED" || j._status === "PARTIAL" || j._status === "CANCELLED") {
      if (j.direction === "SEND" || isAdmin()) actBtns.push('<button type="button" class="btn sm primary" id="tx-i-retry">↻ Retry job</button>');
      if (j.direction === "RETRIEVE" && isAdmin() && node && node.web_url) {
        var alt = j.transport === "DICOMWEB" ? "DIMSE" : "DICOMWEB";
        actBtns.push('<button type="button" class="btn sm" id="tx-i-alt">⇄ Retry via ' + alt + "</button>");
      }
    }
    actBtns.push('<button type="button" class="btn sm ghost" id="tx-i-copy-all">⧉ Copy all UIDs</button>');

    var uidsHtml = j._uids.map(function (uid, i) {
      var canView = j.direction === "SEND" || j._status === "COMPLETED";
      return '<div class="tx-uid-row"><span class="tx-uid-i">' + (i + 1) + '</span><span class="mono tx-uid-v" title="' + H(uid) + '">' + H(uid) + "</span>" +
        '<div class="tx-uid-acts">' +
        '<button type="button" class="tx-act" data-uid-copy="' + H(uid) + '" title="Copy UID">⧉</button>' +
        (canView ? '<a class="tx-act" href="' + H(studyHref(uid)) + '" target="_blank" rel="noopener" title="Open in viewer">◉</a>' : "") +
        ((j.direction === "SEND" || isAdmin()) ? '<button type="button" class="tx-act ok" data-uid-retry="' + H(uid) + '" title="Queue single-study retry">↻</button>' : "") +
        "</div></div>";
    }).join("");

    openDrawer("Transfer job · #" + j.id, (j.direction === "RETRIEVE" ? "Retrieve from " : "Send to ") + j.node_ae_title,
      '<div class="tx-i-top"><span class="transfer-status ' + H(j._status.toLowerCase()) + '">' + H(j._status) + '</span><span class="mono tx-dim">' + H(j.transport) + (node ? " · " + H(node.host) + ":" + node.port : "") + "</span></div>" +
      '<div class="tx-i-acts">' + actBtns.join("") + "</div>" +
      '<div class="tx-i-prog">' +
        '<div class="transfer-progress-head"><span>' + H(progressText(j)) + "</span><strong>" + (knownTotal ? pct + "%" : j._status) + "</strong></div>" +
        '<div class="transfer-progress-track ' + (knownTotal ? "determinate" : j._status === "RUNNING" ? "indeterminate" : "empty") + '"><span style="width:' + (knownTotal ? pct : 0) + '%"></span></div>' +
      "</div>" +
      '<div class="tx-i-grid">' +
        '<div><span>Created</span><b class="mono">' + (j._created ? j._created.toLocaleString() : "—") + "</b></div>" +
        '<div><span>Started</span><b class="mono">' + (j._started ? j._started.toLocaleString() : "—") + "</b></div>" +
        '<div><span>Finished</span><b class="mono">' + (j._finished ? j._finished.toLocaleString() : "—") + "</b></div>" +
        '<div><span>Queue wait</span><b class="mono">' + fmtDurSec(j._waitSec) + "</b></div>" +
        '<div><span>Duration</span><b class="mono">' + fmtDurSec(j._durSec) + "</b></div>" +
        '<div><span>Throughput</span><b class="mono">' + (j._rate ? j._rate.toFixed(2) + " img/s" : "—") + "</b></div>" +
      "</div>" +
      ((j.error_code || j.error_message || hint) ? '<div class="tx-i-diag">' +
        (j.error_code ? '<div class="mono"><b>Error code:</b> ' + H(j.error_code) + "</div>" : "") +
        (j.error_message ? "<div>" + H(j.error_message) + "</div>" : "") +
        (hint ? '<div class="tx-i-hint"><b>Hint:</b> ' + H(hint) + "</div>" : "") +
      "</div>" : "") +
      '<div class="tx-i-uids"><h4>Study Instance UIDs (' + j._uids.length + ")</h4>" + uidsHtml + "</div>");

    var body = $id("tx-drawer-body");
    var cBtn = $id("tx-i-cancel"); if (cBtn) cBtn.addEventListener("click", async function () { if (await cancelJob(j.id)) openInspector(j.id); });
    var rBtn = $id("tx-i-retry"); if (rBtn) rBtn.addEventListener("click", async function () { var d = await retryJob(j.id); if (d) openInspector(d.id); });
    var aBtn = $id("tx-i-alt"); if (aBtn) aBtn.addEventListener("click", async function () { var d = await retryJob(j.id, { transport: j.transport === "DICOMWEB" ? "DIMSE" : "DICOMWEB" }); if (d) openInspector(d.id); });
    var cpAll = $id("tx-i-copy-all"); if (cpAll) cpAll.addEventListener("click", function () { copyText(j._uids.join("\n"), "Copied " + j._uids.length + " UID(s)"); });
    qsa("[data-uid-copy]", body).forEach(function (b) { b.addEventListener("click", function () { copyText(b.dataset.uidCopy, "UID copied"); }); });
    qsa("[data-uid-retry]", body).forEach(function (b) {
      b.addEventListener("click", async function () {
        var d = await enqueueJob({ direction: j.direction, transport: j.transport || "DIMSE", node_ae_title: j.node_ae_title, study_uids: [b.dataset.uidRetry], our_ae_title: P.ourAe || "PAXS_SCU" });
        if (d) openInspector(d.id);
      });
    });

    if (S.inspectTimer) { clearInterval(S.inspectTimer); S.inspectTimer = null; }
    if (j._status === "PENDING" || j._status === "RUNNING") {
      S.inspectTimer = setInterval(function () {
        if (S.inspectId === j.id && $id("tx-drawer").classList.contains("is-open")) openInspector(j.id);
        else { clearInterval(S.inspectTimer); S.inspectTimer = null; }
      }, 3000);
    }
  }

  async function openComposer() {
    S.inspectId = null;
    if (S.inspectTimer) { clearInterval(S.inspectTimer); S.inspectTimer = null; }
    S.composerOpen = true;
    S.batchUids = [];
    await ensureNodes();
    var enabledNodes = S.nodes.filter(function (n) { return n.enabled; });
    var nodeOpts = S.nodes.map(function (n) {
      return '<option value="' + H(n.ae_title) + '"' + (n.enabled ? "" : " disabled") + ">" + H(n.ae_title) + " (" + H(n.host) + ":" + n.port + (n.web_url ? " · web" : "") + (n.enabled ? "" : " · disabled") + ")</option>";
    }).join("");

    openDrawer("New transfer job", "Queue Send or Retrieve",
      '<form id="tx-comp-form" class="tx-comp-form">' +
      '<div class="tx-fgrid">' +
        '<label class="tx-f"><span>Direction</span><select name="direction" id="tx-c-dir">' +
          '<option value="SEND">↑ Send local studies (C-STORE)</option>' +
          (isAdmin() ? '<option value="RETRIEVE">↓ Retrieve from peer</option>' : '<option value="RETRIEVE" disabled>↓ Retrieve (ADMIN only)</option>') +
        "</select></label>" +
        '<label class="tx-f"><span>Transport</span><select name="transport" id="tx-c-trans"><option value="DIMSE">DIMSE (Association)</option><option value="DICOMWEB" disabled>DICOMweb (WADO-RS)</option><option value="CMOVE" disabled>DCMTK (C-MOVE)</option></select></label>' +
        '<label class="tx-f tx-f-full"><span>Peer Node (AE Title)</span><div class="tx-f-in"><select name="node_ae_title" id="tx-c-node" required>' +
          (enabledNodes.length ? nodeOpts : '<option value="">No enabled nodes registered</option>') +
        '</select><button type="button" class="btn sm ghost" id="tx-c-ping" title="Test C-ECHO connectivity">Ping</button></div><small class="tx-dim" id="tx-c-ping-res"></small></label>' +
        '<label class="tx-f tx-f-full"><span>Calling AE Title <small class="mono">(our_ae_title, ≤ 16 chars)</small></span><input name="our_ae_title" id="tx-c-ourae" maxlength="16" value="' + H(P.ourAe || "PAXS_SCU") + '" /></label>' +
      "</div>" +
      '<div id="tx-c-study-picker" class="tx-picker">' +
        '<div class="tx-picker-head"><span>Pick local studies to send</span><input type="search" id="tx-c-study-q" placeholder="Filter by patient, MRN, accession…" /></div>' +
        '<div class="tx-picker-list" id="tx-c-study-list"><p class="sub">Type or click Search to load local studies…</p></div>' +
      "</div>" +
      '<div id="tx-c-ret-hint" class="tx-ret-hint" hidden><span>Retrieve takes 1 Study Instance UID per job. Want to browse the remote archive first?</span><button type="button" class="btn sm" id="tx-c-open-find">Search peer (C-FIND / QIDO)…</button></div>' +
      '<label class="tx-f tx-f-full"><span>Study Instance UIDs <small id="tx-c-uid-count" class="mono">(0 / 100)</small></span>' +
        '<textarea id="tx-c-uids" rows="4" placeholder="1.2.840.113619.2... (one per line or comma-separated)" spellcheck="false"></textarea>' +
      "</label>" +
      '<div class="tx-c-validation" id="tx-c-val"></div>' +
      '<div class="tx-drawer-foot"><button type="button" class="btn ghost" id="tx-c-cancel">Cancel</button><button type="submit" class="btn primary" id="tx-c-submit" disabled>Queue transfer</button></div>' +
      "</form>");

    var dirEl = $id("tx-c-dir"), transEl = $id("tx-c-trans"), nodeEl = $id("tx-c-node"), uidsEl = $id("tx-c-uids");
    var pickerEl = $id("tx-c-study-picker"), retHintEl = $id("tx-c-ret-hint"), valEl = $id("tx-c-val"), subBtn = $id("tx-c-submit");

    function parseUids() {
      var raw = (uidsEl.value || "").split(/[\s,;]+/).map(function (s) { return s.trim(); }).filter(Boolean);
      return raw;
    }
    function syncComposerState() {
      var dir = dirEl.value, ae = nodeEl.value;
      var node = S.nodes.find(function (n) { return n.ae_title === ae; });
      var webOpt = transEl.querySelector('option[value="DICOMWEB"]');
      var moveOpt = transEl.querySelector('option[value="CMOVE"]');
      moveOpt.disabled = dir !== "RETRIEVE" || !(node && node.dcmtk_move);
      if (dir === "SEND") {
        transEl.value = "DIMSE";
        if (webOpt) webOpt.disabled = true;
        pickerEl.hidden = false;
        retHintEl.hidden = true;
      } else {
        if (webOpt) webOpt.disabled = !(node && node.web_url);
        if (transEl.selectedOptions[0]?.disabled) transEl.value = "DIMSE";
        pickerEl.hidden = true;
        retHintEl.hidden = false;
      }
      var list = parseUids(), msgs = [];
      var uniq = Array.from(new Set(list));
      var bad = uniq.filter(function (u) { return !isValidUid(u); });
      if (uniq.length !== list.length) msgs.push("Duplicate UIDs removed (" + (list.length - uniq.length) + ")");
      if (bad.length) msgs.push(bad.length + " invalid DICOM UID(s) (digits & dots only, ≤ 64 chars)");
      if (dir === "RETRIEVE" && uniq.length > 1) msgs.push("RETRIEVE accepts exactly 1 Study UID per job");
      if (dir === "SEND" && uniq.length > 100) msgs.push("Maximum 100 Study UIDs per SEND job");
      var ourAe = ($id("tx-c-ourae").value || "").trim();
      if (!ourAe || ourAe.length > 16 || AE_BAD.test(ourAe)) msgs.push("Calling AE must be 1–16 valid AE characters");
      $id("tx-c-uid-count").textContent = "(" + uniq.length + " / " + (dir === "RETRIEVE" ? 1 : 100) + ")";
      valEl.innerHTML = msgs.map(function (m) { return "<span>⚠ " + H(m) + "</span>"; }).join("");
      subBtn.disabled = !ae || !uniq.length || bad.length > 0 || (dir === "RETRIEVE" && uniq.length !== 1) || uniq.length > 100 || !ourAe || ourAe.length > 16 || AE_BAD.test(ourAe);
    }

    async function searchLocalStudies(q) {
      var box = $id("tx-c-study-list"); if (!box) return;
      box.innerHTML = '<p class="sub">Searching…</p>';
      try {
        var params = new URLSearchParams({ page: "1", limit: "12" });
        if (q) params.set("search", q);
        var res = await call("/api/v1/studies?" + params.toString());
        if (!res.ok) { box.innerHTML = '<p class="sub">Could not load studies (' + res.status + ")</p>"; return; }
        var data = await res.json();
        var items = data.items || [];
        if (!items.length) { box.innerHTML = '<p class="sub">No matching local studies.</p>'; return; }
        var current = new Set(parseUids());
        box.innerHTML = items.map(function (s) {
          var uid = s.study_uid || "";
          var added = current.has(uid);
          var mods = (s.modalities || []).join(",") || "DCM";
          return '<div class="tx-pick-row">' +
            '<div><b>' + H(s.patient_name || "Unknown") + '</b> <span class="mono tx-dim">' + H(s.patient_id || "") + '</span><small>' + H(s.description || "—") + " · " + H(mods) + " · " + (s.n_instances || 0) + " img</small></div>" +
            '<button type="button" class="btn sm ' + (added ? "ghost" : "") + '" data-pick-uid="' + H(uid) + '">' + (added ? "✓ Added" : "+ Add") + "</button>" +
          "</div>";
        }).join("");
      } catch (_) {
        box.innerHTML = '<p class="sub">Error querying studies.</p>';
      }
    }

    dirEl.addEventListener("change", syncComposerState);
    nodeEl.addEventListener("change", syncComposerState);
    transEl.addEventListener("change", syncComposerState);
    uidsEl.addEventListener("input", syncComposerState);
    $id("tx-c-ourae").addEventListener("input", syncComposerState);
    $id("tx-c-cancel").addEventListener("click", closeDrawer);

    $id("tx-c-ping").addEventListener("click", async function () {
      var ae = nodeEl.value, out = $id("tx-c-ping-res");
      if (!ae) return;
      out.textContent = "Pinging…";
      var res = await call("/api/v1/nodes/" + encodeURIComponent(ae) + "/ping", { method: "POST" });
      if (!res.ok) { out.textContent = "Ping failed (HTTP " + res.status + ")"; return; }
      var r = await res.json();
      out.textContent = r.ok ? "✓ C-ECHO OK (" + r.ae_title + ")" : "✕ C-ECHO failed: " + (r.reason || r.status || "unreachable");
    });

    $id("tx-c-open-find").addEventListener("click", function () {
      var ae = nodeEl.value, tr = transEl.value === "DICOMWEB" ? "dicomweb" : transEl.value === "CMOVE" ? "cmove" : "dicom";
      if (!ae) { notify("Select a peer node first", "err"); return; }
      closeDrawer();
      if (typeof openFind === "function") openFind(ae, tr);
    });

    var searchTimer = null;
    $id("tx-c-study-q").addEventListener("input", function (e) {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () { searchLocalStudies(e.target.value.trim()); }, 220);
    });
    $id("tx-c-study-list").addEventListener("click", function (e) {
      var b = e.target.closest("[data-pick-uid]"); if (!b) return;
      var uid = b.dataset.pickUid;
      var list = Array.from(new Set(parseUids()));
      var idx = list.indexOf(uid);
      if (idx >= 0) { list.splice(idx, 1); b.textContent = "+ Add"; b.classList.remove("ghost"); }
      else { list.push(uid); b.textContent = "✓ Added"; b.classList.add("ghost"); }
      uidsEl.value = list.join("\n");
      syncComposerState();
    });

    $id("tx-comp-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      var uids = Array.from(new Set(parseUids()));
      var ourAe = ($id("tx-c-ourae").value || "PAXS_SCU").trim();
      P.ourAe = ourAe; savePrefs();
      subBtn.disabled = true; subBtn.textContent = "Queuing…";
      var job = await enqueueJob({
        direction: dirEl.value,
        transport: transEl.value,
        node_ae_title: nodeEl.value,
        study_uids: uids,
        our_ae_title: ourAe
      });
      subBtn.textContent = "Queue transfer";
      if (job) { closeDrawer(); openInspector(job.id); }
      else syncComposerState();
    });

    syncComposerState();
    searchLocalStudies("");
  }

  /* ------------------------------------------------------------ export */
  function exportJobs(kind, subset) {
    var rows = subset || filteredJobs(false);
    var stamp = new Date().toISOString().slice(0, 10);
    if (kind === "json") {
      var blob = new Blob([JSON.stringify({ exported_at: new Date().toISOString(), count: rows.length, jobs: rows }, null, 2)], { type: "application/json" });
      var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "paxs-transfers-" + stamp + ".json"; a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
      return;
    }
    var cell = function (v) { var s = String(v == null ? "" : v); return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    var hdr = ["ID", "Status", "Direction", "Transport", "Node_AE", "Studies", "Completed", "Failed", "Total", "Duration_s", "Wait_s", "Rate_img_s", "Error_Code", "Error_Message", "Study_UIDs", "Created_At", "Started_At", "Finished_At"];
    var lines = [hdr.map(cell).join(",")].concat(rows.map(function (j) {
      return [j.id, j._status, j.direction, j.transport, j.node_ae_title, j._uids.length, j._completed, j._failed, j._total, j._durSec != null ? j._durSec.toFixed(2) : "", j._waitSec != null ? j._waitSec.toFixed(2) : "", j._rate != null ? j._rate.toFixed(2) : "", j.error_code, j.error_message, j._uids.join(";"), j.created_at, j.started_at, j.finished_at].map(cell).join(",");
    }));
    var csv = new Blob(["\uFEFF" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    var a2 = document.createElement("a"); a2.href = URL.createObjectURL(csv); a2.download = "paxs-transfers-" + stamp + ".csv"; a2.click();
    setTimeout(function () { URL.revokeObjectURL(a2.href); }, 2000);
  }

  /* ------------------------------------------------------------ sync & wiring */
  function setSeg(id, val) {
    qsa("#" + id + " button").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.v === val)); });
  }
  function syncControls() {
    setSeg("tx-status", P.status);
    setSeg("tx-layout", P.layout);
    $id("tx-q").value = P.q || "";
    $id("tx-dir").value = P.dir || "";
    $id("tx-transport").value = P.transport || "";
    $id("tx-sort").value = P.sort || "newest";
    $id("tx-limit").value = String(P.limit || 50);
    $id("tx-poll").value = String(P.pollSec);
  }
  function closeMenus() {
    qsa("#transfer-board .tx-menu-list").forEach(function (m) { m.hidden = true; });
    qsa("#transfer-board .tx-menu > button").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
  }

  function bind() {
    var board = $id("transfer-board"); if (!board) return;
    $id("tx-new").addEventListener("click", openComposer);
    $id("tx-drawer-close").addEventListener("click", closeDrawer);
    $id("tx-scrim").addEventListener("click", closeDrawer);
    $id("tx-load-more").addEventListener("click", loadMore);

    $id("tx-live").addEventListener("click", function () {
      P.paused = !P.paused; savePrefs(); renderHeaderMeta(); startPoll();
    });
    $id("tx-poll").addEventListener("change", function (e) {
      P.pollSec = +e.target.value; P.paused = P.pollSec === 0; savePrefs(); renderHeaderMeta(); startPoll();
    });

    /* export dropdown */
    $id("tx-export").addEventListener("click", function (e) {
      e.stopPropagation();
      var menu = $id("tx-export-menu").querySelector(".tx-menu-list");
      var wasOpen = !menu.hidden;
      closeMenus();
      menu.hidden = wasOpen;
      $id("tx-export").setAttribute("aria-expanded", String(!wasOpen));
    });
    $id("tx-export-menu").addEventListener("click", function (e) {
      var b = e.target.closest("[data-tx-export]"); if (!b) return;
      closeMenus(); exportJobs(b.dataset.txExport);
    });
    document.addEventListener("click", function (e) { if (!e.target.closest("#tx-export-menu")) closeMenus(); });

    /* telemetry & peer breakdown shortcuts */
    $id("transfer-stats").addEventListener("click", function (e) {
      var b = e.target.closest("[data-tx-stat]"); if (!b || b.dataset.txStat === "__NONE__") return;
      P.status = P.status === b.dataset.txStat ? "" : b.dataset.txStat;
      savePrefs(); syncControls(); renderAll();
    });
    $id("tx-node-strip").addEventListener("click", function (e) {
      var b = e.target.closest("[data-tx-node]"); if (!b) return;
      P.node = P.node === b.dataset.txNode ? "" : b.dataset.txNode;
      savePrefs(); renderAll();
    });

    /* filters */
    $id("tx-q").addEventListener("input", function (e) { P.q = e.target.value; savePrefs(); renderList(); });
    $id("tx-status").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-v]"); if (!b) return;
      P.status = b.dataset.v; savePrefs(); syncControls(); renderAll();
    });
    $id("tx-layout").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-v]"); if (!b) return;
      P.layout = b.dataset.v; savePrefs(); syncControls(); renderList();
    });
    $id("tx-dir").addEventListener("change", function (e) { P.dir = e.target.value; savePrefs(); renderList(); });
    $id("tx-transport").addEventListener("change", function (e) { P.transport = e.target.value; savePrefs(); renderList(); });
    $id("tx-node").addEventListener("change", function (e) { P.node = e.target.value; savePrefs(); renderAll(); });
    $id("tx-sort").addEventListener("change", function (e) { P.sort = e.target.value; savePrefs(); renderList(); });
    $id("tx-limit").addEventListener("change", function (e) { P.limit = +e.target.value; savePrefs(); load({ force: true }); });
    $id("tx-clear-filters").addEventListener("click", function () {
      Object.assign(P, { q: "", status: "", dir: "", transport: "", node: "" });
      savePrefs(); syncControls(); renderAll();
    });

    /* list delegation */
    var list = $id("transfer-list");
    list.addEventListener("change", function (e) {
      if (e.target.id === "tx-selall") {
        var visible = filteredJobs(false);
        visible.forEach(function (j) { if (e.target.checked) S.sel.add(j.id); else S.sel.delete(j.id); });
        renderList();
        return;
      }
      if (e.target.dataset.txSel) {
        var id = +e.target.dataset.txSel;
        if (e.target.checked) S.sel.add(id); else S.sel.delete(id);
        var card = e.target.closest("[data-job-id]"); if (card) card.classList.toggle("is-sel", e.target.checked);
        updateBulk();
      }
    });
    list.addEventListener("click", async function (e) {
      if (e.target.closest("[data-tx-reload]")) { load({ force: true }); return; }
      if (e.target.closest(".tx-sel") || e.target.closest("a.tx-act")) return;
      var cBtn = e.target.closest("[data-tx-cancel]");
      if (cBtn) { cancelJob(+cBtn.dataset.txCancel); return; }
      var rBtn = e.target.closest("[data-tx-retry]");
      if (rBtn) { rBtn.disabled = true; await retryJob(+rBtn.dataset.txRetry); rBtn.disabled = false; return; }
      var cpBtn = e.target.closest("[data-tx-copy]");
      if (cpBtn) {
        var j = S.jobs.find(function (r) { return r.id === +cpBtn.dataset.txCopy; });
        if (j) copyText(j._uids.join("\n"), "Copied " + j._uids.length + " UID(s)");
        return;
      }
      var iBtn = e.target.closest("[data-tx-inspect]");
      if (iBtn) { openInspector(+iBtn.dataset.txInspect); return; }
      var title = e.target.closest(".transfer-job-title .name, .tx-trow .tx-tpeer");
      if (title) {
        var row = title.closest("[data-job-id]");
        if (row) openInspector(+row.dataset.jobId);
      }
    });

    /* bulk actions */
    $id("tx-bulk").addEventListener("click", async function (e) {
      var b = e.target.closest("[data-tx-bulk]"); if (!b) return;
      var act = b.dataset.txBulk, ids = Array.from(S.sel);
      if (act === "NONE") { S.sel.clear(); renderList(); return; }
      if (act === "EXPORT") { exportJobs("csv", S.jobs.filter(function (j) { return S.sel.has(j.id); })); return; }
      if (act === "CANCEL") {
        var pend = S.jobs.filter(function (j) { return S.sel.has(j.id) && j._status === "PENDING"; });
        if (!pend.length) { notify("No queued jobs in selection", "err"); return; }
        if (!window.confirm("Cancel " + pend.length + " queued job(s)?")) return;
        var okC = 0;
        for (var i = 0; i < pend.length; i++) { if (await cancelJob(pend[i].id, true)) okC++; }
        notify("Cancelled " + okC + " of " + pend.length + " queued jobs", okC ? "ok" : "err");
        S.sel.clear(); await load({ force: true }); return;
      }
      if (act === "RETRY") {
        var retryable = S.jobs.filter(function (j) {
          return S.sel.has(j.id) && (j._status === "FAILED" || j._status === "PARTIAL" || j._status === "CANCELLED") && (j.direction === "SEND" || isAdmin());
        });
        if (!retryable.length) { notify("No retryable failed/cancelled jobs in selection", "err"); return; }
        var okR = 0;
        for (var k = 0; k < retryable.length; k++) { if (await retryJob(retryable[k].id, { quiet: true })) okR++; }
        notify("Re-queued " + okR + " of " + retryable.length + " job(s)", okR ? "ok" : "err");
        S.sel.clear(); await load({ force: true });
      }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && $id("tx-drawer").classList.contains("is-open")) closeDrawer();
    });
  }

  function init() {
    if (!$id("transfer-board")) return;
    if ($id("tx-scrim")) document.body.appendChild($id("tx-scrim"));
    if ($id("tx-drawer")) document.body.appendChild($id("tx-drawer"));
    syncControls();
    bind();
    var view = $id("view-nodes");
    if (view && view.classList.contains("active")) load();
  }

  window.PaxsTransfers = {
    load: load,
    clearHistory: clearHistory,
    stopPoll: stopPoll,
    openComposer: openComposer,
    openInspector: openInspector
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
