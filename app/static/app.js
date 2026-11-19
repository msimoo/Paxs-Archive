/* PAXS Archive dashboard */
"use strict";

const $ = (id) => document.getElementById(id);
const COLUMN_DEFS = [
  { key: "patient",   label: "Patient" },
  { key: "study",     label: "Study" },
  { key: "accession", label: "Accession" },
  { key: "series",    label: "Series" },
  { key: "modality",  label: "Modalities" },
  { key: "actions",   label: "Actions" },
];
function archivePatientName(value) {
  const stored = value?.trim() || "";
  if (state.patientNameDisplay === "remove_carets") return stored.replace(/\^/g, "").trim();
  if (state.patientNameDisplay === "structured") {
    const group = stored.split("=").find((part) => part.replace(/\^/g, "").trim());
    return group?.split("^").map((part) => part.trim()).filter(Boolean).join(", ") || "";
  }
  return stored;
}
function defaultCols() { return Object.fromEntries(COLUMN_DEFS.map((c) => [c.key, true])); }
function loadCols() {
  try {
    const raw = JSON.parse(localStorage.getItem("paxs_studies_cols") || "null");
    if (raw && typeof raw === "object") return { ...defaultCols(), ...raw };
  } catch (_) { /* corrupt storage */ }
  return defaultCols();
}

const state = { page: 1, limit: 20, total: 0, token: localStorage.getItem("paxs_token") || null, authMode: "login", deleting: null, sort: localStorage.getItem("paxs_studies_sort") || "date", order: localStorage.getItem("paxs_studies_order") || "desc", state: "", critical: false, reported: false, oru: false, his: false, density: localStorage.getItem("paxs_studies_density") || "comfortable", cols: loadCols(), _studiesAbort: null };

function persistStudiesPrefs() {
  try {
    localStorage.setItem("paxs_studies_sort", state.sort);
    localStorage.setItem("paxs_studies_order", state.order);
    localStorage.setItem("paxs_studies_density", state.density);
    localStorage.setItem("paxs_studies_cols", JSON.stringify(state.cols));
  } catch (_) { /* private mode / quota */ }
}

const MOD_COLORS = {
  CT: "#38bdf8", MR: "#a78bfa", CR: "#fbbf24", DX: "#fb923c",
  US: "#34d399", NM: "#fb7185", PT: "#f472b6", XA: "#22d3ee",
  MG: "#e879f9", OT: "#94a3b8",
};

let _refreshing = null;
async function silentRefresh() {
  // Rotate the refresh cookie once and swap in the new access token. Guarded
  // so concurrent 401s share one refresh instead of rotating in a loop.
  if (_refreshing) return _refreshing;
  _refreshing = (async () => {
    try {
      const r = await fetch("/api/v1/auth/refresh", {
        method: "POST", credentials: "same-origin",
      });
      if (!r.ok) return false;
      const d = await r.json();
      state.token = d.access_token;
      localStorage.setItem("paxs_token", state.token);
      return true;
    } catch (e) {
      return false;
    } finally {
      _refreshing = null;
    }
  })();
  return _refreshing;
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  const fetchOpts = { ...opts, headers };
  let res = await fetch(path, fetchOpts);
  if (res.status === 401 && state.token) {
    // Access token expired (15 min) — try one silent refresh, then retry.
    if (await silentRefresh()) {
      headers["Authorization"] = `Bearer ${state.token}`;
      res = await fetch(path, { ...opts, headers });
      if (res.status !== 401) return res;
    }
    // Session truly gone (revoked / expired) — lock the app back behind login.
    state.token = null; localStorage.removeItem("paxs_token");
    enterGate();
  }
  return res;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

function fmtBytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtDate(d) {
  if (!d) return "—";
  return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- theme ---------------- */
const THEME_KEY = "paxs_theme";

function applyTheme(theme, opts = {}) {
  const next = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  if (opts.persist) {
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* storage unavailable */ }
  }
  const btn = $("btn-theme");
  if (btn) {
    btn.title = next === "dark" ? "Switch to light clinical mode" : "Switch to dark reading-room mode";
    btn.setAttribute("aria-label", btn.title);
  }
  if (!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
    document.documentElement.classList.add("switching");
    clearTimeout(applyTheme._t);
    applyTheme._t = setTimeout(() => document.documentElement.classList.remove("switching"), 320);
  }
}

/* ---------------- triage ---------------- */
async function loadTriage() {
  const el = $("triage");
  if (!state.token) { el.hidden = true; return; }
  const res = await api("/api/v1/triage");
  if (!res.ok) { el.hidden = true; return; }
  const flags = await res.json();
  if (!flags.length) { el.hidden = true; return; }
  el.hidden = false;
  $("triage-count").textContent = `${flags.length} finding${flags.length === 1 ? "" : "s"} — click to review`;
  $("triage-list").innerHTML = flags.map((f) => `
    <div class="triage-item sev-${esc(f.severity.toLowerCase())}">
      <span class="triage-sev">${esc(f.severity)}</span>
      <div class="triage-body">
        <div class="triage-title">${esc(f.finding)} · ${esc(f.patient_name || "?")} (${esc(f.patient_id || "—")})</div>
        <div class="triage-summary">${esc(f.summary || "")} <span class="mono">conf ${(f.confidence * 100).toFixed(0)}%</span></div>
      </div>
      <button class="btn sm" data-review="${f.id}">Reviewed ✓</button>
    </div>`).join("");
  [...document.querySelectorAll("[data-review]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/triage/${b.dataset.review}/review`, { method: "POST" });
      if (res.ok) { toast("Finding marked reviewed", "ok"); loadTriage(); }
      else toast(`Review failed (${res.status})`, "err");
    }));
}

/* ---------------- load data ---------------- */
async function loadStats() {
  try {
    const res = await api("/api/v1/stats");
    if (!res.ok) return;
    const s = await res.json();
    $("st-patients").textContent = s.patients;
    $("st-studies").textContent = s.studies;
    $("st-series").textContent = s.series;
    $("st-instances").textContent = s.instances;
    $("st-storage").textContent = fmtBytes(s.storage_bytes);
    loadFeatures(s);
  } catch (err) {
    console.warn("[PAXS] Failed to load stats:", err.message);
  }
}

async function loadModalities() {
  if ($("f-modality").options.length > 1) return;
  const res = await api("/dicom-web/series?includefield=all");
  if (!res.ok) return;
  const rows = await res.json();
  const set = new Set();
  rows.forEach((r) => { const v = r["00080060"]?.Value?.[0]; if (v) set.add(v); });
  [...set].sort().forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m; opt.textContent = m;
    $("f-modality").appendChild(opt);
  });
}

async function loadTenantFilter() {
  const el = $("f-tenant");
  if (!el) return;
  // The backend only honors ?tenant= for admins (include_all scope); other
  // users are force-scoped to their own tenant, so keep the control hidden.
  if (state.role !== "ADMIN") return;
  if (el.dataset.loaded) return;
  el.dataset.loaded = "1";
  const res = await api("/api/v1/tenants");
  if (!res.ok) return;
  const tenants = await res.json();
  if (!tenants.length) return;
  el.hidden = false;
  tenants.forEach((t) => {
    const opt = document.createElement("option");
    opt.value = String(t.id); opt.textContent = t.name;
    el.appendChild(opt);
  });
}

async function loadStudies() {
  /* Cancel any in-flight studies request from a previous search/filter change
     to prevent stale results from overwriting newer data. */
  if (state._studiesAbort) { state._studiesAbort.abort(); }
  const ctrl = new AbortController();
  state._studiesAbort = ctrl;

  const params = new URLSearchParams({ page: state.page, limit: state.limit });
  if (state.sort) params.set("sort", state.sort);
  if (state.order) params.set("order", state.order);
  if (state.state) params.set("state", state.state);
  if (state.critical) params.set("critical", "1");
  if (state.reported) params.set("reported", "1");
  if (state.oru) params.set("oru", "1");
  if (state.his) params.set("his", "1");
  const search = $("search").value.trim();
  if (search) params.set("search", search);
  const mod = $("f-modality").value;
  if (mod) params.set("modality", mod);
  const tenantEl = $("f-tenant");
  if (tenantEl && tenantEl.value) params.set("tenant", tenantEl.value);
  if ($("f-from").value) params.set("from", $("f-from").value.replace(/-/g, ""));
  if ($("f-to").value) params.set("to", $("f-to").value.replace(/-/g, ""));

  try {
    const res = await api(`/api/v1/studies?${params}`, { signal: ctrl.signal });
    if (!res.ok) { $("study-list").innerHTML = '<div class="empty-state"><p>Failed to load studies.</p></div>'; return; }
    const data = await res.json();
    state.total = data.total;
    renderStatusChips(data.counts);
    renderSortIndicators(data.sort, data.order);
    renderStudies(data.items);
  } catch (err) {
    if (err.name === "AbortError") return;  /* intentionally cancelled — newer request in flight */
    console.warn("[PAXS] Failed to load studies:", err.message);
    $("study-list").innerHTML = '<div class="empty-state"><p>Failed to load studies.</p></div>';
  }
}

function renderStatusChips(counts) {
  if (!counts) return;
  $("cnt-total").textContent = counts.total ?? 0;
  $("cnt-online").textContent = counts.online ?? 0;
  $("cnt-archived").textContent = counts.archived ?? 0;
  $("cnt-critical").textContent = counts.critical ?? 0;
  $("cnt-reported").textContent = counts.reported ?? 0;
  $("cnt-oru").textContent = counts.oru_sent ?? 0;
  $("cnt-his").textContent = counts.his ?? 0;
}

function renderSortIndicators(sort, order) {
  [...document.querySelectorAll(".sort-btn")].forEach((b) => {
    const on = b.dataset.sort === sort;
    b.classList.toggle("on", on);
    const s = b.querySelector(".srt");
    if (s) s.textContent = on ? (order === "asc" ? "▲" : "▼") : "";
  });
}

/* Column visibility (Studies list) — persisted in localStorage. */
const COLUMN_WIDTHS = { patient: "1.4fr", study: "1.2fr", accession: "0.9fr", series: "0.7fr", modality: "0.9fr", actions: "96px" };
function applyColumns() {
  const view = $("view-studies");
  if (!view) return;
  const visible = COLUMN_DEFS.filter((c) => state.cols[c.key]).map((c) => c.key);
  const template = "64px " + visible.map((k) => COLUMN_WIDTHS[k] || "1fr").join(" ");
  view.style.setProperty("--studies-cols", template);
  [...document.querySelectorAll("#view-studies [data-col]")].forEach((el) => {
    el.style.display = state.cols[el.dataset.col] ? "" : "none";
  });
  [...document.querySelectorAll("#cols-menu input[data-colkey]")].forEach((cb) => {
    cb.checked = !!state.cols[cb.dataset.colkey];
  });
}

/* Row density (comfortable / compact) — persisted in localStorage. */
function applyDensity() {
  const view = $("view-studies");
  if (!view) return;
  view.classList.toggle("density-compact", state.density === "compact");
  const btn = $("density-toggle");
  if (btn) {
    btn.title = state.density === "compact"
      ? "Compact rows — switch to comfortable"
      : "Comfortable rows — switch to compact";
    btn.classList.toggle("active", state.density === "compact");
    btn.innerHTML = state.density === "compact"
      ? "≡ Compact" : "⫶ Comfortable";
  }
}

/* ---------------- PaxsViewer (PaxsViewer v3 fork) ---------------- */
window.PAXS_VIEWER = false; // becomes true when /viewer is served (dist present)

async function checkViewer() {
  try {
    const res = await fetch("/viewer/viewer-version.txt", { cache: "no-store" });
    if (!res.ok) return;
    const ver = (await res.text()).trim() || "?";
    window.PAXS_VIEWER = true;
    const btn = $("btn-viewer");
    if (btn) { btn.hidden = false; btn.title = `Open the PaxsViewer v${ver} — DICOMweb study browser`; }
  } catch (e) { /* viewer not served — hide the entry points */ }
}

async function checkAdminPanel() {
  try {
    const res = await fetch("/admin/admin-version.txt", { cache: "no-store" });
    if (!res.ok) return;
    const ver = (await res.text()).trim() || "?";
    window.PAXS_ADMIN = true;
    const btn = $("btn-admin");
    if (btn) { btn.hidden = false; btn.title = `Open the PaxsViewer Control Panel v${ver} — monitor &amp; manage the Viewers/Orthanc services`; }
  } catch (e) { /* admin panel not served — hide the entry point */ }
}

function viewerUrl(studyUid) {
  // The one place that builds PaxsViewer study deep-links. Uses the configured
  // PUBLIC_BASE_URL (surfaced via /api/v1/health) so links resolve to the same
  // public origin no matter which host the dashboard was opened from
  // (localhost vs 127.0.0.1 vs a reverse-proxy hostname), and falls back to the
  // current origin until the health payload lands. Deep-links go straight to
  // the viewer mode route (/viewer/viewer) which opens the study directly.
  //
  // V2-4: the fallback also carries the sub-path. location.origin alone drops
  // any path prefix, so behind https://host/paxs/ the link would point at
  // https://host/viewer/viewer (404). PAXS_BASE_PATH is injected into the shell
  // by the server; the server-provided PUBLIC_BASE_URL already includes it.
  const prefix = (window.PAXS_BASE_PATH || '').replace(/\/+$/, "");
  const base = (window.PAXS_PUBLIC_BASE_URL || (location.origin + prefix)).replace(/\/+$/, "");
  return `${base}/viewer/viewer?StudyInstanceUIDs=${encodeURIComponent(studyUid)}`;
}

function viewerLink(studyUid) {
  return window.PAXS_VIEWER
    ? `<button class="btn sm viewer" title="Open this study in PaxsViewer (DICOMweb)" onclick="location.href=viewerUrl('${studyUid}')">Viewer</button>`
    : "";
}

function modalityChips(mods) {
  if (!mods || !mods.length) return '<span class="chips"><span class="chip">—</span></span>';
  return `<span class="chips">${mods.map((m) =>
    `<span class="chip mod" style="color:${MOD_COLORS[m] || "#94a3b8"};border-color:${MOD_COLORS[m] || "#94a3b8"}44">${esc(m)}</span>`).join("")}</span>`;
}

function renderStudies(items) {
  const list = $("study-list");
  if (!items.length) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="big">🩻</div>
        <p><b>No studies yet</b></p>
        <p>Send images from a modality with C-STORE (port 10400),</p>
        <p>or upload a DICOM file here with the Upload button.</p>
        <p class="hint">storescu -aet TEST_SCU -aec PAXS_SCP 127.0.0.1 10400 file.dcm</p>
      </div>`;
    $("pager").hidden = true;
    return;
  }
  list.innerHTML = items.map((s) => {
    const mods = s.modalities || [];
    const modCode = mods.length ? esc(mods[0]) : "DCM";
    const modColor = MOD_COLORS[mods[0]] || "#94a3b8";
    const thumb = s.thumbnail
      ? `<div class="thumb-wrap"><div class="thumb-skel" aria-hidden="true"></div><img class="thumb" src="${s.thumbnail}" loading="lazy" alt="" onload="this.previousElementSibling.remove();this.classList.add('loaded')" onerror="this.previousElementSibling.remove();this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb empty',innerHTML:'NO<br/>IMG'}))" /><span class="thumb-hint">View</span><span class="mod-tag" style="--mod:${modColor}">${modCode}</span></div>`
      : `<div class="thumb-wrap"><div class="thumb empty">NO<br/>IMG</div><span class="mod-tag" style="--mod:${modColor}">${modCode}</span></div>`;
    const stateBadge = s.lifecycle_state === "ARCHIVED"
      ? '<span class="state-badge archived" title="Cold storage tier">❄ ARCHIVED</span>'
      : s.lifecycle_state === "ONLINE"
        ? '<span class="state-badge online" title="Hot storage tier">● ONLINE</span>'
        : "";
    // Radiology report state badge — click to open the report.
    const repBadge = s.report_status
      ? `<a class="report-badge ${s.report_status.toLowerCase()}${s.report_status === "FINALIZED" && s.oru_status === "SENT" ? " sent" : ""}${s.report_status === "FINALIZED" && s.oru_status === "FAILED" ? " fail" : ""}" title="Open the radiology report" onclick="openReport('${encodeURIComponent(s.study_uid)}', '${esc(s.patient_name || "Patient")}')">${s.report_status === "FINALIZED" ? (s.oru_status === "SENT" ? "📄 EMR ✓" : s.oru_status === "FAILED" ? "📄 EMR ✕" : "📄 FINALIZED") : "📝 DRAFT"}</a>`
      : "";
    return `
    <div class="study-row" style="--mod:${modColor};animation-delay:${Math.min((state.page - 1) * 20, 300)}ms">
      ${thumb}
      <div class="cell-main" data-col="patient">
        <div class="name">${esc(archivePatientName(s.patient_name)) || "Unknown patient"}${s.patient_source === "his" ? '<span class="his-badge" title="Patient registered by the hospital HIS (HL7 ADT)">HIS</span>' : ""}${s.tenant_name ? `<span class="vna-badge" title="VNA tenant — vendor-neutral archive isolation">${esc(s.tenant_name)}</span>` : ""}</div>
        <div class="sub mono">${esc(s.patient_id || "—")}</div>
      </div>
      <div class="cell-main" data-col="study">
        <div class="desc">${esc(s.description) || "—"}${stateBadge}${repBadge}</div>
        <div class="sub">${fmtDate(s.study_date)}</div>
      </div>
      <div class="cell col-acc mono" data-col="accession"><span class="acc-chip">${esc(s.accession) || "—"}</span></div>
      <div class="cell col-series" data-col="series"><span class="counts"><b>${s.n_series}</b><small>ser</small><i>×</i><b>${s.n_instances}</b><small>img</small></span></div>
      <div class="cell" data-col="modality">${modalityChips(s.modalities)}</div>
      <div class="actions" data-col="actions">
        ${viewerLink(s.study_uid)}
        <button class="icon-btn" title="Open in viewer" onclick="location.href=viewerUrl('${s.study_uid}')">👁</button>
        <button class="icon-btn" title="Send study to a DICOM node (C-STORE SCU)" onclick="openForward('${encodeURIComponent(s.study_uid)}', '${esc(s.patient_name || "Patient")}')">➤</button>
        <button class="icon-btn" title="Create patient share link" onclick="createShare('${encodeURIComponent(s.study_uid)}', '${esc(s.patient_name || "Patient")}')">🔗</button>
        <button class="icon-btn danger" title="Delete study" onclick="confirmDelete('${encodeURIComponent(s.study_uid)}', '${esc(s.patient_name || "study")}')">🗑</button>
      </div>
    </div>`;
  }).join("");

  $("pager").hidden = !items.length;
  const pages = Math.max(1, Math.ceil(state.total / state.limit));
  $("pg-info").textContent = `Page ${state.page} of ${pages} · ${state.total} studies`;
  $("pg-prev").disabled = state.page <= 1;
  $("pg-next").disabled = state.page >= pages;
  applyColumns();
}

/* ---------------- terms & privacy (footer popup) ---------------- */
let _termsCache = null;

function inlineMd(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function mdToHtml(md) {
  const out = [];
  let inList = false;
  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  for (const raw of String(md || "").split(/\r?\n/)) {
    const t = raw.trim();
    if (!t) { closeList(); continue; }
    if (/^###\s+/.test(t)) { closeList(); out.push(`<h3>${inlineMd(t.slice(4))}</h3>`); }
    else if (/^##\s+/.test(t)) { closeList(); out.push(`<h2>${inlineMd(t.slice(3))}</h2>`); }
    else if (/^-{3,}\s*$/.test(t)) { closeList(); out.push("<hr/>"); }
    else if (/^\*\s+/.test(t)) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inlineMd(t.slice(2))}</li>`);
    }
    else if (/^\d+\.\s+/.test(t)) { closeList(); out.push(`<p class="md-num">${inlineMd(t.replace(/^\d+\.\s+/, ""))}</p>`); }
    else { closeList(); out.push(`<p>${inlineMd(t)}</p>`); }
  }
  closeList();
  return out.join("\n");
}

async function openTerms(tab = "terms") {
  $("terms-modal").hidden = false;
  if (!_termsCache) {
    const body = $("terms-body");
    body.innerHTML = '<p class="sub">Loading…</p>';
    try {
      const res = await fetch("/terms", { cache: "no-store" });
      if (!res.ok) { body.innerHTML = `<p class="sub">Terms unavailable (${res.status})</p>`; return; }
      // The document is split by horizontal rules: [Terms, Privacy, footer spec].
      const parts = (await res.text()).split(/\r?\n-{3,}\r?\n/).map((p) => mdToHtml(p.trim()));
      _termsCache = { terms: parts[0] || "", privacy: parts[1] || parts[0] || "" };
    } catch (e) {
      body.innerHTML = '<p class="sub">Terms unavailable — check the network connection.</p>';
      return;
    }
  }
  setTermsTab(tab);
}

function setTermsTab(tab) {
  const key = tab === "privacy" ? "privacy" : "terms";
  $("terms-body").innerHTML = _termsCache[key];
  [...document.querySelectorAll(".terms-tab")].forEach((b) => {
    const on = b.dataset.termsTab === key;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
}

/* ---------------- radiology reports ---------------- */
async function openReport(studyUid, patientName) {
  if (!state.token) { toast("Sign in to open reports", "err"); openLogin(); return; }
  const uid = decodeURIComponent(studyUid);
  $("report-modal").hidden = false;
  $("report-meta").textContent = `${patientName} — loading report…`;
  $("report-body").innerHTML = "";
  const res = await api(`/api/v1/reports?study_uid=${encodeURIComponent(uid)}`);
  if (!res.ok) { $("report-meta").textContent = `Failed to load report (${res.status})`; return; }
  const reports = await res.json();
  if (!reports.length) {
    $("report-meta").textContent = `${patientName} — no report yet`;
    $("report-body").innerHTML = `<p class="sub">This study has no radiology report. Draft one through the reports API.</p>`;
    return;
  }
  const r = reports[0];
  $("report-meta").textContent = `${patientName}${r.patient_id ? ` (${r.patient_id})` : ""} · ${r.study_instance_uid || uid}`;
  const head = r.status === "FINALIZED"
    ? '<span class="wl-status done">✓ FINALIZED</span>'
    : '<span class="wl-status open">DRAFT</span>';
  const delivery = r.status === "FINALIZED"
    ? (r.oru_status === "SENT" ? '<span class="wl-status done">→ EMR ✓</span>'
       : r.oru_status === "FAILED" ? '<span class="wl-status cx">→ EMR ✕</span>'
       : '<span class="wl-status open">→ EMR pending</span>')
    : "";
  $("report-body").innerHTML = `
    <div class="report-head">${head} ${delivery}
      ${r.radiologist ? `<span class="sub">by ${esc(r.radiologist)}</span>` : ""}
      ${r.finalized_at ? `<span class="sub mono">finalized ${new Date(r.finalized_at).toLocaleString()}</span>` : ""}
      ${r.oru_sent_at ? `<span class="sub mono">ORU ${new Date(r.oru_sent_at).toLocaleString()}</span>` : ""}
    </div>
    ${r.clinical_history ? `<div class="report-sec"><b>Clinical history</b><p>${esc(r.clinical_history)}</p></div>` : ""}
    ${r.findings ? `<div class="report-sec"><b>Findings</b><p>${esc(r.findings)}</p></div>` : ""}
    ${r.impression ? `<div class="report-sec"><b>Impression</b><p>${esc(r.impression)}</p></div>` : ""}
    ${r.oru_error ? `<div class="report-sec err"><b>EMR delivery error</b><p>${esc(r.oru_error)}</p></div>` : ""}
    ${!r.clinical_history && !r.findings && !r.impression ? `<p class="sub">No content in this report yet.</p>` : ""}
  `;
}

/* ---------------- share ---------------- */
async function createShare(studyUid, patientName) {
  if (!state.token) { toast("Sign in to create share links", "err"); openLogin(); return; }
  $("share-patient").textContent = `${patientName} — generating link…`;
  $("share-modal").hidden = false;
  const res = await api("/api/v1/share", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ study_uid: decodeURIComponent(studyUid) }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    $("share-patient").textContent = `Failed: ${err.detail || res.status}`;
    $("share-qr").removeAttribute("src");
    $("share-url").textContent = "";
    return;
  }
  const data = await res.json();
  $("share-patient").textContent = `${patientName} — link valid until ${new Date(data.expires_at).toLocaleString()}`;
  $("share-qr").src = `data:image/png;base64,${data.qr_base64}`;
  $("share-url").textContent = data.url;
  $("share-url").dataset.url = data.url;
  toast("Share link created", "ok");
}

/* ---------------- delete ---------------- */
function confirmDelete(studyUid, patientName) {
  if (!state.token) { toast("Sign in to delete studies", "err"); openLogin(); return; }
  state.deleting = { uid: decodeURIComponent(studyUid), name: patientName };
  $("confirm-text").textContent = `This permanently removes the study for ${patientName} and all its files.`;
  $("confirm-modal").hidden = false;
}

/* ---------------- login ---------------- */
function shakeAuth() {
  const card = $("auth-card");
  if (!card) return;
  card.classList.remove("shake");
  void card.offsetWidth; /* restart the animation */
  card.classList.add("shake");
}

function setAuthMode(mode) {
  state.authMode = mode;
  const isLogin = mode === "login";
  $("tab-login").classList.toggle("active", isLogin);
  $("tab-register").classList.toggle("active", !isLogin);
  $("auth-name-field").hidden = isLogin;
  $("auth-title").textContent = isLogin ? "Welcome back" : "Create your account";
  $("auth-subtitle").textContent = isLogin ? "Sign in to manage the archive" : "Join your team on PAXS Archive";
  $("auth-submit-label").textContent = isLogin ? "Sign in" : "Create account";
  setTimeout(() => (isLogin ? $("auth-user") : $("auth-name")).focus(), 60);
}

function setAuthLoading(on) {
  const btn = $("auth-submit");
  btn.disabled = on;
  btn.classList.toggle("loading", on);
  $("auth-submit-label").textContent = on
    ? (state.authMode === "login" ? "Signing in…" : "Creating account…")
    : (state.authMode === "login" ? "Sign in" : "Create account");
}

function openLogin() {
  $("login-modal").hidden = false;
  $("auth-err").textContent = "";
  setAuthMode(state.authMode);
}

async function doAuth() {
  if ($("auth-submit").disabled) return; /* guard against double-submit via Enter */
  const username = $("auth-user").value.trim();
  const password = $("auth-pass").value;
  const fullName = $("auth-name").value.trim();
  $("auth-err").textContent = "";
  if (!username || !password) {
    $("auth-err").textContent = "Username and password are required.";
    shakeAuth();
    return;
  }
  setAuthLoading(true);
  try {
    const body = state.authMode === "login"
      ? JSON.stringify({ username, password })
      : JSON.stringify({ username, password, full_name: fullName || null });
    const res = await fetch(`/api/v1/auth/${state.authMode === "login" ? "login" : "register"}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      $("auth-err").textContent = data.detail || "Authentication failed";
      shakeAuth();
      return;
    }
    state.token = data.access_token;
    state.role = data.user?.role;
    state.username = data.user?.username;
    localStorage.setItem("paxs_token", state.token);
    $("login-modal").hidden = true;
    document.body.classList.remove("auth-gate");
    document.body.classList.remove("preboot");
    toast(`Signed in as ${data.user.username} (${data.user.role})`, "ok");
    $("btn-login").textContent = data.user.username;
    $("btn-login").style.color = "var(--accent)";
    loadTenantFilter();
    await loadTriage();
    // A gated boot defers all data loading until login succeeds.
    if (state.gated) { state.gated = false; finishBoot(); }
  } finally {
    setAuthLoading(false);
  }
}

/* ---------------- view navigation ---------------- */
const VIEWS = ["dashboard", "studies", "reading", "workflow", "nodes", "admin", "settings"];

function switchView(view, settingsTab = "sys") {
  if (view !== "settings") window.clearTimeout(discActivityTimer);
  if (!VIEWS.includes(view)) view = "dashboard";
  [...document.querySelectorAll(".view")].forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  [...document.querySelectorAll(".nav-tab")].forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view));
  const loaders = {
    dashboard: () => { loadStats(); loadTriage(); },
    studies: () => { loadStudies(); loadModalities(); loadTenantFilter(); },
    reading: () => { loadReadingCounts(); loadReadingTasks(); },
    workflow: () => { loadWorklist(); loadPerformed(); loadCommitments(); },
    nodes: () => { loadNodes(); },
    admin: () => {
      const activeBtn = document.querySelector("#admin-tabs button.active");
      loadAdminSubTab(activeBtn ? activeBtn.dataset.tab : "sys");
    },
    settings: () => { loadSettings(false, settingsTab); },
  };
  (loaders[view] || (() => {}))();
  /* Push browser history so the Back button navigates between views
     instead of leaving the dashboard entirely. */
  if (history.state?.view !== view) {
    history.pushState({ view }, "", `#${view}`);
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ---------------- settings (editable config) ---------------- */
const CFG_PANELS = ["sys", "dicom", "modality", "interop", "storage", "compression", "optical", "security", "workflow", "viewer", "db"];

function cfgCtrlValue(f) {
  const c = document.querySelector(`[data-cfg="${f.key}"]`);
  if (!c) return undefined;
  if (f.type === "bool") return c.checked;
  if (f.type === "int") { const n = parseInt(c.value, 10); return Number.isNaN(n) ? undefined : n; }
  if (f.type === "float") { const n = parseFloat(c.value); return Number.isNaN(n) ? undefined : n; }
  return c.value;
}

function cfgRowHTML(f) {
  const ro = state.config && !state.config.admin;
  let ctrl;
  if (f.type === "bool") {
    ctrl = `<label class="sw${ro ? " off" : ""}"><input type="checkbox" aria-label="${esc(f.label)}" data-cfg="${f.key}"${f.value ? " checked" : ""}${ro ? " disabled" : ""} /><i></i></label>`;
  } else if (f.type === "select") {
    ctrl = `<select aria-label="${esc(f.label)}" data-cfg="${f.key}"${ro ? " disabled" : ""}>${(f.options || [])
      .map((o) => `<option value="${esc(o)}"${String(o) === String(f.value) ? " selected" : ""}>${esc(f.option_labels?.[o] || o)}</option>`).join("")}</select>`;
  } else {
    const kind = f.type === "int" || f.type === "float" ? "number" : f.input === "time" ? "time" : "text";
    const min = f.min === undefined ? "" : ` min="${esc(f.min)}"`;
    const max = f.max === undefined ? "" : ` max="${esc(f.max)}"`;
    const step = f.type === "float" ? ' step="any"' : "";
    ctrl = `<input type="${kind}" aria-label="${esc(f.label)}" data-cfg="${f.key}" value="${esc(f.value)}"${min}${max}${step}${ro ? " disabled" : ""} />`;
  }
  const tags = [
    f.restart ? '<span class="restart-tag" title="Consumed at boot — takes full effect after the next server start">restart ↵</span>' : "",
    f.changed ? '<span class="changed-tag" title="Overrides the environment default">edited</span>' : "",
  ].join(" ");
  return `<div class="cfg-row" data-row="${f.key}">
    <div class="cfg-label">
      <span class="cfg-name">${esc(f.label)} <code>${esc(f.key)}</code></span>
      ${f.description ? `<span class="cfg-hint">${esc(f.description)}</span>` : ""}
    </div>
    <div class="cfg-control">${ctrl}</div>
    <div class="cfg-meta">${tags}</div>
  </div>`;
}

function cfgLiveHTML(ro) {
  const d = ro.dicom || {};
  const st = ro.storage || {};
  const cmp = ro.compression || {};
  const codecs = (cmp.codecs || [])
    .map((c) => `${esc(c.method)} ${c.available ? "✓" : "✗"}`)
    .join(" · ");
  return `<div class="cfg-live">
    <div><span class="sys-k">Server</span><span class="sys-v">${esc(ro.app || "PAXS")} <span class="mono faint">v${esc(ro.version || "?")}</span></span></div>
    <div><span class="sys-k">Database</span><span class="sys-v">${ro.database === "ok" ? '<span class="ok-dot"></span>ok' : '<span class="err-dot"></span>error'}</span></div>
    <div><span class="sys-k">DICOM</span><span class="sys-v">${d.enabled ? `${esc(d.ae_title || "")} :${d.port}` : "off"} ${ro.tls?.enabled ? '<span class="badge tls">TLS 🔒</span>' : ""}</span></div>
    <div><span class="sys-k">Storage</span><span class="sys-v">${esc(st.backend || "local")}${st.endpoint ? ` · ${esc(st.endpoint)}` : ""}</span></div>
    <div><span class="sys-k">Compression</span><span class="sys-v">${cmp.enabled ? `${esc(cmp.method || "RLE")} · ${codecs || "no codecs reported"}` : "off"}</span></div>
    <div><span class="sys-k">Interop</span><span class="sys-v">HL7 ${ro.hl7?.enabled ? `${esc(ro.hl7.host)}:${ro.hl7.port}` : "off"} · FHIR ${ro.fhir?.enabled ? "on" : "off"}</span></div>
  </div>`;
}

function renderGroup(g) {
  const el = $(`set-${g.key}`);
  if (!el) return;
  const ro = !state.config || !state.config.admin;
  const head = `<div class="cfg-head">
      <div><b>${esc(g.label)}</b><span class="cfg-desc">${esc(g.description)}</span></div>
      ${ro ? '<span class="cfg-ro">🔒 read-only — admin can edit</span>' : `<button class="btn sm ghost cfg-save-one" data-g="${g.key}">Save ${esc(g.label)}</button>`}
    </div>`;
  el.innerHTML = head + `<div class="cfg-fields">${g.fields.map(cfgRowHTML).join("")}</div>`;
}

function countDirty() {
  if (!state.config) return 0;
  let n = 0;
  state.config.groups.forEach((g) => g.fields.forEach((f) => {
    const v = cfgCtrlValue(f);
    if (v !== undefined && String(v) !== String(f.value)) n++;
  }));
  return n;
}

function refreshSavebar() {
  const n = countDirty();
  const bar = $("savebar");
  if (!bar || !state.config || !state.config.admin) return;
  bar.hidden = n === 0;
  if (!bar.hidden) {
    $("savebar-count").textContent = n;
    $("savebar-s").textContent = n === 1 ? "" : "s";
  }
}

function wireCfgInputs() {
  document.querySelectorAll(".settings-panel [data-cfg]").forEach((c) => {
    c.addEventListener("input", refreshSavebar);
    c.addEventListener("change", refreshSavebar);
  });
  document.querySelectorAll(".cfg-save-one").forEach((b) =>
    b.addEventListener("click", () => saveSettings(b.dataset.g)));
}

async function loadSettings(force = false, tab = "sys") {
  if (state.configLoaded && !force) { openSettingsTab(tab); return; }
  const res = await api("/api/v1/config");
  if (!res.ok) {
    if (res.status !== 401) {
      CFG_PANELS.forEach((p) => {
        const el = $(`set-${p}`);
        if (el) el.innerHTML = `<p class="sub">Settings unavailable (${res.status})</p>`;
      });
    }
    return;
  }
  const cfg = await res.json();
  state.config = cfg;
  state.configLoaded = true;
  (cfg.groups || []).forEach(renderGroup);
  renderCompressionProfiles(cfg);

  // brand + title follow the (possibly overridden) APP_NAME
  const nameField = (cfg.groups.find((g) => g.key === "sys")?.fields || []).find((f) => f.key === "APP_NAME");
  if (nameField) {
    const brand = document.querySelector(".brand h1");
    if (brand) brand.textContent = nameField.value;
    document.title = `${nameField.value} — Dashboard`;
  }

  // live status strip in the System panel
  const sys = $(`set-sys`);
  if (sys && cfg.readonly) sys.insertAdjacentHTML("afterbegin", cfgLiveHTML(cfg.readonly));

  wireCfgInputs();
  refreshSavebar();
  openSettingsTab(tab);
}

async function saveSettings(groupKey) {
  const updates = {};
  const groups = groupKey
    ? state.config.groups.filter((g) => g.key === groupKey)
    : state.config.groups;
  groups.forEach((g) => g.fields.forEach((f) => {
    const v = cfgCtrlValue(f);
    if (v !== undefined && String(v) !== String(f.value)) updates[f.key] = v;
  }));
  const keys = Object.keys(updates);
  if (!keys.length) { refreshSavebar(); return; }
  if (updates.LOSSY_COMPRESSION_ENABLED === true &&
      !confirm("Allow lossy rules to permanently change stored pixel values when run?")) return;
  const res = await api("/api/v1/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = data.errors || [];
    toast(`Save failed (${res.status})${err.length ? `: ${err[0].key} ${err[0].error}` : ""}`, "err");
    return;
  }
  if (data.partial) toast(`Applied ${data.applied?.length ?? 0}, ${data.errors?.length ?? 0} rejected`, "err");
  else toast(`Applied ${keys.length} setting${keys.length === 1 ? "" : "s"} ✓`, "ok");
  const patientNameSetting = data.applied?.find((field) => field.key === "PATIENT_NAME_DISPLAY");
  if (patientNameSetting) state.patientNameDisplay = patientNameSetting.value;
  const activeTab = document.querySelector("#settings-tabs button.active")?.dataset.stab;
  loadSettings(true, activeTab || "sys");
}

function renderCompressionProfiles(config) {
  const status = $("compression-codecs");
  const codecs = config.readonly?.compression?.codecs || [];
  status.textContent = codecs.length
    ? codecs.map((codec) => codec.method + (codec.available ? " available" : " unavailable")).join(" · ")
    : "Server codec status unavailable.";
  document.querySelectorAll("[data-compression-profile]").forEach((button) => {
    const method = button.dataset.compressionProfile;
    button.disabled = !config.admin || (method !== "AS_SOURCE" && !codecs.some((c) => c.method === method && c.available));
  });
}

function selectCompressionProfile(method) {
  const format = document.querySelector('[data-cfg="DEFAULT_COMPRESSION"]');
  const enabled = document.querySelector('[data-cfg="COMPRESSION_ENABLED"]');
  if (!format || !enabled || format.disabled) return;
  format.value = method;
  if (method !== "AS_SOURCE") enabled.checked = true;
  refreshSavebar();
  toast("Profile selected. Save Compression to apply it.", "ok");
}

/* ---------------- CD/DVD diagnostics ---------------- */
const DISC_TOOLS = "/api/v1/export/dicomdir/disc";
const DISC_PHASES = {
  queued: "Checking recorder", preparing: "Preparing files", writing: "Writing disc",
  verifying: "Verifying disc", verified: "Verified", failed: "Failed",
};
let discActivityTimer;

async function discTool(path) {
  const response = await api(DISC_TOOLS + path, { cache: "no-store" });
  const data = await response.json().catch(() => null);
  if (!response.ok || !data) {
    throw new Error(typeof data?.detail === "string" ? data.detail : "The server could not complete the check.");
  }
  return data;
}

async function scanDiscRecorders() {
  const button = $("disc-scan");
  if (button.disabled) return;
  const select = $("disc-recorders");
  const status = $("disc-recorder-status");
  button.disabled = select.disabled = true;
  select.replaceChildren(new Option("Choose a recorder", ""));
  status.textContent = "Scanning for optical recorders…";
  try {
    const { recorders } = await discTool("/recorders");
    for (const recorder of recorders) {
      select.add(new Option([recorder.drive, recorder.model].filter(Boolean).join(" · "), recorder.drive));
    }
    select.disabled = recorders.length === 0;
    status.textContent = recorders.length
      ? "Choose a recorder to fill the drive letter above, then save to apply it."
      : "No compatible optical recorder detected on the Windows server.";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function checkDiscMedia() {
  const button = $("disc-check");
  if (button.disabled) return;
  const status = $("disc-media-status");
  const driveInput = document.querySelector('[data-cfg="DISC_BURN_DRIVE"]');
  if (!driveInput) {
    status.textContent = "Load the CD/DVD settings before checking media.";
    return;
  }
  const drive = driveInput.value.trim().toUpperCase();
  if (!/^([A-Z]:)?$/.test(drive)) {
    status.textContent = "Enter a drive letter such as D: or leave the field blank for automatic selection.";
    return;
  }
  button.disabled = true;
  status.textContent = "Checking the inserted disc…";
  try {
    const media = await discTool("/media?drive=" + encodeURIComponent(drive));
    status.textContent = "Blank writable disc · " + media.drive + " · " + media.media_type
      + " · " + fmtBytes(media.available_bytes) + " writable capacity"
      + " · " + fmtBytes(media.staging_free_bytes) + " temporary space on the server"
      + " · Checked at " + new Date().toLocaleTimeString();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function refreshDiscActivity() {
  window.clearTimeout(discActivityTimer);
  const button = $("disc-refresh-jobs");
  if (button.disabled) return;
  const status = $("disc-job-status");
  const list = $("disc-jobs");
  button.disabled = true;
  try {
    const { jobs, active } = await discTool("/activity");
    status.textContent = jobs.length ? "" : "No burn jobs in this server session.";
    list.replaceChildren();
    for (const job of jobs) {
      const item = document.createElement("li");
      item.dataset.phase = job.phase;
      const title = document.createElement("strong");
      title.textContent = DISC_PHASES[job.phase] || job.phase;
      const detail = document.createElement("span");
      detail.textContent = [new Date(job.started_at).toLocaleString(), job.drive || "Automatic recorder",
        job.count + " DICOM instances", job.error].filter(Boolean).join(" · ");
      item.append(title, detail);
      list.append(item);
    }
    list.hidden = !jobs.length;
    if (active && $("view-settings").classList.contains("active") && !$("spanel-optical").hidden) {
      discActivityTimer = window.setTimeout(refreshDiscActivity, 3000);
    }
  } catch (error) {
    list.hidden = true;
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function loadModalityRegistry() {
  const el = $("set-modality");
  if (!el) return;
  try {
    const res = await api("/dicom-web/series?includefield=all");
    if (!res.ok) return;
    const rows = await res.json();
    const counts = {};
    rows.forEach((r) => {
      const m = r["00080060"]?.Value?.[0];
      if (m) counts[m] = (counts[m] || 0) + 1;
    });
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    el.innerHTML = entries.length
      ? `<div class="mod-grid">${entries.map(([m, c]) =>
          `<div class="mod-tile"><span class="mod-tile-ico" style="color:${MOD_COLORS[m] || "#94a3b8"}">${esc(m)}</span><span class="mod-tile-n">${c} series</span></div>`).join("")}</div>`
      : `<p class="sub">No modalities yet — ingest a study to populate the registry.</p>`;
  } catch (e) { /* offline */ }
}

function openSettingsTab(stab) {
  [...document.querySelectorAll("#settings-tabs button")].forEach((b) =>
    b.classList.toggle("active", b.dataset.stab === stab));
  [...document.querySelectorAll(".settings-panel")].forEach((p) =>
    (p.hidden = p.id !== `spanel-${stab}`));
  if (stab === "modality") loadModalityRegistry();
  if (stab === "db") loadDatabaseTab();
  if (stab === "redis") loadRedisTab();
  if (stab === "compression") { loadLossy(); loadLifecycle(); loadRouter(); fillNodeSelect(); loadRoutingQueue(); }
  if (stab === "optical") refreshDiscActivity();
  else window.clearTimeout(discActivityTimer);
}

/* ---------------- database tab ---------------- */
function dbFormBody() {
  return {
    backend: $("db-backend").value,
    host: $("db-host").value.trim(),
    port: parseInt($("db-port").value, 10) || 3306,
    user: $("db-user").value.trim(),
    password: $("db-pass").value,
    name: $("db-name").value.trim(),
  };
}

async function loadDatabaseTab() {
  const st = $("db-status");
  const res = await api("/api/v1/db/status");
  if (!res.ok) {
    if (st) st.innerHTML = `<p class="sub">Database status unavailable (${res.status}) — sign in.</p>`;
    return;
  }
  const s = await res.json();
  const admin = !!s.admin;
  const badge = s.backend === "mysql"
    ? '<span class="db-badge mysql">MySQL 8.0</span>'
    : '<span class="db-badge sqlite">SQLite</span>';
  const mig = s.up_to_date
    ? '<span class="ok-dot"></span> up to date'
    : `<span class="warn-txt">migration pending</span> <code class="mono faint">${esc(s.current_revision || "—")} → ${esc(s.head_revision || "?")}</code>`;
  st.innerHTML = `<h3 style="margin-bottom:10px">Current connection ${badge}</h3>
    <div class="sys-grid" style="grid-template-columns:repeat(auto-fill,minmax(230px,1fr))">
      <div class="sys-row"><span class="sys-k">Backend</span><span class="sys-v">${esc(s.backend)}</span></div>
      <div class="sys-row"><span class="sys-k">Connection</span><span class="sys-v mono">${esc(s.url_masked)}</span></div>
      ${s.host ? `<div class="sys-row"><span class="sys-k">Host · port</span><span class="sys-v">${esc(s.host)}:${esc(s.port)}</span></div>` : ""}
      ${s.database ? `<div class="sys-row"><span class="sys-k">Database</span><span class="sys-v">${esc(s.database)}</span></div>` : ""}
      <div class="sys-row"><span class="sys-k">Migrations</span><span class="sys-v">${mig}</span></div>
      <div class="sys-row"><span class="sys-k">Saved config</span><span class="sys-v">${s.saved_config_present ? `<span class="badge">${esc(s.saved_backend || "")} ✓</span>` : "<span class=\"faint\">none</span>"}</span></div>
      <div class="sys-row"><span class="sys-k">Backups</span><span class="sys-v">${s.backup_count} · <span class="mono faint">${esc(s.backup_dir)}</span></span></div>
    </div>`;

  // prefill the config form from the saved configuration (password lives in .env only)
  if (s.saved) {
    $("db-backend").value = s.saved.backend === "sqlite" ? "sqlite" : "mysql";
    $("db-host").value = s.saved.host || "";
    $("db-port").value = s.saved.port || 3306;
    $("db-user").value = s.saved.user || "";
    $("db-name").value = s.saved.name || "";
    $("db-config-note").innerHTML = '<span class="faint">Password kept server-side — re-enter only to change it.</span>';
  }
  syncDbForm();
  if (!admin) {
    [...document.querySelectorAll("#spanel-db button")].forEach((b) => { b.disabled = true; });
  }
  loadBackups();
}

function syncDbForm() {
  const mysql = $("db-backend").value === "mysql";
  ["db-host", "db-port", "db-user", "db-pass", "db-name"].forEach((id) => { $(id).disabled = !mysql; });
  $("db-test").disabled = !mysql;
}

async function dbTest() {
  const btn = $("db-test");
  const note = $("db-config-note");
  btn.disabled = true;
  note.innerHTML = '<span class="faint">Testing…</span>';
  try {
    const res = await api("/api/v1/db/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(dbFormBody()),
    });
    const data = await res.json().catch(() => ({}));
    if (data.ok) {
      note.innerHTML = `<span class="ok-dot"></span> connected in ${data.latency_ms} ms — ${esc(data.version || "")}`
        + (data.database_exists ? ` · database “${esc(data.database)}” exists`
          : ` · database “${esc(data.database)}” will be created on Initialize`);
    } else {
      note.innerHTML = `<span class="err-dot"></span> failed: ${esc(data.error || res.status)}`;
    }
  } finally {
    btn.disabled = false;
  }
}

async function dbSave() {
  const note = $("db-config-note");
  const body = dbFormBody();
  if (body.backend === "mysql" && (!body.host || !body.user || !body.name)) {
    note.innerHTML = '<span class="err-dot"></span> host, username and database are required for MySQL';
    return;
  }
  const res = await api("/api/v1/db/configure", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    note.innerHTML = `<span class="err-dot"></span> save failed (${res.status}): ${esc(typeof data.detail === "string" ? data.detail : "")}`;
    return;
  }
  note.innerHTML = `<span class="ok-dot"></span> saved ${esc(data.url_masked)} — restart the server to apply`;
  loadDatabaseTab();
}

async function dbInit() {
  const btn = $("db-init");
  const log = $("db-log");
  btn.disabled = true;
  log.hidden = false;
  log.innerHTML = '<div class="db-line">Running migrations…</div>';
  const body = { ...dbFormBody(), seed_admin: $("db-seed").checked };
  try {
    const res = await api("/api/v1/db/init", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    const lines = data.log || [];
    const detailErr = typeof data.detail === "object" ? data.detail?.error : data.detail;
    log.innerHTML = lines.map((l) => `<div class="db-line">${esc(l)}</div>`).join("")
      + (data.ok ? "" : `<div class="db-line err">ERROR ${esc(detailErr || res.status)}</div>`);
    if (data.ok && data.seeded) {
      log.insertAdjacentHTML("beforeend", '<div class="db-line ok">✓ ADMIN account seeded — log in with admin / admin123</div>');
    }
    toast(data.ok ? "Database initialized ✓" : `Init failed (${res.status})`, data.ok ? "ok" : "err");
    loadDatabaseTab();
  } finally {
    btn.disabled = false;
  }
}

async function dbBackup() {
  const btn = $("db-backup");
  const note = $("db-backup-note");
  btn.disabled = true;
  note.textContent = "Snapshotting…";
  try {
    const res = await api("/api/v1/db/backup", { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      note.innerHTML = `<span class="err-dot"></span> backup failed (${res.status})`;
      return;
    }
    note.innerHTML = `<span class="ok-dot"></span> saved ${esc(data.file)} (${fmtBytes(data.size)})`;
    loadBackups();
  } finally {
    btn.disabled = false;
  }
}

async function loadBackups() {
  const el = $("db-backups");
  const res = await api("/api/v1/db/backups");
  if (!res.ok) {
    el.innerHTML = `<p class="sub">Backups unavailable (${res.status}) — sign in.</p>`;
    return;
  }
  const rows = await res.json();
  if (!rows.length) {
    el.innerHTML = `<p class="sub">No backups yet — create one to snapshot the database.</p>`;
    return;
  }
  el.innerHTML = rows.map((b) => `
    <div class="node-item backup-row">
      <div class="cell-main">
        <div class="name mono">${esc(b.file)}</div>
        <div class="sub">${fmtBytes(b.size)} · ${esc((b.mtime || "").replace("T", " "))}</div>
      </div>
      <div class="actions">
        <button class="btn sm" data-dl="${esc(b.file)}">Download</button>
        <button class="btn sm danger" data-del="${esc(b.file)}">Delete</button>
      </div>
    </div>`).join("");
  [...el.querySelectorAll("[data-dl]")].forEach((b) => b.addEventListener("click", () => dbDownload(b.dataset.dl)));
  [...el.querySelectorAll("[data-del]")].forEach((b) => b.addEventListener("click", () => dbDelete(b.dataset.del)));
}

async function dbDownload(name) {
  const res = await api(`/api/v1/db/backups/${encodeURIComponent(name)}`);
  if (!res.ok) { toast(`Download failed (${res.status})`, "err"); return; }
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

async function dbDelete(name) {
  const res = await api(`/api/v1/db/backups/${encodeURIComponent(name)}`, { method: "DELETE" });
  toast(res.ok ? "Backup deleted" : `Delete failed (${res.status})`, res.ok ? "ok" : "err");
  loadBackups();
}

/* ---------------- redis tab ---------------- */
function redisFormBody() {
  return {
    host: $("redis-host").value.trim() || "127.0.0.1",
    port: parseInt($("redis-port").value, 10) || 6379,
    db: parseInt($("redis-db").value, 10) || 0,
    password: $("redis-pass").value,
    ssl: $("redis-ssl").checked,
    enabled: $("redis-enabled").checked,
  };
}

async function loadRedisTab() {
  const st = $("redis-status");
  const srvDesc = $("redis-srv-status-desc");
  const srvBadges = $("redis-srv-badges");
  const srvStart = $("redis-srv-start");
  const srvStop = $("redis-srv-stop");
  const srvRestart = $("redis-srv-restart");
  const srvInstall = $("redis-srv-install");
  const srvNote = $("redis-srv-note");
  const cfgNote = $("redis-config-note");

  let s = null;
  try {
    const res = await api("/api/v1/redis/status");
    if (res.ok) {
      s = await res.json();
    } else if (res.status === 401) {
      if (st) st.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center"><p class="sub" style="margin:0"><span class="warn-txt">●</span> Redis status requires sign-in. Please log in as ADMIN.</p><button class="btn ghost sm" id="redis-login-btn">Sign in</button></div>`;
      $("redis-login-btn")?.addEventListener("click", () => { if (typeof openLogin === "function") openLogin(); });
      return;
    } else {
      if (st) st.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center"><p class="sub" style="margin:0"><span class="err-dot"></span> Redis status unavailable (${res.status}). Ensure server is running.</p><button class="btn ghost sm" id="redis-refresh-btn">⟳ Retry</button></div>`;
      $("redis-refresh-btn")?.addEventListener("click", loadRedisTab);
      return;
    }
  } catch (err) {
    if (st) st.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center"><p class="sub" style="margin:0"><span class="err-dot"></span> Could not connect to API: ${esc(err.message)}</p><button class="btn ghost sm" id="redis-refresh-btn">⟳ Retry</button></div>`;
    $("redis-refresh-btn")?.addEventListener("click", loadRedisTab);
    return;
  }

  if (s) {
    const admin = !!s.admin;
    const connected = !!s.connected;
    const enabled = !!s.enabled;

    let badge = "";
    if (!enabled) {
      badge = '<span class="db-badge redis-off">Disabled (L1 RAM only)</span>';
    } else if (connected) {
      badge = `<span class="db-badge redis">Redis ${esc(s.server_info?.version || "Online")}</span>`;
    } else {
      badge = '<span class="db-badge redis-off">Offline / Unreachable</span>';
    }

    const pingTxt = connected
      ? `<span class="ok-dot"></span> connected <code class="mono faint">(${s.latency_ms} ms)</code>`
      : `<span class="err-dot"></span> ${esc(s.error || "disconnected")}`;

    const ram = s.tier1_ram || {};
    const mem = s.memory_info || {};
    const srv = s.server_info || {};
    const stats = s.stats_info || {};

    if (st) {
      st.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <h3 style="margin:0">Redis Cache &amp; Storage Engine ${badge}</h3>
          <button class="btn ghost sm" id="redis-refresh-btn" title="Refresh metrics">⟳ Refresh</button>
        </div>
        <div class="sys-grid" style="grid-template-columns:repeat(auto-fill,minmax(230px,1fr))">
          <div class="sys-row"><span class="sys-k">Status</span><span class="sys-v">${pingTxt}</span></div>
          <div class="sys-row"><span class="sys-k">Connection</span><span class="sys-v mono">${esc(s.url_masked || "—")}</span></div>
          <div class="sys-row"><span class="sys-k">Server Mode</span><span class="sys-v">${esc(srv.mode || "standalone")} · ${esc(srv.role || "master")}</span></div>
          <div class="sys-row"><span class="sys-k">Uptime</span><span class="sys-v">${srv.uptime_days ? esc(srv.uptime_days + " days") : (srv.uptime_seconds ? esc(srv.uptime_seconds + "s") : "—")}</span></div>
          <div class="sys-row"><span class="sys-k">Clients</span><span class="sys-v">${srv.connected_clients !== undefined ? esc(srv.connected_clients) : "0"} connected</span></div>
          <div class="sys-row"><span class="sys-k">Memory (L2 Redis)</span><span class="sys-v">${mem.used_memory_human ? `<b>${esc(mem.used_memory_human)}</b> (peak: ${esc(mem.used_memory_peak_human || "0B")})` : "—"}</span></div>
          <div class="sys-row"><span class="sys-k">DB Keys</span><span class="sys-v">${stats.total_keys !== undefined ? `<b>${esc(stats.total_keys)}</b> keys` : "—"}</span></div>
          <div class="sys-row"><span class="sys-k">Hit Rate</span><span class="sys-v">${stats.hit_rate_pct !== undefined ? `<b style="color:#34d399">${esc(stats.hit_rate_pct)}%</b> (${esc(stats.keyspace_hits || 0)} hits / ${esc(stats.keyspace_misses || 0)} misses)` : "—"}</span></div>
          <div class="sys-row"><span class="sys-k">Tier-1 RAM Cache (L1)</span><span class="sys-v">${ram.items_count || 0} items · ${ram.used_mb || 0} MB / ${ram.max_mb || 256} MB</span></div>
          <div class="sys-row"><span class="sys-k">Saved config</span><span class="sys-v">${s.saved_present ? '<span class="badge">persisted ✓</span>' : '<span class="faint">defaults</span>'}</span></div>
        </div>`;
      $("redis-refresh-btn")?.addEventListener("click", loadRedisTab);
    }

    // Process & Service Installation Info
    const inst = s.server_install || {};
    if (srvDesc && srvBadges) {
      if (inst.process_running) {
        srvDesc.innerHTML = `<span class="ok-dot"></span> <b>${esc(inst.product_name || "Redis")}</b> is running (PID: <code>${esc(inst.pid || "active")}</code>${inst.service_name ? `, Service: <code>${esc(inst.service_name)}</code>` : ""})`;
        srvBadges.innerHTML = '<span class="badge" style="background:rgba(52,211,153,.15);color:#34d399">● Process Active</span>';
        if (srvStart) srvStart.disabled = true;
        if (srvStop) srvStop.disabled = !admin;
        if (srvRestart) srvRestart.disabled = !admin;
        if (srvInstall) srvInstall.style.display = "none";
      } else if (inst.installed) {
        srvDesc.innerHTML = `<span class="warn-txt">●</span> <b>${esc(inst.product_name || "Redis")}</b> binary detected, but the process is currently stopped.`;
        srvBadges.innerHTML = '<span class="badge" style="background:rgba(251,191,36,.15);color:#fbbf24">● Stopped</span>';
        if (srvStart) srvStart.disabled = !admin;
        if (srvStop) srvStop.disabled = true;
        if (srvRestart) srvRestart.disabled = true;
        if (srvInstall) srvInstall.style.display = "inline-flex";
      } else {
        srvDesc.innerHTML = '<span class="err-dot"></span> No Redis / Memurai installation detected on host. Click Install to set up portable Redis for Windows automatically.';
        srvBadges.innerHTML = '<span class="badge" style="background:rgba(244,63,94,.15);color:#f43f5e">Not Installed</span>';
        if (srvStart) srvStart.disabled = !admin;
        if (srvStop) srvStop.disabled = true;
        if (srvRestart) srvRestart.disabled = true;
        if (srvInstall) {
          srvInstall.style.display = "inline-flex";
          srvInstall.classList.add("primary");
          srvInstall.disabled = !admin;
        }
      }
    }

    if (!admin && srvNote) {
      srvNote.innerHTML = '<span class="faint">Sign in as ADMIN to start/stop or change Redis settings.</span>';
    }

    // Fill form
    if (s.parsed) {
      if ($("redis-host")) $("redis-host").value = s.parsed.host || "127.0.0.1";
      if ($("redis-port")) $("redis-port").value = s.parsed.port || 6379;
      if ($("redis-db")) $("redis-db").value = s.parsed.db || 0;
      if ($("redis-ssl")) $("redis-ssl").checked = !!s.parsed.ssl;
      if ($("redis-enabled")) $("redis-enabled").checked = !!s.enabled;
      if (s.parsed.has_password && cfgNote) {
        cfgNote.innerHTML = '<span class="faint">Password kept server-side — re-enter only to change it.</span>';
      }
    }

    if (!admin) {
      [...document.querySelectorAll("#spanel-redis button")].forEach((b) => {
        if (!["redis-scan-btn", "redis-ping-btn", "rk-detail-close", "redis-refresh-btn", "redis-login-btn"].includes(b.id)) {
          b.disabled = true;
        }
      });
    }
  }
}

async function redisServerAction(action) {
  const note = $("redis-srv-note");
  const btn = $(`redis-srv-${action}`);
  if (btn) btn.disabled = true;
  if (note) note.innerHTML = `<span class="faint">Executing ${esc(action)}…</span>`;
  try {
    const res = await api(`/api/v1/redis/server/${action}`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      if (note) note.innerHTML = `<span class="ok-dot"></span> ${esc(data.message || "Action completed")}`;
      toast(data.message || `Redis server ${action} successful`, "ok");
      setTimeout(loadRedisTab, 600);
    } else {
      const errMsg = (res.status === 401 || res.status === 403)
        ? "Sign in as administrator (ADMIN) to manage Redis server."
        : (data.error || data.detail || `Action failed (${res.status})`);
      if (note) note.innerHTML = `<span class="err-dot"></span> ${esc(errMsg)}`;
      toast(errMsg, "err");
      if (res.status === 401 && typeof openLogin === "function") openLogin();
    }
  } catch (e) {
    if (note) note.innerHTML = `<span class="err-dot"></span> ${esc(e.message)}`;
    toast(`Error: ${e.message}`, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function redisTest() {
  const btn = $("redis-test");
  const note = $("redis-config-note");
  btn.disabled = true;
  note.innerHTML = '<span class="faint">Probing Redis server…</span>';
  try {
    const res = await api("/api/v1/redis/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(redisFormBody()),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      note.innerHTML = `<span class="ok-dot"></span> PING successful in <b>${data.latency_ms} ms</b> — Redis v${esc(data.version || "")} (${esc(data.total_keys || 0)} keys, ${esc(data.used_memory_human || "")} used)`;
    } else {
      const errMsg = (res.status === 401 || res.status === 403)
        ? "Sign in as administrator (ADMIN) to test connection."
        : (data.error || data.detail || `Failed (${res.status})`);
      note.innerHTML = `<span class="err-dot"></span> Connection failed: ${esc(errMsg)}`;
    }
  } finally {
    btn.disabled = false;
  }
}

async function redisSave() {
  const note = $("redis-config-note");
  const body = redisFormBody();
  const res = await api("/api/v1/redis/configure", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const errMsg = (res.status === 401 || res.status === 403)
      ? "Sign in as administrator (ADMIN) to save configuration."
      : (typeof data.detail === "string" ? data.detail : `Save failed (${res.status})`);
    note.innerHTML = `<span class="err-dot"></span> ${esc(errMsg)}`;
    return;
  }
  note.innerHTML = `<span class="ok-dot"></span> Saved configuration (${esc(data.url_masked)}) — active connection updated!`;
  toast("Redis configuration saved", "ok");
  loadRedisTab();
}

async function redisScanKeys(pattern = null) {
  const listEl = $("redis-keys-list");
  const pat = pattern !== null ? pattern : ($("redis-key-pattern").value.trim() || "*");
  const limit = parseInt($("redis-key-limit").value, 10) || 100;
  if (pattern !== null) {
    $("redis-key-pattern").value = pattern;
  }

  listEl.innerHTML = '<p class="sub">Scanning keys…</p>';
  try {
    const res = await api(`/api/v1/redis/keys?pattern=${encodeURIComponent(pat)}&limit=${limit}`);
    if (!res.ok) {
      listEl.innerHTML = `<p class="sub">Failed to scan keys (${res.status})</p>`;
      return;
    }
    const data = await res.json();
    const keys = data.keys || [];
    if (!keys.length) {
      listEl.innerHTML = `<p class="sub">No keys found matching pattern <code>${esc(pat)}</code></p>`;
      return;
    }

    listEl.innerHTML = keys.map((k) => {
      const ttlStr = k.ttl > 0 ? `${k.ttl}s` : (k.ttl === -1 ? "persistent" : "expired");
      const sizeStr = k.size_bytes ? fmtBytes(k.size_bytes) : "—";
      const isL1 = k.tier && k.tier.includes("RAM");
      const tierBadge = isL1 ? '<span class="badge" style="background:rgba(52,211,153,.15);color:#34d399">L1 RAM</span>' : '<span class="badge" style="background:rgba(244,63,94,.15);color:#f43f5e">Redis L2</span>';
      const typeBadge = `<span class="mwl-tag" style="padding:1px 5px;font-size:10px">${esc(k.type)}</span>`;
      return `
        <div class="node-item" style="padding:8px 12px">
          <div class="cell-main" style="cursor:pointer" data-viewkey="${esc(k.key)}">
            <div class="name mono" style="font-size:12px;color:var(--text)">${esc(k.key)}</div>
            <div class="sub" style="margin-top:2px">${tierBadge} ${typeBadge} · TTL: <code class="mono">${ttlStr}</code> · Size: ${sizeStr}</div>
          </div>
          <div class="actions">
            <button class="btn sm ghost" data-viewkey="${esc(k.key)}" title="Inspect key contents">View</button>
            <button class="btn sm danger" data-delkey="${esc(k.key)}" title="Delete key">Delete</button>
          </div>
        </div>
      `;
    }).join("");

    [...listEl.querySelectorAll("[data-viewkey]")].forEach((b) =>
      b.addEventListener("click", () => redisInspectKey(b.dataset.viewkey))
    );
    [...listEl.querySelectorAll("[data-delkey]")].forEach((b) =>
      b.addEventListener("click", () => redisDeleteKey(b.dataset.delkey))
    );
  } catch (e) {
    listEl.innerHTML = `<p class="sub">Error: ${esc(e.message)}</p>`;
  }
}

async function redisInspectKey(key) {
  const box = $("redis-key-detail");
  const title = $("rk-detail-title");
  const content = $("rk-detail-content");
  box.hidden = false;
  title.innerText = `Key: ${key}`;
  content.innerText = "Loading key details…";

  try {
    const res = await api(`/api/v1/redis/key?key=${encodeURIComponent(key)}`);
    if (!res.ok) {
      content.innerText = `Failed to load key (${res.status})`;
      return;
    }
    const data = await res.json();
    title.innerText = `Key: ${data.key} [${data.tier || ""}, Type: ${data.type}, TTL: ${data.ttl}s, Size: ${data.size_bytes}B]`;
    content.innerText = data.preview || "(empty value)";
  } catch (e) {
    content.innerText = `Error: ${e.message}`;
  }
}

async function redisDeleteKey(key) {
  if (!confirm(`Delete key "${key}" from cache?`)) return;
  try {
    const res = await api("/api/v1/redis/key", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    if (res.ok) {
      toast(`Deleted key ${key}`, "ok");
      redisScanKeys();
      loadRedisTab();
    } else {
      toast(`Failed to delete key (${res.status})`, "err");
    }
  } catch (e) {
    toast(`Error: ${e.message}`, "err");
  }
}

async function redisFlush(target, pattern = "") {
  let promptMsg = "";
  if (target === "all") {
    promptMsg = "⚠️ WARNING: Are you sure you want to flush the ENTIRE Redis DB (FLUSHDB)? All keys in this database index will be deleted!";
  } else if (target === "l1") {
    promptMsg = "Flush all in-memory Layer-1 RAM cache items?";
  } else if (target === "paxs") {
    promptMsg = "Flush all PAXS cached items (paxs:* frames, volumes, thumbnails, rate limits)?";
  } else {
    promptMsg = `Flush keys matching pattern "${pattern}"?`;
  }

  if (!confirm(promptMsg)) return;

  const logBox = $("redis-log");
  logBox.hidden = false;
  logBox.innerHTML = `<div class="db-line">Flushing cache (target: ${esc(target)})…</div>`;

  try {
    const res = await api("/api/v1/redis/flush", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, pattern }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      logBox.innerHTML += `<div class="db-line ok">✓ Flush complete: deleted ${data.deleted_redis || 0} Redis keys, ${data.deleted_ram || 0} RAM keys.</div>`;
      toast("Cache flushed successfully", "ok");
      loadRedisTab();
      redisScanKeys();
    } else {
      logBox.innerHTML += `<div class="db-line err">✕ Flush failed: ${esc(data.error || res.status)}</div>`;
      toast("Flush failed", "err");
    }
  } catch (e) {
    logBox.innerHTML += `<div class="db-line err">✕ Exception: ${esc(e.message)}</div>`;
  }
}

async function redisPing() {
  const logBox = $("redis-log");
  logBox.hidden = false;
  logBox.innerHTML = '<div class="db-line">Sending PING to Redis server…</div>';
  try {
    const res = await api("/api/v1/redis/ping", { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      logBox.innerHTML += `<div class="db-line ok">✓ PONG in ${data.latency_ms} ms</div>`;
      toast(`Redis PONG (${data.latency_ms} ms)`, "ok");
    } else {
      logBox.innerHTML += `<div class="db-line err">✕ Ping failed: ${esc(data.error || res.status)}</div>`;
      toast("Ping failed", "err");
    }
  } catch (e) {
    logBox.innerHTML += `<div class="db-line err">✕ Ping error: ${esc(e.message)}</div>`;
  }
}

async function redisRawInfo() {
  const logBox = $("redis-log");
  logBox.hidden = false;
  logBox.innerHTML = '<div class="db-line">Fetching Redis INFO…</div>';
  try {
    const res = await api("/api/v1/redis/info");
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      logBox.innerHTML = `<div class="db-line ok">--- Redis INFO ---</div><pre style="margin:0;font-size:11px">${esc(JSON.stringify(data.info, null, 2))}</pre>`;
    } else {
      logBox.innerHTML = `<div class="db-line err">✕ Failed to load INFO: ${esc(data.error || res.status)}</div>`;
    }
  } catch (e) {
    logBox.innerHTML = `<div class="db-line err">✕ Error: ${esc(e.message)}</div>`;
  }
}

/* ---------------- reading worklist (roadmap #30) ---------------- */
const RWL = { state: "UNREAD", page: 1, limit: 25, mine: false };

async function loadReadingCounts() {
  const res = await api("/api/v1/worklist-tasks/counts");
  if (!res.ok) return;
  const c = await res.json();
  setCount("rwl-cnt-unread", c.unread);
  setCount("rwl-cnt-stat", c.stat);
  setCount("rwl-cnt-dictated", c.dictated);
  setCount("rwl-cnt-read", c.read);
}

function setCount(id, n) { const el = $(id); if (el) el.textContent = n ?? 0; }

async function loadReadingTasks() {
  if (!state.token) { $("rwl-list").innerHTML = `<p class="sub" style="padding:18px">Sign in to see your reading tasks.</p>`; return; }
  const q = new URLSearchParams({ state: RWL.state, page: RWL.page, limit: RWL.limit });
  if (RWL.mine) q.set("radiologist", state.username || "");
  const res = await api(`/api/v1/worklist-tasks?${q}`);
  if (!res.ok) { $("rwl-list").innerHTML = `<p class="sub" style="padding:18px">Failed to load tasks (${res.status}).</p>`; return; }
  const data = await res.json();
  renderReadingTasks(data.items || []);
}

function renderReadingTasks(items) {
  const list = $("rwl-list");
  if (!items.length) {
    list.innerHTML = `<div class="empty-state"><div class="big">📖</div><p><b>No ${RWL.state.toLowerCase()} tasks</b></p><p>Nothing waiting in this pile right now.</p></div>`;
    return;
  }
  list.innerHTML = items.map((s) => {
    const mods = s.modality || [];
    const modCode = mods.length ? esc(mods[0]) : "DCM";
    const modColor = MOD_COLORS[mods[0]] || "#94a3b8";
    const statBadge = s.stat
      ? '<span class="state-badge archived" style="background:rgba(251,113,133,.14);color:#fb7185;border-color:rgba(251,113,133,.4)" title="Urgent — manual flag / critical result / HIGH triage">🚨 STAT</span>'
      : "";
    const taskBadge = s.task_state === "READ"
      ? '<span class="wl-status done">✓ read</span>'
      : s.task_state === "DICTATED"
        ? '<span class="wl-status prog">🎙️ dictated</span>'
        : '<span class="wl-status open">📖 unread</span>';
    const repBadge = s.report_status
      ? `<a class="report-badge ${s.report_status.toLowerCase()}" title="Open the radiology report" onclick="openReport('${encodeURIComponent(s.study_uid)}', '${esc(s.patient_name || "Patient")}')">${s.report_status === "FINALIZED" ? "📄 FINALIZED" : "📝 DRAFT"}</a>`
      : "";
    const assignee = s.assigned_radiologist
      ? `<span class="sub">→ ${esc(s.assigned_radiologist)}</span>` : "";
    const thumb = s.thumbnail
      ? `<div class="thumb-wrap"><div class="thumb-skel" aria-hidden="true"></div><img class="thumb" src="${s.thumbnail}" loading="lazy" alt="" onload="this.previousElementSibling.remove();this.classList.add('loaded')" onerror="this.previousElementSibling.remove();this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb empty',innerHTML:'NO<br/>IMG'}))" /><span class="thumb-hint">View</span><span class="mod-tag" style="--mod:${modColor}">${modCode}</span></div>`
      : `<div class="thumb-wrap"><div class="thumb empty">NO<br/>IMG</div><span class="mod-tag" style="--mod:${modColor}">${modCode}</span></div>`;
    return `
    <div class="study-row" style="--mod:${modColor};animation-delay:${Math.min((RWL.page - 1) * 20, 300)}ms">
      ${thumb}
      <div class="cell-main">
        <div class="name">${esc(archivePatientName(s.patient_name)) || "Unknown patient"}${s.patient_source === "his" ? '<span class="his-badge" title="Patient registered by the hospital HIS">HIS</span>' : ""}</div>
        <div class="sub mono">${esc(s.patient_id || "—")}${assignee}</div>
      </div>
      <div class="cell-main">
        <div class="desc">${esc(s.description) || "—"}${statBadge}${repBadge}</div>
        <div class="sub">${fmtDate(s.study_date)}</div>
      </div>
      <div class="cell col-acc mono"><span class="acc-chip">${esc(s.accession) || "—"}</span></div>
      <div class="cell col-series"><span class="counts"><b>${s.n_series ?? "—"}</b><small>ser</small><i>×</i><b>${s.n_instances ?? "—"}</b><small>img</small></span></div>
      <div class="cell">${taskBadge}</div>
      <div class="actions">
        ${viewerLink(s.study_uid)}
        <button class="icon-btn" title="Open in viewer" onclick="location.href=viewerUrl('${s.study_uid}')">👁</button>
        <button class="icon-btn ${s.stat_flag ? "danger" : ""}" title="Toggle STAT flag" onclick="rwlToggleStat('${encodeURIComponent(s.study_uid)}', ${s.stat_flag ? "false" : "true"})">🚨</button>
        <button class="icon-btn" title="Assign a radiologist" onclick="rwlAssign('${encodeURIComponent(s.study_uid)}', '${esc(s.assigned_radiologist || "")}')">👤</button>
        ${s.task_state !== "READ" ? `<button class="icon-btn" title="Mark as read" onclick="rwlMarkRead('${encodeURIComponent(s.study_uid)}')">✓</button>` : `<button class="icon-btn" title="Move back to unread" onclick="rwlMarkUnread('${encodeURIComponent(s.study_uid)}')">↺</button>`}
      </div>
    </div>`;
  }).join("");
}

async function rwlToggleStat(uid, on) {
  const res = await api(`/api/v1/worklist-tasks/${decodeURIComponent(uid)}/stat`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ on }),
  });
  toast(res.ok ? (on ? "Marked STAT" : "STAT cleared") : `Failed (${res.status})`, res.ok ? "ok" : "err");
  if (res.ok) { loadReadingCounts(); loadReadingTasks(); }
}

function rwlAssign(uid, current) {
  const val = prompt(`Assign radiologist for ${decodeURIComponent(uid)}`, current || "")?.trim();
  if (val === undefined) return;
  api(`/api/v1/worklist-tasks/${decodeURIComponent(uid)}/assign`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ radiologist: val || null }),
  }).then((res) => {
    toast(res.ok ? "Assignment updated" : `Failed (${res.status})`, res.ok ? "ok" : "err");
    if (res.ok) loadReadingTasks();
  });
}

async function rwlMarkRead(uid) {
  const res = await api(`/api/v1/worklist-tasks/${decodeURIComponent(uid)}/read`, { method: "POST" });
  toast(res.ok ? "Marked read" : `Failed (${res.status})`, res.ok ? "ok" : "err");
  if (res.ok) { loadReadingCounts(); loadReadingTasks(); }
}

async function rwlMarkUnread(uid) {
  const res = await api(`/api/v1/worklist-tasks/${decodeURIComponent(uid)}/unread`, { method: "POST" });
  toast(res.ok ? "Moved back to unread" : `Failed (${res.status})`, res.ok ? "ok" : "err");
  if (res.ok) { loadReadingCounts(); loadReadingTasks(); }
}

/* ---------------- modality worklist ---------------- */
async function loadWorklist() {
  const status = $("wl-filter").value;
  const res = await api(`/api/v1/worklist${status ? `?status=${encodeURIComponent(status)}` : ""}`);
  if (!res.ok) { $("worklist-list").innerHTML = `<p class="sub">Failed to load worklist (${res.status}) — sign in.</p>`; return; }
  const steps = await res.json();
  if (!steps.length) { $("worklist-list").innerHTML = `<p class="sub">No scheduled procedures. HIS admissions (HL7 ADT) and imaging orders (HL7 ORM) open steps automatically.</p>`; return; }
  $("worklist-list").innerHTML = steps.map((w) => {
    const st = w.status;
    const badge = st === "COMPLETED" ? '<span class="wl-status done">✓ done</span>'
      : st === "IN PROGRESS" ? '<span class="wl-status prog">● in progress</span>'
      : st === "CANCELED" ? '<span class="wl-status cx">✕ canceled</span>'
      : '<span class="wl-status open">● scheduled</span>';
    const linked = w.study_instance_uid
      ? ` · <a class="wl-link" href="${viewerUrl(w.study_instance_uid)}">view study →</a>` : "";
    return `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(w.patient_name) || "Unknown"} ${badge}
          ${w.patient_source === "his" ? '<span class="his-badge" title="Patient registered by the hospital HIS (HL7 ADT)">HIS</span>' : ""}
        </div>
        <div class="sub mono">${esc(w.patient_id) || "—"} · ${esc(w.modality) || "any"} @ ${esc(w.scheduled_station_aet) || "any station"} · ${fmtDate(w.start_date)}${w.start_time ? ` ${esc(w.start_time)}` : ""}${linked}</div>
        ${w.requested_procedure_description ? `<div class="sub">${esc(w.requested_procedure_description)}</div>` : ""}
      </div>
      <div class="actions">
        ${st === "SCHEDULED" ? `<button class="btn sm" data-done="${w.id}">Complete</button>
        <button class="btn sm danger" data-cancel="${w.id}">Cancel</button>` : ""}
      </div>
    </div>`;
  }).join("");
  [...document.querySelectorAll("[data-done]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/worklist/${b.dataset.done}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "COMPLETED" }),
      });
      toast(res.ok ? "Step marked completed" : `Update failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadWorklist();
    }));
  [...document.querySelectorAll("[data-cancel]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/worklist/${b.dataset.cancel}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "CANCELED" }),
      });
      toast(res.ok ? "Step canceled" : `Update failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadWorklist();
    }));
}

async function addStep() {
  const body = {
    patient_id: $("wl-mrn").value.trim(),
    patient_name: $("wl-name").value.trim() || null,
    modality: $("wl-modality").value.trim().toUpperCase() || null,
    scheduled_station_aet: $("wl-station").value.trim() || null,
    start_date: $("wl-date").value.replace(/-/g, "") || null,
    accession_number: $("wl-accession").value.trim() || null,
    requested_procedure_description: $("wl-desc").value.trim() || null,
  };
  if (!body.patient_id) { toast("MRN is required", "err"); return; }
  const res = await api("/api/v1/worklist", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    toast(err.detail || `Schedule failed (${res.status})`, "err"); return;
  }
  ["wl-mrn", "wl-name", "wl-modality", "wl-station", "wl-accession", "wl-desc"].forEach((id) => ($(id).value = ""));
  $("wl-date").value = "";
  toast("Procedure scheduled", "ok");
  loadWorklist();
}

/* ---------------- performed steps (MPPS) ---------------- */
async function loadPerformed() {
  const res = await api("/api/v1/mpps");
  if (!res.ok) { $("mpps-list").innerHTML = `<p class="sub">Failed to load performed steps (${res.status}) — sign in.</p>`; return; }
  const steps = await res.json();
  if (!steps.length) { $("mpps-list").innerHTML = `<p class="sub">No performed steps yet. When a modality reports an exam via MPPS (N-CREATE/N-SET), it appears here in real time.</p>`; return; }
  $("mpps-list").innerHTML = steps.map((p) => {
    const badge = p.status === "COMPLETED" ? '<span class="wl-status done">✓ done</span>'
      : p.status === "DISCONTINUED" ? '<span class="wl-status cx">✕ discontinued</span>'
      : '<span class="wl-status prog">● in progress</span>';
    const linked = p.study_instance_uid
      ? ` · <a class="wl-link" href="${viewerUrl(p.study_instance_uid)}">view study →</a>` : "";
    const sched = p.scheduled_step_id ? ` · order <span class="mono">${esc(p.scheduled_step_id)}</span>` : "";
    return `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(p.patient_name) || "Unknown patient"} ${badge}</div>
        <div class="sub mono">${esc(p.patient_id) || "—"} · ${esc(p.modality) || "—"} @ ${esc(p.performed_station_aet) || "—"} · ${fmtDate(p.start_date)}${p.start_time ? ` ${esc(p.start_time)}` : ""}${sched}${linked}</div>
        ${p.description ? `<div class="sub">${esc(p.description)}</div>` : ""}
      </div>
    </div>`;
  }).join("");
}

/* ---------------- storage commitments ---------------- */
const FAIL_REASON = {
  272: "0x0110 processing failure",
  274: "0x0112 no such object",
  290: "0x0122 SOP class unsupported",
};

async function loadCommitments() {
  const status = $("commit-filter").value;
  const res = await api(`/api/v1/commitments${status ? `?status=${encodeURIComponent(status)}` : ""}`);
  if (!res.ok) { $("commit-list").innerHTML = `<p class="sub">Failed to load commitments (${res.status}) — sign in.</p>`; return; }
  const rows = await res.json();
  if (!rows.length) { $("commit-list").innerHTML = `<p class="sub">No storage commitment requests yet. A modality asking &quot;are these images archived?&quot; via N-ACTION appears here.</p>`; return; }
  $("commit-list").innerHTML = rows.map((c) => {
    const badge = c.status === "COMPLETED" ? '<span class="wl-status done">✓ committed</span>'
      : c.status === "FAILED" ? '<span class="wl-status cx">✕ failed</span>'
      : '<span class="wl-status open">⏳ pending</span>';
    const fails = (c.failures || []).map((f) =>
      `<span class="cmt-fail">${esc(f.sop_instance_uid)} → ${FAIL_REASON[f.failure_reason] || `0x${(f.failure_reason ?? 0).toString(16)}`}</span>`).join("");
    return `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(c.ae_title || "unknown AE")} ${badge}</div>
        <div class="sub mono">tx ${esc(c.transaction_uid)} · ${c.verified_count}/${c.instance_count} instances verified${c.sent_at ? ` · report ${new Date(c.sent_at).toLocaleString()}` : ""}</div>
        ${fails ? `<div class="sub">${fails}</div>` : ""}
      </div>
    </div>`;
  }).join("");
}

/* ---------------- dicom nodes ---------------- */
async function loadNodes() {
  const res = await api("/api/v1/nodes");
  if (!res.ok) { $("node-list").innerHTML = `<p class="sub">Failed to load nodes (${res.status}) — sign in as admin.</p>`; return; }
  const nodes = await res.json();
  if (!nodes.length) { $("node-list").innerHTML = `<p class="sub">No nodes registered yet. Add the AE title of a peer archive so C-MOVE can find it.</p>`; return; }
  $("node-list").innerHTML = nodes.map((n) => {
    const webBtns = n.web_url ? `
        <button class="btn sm" data-webq="${esc(n.ae_title)}" title="Query this node over DICOMweb (QIDO-RS — C-FIND's REST twin)">Web Query</button>
        <button class="btn sm" data-webr="${esc(n.ae_title)}" title="Retrieve a study over DICOMweb (WADO-RS — C-GET's REST twin)">Web Retrieve</button>
        <button class="btn sm" data-webu="${esc(n.ae_title)}" title="Upload a local study to this node over DICOMweb (STOW-RS — C-STORE's REST twin)">Web Upload</button>
        <button class="btn sm fast" data-fastu="${esc(n.ae_title)}" title="Transfer Accelerator — parallel chunked STOW-RS upload, bypassing C-STORE">⚡ Fast Upload</button>` : "";
    return `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(n.ae_title)} ${n.enabled ? "" : '<span class="tag-disabled">disabled</span>'}
          ${n.web_url ? '<span class="wl-status open" style="font-size:10px">DICOMweb</span>' : ""}
          ${n.has_web_auth ? '<span class="mwl-tag" title="HTTP Basic auth configured">auth</span>' : ""}</div>
        <div class="sub mono">${esc(n.host)}:${n.port}${n.description ? ` · ${esc(n.description)}` : ""}${n.web_url ? `<br/><span class="faint">web ${esc(n.web_url)}</span>` : ""}</div>
      </div>
      <div class="actions">
        <button class="btn sm" data-ping="${esc(n.ae_title)}">Ping</button>
        <button class="btn sm" data-find="${esc(n.ae_title)}" title="Query this node (C-FIND SCU)">Find</button>
        <button class="btn sm" data-pull="${esc(n.ae_title)}" title="Pull studies from this node (C-GET SCU)">Pull</button>
        ${webBtns}
        <button class="btn sm danger" data-del="${esc(n.ae_title)}">Del</button>
      </div>
    </div>`;
  }).join("");
  [...document.querySelectorAll("[data-ping]")].forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "…";
      const res = await api(`/api/v1/nodes/${encodeURIComponent(b.dataset.ping)}/ping`, { method: "POST" });
      b.disabled = false; b.textContent = "Ping";
      if (!res.ok) { toast(`Ping failed (${res.status})`, "err"); return; }
      const r = await res.json();
      toast(r.ok ? `C-ECHO ok (${r.ae_title})` : `C-ECHO failed: ${r.reason || r.status}`, r.ok ? "ok" : "err");
    }));
  [...document.querySelectorAll("[data-find]")].forEach((b) =>
    b.addEventListener("click", () => openFind(b.dataset.find)));
  [...document.querySelectorAll("[data-pull]")].forEach((b) =>
    b.addEventListener("click", () => openPull(b.dataset.pull)));
  [...document.querySelectorAll("[data-webq]")].forEach((b) =>
    b.addEventListener("click", () => openFind(b.dataset.webq, "dicomweb")));
  [...document.querySelectorAll("[data-webr]")].forEach((b) =>
    b.addEventListener("click", () => openPull(b.dataset.webr, "dicomweb")));
  [...document.querySelectorAll("[data-webu]")].forEach((b) =>
    b.addEventListener("click", () => openWebUpload(b.dataset.webu)));
  [...document.querySelectorAll("[data-fastu]")].forEach((b) =>
    b.addEventListener("click", () => openWebUpload(b.dataset.fastu, true)));
  [...document.querySelectorAll("[data-del]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/nodes/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
      if (res.ok) { toast("Node removed", "ok"); loadNodes(); }
      else toast(`Delete failed (${res.status})`, "err");
    }));
}

async function addNode() {
  const ae_title = $("node-ae").value.trim();
  const host = $("node-host").value.trim();
  const port = parseInt($("node-port").value, 10);
  if (!ae_title || !host) { toast("AE title and host are required", "err"); return; }
  const res = await api("/api/v1/nodes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ae_title, host, port, description: $("node-desc").value.trim() || null,
      web_url: $("node-web").value.trim() || null,
      web_username: $("node-web-user").value.trim() || null,
      web_password: $("node-web-pass").value.trim() || null,
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    toast(err.detail || `Add failed (${res.status})`, "err"); return;
  }
  ["node-ae", "node-host", "node-desc", "node-web", "node-web-user", "node-web-pass"].forEach((id) => ($(id).value = ""));
  toast("Node registered", "ok");
  loadNodes();
}

/* ---------------- feature cards ---------------- */
async function loadFeatures(s) {
  if (!s) {
    const res = await api("/api/v1/stats");
    if (!res.ok) return;
    s = await res.json();
  }
  state.features = s;
  const f = s.features || {};
  const wl = f.worklist || {}, mp = f.mpps || {}, cm = f.commitments || {};
  featCount("fc-workflow", (wl.scheduled || 0) + (wl.in_progress || 0) + (mp.in_progress || 0) + (cm.pending || 0));
  const rd = f.reading || {};
  featCount("fc-reading", (rd.unread || 0) + (rd.stat || 0) + (rd.dictated || 0));
  featCount("fc-router", f.routing ? `${f.routing.enabled}/${f.routing.total}` : "–");
  const crit = f.critical_results?.pending || 0;
  featCount("fc-critical", crit);
  featCount("fc-storage", f.lossy_compression?.rules || 0);
  featCount("fc-migration", f.migration ? `${f.migration.running}/${f.migration.total}` : "–");
  const ds = f.dicom_services || {};
  featCount("fc-services", ds.total ? `${ds.enabled}/${ds.total}` : "–");
  featCount("fc-audit", f.audit?.events || 0);
  featCount("fc-nodes", s.nodes || 0);
  featCount("fc-sys", s.users ? `${s.users} user${s.users === 1 ? "" : "s"}` : "–");
  featCount("fc-scripts", f.scripts?.count != null ? f.scripts.count : "–");
  const rp = f.replication || {};
  featCount("fc-replication", rp.enabled ? `${rp.replicated || 0}/${(rp.pending || 0) + (rp.replicated || 0) + (rp.failed || 0)}` : "off");
  featCount("fc-protocols", f.protocols?.count ?? 0);
  const sub = $("fc-storage-sub");
  if (sub) sub.textContent = s.compression?.enabled ? `${s.compression.method} on` : "backend";
  const card = $("fc-critical")?.closest(".feat");
  if (card) card.classList.toggle("hot", crit > 0);
}

function featCount(id, value) {
  const el = $(id);
  if (el) el.textContent = value;
}

/* ---------------- admin console ---------------- */
function loadAdminSubTab(tab) {
  tab = tab || "sys";
  [...document.querySelectorAll("#admin-tabs button")].forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  [...document.querySelectorAll(".admin-panel")].forEach((p) =>
    (p.hidden = p.id !== `panel-${tab}`));
  const loaders = {
    sys: loadSysInfo,
    services: loadServices,
    critical: loadCritical,
    storage: loadStoragePanel,
    migration: loadMigration,
    audit: loadAudit,
    "auth-audit": () => { fillAuthAuditUsers(); loadAuthAudit(); loadLocked(); },
    scripts: loadScripts,
    replication: loadReplication,
    protocols: loadProtocols,
    tenants: loadTenants,
    users: loadUsers,
    sessions: loadSessions,
    dbperf: loadDbPerf,
    license: loadLicensePanel,
  };
  (loaders[tab] || (() => {}))();
}

function openAdminTab(tab) {
  if (!state.token) { toast("Sign in to open the admin console", "err"); openLogin(); return; }
  switchView("admin");
  loadAdminSubTab(tab);
}

/* ----- DB Pool panel (read-replica / connection-pool dashboard) ----- */
const DBP = { sqTimer: null };

function dbpPoolHTML(p) {
  const sync = p.pool?.sync || {};
  const pct = sync.pool_size ? Math.round((sync.checkedout / sync.pool_size) * 100) : 0;
  const overflowPct = sync.max_overflow ? Math.round((sync.overflow / sync.max_overflow) * 100) : 0;
  return `
    <div class="sys-row"><span class="sys-k">Backend</span><span class="sys-v">${esc(p.backend)}${p.backend === "mysql" ? ' <span class="badge">MySQL</span>' : ""}</span></div>
    <div class="sys-row"><span class="sys-k">Database</span><span class="sys-v mono">${esc(p.database)}</span></div>
    <div class="sys-row"><span class="sys-k">Sync pool</span><span class="sys-v">${esc(sync.class || "—")}</span></div>
    <div class="sys-row"><span class="sys-k">Checked out</span><span class="sys-v"><b>${sync.checkedout}</b> / ${sync.pool_size} <span class="faint">(${pct}%)</span></span></div>
    <div class="sys-row"><span class="sys-k">Pool size</span><span class="sys-v">${sync.size} <span class="faint">(max ${sync.pool_size})</span></span></div>
    <div class="sys-row"><span class="sys-k">Overflow</span><span class="sys-v">${sync.overflow} <span class="faint">(max ${sync.max_overflow}, ${overflowPct}%)</span></span></div>
    <div class="sys-row"><span class="sys-k">Async engine pool</span><span class="sys-v">${esc(p.pool?.async_class || "—")} <span class="faint">(NullPool)</span></span></div>
    <div class="sys-row"><span class="sys-k">Staleness guards</span><span class="sys-v">pre-ping ${p.pool?.pre_ping ? "✓" : "✕"} · recycle ${p.pool?.recycle_seconds}s</span></div>`;
}

function dbpMySQLHTML(m) {
  if (!m) return "";
  if (m.ok === false) return `<div class="sys-row"><span class="sys-k">MySQL counters</span><span class="sys-v err-dot"></span><span class="faint"> ${esc(m.error)}</span></div>`;
  return `
    <div class="sys-row"><span class="sys-k">Threads connected</span><span class="sys-v"><b>${m.threads_connected}</b> / ${m.max_connections}</span></div>
    <div class="sys-row"><span class="sys-k">Max used connections</span><span class="sys-v">${m.max_used_connections}</span></div>
    <div class="sys-row"><span class="sys-k">Total connections</span><span class="sys-v">${m.total_connections}</span></div>
    <div class="sys-row"><span class="sys-k">Server slow queries</span><span class="sys-v">${m.slow_queries_total} <span class="faint">(long_query_time ${m.long_query_time}s)</span></span></div>
    <div class="sys-row"><span class="sys-k">Uptime</span><span class="sys-v">${(m.uptime_seconds / 3600).toFixed(1)} h</span></div>`;
}

async function loadDbPerf() {
  const pool = $("dbperf-pool");
  const res = await api("/api/v1/db/perf");
  if (!res.ok) {
    pool.innerHTML = `<p class="sub">DB Pool unavailable (${res.status}) — admin/radiologist sign-in required.</p>`;
    return;
  }
  const p = await res.json();
  pool.innerHTML = `<div class="sys-grid" style="grid-template-columns:repeat(auto-fill,minmax(210px,1fr))">${dbpPoolHTML(p)}${dbpMySQLHTML(p.mysql)}</div>`;

  // replica card
  const rep = $("dbperf-replica");
  if (!p.replica?.configured) {
    rep.innerHTML = `<p class="sub">No replica configured — set <code>DB_READ_REPLICA_URL</code> in <code>.env</code> and restart.</p>`;
  } else {
    const on = p.replica.enabled;
    const lag = p.replica.lag_seconds;
    rep.innerHTML = `<div class="sys-grid" style="grid-template-columns:repeat(auto-fill,minmax(210px,1fr))">
      <div class="sys-row"><span class="sys-k">Replica</span><span class="sys-v mono">${esc(p.replica.url_masked || "")}</span></div>
      <div class="sys-row"><span class="sys-k">Status</span><span class="sys-v">${p.replica.connected ? '<span class="ok-dot"></span>connected' : `<span class="err-dot"></span>down — ${esc(p.replica.error || "")}`}</span></div>
      <div class="sys-row"><span class="sys-k">Replication lag</span><span class="sys-v">${lag == null ? "—" : `<b>${lag}</b> s`}${p.replica.io_running ? ` · IO ${esc(p.replica.io_running)} · SQL ${esc(p.replica.sql_running)}` : ""}</span></div>
      <div class="sys-row"><span class="sys-k">Route reads</span><span class="sys-v"><label class="sw"><input type="checkbox" id="rep-route"${on ? " checked" : ""}${p.replica.connected ? "" : " disabled"} /><i></i></label> <span class="faint">${on ? "replica" : "primary"}</span></span></div>
    </div>`;
    const sw = $("rep-route");
    if (sw) sw.addEventListener("change", async () => {
      const r = await api("/api/v1/db/perf/replica", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: sw.checked }),
      });
      toast(r.ok ? (sw.checked ? "Reads routed to replica" : "Reads back on primary") : `Toggle failed (${r.status})`, r.ok ? "ok" : "err");
      if (r.ok) loadDbPerf();
    });
  }

  // slow-query log controls
  const sq = p.slow_query || {};
  $("sq-enabled").checked = !!sq.enabled;
  $("sq-threshold").value = sq.threshold_ms ?? sq.default_threshold_ms ?? 500;
  loadDbPerfSlow();

  // hot-query dropdown (rebuild each open to stay fresh)
  const sel = $("xp-name");
  sel.innerHTML = `<option value="">— pick a hot query —</option>`
    + (p.hot_queries || []).map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
}

async function loadDbPerfSlow() {
  const el = $("slow-query-list");
  const res = await api("/api/v1/db/perf/slow-queries");
  if (!res.ok) { el.innerHTML = `<p class="sub">Slow-query log unavailable (${res.status}).</p>`; return; }
  const rows = await res.json();
  if (!rows.length) {
    el.innerHTML = `<p class="sub">No slow queries recorded yet — flip the log on and hammer the archive.</p>`;
    return;
  }
  el.innerHTML = rows.map((q) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name mono"><span class="warn-txt">${q.ms} ms</span> · ${esc(q.ts)} <span class="badge" style="margin-left:6px">${esc(q.engine)}</span></div>
        <div class="sub mono">${esc(q.sql)}</div>
        ${q.params && q.params !== "None" ? `<div class="sub faint mono">params ${esc(q.params)}</div>` : ""}
      </div>
    </div>`).join("");
}

async function runExplain() {
  const out = $("xp-result");
  const name = $("xp-name").value;
  const sql = $("xp-sql").value.trim();
  if (!name && !sql) { toast("Pick a hot query or enter custom SQL", "err"); return; }
  out.innerHTML = '<p class="sub">Running EXPLAIN…</p>';
  const q = new URLSearchParams();
  if (name) q.set("name", name); else q.set("sql", sql);
  const res = await api(`/api/v1/db/perf/explain?${q}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    out.innerHTML = `<p class="sub err">EXPLAIN failed (${res.status}): ${esc(typeof err.detail === "string" ? err.detail : "")}</p>`;
    return;
  }
  const d = await res.json();
  if (!d.rows.length) { out.innerHTML = `<p class="sub">Empty plan for <code>${esc(d.name)}</code>.</p>`; return; }
  const cols = d.columns || Object.keys(d.rows[0]);
  out.innerHTML = `
    <p class="sub">Plan for <code>${esc(d.name)}</code> — <span class="mono faint">${esc(d.sql)}</span></p>
    <div class="xp-table"><table>
      <thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${d.rows.map((r) => `<tr>${cols.map((c) => `<td>${esc(r[c] ?? "")}</td>`).join("")}</tr>`).join("" )}</tbody>
    </table></div>`;
}

function bindDbPerf() {
  const sw = $("sq-enabled");
  if (!sw) return;
  sw.addEventListener("change", async () => {
    const res = await api("/api/v1/db/perf/slow-query", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: sw.checked, threshold_ms: parseInt($("sq-threshold").value, 10) || 500 }),
    });
    toast(res.ok ? (sw.checked ? "Slow-query log on" : "Slow-query log off") : `Toggle failed (${res.status})`, res.ok ? "ok" : "err");
    if (res.ok) { loadDbPerfSlow(); }
  });
  $("sq-threshold").addEventListener("change", async () => {
    const res = await api("/api/v1/db/perf/slow-query", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: $("sq-enabled").checked, threshold_ms: parseInt($("sq-threshold").value, 10) || 500 }),
    });
    toast(res.ok ? `Threshold set to ${$("sq-threshold").value} ms` : `Save failed (${res.status})`, res.ok ? "ok" : "err");
  });
  $("sq-refresh").addEventListener("click", loadDbPerfSlow);
  $("xp-run").addEventListener("click", runExplain);
  // auto-refresh the slow list while the panel is visible
  if (DBP.sqTimer) clearInterval(DBP.sqTimer);
  DBP.sqTimer = setInterval(() => {
    if (!$("panel-dbperf") || $("panel-dbperf").hidden) return;
    loadDbPerfSlow();
  }, 8000);
}

/* ----- licensing management (Admin → License) ----- */
let _adminServerFp = "";

window.copyLicenseFingerprint = function() {
  const fp = $("lic-fp-input")?.value || _adminServerFp;
  if (fp) {
    navigator.clipboard?.writeText(fp).then(() => toast("Hardware fingerprint copied to clipboard! ✓", "ok")).catch(() => {
      // Fallback
      const inp = $("lic-fp-input");
      if (inp) { inp.select(); document.execCommand("copy"); toast("Hardware fingerprint copied! ✓", "ok"); }
    });
  } else {
    toast("Fingerprint not loaded yet", "err");
  }
};

async function loadLicensePanel() {
  const infoEl = $("license-info");
  const featEl = $("license-features-list");
  if (!infoEl) return;
  infoEl.innerHTML = '<p class="sub">Loading license data…</p>';

  try {
    const [stRes, fpRes] = await Promise.all([
      api("/api/v1/license/status"),
      api("/api/v1/license/fingerprint")
    ]);

    let st = null;
    if (stRes.ok) {
      st = await stRes.json();
      if (st.server_fingerprint) _adminServerFp = st.server_fingerprint;
    }

    if (fpRes.ok) {
      const fpData = await fpRes.json().catch(() => ({}));
      if (fpData.fingerprint) _adminServerFp = fpData.fingerprint;
    }

    const fpInput = $("lic-fp-input");
    if (fpInput && _adminServerFp) {
      fpInput.value = _adminServerFp;
      fpInput.onclick = () => fpInput.select();
    }

    if (!stRes.ok || !st) {
      infoEl.innerHTML = `<p class="sub err">Failed to load license (${stRes.status})</p>`;
      return;
    }

    const isGood = st.status === "VALID";
    const badgeStyle = isGood ? 'style="background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.35)"' : (st.status === "GRACE_PERIOD" ? 'style="background:rgba(251,191,36,.15);color:#fbbf24;border:1px solid rgba(251,191,36,.35)"' : 'style="background:rgba(244,63,94,.15);color:#f43f5e;border:1px solid rgba(244,63,94,.35)"');
    
    infoEl.innerHTML = `
      <div class="sys-row"><span class="sys-k">License Status</span><span class="sys-v"><span class="badge" ${badgeStyle}>${esc(st.status)}</span> <small class="faint">${esc(st.status_message || "")}</small></span></div>
      <div class="sys-row"><span class="sys-k">Edition</span><span class="sys-v"><b>${esc(st.edition)}</b></span></div>
      <div class="sys-row"><span class="sys-k">Customer</span><span class="sys-v">${esc(st.customer_name || "—")} <small class="faint">${esc(st.customer_organization || "")}</small></span></div>
      <div class="sys-row"><span class="sys-k">License ID</span><span class="sys-v mono">${esc(st.license_id || "—")}</span></div>
      <div class="sys-row"><span class="sys-k">Validity</span><span class="sys-v">${st.is_perpetual ? "<b>PERPETUAL</b>" : (st.expires_at ? `${new Date(st.expires_at).toLocaleDateString()} (${st.days_remaining || 0} days remaining)` : "—")}</span></div>
      <div class="sys-row"><span class="sys-k">Capacity Quotas</span><span class="sys-v">${st.max_users || 0} User Seats · ${(st.max_studies_per_year || 0).toLocaleString()} Studies/year</span></div>
      <div class="sys-row"><span class="sys-k">Hardware Lock</span><span class="sys-v">${st.machine_fingerprint === "*" ? '<span class="badge">WILDCARD / CLOUD</span>' : (st.hardware_matched ? '<span class="badge" style="background:rgba(52,211,153,.15);color:#34d399">MATCHED ✓</span>' : '<span class="badge" style="background:rgba(244,63,94,.15);color:#f43f5e">MISMATCH ✕</span>')}</span></div>
    `;

    if (featEl) {
      const FEATURE_META = {
        mpr_3d: { name: "3D Multi-Planar Reconstruction (MPR)", tag: "CT / MR" },
        recist: { name: "RECIST 1.1 Tumor Tracking", tag: "ONCOLOGY" },
        brain_seg: { name: "3D Brain Volumetry & MS Lesion Segmentation", tag: "MRI" },
        ich_triage: { name: "Emergency Intracranial Hemorrhage Triage", tag: "AI / CT" },
        tmtv_pet: { name: "Total Metabolic Tumor Volume (TMTV)", tag: "PET / CT" },
        li_rads: { name: "Liver Imaging LI-RADS Classifier", tag: "CT / MR" },
        lung_rads: { name: "Lung-RADS Assessment & Nodule Tracker", tag: "CT" },
        agatston: { name: "Coronary Calcium Agatston Score", tag: "CARDIAC CT" },
        pe_clot: { name: "Pulmonary Embolism Clot Detection", tag: "AI / CTA" },
        aortic_aneurysm: { name: "Aortic Aneurysm 3D Centerline Measurement", tag: "CTA" },
        cardiac_ef: { name: "Left Ventricular Ejection Fraction & Strain", tag: "CINE MRI" },
        breast_density: { name: "BI-RADS Breast Density Scoring", tag: "MAMMO" },
        cad_mammography: { name: "CAD Mammography & Lesion Scoring", tag: "AI / DBT" },
        cobb_angle: { name: "Automated Scoliosis Cobb Angle", tag: "XR" },
        bone_age_fracture: { name: "Pediatric Bone Age & Fracture Detection", tag: "AI / XR" },
        hl7_router: { name: "Live HL7 ADT/ORM Integration", tag: "INTEROP" },
        fhir_gateway: { name: "HL7 FHIR DiagnosticReport Gateway", tag: "FHIR R4" },
        ai_report_draft: { name: "DICOM SR (TID 1500) Report Auto-Draft", tag: "AI / SR" },
        telerad_edge: { name: "Teleradiology Safe Harbor Anonymizer & Gateway", tag: "CLOUD" },
        storage_tiering: { name: "Hierarchical Cold Storage Tiering", tag: "STORAGE" },
        ai_pipeline: { name: "AI Automated Triage Engine", tag: "CORE AI" }
      };

      if (st.features && st.features.length > 0) {
        featEl.innerHTML = st.features.map(f => {
          const meta = FEATURE_META[f] || { name: f.replace(/_/g, " ").toUpperCase(), tag: "MODULE" };
          return `
            <div class="node-item" style="padding:10px 14px; display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;">
              <div style="display:flex; align-items:center; gap:10px;">
                <span class="badge" style="background:rgba(52,211,153,.15); color:#34d399; font-weight:700;">✓ ACTIVE</span>
                <b style="font-size:0.92rem;">${esc(meta.name)}</b>
              </div>
              <span class="mwl-tag" style="background:rgba(34,211,238,.12); color:#22d3ee; border:1px solid rgba(34,211,238,.3);">${esc(meta.tag)}</span>
            </div>
          `;
        }).join("");
      } else {
        featEl.innerHTML = '<p class="sub">No premium clinical features enabled.</p>';
      }
    }
  } catch (err) {
    infoEl.innerHTML = `<p class="sub err">Error loading license: ${esc(err.message)}</p>`;
  }
}

/* ----- clinical protocols library (Admin → Protocols, #31) ----- */
async function loadProtocols() {
  const res = await api("/api/v1/clinical-protocols");
  if (!res.ok) { $("protocols-list").innerHTML = `<p class="sub">Failed to load protocols (${res.status}).</p>`; return; }
  const rows = await res.json();
  if (!rows.length) { $("protocols-list").innerHTML = `<p class="sub">No clinical protocols yet. Add one above to map study descriptions to standardized names + RIS codes.</p>`; return; }
  $("protocols-list").innerHTML = rows.map((p) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(p.name)} ${p.enabled ? "" : '<span class="tag-disabled">disabled</span>'} ${p.standardized_name ? `<code class="mono">${esc(p.standardized_name)}</code>` : ""}</div>
        <div class="sub mono">${esc(p.ris_code || "—")} · ${esc(p.modality || "any")} · ${esc(p.body_part_examined || "any body")} · priority ${p.priority}${p.description ? ` · ${esc(p.description)}` : ""}</div>
        ${p.study_description_pattern ? `<div class="sub">↳ matches: <code>${esc(p.study_description_pattern)}</code></div>` : ""}
      </div>
      <div class="actions">
        <button class="btn sm" data-cp-edit="${p.id}">Edit</button>
        <button class="btn sm ${p.enabled ? "danger" : ""}" data-cp-toggle="${p.id}">${p.enabled ? "Disable" : "Enable"}</button>
        <button class="btn sm danger" data-cp-del="${p.id}">Del</button>
      </div>
    </div>`).join("");
  bindProtocolRowActions(rows);
}

function bindProtocolRowActions(rows) {
  [...document.querySelectorAll("[data-cp-edit]")].forEach((b) =>
    b.addEventListener("click", () => {
      const p = rows.find((r) => r.id === Number(b.dataset.cpEdit));
      if (!p) return;
      $("cp-name").value = p.name || "";
      $("cp-std").value = p.standardized_name || "";
      $("cp-ris").value = p.ris_code || "";
      $("cp-modality").value = p.modality || "";
      $("cp-body").value = p.body_part_examined || "";
      $("cp-priority").value = p.priority ?? 10;
      $("cp-pattern").value = p.study_description_pattern || "";
      $("cp-desc").value = p.description || "";
      const btn = $("cp-add");
      btn.dataset.editId = p.id;
      btn.textContent = "Save changes";
      window.scrollTo({ top: 0, behavior: "smooth" });
    }));
  [...document.querySelectorAll("[data-cp-toggle]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const p = rows.find((r) => r.id === Number(b.dataset.cpToggle));
      const res = await api(`/api/v1/clinical-protocols/${p.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !p.enabled }),
      });
      toast(res.ok ? "Protocol updated" : `Failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) { loadProtocols(); loadFeatures(); }
    }));
  [...document.querySelectorAll("[data-cp-del]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const p = rows.find((r) => r.id === Number(b.dataset.cpDel));
      if (!confirm(`Delete protocol "${p.name}"?`)) return;
      const res = await api(`/api/v1/clinical-protocols/${p.id}`, { method: "DELETE" });
      toast(res.ok ? "Protocol deleted" : `Delete failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) { loadProtocols(); loadFeatures(); }
    }));
}

async function addProtocol() {
  const id = $("cp-add").dataset.editId;
  const body = {
    name: $("cp-name").value.trim(),
    standardized_name: $("cp-std").value.trim() || null,
    ris_code: $("cp-ris").value.trim() || null,
    modality: $("cp-modality").value.trim().toUpperCase() || null,
    body_part_examined: $("cp-body").value.trim().toUpperCase() || null,
    priority: parseInt($("cp-priority").value, 10) || 10,
    study_description_pattern: $("cp-pattern").value.trim() || null,
    description: $("cp-desc").value.trim() || null,
  };
  if (!body.name) { toast("Protocol name is required", "err"); return; }
  const res = await api(`/api/v1/clinical-protocols${id ? `/${id}` : ""}`, {
    method: id ? "PATCH" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) { toast(`Save failed (${res.status})`, "err"); return; }
  toast(id ? "Protocol updated" : "Protocol added", "ok");
  ["cp-name", "cp-std", "cp-ris", "cp-modality", "cp-body", "cp-pattern", "cp-desc"].forEach((i) => ($(i).value = ""));
  $("cp-priority").value = 10;
  delete $("cp-add").dataset.editId;
  $("cp-add").textContent = "+ Add protocol";
  loadProtocols(); loadFeatures();
}

async function testProtocolMatch() {
  const q = new URLSearchParams();
  const d = $("cp-tdesc").value.trim();
  const m = $("cp-tmod").value.trim();
  const b = $("cp-tbody").value.trim();
  if (d) q.set("study_description", d);
  if (m) q.set("modality", m);
  if (b) q.set("body_part", b);
  const res = await api(`/api/v1/clinical-protocols/match?${q}`);
  const data = await res.json().catch(() => ({}));
  const hit = data.match;
  $("cp-match-result").innerHTML = hit
    ? `<div class="node-item" style="margin-top:10px"><div class="cell-main"><div class="name">✅ ${esc(hit.name)} <code class="mono">${esc(hit.standardized_name || "")}</code></div><div class="sub mono">${esc(hit.ris_code || "—")} · ${esc(hit.modality || "any")} · ${esc(hit.body_part_examined || "any body")}</div></div></div>`
    : `<p class="sub" style="margin-top:10px">No matching protocol — add one or adjust the context.</p>`;
}

/* ----- user management (Admin → Users) ----- */
const USER_ROLES = ["ADMIN", "RADIOLOGIST", "PHYSICIAN", "TECHNOLOGIST"];
let _userModalAction = null;

function avatarColor(name) {
  const palette = ["#22d3ee", "#a78bfa", "#34d399", "#fbbf24", "#fb7185", "#38bdf8"];
  let h = 0;
  for (const ch of String(name || "?")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return palette[h % palette.length];
}

function userModal({ title, text, input = false, okLabel = "Confirm", onOk }) {
  $("user-modal-title").textContent = title;
  $("user-modal-text").textContent = text;
  $("user-modal-field").hidden = !input;
  $("user-modal-input").value = "";
  $("user-modal-ok").textContent = okLabel;
  _userModalAction = onOk;
  $("user-modal").hidden = false;
  if (input) setTimeout(() => $("user-modal-input").focus(), 60);
}

async function loadUsers() {
  const el = $("users-list");
  const me = await api("/api/v1/auth/me").then((r) => r.json()).catch(() => null);
  const res = await api("/api/v1/users");
  if (!res.ok) {
    el.innerHTML = `<p class="sub">Users unavailable (${res.status}) — admin only.</p>`;
    return;
  }
  const users = await res.json();
  const meName = me?.username;
  const admins = users.filter((u) => u.role === "ADMIN" && u.is_active).length;
  el.innerHTML = users.map((u) => {
    const isSelf = u.username === meName;
    const lastAdmin = u.role === "ADMIN" && admins === 1 && u.is_active;
    const canEdit = !isSelf && !lastAdmin;
    return `
    <div class="node-item user-row" data-id="${u.id}">
      <div class="user-avatar" style="background:${avatarColor(u.username)}">${esc((u.username[0] || "?").toUpperCase())}</div>
      <div class="cell-main">
        <div class="name">${esc(u.full_name || u.username)}
          ${isSelf ? '<span class="mwl-tag" title="This is your account">you</span>' : ""}
          ${u.is_active ? '<span class="wl-status done">active</span>' : '<span class="wl-status cx">disabled</span>'}
        </div>
        <div class="sub mono">@${esc(u.username)} · joined ${esc((u.created_at || "").slice(0, 10))}${u.tenant_id ? ` · tenant ${u.tenant_id}` : ""}</div>
      </div>
      <div class="user-controls">
        <select class="role-sel" data-role="${u.id}"${canEdit ? "" : " disabled"} title="${canEdit ? "Change role" : "Locked — protects your own / the last ADMIN account"}">
          ${USER_ROLES.map((r) => `<option value="${r}"${u.role === r ? " selected" : ""}>${r}</option>`).join("")}
        </select>
        <label class="sw" title="Enable / disable login for this account"><input type="checkbox" data-active="${u.id}"${u.is_active ? " checked" : ""}${canEdit ? "" : " disabled"} /><i></i></label>
        <button class="btn sm ghost" data-reset="${u.id}"${canEdit ? "" : " disabled"} title="Set a new password">Reset pw</button>
        <button class="btn sm danger" data-del="${u.id}"${canEdit ? "" : " disabled"} title="Remove account">Delete</button>
      </div>
    </div>`;
  }).join("");

  [...el.querySelectorAll("[data-role]")].forEach((s) => s.addEventListener("change", async () => {
    const res = await api(`/api/v1/users/${s.dataset.role}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: s.value }),
    });
    if (!res.ok) { toast(`Role change rejected (${res.status})`, "err"); loadUsers(); return; }
    toast(`Role updated → ${s.value}`, "ok");
    loadUsers();
  }));
  [...el.querySelectorAll("[data-active]")].forEach((c) => c.addEventListener("change", async () => {
    const res = await api(`/api/v1/users/${c.dataset.active}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_active: c.checked }),
    });
    if (!res.ok) { toast(`Could not ${c.checked ? "enable" : "disable"} (${res.status})`, "err"); loadUsers(); return; }
    toast(c.checked ? "Account enabled" : "Account disabled", "ok");
    loadUsers();
  }));
  [...el.querySelectorAll("[data-reset]")].forEach((b) => b.addEventListener("click", () => askResetPassword(b.dataset.reset)));
  [...el.querySelectorAll("[data-del]")].forEach((b) => b.addEventListener("click", () => askDeleteUser(b.dataset.del)));
}

async function addUser() {
  const username = $("us-username").value.trim();
  const password = $("us-password").value;
  const fullName = $("us-fullname").value.trim();
  const role = $("us-role").value;
  if (!username || !password) { toast("Username and password are required", "err"); return; }
  if (password.length < 6) { toast("Password must be at least 6 characters", "err"); return; }
  const res = await api("/api/v1/users", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, full_name: fullName || null, role }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(`Create failed: ${data.detail || res.status}`, "err"); return; }
  toast(`Created ${username} (${role}) ✓`, "ok");
  $("us-username").value = "";
  $("us-password").value = "";
  $("us-fullname").value = "";
  loadUsers();
}

async function askResetPassword(id) {
  userModal({
    title: "Reset password",
    text: "Set a brand-new password for this account.",
    input: true, okLabel: "Set password",
    onOk: async () => {
      const pw = $("user-modal-input").value;
      if (pw.length < 6) { toast("Password must be at least 6 characters", "err"); return; }
      const res = await api(`/api/v1/users/${id}/reset-password`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
      if (res.ok) { toast("Password updated ✓", "ok"); $("user-modal").hidden = true; }
      else toast(`Reset failed (${res.status})`, "err");
      loadUsers();
    },
  });
}

async function askDeleteUser(id) {
  userModal({
    title: "Delete account?",
    text: "This permanently removes the account — the user can no longer log in.",
    okLabel: "Delete",
    onOk: async () => {
      const res = await api(`/api/v1/users/${id}`, { method: "DELETE" });
      if (res.ok) { toast("Account deleted", "ok"); $("user-modal").hidden = true; }
      else toast(`Delete rejected (${res.status})`, "err");
      loadUsers();
    },
  });
}

async function loadSessions() {
  const el = $("sessions-list");
  const res = await api("/api/v1/users/sessions");
  if (!res.ok) {
    el.innerHTML = `<p class="sub">Sessions unavailable (${res.status}) — admin only.</p>`;
    return;
  }
  const sessions = await res.json();
  if (!sessions.length) {
    el.innerHTML = `<p class="sub">No active sessions — every login has expired or been signed out.</p>`;
    return;
  }
  const total = sessions.length;
  // Group sessions per user so the admin sees "who is signed in" first,
  // with each device underneath. Keys keep the newest user first.
  const byUser = new Map();
  for (const s of sessions) {
    if (!byUser.has(s.user_id)) byUser.set(s.user_id, []);
    byUser.get(s.user_id).push(s);
  }
  const groups = [...byUser.values()].sort(
    (a, b) => (b[0].last_used_at || "").localeCompare(a[0].last_used_at || ""));
  el.innerHTML = `<p class="sub" style="margin:0 0 10px">${total} active session${total === 1 ? "" : "s"} across ${groups.length} user${groups.length === 1 ? "" : "s"}.</p>` +
    groups.map((rows) => {
      const u = rows[0];
      const deviceCount = rows.length;
      const devices = rows.map((s) => {
        const ua = (s.user_agent || "").replace(/\s+/g, " ").slice(0, 60);
        return `
      <div class="node-item user-row" data-id="${esc(s.id)}" style="border-top:1px dashed var(--line,#2a3350)">
        <div class="cell-main" style="padding-left:14px">
          <div class="sub mono">${esc(s.ip_address || "unknown ip")} · ${esc(ua || "unknown device")}</div>
          <div class="sub">first ${esc((s.created_at || "").slice(0, 16).replace("T", " "))} · last use ${esc((s.last_used_at || "").slice(0, 16).replace("T", " "))} · expires ${esc((s.expires_at || "").slice(0, 16).replace("T", " "))}</div>
        </div>
        <div class="user-controls">
          <button class="btn sm danger" data-revoke="${esc(s.id)}" title="Sign this device out immediately">Revoke</button>
        </div>
      </div>`;
      }).join("");
      return `
    <div class="session-group" style="margin:0 0 14px">
      <div class="node-item user-row">
        <div class="user-avatar" style="background:${avatarColor(u.username || "?")}">${esc((u.username || "?")[0].toUpperCase())}</div>
        <div class="cell-main">
          <div class="name">${esc(u.full_name || u.username)}
            <span class="mwl-tag" style="background:rgba(99,102,241,.16);color:#a5b4fc;border-color:rgba(99,102,241,.35)">${esc(u.role || "")}</span>
            <span class="mwl-tag">${deviceCount} device${deviceCount === 1 ? "" : "s"}</span>
          </div>
          <div class="sub mono">@${esc(u.username)}</div>
        </div>
        <div class="user-controls">
          <button class="btn sm danger" data-revoke-user="${esc(u.user_id)}" title="Sign ${esc(u.username)} out of every device">Revoke all</button>
        </div>
      </div>
      ${devices}
    </div>`;
    }).join("");

  [...el.querySelectorAll("[data-revoke]")].forEach((b) => b.addEventListener("click", async () => {
    const id = b.dataset.revoke;
    const res = await api(`/api/v1/users/sessions/${id}/revoke`, { method: "POST" });
    if (!res.ok) { toast(`Revoke failed (${res.status})`, "err"); return; }
    toast("Session revoked — that device is signed out", "ok");
    loadSessions();
  }));
  [...el.querySelectorAll("[data-revoke-user]")].forEach((b) => b.addEventListener("click", async () => {
    const uid = b.dataset.revokeUser;
    const res = await api(`/api/v1/users/sessions/revoke-user/${uid}`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { toast(`Revoke all failed (${res.status})`, "err"); return; }
    toast(`Signed out ${data.revoked ?? 0} device(s)`, "ok");
    loadSessions();
  }));
}

/* ----- Auth audit trail (admin console → Auth Audit tab) ----- */
function authAuditBadge(action) {
  const map = {
    login_success: ["ok", "LOGIN"],
    login_failed: ["danger", "FAILED"],
    account_locked: ["danger", "LOCKED"],
    logout: ["muted", "LOGOUT"],
    token_refresh: ["muted", "REFRESH"],
    token_theft_detected: ["danger", "THEFT"],
    password_change: ["accent", "PASSWORD"],
    role_changed: ["accent", "ROLE"],
    session_revoked: ["accent", "REVOKED"],
    sessions_revoked_all: ["accent", "REVOKED ALL"],
    lockout_reset: ["ok", "UNLOCKED"],
  };
  const [cls, label] = map[action] || ["muted", action];
  return `<span class="audit-action ${cls}">${label}</span>`;
}

async function fillAuthAuditUsers() {
  const sel = $("aa-user");
  const res = await api("/api/v1/users");
  if (!res.ok) return;
  const users = await res.json();
  const current = sel.value;
  sel.innerHTML = `<option value="">All users</option>` +
    users.map((u) => `<option value="${u.id}">${esc(u.username)} (${esc(u.role || "")})</option>`).join("");
  sel.value = current;
}

async function loadAuthAudit() {
  const el = $("auth-audit-list");
  const params = new URLSearchParams({ limit: $("aa-limit").value });
  if ($("aa-action").value) params.set("action", $("aa-action").value);
  if ($("aa-user").value) params.set("user_id", $("aa-user").value);
  const res = await api(`/api/v1/users/auth-audit?${params}`);
  if (!res.ok) { el.innerHTML = `<p class="sub">Failed (${res.status}) — admin only.</p>`; return; }
  const rows = await res.json();
  if (!rows.length) {
    el.innerHTML = `<p class="sub">No auth events match the current filter.</p>`;
    return;
  }
  el.innerHTML = rows.map((e) => {
    const detail = e.details && Object.keys(e.details).length
      ? `<div class="sub mono faint">${esc(JSON.stringify(e.details))}</div>` : "";
    return `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${authAuditBadge(e.action)}
          <span class="mono faint">${e.username ? `@${esc(e.username)}` : "unknown"}</span>
          <span class="mono faint">· ${e.timestamp ? new Date(e.timestamp).toLocaleString() : ""}</span></div>
        <div class="sub mono">${esc(e.ip_address || "—")}${e.user_agent ? ` · ${esc(e.user_agent.slice(0, 70))}` : ""}</div>
        ${detail}
      </div>
    </div>`;
  }).join("");
}

async function loadLocked() {
  const el = $("locked-list");
  const res = await api("/api/v1/users/locked");
  if (!res.ok) { el.hidden = true; return; }
  const locked = await res.json();
  if (!locked.length) { el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = `<p class="sub" style="margin:0 0 8px">🔒 Locked accounts (brute-force guard) — unlock restores access immediately.</p>` +
    locked.map((u) => `
    <div class="node-item user-row" data-id="${u.id}">
      <div class="user-avatar" style="background:${avatarColor(u.username)}">${esc((u.username[0] || "?").toUpperCase())}</div>
      <div class="cell-main">
        <div class="name">${esc(u.full_name || u.username)} <span class="wl-status cx">locked</span></div>
        <div class="sub mono">@${esc(u.username)} · ${u.failed_attempts} failed attempt${u.failed_attempts === 1 ? "" : "s"} · unlocks in ~${u.remaining_minutes} min</div>
      </div>
      <div class="user-controls">
        <button class="btn sm ghost" data-unlock="${u.id}" title="Clear the lock and failed-attempt counter now">Reset lockout</button>
      </div>
    </div>`).join("");
  [...el.querySelectorAll("[data-unlock]")].forEach((b) => b.addEventListener("click", async () => {
    const id = b.dataset.unlock;
    const res = await api(`/api/v1/users/${id}/reset-lockout`, { method: "POST" });
    if (!res.ok) { toast(`Unlock failed (${res.status})`, "err"); return; }
    toast("Account unlocked ✓", "ok");
    loadLocked();
    loadAuthAudit();
  }));
}

/* ----- DICOM services matrix ----- */
function svcBadge(on) {
  return on
    ? '<span class="wl-status done">● live</span>'
    : '<span class="wl-status cx">○ off</span>';
}

async function loadServices() {
  const grid = $("services-grid");
  const web = $("services-web");
  const health = await (await api("/api/v1/health")).json().catch(() => null);
  if (!health?.services) { grid.innerHTML = `<p class="sub">Failed to load services.</p>`; return; }
  const sv = health.services;
  const dimse = sv.dicom || [];
  const enabledCount = dimse.filter((d) => d.enabled).length;
  grid.innerHTML = dimse.map((d) => `
    <div class="svc-card ${d.enabled ? "on" : "off"}">
      <div class="svc-head">${svcBadge(d.enabled)} <b>${esc(d.name)}</b></div>
      <div class="svc-sub mono">${esc(d.roles)}</div>
      <div class="svc-sub mono faint">${esc(d.sop_class)}</div>
    </div>`).join("")
    + `<div class="svc-card sum"><div class="svc-head">${svcBadge(sv.dicom_enabled)} <b>${enabledCount}/${dimse.length} DIMSE services live</b></div>`
    + `<div class="svc-sub mono">${sv.tls_enabled ? "🔒 TLS on" : "plaintext"} · ${sv.dicom_enabled ? "receiver up" : "receiver off"}</div></div>`;
  web.innerHTML = (sv.web || []).map((w) => `
    <div class="svc-card ${w.enabled ? "on" : "off"}">
      <div class="svc-head">${svcBadge(w.enabled)} <b>${esc(w.name)}</b></div>
      <div class="svc-sub mono">${esc(w.detail)}</div>
    </div>`).join("");
}

/* ----- system overview ----- */
async function loadSysInfo() {
  const grid = $("sys-grid");
  const health = await (await api("/api/v1/health")).json().catch(() => null);
  if (!health) { grid.innerHTML = `<p class="sub">Failed to load system info.</p>`; return; }
  const rows = [
    ["Application", `${esc(health.app)} <span class="mono faint">v${esc(health.version)}</span>`],
    ["Node instance", esc(health.instance || "node-1")],
    ["Database", health.database === "ok" ? '<span class="ok-dot"></span>ok' : '<span class="err-dot"></span>error'],
    ["DICOM receiver", health.dicom?.enabled ? `on · ${esc(health.dicom.ae_title)} :${health.dicom.port}` : "off"],
    ["DICOM TLS", health.tls?.enabled ? '<span class="badge tls">encrypted 🔒</span>' : '<span class="muted-badge">plaintext</span>'],
    ["HL7 (ADT + ORM)", health.hl7?.enabled ? `${esc(health.hl7.host)}:${health.hl7.port}` : "off"],
    ["FHIR R4", health.fhir?.enabled ? "on" : "off"],
    ["Storage backend", `${esc(health.storage?.backend || "local")}${health.storage?.backend === "s3" ? ` · ${esc(health.storage.endpoint)} / ${esc(health.storage.bucket)}` : ""}`],
    ["Compression", health.compression?.enabled ? esc(health.compression.method) : "off"],
    ["Rate limit", `${health.rate_limit_per_minute}/min`],
  ];
  const s = state.features;
  if (s) {
    rows.push(
      ["Users", String(s.users ?? 0)],
      ["DICOM nodes", String(s.nodes ?? 0)],
      ["Active shares", String(s.active_shares ?? 0)],
      ["Key image notes", String(s.features?.kin?.notes ?? 0)],
      ["Audit events", String(s.features?.audit?.events ?? 0)],
      ["VNA tenants", `${String(s.features?.tenants?.total ?? 0)} (${String(s.features?.tenants?.active ?? 0)} active)`],
      ["Pending jobs", String(s.pending_jobs ?? 0)],
    );
  }
  grid.innerHTML = rows.map(([k, v]) =>
    `<div class="sys-row"><span class="sys-k">${k}</span><span class="sys-v">${v}</span></div>`).join("");
}

/* ----- smart router ----- */
async function fillNodeSelect() {
  const res = await api("/api/v1/nodes");
  const sel = $("rt-node");
  if (!res.ok) { sel.innerHTML = `<option value="">Sign in as admin</option>`; sel.disabled = true; return; }
  const nodes = await res.json();
  sel.innerHTML = nodes.length
    ? nodes.map((n) => `<option value="${n.id}">${esc(n.ae_title)} (${esc(n.host)}:${n.port})</option>`).join("")
    : `<option value="">No nodes — add one first</option>`;
  sel.disabled = !nodes.length;
}

async function loadRouter() {
  const res = await api("/api/v1/routing/rules");
  if (!res.ok) { $("router-list").innerHTML = `<p class="sub">Failed (${res.status}) — sign in as admin.</p>`; return; }
  const rules = await res.json();
  if (!rules.length) { $("router-list").innerHTML = `<p class="sub">No routing rules yet. Add one to auto-forward matching studies to a peer node.</p>`; return; }
  $("router-list").innerHTML = rules.map((r) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(r.name)} ${r.enabled ? '<span class="wl-status open">on</span>' : '<span class="wl-status cx">off</span>'}
          <span class="mono faint">prio ${r.priority}</span></div>
        <div class="sub mono">→ ${esc(r.destination_ae_title || r.destination_node_id)}${r.modality ? ` · ${esc(r.modality)}` : ""}${r.body_part_examined ? ` · ${esc(r.body_part_examined)}` : ""}${r.study_description_pattern ? ` · “${esc(r.study_description_pattern)}”` : ""}</div>
      </div>
      <div class="actions">
        <button class="btn sm" data-rt-toggle="${r.id}" data-rt-enabled="${r.enabled}">${r.enabled ? "Disable" : "Enable"}</button>
        <button class="btn sm danger" data-rt-del="${r.id}">Del</button>
      </div>
    </div>`).join("");
  [...document.querySelectorAll("#router-list [data-rt-toggle]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/routing/rules/${b.dataset.rtToggle}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: b.dataset.rtEnabled !== "true" }),
      });
      toast(res.ok ? "Rule updated" : `Update failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadRouter();
    }));
  [...document.querySelectorAll("#router-list [data-rt-del]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/routing/rules/${b.dataset.rtDel}`, { method: "DELETE" });
      toast(res.ok ? "Rule deleted" : `Delete failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadRouter();
    }));
}

async function addRoutingRule() {
  const nodeId = parseInt($("rt-node").value, 10);
  const name = $("rt-name").value.trim();
  if (!name || !nodeId) { toast("Rule name and destination node are required", "err"); return; }
  const priority = $("rt-priority");
  const delay = $("rt-delay");
  if (!priority.value || !priority.checkValidity() || !delay.value || !delay.checkValidity()) {
    toast("Enter a valid routing priority and delay.", "err");
    return;
  }
  const res = await api("/api/v1/routing/rules", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      destination_node_id: nodeId,
      modality: $("rt-modality").value.trim().toUpperCase() || null,
      body_part_examined: $("rt-body").value.trim().toUpperCase() || null,
      source_aet: $("rt-source").value.trim() || null,
      priority: Number(priority.value),
      delay_seconds: Number(delay.value),
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(data.detail || `Add failed (${res.status})`, "err"); return; }
  ["rt-name", "rt-modality", "rt-body"].forEach((id) => ($(id).value = ""));
  toast(`Rule “${data.name}” added`, "ok");
  loadRouter();
  loadRoutingQueue();
}

/* ----- routing send queue (C1) ----- */
function rqBadge(status) {
  const map = {
    COMPLETED: ["done", "✓ sent"],
    FAILED: ["cx", "✕ failed"],
    SENDING: ["prog", "● sending"],
  };
  const [cls, label] = map[status] || ["open", status.toLowerCase()];
  return `<span class="wl-status ${cls}">${label}</span>`;
}

async function loadRoutingQueue() {
  const status = $("rq-filter").value;
  const res = await api(`/api/v1/routing/queue${status ? `?status=${encodeURIComponent(status)}` : ""}`);
  if (!res.ok) { $("routing-queue-list").innerHTML = `<p class="sub">Failed (${res.status}) — sign in as admin.</p>`; return; }
  const rows = await res.json();
  if (!rows.length) { $("routing-queue-list").innerHTML = `<p class="sub">No dispatches yet. Matching studies appear here and are auto-forwarded to their destination node with retry &amp; backoff.</p>`; return; }
  $("routing-queue-list").innerHTML = rows.map((q) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name"><span class="mono">${esc(q.study_instance_uid)}</span> ${rqBadge(q.status)}
          <span class="mono faint">${q.attempts}/${q.max_attempts} attempts</span></div>
        <div class="sub mono">→ ${esc(q.node_ae_title || q.node_id)}${q.next_retry_at ? ` · retry ${new Date(q.next_retry_at).toLocaleString()}` : ""}${q.sent_count || q.failed_count ? ` · ${q.sent_count} sent / ${q.failed_count} failed` : ""}${q.created_at ? ` · ${new Date(q.created_at).toLocaleString()}` : ""}</div>
        ${q.last_error ? `<div class="sub" style="color:var(--danger)">${esc(q.last_error)}</div>` : ""}
      </div>
      <div class="actions">
        ${q.status === "FAILED" ? `<button class="btn sm" data-rq-retry="${q.id}" title="Re-queue for immediate dispatch">Retry</button>` : ""}
      </div>
    </div>`).join("");
  [...document.querySelectorAll("[data-rq-retry]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/routing/queue/${b.dataset.rqRetry}/retry`, { method: "POST" });
      toast(res.ok ? "Dispatch re-queued — will retry on next sweep" : `Retry failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadRoutingQueue();
    }));
}

/* ----- critical results ----- */
function crLevel(level) {
  const map = {
    CRITICAL_RED: ["red", "CRITICAL"],
    URGENT_ORANGE: ["org", "URGENT"],
    ACTIONABLE_YELLOW: ["ylw", "ACTIONABLE"],
  };
  const [cls, label] = map[level] || ["", level];
  return `<span class="cr-level ${cls}">${label}</span>`;
}

async function loadCritical() {
  const res = await api("/api/v1/critical-results");
  if (!res.ok) { $("critical-list").innerHTML = `<p class="sub">Failed (${res.status}) — sign in.</p>`; return; }
  const rows = await res.json();
  if (!rows.length) { $("critical-list").innerHTML = `<p class="sub">No critical results. Flag one to begin the acknowledgement loop with the referring physician.</p>`; return; }
  $("critical-list").innerHTML = rows.map((r) => {
    const ack = r.status === "ACKNOWLEDGED"
      ? `<span class="wl-status done">✓ acknowledged${r.acknowledged_by ? ` by ${esc(r.acknowledged_by)}` : ""}</span>`
      : '<span class="wl-status open">⏳ pending ack</span>';
    return `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${crLevel(r.finding_level)} ${esc(r.description || r.id)} ${ack}</div>
        <div class="sub mono">study ${esc(r.study_instance_uid)}${r.patient_id ? ` · ${esc(r.patient_id)}` : ""}${r.reported_by ? ` · by ${esc(r.reported_by)}` : ""}${r.communicated_to ? ` → ${esc(r.communicated_to)}` : ""}${r.created_at ? ` · ${new Date(r.created_at).toLocaleString()}` : ""}</div>
        ${r.acknowledgement_notes ? `<div class="sub">${esc(r.acknowledgement_notes)}</div>` : ""}
      </div>
      <div class="actions">
        ${r.status !== "ACKNOWLEDGED" ? `<button class="btn sm" data-ack="${r.id}">Acknowledge ✓</button>` : ""}
      </div>
    </div>`;
  }).join("");
  [...document.querySelectorAll("#critical-list [data-ack]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/critical-results/${b.dataset.ack}/acknowledge`, { method: "POST" });
      toast(res.ok ? "Critical result acknowledged" : `Failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) { loadCritical(); loadFeatures(); }
    }));
}

async function addCriticalResult() {
  const study = $("cr-study").value.trim();
  const desc = $("cr-desc").value.trim();
  if (!study || !desc) { toast("Study UID and description are required", "err"); return; }
  const res = await api("/api/v1/critical-results", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ study_instance_uid: study, description: desc, finding_level: $("cr-level").value }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(data.detail || `Flag failed (${res.status})`, "err"); return; }
  $("cr-study").value = ""; $("cr-desc").value = "";
  toast("Critical result flagged", "ok");
  loadCritical();
  loadFeatures();
}

/* ----- storage & compression ----- */
async function loadStoragePanel() {
  const health = await (await api("/api/v1/health")).json().catch(() => null);
  const st = health?.storage || {};
  $("storage-info").innerHTML = [
    ["Backend", esc(st.backend || "local")],
    ["Endpoint", esc(st.endpoint || "—")],
    ["Bucket", esc(st.bucket || "—")],
    ["Presign expiry", st.presign_expires ? `${st.presign_expires} s` : "n/a (local)"],
    ["Named backends", `${st.backends != null ? st.backends : 0} registered`],
  ].map(([k, v]) => `<div class="sys-row"><span class="sys-k">${k}</span><span class="sys-v">${v}</span></div>`).join("");
  loadBackends();
}

/* ----- multi-storage backends ----- */
async function loadBackends() {
  const list = $("backend-list");
  const mv = $("mv-target");
  try {
    const res = await api("/api/v1/storage/backends");
    const data = await res.json();
    if (!res.ok) { list.innerHTML = `<p class="sub">${esc(data.detail || "failed to load")}</p>`; return; }
    const bks = data.backends || [];
    mv.innerHTML = `<option value="">— pick —</option>` + bks.map((b) =>
      `<option value="${esc(b.name)}" ${b.enabled ? "" : "disabled"}>${esc(b.name)} (${b.kind})</option>`).join("");
    if (!bks.length) {
      list.innerHTML = `<p class="sub">No registered backends — the env backend (${esc(data.env_backend?.kind || "local")}) is active with ${data.env_backend?.instances || 0} instances. Register one below.</p>`;
      return;
    }
    list.innerHTML = bks.map((b) => `
      <div class="node-row bk-row ${b.enabled ? "" : "bk-disabled"}">
        <div class="bk-main">
          <span class="bk-name">${esc(b.name)} ${b.is_default ? '<span class="bk-def">default</span>' : ""}${b.enabled ? "" : ' <span class="bk-off">disabled</span>'}</span>
          <span class="bk-meta">${esc(b.kind)} · ${esc(b.kind === "local" ? (b.root_path || "?") : ((b.endpoint_url || "s3") + "/" + (b.bucket || "?")))}</span>
          <span class="bk-meta">${b.instances || 0} instances · ${fmtBytes(b.usage_bytes || 0)}</span>
        </div>
        <div class="bk-actions">
          ${b.is_default ? "" : `<button class="btn mini" data-bk="${esc(b.name)}" data-act="default">Make default</button>`}
          <button class="btn mini" data-bk="${esc(b.name)}" data-act="toggle">${b.enabled ? "Disable" : "Enable"}</button>
          <button class="btn mini" data-bk="${esc(b.name)}" data-act="test">Test</button>
          <button class="btn mini danger" data-bk="${esc(b.name)}" data-act="delete" ${b.instances ? "title=\"has instances — move them first\"" : ""}>${b.instances ? "In use" : "Delete"}</button>
        </div>
      </div>`).join("");
  } catch (e) {
    list.innerHTML = `<p class="sub">load failed: ${esc(String(e))}</p>`;
  }
}

function fmtBytes(n) {
  if (n >= 1 << 30) return (n / (1 << 30)).toFixed(2) + " GiB";
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MiB";
  if (n >= 1 << 10) return (n / (1 << 10)).toFixed(0) + " KiB";
  return String(n) + " B";
}

async function addBackend() {
  const name = $("bk-name").value.trim();
  const kind = $("bk-kind").value;
  if (!name) { toast("Backend name required", "err"); return; }
  const body = { name, kind };
  if (kind === "local") {
    const root = $("bk-endpoint").value.trim();
    if (!root) { toast("Root path required for local backends", "err"); return; }
    body.root_path = root;
  } else {
    body.endpoint_url = $("bk-endpoint").value.trim() || null;
    body.bucket = $("bk-bucket").value.trim();
    if (!body.bucket) { toast("Bucket required for s3 backends", "err"); return; }
    body.region = $("bk-region").value.trim() || "us-east-1";
    body.access_key = $("bk-access").value.trim() || null;
    body.secret_key = $("bk-secret").value.trim() || null;
  }
  const res = await api("/api/v1/storage/backends", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (res.ok) {
    toast("Backend registered", "ok");
    ["bk-name", "bk-endpoint", "bk-bucket", "bk-access", "bk-secret"].forEach((id) => { const el = $(id); if (el) el.value = ""; });
    loadBackends(); loadFeatures();
  } else {
    const d = await res.json().catch(() => ({}));
    toast(d.detail || "failed to register", "err");
  }
}

async function backendAction(name, act) {
  if (act === "test") {
    const res = await api(`/api/v1/storage/backends/${encodeURIComponent(name)}/test`, { method: "POST" });
    const d = await res.json().catch(() => ({}));
    if (d.ok) toast(`${name}: connected (${d.detail || "ok"})`, "ok");
    else toast(`${name}: ${d.detail || "test failed"}`, "err");
    return;
  }
  if (act === "delete") {
    const res = await api(`/api/v1/storage/backends/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (res.ok) { toast(`Backend ${name} deleted`, "ok"); loadBackends(); loadFeatures(); }
    else { const d = await res.json().catch(() => ({})); toast(d.detail || "delete failed", "err"); }
    return;
  }
  const patch = act === "toggle" ? { enabled: null } : { is_default: true };
  if (act === "toggle") {
    const row = [...document.querySelectorAll(".bk-row")].find((r) => r.querySelector(`[data-bk="${CSS.escape(name)}"][data-act="toggle"]`));
    patch.enabled = !(row && row.classList.contains("bk-disabled"));
  }
  const res = await api(`/api/v1/storage/backends/${encodeURIComponent(name)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
  if (res.ok) { toast(`${name} updated`, "ok"); loadBackends(); loadFeatures(); }
  else { const d = await res.json().catch(() => ({})); toast(d.detail || "update failed", "err"); }
}

async function moveStudy() {
  const study = $("mv-study").value.trim();
  const target = $("mv-target").value;
  if (!study || !target) { toast("Study UID and target backend required", "err"); return; }
  const res = await api(`/api/v1/storage/studies/${encodeURIComponent(study)}/move`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target_backend: target }) });
  const d = await res.json().catch(() => ({}));
  if (res.ok) {
    toast(`Moved ${d.moved || 0} objects to ${target}`, "ok");
    $("mv-study").value = "";
    loadBackends(); loadStudies(); loadFeatures();
  } else {
    toast(d.detail || "move failed", "err");
  }
}

async function loadLossy() {
  const res = await api("/api/v1/compression/lossy/rules");
  if (!res.ok) { $("lossy-list").innerHTML = `<p class="sub">Failed (${res.status}) — sign in as admin.</p>`; return; }
  const rules = await res.json();
  if (!rules.length) { $("lossy-list").innerHTML = `<p class="sub">No lossy compression rules. Add one to auto-transcode aged studies (e.g. CT after 365 days at quality 85).</p>`; return; }
  $("lossy-list").innerHTML = rules.map((r) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(r.name)} ${r.enabled ? '<span class="wl-status open">on</span>' : '<span class="wl-status cx">off</span>'}</div>
        <div class="sub mono">${r.modality ? `${esc(r.modality)} · ` : ""}age ≥ ${r.min_age_days}d · q${r.quality_factor} · ${esc(r.transfer_syntax_uid)}</div>
      </div>
      <div class="actions">
        <button class="btn sm" data-rt-toggle="${r.id}" data-rt-enabled="${r.enabled}">${r.enabled ? "Disable" : "Enable"}</button>
        <button class="btn sm danger" data-lc-del="${r.id}">Del</button>
      </div>
    </div>`).join("");
  [...document.querySelectorAll("#lossy-list [data-rt-toggle]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/compression/lossy/rules/${b.dataset.rtToggle}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: b.dataset.rtEnabled !== "true" }),
      });
      toast(res.ok ? "Rule updated" : `Update failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadLossy();
    }));
  [...document.querySelectorAll("#lossy-list [data-lc-del]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/compression/lossy/rules/${b.dataset.lcDel}`, { method: "DELETE" });
      toast(res.ok ? "Rule deleted" : `Delete failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadLossy();
    }));
}

async function addLossyRule() {
  const name = $("lc-name").value.trim();
  if (!name) { toast("Rule name is required", "err"); return; }
  const age = $("lc-age");
  const quality = $("lc-quality");
  if (!age.value || !age.checkValidity() || !quality.value || !quality.checkValidity()) {
    toast("Enter a valid age and quality factor.", "err");
    return;
  }
  const res = await api("/api/v1/compression/lossy/rules", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      modality: $("lc-modality").value.trim().toUpperCase() || null,
      min_age_days: Number(age.value),
      quality_factor: Number(quality.value),
      transfer_syntax_uid: $("lc-syntax").value,
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(data.detail || `Add failed (${res.status})`, "err"); return; }
  $("lc-name").value = ""; $("lc-modality").value = "";
  toast(`Rule “${data.name}” added`, "ok");
  loadLossy();
  loadFeatures();
}

async function runLossy() {
  if (!confirm("Run enabled lossy rules now? Matching original pixel values will be permanently replaced.")) return;
  const btn = $("lc-run");
  btn.disabled = true;
  const res = await api("/api/v1/compression/lossy/run", { method: "POST" });
  btn.disabled = false;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(data.detail || `Run failed (${res.status})`, "err"); return; }
  if (data.disabled) { toast(data.message, "err"); return; }
  toast(`Compression run finished — ${data.processed_instances ?? 0} instance(s) processed`, "ok");
}

/* ----- study lifecycle (auto-archive / restore / purge) ----- */
async function loadLifecycle() {
  const res = await api("/api/v1/lifecycle/rules");
  if (!res.ok) { $("lifecycle-list").innerHTML = `<p class="sub">Failed (${res.status}) — sign in as admin.</p>`; return; }
  const rules = await res.json();
  loadLifecycleStudies();
  if (!rules.length) { $("lifecycle-list").innerHTML = `<p class="sub">No lifecycle rules yet. Add one to auto-tier aged studies — e.g. ARCHIVE CT after 365 days, or PURGE after retention expiry.</p>`; return; }
  $("lifecycle-list").innerHTML = rules.map((r) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(r.name)} ${r.enabled ? '<span class="wl-status open">on</span>' : '<span class="wl-status cx">off</span>'}
          <span class="mono faint">prio ${r.priority}</span></div>
        <div class="sub mono">${esc(r.action)}${r.modality ? ` · ${esc(r.modality)}` : ""}${r.body_part_examined ? ` · ${esc(r.body_part_examined)}` : ""} · age ≥ ${r.min_age_days}d</div>
      </div>
      <div class="actions">
        <button class="btn sm" data-lc2-toggle="${r.id}" data-lc2-enabled="${r.enabled}" data-lc2-action="${esc(r.action)}">${r.enabled ? "Disable" : "Enable"}</button>
        <button class="btn sm danger" data-lc2-del="${r.id}">Del</button>
      </div>
    </div>`).join("");
  [...document.querySelectorAll("#lifecycle-list [data-lc2-toggle]")].forEach((b) =>
    b.addEventListener("click", async () => {
      if (b.dataset.lc2Action === "PURGE" && b.dataset.lc2Enabled !== "true" &&
          !confirm("Enable this purge rule? Matching archived studies may be permanently deleted by the worker.")) return;
      const res = await api(`/api/v1/lifecycle/rules/${b.dataset.lc2Toggle}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: b.dataset.lc2Enabled !== "true" }),
      });
      toast(res.ok ? "Rule updated" : `Update failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadLifecycle();
    }));
  [...document.querySelectorAll("#lifecycle-list [data-lc2-del]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/lifecycle/rules/${b.dataset.lc2Del}`, { method: "DELETE" });
      toast(res.ok ? "Rule deleted" : `Delete failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadLifecycle();
    }));
}

async function loadLifecycleStudies() {
  const res = await api("/api/v1/lifecycle/studies?state=ARCHIVED");
  if (!res.ok) { $("lifecycle-studies").innerHTML = `<p class="sub">Failed (${res.status}) — sign in.</p>`; return; }
  const rows = await res.json();
  if (!rows.length) { $("lifecycle-studies").innerHTML = `<p class="sub">No archived studies — the cold tier is empty.</p>`; return; }
  $("lifecycle-studies").innerHTML = rows.map((r) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(r.patient_name || "Unknown")} <span class="mono faint">${esc(r.patient_id || "")}</span></div>
        <div class="sub mono">${fmtDate(r.study_date)}${r.accession ? ` · ${esc(r.accession)}` : ""} · archived ${r.archived_at ? new Date(r.archived_at).toLocaleDateString() : "—"}</div>
      </div>
      <div class="actions">
        <button class="btn sm" data-restore="${encodeURIComponent(r.study_uid)}" title="Bring back to the hot tier">Restore</button>
        <button class="btn sm danger" data-purge="${encodeURIComponent(r.study_uid)}" title="Permanently delete — admin/radiologist only">Purge</button>
      </div>
    </div>`).join("");
  [...document.querySelectorAll("[data-restore]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/lifecycle/studies/${b.dataset.restore}/restore`, { method: "POST" });
      toast(res.ok ? "Study restored to hot tier" : `Restore failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) { loadLifecycle(); loadStudies(); loadFeatures(); }
    }));
  [...document.querySelectorAll("[data-purge]")].forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm("Permanently purge this study and all its files? This cannot be undone.")) return;
      const res = await api(`/api/v1/lifecycle/studies/${b.dataset.purge}/purge`, { method: "POST" });
      toast(res.ok ? "Study purged" : `Purge failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) { loadLifecycle(); loadStudies(); loadFeatures(); }
    }));
}

async function addLifecycleRule() {
  const name = $("lc2-name").value.trim();
  if (!name) { toast("Rule name is required", "err"); return; }
  const age = $("lc2-age");
  if (!age.value || !age.checkValidity()) { toast("Enter a valid minimum study age.", "err"); return; }
  const res = await api("/api/v1/lifecycle/rules", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      action: $("lc2-action").value,
      enabled: $("lc2-action").value !== "PURGE",
      modality: $("lc2-modality").value.trim().toUpperCase() || null,
      body_part_examined: $("lc2-body").value.trim().toUpperCase() || null,
      min_age_days: Number(age.value),
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(data.detail || `Add failed (${res.status})`, "err"); return; }
  $("lc2-name").value = ""; $("lc2-modality").value = ""; $("lc2-body").value = "";
  toast(`Lifecycle rule “${data.name}” added`, "ok");
  loadLifecycle();
  loadFeatures();
}

async function runLifecycle() {
  if (!confirm("Run enabled retention rules now? Enabled purge rules can permanently delete archived studies.")) return;
  const btn = $("lc2-run");
  btn.disabled = true;
  const res = await api("/api/v1/lifecycle/run", { method: "POST" });
  btn.disabled = false;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(data.detail || `Run failed (${res.status})`, "err"); return; }
  toast(`Lifecycle run finished — scanned ${data.scanned}, archived ${data.archived}, restored ${data.restored}, purged ${data.purged}`, "ok");
  loadLifecycle();
  loadFeatures();
}

/* ----- migration ----- */
async function loadMigration() {
  const res = await api("/api/v1/migration/jobs");
  if (!res.ok) { $("migration-list").innerHTML = `<p class="sub">Failed (${res.status}) — sign in.</p>`; return; }
  const jobs = await res.json();
  if (!jobs.length) { $("migration-list").innerHTML = `<p class="sub">No migration jobs yet. Point PAXS at a DICOM folder or request studies from a peer node via C-MOVE.</p>`; return; }
  $("migration-list").innerHTML = jobs.map((j) => {
    const badge = j.status === "COMPLETED" ? '<span class="wl-status done">✓ done</span>'
      : j.status === "RUNNING" ? '<span class="wl-status prog">● running</span>'
      : j.status === "FAILED" ? '<span class="wl-status cx">✕ failed</span>'
      : '<span class="wl-status open">⏳ queued</span>';
    const prog = j.total_items ? `${j.processed_items}/${j.total_items}` : `${j.processed_items ?? 0}`;
    return `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(j.job_type)} ${badge}</div>
        <div class="sub mono">${esc(j.source_info || "—")} · ${prog}${j.failed_items ? ` · <span style="color:var(--danger)">${j.failed_items} failed</span>` : ""}${j.created_at ? ` · ${new Date(j.created_at).toLocaleString()}` : ""}</div>
        ${j.error_message ? `<div class="sub" style="color:var(--danger)">${esc(j.error_message)}</div>` : ""}
      </div>
    </div>`;
  }).join("");
}

async function startFolderMigration() {
  const folder = $("mg-folder").value.trim();
  if (!folder) { toast("Folder path is required", "err"); return; }
  const res = await api("/api/v1/migration/folder", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ folder_path: folder, delete_after_import: false }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(data.detail || `Start failed (${res.status})`, "err"); return; }
  toast(`Migration job #${data.job?.id} started`, "ok");
  loadMigration();
}

/* ----- tenants (VNA) ----- */
async function loadTenants() {
  const res = await api("/api/v1/tenants");
  if (!res.ok) { $("tenants-list").innerHTML = `<p class="sub">Failed (${res.status}) — sign in as admin/radiologist.</p>`; return; }
  const rows = await res.json();
  if (!rows.length) { $("tenants-list").innerHTML = `<p class="sub">No tenants yet. Create one to start isolating departments (VNA); unmapped studies remain in the default scope.</p>`; return; }
  $("tenants-list").innerHTML = rows.map((t) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(t.name)} <span class="mono faint">${esc(t.code)}</span>
          ${t.is_active ? '<span class="wl-status open">active</span>' : '<span class="wl-status cx">inactive</span>'}
          <span class="mono faint">· ${t.study_count} studies</span></div>
        <div class="sub mono">${(t.ae_titles || []).length
          ? t.ae_titles.map((a) => `<span class="vna-ae">${esc(a)} <button class="vna-ae-x" data-tn-ae-del="${t.id}" data-ae="${encodeURIComponent(a)}" title="Unmap AE">×</button></span>`).join("")
          : '<span class="faint">no AE mappings — nothing lands here yet</span>'}</div>
        ${t.description ? `<div class="sub">${esc(t.description)}</div>` : ""}
        <div class="field-row" style="margin:8px 0 0;max-width:430px">
          <div class="field" style="min-width:170px;margin:0"><input data-tn-ae-input="${t.id}" placeholder="Map an AE title (e.g. CT1)" /></div>
          <button class="btn sm" data-tn-ae="${t.id}" style="align-self:flex-end">+ Map AE</button>
        </div>
      </div>
      <div class="actions">
        ${t.is_active
          ? `<button class="btn sm" data-tn-toggle="${t.id}" data-tn-active="true">Deactivate</button>`
          : `<button class="btn sm" data-tn-toggle="${t.id}" data-tn-active="false">Activate</button>`}
        <button class="btn sm danger" data-tn-del="${t.id}">Del</button>
      </div>
    </div>`).join("");

  [...document.querySelectorAll("[data-tn-ae]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const input = document.querySelector(`[data-tn-ae-input="${b.dataset.tnAe}"]`);
      const ae = ((input && input.value) || "").trim();
      if (!ae) { toast("Enter an AE title first", "err"); return; }
      const res = await api(`/api/v1/tenants/${b.dataset.tnAe}/aes`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ae_title: ae }),
      });
      const data = await res.json().catch(() => ({}));
      toast(res.ok ? `Mapped ${ae}` : (data.detail || `Failed (${res.status})`), res.ok ? "ok" : "err");
      if (res.ok) loadTenants();
    }));
  [...document.querySelectorAll("[data-tn-ae-del]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/tenants/${b.dataset.tnAeDel}/aes/${encodeURIComponent(b.dataset.ae)}`, { method: "DELETE" });
      toast(res.ok ? "AE unmapped" : `Failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadTenants();
    }));
  [...document.querySelectorAll("[data-tn-toggle]")].forEach((b) =>
    b.addEventListener("click", async () => {
      const res = await api(`/api/v1/tenants/${b.dataset.tnToggle}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: b.dataset.tnActive !== "true" }),
      });
      toast(res.ok ? "Tenant updated" : `Failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadTenants();
    }));
  [...document.querySelectorAll("[data-tn-del]")].forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm(`Delete tenant #${b.dataset.tnDel}? Its studies stay in the default scope.`)) return;
      const res = await api(`/api/v1/tenants/${b.dataset.tnDel}`, { method: "DELETE" });
      toast(res.ok ? "Tenant deleted" : `Failed (${res.status})`, res.ok ? "ok" : "err");
      if (res.ok) loadTenants();
    }));
}

async function addTenant() {
  const name = $("tn-name").value.trim();
  const code = $("tn-code").value.trim();
  if (!name || !code) { toast("Name and code are required", "err"); return; }
  const res = await api("/api/v1/tenants", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, code, description: $("tn-desc").value.trim() || null }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(data.detail || `Add failed (${res.status})`, "err"); return; }
  ["tn-name", "tn-code", "tn-desc"].forEach((id) => ($(id).value = ""));
  toast(`Tenant “${data.name}” created`, "ok");
  loadTenants();
  loadFeatures();
}

/* ----- audit ----- */
function auditBadge(action) {
  const map = {
    STORED: ["ok", "STORED"], RETRIEVED: ["muted", "RETRIEVED"],
    COMPRESSED: ["accent", "COMPRESSED"], SHARED: ["accent", "SHARED"],
    DELETED: ["danger", "DELETED"], LOGIN: ["muted", "LOGIN"],
    FORWARDED: ["accent", "FORWARDED"], AUTO_ROUTED: ["accent", "AUTO_ROUTED"],
  };
  const [cls, label] = map[action] || ["muted", action];
  return `<span class="audit-action ${cls}">${label}</span>`;
}

async function loadAudit() {
  const res = await api("/api/v1/audit?limit=60");
  if (!res.ok) { $("audit-list").innerHTML = `<p class="sub">Failed (${res.status}) — sign in.</p>`; return; }
  const rows = await res.json();
  if (!rows.length) { $("audit-list").innerHTML = `<p class="sub">No audit events yet. Ingestion, retrieval, sharing and deletion are all fingerprinted here.</p>`; return; }
  $("audit-list").innerHTML = rows.map((e) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${auditBadge(e.action)}<span class="mono faint">${e.performed_by ? `by ${esc(e.performed_by)}` : "system"}</span>
          <span class="mono faint">· ${e.performed_at ? new Date(e.performed_at).toLocaleString() : ""}</span></div>
        <div class="sub mono">${esc(e.sop_instance_uid || "—")}${e.sha256 ? ` · sha256 <span class="faint">${esc(e.sha256.slice(0, 16))}…</span>` : ""}</div>
      </div>
    </div>`).join("");
}

/* ----- custom server scripts (roadmap #43) ----- */
async function loadScripts() {
  const list = $("script-list");
  const src = $("script-source");
  src.hidden = true; src.textContent = "";
  try {
    const res = await api("/api/v1/scripts");
    const data = await res.json();
    if (!res.ok) { list.innerHTML = `<p class="sub">Failed (${res.status}) — sign in.</p>`; return; }
    const dir = $("scripts-dir");
    if (dir) dir.textContent = data.dir || "scripts/";
    const bks = data.scripts || [];
    if (!bks.length) {
      list.innerHTML = `<p class="sub">No scripts yet. Drop a <code>.py</code> file in <code>${esc(data.dir || "scripts/")}</code> — see <code>scripts/README.md</code> for the contract.</p>`;
      return;
    }
    list.innerHTML = bks.map((sc) => `
      <div class="node-row bk-row ${sc.enabled ? "" : "bk-disabled"}">
        <div class="bk-main">
          <span class="bk-name">🐍 ${esc(sc.name)} ${sc.loaded ? '<span class="bk-def">loaded</span>' : '<span class="bk-off">error</span>'}${sc.enabled ? "" : ' <span class="bk-off">disabled</span>'}</span>
          <span class="bk-meta">${sc.handlers && sc.handlers.length ? esc(sc.handlers.join(", ")) : "no handlers"}${sc.events ? ` · events: ${esc(sc.events.join(", "))}` : ""}</span>
          <span class="bk-meta">${sc.modified ? new Date(sc.modified).toLocaleString() : ""}</span>
        </div>
        <div class="bk-actions">
          <button class="btn mini" data-sc="${esc(sc.name)}" data-act="toggle">${sc.enabled ? "Disable" : "Enable"}</button>
          <button class="btn mini" data-sc="${esc(sc.name)}" data-act="run" title="Test-run with a sample StudyReceived event">Run ▶</button>
          <button class="btn mini" data-sc="${esc(sc.name)}" data-act="source">Source</button>
        </div>
      </div>`).join("");
  } catch (e) {
    list.innerHTML = `<p class="sub">load failed: ${esc(String(e))}</p>`;
  }
}

async function reloadScripts() {
  const res = await api("/api/v1/scripts/reload", { method: "POST" });
  const d = await res.json().catch(() => ({}));
  if (res.ok) toast(`Reloaded ${d.dropped || 0} cached module(s)`, "ok");
  else toast(d.detail || "reload failed", "err");
  loadScripts(); loadFeatures();
}

async function scriptAction(name, act) {
  if (act === "source") {
    const res = await api(`/api/v1/scripts/${encodeURIComponent(name)}/source`);
    const d = await res.json().catch(() => ({}));
    if (res.ok) {
      const el = $("script-source");
      el.textContent = d.source || "";
      el.hidden = false;
    } else toast(d.detail || "source unavailable", "err");
    return;
  }
  if (act === "run") {
    const res = await api(`/api/v1/scripts/${encodeURIComponent(name)}/run`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event: "StudyReceived", payload: { study_uid: "1.2.3.9.9.9", patient_id: "P-TEST" } }),
    });
    const d = await res.json().catch(() => ({}));
    if (res.ok) toast(`${name}: ran ${d.event || "ok"}`, "ok");
    else toast(`${name}: ${d.detail || d.error || "run failed"}`, "err");
    return;
  }
  if (act === "toggle") {
    const res = await api(`/api/v1/scripts/${encodeURIComponent(name)}/toggle`, { method: "POST" });
    const d = await res.json().catch(() => ({}));
    if (res.ok) toast(`${name} ${d.enabled ? "enabled" : "disabled"}`, "ok");
    else toast(d.detail || "toggle failed", "err");
    loadScripts();
  }
}

async function newScript() {
  const name = prompt("Script name (e.g. my_hook):");
  if (!name || !/^[A-Za-z0-9_]+$/.test(name)) { toast("Invalid script name", "err"); return; }
  const tpl = `# ${name} — PAXS custom server script (roadmap #43)\n\nEVENTS = ["StudyReceived"]\n\n\ndef on_study_received(ctx):\n    ctx.log.info("study %s received", ctx.payload.get("study_uid"))\n`;
  const res = await api(`/api/v1/scripts/${encodeURIComponent(name)}`, {
    method: "PUT", headers: { "Content-Type": "text/plain" }, body: tpl,
  });
  const d = await res.json().catch(() => ({}));
  if (res.ok) { toast(`Script ${name} created`, "ok"); loadScripts(); }
  else toast(d.detail || "create failed", "err");
}

/* ----- DR replication (roadmap #49) ----- */
async function loadReplication() {
  const info = $("replication-info");
  const list = $("replication-list");
  try {
    const [st, rec] = await Promise.all([
      api("/api/v1/replication").then((r) => r.json()).catch(() => null),
      api("/api/v1/replication/records?limit=30").then((r) => r.json()).catch(() => ({ records: [] })),
    ]);
    if (!st) { info.innerHTML = `<p class="sub">Failed to load replication status.</p>`; return; }
    const statusBadge = st.enabled
      ? '<span class="bk-def">enabled</span>'
      : '<span class="bk-off">disabled — set DR_REPLICATION_TARGET or STORAGE_BACKEND=s3</span>';
    info.innerHTML = [
      ["Status", statusBadge],
      ["Target", esc(st.target || "—")],
      ["Object prefix", `<code>${esc(st.prefix || "replica/")}</code>`],
      ["Replicated studies", String(st.replicated || 0)],
      ["Pending / failed", `${st.pending || 0} / ${st.failed || 0}`],
      ["Bytes off-site", fmtBytes(st.total_bytes || 0)],
    ].map(([k, v]) => `<div class="sys-row"><span class="sys-k">${k}</span><span class="sys-v">${v}</span></div>`).join("");
    const rows = rec.records || [];
    if (!rows.length) {
      list.innerHTML = `<p class="sub">No replication records yet — the worker enqueues new studies automatically when enabled.</p>`;
      return;
    }
    list.innerHTML = rows.map((r) => `
      <div class="node-row bk-row">
        <div class="bk-main">
          <span class="bk-name"><span class="mono">${esc(r.study_instance_uid)}</span> ${repBadge(r.status)}</span>
          <span class="bk-meta">target ${esc(r.target)} · ${r.object_count || 0} objects · ${fmtBytes(r.bytes_replicated || 0)} · attempt ${r.attempts || 0}</span>
          <span class="bk-meta">${r.last_error ? `<span class="err-text">${esc(String(r.last_error).slice(0, 140))}</span>` : (r.replicated_at ? new Date(r.replicated_at).toLocaleString() : "")}</span>
        </div>
      </div>`).join("");
  } catch (e) {
    list.innerHTML = `<p class="sub">load failed: ${esc(String(e))}</p>`;
  }
}

function repBadge(status) {
  if (status === "REPLICATED") return '<span class="bk-def">replicated ✓</span>';
  if (status === "FAILED") return '<span class="bk-off" style="color:#fb7185;border-color:rgba(248,113,113,.4)">failed</span>';
  return '<span class="bk-off" style="color:#fbbf24;border-color:rgba(251,191,36,.4)">pending</span>';
}

async function sweepReplication() {
  const res = await api("/api/v1/replication/sweep", { method: "POST" });
  const d = await res.json().catch(() => ({}));
  if (res.ok) {
    toast(`Sweep: ${d.replicated || 0} replicated · ${d.failed || 0} failed · ${d.pending || 0} pending`, "ok");
  } else {
    toast(d.detail || "sweep failed (is DR enabled?)", "err");
  }
  loadReplication(); loadFeatures();
}

async function replicateStudyNow() {
  const uid = $("rep-study").value.trim();
  if (!uid) { toast("Study UID required", "err"); return; }
  const res = await api(`/api/v1/replication/studies/${encodeURIComponent(uid)}`, { method: "POST" });
  const d = await res.json().catch(() => ({}));
  if (res.ok) {
    toast(`Replicated: ${d.objects || 0} objects (${fmtBytes(d.bytes || 0)})`, "ok");
    $("rep-study").value = "";
  } else {
    toast(d.detail || "replication failed", "err");
  }
  loadReplication(); loadFeatures();
}

function openSchedule() {
  if (!state.token) { toast("Sign in to view the worklist", "err"); openLogin(); return; }
  switchView("workflow");
  loadWorklist(); loadPerformed(); loadCommitments();
}

function openNodes() {
  if (!state.token) { toast("Sign in to manage nodes", "err"); openLogin(); return; }
  switchView("nodes");
  loadNodes();
}

/* ---------------- find (C-FIND SCU) ---------------- */
function findFilters() {
  const f = {};
  const pid = $("find-pid").value.trim();
  const name = $("find-name").value.trim();
  const date = $("find-date").value.trim();
  const acc = $("find-acc").value.trim();
  const mod = $("find-mod").value.trim();
  const study = $("find-study").value.trim();
  if (pid) f.PatientID = pid;
  if (name) f.PatientName = name;
  if (date) f.StudyDate = date;
  if (acc) f.AccessionNumber = acc;
  if (mod) f.Modality = mod;
  if (study) f.StudyInstanceUID = study;
  return f;
}

function openFind(aeTitle, transport) {
  if (!state.token) { toast("Sign in to query nodes", "err"); openLogin(); return; }
  state.findNode = aeTitle;
  state.findTransport = transport === "dicomweb" ? "dicomweb" : "dicom";
  const tag = state.findTransport === "dicomweb"
    ? '<span class="mwl-tag" style="background:rgba(52,211,153,.14);color:#34d399;border-color:rgba(52,211,153,.35)">QIDO-RS</span>'
    : '<span class="mwl-tag">C-FIND</span>';
  $("find-meta").innerHTML = `Querying ${esc(aeTitle)} — ${state.findTransport === "dicomweb" ? "DICOMweb QIDO-RS" : "C-FIND"} ${$("find-level").value} level. ${tag}`;
  $("find-results").innerHTML = `<p class="sub">Set match keys, then run the query.</p>`;
  $("find-modal").hidden = false;
}

async function runFind() {
  const btn = $("find-run");
  btn.disabled = true; btn.textContent = "…";
  $("find-results").innerHTML = `<p class="sub">Querying ${esc(state.findNode)}…</p>`;
  const path = state.findTransport === "dicomweb" ? "web/query" : "find";
  const res = await api(`/api/v1/nodes/${encodeURIComponent(state.findNode)}/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ level: $("find-level").value, filters: findFilters() }),
  });
  btn.disabled = false; btn.textContent = "Run query ▶";
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { $("find-results").innerHTML = `<p class="sub">Query failed (${res.status}): ${esc(data.detail || "")}</p>`; return; }
  const matches = data.matches || [];
  if (!matches.length) { $("find-results").innerHTML = `<p class="sub">No matches on ${esc(state.findNode)}.</p>`; return; }
  const level = $("find-level").value;
  $("find-results").innerHTML = matches.map((m) => {
    const id = m.PatientID ? `<span class="mono">${esc(m.PatientID)}</span>` : "";
    const when = m.StudyDate ? `<span class="mono">${esc(m.StudyDate)}</span>` : "";
    const desc = m.StudyDescription ? ` · ${esc(m.StudyDescription)}` : "";
    const mods = m.ModalitiesInStudy ? ` · ${esc(m.ModalitiesInStudy)}` : (m.Modality ? ` · ${esc(m.Modality)}` : "");
    const view = (level === "STUDY" && m.StudyInstanceUID)
      ? ` · <a class="wl-link" href="${viewerUrl(m.StudyInstanceUID)}">open →</a>`
      : (m.StudyInstanceUID ? ` · <span class="mono faint">${esc(m.StudyInstanceUID)}</span>` : "");
    const pullBtn = m.StudyInstanceUID
      ? `<button class="btn sm" data-pull-study="${esc(m.StudyInstanceUID)}" data-pull-node="${esc(state.findNode)}" style="margin-left:auto;white-space:nowrap" title="Download & copy this study into PAXS Archive">Pull to PAXS ▼</button>`
      : (m.PatientID ? `<button class="btn sm" data-pull-pid="${esc(m.PatientID)}" data-pull-node="${esc(state.findNode)}" style="margin-left:auto;white-space:nowrap" title="Download & copy all patient studies into PAXS Archive">Pull to PAXS ▼</button>` : "");
    return `<div class="node-item" style="display:flex;align-items:center;justify-content:space-between">
      <div class="cell-main">
        <div class="name">${esc(m.PatientName || "—")} ${id}</div>
        <div class="sub mono">${when}${mods}${desc}${view}</div>
      </div>
      ${pullBtn}
    </div>`;
  }).join("");

  // Wire 1-click pull buttons from find results
  document.querySelectorAll("[data-pull-study]").forEach((b) => {
    b.addEventListener("click", () => {
      openPull(b.dataset.pullNode, state.findTransport);
      $("pull-study").value = b.dataset.pullStudy;
      if ($("pull-pid")) $("pull-pid").value = "";
      runPull();
    });
  });
  document.querySelectorAll("[data-pull-pid]").forEach((b) => {
    b.addEventListener("click", () => {
      openPull(b.dataset.pullNode, state.findTransport);
      if ($("pull-pid")) $("pull-pid").value = b.dataset.pullPid;
      $("pull-study").value = "";
      runPull();
    });
  });

  toast(`${matches.length} match(es) from ${state.findNode}`, "ok");
}

/* ---------------- pull (C-GET SCU) ---------------- */
function pullFilters() {
  const f = {};
  const pid = $("pull-pid") ? $("pull-pid").value.trim() : "";
  const study = $("pull-study").value.trim();
  const series = $("pull-series").value.trim();
  const sop = $("pull-sop").value.trim();
  if (pid) f.PatientID = pid;
  if (study) f.StudyInstanceUID = study;
  if (series) f.SeriesInstanceUID = series;
  if (sop) f.SOPInstanceUID = sop;
  return f;
}

function openPull(aeTitle, transport) {
  if (!state.token) { toast("Sign in to pull studies", "err"); openLogin(); return; }
  state.pullNode = aeTitle;
  state.pullTransport = transport === "dicomweb" ? "dicomweb" : "dicom";
  const tag = state.pullTransport === "dicomweb"
    ? '<span class="mwl-tag" style="background:rgba(52,211,153,.14);color:#34d399;border-color:rgba(52,211,153,.35)">WADO-RS</span>'
    : '<span class="mwl-tag" style="background:rgba(129,140,248,.14);color:#818cf8;border-color:rgba(129,140,248,.35)">C-GET</span>';
  $("pull-meta").innerHTML = `${esc(aeTitle)} — ${state.pullTransport === "dicomweb" ? "DICOMweb WADO-RS fetch" : "C-GET fetch"}; results are stored and indexed locally. ${tag}`;
  $("pull-results").innerHTML = `<p class="sub">Set a Study UID (or Series/SOP), then pull — instances are stored and indexed like a C-STORE.</p>`;
  /* auto-archive (lifecycle tiering) is a DICOMweb WADO-RS option */
  const wrap = $("pull-archive-wrap");
  wrap.hidden = state.pullTransport !== "dicomweb";
  $("pull-archive").checked = false;
  $("pull-modal").hidden = false;
}

async function runPull() {
  const btn = $("pull-run");
  const filters = pullFilters();
  if (!Object.keys(filters).length) { toast("Enter a Patient ID or Study UID first", "err"); return; }
  btn.disabled = true; btn.textContent = "…";
  $("pull-results").innerHTML = `<p class="sub">Pulling from ${esc(state.pullNode)}…</p>`;
  const path = state.pullTransport === "dicomweb" ? "web/retrieve" : "pull";
  const body = { level: $("pull-level").value, filters };
  if (state.pullTransport === "dicomweb") body.auto_archive = $("pull-archive").checked;
  const res = await api(`/api/v1/nodes/${encodeURIComponent(state.pullNode)}/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  btn.disabled = false; btn.textContent = "Pull down ▼";
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { $("pull-results").innerHTML = `<p class="sub">Pull failed (${res.status}): ${esc(data.detail || "")}</p>`; return; }
  const rows = data.results || [];
  if (!rows.length) { $("pull-results").innerHTML = `<p class="sub">No instances received from ${esc(state.pullNode)}.</p>`; return; }
  const archiveNote = data.auto_archived
    ? `<div class="sub" style="color:#34d399;margin-top:6px">❄️ Study tiered to cold storage (${esc(data.archive_state || "ARCHIVED")})</div>`
    : (data.archive_error
      ? `<div class="sub" style="color:var(--danger);margin-top:6px">Auto-archive requested but failed: ${esc(data.archive_error)}</div>`
      : (data.archive_state ? `<div class="sub" style="color:var(--danger);margin-top:6px">Auto-archive requested but failed (${esc(data.archive_state || "error")})</div>` : ""));
  $("pull-results").innerHTML = rows.map((r) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${r.ok ? "✅" : "❌"} <span class="mono">${esc(r.sop_uid || "?")}</span></div>
        <div class="sub mono">${esc(r.study_uid || "")}${r.reason ? ` · ${esc(r.reason)}` : ""}</div>
      </div>
    </div>`).join("") + archiveNote;
  toast(`${data.pulled} instance(s) pulled from ${state.pullNode}`, data.failed ? "err" : "ok");
  loadStudies();
}

/* ---------------- web upload (STOW-RS SCU) ---------------- */
function openWebUpload(aeTitle, fast) {
  if (!state.token) { toast("Sign in to upload studies", "err"); openLogin(); return; }
  state.uploadNode = aeTitle;
  $("up-fast").checked = !!fast;
  $("upload-meta").innerHTML = `${esc(aeTitle)} — DICOMweb STOW-RS upload; matching local instances are sent as multipart/related (C-STORE's REST twin).${fast ? ' <span class="mwl-tag" style="background:rgba(52,211,153,.14);color:#34d399;border-color:rgba(52,211,153,.35)">⚡ accelerated — parallel chunked HTTP</span>' : ""}`;
  $("up-results").innerHTML = `<p class="sub">Set a Study UID (or Series/SOP), then upload — instances are sent to the node's DICOMweb service over STOW-RS.</p>`;
  $("upload-modal").hidden = false;
}

async function runWebUpload() {
  const btn = $("up-run");
  const study = $("up-study").value.trim();
  const series = $("up-series").value.trim();
  const sop = $("up-sop").value.trim();
  if (!study && !series && !sop) { toast("Enter a Study, Series or SOP UID first", "err"); return; }
  const fast = $("up-fast").checked;
  const node = state.uploadNode;
  btn.disabled = true; btn.textContent = "…";
  $("up-results").innerHTML = `<p class="sub">Uploading to ${esc(node)}${fast ? " ⚡ (accelerated)" : ""}…</p>`;
  // Drive the shared upload panel. The Accelerator path runs parallel
  // batches server-side, so ?progress=1 streams per-batch NDJSON events back
  // and the bar is determinate (real confirmed-sent counts); the plain path
  // is a single opaque request and stays sweeping.
  upOpen(`Uploading to ${node}${fast ? " ⚡" : ""}`);
  upPhase("scan");
  upProgress(0, "Preparing local instances…");
  upPhase("upload");
  if (fast) upProgress(0, "Starting transfer…");
  else upIndeterminate(`Transferring to ${node} (STOW-RS)…`);
  const ac = new AbortController();
  upBegin(ac);
  const qs = new URLSearchParams();
  if (fast) { qs.set("accelerated", "1"); qs.set("progress", "1"); }
  const q = qs.toString();
  let data = null;
  try {
    const res = await api(`/api/v1/nodes/${encodeURIComponent(node)}/web/upload${q ? `?${q}` : ""}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        study_uids: study ? [study] : [],
        series_uids: series ? [series] : [],
        instance_uids: sop ? [sop] : [],
      }),
      signal: ac.signal,
    });
    if (!res.ok) {
      upEnd(ac);
      btn.disabled = false; btn.textContent = "Upload ↑";
      const errData = await res.json().catch(() => ({}));
      upFail(`Upload failed (${res.status})${errData.detail ? ` · ${errData.detail}` : ""}`);
      $("up-results").innerHTML = `<p class="sub">Upload failed (${res.status}): ${esc(errData.detail || "")}</p>`;
      return;
    }
    data = (res.headers.get("content-type") || "").includes("ndjson")
      ? await readNdjsonUpload(res)
      : await res.json().catch(() => ({}));
    if (data === null) {  // terminal error event already surfaced
      upEnd(ac);
      btn.disabled = false; btn.textContent = "Upload ↑";
      return;
    }
  } catch (err) {
    upEnd(ac);
    btn.disabled = false; btn.textContent = "Upload ↑";
    if (err?.name === "AbortError") {
      upCancelled(`Cancelled — transfer to ${node} stopped`);
      toast("Upload cancelled", "err");
    } else {
      upFail(`Transfer failed · ${err?.message || "network error"}`);
      toast(`Upload failed: ${err?.message || "network error"}`, "err");
    }
    return;
  }
  upEnd(ac);
  btn.disabled = false; btn.textContent = "Upload ↑";
  const rows = data.results || [];
  if (!rows.length) {
    upFail("No instances matched the given scope");
    $("up-results").innerHTML = `<p class="sub">No instances matched the given scope.</p>`;
    return;
  }
  const byStudy = new Map();
  for (const r of rows) {
    if (!r.ok) continue;
    const uid = r.study_uid || "unknown";
    const cur = byStudy.get(uid) || { instances: 0 };
    cur.instances += 1;
    byStudy.set(uid, cur);
  }
  const summaryList = [...byStudy.entries()].map(([uid, s]) => ({
    study_instance_uid: uid, instances: s.instances, sent: true,
  }));
  const accel = data.accelerator
    ? ` ⚡ ${data.batches ?? "?"} batch(es) · ${data.workers ?? "?"} connections · ${(data.bytes ?? 0).toLocaleString()} B · ${data.elapsed_seconds ?? 0}s · ${data.mb_per_sec ?? 0} MB/s`
    : "";
  const head = `Sent ${byStudy.size} ${byStudy.size === 1 ? "study" : "studies"} · ${data.sent} instance${data.sent === 1 ? "" : "s"} to ${node}`;
  if (data.sent > 0) {
    upSuccess(`${data.sent} instance(s) sent to ${node}${accel}${data.failed ? ` · ${data.failed} failed` : ""}`, summaryList, head);
  } else {
    upFail(`${data.failed || 0} instance(s) failed — nothing sent`);
  }
  const stats = data.accelerator
    ? `<p class="sub mono" style="margin-bottom:10px">⚡ ${data.batches ?? "?"} batch(es) · ${data.workers ?? "?"} connections · ${(data.bytes ?? 0).toLocaleString()} B · ${data.elapsed_seconds ?? 0}s · ${data.mb_per_sec ?? 0} MB/s</p>`
    : "";
  $("up-results").innerHTML = stats + rows.map((r) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${r.ok ? "✅" : "❌"} <span class="mono">${esc(r.sop_uid || "?")}</span></div>
        <div class="sub mono">${esc(r.study_uid || "")}${r.reason ? ` · ${esc(r.reason)}` : ""}</div>
      </div>
    </div>`).join("");
  toast(`${data.sent} instance(s) uploaded to ${node}${data.accelerator ? " ⚡" : ""}`, data.failed ? "err" : "ok");
}

// Read the NDJSON progress stream from ?progress=1 web uploads. Drives the
// determinate panel bar from per-batch {event:"progress"} lines and returns
// the final {event:"done"} result object, or null after surfacing a terminal
// {event:"error"} line (upFail + toast + up-results). An abort mid-stream
// propagates to the caller's catch (which shows the cancelled state).
async function readNdjsonUpload(res) {
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let result = null;
  let errMsg = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (!line.trim()) continue;
      let ev;
      try { ev = JSON.parse(line); } catch { continue; }
      if (ev.event === "progress" && ev.total > 0) {
        upProgress((ev.done / ev.total) * 100, `Uploading ${ev.done} of ${ev.total} instances…`);
      } else if (ev.event === "error") {
        errMsg = ev.message || "Upload failed";
      } else if (ev.event === "done") {
        result = ev.result;
      }
    }
  }
  if (errMsg) {
    upFail(`Upload failed · ${errMsg}`);
    $("up-results").innerHTML = `<p class="sub">Upload failed: ${esc(errMsg)}</p>`;
    toast(`Upload failed: ${errMsg}`, "err");
    return null;
  }
  return result;
}

/* ---------------- forward (C-STORE SCU) ---------------- */
async function openForward(studyUid, patientName) {
  if (!state.token) { toast("Sign in to forward studies", "err"); openLogin(); return; }
  const study = decodeURIComponent(studyUid);
  state.forwardStudy = study;
  $("forward-meta").textContent = `${patientName || "Patient"} — ${study} — pick a destination node.`;
  $("forward-nodes").innerHTML = `<p class="sub">Loading nodes…</p>`;
  $("forward-modal").hidden = false;
  const res = await api("/api/v1/nodes");
  if (!res.ok) { $("forward-nodes").innerHTML = `<p class="sub">Failed to load nodes (${res.status}) — sign in as admin.</p>`; return; }
  const nodes = await res.json();
  if (!nodes.length) { $("forward-nodes").innerHTML = `<p class="sub">No nodes registered. Add a destination in the Nodes panel first.</p>`; return; }
  $("forward-nodes").innerHTML = nodes.map((n) => `
    <div class="node-item">
      <div class="cell-main">
        <div class="name">${esc(n.ae_title)} ${n.enabled ? "" : '<span class="tag-disabled">disabled</span>'}</div>
        <div class="sub mono">${esc(n.host)}:${n.port}${n.description ? ` · ${esc(n.description)}` : ""}</div>
      </div>
      <div class="actions">
        <button class="btn sm" data-fwd="${esc(n.ae_title)}" ${n.enabled ? "" : "disabled"}>Send →</button>
      </div>
    </div>`).join("");
  [...document.querySelectorAll("[data-fwd]")].forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "…";
      const res = await api(`/api/v1/nodes/${encodeURIComponent(b.dataset.fwd)}/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ study_uids: [state.forwardStudy] }),
      });
      b.disabled = false; b.textContent = "Send →";
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { toast(data.detail || `Forward failed (${res.status})`, "err"); return; }
      const msg = data.ok
        ? `Forwarded ${data.sent} instance(s) to ${data.ae_title}`
        : `Forward to ${data.ae_title} failed: ${data.reason || `${data.failed} instance(s) failed`}`;
      toast(msg, data.ok ? "ok" : "err");
    }));
}

/* ---------------- upload (STOW-RS) ---------------- */
/* Folder upload: the browser's directory picker (webkitdirectory) hands us
   every file in the tree incl. subfolders (via webkitRelativePath). We filter
   real DICOM files — extension match OR the 128-byte preamble + "DICM" magic
   at offset 128 — then stream them to the STOW-RS endpoint in bounded batches
   so a multi-GB study never buffers fully in browser RAM. */
const DICOM_EXT_RE = /\.(dcm|dicom|ima)$/i;
const BATCH_FILES = 40;                  // max parts per STOW request
const MAX_BATCH_BYTES = 48 * 1024 * 1024; // ...and max bytes per request

async function isDicomFile(file) {
  if (DICOM_EXT_RE.test(file.name)) return true;
  try {
    const head = new Uint8Array(await file.slice(0, 132).arrayBuffer());
    if (head.length >= 132) {
      const magic = String.fromCharCode(head[128], head[129], head[130], head[131]);
      if (magic === "DICM") return true;
    }
  } catch (_) { /* unreadable — treat as non-DICOM */ }
  return false;
}

function buildMultipartBody(files, boundary) {
  const enc = new TextEncoder();
  const parts = [];
  for (const f of files) {
    parts.push(enc.encode(`--${boundary}\r\nContent-Type: application/dicom\r\n\r\n`));
    parts.push(f.data);
    parts.push(enc.encode("\r\n"));
  }
  parts.push(enc.encode(`--${boundary}--\r\n`));
  const total = parts.reduce((n, p) => n + p.length, 0);
  const body = new Uint8Array(total);
  let off = 0;
  for (const p of parts) { body.set(p, off); off += p.length; }
  return body;
}

async function* batchDicomFiles(files) {
  let cur = [], curBytes = 0;
  for (const file of files) {
    const data = new Uint8Array(await file.arrayBuffer());
    if (cur.length && (cur.length >= BATCH_FILES || curBytes + data.length > MAX_BATCH_BYTES)) {
      yield cur; cur = []; curBytes = 0;
    }
    cur.push({ file, data });
    curBytes += data.length;
  }
  if (cur.length) yield cur;
}

/* Upload panel — colored phase UI (Scan → Upload → Done) with a green success state */
let _upPanel = null;
let _upAutoHide = null;
let _upAbort = null; // AbortController for the active transfer (cancel button)

function upOpen(title) {
  if (_upAutoHide) { clearTimeout(_upAutoHide); _upAutoHide = null; }
  if (!_upPanel) {
    const el = document.createElement("div");
    el.id = "upload-panel";
    el.innerHTML = `
      <div class="up-head">
        <span class="up-ico">📤</span>
        <span class="up-title"></span>
        <button type="button" class="up-close" title="Dismiss">✕</button>
      </div>
      <div class="up-phases">
        <span class="up-phase" data-phase="scan"><i></i>Scan</span>
        <span class="up-phase" data-phase="upload"><i></i>Upload</span>
        <span class="up-phase" data-phase="done"><i></i>Done</span>
      </div>
      <div class="up-track"><div class="up-fill"></div></div>
      <div class="up-stats">
        <span class="up-label">Preparing…</span>
        <span class="up-pct">0%</span>
      </div>
      <div class="up-actions">
        <button type="button" class="up-cancel" hidden title="Stop the upload — files already stored stay in the archive">⏹ Cancel upload</button>
      </div>
      <div class="up-result hidden">
        <span class="up-result-ico"></span><span class="up-result-text"></span>
      </div>
      <div class="up-summary hidden">
        <div class="up-summary-head"></div>
        <div class="up-summary-body"></div>
      </div>`;
    el.querySelector(".up-close").addEventListener("click", () => upClose(true));
    el.querySelector(".up-cancel").addEventListener("click", () => {
      const ac = _upAbort;
      if (!ac || ac.signal.aborted) return;
      ac.abort();
      const b = el.querySelector(".up-cancel");
      b.disabled = true;
      b.textContent = "Cancelling…";
    });
    document.body.appendChild(el);
    _upPanel = el;
  }
  const panel = _upPanel;
  _upAbort = null;
  panel.classList.remove("ok", "err", "cancel", "closing");
  panel.querySelector(".up-title").textContent = title;
  panel.querySelector(".up-ico").textContent = "📤";
  panel.querySelector(".up-result").classList.add("hidden");
  const summary = panel.querySelector(".up-summary");
  summary.classList.add("hidden");
  summary.querySelector(".up-summary-body").innerHTML = "";
  const cancelBtn = panel.querySelector(".up-cancel");
  cancelBtn.hidden = true;
  cancelBtn.disabled = false;
  cancelBtn.textContent = "⏹ Cancel upload";
  upPhase("scan");
  upProgress(0, "Preparing…");
  return panel;
}

// Show the cancel button and record the controller for the active transfer.
// The loop keeps its own `ac` reference, so a second upload started after a
// dismiss can't accidentally abort the first one's in-flight requests.
function upBegin(ac) {
  _upAbort = ac;
  if (!_upPanel) return;
  const b = _upPanel.querySelector(".up-cancel");
  b.hidden = false;
  b.disabled = false;
  b.textContent = "⏹ Cancel upload";
}

function upEnd(ac) {
  if (_upAbort === ac) _upAbort = null;
  if (!_upPanel) return;
  _upPanel.querySelector(".up-cancel").hidden = true;
}

function upCancelled(detail, summaryList, summaryHead) {
  if (!_upPanel) return;
  const panel = _upPanel;
  panel.classList.add("cancel");
  panel.classList.remove("ok", "err");
  upPhase("done");
  const fill = panel.querySelector(".up-fill");
  fill.classList.remove("indet");
  panel.querySelector(".up-label").textContent = "Cancelled";
  panel.querySelector(".up-pct").textContent = "–";
  const res = panel.querySelector(".up-result");
  res.classList.remove("hidden");
  res.querySelector(".up-result-ico").textContent = "⏹";
  res.querySelector(".up-result-text").textContent = detail;
  // Partial per-study breakdown: which studies and how many instances
  // actually landed in the completed batches before the abort.
  renderUpSummary(summaryList || [], summaryHead);
}

function upPhase(name) {
  if (!_upPanel) return;
  const order = ["scan", "upload", "done"];
  const cur = order.indexOf(name);
  [..._upPanel.querySelectorAll(".up-phase")].forEach((p) => {
    const i = order.indexOf(p.dataset.phase);
    p.classList.toggle("active", p.dataset.phase === name);
    p.classList.toggle("done", i < cur);
  });
}

function upProgress(pct, label) {
  if (!_upPanel) return;
  const fill = _upPanel.querySelector(".up-fill");
  fill.classList.remove("indet");
  const p = Math.max(0, Math.min(100, Math.round(pct)));
  fill.style.width = `${p}%`;
  _upPanel.querySelector(".up-label").textContent = label;
  _upPanel.querySelector(".up-pct").textContent = `${p}%`;
}

// Indeterminate mode for single-request transfers (node web upload): a
// sweeping bar with no percentage — the server reports progress only at the end.
function upIndeterminate(label) {
  if (!_upPanel) return;
  _upPanel.querySelector(".up-fill").classList.add("indet");
  _upPanel.querySelector(".up-label").textContent = label;
  _upPanel.querySelector(".up-pct").textContent = "…";
}

function upSuccess(detail, summaryList, summaryHead) {
  if (!_upPanel) return;
  const panel = _upPanel;
  panel.classList.add("ok");
  panel.classList.remove("err", "cancel");
  upPhase("done");
  upProgress(100, "Complete");
  const res = panel.querySelector(".up-result");
  res.classList.remove("hidden");
  res.querySelector(".up-result-ico").textContent = "✓";
  res.querySelector(".up-result-text").textContent = detail;
  renderUpSummary(summaryList || [], summaryHead);
  // A per-study breakdown needs a moment to read — keep it up a bit longer.
  _upAutoHide = setTimeout(() => upClose(true), 8000);
}

function renderUpSummary(list, head) {
  if (!_upPanel) return;
  const wrap = _upPanel.querySelector(".up-summary");
  const body = wrap.querySelector(".up-summary-body");
  if (!list.length) { wrap.classList.add("hidden"); return; }
  const imported = list.filter((s) => !s.existed);
  const existed = list.filter((s) => s.existed);
  const total = list.reduce((n, s) => n + s.instances, 0);
  wrap.querySelector(".up-summary-head").textContent = head ||
    `Imported ${imported.length} ${imported.length === 1 ? "study" : "studies"} · ${total} instance${total === 1 ? "" : "s"} · ${existed.length} already existed`;
  body.innerHTML = list
    .map((s) => {
      const uid = s.study_instance_uid || "unknown";
      const short = uid.length > 16 ? `…${uid.slice(-16)}` : uid;
      const tag = s.sent
        ? { cls: "sent", txt: "SENT" }
        : s.existed
          ? { cls: "dup", txt: "EXISTED" }
          : { cls: "new", txt: "NEW" };
      return `<div class="up-srow">
        <span class="up-suid" title="${esc(uid)}">${esc(short)}</span>
        <span class="up-sinst">${s.instances} inst.</span>
        <span class="up-stag ${tag.cls}">${tag.txt}</span>
      </div>`;
    })
    .join("");
  wrap.classList.remove("hidden");
}

function upFail(detail) {
  if (!_upPanel) return;
  const panel = _upPanel;
  panel.classList.add("err");
  panel.classList.remove("ok", "cancel");
  upPhase("done");
  const res = panel.querySelector(".up-result");
  res.classList.remove("hidden");
  res.querySelector(".up-result-ico").textContent = "⚠";
  res.querySelector(".up-result-text").textContent = detail;
}

function upClose() {
  if (_upAutoHide) { clearTimeout(_upAutoHide); _upAutoHide = null; }
  if (!_upPanel) return;
  const el = _upPanel;
  el.classList.add("closing");
  setTimeout(() => { el.remove(); if (_upPanel === el) _upPanel = null; }, 250);
}

async function uploadFiles(files, opts = {}) {
  if (!files.length) return;
  const { prefiltered = false, source = "files" } = opts;
  upOpen(source === "folder" ? "Uploading folder" : "Uploading files");
  const ac = new AbortController();
  upBegin(ac);

  let dicom = files;
  let cancelled = false;
  if (!prefiltered) {
    upPhase("scan");
    upProgress(0, `Scanning ${files.length} files for DICOM…`);
    dicom = [];
    for (const f of files) {
      if (ac.signal.aborted) { cancelled = true; break; }
      if (await isDicomFile(f)) dicom.push(f);
    }
  }
  if (!cancelled && !dicom.length) {
    upEnd(ac);
    upFail("No DICOM files found");
    toast("No DICOM files found", "err");
    return;
  }
  const skipped = files.length - dicom.length;
  const folders = new Set(
    dicom.map((f) => (f.webkitRelativePath || f.name).split("/").slice(0, -1).join("/")).filter(Boolean)
  );

  upPhase("upload");
  let done = 0, failed = 0, batches = 0;
  // Per-study summary aggregated across batches (STOW response carries a
  // "summary.studies" block: study uid, instance count, existed flag).
  const aggStudies = new Map(); // study_instance_uid -> { instances, existed }
  for await (const batch of batchDicomFiles(dicom)) {
    if (ac.signal.aborted) { cancelled = true; break; }
    batches++;
    const boundary = `paxs-ui-${Math.random().toString(36).slice(2)}`;
    const label = batches === 1 && dicom.length <= BATCH_FILES
      ? `Uploading ${dicom.length} file(s)…`
      : `Uploading ${Math.min(done + batch.length, dicom.length)}/${dicom.length} files (batch ${batches})`;
    upProgress((done / dicom.length) * 100, label);
    try {
      const res = await fetch("/dicom-web/studies", {
        method: "POST",
        headers: { "Content-Type": `multipart/related; type="application/dicom"; boundary=${boundary}` },
        body: buildMultipartBody(batch, boundary),
        signal: ac.signal,
      });
      let storedThis = 0;
      try {
        const data = await res.json();
        const list = data?.summary?.studies;
        if (Array.isArray(list)) {
          for (const s of list) {
            const cur = aggStudies.get(s.study_instance_uid) || { instances: 0, existed: !!s.existed };
            cur.instances += s.instances;
            aggStudies.set(s.study_instance_uid, cur);
          }
        }
        storedThis = Array.isArray(data?.["00081199"]?.Value) ? data["00081199"].Value.length : 0;
      } catch (_) { /* non-JSON error body (400/413) */ }
      failed += Math.max(0, batch.length - storedThis);
    } catch (err) {
      if (err?.name === "AbortError") { cancelled = true; break; }
      failed += batch.length;
    }
    done += batch.length;
  }
  upEnd(ac);

  const extra = source === "folder" ? ` from ${folders.size} folder(s)` : "";
  const summaryList = [...aggStudies.entries()].map(([uid, s]) => ({
    study_instance_uid: uid, instances: s.instances, existed: s.existed,
  }));
  if (cancelled) {
    // `done` counts whole completed batches — files in the in-flight batch
    // may have landed server-side too, so we report the confirmed count.
    // If the scan was interrupted, the full picked count is the denominator.
    const denom = dicom.length < files.length ? files.length : dicom.length;
    const landed = summaryList.reduce((n, s) => n + s.instances, 0);
    upCancelled(
      `Cancelled — ${done} of ${denom} file(s) confirmed stored${extra}`,
      summaryList,
      `Landed in ${summaryList.length} ${summaryList.length === 1 ? "study" : "studies"} · ${landed} instance${landed === 1 ? "" : "s"}`
    );
    toast(`Upload cancelled — ${done} file(s) already stored`, "err");
  } else if (!failed) {
    const detail = `Upload complete — ${dicom.length} file(s) uploaded${extra}${skipped ? ` · ${skipped} non-DICOM skipped` : ""}`;
    upSuccess(detail, summaryList);
    toast(`Upload complete — ${dicom.length} file(s)${extra}${skipped ? `, ${skipped} non-DICOM skipped` : ""}`, "ok");
  } else {
    upFail(`${dicom.length - failed}/${dicom.length} uploaded · ${failed} failed`);
    toast(`Upload finished — ${dicom.length - failed}/${dicom.length} uploaded, ${failed} failed`, "err");
  }
  loadStudies(); loadStats();
}

/* ---------------- wire up ---------------- */
function bind() {
  /* ---- browser back/forward button ---- */
  window.addEventListener("popstate", (e) => {
    const view = e.state?.view || location.hash.replace("#", "") || "dashboard";
    if (VIEWS.includes(view)) switchView(view);
  });

  let searchTimer = null;
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.page = 1; loadStudies(); }, 300);
  });
  $("f-modality").addEventListener("change", () => { state.page = 1; loadStudies(); });
  const tenantEl = $("f-tenant");
  if (tenantEl) tenantEl.addEventListener("change", () => { state.page = 1; loadStudies(); });
  $("f-from").addEventListener("change", () => { state.page = 1; loadStudies(); });
  $("f-to").addEventListener("change", () => { state.page = 1; loadStudies(); });
  [...document.querySelectorAll(".sort-btn")].forEach((b) => {
    b.addEventListener("click", () => {
      const key = b.dataset.sort;
      if (state.sort === key) state.order = state.order === "asc" ? "desc" : "asc";
      else { state.sort = key; state.order = "desc"; }
      state.page = 1;
      persistStudiesPrefs();
      loadStudies();
    });
  });
  const densityBtn = $("density-toggle");
  if (densityBtn) {
    densityBtn.addEventListener("click", () => {
      state.density = state.density === "comfortable" ? "compact" : "comfortable";
      applyDensity();
      persistStudiesPrefs();
    });
  }
  // Column visibility dropdown (Studies list).
  const colsToggle = $("cols-toggle");
  const colsMenu = $("cols-menu");
  if (colsToggle && colsMenu) {
    colsToggle.addEventListener("click", (e) => {
      e.stopPropagation();
      colsMenu.hidden = !colsMenu.hidden;
    });
    [...colsMenu.querySelectorAll("input[data-colkey]")].forEach((cb) => {
      cb.addEventListener("change", () => {
        state.cols[cb.dataset.colkey] = cb.checked;
        persistStudiesPrefs();
        applyColumns();
      });
    });
    const reset = $("cols-reset");
    if (reset) reset.addEventListener("click", () => {
      state.cols = defaultCols();
      persistStudiesPrefs();
      applyColumns();
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest("#col-picker")) colsMenu.hidden = true;
    });
  }
  [...document.querySelectorAll("#status-chips .chip-btn")].forEach((c) => {
    c.addEventListener("click", () => {
      const st = c.dataset.state || "";
      const cr = c.dataset.critical ? c.dataset.critical === "1" : false;
      const rep = c.dataset.reported ? c.dataset.reported === "1" : false;
      const oru = c.dataset.oru ? c.dataset.oru === "1" : false;
      const his = c.dataset.his ? c.dataset.his === "1" : false;
      state.state = st;
      state.critical = cr;
      state.reported = rep;
      state.oru = oru;
      state.his = his;
      state.page = 1;
      [...document.querySelectorAll("#status-chips .chip-btn")].forEach((x) =>
        x.classList.toggle("active", x === c));
      loadStudies();
    });
  });
  $("btn-refresh").addEventListener("click", () => { loadStats(); loadStudies(); });
  const termsBtn = $("btn-terms");
  if (termsBtn) termsBtn.addEventListener("click", () => openTerms("terms"));
  $("terms-close")?.addEventListener("click", () => { $("terms-modal").hidden = true; });
  $("terms-close-x")?.addEventListener("click", () => { $("terms-modal").hidden = true; });
  [...document.querySelectorAll(".terms-tab")].forEach((b) =>
    b.addEventListener("click", () => setTermsTab(b.dataset.termsTab)));
  const themeBtn = $("btn-theme");
  if (themeBtn) themeBtn.addEventListener("click", () => {
    const next = (document.documentElement.dataset.theme || "dark") === "dark" ? "light" : "dark";
    applyTheme(next, { persist: true });
    toast(next === "light" ? "Light clinical mode" : "Dark reading-room mode");
  });
  window.addEventListener("storage", (e) => { if (e.key === THEME_KEY) applyTheme(e.newValue || "dark"); });
  $("pg-prev").addEventListener("click", () => { if (state.page > 1) { state.page--; loadStudies(); } });
  $("pg-next").addEventListener("click", () => { state.page++; loadStudies(); });

  $("btn-worklist").addEventListener("click", openSchedule);
  $("wl-add").addEventListener("click", addStep);
  $("cp-add")?.addEventListener("click", addProtocol);
  $("cp-test")?.addEventListener("click", testProtocolMatch);
  $("wl-filter").addEventListener("change", loadWorklist);
  $("commit-filter").addEventListener("change", loadCommitments);

  $("btn-nodes").addEventListener("click", openNodes);
  $("node-add").addEventListener("click", addNode);
  $("forward-close").addEventListener("click", () => { $("forward-modal").hidden = true; state.forwardStudy = null; });
  $("find-close").addEventListener("click", () => { $("find-modal").hidden = true; state.findNode = null; });
  $("find-run").addEventListener("click", runFind);
  $("pull-close").addEventListener("click", () => { $("pull-modal").hidden = true; state.pullNode = null; });
  $("pull-run").addEventListener("click", runPull);
  $("up-close").addEventListener("click", () => { $("upload-modal").hidden = true; state.uploadNode = null; });
  $("up-run").addEventListener("click", runWebUpload);

  $("btn-upload").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
  const btnUploadFolder = $("btn-upload-folder");
  if (btnUploadFolder) {
    btnUploadFolder.addEventListener("click", () => {
      const folderInput = $("folder-input");
      if (!folderInput) return;
      if (!("webkitdirectory" in folderInput)) {
        toast("Folder upload isn't supported in this browser — use Upload or drag files", "err");
        return;
      }
      folderInput.click();
    });
    $("folder-input").addEventListener("change", (e) => {
      const files = [...e.target.files];
      e.target.value = "";
      if (files.length) uploadFiles(files, { source: "folder" });
    });
  }

  // primary navigation
  [...document.querySelectorAll(".nav-tab")].forEach((b) =>
    b.addEventListener("click", () => {
      const view = b.dataset.view;
      if (view === "admin") openAdminTab("sys");
      else if (view === "settings") { switchView("settings"); loadSettings(); }
      else switchView(view);
    }));

  // feature cards → views
  [...document.querySelectorAll(".feat")].forEach((c) =>
    c.addEventListener("click", () => {
      const open = c.dataset.open;
      if (open === "workflow") openSchedule();
      else if (open === "nodes") openNodes();
      else if (open === "reading") { if (!state.token) { toast("Sign in to see reading tasks", "err"); openLogin(); return; } switchView("reading"); }
      else openAdminTab(c.dataset.tab || "sys");
    }));

  // reading worklist
  [...document.querySelectorAll("#rwl-chips .chip-btn")].forEach((c) =>
    c.addEventListener("click", () => {
      RWL.state = c.dataset.rstate;
      RWL.page = 1;
      [...document.querySelectorAll("#rwl-chips .chip-btn")].forEach((x) =>
        x.classList.toggle("active", x === c));
      loadReadingTasks();
    }));
  const rwlMine = $("btn-rwl-assign");
  if (rwlMine) rwlMine.addEventListener("click", () => {
    RWL.mine = !RWL.mine;
    rwlMine.classList.toggle("active", RWL.mine);
    RWL.page = 1;
    loadReadingTasks();
  });
  const rwlRefresh = $("btn-rwl-refresh");
  if (rwlRefresh) rwlRefresh.addEventListener("click", () => { loadReadingCounts(); loadReadingTasks(); });

  // admin console
  [...document.querySelectorAll("#admin-tabs button")].forEach((b) =>
    b.addEventListener("click", () => openAdminTab(b.dataset.tab)));

  // settings sub-tabs
  [...document.querySelectorAll("#settings-tabs button")].forEach((b) =>
    b.addEventListener("click", () => openSettingsTab(b.dataset.stab)));

  // settings save bar (static elements — bind once, not per render)
  $("cfg-save")?.addEventListener("click", () => saveSettings());
  $("cfg-discard")?.addEventListener("click", () => { loadSettings(true); });
  document.querySelectorAll("[data-open-compression]").forEach((button) =>
    button.addEventListener("click", () => switchView("settings", "compression")));
  document.querySelectorAll("[data-compression-profile]").forEach((button) =>
    button.addEventListener("click", () => selectCompressionProfile(button.dataset.compressionProfile)));
  $("compression-dicom-settings")?.addEventListener("click", () => openSettingsTab("dicom"));
  $("disc-scan")?.addEventListener("click", scanDiscRecorders);
  $("disc-check")?.addEventListener("click", checkDiscMedia);
  $("disc-refresh-jobs")?.addEventListener("click", refreshDiscActivity);
  $("disc-recorders")?.addEventListener("change", (event) => {
    const drive = document.querySelector('[data-cfg="DISC_BURN_DRIVE"]');
    if (!drive || drive.disabled || !event.target.value) return;
    drive.value = event.target.value;
    drive.dispatchEvent(new Event("input"));
  });



  // database tab (static elements — bind once)
  $("db-backend")?.addEventListener("change", syncDbForm);
  $("db-test")?.addEventListener("click", dbTest);
  $("db-save")?.addEventListener("click", dbSave);
  $("db-init")?.addEventListener("click", dbInit);
  $("db-backup")?.addEventListener("click", dbBackup);
  bindDbPerf();

  // redis tab (static elements — bind once)
  $("redis-srv-start")?.addEventListener("click", () => redisServerAction("start"));
  $("redis-srv-stop")?.addEventListener("click", () => redisServerAction("stop"));
  $("redis-srv-restart")?.addEventListener("click", () => redisServerAction("restart"));
  $("redis-srv-install")?.addEventListener("click", () => redisServerAction("install"));
  $("redis-test")?.addEventListener("click", redisTest);
  $("redis-save")?.addEventListener("click", redisSave);
  $("redis-scan-btn")?.addEventListener("click", () => redisScanKeys());
  $("rk-detail-close")?.addEventListener("click", () => { $("redis-key-detail").hidden = true; });
  $("redis-flush-paxs")?.addEventListener("click", () => redisFlush("paxs"));
  $("redis-flush-l1")?.addEventListener("click", () => redisFlush("l1"));
  $("redis-flush-custom")?.addEventListener("click", () => {
    const pat = prompt("Enter wildcard pattern to delete (e.g. paxs:frame:* or paxs:vol:*):", "paxs:*");
    if (pat && pat.trim()) redisFlush("pattern", pat.trim());
  });
  $("redis-flush-all")?.addEventListener("click", () => redisFlush("all"));
  $("redis-ping-btn")?.addEventListener("click", redisPing);
  $("redis-raw-info-btn")?.addEventListener("click", redisRawInfo);
  $("redis-filter-chips")?.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-pattern]");
    if (chip) redisScanKeys(chip.dataset.pattern);
  });
  $("rt-add").addEventListener("click", addRoutingRule);
  $("rq-filter").addEventListener("change", loadRoutingQueue);
  $("aa-action")?.addEventListener("change", loadAuthAudit);
  $("aa-user")?.addEventListener("change", loadAuthAudit);
  $("aa-limit")?.addEventListener("change", loadAuthAudit);
  $("aa-refresh")?.addEventListener("click", () => { loadAuthAudit(); loadLocked(); });
  $("rq-run").addEventListener("click", async () => {
    const res = await api("/api/v1/routing/queue/run", { method: "POST" });
    toast(res.ok ? "Queue sweep started" : `Sweep failed (${res.status})`, res.ok ? "ok" : "err");
    if (res.ok) setTimeout(loadRoutingQueue, 900);
  });
  $("cr-add").addEventListener("click", addCriticalResult);
  $("lc-add").addEventListener("click", addLossyRule);
  $("lc-run").addEventListener("click", runLossy);
  $("lc2-add").addEventListener("click", addLifecycleRule);
  $("bk-add").addEventListener("click", addBackend);
  $("bk-move").addEventListener("click", moveStudy);
  $("backend-list").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (btn) backendAction(btn.dataset.bk, btn.dataset.act);
  });
  $("sc-reload").addEventListener("click", reloadScripts);
  $("sc-add").addEventListener("click", newScript);
  $("script-list").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-sc]");
    if (btn) scriptAction(btn.dataset.sc, btn.dataset.act);
  });
  $("rep-sweep").addEventListener("click", sweepReplication);
  $("rep-replicate").addEventListener("click", replicateStudyNow);
  $("lc2-run").addEventListener("click", runLifecycle);
  $("mg-start").addEventListener("click", startFolderMigration);
  $("tn-add").addEventListener("click", addTenant);
  $("us-add").addEventListener("click", addUser);
  $("user-modal-cancel").addEventListener("click", () => { $("user-modal").hidden = true; _userModalAction = null; });
  $("user-modal-ok").addEventListener("click", async () => { if (_userModalAction) await _userModalAction(); });

  if ($("lic-btn-copy-fp")) {
    $("lic-btn-copy-fp").addEventListener("click", () => {
      if (_adminServerFp) {
        navigator.clipboard?.writeText(_adminServerFp).then(() => toast("Fingerprint copied", "ok"));
      }
    });
  }
  if ($("lic-btn-reload")) {
    $("lic-btn-reload").addEventListener("click", async () => {
      const res = await api("/api/v1/license/reload", { method: "POST" });
      if (res.ok) { toast("License reloaded from disk", "ok"); loadLicensePanel(); }
      else { const err = await res.json().catch(() => ({})); toast(err.detail || "Reload failed", "err"); }
    });
  }
  if ($("lic-btn-upload-file")) {
    $("lic-btn-upload-file").addEventListener("click", async () => {
      const inp = $("lic-file-upload");
      if (!inp?.files || !inp.files[0]) { toast("Select a .lic file first", "err"); return; }
      const fd = new FormData();
      fd.append("file", inp.files[0]);
      const res = await fetch("/api/v1/license/upload", {
        method: "POST",
        headers: state.token ? { "Authorization": `Bearer ${state.token}` } : {},
        body: fd
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        toast(data.message || "License activated!", "ok");
        loadLicensePanel();
      } else {
        toast(data.detail || data.message || "Upload failed", "err");
      }
    });
  }

  $("btn-login").addEventListener("click", () => {
    if (state.token) {
      state.token = null; localStorage.removeItem("paxs_token");
      // Clear the HttpOnly session cookie too — otherwise the server-side
      // auth gate still considers this browser signed in and reloads the
      // dashboard instead of redirecting to /login.
      fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" })
        .catch(() => {});
      toast("Signed out");
      enterGate();
    }
    else openLogin();
  });
  $("auth-cancel").addEventListener("click", () => {
    if (document.body.classList.contains("auth-gate")) return; // the gate cannot be dismissed
    $("login-modal").hidden = true;
  });
  $("tab-login").addEventListener("click", () => setAuthMode("login"));
  $("tab-register").addEventListener("click", () => setAuthMode("register"));
  $("auth-submit").addEventListener("click", doAuth);
  $("auth-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") doAuth(); });
  $("auth-pass").addEventListener("keyup", (e) => {
    const on = typeof e.getModifierState === "function" && e.getModifierState("CapsLock");
    $("auth-caps").hidden = !on;
  });
  $("auth-eye").addEventListener("click", () => {
    const input = $("auth-pass");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    const btn = $("auth-eye");
    btn.classList.toggle("reveal", show);
    btn.title = show ? "Hide password" : "Show password";
    btn.setAttribute("aria-label", btn.title);
    input.focus();
  });

  $("share-close").addEventListener("click", () => { $("share-modal").hidden = true; });
  $("report-close").addEventListener("click", () => { $("report-modal").hidden = true; });
  $("share-copy").addEventListener("click", () => {
    const url = $("share-url").dataset.url;
    if (url) { navigator.clipboard?.writeText(url).then(() => toast("URL copied", "ok")); }
  });
  $("confirm-cancel").addEventListener("click", () => { $("confirm-modal").hidden = true; state.deleting = null; });
  $("confirm-ok").addEventListener("click", async () => {
    if (!state.deleting) return;
    const res = await api(`/api/v1/studies/${encodeURIComponent(state.deleting.uid)}`, { method: "DELETE" });
    $("confirm-modal").hidden = true;
    if (res.ok) { toast("Study deleted", "ok"); loadStudies(); loadStats(); }
    else toast(`Delete failed (${res.status})`, "err");
    state.deleting = null;
  });

  [...document.querySelectorAll(".modal-backdrop")].forEach((b) =>
    b.addEventListener("click", (e) => {
      if (e.target !== b) return;
      if (document.body.classList.contains("auth-gate") && b.id === "login-modal") return;
      b.hidden = true;
      if (b.id === "user-modal") _userModalAction = null;
    }));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (document.body.classList.contains("auth-gate")) return; // gate cannot be dismissed
      [...document.querySelectorAll(".modal-backdrop")].forEach((b) => {
        if (!b.hidden) {
          b.hidden = true;
          if (b.id === "user-modal") _userModalAction = null;
        }
      });
    }
  });
}

/* ---------------- auth gate ---------------- */
function enterGate() {
  // Nothing opens until login succeeds: blur the app behind an opaque gate
  // and keep the login card front and center (no backdrop/ESC dismissal).
  state.gated = true;
  document.body.classList.add("auth-gate");
  document.body.classList.remove("preboot");
  openLogin();
}

async function finishBoot() {
  const health = await api("/api/v1/health");
  if (health.ok) {
    const h = await health.json();
    // Deterministic re-set from the authenticated boot fetch as well (the
    // early fire-and-forget fetch may have raced or been filtered).
    if (h.public_base_url) window.PAXS_PUBLIC_BASE_URL = h.public_base_url;
    const ok = h.dicom?.enabled;
    const statusEl = $("dicom-status");
    if (!ok) statusEl.classList.add("off");
    $("dicom-status-text").textContent = ok ? `DICOM ${h.dicom.port} · ${h.dicom.ae_title}` : "DICOM receiver off";
    const badgeTls = $("badge-tls");
    if (badgeTls) badgeTls.hidden = !h.tls?.enabled;
    const badgeBackend = $("badge-backend");
    if (badgeBackend) {
      badgeBackend.hidden = false;
      badgeBackend.textContent = h.storage?.backend === "s3"
        ? `S3 · ${h.storage.bucket}` : "local storage";
    }
  }
  $("btn-login").textContent = state.role === "ADMIN" ? "Admin" : (state.username || "Account");
  $("btn-login").style.color = "var(--accent)";
  await Promise.all([loadStats(), loadModalities(), loadStudies(), loadTriage()]);

  const initialView = location.hash.replace("#", "") || "dashboard";
  if (VIEWS.includes(initialView) && initialView !== "dashboard") {
    switchView(initialView);
  }
}

async function boot() {
  applyTheme(document.documentElement.dataset.theme || "dark");
  applyDensity();
  applyColumns();
  bind();
  // Learn the configured public base URL early (public endpoint) so viewer
  // deep-links resolve to the same origin regardless of the access host.
  api("/api/v1/health").then((r) => r.ok && r.json().then((h) => {
    window.PAXS_PUBLIC_BASE_URL = h.public_base_url || "";
  })).catch(() => {});
  checkViewer(); // reveal the viewer entry points if /viewer is served (no auth needed)
  checkAdminPanel(); // reveal the Control Panel entry point if /admin is served
  if (!state.token) { enterGate(); return; }
  // Validate the stored token before showing anything.
  const me = await api("/api/v1/auth/me");
  if (me.ok) {
    const u = await me.json().catch(() => null);
    state.role = u?.role;
    state.username = u?.username;
    state.patientNameDisplay = u?.patient_name_display;
    state.gated = false;
    document.body.classList.remove("preboot");
    finishBoot();
  } else {
    state.token = null;
    localStorage.removeItem("paxs_token");
    enterGate();
  }
}

boot();
