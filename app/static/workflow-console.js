/* ============================================================
   PAXS Archive — Workflow console (#workflow)
   ------------------------------------------------------------
   MWL (scheduled procedure steps) → MPPS (performed steps) →
   Storage Commitment, plus:

     · statistical analytics: descriptive statistics with Student-t
       confidence intervals, Wilson score intervals for proportions,
       histograms (Freedman–Diaconis / Sturges / Scott) with a Gaussian
       KDE, Tukey box plot, I-MR control chart with Western Electric
       rules, OLS trend with a prediction band, Kruskal–Wallis test
       between groups, weekday × hour load heatmap, Little's law.
     · a DICOM data-quality audit (VR length limits, Defined Terms,
       duplicates, stale steps, orphan MPPS, unanswered commitments).
     · table / board (drag & drop) / timeline layouts, saved views,
       column chooser, bulk actions, keyboard navigation, exports.

   Depends on globals from app.js (classic script): api, toast, esc,
   state, viewerUrl. Exposes window.PaxsWorkflow = { load }.
   ============================================================ */
(function () {
  "use strict";

  /* ------------------------------------------------------------ basics */
  var $id = function (id) { return document.getElementById(id); };
  var qsa = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var H = function (s) { return (typeof esc === "function") ? esc(s) : String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]; }); };
  var notify = function (msg, kind) { if (typeof toast === "function") toast(msg, kind || ""); };
  var call = function (path, opts) { return api(path, opts || {}); };
  var jsonOpts = function (method, body) { return { method: method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }; };
  var isAdmin = function () { return typeof state !== "undefined" && state.role === "ADMIN"; };
  var canWrite = function () { return typeof state !== "undefined" && (state.role === "ADMIN" || state.role === "TECHNOLOGIST"); };
  var signedIn = function () { return typeof state !== "undefined" && !!state.token; };
  var studyHref = function (uid) { return typeof viewerUrl === "function" ? viewerUrl(uid) : "/viewer?study=" + encodeURIComponent(uid); };

  var MIN = 60000, HOUR = 60 * MIN, DAY = 24 * HOUR;
  var DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  /* DICOM PS3.3 C.7.3.1.1.1 Modality Defined Terms (current + common). */
  var MODALITY_TERMS = ["AR", "ASMT", "AU", "BDUS", "BI", "BMD", "CFM", "CR", "CT", "CTPROTOCOL", "DMS", "DG", "DOC", "DX", "ECG", "EEG", "EMG", "EOG", "EPS", "ES", "FID", "GM", "HC", "HD", "IO", "IOL", "IVOCT", "IVUS", "KER", "KO", "LEN", "LS", "MG", "MR", "M3D", "NM", "OAM", "OCT", "OP", "OPM", "OPT", "OPTBSV", "OPTENF", "OPV", "OSS", "OT", "PA", "PLAN", "POS", "PR", "PT", "PX", "REG", "RESP", "RF", "RG", "RTDOSE", "RTIMAGE", "RTINTENT", "RTPLAN", "RTRAD", "RTRECORD", "RTSEGANN", "RTSTRUCT", "RWV", "SEG", "SM", "SMR", "SR", "SRF", "STAIN", "TEXTUREMAP", "TG", "US", "VA", "XA", "XAPROTOCOL", "XC"];
  var MOD_COLOR = { CT: "#38bdf8", MR: "#a78bfa", CR: "#fbbf24", DX: "#fb923c", US: "#34d399", NM: "#fb7185", PT: "#f472b6", XA: "#22d3ee", MG: "#e879f9", RF: "#facc15", OT: "#94a3b8" };
  var modColor = function (m) { return MOD_COLOR[m] || "#94a3b8"; };
  var STATUS_ORDER = { "IN PROGRESS": 0, "SCHEDULED": 1, "COMPLETED": 2, "CANCELED": 3 };
  var STATUS_LABEL = { "SCHEDULED": "Scheduled", "IN PROGRESS": "In progress", "COMPLETED": "Completed", "CANCELED": "Canceled", "DISCONTINUED": "Discontinued", "PENDING": "Pending", "FAILED": "Failed" };
  var FAIL_REASON = { 272: "0x0110 Processing failure", 274: "0x0112 No such object instance", 290: "0x0122 SOP class not supported", 281: "0x0119 Class/instance conflict", 305: "0x0131 Duplicate transaction UID" };

  /* ------------------------------------------------------------ prefs */
  var PREF_KEY = "paxs_wfx_v2";
  var COLS = [
    { key: "sched", label: "Scheduled", w: "minmax(118px,.9fr)" },
    { key: "patient", label: "Patient", w: "minmax(170px,1.5fr)" },
    { key: "proc", label: "Procedure", w: "minmax(170px,1.6fr)" },
    { key: "acc", label: "Accession", w: "minmax(96px,.8fr)" },
    { key: "mod", label: "Modality", w: "70px" },
    { key: "station", label: "Station", w: "minmax(80px,.7fr)" },
    { key: "phys", label: "Physician", w: "minmax(110px,.9fr)" },
    { key: "link", label: "Chain", w: "92px" },
    { key: "age", label: "Wait / age", w: "minmax(84px,.6fr)" }
  ];
  var DEFAULT_PREFS = {
    tab: "mwl", mode: "table", status: "", mod: "", station: "", source: "", range: "all", from: "", to: "",
    sort: "date-asc", pageSize: 50, group: true, dense: false, auto: 0, q: "",
    cols: { sched: true, patient: true, proc: true, acc: true, mod: true, station: true, phys: false, link: true, age: true },
    mppsStatus: "", mppsLink: "", mppsMod: "", commitStatus: "", commitAe: "",
    s: { window: 30, metric: "duration", group: "modality", bins: "fd", conf: 0.95, sigma: 3, tol: 15, kde: true, outliers: false, log: false, spc: "done", heat: "scheduled" },
    dq: { sev: "", stale: 4, ctimeout: 15, scope: "", muted: [] },
    tlDay: "", views: []
  };
  function loadPrefs() {
    var p = JSON.parse(JSON.stringify(DEFAULT_PREFS));
    try {
      var raw = JSON.parse(localStorage.getItem(PREF_KEY) || "null");
      if (raw && typeof raw === "object") {
        Object.keys(raw).forEach(function (k) {
          if (raw[k] && typeof raw[k] === "object" && !Array.isArray(raw[k]) && p[k] && typeof p[k] === "object" && !Array.isArray(p[k])) Object.assign(p[k], raw[k]);
          else p[k] = raw[k];
        });
      }
    } catch (_) { /* corrupt storage */ }
    return p;
  }
  var P = loadPrefs();
  function savePrefs() { try { localStorage.setItem(PREF_KEY, JSON.stringify(P)); } catch (_) { /* quota */ } }

  /* ------------------------------------------------------------ state */
  var S = {
    mwl: [], mpps: [], commit: [], errors: {}, loadedAt: null, loading: false,
    sel: new Set(), page: 1, timer: null, focusIdx: -1, fieldEditOk: null,
    stats: null, dq: null, drawerFor: null
  };

  /* ------------------------------------------------------------ time */
  function parseDA(s) {
    if (!s) return null;
    var d = String(s).replace(/\D/g, "");
    if (d.length !== 8) return null;
    var y = +d.slice(0, 4), m = +d.slice(4, 6), dd = +d.slice(6, 8);
    var dt = new Date(y, m - 1, dd);
    if (dt.getFullYear() !== y || dt.getMonth() !== m - 1 || dt.getDate() !== dd) return null;
    return dt;
  }
  function parseTM(s) {
    if (s == null || s === "") return null;
    var t = String(s).split(".")[0].replace(/\D/g, "");
    if (!t.length) return null;
    if (t.length % 2) t = "0" + t;
    if (t.length > 6) t = t.slice(0, 6);
    var hh = +t.slice(0, 2), mm = +(t.slice(2, 4) || 0), ss = +(t.slice(4, 6) || 0);
    if (hh > 23 || mm > 59 || ss > 60) return null;
    return hh * 60 + mm + ss / 60;
  }
  function isoDate(s) {
    if (!s) return null;
    var str = String(s);
    if (!/[zZ]|[+-]\d\d:?\d\d$/.test(str)) str += "Z"; // server stores naive UTC
    var d = new Date(str);
    return isNaN(d) ? null : d;
  }
  function combine(da, tm) {
    var d = parseDA(da); if (!d) return null;
    var m = parseTM(tm);
    if (m != null) d = new Date(d.getTime() + m * MIN);
    return d;
  }
  function startOfDay(d) { var x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
  function ymd(d) { return d.getFullYear() + String(d.getMonth() + 1).padStart(2, "0") + String(d.getDate()).padStart(2, "0"); }
  function inputDate(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
  function fmtDA(s) { var d = parseDA(s); return d ? inputDate(d) : "—"; }
  function fmtTM(s) { var m = parseTM(s); if (m == null) return ""; return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(Math.floor(m % 60)).padStart(2, "0"); }
  function fmtDateTime(d) { if (!d) return "—"; return inputDate(d) + " " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); }
  function fmtDur(min) {
    if (min == null || !isFinite(min)) return "—";
    var a = Math.abs(min), sign = min < 0 ? "−" : "";
    if (a < 1) return sign + Math.round(a * 60) + " s";
    if (a < 120) return sign + (a < 10 ? a.toFixed(1) : Math.round(a)) + " min";
    if (a < 2880) return sign + (a / 60).toFixed(1) + " h";
    return sign + (a / 1440).toFixed(1) + " d";
  }
  function fmtRel(d) {
    if (!d) return "";
    var diff = (d.getTime() - Date.now()) / MIN;
    if (Math.abs(diff) < 1) return "now";
    return diff > 0 ? "in " + fmtDur(diff) : fmtDur(-diff) + " ago";
  }
  function fmtNum(x, dp) { if (x == null || !isFinite(x)) return "—"; return Number(x).toFixed(dp == null ? 2 : dp); }
  function fmtPct(x, dp) { if (x == null || !isFinite(x)) return "—"; return (x * 100).toFixed(dp == null ? 1 : dp) + "%"; }
  function fmtP(p) { if (p == null || !isFinite(p)) return "—"; if (p < 0.001) return "< 0.001"; return p.toFixed(3); }

  /* ------------------------------------------------------------ statistics */
  var ST = {
    sum: function (a) { var s = 0; for (var i = 0; i < a.length; i++) s += a[i]; return s; },
    mean: function (a) { return a.length ? ST.sum(a) / a.length : NaN; },
    variance: function (a) { if (a.length < 2) return NaN; var m = ST.mean(a), s = 0; for (var i = 0; i < a.length; i++) s += (a[i] - m) * (a[i] - m); return s / (a.length - 1); },
    sd: function (a) { return Math.sqrt(ST.variance(a)); },
    sorted: function (a) { return a.slice().sort(function (x, y) { return x - y; }); },
    /* Hyndman & Fan type 7 (R default). */
    quantile: function (s, p) {
      if (!s.length) return NaN;
      var h = (s.length - 1) * p, lo = Math.floor(h), hi = Math.ceil(h);
      return s[lo] + (h - lo) * (s[hi] - s[lo]);
    },
    skew: function (a) {
      var n = a.length; if (n < 3) return NaN;
      var m = ST.mean(a), s2 = 0, s3 = 0;
      for (var i = 0; i < n; i++) { var d = a[i] - m; s2 += d * d; s3 += d * d * d; }
      s2 /= n; s3 /= n;
      var g1 = s3 / Math.pow(s2, 1.5);
      return Math.sqrt(n * (n - 1)) / (n - 2) * g1; // adjusted Fisher–Pearson
    },
    kurt: function (a) {
      var n = a.length; if (n < 4) return NaN;
      var m = ST.mean(a), s2 = 0, s4 = 0;
      for (var i = 0; i < n; i++) { var d = a[i] - m; s2 += d * d; s4 += d * d * d * d; }
      var v = s2 / (n - 1);
      return (n * (n + 1) * s4) / ((n - 1) * (n - 2) * (n - 3) * v * v) - 3 * (n - 1) * (n - 1) / ((n - 2) * (n - 3)); // excess
    },
    /* Lanczos log-gamma. */
    lgamma: function (x) {
      var c = [76.18009172947146, -86.50532032941677, 24.01409824083091, -1.231739572450155, 0.1208650973866179e-2, -0.5395239384953e-5];
      var y = x, t = x + 5.5; t -= (x + 0.5) * Math.log(t);
      var ser = 1.000000000190015;
      for (var j = 0; j < 6; j++) ser += c[j] / ++y;
      return -t + Math.log(2.5066282746310005 * ser / x);
    },
    betacf: function (a, b, x) {
      var MAXIT = 200, EPS = 3e-14, FPMIN = 1e-300;
      var qab = a + b, qap = a + 1, qam = a - 1, c = 1, d = 1 - qab * x / qap;
      if (Math.abs(d) < FPMIN) d = FPMIN; d = 1 / d; var h = d;
      for (var m = 1; m <= MAXIT; m++) {
        var m2 = 2 * m, aa = m * (b - m) * x / ((qam + m2) * (a + m2));
        d = 1 + aa * d; if (Math.abs(d) < FPMIN) d = FPMIN; c = 1 + aa / c; if (Math.abs(c) < FPMIN) c = FPMIN; d = 1 / d; h *= d * c;
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2));
        d = 1 + aa * d; if (Math.abs(d) < FPMIN) d = FPMIN; c = 1 + aa / c; if (Math.abs(c) < FPMIN) c = FPMIN; d = 1 / d;
        var del = d * c; h *= del; if (Math.abs(del - 1) < EPS) break;
      }
      return h;
    },
    /* Regularized incomplete beta I_x(a,b). */
    ibeta: function (x, a, b) {
      if (x <= 0) return 0; if (x >= 1) return 1;
      var bt = Math.exp(ST.lgamma(a + b) - ST.lgamma(a) - ST.lgamma(b) + a * Math.log(x) + b * Math.log(1 - x));
      return x < (a + 1) / (a + b + 2) ? bt * ST.betacf(a, b, x) / a : 1 - bt * ST.betacf(b, a, 1 - x) / b;
    },
    /* Student t CDF. */
    tcdf: function (t, df) {
      var x = df / (df + t * t), p = 0.5 * ST.ibeta(x, df / 2, 0.5);
      return t > 0 ? 1 - p : p;
    },
    /* Inverse Student t by bisection on the CDF (monotone, robust). */
    tinv: function (p, df) {
      if (!(df > 0)) return NaN;
      var lo = -1e3, hi = 1e3;
      for (var i = 0; i < 120; i++) { var mid = (lo + hi) / 2; if (ST.tcdf(mid, df) < p) lo = mid; else hi = mid; }
      return (lo + hi) / 2;
    },
    /* Acklam's inverse normal. */
    ninv: function (p) {
      var a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2, 1.383577518672690e2, -3.066479806614716e1, 2.506628277459239];
      var b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1];
      var c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
      var d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416];
      var q, r;
      if (p < 0.02425) { q = Math.sqrt(-2 * Math.log(p)); return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1); }
      if (p > 1 - 0.02425) { q = Math.sqrt(-2 * Math.log(1 - p)); return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1); }
      q = p - 0.5; r = q * q;
      return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
    },
    /* Regularized lower incomplete gamma P(a,x) → chi-square CDF. */
    gammap: function (a, x) {
      if (x <= 0) return 0;
      var gln = ST.lgamma(a), i;
      if (x < a + 1) {
        var ap = a, sum = 1 / a, del = sum;
        for (i = 0; i < 300; i++) { ap++; del *= x / ap; sum += del; if (Math.abs(del) < Math.abs(sum) * 3e-14) break; }
        return sum * Math.exp(-x + a * Math.log(x) - gln);
      }
      var b = x + 1 - a, c = 1 / 1e-300, d = 1 / b, h = d;
      for (i = 1; i < 300; i++) {
        var an = -i * (i - a); b += 2; d = an * d + b; if (Math.abs(d) < 1e-300) d = 1e-300; c = b + an / c; if (Math.abs(c) < 1e-300) c = 1e-300; d = 1 / d; var del2 = d * c; h *= del2; if (Math.abs(del2 - 1) < 3e-14) break;
      }
      return 1 - Math.exp(-x + a * Math.log(x) - gln) * h;
    },
    chi2sf: function (x, k) { return 1 - ST.gammap(k / 2, x / 2); },
    /* Wilson score interval for a binomial proportion. */
    wilson: function (k, n, conf) {
      if (!n) return { p: NaN, lo: NaN, hi: NaN };
      var z = ST.ninv(1 - (1 - conf) / 2), p = k / n, z2 = z * z;
      var den = 1 + z2 / n, centre = (p + z2 / (2 * n)) / den;
      var half = z * Math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / den;
      return { p: p, lo: Math.max(0, centre - half), hi: Math.min(1, centre + half) };
    },
    describe: function (vals, conf) {
      var a = vals.filter(function (v) { return isFinite(v); });
      var n = a.length, s = ST.sorted(a);
      var out = { n: n };
      if (!n) return out;
      out.mean = ST.mean(a); out.sd = ST.sd(a); out.min = s[0]; out.max = s[n - 1];
      out.q1 = ST.quantile(s, 0.25); out.median = ST.quantile(s, 0.5); out.q3 = ST.quantile(s, 0.75);
      out.p90 = ST.quantile(s, 0.9); out.p95 = ST.quantile(s, 0.95); out.iqr = out.q3 - out.q1;
      out.se = n > 1 ? out.sd / Math.sqrt(n) : NaN;
      out.cv = out.mean ? out.sd / Math.abs(out.mean) : NaN;
      out.skew = ST.skew(a); out.kurt = ST.kurt(a);
      var dev = s.map(function (v) { return Math.abs(v - out.median); }).sort(function (x, y) { return x - y; });
      out.mad = 1.4826 * ST.quantile(dev, 0.5);
      if (n > 1) { var t = ST.tinv(1 - (1 - conf) / 2, n - 1); out.ciLo = out.mean - t * out.se; out.ciHi = out.mean + t * out.se; out.tcrit = t; }
      out.fenceLo = out.q1 - 1.5 * out.iqr; out.fenceHi = out.q3 + 1.5 * out.iqr;
      out.outliers = s.filter(function (v) { return v < out.fenceLo || v > out.fenceHi; }).length;
      return out;
    },
    /* Ordinary least squares y = a + b x with inference. */
    ols: function (xs, ys, conf) {
      var n = xs.length; if (n < 3) return null;
      var mx = ST.mean(xs), my = ST.mean(ys), sxx = 0, sxy = 0, syy = 0;
      for (var i = 0; i < n; i++) { sxx += (xs[i] - mx) * (xs[i] - mx); sxy += (xs[i] - mx) * (ys[i] - my); syy += (ys[i] - my) * (ys[i] - my); }
      if (!sxx) return null;
      var b = sxy / sxx, a = my - b * mx, sse = 0;
      for (var j = 0; j < n; j++) { var r = ys[j] - (a + b * xs[j]); sse += r * r; }
      var df = n - 2, s = Math.sqrt(sse / df), seb = s / Math.sqrt(sxx);
      var tstat = seb ? b / seb : (b ? Infinity : 0), p = seb ? 2 * (1 - ST.tcdf(Math.abs(tstat), df)) : (b ? 0 : 1);
      var r2 = syy ? 1 - sse / syy : 0, tc = ST.tinv(1 - (1 - conf) / 2, df);
      return {
        a: a, b: b, r2: r2, s: s, seb: seb, t: tstat, p: p, df: df, n: n,
        bLo: b - tc * seb, bHi: b + tc * seb,
        predict: function (x) { var y = a + b * x, half = tc * s * Math.sqrt(1 + 1 / n + (x - mx) * (x - mx) / sxx); return { y: y, lo: y - half, hi: y + half }; }
      };
    },
    /* Kruskal–Wallis H with tie correction; p from χ²(k−1). */
    kruskal: function (groups) {
      var gs = groups.filter(function (g) { return g.length > 0; });
      if (gs.length < 2) return null;
      var all = [];
      gs.forEach(function (g, gi) { g.forEach(function (v) { all.push({ v: v, g: gi }); }); });
      var N = all.length; if (N < 3) return null;
      all.sort(function (x, y) { return x.v - y.v; });
      var ranks = new Array(N), tie = 0;
      for (var i = 0; i < N;) {
        var j = i; while (j + 1 < N && all[j + 1].v === all[i].v) j++;
        var r = (i + j) / 2 + 1, t = j - i + 1;
        for (var k = i; k <= j; k++) ranks[k] = r;
        if (t > 1) tie += t * t * t - t;
        i = j + 1;
      }
      var R = gs.map(function () { return 0; });
      all.forEach(function (o, idx) { R[o.g] += ranks[idx]; });
      var Hs = 0; gs.forEach(function (g, gi) { Hs += R[gi] * R[gi] / g.length; });
      Hs = 12 / (N * (N + 1)) * Hs - 3 * (N + 1);
      var C = 1 - tie / (N * N * N - N); if (C > 0) Hs /= C;
      var df = gs.length - 1;
      return { H: Hs, df: df, p: ST.chi2sf(Hs, df), N: N, eps2: (Hs - df + 1) / (N - df) };
    },
    binWidth: function (s, rule) {
      var n = s.length, range = s[n - 1] - s[0];
      if (!range) return 1;
      var k;
      if (rule === "sturges") k = Math.ceil(Math.log2(n)) + 1;
      else if (rule === "scott") { var sd = ST.sd(s); k = sd ? Math.ceil(range / (3.49 * sd * Math.pow(n, -1 / 3))) : 10; }
      else if (rule === "fd") { var iqr = ST.quantile(s, 0.75) - ST.quantile(s, 0.25); k = iqr ? Math.ceil(range / (2 * iqr * Math.pow(n, -1 / 3))) : Math.ceil(Math.log2(n)) + 1; }
      else k = +rule || 10;
      k = Math.max(4, Math.min(60, k));
      return range / k;
    },
    kde: function (s, x) {
      var n = s.length, sd = ST.sd(s), iqr = ST.quantile(s, 0.75) - ST.quantile(s, 0.25);
      var sig = Math.min(sd || Infinity, iqr ? iqr / 1.34 : Infinity);
      if (!isFinite(sig) || !sig) sig = sd || 1;
      var h = 0.9 * sig * Math.pow(n, -0.2), c = 1 / (n * h * Math.sqrt(2 * Math.PI));
      return { h: h, y: x.map(function (xi) { var t = 0; for (var i = 0; i < n; i++) { var u = (xi - s[i]) / h; t += Math.exp(-0.5 * u * u); } return t * c; }) };
    }
  };
  window.PaxsStats = ST; // handy for console inspection / tests

  /* ------------------------------------------------------------ data */
  async function fetchList(path) {
    try {
      var res = await call(path);
      if (!res.ok) return { error: res.status === 401 ? "Session expired — sign in again." : res.status === 403 ? "Your account cannot view this list." : "Server error (HTTP " + res.status + ")." };
      var data = await res.json();
      return { data: Array.isArray(data) ? data : [] };
    } catch (_) { return { error: "Could not reach the server." }; }
  }

  async function load() {
    if (!signedIn()) return;
    if (S.loading) return;
    S.loading = true;
    var btn = $id("wfx-refresh"); if (btn) btn.classList.add("is-busy");
    var r = await Promise.all([fetchList("/api/v1/worklist"), fetchList("/api/v1/mpps"), fetchList("/api/v1/commitments")]);
    S.loading = false;
    if (btn) btn.classList.remove("is-busy");
    S.errors = { mwl: r[0].error, mpps: r[1].error, commit: r[2].error };
    if (r[0].data) S.mwl = r[0].data;
    if (r[1].data) S.mpps = r[1].data;
    if (r[2].data) S.commit = r[2].data;
    S.loadedAt = new Date();
    derive();
    renderAll();
  }

  /* Attach parsed timestamps and the MWL ↔ MPPS chain to every record. */
  function derive() {
    var now = Date.now(), today0 = startOfDay(new Date()).getTime();
    var bySps = {}, byAcc = {};
    S.mpps.forEach(function (p) {
      p._start = combine(p.start_date, p.start_time);
      p._end = combine(p.end_date, p.end_time);
      p._created = isoDate(p.created_at);
      p._updated = isoDate(p.updated_at);
      p._dur = (p._start && p._end && p.status === "COMPLETED") ? (p._end - p._start) / MIN : null;
      if (p._dur != null && (p._dur < 0 || p._dur > 1440)) p._durBad = true;
      p._step = null;
      if (p.scheduled_step_id) (bySps[p.scheduled_step_id] = bySps[p.scheduled_step_id] || []).push(p);
      if (p.accession_number) (byAcc[p.accession_number] = byAcc[p.accession_number] || []).push(p);
    });
    var accCount = {};
    S.mwl.forEach(function (s) { if (s.accession_number) accCount[s.accession_number] = (accCount[s.accession_number] || 0) + 1; });
    S.mwl.forEach(function (s) {
      s._sched = combine(s.start_date, s.start_time);
      s._hasTime = parseTM(s.start_time) != null;
      s._created = isoDate(s.created_at);
      s._updated = isoDate(s.updated_at);
      s._performed = isoDate(s.performed_at);
      var linked = (s.sps_id && bySps[s.sps_id]) || (s.accession_number && byAcc[s.accession_number]) || [];
      s._mpps = linked;
      linked.forEach(function (p) { p._step = s; });
      var first = linked.reduce(function (acc, p) { return p._start && (!acc || p._start < acc) ? p._start : acc; }, null);
      s._actualStart = first || (s.status === "IN PROGRESS" ? s._updated : null) || (s.status === "COMPLETED" ? s._performed : null);
      s._dupAcc = s.accession_number && accCount[s.accession_number] > 1;
      var due = s._sched ? (s._hasTime ? s._sched.getTime() : s._sched.getTime() + DAY) : null;
      s._overdue = s.status === "SCHEDULED" && due != null && due < now;
      s._wait = s._overdue ? (now - (s._hasTime ? s._sched.getTime() : s._sched.getTime())) / MIN : null;
      s._isToday = s._sched && startOfDay(s._sched).getTime() === today0;
      s._doneToday = s.status === "COMPLETED" && s._performed && startOfDay(s._performed).getTime() === today0;
      s._delay = (s._hasTime && s._actualStart) ? (s._actualStart - s._sched) / MIN : null;
      s._lead = (s._sched && s._created) ? (s._sched - s._created) / MIN : null;
    });
    S.commit.forEach(function (c) {
      c._created = isoDate(c.created_at);
      c._sent = isoDate(c.sent_at);
      c._lat = (c._created && c._sent) ? (c._sent - c._created) / MIN : null;
    });
    S.stats = computeStats();
    S.dq = audit();
  }

  /* Metric samples, each { v (minutes), t (Date), g (group), ref }. */
  function samples(metric, groupBy) {
    var out = [], gkey = function (r, st) { return groupBy === "station" ? (st || "—") : (r.modality || "—"); };
    if (metric === "duration") S.mpps.forEach(function (p) { if (p._dur != null && !p._durBad) out.push({ v: p._dur, t: p._start, g: gkey(p, p.performed_station_aet), ref: p, kind: "mpps" }); });
    else if (metric === "delay") S.mwl.forEach(function (s) { if (s._delay != null && Math.abs(s._delay) < 7 * 1440) out.push({ v: s._delay, t: s._sched, g: gkey(s, s.scheduled_station_aet), ref: s, kind: "mwl" }); });
    else if (metric === "lead") S.mwl.forEach(function (s) { if (s._lead != null && s._lead > -1440) out.push({ v: s._lead, t: s._created, g: gkey(s, s.scheduled_station_aet), ref: s, kind: "mwl" }); });
    else if (metric === "commit") S.commit.forEach(function (c) { if (c._lat != null && c._lat >= 0) out.push({ v: c._lat, t: c._created, g: c.ae_title || "—", ref: c, kind: "commit" }); });
    return out;
  }
  function inWindow(d) {
    if (!P.s.window) return true;
    if (!d) return false;
    return d.getTime() >= Date.now() - P.s.window * DAY;
  }
  var METRIC_LABEL = { duration: "Exam duration", delay: "Start delay", lead: "Booking lead time", commit: "Commit latency" };

  /* Daily series for the control chart / regression. */
  function dailySeries(kind) {
    var counts = {}, earliest = null;
    function add(d) { if (!d) return; var k = ymd(d); counts[k] = (counts[k] || 0) + 1; if (!earliest || d < earliest) earliest = d; }
    if (kind === "done") {
      S.mwl.forEach(function (s) { if (s.status === "COMPLETED") add(s._performed || s._actualStart); });
      S.mpps.forEach(function (p) { if (p.status === "COMPLETED" && !p._step) add(p._end || p._start); });
    } else if (kind === "orders") S.mwl.forEach(function (s) { add(s._created); });
    else if (kind === "cancel") S.mwl.forEach(function (s) { if (s.status === "CANCELED") add(s._updated); });
    else if (kind === "mpps") S.mpps.forEach(function (p) { add(p._start || p._created); });
    var end = startOfDay(new Date()), days = P.s.window || 0;
    if (!days) days = earliest ? Math.min(365, Math.round((end - startOfDay(earliest)) / DAY) + 1) : 14;
    days = Math.max(days, 3);
    var out = [];
    for (var i = days - 1; i >= 0; i--) { var d = new Date(end.getTime() - i * DAY); d = startOfDay(new Date(d.getTime() + 2 * HOUR)); out.push({ d: d, y: counts[ymd(d)] || 0 }); }
    return out;
  }
  var SPC_LABEL = { done: "Completed exams / day", orders: "Orders created / day", mpps: "MPPS started / day", cancel: "Cancellations / day" };

  /* I-MR individuals chart with Western Electric / Nelson rules. */
  function controlChart(series, k) {
    var y = series.map(function (p) { return p.y; }), n = y.length;
    var cl = ST.mean(y), mr = [];
    for (var i = 1; i < n; i++) mr.push(Math.abs(y[i] - y[i - 1]));
    var mrbar = mr.length ? ST.mean(mr) : 0, sigma = mrbar / 1.128;
    var ucl = cl + k * sigma, lcl = Math.max(0, cl - k * sigma);
    var flags = series.map(function () { return []; });
    var side = function (v) { return v > cl ? 1 : v < cl ? -1 : 0; };
    for (var j = 0; j < n; j++) {
      if (sigma && (y[j] > ucl || y[j] < cl - k * sigma)) flags[j].push("R1");
      if (j >= 8) { var s0 = side(y[j]); if (s0 && y.slice(j - 8, j + 1).every(function (v) { return side(v) === s0; })) flags[j].push("R2"); }
      if (j >= 5) {
        var w = y.slice(j - 5, j + 1), up = true, dn = true;
        for (var a = 1; a < w.length; a++) { if (!(w[a] > w[a - 1])) up = false; if (!(w[a] < w[a - 1])) dn = false; }
        if (up || dn) flags[j].push("R3");
      }
      if (j >= 2 && sigma) {
        var w3 = y.slice(j - 2, j + 1);
        if (w3.filter(function (v) { return v > cl + 2 * sigma; }).length >= 2 || w3.filter(function (v) { return v < cl - 2 * sigma; }).length >= 2) flags[j].push("R4");
      }
      if (j >= 4 && sigma) {
        var w5 = y.slice(j - 4, j + 1);
        if (w5.filter(function (v) { return v > cl + sigma; }).length >= 4 || w5.filter(function (v) { return v < cl - sigma; }).length >= 4) flags[j].push("R5");
      }
    }
    return { cl: cl, ucl: ucl, lcl: lcl, sigma: sigma, mrbar: mrbar, flags: flags };
  }
  var RULE_TEXT = {
    R1: "Point beyond the control limit",
    R2: "9 consecutive points on one side of the centre line (shift)",
    R3: "6 consecutive points steadily rising or falling (trend)",
    R4: "2 of 3 points beyond 2σ on the same side",
    R5: "4 of 5 points beyond 1σ on the same side"
  };

  function computeStats() {
    var conf = +P.s.conf, out = {};
    var all = samples(P.s.metric, P.s.group).filter(function (s) { return inWindow(s.t); });
    var d0 = ST.describe(all.map(function (s) { return s.v; }), conf);
    var kept = all;
    if (P.s.outliers && d0.n >= 4) kept = all.filter(function (s) { return s.v >= d0.fenceLo && s.v <= d0.fenceHi; });
    out.samples = kept; out.raw = all;
    out.desc = P.s.outliers ? ST.describe(kept.map(function (s) { return s.v; }), conf) : d0;
    out.removed = all.length - kept.length;
    /* groups */
    var groups = {};
    kept.forEach(function (s) { (groups[s.g] = groups[s.g] || []).push(s.v); });
    out.groups = groups;
    out.kw = ST.kruskal(Object.keys(groups).map(function (g) { return groups[g]; }));
    /* duration, on-time, commit KPIs (always over the window) */
    var dur = samples("duration").filter(function (s) { return inWindow(s.t); }).map(function (s) { return s.v; });
    out.dur = ST.describe(dur, conf);
    var del = samples("delay").filter(function (s) { return inWindow(s.t); });
    var onTime = del.filter(function (s) { return Math.abs(s.v) <= P.s.tol; }).length;
    out.onTime = ST.wilson(onTime, del.length, conf); out.onTime.k = onTime; out.onTime.n = del.length;
    var cm = S.commit.filter(function (c) { return inWindow(c._created); });
    var cmDone = cm.filter(function (c) { return c.status === "COMPLETED"; }).length, cmFail = cm.filter(function (c) { return c.status === "FAILED"; }).length;
    out.commitOk = ST.wilson(cmDone, cmDone + cmFail, conf); out.commitOk.k = cmDone; out.commitOk.n = cmDone + cmFail;
    out.commitPending = cm.filter(function (c) { return c.status === "PENDING"; }).length;
    /* funnel over the window, by order creation (fallback: scheduled date) */
    var orders = S.mwl.filter(function (s) { return inWindow(s._created || s._sched); });
    var nOrd = orders.length;
    var nCx = orders.filter(function (s) { return s.status === "CANCELED"; }).length;
    var nStart = orders.filter(function (s) { return s.status === "IN PROGRESS" || s.status === "COMPLETED"; }).length;
    var nDone = orders.filter(function (s) { return s.status === "COMPLETED"; }).length;
    var nStudy = orders.filter(function (s) { return s.status === "COMPLETED" && s.study_instance_uid; }).length;
    out.funnel = {
      ordered: nOrd, canceled: nCx, started: nStart, completed: nDone, reconciled: nStudy, committed: cmDone, commitTotal: cmDone + cmFail,
      startRate: ST.wilson(nStart, nOrd - nCx, conf), doneRate: ST.wilson(nDone, nStart, conf), cxRate: ST.wilson(nCx, nOrd, conf),
      studyRate: ST.wilson(nStudy, nDone, conf)
    };
    /* series */
    out.series = dailySeries(P.s.spc);
    out.spc = controlChart(out.series, +P.s.sigma);
    out.ols = ST.ols(out.series.map(function (_, i) { return i; }), out.series.map(function (p) { return p.y; }), conf);
    out.doneSeries = P.s.spc === "done" ? out.series : dailySeries("done");
    /* Little's law */
    var days = P.s.window || Math.max(1, out.series.length);
    var lambda = nDone / days, arrivals = nOrd / days;
    var wip = S.mwl.filter(function (s) { return s.status === "IN PROGRESS" || (s.status === "SCHEDULED" && s._overdue); }).length;
    var cyc = S.mwl.filter(function (s) { return s.status === "COMPLETED" && s._created && s._performed && inWindow(s._performed); }).map(function (s) { return (s._performed - s._created) / MIN; }).filter(function (v) { return v >= 0; });
    out.little = { lambda: lambda, arrivals: arrivals, wip: wip, implied: lambda ? wip / lambda * 1440 : null, measured: cyc.length ? ST.describe(cyc, conf) : null, balance: arrivals ? lambda / arrivals : null };
    /* heatmap */
    var heat = []; for (var r = 0; r < 7; r++) { heat.push(new Array(24).fill(0)); }
    var hmax = 0;
    var addHeat = function (d) { if (!d || !inWindowAny(d)) return; var dow = (d.getDay() + 6) % 7, h = d.getHours(); heat[dow][h]++; if (heat[dow][h] > hmax) hmax = heat[dow][h]; };
    if (P.s.heat === "performed") { S.mpps.forEach(function (p) { addHeat(p._start); }); }
    else S.mwl.forEach(function (s) { if (s._hasTime) addHeat(s._sched); });
    out.heat = heat; out.heatMax = hmax;
    return out;
  }
  /* The heatmap also looks forward for scheduled load. */
  function inWindowAny(d) { if (!P.s.window) return true; return Math.abs(d.getTime() - Date.now()) <= P.s.window * DAY; }

  /* ------------------------------------------------------------ data-quality audit */
  var AE_BAD = /[\\\x00-\x1f]/;
  var RULES = [
    { id: "acc-missing", scope: "mwl", sev: "warn", tag: "(0008,0050)", title: "Accession number missing", hint: "Reports and images cannot be reconciled to the order without an accession." },
    { id: "acc-vr", scope: "mwl", sev: "error", tag: "(0008,0050)", title: "Accession number exceeds SH (16 chars)", hint: "Modalities may truncate the value and break matching." },
    { id: "acc-dup", scope: "mwl", sev: "error", tag: "(0008,0050)", title: "Duplicate accession number", hint: "Two orders share an accession; images may attach to the wrong order." },
    { id: "aet-missing", scope: "mwl", sev: "warn", tag: "(0040,0001)", title: "Scheduled Station AE Title missing", hint: "Every modality that queries the worklist will receive this order." },
    { id: "aet-vr", scope: "mwl", sev: "error", tag: "(0040,0001)", title: "Station AE Title violates AE VR", hint: "AE values are ≤ 16 characters with no backslash or control characters." },
    { id: "mod-missing", scope: "mwl", sev: "warn", tag: "(0008,0060)", title: "Modality missing", hint: "Modality-filtered C-FIND queries will not match this order." },
    { id: "mod-term", scope: "mwl", sev: "warn", tag: "(0008,0060)", title: "Modality is not a DICOM Defined Term", hint: "Use a PS3.3 Defined Term such as CT, MR, US, DX." },
    { id: "date-missing", scope: "mwl", sev: "error", tag: "(0040,0002)", title: "SPS start date missing", hint: "Date-filtered worklist queries will never return this order." },
    { id: "date-vr", scope: "mwl", sev: "error", tag: "(0040,0002)", title: "SPS start date is not a valid DA (YYYYMMDD)", hint: "" },
    { id: "time-vr", scope: "mwl", sev: "warn", tag: "(0040,0003)", title: "SPS start time is not a valid TM", hint: "" },
    { id: "time-missing", scope: "mwl", sev: "info", tag: "(0040,0003)", title: "SPS start time not set", hint: "Punctuality (on-time start) cannot be measured for this order." },
    { id: "pn-format", scope: "mwl", sev: "info", tag: "(0010,0010)", title: "Patient name has no PN component separator (^)", hint: "Expected Family^Given^Middle^Prefix^Suffix." },
    { id: "pn-vr", scope: "mwl", sev: "error", tag: "(0010,0010)", title: "Patient name component group exceeds 64 chars", hint: "" },
    { id: "pid-vr", scope: "mwl", sev: "error", tag: "(0010,0020)", title: "Patient ID exceeds LO (64 chars)", hint: "" },
    { id: "desc-vr", scope: "mwl", sev: "error", tag: "(0032,1060)", title: "Requested procedure description exceeds LO (64 chars)", hint: "" },
    { id: "rpid-vr", scope: "mwl", sev: "error", tag: "(0040,1001)", title: "Requested procedure ID exceeds SH (16 chars)", hint: "" },
    { id: "overdue", scope: "mwl", sev: "warn", tag: "(0040,0020)", title: "Scheduled step overdue beyond threshold", hint: "Start, reschedule or cancel it so the modality worklist stays truthful." },
    { id: "stale-ip", scope: "mwl", sev: "warn", tag: "(0040,0020)", title: "Step IN PROGRESS beyond threshold", hint: "The modality probably never sent MPPS COMPLETED." },
    { id: "done-nostudy", scope: "mwl", sev: "info", tag: "(0020,000D)", title: "Completed order has no linked study", hint: "Images were not reconciled to this order (yet)." },
    { id: "mpps-orphan", scope: "mpps", sev: "info", tag: "(0040,0270)", title: "Unscheduled acquisition (MPPS not linked to an order)", hint: "Performed without a worklist entry — check patient identity." },
    { id: "mpps-stale", scope: "mpps", sev: "warn", tag: "(0040,0252)", title: "MPPS IN PROGRESS beyond threshold", hint: "No N-SET COMPLETED/DISCONTINUED received." },
    { id: "mpps-nostudy", scope: "mpps", sev: "warn", tag: "(0020,000D)", title: "Completed MPPS without Study Instance UID", hint: "" },
    { id: "mpps-neg", scope: "mpps", sev: "error", tag: "(0040,0250)", title: "MPPS end precedes start, or lasts over 24 h", hint: "Modality clock or N-SET timestamps are wrong." },
    { id: "cm-failed", scope: "commit", sev: "error", tag: "(0008,1198)", title: "Storage commitment failed", hint: "Referenced instances are missing — re-send from the modality before it deletes them." },
    { id: "cm-timeout", scope: "commit", sev: "warn", tag: "(0008,1195)", title: "Commitment pending beyond timeout", hint: "The N-EVENT-REPORT has not been delivered." }
  ];
  var RULE_BY_ID = {}; RULES.forEach(function (r) { RULE_BY_ID[r.id] = r; });
  var SEV_W = { error: 1, warn: 0.4, info: 0.1 };

  function audit() {
    var now = Date.now(), stale = (+P.dq.stale || 4) * HOUR, ctimeout = (+P.dq.ctimeout || 15) * MIN;
    var issues = [];
    var add = function (rule, rec, kind, value) { issues.push({ rule: RULE_BY_ID[rule], rec: rec, kind: kind, value: value == null ? "" : String(value) }); };
    S.mwl.forEach(function (s) {
      if (s.status === "CANCELED") return;
      var acc = s.accession_number || "";
      if (!acc) add("acc-missing", s, "mwl"); else if (acc.length > 16) add("acc-vr", s, "mwl", acc);
      if (s._dupAcc) add("acc-dup", s, "mwl", acc);
      var aet = s.scheduled_station_aet || "";
      if (!aet) add("aet-missing", s, "mwl"); else if (aet.length > 16 || AE_BAD.test(aet) || !aet.trim()) add("aet-vr", s, "mwl", aet);
      var mod = (s.modality || "").toUpperCase();
      if (!mod) add("mod-missing", s, "mwl"); else if (MODALITY_TERMS.indexOf(mod) < 0) add("mod-term", s, "mwl", mod);
      if (!s.start_date) add("date-missing", s, "mwl"); else if (!parseDA(s.start_date)) add("date-vr", s, "mwl", s.start_date);
      if (s.start_time && parseTM(s.start_time) == null) add("time-vr", s, "mwl", s.start_time);
      else if (!s.start_time && s.status === "SCHEDULED") add("time-missing", s, "mwl");
      var pn = s.patient_name || "";
      if (pn && pn.indexOf("^") < 0) add("pn-format", s, "mwl", pn);
      if (pn.split("=").some(function (g) { return g.length > 64; })) add("pn-vr", s, "mwl", pn);
      if ((s.patient_id || "").length > 64) add("pid-vr", s, "mwl", s.patient_id);
      if ((s.requested_procedure_description || "").length > 64) add("desc-vr", s, "mwl", s.requested_procedure_description);
      if ((s.requested_procedure_id || "").length > 16) add("rpid-vr", s, "mwl", s.requested_procedure_id);
      if (s._overdue && s._wait * MIN > stale) add("overdue", s, "mwl", fmtDur(s._wait) + " late");
      if (s.status === "IN PROGRESS") { var since = s._actualStart || s._updated; if (since && now - since > stale) add("stale-ip", s, "mwl", fmtDur((now - since) / MIN)); }
      if (s.status === "COMPLETED" && !s.study_instance_uid) add("done-nostudy", s, "mwl");
    });
    S.mpps.forEach(function (p) {
      if (!p._step && !p.scheduled_step_id) add("mpps-orphan", p, "mpps", p.performed_station_aet || "");
      var since = p._start || p._created;
      if (p.status === "IN PROGRESS" && since && now - since > stale) add("mpps-stale", p, "mpps", fmtDur((now - since) / MIN));
      if (p.status === "COMPLETED" && !p.study_instance_uid) add("mpps-nostudy", p, "mpps");
      if (p._durBad) add("mpps-neg", p, "mpps", fmtDur(p._dur));
    });
    S.commit.forEach(function (c) {
      if (c.status === "FAILED") add("cm-failed", c, "commit", (c.instance_count - c.verified_count) + " of " + c.instance_count + " missing");
      if (c.status === "PENDING" && c._created && now - c._created > ctimeout) add("cm-timeout", c, "commit", fmtDur((now - c._created) / MIN));
    });
    var muted = P.dq.muted || [];
    var live = issues.filter(function (i) { return muted.indexOf(i.rule.id) < 0; });
    /* per-record penalty, capped at 1 → score = mean(1 − penalty) */
    var pen = new Map();
    live.forEach(function (i) { pen.set(i.rec, Math.min(1, (pen.get(i.rec) || 0) + SEV_W[i.rule.sev])); });
    var scopes = { mwl: S.mwl.filter(function (s) { return s.status !== "CANCELED"; }), mpps: S.mpps, commit: S.commit };
    var scoreOf = function (recs) { if (!recs.length) return null; var t = 0; recs.forEach(function (r) { t += 1 - (pen.get(r) || 0); }); return t / recs.length; };
    var allRecs = scopes.mwl.concat(scopes.mpps, scopes.commit);
    var counts = { error: 0, warn: 0, info: 0 };
    live.forEach(function (i) { counts[i.rule.sev]++; });
    var byRule = {}; issues.forEach(function (i) { byRule[i.rule.id] = (byRule[i.rule.id] || 0) + 1; });
    return {
      issues: issues, live: live, counts: counts, byRule: byRule, records: allRecs.length,
      score: scoreOf(allRecs), scoreMwl: scoreOf(scopes.mwl), scoreMpps: scoreOf(scopes.mpps), scoreCommit: scoreOf(scopes.commit),
      clean: allRecs.filter(function (r) { return !pen.get(r); }).length
    };
  }

  /* ------------------------------------------------------------ svg helpers */
  function niceStep(range, count) {
    var raw = range / Math.max(1, count), mag = Math.pow(10, Math.floor(Math.log10(raw || 1))), f = raw / mag;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * mag;
  }
  function ticks(min, max, count) {
    if (!(max > min)) { max = min + 1; }
    var step = niceStep(max - min, count || 5), t0 = Math.ceil(min / step) * step, out = [];
    for (var v = t0; v <= max + step * 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }
  function scale(d0, d1, r0, r1) { var k = (r1 - r0) / ((d1 - d0) || 1); return function (v) { return r0 + (v - d0) * k; }; }
  function svgOpen(w, h, label) { return '<svg class="wfx-svg" viewBox="0 0 ' + w + " " + h + '" role="img" aria-label="' + H(label) + '" preserveAspectRatio="xMidYMid meet">'; }
  function axisY(ys, y, x0, x1, fmt) {
    return ys.map(function (v) {
      var yy = y(v).toFixed(1);
      return '<line class="wfx-grid" x1="' + x0 + '" x2="' + x1 + '" y1="' + yy + '" y2="' + yy + '"/><text class="wfx-tick" x="' + (x0 - 6) + '" y="' + yy + '" text-anchor="end" dominant-baseline="middle">' + H(fmt ? fmt(v) : v) + "</text>";
    }).join("");
  }
  function axisX(xs, x, yBase, fmt) {
    return xs.map(function (v) { var xx = x(v).toFixed(1); return '<line class="wfx-axis" x1="' + xx + '" x2="' + xx + '" y1="' + yBase + '" y2="' + (yBase + 4) + '"/><text class="wfx-tick" x="' + xx + '" y="' + (yBase + 15) + '" text-anchor="middle">' + H(fmt ? fmt(v) : v) + "</text>"; }).join("");
  }
  function sparkline(vals, w, h) {
    if (!vals || vals.length < 2) return "";
    var mx = Math.max.apply(null, vals) || 1, x = scale(0, vals.length - 1, 1, w - 1), y = scale(0, mx, h - 2, 2);
    var d = vals.map(function (v, i) { return (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1); }).join(" ");
    return '<svg class="wfx-spark" viewBox="0 0 ' + w + " " + h + '" aria-hidden="true"><path d="' + d + " L" + (w - 1) + " " + h + " L1 " + h + ' Z" class="wfx-spark-fill"/><path d="' + d + '" class="wfx-spark-line"/></svg>';
  }
  function ciBar(est, lo, hi) {
    if (!isFinite(est)) return '<span class="wfx-cibar is-empty"></span>';
    return '<span class="wfx-cibar" title="' + fmtPct(est) + " (CI " + fmtPct(lo) + " – " + fmtPct(hi) + ')"><i class="wfx-ci-range" style="left:' + (lo * 100).toFixed(1) + "%;width:" + Math.max(0.6, (hi - lo) * 100).toFixed(1) + '%"></i><i class="wfx-ci-pt" style="left:' + (est * 100).toFixed(1) + '%"></i></span>';
  }

  /* ------------------------------------------------------------ header / KPIs / pipeline */
  function setKpi(key, value, sub, spark, tone) {
    var el = document.querySelector('.wfx-kpi[data-kpi="' + key + '"]'); if (!el) return;
    el.querySelector(".wfx-kpi-n").textContent = value;
    el.querySelector(".wfx-kpi-s").innerHTML = (sub || "") + (spark || "");
    if (tone) el.dataset.tone = tone;
  }
  function renderKpis() {
    var st = S.stats, mwl = S.mwl;
    var today = mwl.filter(function (s) { return s._isToday && s.status !== "CANCELED"; });
    var remaining = today.filter(function (s) { return s.status === "SCHEDULED"; }).length;
    setKpi("today", String(today.length), remaining + " still to start");
    var od = mwl.filter(function (s) { return s._overdue; });
    var oldest = od.reduce(function (m, s) { return Math.max(m, s._wait || 0); }, 0);
    setKpi("overdue", String(od.length), od.length ? "oldest " + fmtDur(oldest) + " late" : "nothing late", "", od.length ? "danger" : "ok");
    var ip = mwl.filter(function (s) { return s.status === "IN PROGRESS"; }).length;
    var acq = S.mpps.filter(function (p) { return p.status === "IN PROGRESS"; }).length;
    setKpi("progress", String(ip), acq + " MPPS acquiring");
    var ds = st.doneSeries.map(function (p) { return p.y; });
    var last7 = ds.slice(-8, -1), base = last7.length ? ST.mean(last7) : NaN;
    var dt = mwl.filter(function (s) { return s._doneToday; }).length;
    var delta = isFinite(base) && base > 0 ? (dt - base) / base : null;
    setKpi("donetoday", String(dt), delta == null ? "7d mean " + fmtNum(base, 1) : (delta >= 0 ? "▲ " : "▼ ") + Math.abs(delta * 100).toFixed(0) + "% vs 7d", sparkline(ds.slice(-14), 64, 18));
    var d = st.dur;
    setKpi("tat", d.n ? fmtDur(d.median) : "—", d.n ? "P90 " + fmtDur(d.p90) + " · n=" + d.n : "no completed MPPS");
    var ot = st.onTime;
    setKpi("ontime", ot.n ? fmtPct(ot.p, 0) : "—", ot.n ? "CI " + fmtPct(ot.lo, 0) + "–" + fmtPct(ot.hi, 0) + " · ±" + P.s.tol + " min" : "needs timed orders", "", !ot.n ? "info" : ot.p >= 0.8 ? "ok" : ot.p >= 0.6 ? "warn" : "danger");
    var cm = st.commitOk;
    setKpi("pending", cm.n ? fmtPct(cm.p, 1) : "—", (cm.n ? "CI " + fmtPct(cm.lo, 0) + "–" + fmtPct(cm.hi, 0) + " · " : "") + st.commitPending + " pending", "", !cm.n ? "info" : cm.p >= 0.99 ? "ok" : cm.p >= 0.95 ? "warn" : "danger");
    var q = S.dq;
    setKpi("quality", q.score == null ? "—" : Math.round(q.score * 100) + "", q.counts.error + " errors · " + q.counts.warn + " warnings", "", q.score == null ? "info" : q.score >= 0.9 ? "ok" : q.score >= 0.75 ? "warn" : "danger");
    /* tab badges */
    var tn = function (k, v) { var el = document.querySelector('[data-tabn="' + k + '"]'); if (el) el.textContent = v; };
    tn("mwl", String(mwl.filter(function (s) { return s.status === "SCHEDULED" || s.status === "IN PROGRESS"; }).length));
    tn("mpps", String(acq || S.mpps.length));
    tn("commit", String(st.commitPending + S.commit.filter(function (c) { return c.status === "FAILED"; }).length));
    tn("dq", String(q.counts.error + q.counts.warn));
    var dqTab = document.querySelector('.wfx-tab[data-tab="dq"]'); if (dqTab) dqTab.classList.toggle("has-alert", q.counts.error > 0);
  }

  function renderPipeline() {
    var el = $id("wfx-pipeline"); if (!el) return;
    var f = S.stats.funnel, conf = Math.round(P.s.conf * 100);
    var win = P.s.window ? "last " + P.s.window + " d" : "all data";
    var stage = function (key, label, n, sub, tone) {
      return '<div class="wfx-stage" data-stage="' + key + '" data-tone="' + tone + '"><span class="wfx-stage-l">' + label + '</span><b class="wfx-stage-n">' + n + '</b><span class="wfx-stage-s">' + sub + "</span></div>";
    };
    var arrow = function (rate, label) {
      return '<div class="wfx-conv" title="' + H(label) + " — " + conf + '% Wilson interval"><span class="wfx-conv-p">' + (isFinite(rate.p) ? fmtPct(rate.p, 0) : "—") + "</span>" + ciBar(rate.p, rate.lo, rate.hi) + '<span class="wfx-conv-ci">' + (isFinite(rate.p) ? fmtPct(rate.lo, 0) + "–" + fmtPct(rate.hi, 0) : "no data") + "</span></div>";
    };
    var commitRate = S.stats.commitOk;
    el.innerHTML =
      stage("ordered", "Ordered", f.ordered, f.canceled + " canceled (" + (isFinite(f.cxRate.p) ? fmtPct(f.cxRate.p, 0) : "—") + ")", "info") +
      arrow(f.startRate, "Started ÷ active orders") +
      stage("started", "Started", f.started, "MWL in progress + done", "warn") +
      arrow(f.doneRate, "Completed ÷ started") +
      stage("completed", "Completed", f.completed, f.reconciled + " with study (" + (isFinite(f.studyRate.p) ? fmtPct(f.studyRate.p, 0) : "—") + ")", "ok") +
      arrow(commitRate, "Committed ÷ answered commitments") +
      stage("committed", "Committed", f.committed, f.commitTotal ? "of " + f.commitTotal + " answered" : "no requests", "ok") +
      '<span class="wfx-pipeline-meta">' + win + " · " + conf + "% CI</span>";
  }

  function renderUpdated() {
    var el = $id("wfx-updated"); if (!el) return;
    var errs = Object.keys(S.errors).filter(function (k) { return S.errors[k]; });
    el.classList.toggle("is-err", errs.length > 0);
    el.textContent = errs.length ? "⚠ " + S.errors[errs[0]] : S.loadedAt ? "Updated " + S.loadedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
  }

  /* ------------------------------------------------------------ worklist */
  function statusPill(st) {
    var cls = { "SCHEDULED": "open", "IN PROGRESS": "prog", "COMPLETED": "done", "CANCELED": "cx", "DISCONTINUED": "cx", "PENDING": "open", "FAILED": "cx" }[st] || "open";
    return '<span class="wfx-pill is-' + cls + '">' + H(STATUS_LABEL[st] || st || "—") + "</span>";
  }
  function matchesQ(q, fields) {
    if (!q) return true;
    var hay = fields.join(" ").toLowerCase();
    return q.toLowerCase().split(/\s+/).every(function (t) { return hay.indexOf(t) >= 0; });
  }
  function rangeOk(s) {
    var r = P.range; if (r === "all") return true;
    if (r === "overdue") return s._overdue;
    if (!s._sched) return false;
    var d0 = startOfDay(s._sched).getTime(), t0 = startOfDay(new Date()).getTime();
    if (r === "today") return d0 === t0;
    if (r === "tomorrow") return d0 === t0 + DAY || Math.abs(d0 - (t0 + DAY)) < 2 * HOUR;
    if (r === "week") return d0 >= t0 && d0 < t0 + 7 * DAY;
    if (r === "past7") return d0 < t0 + DAY && d0 >= t0 - 7 * DAY;
    if (r === "custom") {
      var f = P.from ? new Date(P.from + "T00:00").getTime() : -Infinity, t = P.to ? new Date(P.to + "T00:00").getTime() : Infinity;
      return d0 >= f && d0 <= t;
    }
    return true;
  }
  function filteredMwl(ignoreStatus) {
    var rows = S.mwl.filter(function (s) {
      if (!ignoreStatus && P.status && s.status !== P.status) return false;
      if (P.mod && (s.modality || "") !== P.mod) return false;
      if (P.station && (s.scheduled_station_aet || "") !== P.station) return false;
      if (P.source === "his" && s.patient_source !== "his") return false;
      if (P.source === "manual" && s.patient_source === "his") return false;
      if (!rangeOk(s)) return false;
      return matchesQ(P.q, [s.patient_name, s.patient_id, s.accession_number, s.requested_procedure_description, s.sps_id, s.scheduled_performing_physician, s.requested_procedure_id]);
    });
    var t = function (s) { return s._sched ? s._sched.getTime() : Infinity; };
    var cmp = {
      "date-asc": function (a, b) { return t(a) - t(b); },
      "date-desc": function (a, b) { return (b._sched ? b._sched.getTime() : -Infinity) - (a._sched ? a._sched.getTime() : -Infinity); },
      patient: function (a, b) { return (a.patient_name || "").localeCompare(b.patient_name || ""); },
      modality: function (a, b) { return (a.modality || "").localeCompare(b.modality || "") || t(a) - t(b); },
      station: function (a, b) { return (a.scheduled_station_aet || "").localeCompare(b.scheduled_station_aet || "") || t(a) - t(b); },
      status: function (a, b) { return (STATUS_ORDER[a.status] - STATUS_ORDER[b.status]) || t(a) - t(b); },
      wait: function (a, b) { return (b._wait || -1) - (a._wait || -1) || t(a) - t(b); }
    }[P.sort] || function () { return 0; };
    return rows.sort(cmp);
  }
  function fillSelect(id, values, current) {
    var el = $id(id); if (!el) return;
    var opts = ['<option value="">All</option>'].concat(values.map(function (v) { return '<option value="' + H(v) + '"' + (v === current ? " selected" : "") + ">" + H(v) + "</option>"; }));
    el.innerHTML = opts.join("");
    el.value = current || "";
  }
  function uniq(arr) { return Array.from(new Set(arr.filter(Boolean))).sort(); }
  function refreshFacets() {
    fillSelect("wfx-mod", uniq(S.mwl.map(function (s) { return s.modality; })), P.mod);
    fillSelect("wfx-station", uniq(S.mwl.map(function (s) { return s.scheduled_station_aet; })), P.station);
    fillSelect("wfx-mpps-mod", uniq(S.mpps.map(function (p) { return p.modality; })), P.mppsMod);
    fillSelect("wfx-commit-ae", uniq(S.commit.map(function (c) { return c.ae_title; })), P.commitAe);
    var dl = function (id, vals) { var el = $id(id); if (el) el.innerHTML = vals.map(function (v) { return '<option value="' + H(v) + '"></option>'; }).join(""); };
    dl("wfx-dl-station", uniq(S.mwl.map(function (s) { return s.scheduled_station_aet; }).concat(S.mpps.map(function (p) { return p.performed_station_aet; }))));
    dl("wfx-dl-desc", uniq(S.mwl.map(function (s) { return s.requested_procedure_description; })).slice(0, 200));
    dl("wfx-dl-phys", uniq(S.mwl.map(function (s) { return s.scheduled_performing_physician; })).slice(0, 200));
    /* status counts in the segmented control */
    var base = filteredMwl(true), c = { "": base.length };
    base.forEach(function (s) { c[s.status] = (c[s.status] || 0) + 1; });
    qsa("#wfx-status [data-sc]").forEach(function (i) { i.textContent = c[i.dataset.sc] || 0; });
  }

  function visibleCols() { return COLS.filter(function (c) { return P.cols[c.key]; }); }
  function gridTemplate() { return "34px 104px " + visibleCols().map(function (c) { return c.w; }).join(" ") + " minmax(150px,auto)"; }

  function cellFor(key, s) {
    switch (key) {
      case "sched":
        var rel = s._sched ? fmtRel(s._hasTime ? s._sched : null) : "";
        return '<div class="wfx-c-sched"><b class="mono">' + (s._hasTime ? fmtTM(s.start_time) : "—:—") + '</b><small>' + H(fmtDA(s.start_date)) + (rel && s.status === "SCHEDULED" ? " · " + H(rel) : "") + "</small></div>";
      case "patient":
        return '<div class="wfx-c-pt"><b>' + H((s.patient_name || "Unknown").replace(/\^/g, " ").trim() || "Unknown") + (s.patient_source === "his" ? ' <span class="wfx-tag" title="Registered by HIS (HL7 ADT)">HIS</span>' : "") + '</b><small class="mono">' + H(s.patient_id || "—") + "</small></div>";
      case "proc":
        return '<div class="wfx-c-proc"><span>' + H(s.requested_procedure_description || "—") + '</span><small class="mono">' + H(s.sps_id || "") + (s.requested_procedure_id ? " · RP " + H(s.requested_procedure_id) : "") + "</small></div>";
      case "acc":
        return '<span class="mono wfx-c-acc' + (s._dupAcc ? " is-dup" : "") + '"' + (s._dupAcc ? ' title="Duplicate accession"' : "") + ">" + H(s.accession_number || "—") + "</span>";
      case "mod":
        return s.modality ? '<span class="wfx-mod" style="--mc:' + modColor(s.modality) + '">' + H(s.modality) + "</span>" : '<span class="wfx-dim">any</span>';
      case "station":
        return '<span class="mono">' + H(s.scheduled_station_aet || "any") + "</span>";
      case "phys":
        return "<span>" + H((s.scheduled_performing_physician || "—").replace(/\^/g, " ")) + "</span>";
      case "link":
        var m = s._mpps.length, st = !!s.study_instance_uid;
        return '<span class="wfx-chain" title="Order → MPPS → Study"><i class="on"></i><i class="' + (m ? "on" : "") + '" title="' + m + ' MPPS"></i><i class="' + (st ? "on" : "") + '" title="' + (st ? "Study received" : "No study yet") + '"></i></span>';
      case "age":
        if (s._overdue) return '<span class="wfx-late">' + fmtDur(s._wait) + " late</span>";
        if (s.status === "COMPLETED" && s._delay != null) return '<span class="' + (Math.abs(s._delay) <= P.s.tol ? "wfx-ok" : "wfx-warn") + '" title="Start delay vs schedule">Δ ' + fmtDur(s._delay) + "</span>";
        return '<span class="wfx-dim">' + (s._created ? fmtRel(s._created) : "—") + "</span>";
    }
    return "";
  }
  function rowActions(s) {
    var b = [];
    if (s.status === "SCHEDULED") b.push('<button type="button" class="wfx-act" data-act="start" title="Start (MPPS IN PROGRESS)">▶</button>', '<button type="button" class="wfx-act" data-act="complete" title="Complete">✓</button>');
    else if (s.status === "IN PROGRESS") b.push('<button type="button" class="wfx-act" data-act="complete" title="Complete">✓</button>');
    else if (s.status === "CANCELED") b.push('<button type="button" class="wfx-act" data-act="reopen" title="Reopen">↺</button>');
    if (s.study_instance_uid) b.push('<a class="wfx-act" href="' + H(studyHref(s.study_instance_uid)) + '" target="_blank" rel="noopener" title="Open study">◉</a>');
    b.push('<button type="button" class="wfx-act" data-act="menu" title="More actions" aria-haspopup="menu">⋯</button>');
    return b.join("");
  }
  function renderMwlRow(s) {
    var sel = S.sel.has(s.id);
    return '<div class="wfx-row' + (sel ? " is-sel" : "") + (s._overdue ? " is-late" : "") + '" role="row" tabindex="0" data-id="' + s.id + '" data-status="' + H(s.status) + '" style="grid-template-columns:' + gridTemplate() + '">' +
      '<label class="wfx-sel" title="Select"><input type="checkbox" data-sel="' + s.id + '"' + (sel ? " checked" : "") + " /></label>" +
      "<div>" + statusPill(s.status) + "</div>" +
      visibleCols().map(function (c) { return '<div class="wfx-cell" data-col="' + c.key + '">' + cellFor(c.key, s) + "</div>"; }).join("") +
      '<div class="wfx-actions">' + rowActions(s) + "</div></div>";
  }
  function renderMwl() {
    var host = $id("wfx-mwl"); if (!host) return;
    refreshFacets();
    var rows = filteredMwl(false);
    host.classList.toggle("is-dense", !!P.dense);
    host.dataset.mode = P.mode;
    $id("wfx-count").textContent = rows.length + " of " + S.mwl.length + " orders";
    if (S.errors.mwl && !S.mwl.length) { host.innerHTML = errorBox(S.errors.mwl); $id("wfx-pager").hidden = true; return; }
    if (P.mode === "board") { renderBoard(host, rows); $id("wfx-pager").hidden = true; updateBulk(); return; }
    if (P.mode === "timeline") { renderTimeline(host, rows); $id("wfx-pager").hidden = true; updateBulk(); return; }
    if (!rows.length) {
      host.innerHTML = '<div class="wfx-empty-state"><b>No orders match.</b><p class="sub">' + (S.mwl.length ? "Loosen the filters or clear them." : "No scheduled procedures yet. HIS admissions (HL7 ADT) and imaging orders (HL7 ORM) open steps automatically, or use <b>New order</b>.") + "</p></div>";
      $id("wfx-pager").hidden = true; updateBulk(); return;
    }
    var size = +P.pageSize || 0, pages = size ? Math.ceil(rows.length / size) : 1;
    if (S.page > pages) S.page = pages; if (S.page < 1) S.page = 1;
    var pageRows = size ? rows.slice((S.page - 1) * size, S.page * size) : rows;
    var head = '<div class="wfx-row wfx-head-row" role="row" style="grid-template-columns:' + gridTemplate() + '">' +
      '<label class="wfx-sel" title="Select all on page"><input type="checkbox" id="wfx-selall"' + (pageRows.length && pageRows.every(function (s) { return S.sel.has(s.id); }) ? " checked" : "") + " /></label><div>Status</div>" +
      visibleCols().map(function (c) { return '<div data-col="' + c.key + '">' + c.label + "</div>"; }).join("") + '<div class="wfx-actions-h">Actions</div></div>';
    var body = "", lastDay = null;
    pageRows.forEach(function (s) {
      if (P.group && /^date/.test(P.sort)) {
        var day = s._sched ? ymd(s._sched) : "none";
        if (day !== lastDay) {
          lastDay = day;
          var n = rows.filter(function (r) { return (r._sched ? ymd(r._sched) : "none") === day; }).length;
          var label = day === "none" ? "No date" : dayLabel(s._sched);
          body += '<div class="wfx-day" role="row"><span>' + H(label) + "</span><i>" + n + " order" + (n === 1 ? "" : "s") + "</i></div>";
        }
      }
      body += renderMwlRow(s);
    });
    host.innerHTML = head + body;
    var pager = $id("wfx-pager");
    if (pages > 1) {
      pager.hidden = false;
      pager.innerHTML = '<button type="button" class="btn sm" data-pg="prev"' + (S.page <= 1 ? " disabled" : "") + '>← Prev</button><span>Page <b>' + S.page + "</b> of " + pages + '</span><button type="button" class="btn sm" data-pg="next"' + (S.page >= pages ? " disabled" : "") + ">Next →</button>";
    } else pager.hidden = true;
    updateBulk();
  }
  function dayLabel(d) {
    var t0 = startOfDay(new Date()).getTime(), d0 = startOfDay(d).getTime(), diff = Math.round((d0 - t0) / DAY);
    var base = d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "short", year: d.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined });
    return diff === 0 ? "Today · " + base : diff === 1 ? "Tomorrow · " + base : diff === -1 ? "Yesterday · " + base : base;
  }
  function errorBox(msg) { return '<div class="wfx-error" role="alert"><span aria-hidden="true">!</span><p>' + H(msg) + '</p><button type="button" class="btn sm" data-retry>Retry</button></div>'; }

  /* Kanban board — drag a card onto a column to change its status. */
  function renderBoard(host, rows) {
    var cols = ["SCHEDULED", "IN PROGRESS", "COMPLETED", "CANCELED"];
    host.innerHTML = '<div class="wfx-board">' + cols.map(function (st) {
      var items = rows.filter(function (s) { return s.status === st; });
      return '<section class="wfx-lane" data-drop="' + st + '"><header>' + statusPill(st) + "<b>" + items.length + "</b></header><div class=\"wfx-lane-body\">" +
        (items.slice(0, 150).map(function (s) {
          return '<article class="wfx-card' + (s._overdue ? " is-late" : "") + '" draggable="' + (canWrite() ? "true" : "false") + '" data-id="' + s.id + '" tabindex="0">' +
            '<div class="wfx-card-top">' + (s.modality ? '<span class="wfx-mod" style="--mc:' + modColor(s.modality) + '">' + H(s.modality) + "</span>" : "") + '<span class="mono">' + (s._hasTime ? fmtTM(s.start_time) : "") + " " + H(fmtDA(s.start_date)) + "</span></div>" +
            "<b>" + H((s.patient_name || "Unknown").replace(/\^/g, " ")) + "</b><small>" + H(s.requested_procedure_description || "—") + '</small><div class="wfx-card-foot"><span class="mono">' + H(s.scheduled_station_aet || "any") + "</span>" + (s._overdue ? '<span class="wfx-late">' + fmtDur(s._wait) + " late</span>" : "") + "</div></article>";
        }).join("") || '<p class="wfx-lane-empty">Drop here</p>') + (items.length > 150 ? '<p class="wfx-lane-empty">+' + (items.length - 150) + " more — use the table</p>" : "") + "</div></section>";
    }).join("") + "</div>";
  }

  /* Day timeline — one lane per station, blocks sized by the modality's median exam time. */
  function renderTimeline(host, rows) {
    var day = P.tlDay ? new Date(P.tlDay + "T00:00") : startOfDay(new Date());
    if (isNaN(day)) day = startOfDay(new Date());
    var key = ymd(day);
    var items = rows.filter(function (s) { return s._sched && ymd(s._sched) === key && s._hasTime; });
    var untimed = rows.filter(function (s) { return s._sched && ymd(s._sched) === key && !s._hasTime; }).length;
    var medByMod = {};
    Object.keys(S.stats.groups || {}).forEach(function () { /* group medians may be per station */ });
    S.mpps.forEach(function (p) { if (p._dur != null && !p._durBad) (medByMod[p.modality || "—"] = medByMod[p.modality || "—"] || []).push(p._dur); });
    Object.keys(medByMod).forEach(function (m) { medByMod[m] = ST.quantile(ST.sorted(medByMod[m]), 0.5); });
    var lanes = uniq(items.map(function (s) { return s.scheduled_station_aet || "(any station)"; }));
    var h0 = 7, h1 = 19;
    items.forEach(function (s) { var hh = s._sched.getHours(); if (hh < h0) h0 = hh; if (hh + 1 > h1) h1 = Math.min(24, hh + 1); });
    var span = (h1 - h0) * 60, pct = function (min) { return ((min - h0 * 60) / span * 100).toFixed(3); };
    var hours = []; for (var h = h0; h <= h1; h++) hours.push(h);
    var nowMin = (Date.now() - day.getTime()) / MIN;
    var ctrl = '<div class="wfx-tl-ctrl"><button type="button" class="btn sm ghost" data-tl="prev" title="Previous day">‹</button><input type="date" id="wfx-tl-day" value="' + inputDate(day) + '" /><button type="button" class="btn sm ghost" data-tl="next" title="Next day">›</button><button type="button" class="btn sm ghost" data-tl="today">Today</button><span class="wfx-dim">' + items.length + " timed order" + (items.length === 1 ? "" : "s") + (untimed ? " · " + untimed + " without a time (not shown)" : "") + " · block width = median exam time per modality (default 20 min)</span></div>";
    if (!items.length) { host.innerHTML = ctrl + '<div class="wfx-empty-state"><b>Nothing timed on ' + H(dayLabel(day)) + '.</b><p class="sub">Orders need an SPS start time (0040,0003) to appear on the timeline.</p></div>'; return; }
    var axis = '<div class="wfx-tl-axis"><span class="wfx-tl-lane-l"></span><div class="wfx-tl-track">' + hours.map(function (hh) { return '<i style="left:' + pct(hh * 60) + '%">' + String(hh).padStart(2, "0") + ":00</i>"; }).join("") + "</div></div>";
    var body = lanes.map(function (lane) {
      var li = items.filter(function (s) { return (s.scheduled_station_aet || "(any station)") === lane; }).sort(function (a, b) { return a._sched - b._sched; });
      var rowsEnd = [];
      var blocks = li.map(function (s) {
        var start = s._sched.getHours() * 60 + s._sched.getMinutes(), dur = medByMod[s.modality || "—"] || 20;
        var r = 0; while (rowsEnd[r] != null && rowsEnd[r] > start) r++;
        rowsEnd[r] = start + dur;
        return '<button type="button" class="wfx-tl-block is-' + ({ "SCHEDULED": "open", "IN PROGRESS": "prog", "COMPLETED": "done", "CANCELED": "cx" }[s.status] || "open") + (s._overdue ? " is-late" : "") + '" data-id="' + s.id + '" style="left:' + pct(start) + "%;width:" + Math.max(0.8, dur / span * 100).toFixed(3) + "%;top:" + (4 + r * 26) + "px;--mc:" + modColor(s.modality) + '" title="' + H(fmtTM(s.start_time) + " · " + (s.patient_name || "").replace(/\^/g, " ") + " · " + (s.requested_procedure_description || "") + " · " + (STATUS_LABEL[s.status] || s.status)) + '"><b>' + fmtTM(s.start_time) + "</b> " + H((s.patient_name || "").split("^")[0]) + "</button>";
      }).join("");
      var hgt = Math.max(1, rowsEnd.length) * 26 + 8;
      return '<div class="wfx-tl-row"><span class="wfx-tl-lane-l mono">' + H(lane) + '</span><div class="wfx-tl-track" style="height:' + hgt + 'px">' + hours.map(function (hh) { return '<i class="wfx-tl-grid" style="left:' + pct(hh * 60) + '%"></i>'; }).join("") + (nowMin > h0 * 60 && nowMin < h1 * 60 ? '<i class="wfx-tl-now" style="left:' + pct(nowMin) + '%"></i>' : "") + blocks + "</div></div>";
    }).join("");
    host.innerHTML = ctrl + '<div class="wfx-timeline">' + axis + body + "</div>";
  }

  function updateBulk() {
    var bar = $id("wfx-bulk"); if (!bar) return;
    var ids = Array.from(S.sel).filter(function (id) { return S.mwl.some(function (s) { return s.id === id; }); });
    S.sel = new Set(ids);
    bar.hidden = !ids.length;
    $id("wfx-bulk-n").textContent = ids.length + " selected";
    qsa("[data-admin-only]", bar).forEach(function (b) { b.hidden = !isAdmin(); });
  }

  /* ------------------------------------------------------------ MPPS & Commitments */
  function renderMpps() {
    var host = $id("wfx-mpps"); if (!host) return;
    if (S.errors.mpps && !S.mpps.length) { host.innerHTML = errorBox(S.errors.mpps); return; }
    var q = ($id("wfx-mpps-q") && $id("wfx-mpps-q").value || "").trim();
    var rows = S.mpps.filter(function (p) {
      if (P.mppsStatus && p.status !== P.mppsStatus) return false;
      if (P.mppsMod && (p.modality || "") !== P.mppsMod) return false;
      if (P.mppsLink === "linked" && !p._step && !p.scheduled_step_id) return false;
      if (P.mppsLink === "orphan" && (p._step || p.scheduled_step_id)) return false;
      return matchesQ(q, [p.patient_name, p.patient_id, p.performed_station_aet, p.description, p.accession_number, p.pps_id, p.scheduled_step_id]);
    });
    if (!rows.length) {
      host.innerHTML = '<div class="wfx-empty-state"><b>No performed exams match.</b><p class="sub">' + (S.mpps.length ? "Adjust the MPPS filters." : "When a modality reports an exam via DICOM MPPS (N-CREATE / N-SET), it appears here in real time.") + "</p></div>";
      return;
    }
    var head = '<div class="wfx-row wfx-head-row wfx-mpps-row" role="row"><div>Status</div><div>Started</div><div>Patient</div><div>Procedure / PPS</div><div>Mod</div><div>Station</div><div>Duration</div><div>Link / Study</div></div>';
    var body = rows.map(function (p) {
      var durHtml = p._durBad ? '<span class="wfx-late" title="Invalid clock / duration">' + fmtDur(p._dur) + "</span>"
        : p._dur != null ? '<span class="mono">' + fmtDur(p._dur) + "</span>"
        : p.status === "IN PROGRESS" && p._start ? '<span class="wfx-warn">' + fmtDur((Date.now() - p._start.getTime()) / MIN) + " elapsed</span>"
        : '<span class="wfx-dim">—</span>';
      var linkHtml = p._step
        ? '<button type="button" class="wfx-link-btn mono" data-open-step="' + p._step.id + '">' + H(p._step.sps_id) + "</button>"
        : p.scheduled_step_id ? '<span class="mono wfx-dim">' + H(p.scheduled_step_id) + "</span>"
        : '<span class="wfx-tag warn" title="Performed without a matching MWL order">Unscheduled</span>';
      if (p.study_instance_uid) linkHtml += ' <a class="wfx-act" href="' + H(studyHref(p.study_instance_uid)) + '" target="_blank" rel="noopener" title="Open study">◉</a>';
      return '<div class="wfx-row wfx-mpps-row" role="row">' +
        "<div>" + statusPill(p.status) + "</div>" +
        '<div class="wfx-c-sched"><b class="mono">' + (p.start_time ? fmtTM(p.start_time) : "—:—") + "</b><small>" + H(fmtDA(p.start_date)) + (p.end_time ? " → " + fmtTM(p.end_time) : "") + "</small></div>" +
        '<div class="wfx-c-pt"><b>' + H((p.patient_name || "Unknown").replace(/\^/g, " ")) + '</b><small class="mono">' + H(p.patient_id || "—") + "</small></div>" +
        '<div class="wfx-c-proc"><span>' + H(p.description || "—") + '</span><small class="mono">' + H(p.pps_id || p.sop_instance_uid || "") + (p.accession_number ? " · Acc " + H(p.accession_number) : "") + "</small></div>" +
        "<div>" + (p.modality ? '<span class="wfx-mod" style="--mc:' + modColor(p.modality) + '">' + H(p.modality) + "</span>" : '<span class="wfx-dim">—</span>') + "</div>" +
        '<div class="mono">' + H(p.performed_station_aet || "—") + "</div>" +
        "<div>" + durHtml + "</div>" +
        '<div class="wfx-mpps-link">' + linkHtml + "</div></div>";
    }).join("");
    host.innerHTML = head + body;
  }

  function renderCommit() {
    var sum = $id("wfx-commit-summary"), host = $id("wfx-commit");
    if (!host) return;
    if (sum) {
      var cm = S.stats.commitOk, totalInst = 0, verInst = 0;
      S.commit.forEach(function (c) { totalInst += (c.instance_count || 0); verInst += (c.verified_count || 0); });
      var instRate = ST.wilson(verInst, totalInst, +P.s.conf);
      sum.innerHTML =
        '<div class="wfx-csum-item"><span>Transactions</span><b>' + S.commit.length + "</b><small>" + cm.k + " committed · " + (cm.n - cm.k) + " failed · " + S.stats.commitPending + " pending</small></div>" +
        '<div class="wfx-csum-item"><span>Transaction success</span><b>' + (cm.n ? fmtPct(cm.p, 1) : "—") + "</b><small>" + (cm.n ? Math.round(P.s.conf * 100) + "% Wilson CI " + fmtPct(cm.lo, 1) + "–" + fmtPct(cm.hi, 1) : "no answered requests") + "</small></div>" +
        '<div class="wfx-csum-item"><span>Instances verified</span><b>' + verInst + " / " + totalInst + "</b><small>" + (totalInst ? fmtPct(instRate.p, 2) + " verified" : "—") + "</small></div>";
    }
    if (S.errors.commit && !S.commit.length) { host.innerHTML = errorBox(S.errors.commit); return; }
    var q = ($id("wfx-commit-q") && $id("wfx-commit-q").value || "").trim();
    var rows = S.commit.filter(function (c) {
      if (P.commitStatus && c.status !== P.commitStatus) return false;
      if (P.commitAe && (c.ae_title || "") !== P.commitAe) return false;
      return matchesQ(q, [c.ae_title, c.transaction_uid]);
    });
    if (!rows.length) {
      host.innerHTML = '<div class="wfx-empty-state"><b>No storage commitments match.</b><p class="sub">' + (S.commit.length ? "Adjust the commitment filters." : "When a modality asks PAXS to guarantee storage via DICOM N-ACTION, the transaction appears here.") + "</p></div>";
      return;
    }
    var head = '<div class="wfx-row wfx-head-row wfx-cmt-row" role="row"><div>Status</div><div>AE title</div><div>Transaction UID</div><div>Verified</div><div>Latency</div><div>Requested / Report</div></div>';
    var body = rows.map(function (c) {
      var pct = c.instance_count ? (c.verified_count / c.instance_count * 100) : 0;
      var fails = (c.failures || []).map(function (f) {
        return '<div class="wfx-cmt-fail"><span class="mono">' + H(f.sop_instance_uid || "unknown SOP") + "</span><span>" + H(FAIL_REASON[f.failure_reason] || ("0x" + (f.failure_reason == null ? 0 : f.failure_reason).toString(16))) + "</span></div>";
      }).join("");
      return '<div class="wfx-row wfx-cmt-row' + (fails ? " has-fails" : "") + '" role="row">' +
        "<div>" + statusPill(c.status) + "</div>" +
        '<div class="mono"><b>' + H(c.ae_title || "unknown") + "</b></div>" +
        '<div class="mono wfx-trunc" title="' + H(c.transaction_uid) + '">' + H(c.transaction_uid || "—") + "</div>" +
        '<div><div class="wfx-bar-wrap"><span class="mono">' + c.verified_count + "/" + c.instance_count + '</span><span class="wfx-mbar"><i class="' + (c.status === "FAILED" ? "err" : "ok") + '" style="width:' + pct.toFixed(0) + '%"></i></span></div></div>' +
        '<div class="mono">' + (c._lat != null ? fmtDur(c._lat) : (c.status === "PENDING" && c._created ? fmtDur((Date.now() - c._created.getTime()) / MIN) + " wait" : "—")) + "</div>" +
        '<div class="wfx-c-sched"><small>' + fmtDateTime(c._created) + "</small>" + (c._sent ? "<small>N-EVENT " + fmtDateTime(c._sent) + "</small>" : "") + "</div>" +
        (fails ? '<div class="wfx-cmt-fails">' + fails + "</div>" : "") +
        "</div>";
    }).join("");
    host.innerHTML = head + body;
  }

  /* ------------------------------------------------------------ scientific analytics */
  function renderStats() {
    var host = $id("wfx-stats"); if (!host) return;
    var st = S.stats, conf = Math.round(P.s.conf * 100);
    host.innerHTML =
      '<article class="wfx-scard" id="wfx-sc-summary"></article>' +
      '<article class="wfx-scard" id="wfx-sc-dist"></article>' +
      '<article class="wfx-scard wfx-scard-wide" id="wfx-sc-spc"></article>' +
      '<article class="wfx-scard" id="wfx-sc-groups"></article>' +
      '<article class="wfx-scard" id="wfx-sc-heat"></article>';
    renderSummaryCard($id("wfx-sc-summary"), st, conf);
    renderDistCard($id("wfx-sc-dist"), st, conf);
    renderSpcCard($id("wfx-sc-spc"), st, conf);
    renderGroupsCard($id("wfx-sc-groups"), st, conf);
    renderHeatCard($id("wfx-sc-heat"), st);
  }

  function renderSummaryCard(el, st, conf) {
    var d = st.desc, L = st.little;
    var kv = function (k, v, sub) { return '<div class="wfx-skv"><span>' + k + "</span><b>" + v + "</b>" + (sub ? "<small>" + sub + "</small>" : "") + "</div>"; };
    var body = d.n ? (
      '<div class="wfx-skv-grid">' +
      kv("Sample size (n)", d.n, st.removed ? st.removed + " Tukey outlier" + (st.removed === 1 ? "" : "s") + " excluded" : (d.outliers ? d.outliers + " Tukey outlier" + (d.outliers === 1 ? "" : "s") + " detected" : "no outliers")) +
      kv("Mean (" + conf + "% t-CI)", fmtDur(d.mean), d.n > 1 ? fmtDur(d.ciLo) + " to " + fmtDur(d.ciHi) + " · SE " + fmtDur(d.se) : "n = 1") +
      kv("SD · CV", fmtDur(d.sd), isFinite(d.cv) ? "CV = " + fmtNum(d.cv, 2) : "—") +
      kv("Median [IQR]", fmtDur(d.median), "[" + fmtDur(d.q1) + ", " + fmtDur(d.q3) + "] · MAD " + fmtDur(d.mad)) +
      kv("P90 · P95", fmtDur(d.p90) + " · " + fmtDur(d.p95), "range " + fmtDur(d.min) + " to " + fmtDur(d.max)) +
      kv("Skew · Kurtosis", fmtNum(d.skew, 2) + " · " + fmtNum(d.kurt, 2), isFinite(d.skew) ? (Math.abs(d.skew) > 1 ? "strongly skewed" : Math.abs(d.skew) > 0.5 ? "moderately skewed" : "near symmetric") : "needs n ≥ 3") +
      "</div>"
    ) : '<p class="sub">No observations for <b>' + H(METRIC_LABEL[P.s.metric]) + "</b> in this window.</p>";

    var littleHtml =
      '<div class="wfx-little">' +
      '<div class="wfx-little-head"><b>Queue physics · Little’s Law (L = λW)</b><span>' + (P.s.window || "all") + " d window</span></div>" +
      '<div class="wfx-little-row">' +
      '<span>Arrivals λ<sub>in</sub> <b>' + fmtNum(L.arrivals, 2) + "/d</b></span>" +
      '<span>Throughput λ<sub>out</sub> <b>' + fmtNum(L.lambda, 2) + "/d</b></span>" +
      '<span>Balance <b>' + (L.balance != null ? fmtPct(L.balance, 0) : "—") + "</b></span>" +
      '<span>Active WIP (L) <b>' + L.wip + "</b></span>" +
      '<span>Implied W = L/λ <b>' + (L.implied != null ? fmtDur(L.implied) : "—") + "</b></span>" +
      '<span>Measured median W <b>' + (L.measured ? fmtDur(L.measured.median) : "—") + "</b></span>" +
      "</div></div>";

    el.innerHTML =
      '<header class="wfx-scard-h"><div><span class="wfx-scard-tag">DESCRIPTIVE & QUEUE INFERENCE</span><h3>' + H(METRIC_LABEL[P.s.metric]) + "</h3></div></header>" +
      body + littleHtml;
  }

  function renderDistCard(el, st, conf) {
    var raw = st.samples.map(function (s) { return s.v; });
    var vals = P.s.log ? raw.filter(function (v) { return v > 0; }).map(function (v) { return Math.log10(v); }) : raw.slice();
    var head = '<header class="wfx-scard-h"><div><span class="wfx-scard-tag">DISTRIBUTION & TUKEY BOX PLOT</span><h3>Histogram' + (P.s.kde ? " + Gaussian KDE" : "") + (P.s.log ? " (log₁₀ min)" : " (min)") + "</h3></div>" +
      '<span class="wfx-scard-meta">rule: ' + H(P.s.bins) + "</span></header>";
    if (vals.length < 2) {
      el.innerHTML = head + '<div class="wfx-empty-chart">Need at least 2' + (P.s.log ? " positive" : "") + " observations to plot a distribution.</div>";
      return;
    }
    var s = ST.sorted(vals), n = s.length, d = ST.describe(s, P.s.conf);
    var lo = s[0], hi = s[n - 1];
    if (lo === hi) { lo -= 0.5; hi += 0.5; }
    var bw = ST.binWidth(s, P.s.bins);
    var k = Math.max(4, Math.min(60, Math.ceil((hi - lo) / bw) || 1));
    bw = (hi - lo) / k;
    var bins = new Array(k).fill(0);
    s.forEach(function (v) { var idx = Math.min(k - 1, Math.max(0, Math.floor((v - lo) / bw))); bins[idx]++; });
    var densities = bins.map(function (c) { return c / (n * bw); });

    var W = 540, Hh = 260, pL = 46, pR = 16, pT = 16, pB = 58, plotB = Hh - pB;
    var xs = []; for (var i = 0; i <= 80; i++) xs.push(lo + (hi - lo) * i / 80);
    var kdeRes = (P.s.kde && n >= 3) ? ST.kde(s, xs) : null;
    var maxY = Math.max(Math.max.apply(null, densities), kdeRes ? Math.max.apply(null, kdeRes.y) : 0) * 1.12 || 1;
    var x = scale(lo, hi, pL, W - pR), y = scale(0, maxY, plotB, pT);

    var bars = bins.map(function (c, i) {
      var bx0 = lo + i * bw, bx1 = bx0 + bw, xx = x(bx0), ww = Math.max(1, x(bx1) - xx - 1), yy = y(densities[i]);
      return '<rect class="wfx-hist-bar" x="' + xx.toFixed(1) + '" y="' + yy.toFixed(1) + '" width="' + ww.toFixed(1) + '" height="' + Math.max(0, plotB - yy).toFixed(1) + '"><title>[' + fmtNum(bx0, 1) + ", " + fmtNum(bx1, 1) + "): " + c + " (" + fmtPct(c / n, 1) + ")</title></rect>";
    }).join("");

    var kdePath = "";
    if (kdeRes) {
      var kd = xs.map(function (xi, idx) { return (idx ? "L" : "M") + x(xi).toFixed(1) + " " + y(kdeRes.y[idx]).toFixed(1); }).join(" ");
      kdePath = '<path class="wfx-kde" d="' + kd + '"/>';
    }
    var refLine = function (val, cls, lbl, dy) {
      var xx = x(val), nearRight = xx > W - pR - 44;
      return '<line class="' + cls + '" x1="' + xx.toFixed(1) + '" x2="' + xx.toFixed(1) + '" y1="' + pT + '" y2="' + plotB + '"/><text class="wfx-ref-lbl" x="' + (nearRight ? xx - 4 : xx + 4).toFixed(1) + '" y="' + (pT + dy) + '" text-anchor="' + (nearRight ? "end" : "start") + '">' + lbl + "</text>";
    };

    /* Tukey box plot below x-axis */
    var boxY = Hh - 20, wLo = s.find(function (v) { return v >= d.fenceLo; }), wHi = s.slice().reverse().find(function (v) { return v <= d.fenceHi; });
    if (wLo == null) wLo = s[0]; if (wHi == null) wHi = s[n - 1];
    var outs = s.filter(function (v) { return v < d.fenceLo || v > d.fenceHi; });
    var box = '<g class="wfx-box">' +
      '<line x1="' + x(wLo).toFixed(1) + '" x2="' + x(wHi).toFixed(1) + '" y1="' + boxY + '" y2="' + boxY + '" class="wfx-box-whisk"/>' +
      '<line x1="' + x(wLo).toFixed(1) + '" x2="' + x(wLo).toFixed(1) + '" y1="' + (boxY - 4) + '" y2="' + (boxY + 4) + '" class="wfx-box-whisk"/>' +
      '<line x1="' + x(wHi).toFixed(1) + '" x2="' + x(wHi).toFixed(1) + '" y1="' + (boxY - 4) + '" y2="' + (boxY + 4) + '" class="wfx-box-whisk"/>' +
      '<rect x="' + x(d.q1).toFixed(1) + '" y="' + (boxY - 7) + '" width="' + Math.max(2, x(d.q3) - x(d.q1)).toFixed(1) + '" height="14" rx="2" class="wfx-box-iqr"/>' +
      '<line x1="' + x(d.median).toFixed(1) + '" x2="' + x(d.median).toFixed(1) + '" y1="' + (boxY - 8) + '" y2="' + (boxY + 8) + '" class="wfx-box-med"/>' +
      outs.map(function (v) { return '<circle cx="' + x(v).toFixed(1) + '" cy="' + boxY + '" r="2.4" class="wfx-box-out"><title>Outlier: ' + fmtNum(v, 2) + "</title></circle>"; }).join("") +
      "</g>";

    var fmtX = function (v) { return P.s.log ? fmtNum(Math.pow(10, v), 1) : fmtNum(v, Math.abs(hi - lo) < 10 ? 1 : 0); };
    var svg = svgOpen(W, Hh, "Distribution histogram and box plot") +
      axisY(ticks(0, maxY, 4), y, pL, W - pR, function (v) { return fmtNum(v, 2); }) +
      bars + kdePath +
      refLine(d.median, "wfx-ref-med", "Med", 10) +
      refLine(d.mean, "wfx-ref-mean", "Mean", 22) +
      '<line class="wfx-axis" x1="' + pL + '" x2="' + (W - pR) + '" y1="' + plotB + '" y2="' + plotB + '"/>' +
      axisX(ticks(lo, hi, 5), x, plotB, fmtX) + box + "</svg>";

    var foot = '<div class="wfx-scard-foot"><span><i class="dot med"></i>Median ' + fmtNum(d.median, 1) + '</span><span><i class="dot mean"></i>Mean ' + fmtNum(d.mean, 1) + "</span>" +
      (kdeRes ? "<span><i class=\"dot kde\"></i>Silverman h = " + fmtNum(kdeRes.h, 2) + "</span>" : "") +
      "<span>Bins k = " + k + " (w = " + fmtNum(bw, 2) + ")</span></div>";
    el.innerHTML = head + svg + foot;
  }

  function renderSpcCard(el, st, conf) {
    var ser = st.series, spc = st.spc, ols = st.ols, n = ser.length;
    var spcSel = '<label class="wfx-field"><span>Series</span><select id="wfx-s-spc">' +
      Object.keys(SPC_LABEL).map(function (k) { return '<option value="' + k + '"' + (P.s.spc === k ? " selected" : "") + ">" + SPC_LABEL[k] + "</option>"; }).join("") +
      "</select></label>";
    var head = '<header class="wfx-scard-h"><div><span class="wfx-scard-tag">STATISTICAL PROCESS CONTROL · I-MR & OLS</span><h3>' + H(SPC_LABEL[P.s.spc]) + "</h3></div>" + spcSel + "</header>";
    if (n < 2) { el.innerHTML = head + '<div class="wfx-empty-chart">Need at least 2 days of data.</div>'; return; }

    var W = 1080, Hh = 260, pL = 44, pR = 54, pT = 18, pB = 34, plotB = Hh - pB;
    var yMax = Math.max(spc.ucl, Math.max.apply(null, ser.map(function (p) { return p.y; })), ols ? Math.max(ols.predict(0).hi, ols.predict(n - 1).hi) : 0, 1) * 1.1;
    var x = scale(0, n - 1, pL, W - pR), y = scale(0, yMax, plotB, pT);

    /* OLS prediction band & line */
    var olsSvg = "";
    if (ols) {
      var up = [], dn = [];
      for (var i = 0; i < n; i++) { var pr = ols.predict(i); up.push(x(i).toFixed(1) + "," + y(Math.max(0, pr.hi)).toFixed(1)); dn.unshift(x(i).toFixed(1) + "," + y(Math.max(0, pr.lo)).toFixed(1)); }
      olsSvg = '<polygon class="wfx-ols-band" points="' + up.concat(dn).join(" ") + '"/>' +
        '<line class="wfx-ols-line" x1="' + x(0).toFixed(1) + '" y1="' + y(Math.max(0, ols.a)).toFixed(1) + '" x2="' + x(n - 1).toFixed(1) + '" y2="' + y(Math.max(0, ols.a + ols.b * (n - 1))).toFixed(1) + '"/>';
    }

    var hLine = function (val, cls, lbl) {
      var yy = y(val).toFixed(1);
      return '<line class="' + cls + '" x1="' + pL + '" x2="' + (W - pR) + '" y1="' + yy + '" y2="' + yy + '"/><text class="wfx-spc-lbl" x="' + (W - pR + 5) + '" y="' + yy + '" dominant-baseline="middle">' + lbl + " " + fmtNum(val, 1) + "</text>";
    };

    var poly = ser.map(function (p, i) { return (i ? "L" : "M") + x(i).toFixed(1) + " " + y(p.y).toFixed(1); }).join(" ");
    var pts = ser.map(function (p, i) {
      var fl = spc.flags[i], bad = fl.length > 0;
      var tip = inputDate(p.d) + ": " + p.y + (bad ? " · " + fl.map(function (f) { return f + ": " + RULE_TEXT[f]; }).join("; ") : "");
      return '<circle class="wfx-spc-pt' + (bad ? " is-viol" : "") + '" cx="' + x(i).toFixed(1) + '" cy="' + y(p.y).toFixed(1) + '" r="' + (bad ? 4.2 : 2.8) + '"><title>' + H(tip) + "</title></circle>";
    }).join("");

    var step = Math.max(1, Math.floor(n / 7)), xIdx = [];
    for (var xi = 0; xi < n; xi += step) xIdx.push(xi);
    if (xIdx[xIdx.length - 1] !== n - 1) xIdx.push(n - 1);

    var svg = svgOpen(W, Hh, "I-MR statistical process control chart") +
      axisY(ticks(0, yMax, 4), y, pL, W - pR, function (v) { return fmtNum(v, 0); }) +
      olsSvg +
      hLine(spc.ucl, "wfx-spc-lim", "UCL") +
      hLine(spc.cl, "wfx-spc-cl", "CL") +
      (spc.lcl > 0 ? hLine(spc.lcl, "wfx-spc-lim", "LCL") : "") +
      '<path class="wfx-spc-line" d="' + poly + '"/>' +
      pts +
      '<line class="wfx-axis" x1="' + pL + '" x2="' + (W - pR) + '" y1="' + plotB + '" y2="' + plotB + '"/>' +
      axisX(xIdx, x, plotB, function (i) { return inputDate(ser[i].d).slice(5); }) + "</svg>";

    var violList = [];
    spc.flags.forEach(function (fl, i) { if (fl.length) violList.push(inputDate(ser[i].d).slice(5) + " (" + fl.join(",") + ")"); });
    var trendTxt = ols ? ("Slope " + (ols.b >= 0 ? "+" : "") + fmtNum(ols.b, 3) + "/day (" + conf + "% CI " + fmtNum(ols.bLo, 3) + " to " + fmtNum(ols.bHi, 3) + ") · R² = " + fmtNum(ols.r2, 2) + " · p " + (ols.p < 0.001 ? "< 0.001" : "= " + fmtP(ols.p))) : "OLS needs n ≥ 3";
    var foot = '<div class="wfx-scard-foot">' +
      "<span>CL = " + fmtNum(spc.cl, 2) + " · σ̂ = MR̄/1.128 = " + fmtNum(spc.sigma, 2) + "</span>" +
      "<span>" + trendTxt + "</span>" +
      '<span class="' + (violList.length ? "wfx-late" : "wfx-ok") + '">' + (violList.length ? "⚠ " + violList.length + " Nelson signals: " + violList.slice(0, 4).join(", ") : "✓ In statistical control") + "</span>" +
      "</div>";
    el.innerHTML = head + svg + foot;
  }

  function renderGroupsCard(el, st, conf) {
    var groups = st.groups, keys = Object.keys(groups).sort(function (a, b) { return groups[b].length - groups[a].length; });
    var kw = st.kw;
    var kwText = kw ? ("Kruskal–Wallis H = " + fmtNum(kw.H, 2) + " (df=" + kw.df + ") · p " + (kw.p < 0.001 ? "< 0.001" : "= " + fmtP(kw.p)) + " · ε² = " + fmtNum(Math.max(0, kw.eps2), 2)) : "Kruskal–Wallis needs ≥ 2 groups";
    var head = '<header class="wfx-scard-h"><div><span class="wfx-scard-tag">STRATIFIED COMPARISON</span><h3>By ' + H(P.s.group) + "</h3></div><span class=\"wfx-scard-meta\">" + H(kwText) + "</span></header>";
    if (!keys.length) { el.innerHTML = head + '<div class="wfx-empty-chart">No grouped observations in this window.</div>'; return; }

    var rows = keys.slice(0, 12).map(function (k) { return { k: k, d: ST.describe(groups[k], P.s.conf) }; });
    var minV = Math.min.apply(null, rows.map(function (r) { return r.d.min; }));
    var maxV = Math.max.apply(null, rows.map(function (r) { return r.d.max; }));
    if (minV === maxV) maxV = minV + 1;
    var xPct = function (v) { return ((v - minV) / (maxV - minV) * 100).toFixed(2); };

    var table = '<div class="wfx-grp-table">' + rows.map(function (r) {
      var d = r.d, q1 = xPct(d.q1), q3 = xPct(d.q3), med = xPct(d.median), lo = xPct(d.min), hi = xPct(d.max);
      var col = P.s.group === "modality" ? modColor(r.k) : "var(--accent)";
      return '<div class="wfx-grp-row">' +
        '<span class="wfx-grp-k mono">' + H(r.k) + '</span><span class="wfx-grp-n">n=' + d.n + "</span>" +
        '<div class="wfx-grp-plot" title="Min ' + fmtDur(d.min) + " · Q1 " + fmtDur(d.q1) + " · Med " + fmtDur(d.median) + " · Q3 " + fmtDur(d.q3) + " · Max " + fmtDur(d.max) + '">' +
        '<i class="wfx-grp-w" style="left:' + lo + "%;width:" + Math.max(0.5, hi - lo) + '%"></i>' +
        '<i class="wfx-grp-iqr" style="left:' + q1 + "%;width:" + Math.max(1, q3 - q1) + "%;background:" + col + '"></i>' +
        '<i class="wfx-grp-med" style="left:' + med + '%"></i></div>' +
        '<span class="wfx-grp-val mono">' + fmtDur(d.median) + " <small>[" + fmtDur(d.q1) + "–" + fmtDur(d.q3) + "]</small></span></div>";
    }).join("") + "</div>";
    el.innerHTML = head + table + '<div class="wfx-scard-foot"><span>Box = IQR [Q1–Q3] · tick = median · whiskers = min–max · ' + conf + "% window</span></div>";
  }

  function renderHeatCard(el, st) {
    var sel = '<label class="wfx-field"><span>Source</span><select id="wfx-s-heat">' +
      '<option value="scheduled"' + (P.s.heat === "scheduled" ? " selected" : "") + '>Scheduled (MWL)</option>' +
      '<option value="performed"' + (P.s.heat === "performed" ? " selected" : "") + ">Performed (MPPS)</option></select></label>";
    var head = '<header class="wfx-scard-h"><div><span class="wfx-scard-tag">TEMPORAL LOAD MATRIX</span><h3>Weekday × hour (' + (P.s.heat === "performed" ? "MPPS" : "MWL") + ")</h3></div>" + sel + "</header>";
    var heat = st.heat, mx = st.heatMax || 1;
    var peak = { dow: 0, h: 0, n: 0 }, total = 0;
    var hrs = []; for (var h = 0; h < 24; h++) hrs.push("<span>" + (h % 3 === 0 ? String(h).padStart(2, "0") : "") + "</span>");
    var grid = '<div class="wfx-heat"><div class="wfx-heat-head"><span></span>' + hrs.join("") + "<span>Σ</span></div>" +
      heat.map(function (row, r) {
        var rSum = ST.sum(row); total += rSum;
        return '<div class="wfx-heat-row"><span class="wfx-heat-dow">' + DOW[r] + "</span>" +
          row.map(function (v, h) {
            if (v > peak.n) peak = { dow: r, h: h, n: v };
            var alpha = v ? (0.14 + 0.86 * (v / mx)).toFixed(2) : "0";
            return '<i class="wfx-heat-cell' + (v ? " is-on" : "") + '" style="--ha:' + alpha + '" title="' + DOW[r] + " " + String(h).padStart(2, "0") + ":00–" + String(h).padStart(2, "0") + ":59 · " + v + '">' + (v || "") + "</i>";
          }).join("") + '<span class="wfx-heat-sum mono">' + rSum + "</span></div>";
      }).join("") + "</div>";
    var foot = '<div class="wfx-scard-foot"><span>Total timed: ' + total + "</span><span>" + (peak.n ? "Peak slot: " + DOW[peak.dow] + " " + String(peak.h).padStart(2, "0") + ":00 (" + peak.n + ")" : "No timed records") + "</span></div>";
    el.innerHTML = head + grid + foot;
  }

  /* ------------------------------------------------------------ data quality */
  function renderDq() {
    var host = $id("wfx-dq"); if (!host) return;
    var q = S.dq, muted = P.dq.muted || [];
    var scoreTile = function (label, sc) {
      var val = sc == null ? "—" : Math.round(sc * 100) + "%";
      var tone = sc == null ? "info" : sc >= 0.9 ? "ok" : sc >= 0.75 ? "warn" : "danger";
      return '<div class="wfx-dq-stat" data-tone="' + tone + '"><span>' + label + "</span><b>" + val + "</b></div>";
    };
    var summary = '<div class="wfx-dq-head">' +
      scoreTile("Overall quality", q.score) +
      scoreTile("Worklist (MWL)", q.scoreMwl) +
      scoreTile("Performed (MPPS)", q.scoreMpps) +
      scoreTile("Commitments", q.scoreCommit) +
      '<div class="wfx-dq-stat" data-tone="' + (q.counts.error ? "danger" : "ok") + '"><span>Errors / Warnings / Info</span><b>' + q.counts.error + " / " + q.counts.warn + " / " + q.counts.info + "</b><small>" + q.clean + " of " + q.records + " records clean</small></div>" +
      "</div>";

    var activeRules = RULES.filter(function (r) { return q.byRule[r.id]; });
    var ruleBar = activeRules.length ? '<div class="wfx-dq-rules"><span>Triggered rules (click to mute/unmute):</span>' + activeRules.map(function (r) {
      var isMuted = muted.indexOf(r.id) >= 0;
      return '<button type="button" class="wfx-dq-rchip is-' + r.sev + (isMuted ? " is-muted" : "") + '" data-mute="' + r.id + '" title="' + H(r.hint || r.title) + '"><span class="mono">' + H(r.tag) + "</span> " + H(r.title) + " <b>" + q.byRule[r.id] + "</b></button>";
    }).join("") + "</div>" : "";

    var rows = q.live.filter(function (i) {
      if (P.dq.sev && i.rule.sev !== P.dq.sev) return false;
      if (P.dq.scope && i.kind !== P.dq.scope) return false;
      return true;
    });
    var sevOrder = { error: 0, warn: 1, info: 2 };
    rows.sort(function (a, b) { return sevOrder[a.rule.sev] - sevOrder[b.rule.sev]; });

    var table = rows.length ? ('<div class="wfx-table"><div class="wfx-row wfx-head-row wfx-dq-row" role="row"><div>Sev</div><div>DICOM Tag</div><div>Finding</div><div>Record</div><div>Value</div><div>Action</div></div>' +
      rows.slice(0, 200).map(function (i) {
        var r = i.rule, rec = i.rec;
        var who = i.kind === "commit" ? (rec.ae_title || "unknown AE") : ((rec.patient_name || "Unknown").replace(/\^/g, " ") + " (" + (rec.patient_id || "—") + ")");
        var refId = i.kind === "mwl" ? rec.id : (rec._step ? rec._step.id : null);
        var act = refId ? '<button type="button" class="btn sm" data-open-step="' + refId + '">Inspect</button>' : '<span class="wfx-dim mono">' + H(i.kind.toUpperCase()) + "</span>";
        return '<div class="wfx-row wfx-dq-row" role="row">' +
          '<div><span class="wfx-sev is-' + r.sev + '">' + r.sev.toUpperCase() + "</span></div>" +
          '<div class="mono wfx-dim">' + H(r.tag) + "</div>" +
          '<div><b>' + H(r.title) + "</b>" + (r.hint ? '<small class="wfx-dq-hint">' + H(r.hint) + "</small>" : "") + "</div>" +
          '<div class="wfx-c-proc"><span>' + H(who) + '</span><small class="mono">' + H(rec.sps_id || rec.pps_id || rec.transaction_uid || "") + "</small></div>" +
          '<div class="mono wfx-trunc" title="' + H(i.value) + '">' + H(i.value || "—") + "</div>" +
          "<div>" + act + "</div></div>";
      }).join("") + (rows.length > 200 ? '<p class="sub wfx-empty">Showing 200 of ' + rows.length + " findings.</p>" : "") + "</div>")
      : '<div class="wfx-empty-state"><b>No findings match.</b><p class="sub">' + (q.records ? "All audited records pass the active DICOM VR & workflow checks." : "No records loaded yet.") + "</p></div>";

    host.innerHTML = summary + ruleBar + table;
  }

  /* ------------------------------------------------------------ drawer */
  function openDrawer(eyebrow, title, html) {
    $id("wfx-drawer-eyebrow").innerHTML = '<i aria-hidden="true"></i>' + H(eyebrow);
    $id("wfx-drawer-title").textContent = title;
    $id("wfx-drawer-body").innerHTML = html;
    $id("wfx-scrim").hidden = false;
    var dr = $id("wfx-drawer");
    dr.setAttribute("aria-hidden", "false");
    dr.classList.add("is-open");
  }
  function closeDrawer() {
    S.drawerFor = null;
    $id("wfx-scrim").hidden = true;
    var dr = $id("wfx-drawer");
    dr.setAttribute("aria-hidden", "true");
    dr.classList.remove("is-open");
  }
  function genAccession() {
    var d = new Date();
    return "ACC" + ymd(d).slice(2) + "-" + String(Math.floor(1000 + Math.random() * 9000));
  }

  function openNewOrder(prefill) {
    if (!canWrite()) { notify("Only ADMIN or TECHNOLOGIST can schedule orders", "err"); return; }
    S.drawerFor = { type: "new" };
    var f = prefill || {};
    var todayStr = inputDate(new Date());
    openDrawer("Modality worklist · new", "New procedure order",
      '<form id="wfx-new-form" class="wfx-form">' +
      '<div class="wfx-fgrid">' +
      '<label class="wfx-f"><span>Patient ID / MRN * <small class="mono">(0010,0020)</small></span><input name="patient_id" required maxlength="64" value="' + H(f.patient_id || "") + '" placeholder="MRN123456" /></label>' +
      '<label class="wfx-f"><span>Patient name <small class="mono">(0010,0010)</small></span><input name="patient_name" maxlength="192" value="' + H(f.patient_name || "") + '" placeholder="DOE^JANE" /></label>' +
      '<label class="wfx-f"><span>Modality <small class="mono">(0008,0060)</small></span><input name="modality" list="wfx-dl-modality" maxlength="16" value="' + H(f.modality || "CT") + '" placeholder="CT" /></label>' +
      '<label class="wfx-f"><span>Station AE Title <small class="mono">(0040,0001)</small></span><input name="scheduled_station_aet" list="wfx-dl-station" maxlength="64" value="' + H(f.scheduled_station_aet || "") + '" placeholder="CT1" /></label>' +
      '<label class="wfx-f"><span>Start date <small class="mono">(0040,0002)</small></span><input type="date" name="start_date" value="' + H(f.start_date ? fmtDA(f.start_date) : todayStr) + '" /></label>' +
      '<label class="wfx-f"><span>Start time <small class="mono">(0040,0003)</small></span><input type="time" name="start_time" value="' + H(f.start_time ? fmtTM(f.start_time) : "") + '" /></label>' +
      '<label class="wfx-f"><span>Accession number <small class="mono">(0008,0050)</small></span><div class="wfx-f-in"><input name="accession_number" maxlength="64" value="' + H(f.accession_number || genAccession()) + '" /><button type="button" class="btn sm ghost" id="wfx-gen-acc" title="Generate accession">⟳</button></div></label>' +
      '<label class="wfx-f"><span>Requested Procedure ID <small class="mono">(0040,1001)</small></span><input name="requested_procedure_id" maxlength="64" value="' + H(f.requested_procedure_id || "") + '" placeholder="RP-101" /></label>' +
      '<label class="wfx-f wfx-f-full"><span>Procedure description <small class="mono">(0032,1060)</small></span><input name="requested_procedure_description" list="wfx-dl-desc" maxlength="255" value="' + H(f.requested_procedure_description || "") + '" placeholder="CT CHEST W/ CONTRAST" /></label>' +
      '<label class="wfx-f"><span>Performing physician <small class="mono">(0040,0006)</small></span><input name="scheduled_performing_physician" list="wfx-dl-phys" maxlength="192" value="' + H(f.scheduled_performing_physician || "") + '" placeholder="SMITH^ALEX" /></label>' +
      '<label class="wfx-f"><span>Initial status</span><select name="status"><option value="SCHEDULED">SCHEDULED</option><option value="IN PROGRESS">IN PROGRESS</option></select></label>' +
      "</div>" +
      '<div class="wfx-vr-msg" id="wfx-new-vr"></div>' +
      '<div class="wfx-drawer-foot"><button type="button" class="btn ghost" id="wfx-new-cancel">Cancel</button><button type="submit" class="btn primary">Schedule order</button></div>' +
      "</form>");
    var form = $id("wfx-new-form");
    $id("wfx-gen-acc").addEventListener("click", function () { form.elements.accession_number.value = genAccession(); checkFormVr(form, $id("wfx-new-vr")); });
    $id("wfx-new-cancel").addEventListener("click", closeDrawer);
    form.addEventListener("input", function () { checkFormVr(form, $id("wfx-new-vr")); });
    checkFormVr(form, $id("wfx-new-vr"));
    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      var fd = new FormData(form);
      var tm = (fd.get("start_time") || "").replace(":", "");
      var body = {
        patient_id: String(fd.get("patient_id") || "").trim(),
        patient_name: String(fd.get("patient_name") || "").trim() || null,
        modality: String(fd.get("modality") || "").trim().toUpperCase() || null,
        scheduled_station_aet: String(fd.get("scheduled_station_aet") || "").trim() || null,
        start_date: String(fd.get("start_date") || "").replace(/-/g, "") || null,
        start_time: tm ? tm + "00" : null,
        accession_number: String(fd.get("accession_number") || "").trim() || null,
        requested_procedure_id: String(fd.get("requested_procedure_id") || "").trim() || null,
        requested_procedure_description: String(fd.get("requested_procedure_description") || "").trim() || null,
        scheduled_performing_physician: String(fd.get("scheduled_performing_physician") || "").trim() || null,
        status: String(fd.get("status") || "SCHEDULED")
      };
      if (!body.patient_id) { notify("Patient ID / MRN is required", "err"); return; }
      var res = await call("/api/v1/worklist", jsonOpts("POST", body));
      if (!res.ok) {
        var err = await res.json().catch(function () { return {}; });
        notify(err.detail || "Schedule failed (" + res.status + ")", "err");
        return;
      }
      notify("Procedure scheduled", "ok");
      closeDrawer();
      await load();
    });
  }

  function checkFormVr(form, out) {
    if (!form || !out) return;
    var hints = [];
    var v = function (n) { return (form.elements[n] && form.elements[n].value || "").trim(); };
    if (v("accession_number").length > 16) hints.push("Accession exceeds SH (16 chars)");
    if (v("scheduled_station_aet").length > 16 || AE_BAD.test(v("scheduled_station_aet"))) hints.push("Station AET violates AE VR (≤ 16 chars, no \\)");
    if (v("modality") && MODALITY_TERMS.indexOf(v("modality").toUpperCase()) < 0) hints.push("Modality '" + v("modality").toUpperCase() + "' is not a DICOM Defined Term");
    if (v("patient_name") && v("patient_name").indexOf("^") < 0) hints.push("Tip: use Family^Given for DICOM PN");
    if (v("requested_procedure_description").length > 64) hints.push("Description exceeds LO (64 chars)");
    if (v("requested_procedure_id").length > 16) hints.push("Requested Procedure ID exceeds SH (16 chars)");
    out.innerHTML = hints.map(function (h) { return "<span>⚠ " + H(h) + "</span>"; }).join("");
  }

  function openOrder(stepId) {
    var s = S.mwl.find(function (r) { return r.id === stepId; });
    if (!s) return;
    S.drawerFor = { type: "step", id: stepId };
    var editable = canWrite();
    var findings = S.dq.issues.filter(function (i) { return i.rec === s; });
    var btns = [];
    if (editable) {
      if (s.status === "SCHEDULED") btns.push('<button type="button" class="btn sm primary" data-dact="IN PROGRESS">▶ Start exam</button>', '<button type="button" class="btn sm" data-dact="COMPLETED">✓ Complete</button>', '<button type="button" class="btn sm danger" data-dact="CANCELED">✕ Cancel</button>');
      else if (s.status === "IN PROGRESS") btns.push('<button type="button" class="btn sm primary" data-dact="COMPLETED">✓ Complete</button>', '<button type="button" class="btn sm" data-dact="SCHEDULED">↺ Reopen</button>');
      else if (s.status === "CANCELED" || s.status === "COMPLETED") btns.push('<button type="button" class="btn sm" data-dact="SCHEDULED">↺ Reopen</button>');
      btns.push('<button type="button" class="btn sm ghost" id="wfx-d-clone">⧉ Duplicate</button>');
      if (isAdmin()) btns.push('<button type="button" class="btn sm danger" id="wfx-d-del">🗑 Delete</button>');
    }
    if (s.study_instance_uid) btns.push('<a class="btn sm viewer" href="' + H(studyHref(s.study_instance_uid)) + '" target="_blank" rel="noopener">◉ Open in viewer</a>');

    var chainHtml = '<div class="wfx-dchain">' +
      '<div class="wfx-dc-step is-on"><b>01 Ordered</b><span>' + (s._created ? fmtDateTime(s._created) : "—") + '</span><small class="mono">' + H(s.sps_id) + (s.patient_source === "his" ? " · HIS" : " · Manual") + "</small></div>" +
      '<div class="wfx-dc-step ' + (s._mpps.length ? "is-on" : "") + '"><b>02 MPPS (' + s._mpps.length + ")</b><span>" + (s._mpps.length ? H(s._mpps[0].status + " @ " + (s._mpps[0].performed_station_aet || "—")) : "Awaiting modality") + "</span>" +
      (s._mpps.length && s._mpps[0]._dur != null ? "<small>Duration " + fmtDur(s._mpps[0]._dur) + (s._delay != null ? " · Start Δ " + fmtDur(s._delay) : "") + "</small>" : "") + "</div>" +
      '<div class="wfx-dc-step ' + (s.study_instance_uid ? "is-on" : "") + '"><b>03 Study</b><span>' + (s.study_instance_uid ? "Reconciled" : "Not linked yet") + '</span><small class="mono wfx-trunc">' + H(s.study_instance_uid || "—") + "</small></div>" +
      "</div>";

    var dqHtml = findings.length ? '<div class="wfx-d-dq"><h4>Data-quality findings (' + findings.length + ")</h4>" + findings.map(function (i) {
      return '<div class="wfx-d-dq-item"><span class="wfx-sev is-' + i.rule.sev + '">' + i.rule.sev.toUpperCase() + '</span><span class="mono">' + H(i.rule.tag) + "</span><span>" + H(i.rule.title) + "</span></div>";
    }).join("") + "</div>" : "";

    openDrawer("Order · " + (s.sps_id || "#" + s.id), (s.patient_name || "Unknown").replace(/\^/g, " "),
      '<div class="wfx-d-top">' + statusPill(s.status) + '<span class="mono wfx-dim">MRN ' + H(s.patient_id || "—") + "</span>" + (s._overdue ? '<span class="wfx-late">' + fmtDur(s._wait) + " overdue</span>" : "") + "</div>" +
      '<div class="wfx-d-acts">' + btns.join("") + "</div>" +
      chainHtml + dqHtml +
      '<form id="wfx-edit-form" class="wfx-form">' +
      '<div class="wfx-fgrid">' +
      '<label class="wfx-f"><span>Modality <small class="mono">(0008,0060)</small></span><input name="modality" list="wfx-dl-modality" maxlength="16" value="' + H(s.modality || "") + '"' + (editable ? "" : " disabled") + " /></label>" +
      '<label class="wfx-f"><span>Station AE Title <small class="mono">(0040,0001)</small></span><input name="scheduled_station_aet" list="wfx-dl-station" maxlength="64" value="' + H(s.scheduled_station_aet || "") + '"' + (editable ? "" : " disabled") + " /></label>" +
      '<label class="wfx-f"><span>Start date <small class="mono">(0040,0002)</small></span><input type="date" name="start_date" value="' + (parseDA(s.start_date) ? fmtDA(s.start_date) : "") + '"' + (editable ? "" : " disabled") + " /></label>" +
      '<label class="wfx-f"><span>Start time <small class="mono">(0040,0003)</small></span><input type="time" name="start_time" value="' + (parseTM(s.start_time) != null ? fmtTM(s.start_time) : "") + '"' + (editable ? "" : " disabled") + " /></label>" +
      '<label class="wfx-f"><span>Accession number <small class="mono">(0008,0050)</small></span><input name="accession_number" maxlength="64" value="' + H(s.accession_number || "") + '"' + (editable ? "" : " disabled") + " /></label>" +
      '<label class="wfx-f"><span>Requested Procedure ID <small class="mono">(0040,1001)</small></span><input name="requested_procedure_id" maxlength="64" value="' + H(s.requested_procedure_id || "") + '"' + (editable ? "" : " disabled") + " /></label>" +
      '<label class="wfx-f wfx-f-full"><span>Procedure description <small class="mono">(0032,1060)</small></span><input name="requested_procedure_description" list="wfx-dl-desc" maxlength="255" value="' + H(s.requested_procedure_description || "") + '"' + (editable ? "" : " disabled") + " /></label>" +
      '<label class="wfx-f wfx-f-full"><span>Performing physician <small class="mono">(0040,0006)</small></span><input name="scheduled_performing_physician" list="wfx-dl-phys" maxlength="192" value="' + H(s.scheduled_performing_physician || "") + '"' + (editable ? "" : " disabled") + " /></label>" +
      "</div>" +
      '<div class="wfx-vr-msg" id="wfx-edit-vr"></div>' +
      (editable ? '<div class="wfx-drawer-foot"><button type="submit" class="btn primary">Save changes</button></div>' : "") +
      "</form>");

    var body = $id("wfx-drawer-body");
    qsa("[data-dact]", body).forEach(function (b) {
      b.addEventListener("click", async function () {
        if (await patchStep(s.id, { status: b.dataset.dact })) { await load(); openOrder(s.id); }
      });
    });
    var cloneBtn = $id("wfx-d-clone");
    if (cloneBtn) cloneBtn.addEventListener("click", function () {
      openNewOrder({ patient_id: s.patient_id, patient_name: s.patient_name, modality: s.modality, scheduled_station_aet: s.scheduled_station_aet, requested_procedure_id: s.requested_procedure_id, requested_procedure_description: s.requested_procedure_description, scheduled_performing_physician: s.scheduled_performing_physician });
    });
    var delBtn = $id("wfx-d-del");
    if (delBtn) delBtn.addEventListener("click", async function () {
      if (!window.confirm("Delete order " + (s.sps_id || s.id) + " permanently?")) return;
      var res = await call("/api/v1/worklist/" + s.id, { method: "DELETE" });
      if (!res.ok) { notify("Delete failed (" + res.status + ")", "err"); return; }
      notify("Order deleted", "ok");
      closeDrawer();
      await load();
    });
    var form = $id("wfx-edit-form");
    form.addEventListener("input", function () { checkFormVr(form, $id("wfx-edit-vr")); });
    checkFormVr(form, $id("wfx-edit-vr"));
    if (editable) form.addEventListener("submit", async function (e) {
      e.preventDefault();
      var fd = new FormData(form), tm = (fd.get("start_time") || "").replace(":", "");
      var payload = {
        modality: String(fd.get("modality") || "").trim(),
        scheduled_station_aet: String(fd.get("scheduled_station_aet") || "").trim(),
        start_date: String(fd.get("start_date") || "").replace(/-/g, ""),
        start_time: tm ? tm + "00" : "",
        accession_number: String(fd.get("accession_number") || "").trim(),
        requested_procedure_id: String(fd.get("requested_procedure_id") || "").trim(),
        requested_procedure_description: String(fd.get("requested_procedure_description") || "").trim(),
        scheduled_performing_physician: String(fd.get("scheduled_performing_physician") || "").trim()
      };
      if (await patchStep(s.id, payload, "Changes saved")) { await load(); openOrder(s.id); }
    });
  }

  async function patchStep(id, body, msg) {
    var res = await call("/api/v1/worklist/" + id, jsonOpts("PATCH", body));
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      notify(err.detail || "Update failed (" + res.status + ")", "err");
      return false;
    }
    notify(msg || (body.status ? "Marked " + (STATUS_LABEL[body.status] || body.status) : "Updated"), "ok");
    return true;
  }

  /* ------------------------------------------------------------ menus & exports */
  function closeMenus() {
    qsa(".wfx-menu-list", $id("view-workflow")).forEach(function (m) { m.hidden = true; });
    qsa(".wfx-menu > button", $id("view-workflow")).forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
    var ctx = document.querySelector(".wfx-ctx"); if (ctx) ctx.remove();
  }
  function renderColsMenu() {
    var box = document.querySelector("#wfx-cols-menu .wfx-cols"); if (!box) return;
    box.innerHTML = COLS.map(function (c) {
      return '<label class="wfx-mcheck"><input type="checkbox" data-col-toggle="' + c.key + '"' + (P.cols[c.key] ? " checked" : "") + " /><span>" + H(c.label) + "</span></label>";
    }).join("");
  }
  var PRESET_VIEWS = [
    { name: "Today's active queue", patch: { status: "SCHEDULED", range: "today", sort: "date-asc", mode: "table" } },
    { name: "Overdue orders", patch: { status: "SCHEDULED", range: "overdue", sort: "wait", mode: "table" } },
    { name: "HIS (HL7) orders", patch: { status: "", range: "all", source: "his", sort: "date-desc", mode: "table" } }
  ];
  function renderViewsMenu() {
    var box = document.querySelector("#wfx-views-menu .wfx-views"); if (!box) return;
    var saved = (P.views || []).map(function (v, idx) {
      return '<div class="wfx-vitem"><button type="button" data-load-view="' + idx + '">' + H(v.name) + '</button><button type="button" class="wfx-vdel" data-del-view="' + idx + '" title="Delete">✕</button></div>';
    }).join("");
    var presets = PRESET_VIEWS.map(function (v, idx) {
      return '<button type="button" role="menuitem" data-preset="' + idx + '">' + H(v.name) + "</button>";
    }).join("");
    box.innerHTML = '<div class="wfx-mhead">Presets</div>' + presets +
      (saved ? '<div class="wfx-mhead">Saved</div>' + saved : "") +
      '<button type="button" class="wfx-msave" data-save-view>+ Save current view…</button>';
  }

  function csvDownload(filename, headers, rows) {
    var cell = function (v) { var s = String(v == null ? "" : v); return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    var lines = [headers.map(cell).join(",")].concat(rows.map(function (r) { return r.map(cell).join(","); }));
    var blob = new Blob(["\uFEFF" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = filename; a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
  }
  function exportData(kind, onlyRows) {
    var stamp = ymd(new Date());
    if (kind === "print") { switchTab("stats"); setTimeout(function () { window.print(); }, 60); return; }
    if (kind === "json") {
      var payload = { exported_at: new Date().toISOString(), window_days: P.s.window, worklist: S.mwl, mpps: S.mpps, commitments: S.commit, analytics: S.stats, quality_score: S.dq.score };
      var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "paxs-workflow-" + stamp + ".json"; a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
      return;
    }
    if (kind === "stats") {
      var d = S.stats.desc, rows = [
        ["Metric", METRIC_LABEL[P.s.metric], "Window (days)", P.s.window || "all"],
        ["n", d.n, "Mean (min)", fmtNum(d.mean, 3)],
        ["SD (min)", fmtNum(d.sd, 3), "CI low", fmtNum(d.ciLo, 3)],
        ["CI high", fmtNum(d.ciHi, 3), "Median (min)", fmtNum(d.median, 3)],
        ["IQR", fmtNum(d.iqr, 3), "P90 (min)", fmtNum(d.p90, 3)],
        ["P95 (min)", fmtNum(d.p95, 3), "Skewness", fmtNum(d.skew, 3)],
        [], ["Date", SPC_LABEL[P.s.spc], "Nelson flags"]
      ];
      S.stats.series.forEach(function (p, i) { rows.push([inputDate(p.d), p.y, S.stats.spc.flags[i].join(";")]); });
      csvDownload("paxs-workflow-stats-" + stamp + ".csv", ["Field", "Value", "Detail", "Value2"], rows);
      return;
    }
    /* csv of current tab or selected rows */
    if (onlyRows || P.tab === "mwl" || P.tab === "stats") {
      var list = onlyRows || filteredMwl(false);
      csvDownload("paxs-worklist-" + stamp + ".csv",
        ["ID", "SPS_ID", "Status", "Patient_ID", "Patient_Name", "Source", "Modality", "Station_AET", "Start_Date", "Start_Time", "Accession", "Procedure_ID", "Description", "Physician", "Study_UID"],
        list.map(function (s) { return [s.id, s.sps_id, s.status, s.patient_id, s.patient_name, s.patient_source, s.modality, s.scheduled_station_aet, s.start_date, s.start_time, s.accession_number, s.requested_procedure_id, s.requested_procedure_description, s.scheduled_performing_physician, s.study_instance_uid]; }));
    } else if (P.tab === "mpps") {
      csvDownload("paxs-mpps-" + stamp + ".csv",
        ["ID", "PPS_ID", "Status", "Patient_ID", "Patient_Name", "Modality", "Station_AET", "Start_Date", "Start_Time", "End_Date", "End_Time", "Duration_Min", "Scheduled_Step_ID", "Study_UID"],
        S.mpps.map(function (p) { return [p.id, p.pps_id, p.status, p.patient_id, p.patient_name, p.modality, p.performed_station_aet, p.start_date, p.start_time, p.end_date, p.end_time, p._dur != null ? p._dur.toFixed(2) : "", p.scheduled_step_id, p.study_instance_uid]; }));
    } else if (P.tab === "commit") {
      csvDownload("paxs-commitments-" + stamp + ".csv",
        ["ID", "Status", "AE_Title", "Transaction_UID", "Verified", "Requested", "Created_At", "Sent_At"],
        S.commit.map(function (c) { return [c.id, c.status, c.ae_title, c.transaction_uid, c.verified_count, c.instance_count, c.created_at, c.sent_at]; }));
    } else if (P.tab === "dq") {
      csvDownload("paxs-data-quality-" + stamp + ".csv",
        ["Severity", "DICOM_Tag", "Rule", "Scope", "Record_ID", "Value"],
        S.dq.live.map(function (i) { return [i.rule.sev, i.rule.tag, i.rule.title, i.kind, i.rec.sps_id || i.rec.pps_id || i.rec.transaction_uid || i.rec.id, i.value]; }));
    }
  }

  function showRowMenu(btn, s) {
    closeMenus();
    var rect = btn.getBoundingClientRect();
    var menu = document.createElement("div");
    menu.className = "wfx-menu-list wfx-ctx";
    menu.setAttribute("role", "menu");
    menu.style.left = Math.min(window.innerWidth - 190, Math.max(8, rect.right - 176)) + "px";
    menu.style.top = Math.min(window.innerHeight - 200, rect.bottom + 4) + "px";
    var items = [
      '<button type="button" role="menuitem" data-mact="open">Inspect / edit</button>',
      canWrite() ? '<button type="button" role="menuitem" data-mact="shift">Reschedule +1 day</button>' : "",
      canWrite() ? '<button type="button" role="menuitem" data-mact="clone">Duplicate order</button>' : "",
      s.study_instance_uid ? '<button type="button" role="menuitem" data-mact="study">Open in viewer</button>' : "",
      canWrite() && s.status !== "CANCELED" ? '<button type="button" role="menuitem" class="danger" data-mact="cancel">Cancel order</button>' : "",
      isAdmin() ? '<button type="button" role="menuitem" class="danger" data-mact="delete">Delete permanently</button>' : ""
    ].filter(Boolean).join("");
    menu.innerHTML = items;
    document.body.appendChild(menu);
    menu.addEventListener("click", async function (e) {
      var b = e.target.closest("[data-mact]"); if (!b) return;
      var act = b.dataset.mact;
      closeMenus();
      if (act === "open") openOrder(s.id);
      else if (act === "study") window.open(studyHref(s.study_instance_uid), "_blank", "noopener");
      else if (act === "clone") openNewOrder({ patient_id: s.patient_id, patient_name: s.patient_name, modality: s.modality, scheduled_station_aet: s.scheduled_station_aet, requested_procedure_id: s.requested_procedure_id, requested_procedure_description: s.requested_procedure_description, scheduled_performing_physician: s.scheduled_performing_physician });
      else if (act === "shift") { var d = parseDA(s.start_date) || new Date(); d.setDate(d.getDate() + 1); if (await patchStep(s.id, { start_date: ymd(d) }, "Moved to " + inputDate(d))) load(); }
      else if (act === "cancel") { if (await patchStep(s.id, { status: "CANCELED" })) load(); }
      else if (act === "delete") {
        if (!window.confirm("Delete order " + (s.sps_id || s.id) + " permanently?")) return;
        var res = await call("/api/v1/worklist/" + s.id, { method: "DELETE" });
        notify(res.ok ? "Order deleted" : "Delete failed (" + res.status + ")", res.ok ? "ok" : "err");
        if (res.ok) load();
      }
    });
  }

  /* ------------------------------------------------------------ sync & wiring */
  function switchTab(tab) {
    if (["mwl", "mpps", "commit", "stats", "dq"].indexOf(tab) < 0) tab = "mwl";
    P.tab = tab; savePrefs();
    qsa(".wfx-tab", $id("view-workflow")).forEach(function (t) { t.setAttribute("aria-selected", String(t.dataset.tab === tab)); });
    qsa(".wfx-pane", $id("view-workflow")).forEach(function (p) { p.hidden = p.dataset.pane !== tab; });
    if (tab === "stats") renderStats();
    else if (tab === "dq") renderDq();
  }
  function setSeg(id, val) {
    qsa("#" + id + " button").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.v === val)); });
  }
  function syncControls() {
    switchTab(P.tab);
    setSeg("wfx-status", P.status);
    setSeg("wfx-mode", P.mode);
    setSeg("wfx-range", P.range);
    setSeg("wfx-mpps-status", P.mppsStatus);
    setSeg("wfx-commit-status", P.commitStatus);
    setSeg("wfx-dq-sev", P.dq.sev);
    $id("wfx-custom").hidden = P.range !== "custom";
    $id("wfx-q").value = P.q || "";
    $id("wfx-source").value = P.source || "";
    $id("wfx-from").value = P.from || "";
    $id("wfx-to").value = P.to || "";
    $id("wfx-sort").value = P.sort;
    $id("wfx-pagesize").value = String(P.pageSize);
    $id("wfx-group").checked = !!P.group;
    $id("wfx-dense").checked = !!P.dense;
    $id("wfx-auto").value = String(P.auto || 0);
    $id("wfx-mpps-link").value = P.mppsLink || "";
    $id("wfx-s-window").value = String(P.s.window);
    $id("wfx-s-metric").value = P.s.metric;
    $id("wfx-s-group").value = P.s.group;
    $id("wfx-s-bins").value = String(P.s.bins);
    $id("wfx-s-conf").value = String(P.s.conf);
    $id("wfx-s-sigma").value = String(P.s.sigma);
    $id("wfx-s-tol").value = String(P.s.tol);
    $id("wfx-s-kde").checked = !!P.s.kde;
    $id("wfx-s-outliers").checked = !!P.s.outliers;
    $id("wfx-s-log").checked = !!P.s.log;
    $id("wfx-dq-stale").value = String(P.dq.stale);
    $id("wfx-dq-ctimeout").value = String(P.dq.ctimeout);
    $id("wfx-dq-scope").value = P.dq.scope || "";
    renderColsMenu();
    renderViewsMenu();
    setAutoTimer();
  }
  function setAutoTimer() {
    if (S.timer) { clearInterval(S.timer); S.timer = null; }
    var sec = +P.auto || 0;
    if (sec > 0) S.timer = setInterval(function () {
      var v = $id("view-workflow");
      if (v && v.classList.contains("active") && !document.hidden) load();
    }, sec * 1000);
  }
  function renderAll() {
    renderUpdated();
    renderPipeline();
    renderKpis();
    renderMwl();
    renderMpps();
    renderCommit();
    if (P.tab === "stats") renderStats();
    if (P.tab === "dq") renderDq();
  }

  function bind() {
    var root = $id("view-workflow"); if (!root) return;
    $id("wfx-new").addEventListener("click", function () { openNewOrder(); });
    $id("wfx-refresh").addEventListener("click", load);
    $id("wfx-auto").addEventListener("change", function (e) { P.auto = +e.target.value; savePrefs(); setAutoTimer(); });
    $id("wfx-drawer-close").addEventListener("click", closeDrawer);
    $id("wfx-scrim").addEventListener("click", closeDrawer);

    /* tabs */
    qsa(".wfx-tab", root).forEach(function (t) { t.addEventListener("click", function () { switchTab(t.dataset.tab); }); });

    /* KPI shortcuts */
    $id("wfx-kpis").addEventListener("click", function (e) {
      var b = e.target.closest(".wfx-kpi"); if (!b) return;
      var k = b.dataset.kpi;
      if (k === "today") { P.status = ""; P.range = "today"; switchTab("mwl"); }
      else if (k === "overdue") { P.status = "SCHEDULED"; P.range = "overdue"; P.sort = "wait"; switchTab("mwl"); }
      else if (k === "progress") { P.status = "IN PROGRESS"; P.range = "all"; switchTab("mwl"); }
      else if (k === "donetoday") { P.status = "COMPLETED"; P.range = "today"; switchTab("mwl"); }
      else if (k === "tat") { P.s.metric = "duration"; switchTab("stats"); }
      else if (k === "ontime") { P.s.metric = "delay"; switchTab("stats"); }
      else if (k === "pending") { switchTab("commit"); }
      else if (k === "quality") { switchTab("dq"); }
      savePrefs(); syncControls(); renderAll();
    });

    /* menus */
    qsa(".wfx-menu > button", root).forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        var list = btn.nextElementSibling, wasOpen = !list.hidden;
        closeMenus();
        list.hidden = wasOpen;
        btn.setAttribute("aria-expanded", String(!wasOpen));
      });
    });
    document.addEventListener("click", function (e) {
      if (!e.target.closest(".wfx-menu") && !e.target.closest(".wfx-ctx")) closeMenus();
    });
    $id("wfx-export-menu").addEventListener("click", function (e) {
      var b = e.target.closest("[data-export]"); if (!b) return;
      closeMenus(); exportData(b.dataset.export);
    });
    $id("wfx-cols-menu").addEventListener("change", function (e) {
      var k = e.target.dataset.colToggle; if (!k) return;
      P.cols[k] = e.target.checked; savePrefs(); renderMwl();
    });
    $id("wfx-cols-menu").addEventListener("click", function (e) { e.stopPropagation(); });
    $id("wfx-views-menu").addEventListener("click", function (e) {
      var pr = e.target.closest("[data-preset]");
      if (pr) { Object.assign(P, PRESET_VIEWS[+pr.dataset.preset].patch); S.page = 1; savePrefs(); syncControls(); renderMwl(); closeMenus(); return; }
      var lv = e.target.closest("[data-load-view]");
      if (lv) { Object.assign(P, P.views[+lv.dataset.loadView].state); S.page = 1; savePrefs(); syncControls(); renderMwl(); closeMenus(); return; }
      var dv = e.target.closest("[data-del-view]");
      if (dv) { e.stopPropagation(); P.views.splice(+dv.dataset.delView, 1); savePrefs(); renderViewsMenu(); return; }
      if (e.target.closest("[data-save-view]")) {
        closeMenus();
        var name = window.prompt("Name for this saved view:");
        if (!name || !name.trim()) return;
        P.views.push({ name: name.trim(), state: { status: P.status, mod: P.mod, station: P.station, source: P.source, range: P.range, from: P.from, to: P.to, sort: P.sort, mode: P.mode, q: P.q } });
        savePrefs(); renderViewsMenu(); notify("Saved view '" + name.trim() + "'", "ok");
      }
    });

    /* MWL filters */
    var segBind = function (id, onPick) {
      $id(id).addEventListener("click", function (e) {
        var b = e.target.closest("button[data-v]"); if (!b) return;
        setSeg(id, b.dataset.v); onPick(b.dataset.v);
      });
    };
    segBind("wfx-status", function (v) { P.status = v; S.page = 1; savePrefs(); renderMwl(); });
    segBind("wfx-mode", function (v) { P.mode = v; savePrefs(); renderMwl(); });
    segBind("wfx-range", function (v) { P.range = v; $id("wfx-custom").hidden = v !== "custom"; S.page = 1; savePrefs(); renderMwl(); });
    $id("wfx-q").addEventListener("input", function (e) { P.q = e.target.value; S.page = 1; savePrefs(); renderMwl(); });
    $id("wfx-mod").addEventListener("change", function (e) { P.mod = e.target.value; S.page = 1; savePrefs(); renderMwl(); });
    $id("wfx-station").addEventListener("change", function (e) { P.station = e.target.value; S.page = 1; savePrefs(); renderMwl(); });
    $id("wfx-source").addEventListener("change", function (e) { P.source = e.target.value; S.page = 1; savePrefs(); renderMwl(); });
    $id("wfx-from").addEventListener("change", function (e) { P.from = e.target.value; S.page = 1; savePrefs(); renderMwl(); });
    $id("wfx-to").addEventListener("change", function (e) { P.to = e.target.value; S.page = 1; savePrefs(); renderMwl(); });
    $id("wfx-sort").addEventListener("change", function (e) { P.sort = e.target.value; S.page = 1; savePrefs(); renderMwl(); });
    $id("wfx-pagesize").addEventListener("change", function (e) { P.pageSize = +e.target.value; S.page = 1; savePrefs(); renderMwl(); });
    $id("wfx-group").addEventListener("change", function (e) { P.group = e.target.checked; savePrefs(); renderMwl(); });
    $id("wfx-dense").addEventListener("change", function (e) { P.dense = e.target.checked; savePrefs(); renderMwl(); });
    $id("wfx-clear").addEventListener("click", function () {
      Object.assign(P, { status: "", mod: "", station: "", source: "", range: "all", from: "", to: "", q: "" });
      S.page = 1; savePrefs(); syncControls(); renderMwl();
    });

    /* MWL table / board / timeline delegation */
    var mwlHost = $id("wfx-mwl");
    mwlHost.addEventListener("change", function (e) {
      if (e.target.id === "wfx-selall") {
        qsa("[data-sel]", mwlHost).forEach(function (cb) { cb.checked = e.target.checked; if (e.target.checked) S.sel.add(+cb.dataset.sel); else S.sel.delete(+cb.dataset.sel); });
        renderMwl(); return;
      }
      if (e.target.dataset.sel) {
        var id = +e.target.dataset.sel;
        if (e.target.checked) S.sel.add(id); else S.sel.delete(id);
        var row = e.target.closest(".wfx-row"); if (row) row.classList.toggle("is-sel", e.target.checked);
        updateBulk();
      }
      if (e.target.id === "wfx-tl-day") { P.tlDay = e.target.value; savePrefs(); renderMwl(); }
    });
    mwlHost.addEventListener("click", async function (e) {
      if (e.target.closest("[data-retry]")) { load(); return; }
      var tlBtn = e.target.closest("[data-tl]");
      if (tlBtn) {
        var cur = P.tlDay ? new Date(P.tlDay + "T00:00") : startOfDay(new Date());
        if (tlBtn.dataset.tl === "today") cur = startOfDay(new Date());
        else cur.setDate(cur.getDate() + (tlBtn.dataset.tl === "next" ? 1 : -1));
        P.tlDay = inputDate(cur); savePrefs(); renderMwl(); return;
      }
      if (e.target.closest(".wfx-sel") || e.target.closest("a.wfx-act")) return;
      var actBtn = e.target.closest("[data-act]");
      var row = e.target.closest("[data-id]"); if (!row) return;
      var id = +row.dataset.id, step = S.mwl.find(function (s) { return s.id === id; }); if (!step) return;
      if (actBtn) {
        var a = actBtn.dataset.act;
        if (a === "menu") { showRowMenu(actBtn, step); return; }
        var map = { start: "IN PROGRESS", complete: "COMPLETED", reopen: "SCHEDULED" };
        if (map[a] && await patchStep(id, { status: map[a] })) load();
        return;
      }
      openOrder(id);
    });
    /* Drag & drop on Kanban board */
    mwlHost.addEventListener("dragstart", function (e) {
      var card = e.target.closest(".wfx-card[data-id]"); if (!card) return;
      e.dataTransfer.setData("text/plain", card.dataset.id);
      card.classList.add("is-drag");
    });
    mwlHost.addEventListener("dragend", function (e) { var c = e.target.closest(".wfx-card"); if (c) c.classList.remove("is-drag"); qsa(".wfx-lane.is-over", mwlHost).forEach(function (l) { l.classList.remove("is-over"); }); });
    mwlHost.addEventListener("dragover", function (e) { var lane = e.target.closest(".wfx-lane[data-drop]"); if (lane && canWrite()) { e.preventDefault(); lane.classList.add("is-over"); } });
    mwlHost.addEventListener("dragleave", function (e) { var lane = e.target.closest(".wfx-lane[data-drop]"); if (lane && !lane.contains(e.relatedTarget)) lane.classList.remove("is-over"); });
    mwlHost.addEventListener("drop", async function (e) {
      var lane = e.target.closest(".wfx-lane[data-drop]"); if (!lane || !canWrite()) return;
      e.preventDefault(); lane.classList.remove("is-over");
      var id = +e.dataTransfer.getData("text/plain"), st = lane.dataset.drop;
      var step = S.mwl.find(function (s) { return s.id === id; });
      if (step && step.status !== st && await patchStep(id, { status: st })) load();
    });

    /* Pagination */
    $id("wfx-pager").addEventListener("click", function (e) {
      var b = e.target.closest("[data-pg]"); if (!b) return;
      S.page += b.dataset.pg === "next" ? 1 : -1;
      renderMwl();
    });

    /* Bulk actions */
    $id("wfx-bulk").addEventListener("click", async function (e) {
      var b = e.target.closest("[data-bulk]"); if (!b) return;
      var act = b.dataset.bulk, ids = Array.from(S.sel);
      if (act === "NONE") { S.sel.clear(); renderMwl(); return; }
      if (act === "EXPORT") { exportData("csv", S.mwl.filter(function (s) { return S.sel.has(s.id); })); return; }
      if (!ids.length) return;
      if (act === "DELETE") {
        if (!window.confirm("Delete " + ids.length + " selected order(s) permanently?")) return;
        var okD = 0;
        for (var i = 0; i < ids.length; i++) { if ((await call("/api/v1/worklist/" + ids[i], { method: "DELETE" })).ok) okD++; }
        notify("Deleted " + okD + " of " + ids.length, okD === ids.length ? "ok" : "err");
        S.sel.clear(); await load(); return;
      }
      if (act === "SHIFT") {
        var okS = 0;
        for (var j = 0; j < ids.length; j++) {
          var s = S.mwl.find(function (r) { return r.id === ids[j]; });
          var d = (s && parseDA(s.start_date)) || new Date(); d.setDate(d.getDate() + 1);
          if ((await call("/api/v1/worklist/" + ids[j], jsonOpts("PATCH", { start_date: ymd(d) }))).ok) okS++;
        }
        notify("Shifted " + okS + " of " + ids.length + " by +1 day", "ok");
        await load(); return;
      }
      var ok = 0;
      for (var k = 0; k < ids.length; k++) {
        if ((await call("/api/v1/worklist/" + ids[k], jsonOpts("PATCH", { status: act }))).ok) ok++;
      }
      notify("Updated " + ok + " of " + ids.length, ok === ids.length ? "ok" : "err");
      S.sel.clear(); await load();
    });

    /* MPPS & Commitment filters + step links */
    segBind("wfx-mpps-status", function (v) { P.mppsStatus = v; savePrefs(); renderMpps(); });
    $id("wfx-mpps-q").addEventListener("input", renderMpps);
    $id("wfx-mpps-link").addEventListener("change", function (e) { P.mppsLink = e.target.value; savePrefs(); renderMpps(); });
    $id("wfx-mpps-mod").addEventListener("change", function (e) { P.mppsMod = e.target.value; savePrefs(); renderMpps(); });
    segBind("wfx-commit-status", function (v) { P.commitStatus = v; savePrefs(); renderCommit(); });
    $id("wfx-commit-q").addEventListener("input", renderCommit);
    $id("wfx-commit-ae").addEventListener("change", function (e) { P.commitAe = e.target.value; savePrefs(); renderCommit(); });

    root.addEventListener("click", function (e) {
      var st = e.target.closest("[data-open-step]");
      if (st) openOrder(+st.dataset.openStep);
      if (e.target.closest("#wfx-mpps [data-retry], #wfx-commit [data-retry]")) load();
    });

    /* Analytics controls */
    var statsRecompute = function () { savePrefs(); S.stats = computeStats(); renderPipeline(); renderKpis(); renderStats(); };
    $id("wfx-stats-controls").addEventListener("change", function () {
      P.s.window = +$id("wfx-s-window").value;
      P.s.metric = $id("wfx-s-metric").value;
      P.s.group = $id("wfx-s-group").value;
      P.s.bins = $id("wfx-s-bins").value;
      P.s.conf = +$id("wfx-s-conf").value;
      P.s.sigma = +$id("wfx-s-sigma").value;
      P.s.tol = +$id("wfx-s-tol").value;
      P.s.kde = $id("wfx-s-kde").checked;
      P.s.outliers = $id("wfx-s-outliers").checked;
      P.s.log = $id("wfx-s-log").checked;
      statsRecompute();
    });
    $id("wfx-stats").addEventListener("change", function (e) {
      if (e.target.id === "wfx-s-spc") { P.s.spc = e.target.value; statsRecompute(); }
      if (e.target.id === "wfx-s-heat") { P.s.heat = e.target.value; statsRecompute(); }
    });

    /* Data quality controls */
    segBind("wfx-dq-sev", function (v) { P.dq.sev = v; savePrefs(); renderDq(); });
    $id("wfx-dq-stale").addEventListener("change", function (e) { P.dq.stale = +e.target.value; savePrefs(); S.dq = audit(); renderKpis(); renderDq(); });
    $id("wfx-dq-ctimeout").addEventListener("change", function (e) { P.dq.ctimeout = +e.target.value; savePrefs(); S.dq = audit(); renderKpis(); renderDq(); });
    $id("wfx-dq-scope").addEventListener("change", function (e) { P.dq.scope = e.target.value; savePrefs(); renderDq(); });
    $id("wfx-dq").addEventListener("click", function (e) {
      var m = e.target.closest("[data-mute]"); if (!m) return;
      var id = m.dataset.mute, idx = P.dq.muted.indexOf(id);
      if (idx >= 0) P.dq.muted.splice(idx, 1); else P.dq.muted.push(id);
      savePrefs(); S.dq = audit(); renderKpis(); renderDq();
    });

    /* Keyboard shortcuts */
    document.addEventListener("keydown", function (e) {
      if (!root.classList.contains("active")) return;
      if (e.key === "Escape") { closeMenus(); closeDrawer(); return; }
      var tag = (e.target && e.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.ctrlKey || e.metaKey || e.altKey) return;
      if ($id("wfx-drawer").classList.contains("is-open")) return;
      if (e.key === "/") { e.preventDefault(); switchTab("mwl"); $id("wfx-q").focus(); }
      else if (e.key === "n" || e.key === "N") { e.preventDefault(); openNewOrder(); }
      else if (e.key === "r" || e.key === "R") { e.preventDefault(); load(); }
      else if ("12345".indexOf(e.key) >= 0) { switchTab(["mwl", "mpps", "commit", "stats", "dq"][+e.key - 1]); }
      else if ((e.key === "j" || e.key === "k") && P.tab === "mwl") {
        var rows = qsa(".wfx-row[data-id]", mwlHost); if (!rows.length) return;
        S.focusIdx = Math.max(0, Math.min(rows.length - 1, (S.focusIdx < 0 ? 0 : S.focusIdx + (e.key === "j" ? 1 : -1))));
        rows[S.focusIdx].focus();
      }
    });
    mwlHost.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && e.target.classList.contains("wfx-row") && e.target.dataset.id) openOrder(+e.target.dataset.id);
    });
  }

  /* ------------------------------------------------------------ bootstrap */
  function init() {
    if (!$id("view-workflow")) return;
    if ($id("wfx-scrim")) document.body.appendChild($id("wfx-scrim"));
    if ($id("wfx-drawer")) document.body.appendChild($id("wfx-drawer"));
    syncControls();
    bind();
    if ($id("view-workflow").classList.contains("active")) load();
  }
  window.PaxsWorkflow = { load: load };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
