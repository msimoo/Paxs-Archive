/* PAXS Viewer — Upgraded Standalone DICOM Viewer & Three.js 3D Orientation Engine */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  studyUid: null, series: [], seriesIdx: 0, instanceUid: null,
  instances: [], instIdx: 0, window: "", shareToken: null,
  cine: { playing: false, timer: null },
  _abortCtrl: null,
};
const canvas = $("v-canvas");
const ctx = canvas.getContext("2d");
const img = new Image();
img.crossOrigin = "anonymous";
let _canvasW = 0, _canvasH = 0;

const MOD_COLORS = {
  CT: "#22d3ee", MR: "#2dd4bf", CR: "#fbbf24", DX: "#fb923c",
  US: "#a855f7", NM: "#fb7185", PT: "#f472b6", XA: "#38bdf8",
  MG: "#e879f9", OT: "#94a3b8",
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtDate(d) { return d ? `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}` : "—"; }

/* ---------------- Theme System ---------------- */
const THEME_KEY = "paxs_theme";

function applyTheme(theme, opts = {}) {
  const next = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  if (opts.persist) {
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  }
}

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res;
}

/* ---------------- Three.js 3D Orientation Cube ---------------- */
let threeCube = { scene: null, camera: null, renderer: null, cube: null, animFrame: null };

function initThreeOrientationCube() {
  if (!window.THREE) return;
  const cv = $("v-3d-cube-canvas");
  if (!cv) return;

  const THREE = window.THREE;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x060b17);

  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
  camera.position.set(0, 0, 4);

  const renderer = new THREE.WebGLRenderer({ canvas: cv, antialias: true, alpha: true });
  renderer.setSize(120, 120);
  renderer.setPixelRatio(window.devicePixelRatio || 1);

  // 3D Anatomical Cube
  const geo = new THREE.BoxGeometry(1.5, 1.5, 1.5);
  const materials = [
    createAxisMaterial("R", "#22d3ee"), // Right (+X)
    createAxisMaterial("L", "#0284c7"), // Left (-X)
    createAxisMaterial("S", "#2dd4bf"), // Superior (+Y)
    createAxisMaterial("I", "#0f766e"), // Inferior (-Y)
    createAxisMaterial("A", "#a855f7"), // Anterior (+Z)
    createAxisMaterial("P", "#7e22ce"), // Posterior (-Z)
  ];

  const cube = new THREE.Mesh(geo, materials);
  scene.add(cube);
  threeCube = { scene, camera, renderer, cube };

  function animate() {
    threeCube.animFrame = requestAnimationFrame(animate);
    if (threeCube.cube) {
      threeCube.cube.rotation.y += 0.008;
      threeCube.cube.rotation.x += 0.004;
    }
    renderer.render(scene, camera);
  }
  animate();
}

function createAxisMaterial(text, bgColorHex) {
  const THREE = window.THREE;
  const cv = document.createElement("canvas");
  cv.width = 128; cv.height = 128;
  const ctx = cv.getContext("2d");
  ctx.fillStyle = bgColorHex;
  ctx.fillRect(0, 0, 128, 128);
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 4;
  ctx.strokeRect(4, 4, 120, 120);
  ctx.fillStyle = "#ffffff";
  ctx.font = "bold 56px ui-monospace, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, 64, 64);
  const tex = new THREE.CanvasTexture(cv);
  return new THREE.MeshBasicMaterial({ map: tex });
}

/* ---------------- Boot & Load ---------------- */
async function boot() {
  try {
    initThreeOrientationCube();
    const params = new URLSearchParams(location.search);
    const shareToken = params.get("share");
    let studyUid = params.get("StudyInstanceUIDs") || params.get("study");

    if (shareToken) {
      state.shareToken = shareToken;
      const res = await api(`/api/v1/share/${shareToken}`);
      const info = await res.json();
      studyUid = info.study_uid;
      $("v-patient").textContent = info.patient.name || "Patient";
      $("v-meta").textContent = `${fmtDate(info.study.date)} · ${info.study.description || "—"} · shared link`;
    }

    if (!studyUid) { $("v-patient").textContent = "No study selected"; return; }
    state.studyUid = studyUid;
    await loadStudy(studyUid);
  } catch (err) {
    console.error("[PAXS Viewer] boot failed:", err);
    $("v-patient").textContent = "Failed to load study";
    $("v-meta").textContent = err.message || "Network error";
    drawEmpty("Failed to load study");
  }
}

async function loadStudy(studyUid) {
  const res = await api(`/api/v1/studies/${encodeURIComponent(studyUid)}`);
  const detail = await res.json();
  $("v-patient").textContent = detail.patient.name || "Unknown patient";
  $("v-meta").textContent =
    `${detail.patient.id || "—"} · ${fmtDate(detail.study.date)} · ${detail.study.description || "No description"}`;
  state.series = detail.series;
  renderSeries();
  if (state.series.length) selectSeries(0);
}

function renderSeries() {
  $("v-series").innerHTML = state.series.map((s, i) => `
    <div class="series-card ${i === state.seriesIdx ? "active" : ""}" data-i="${i}" role="button" tabindex="0">
      ${s.thumbnail ? `<img src="${s.thumbnail}" loading="lazy" alt="" />` : `<div style="width:56px;height:56px;background:#000;border-radius:6px;"></div>`}
      <div class="info">
        <div style="display:flex;gap:6px;align-items:center;">
          <span style="font-family:var(--mono);font-size:10px;font-weight:700;color:${MOD_COLORS[s.modality] || "#22d3ee"}">${esc(s.modality || "?")}</span>
          <span style="font-size:11px;color:var(--muted);">${s.instances} imgs</span>
        </div>
        <div style="font-size:12px;font-weight:600;margin-top:2px;">${esc(s.description) || "Series"}</div>
      </div>
    </div>`).join("");

  document.querySelectorAll(".series-card").forEach((el) => {
    el.addEventListener("click", () => selectSeries(Number(el.dataset.i)));
  });
}

async function selectSeries(idx) {
  state.seriesIdx = idx;
  document.querySelectorAll(".series-card").forEach((el, i) => el.classList.toggle("active", i === idx));
  if (state._abortCtrl) { state._abortCtrl.abort(); state._abortCtrl = null; }

  const s = state.series[idx];
  try {
    const res = await api(`/dicom-web/studies/${encodeURIComponent(state.studyUid)}/series/${encodeURIComponent(s.series_uid)}/instances`);
    state.instances = await res.json();
  } catch (err) {
    state.instances = [];
  }
  state.instIdx = 0;
  if (state.instances.length) renderImage(0);
  else drawEmpty();
}

function sopOf(i) {
  return state.instances[i]?.["00080018"]?.Value?.[0] || state.instances[i]?.sop_uid;
}

function renderImage(idx) {
  state.instIdx = idx;
  const sop = sopOf(idx);
  state.instanceUid = sop;
  const series = state.series[state.seriesIdx];
  const q = new URLSearchParams({ viewport: "1536,1536" });
  if (state.window) q.set("window", state.window);
  const url = `/dicom-web/studies/${encodeURIComponent(state.studyUid)}/series/${encodeURIComponent(series.series_uid)}/instances/${encodeURIComponent(sop)}/rendered?${q}`;

  if (state._abortCtrl) state._abortCtrl.abort();
  const ctrl = new AbortController();
  state._abortCtrl = ctrl;

  fetch(url, { signal: ctrl.signal })
    .then((res) => res.blob())
    .then((blob) => {
      if (ctrl.signal.aborted) return;
      const objUrl = URL.createObjectURL(blob);
      img.onload = () => {
        URL.revokeObjectURL(objUrl);
        draw();
        $("v-counter").textContent = `${state.instIdx + 1} / ${state.instances.length}`;
        $("v-prev").disabled = state.instIdx === 0;
        $("v-next").disabled = state.instIdx >= state.instances.length - 1;
      };
      img.src = objUrl;
    })
    .catch((err) => {
      if (err.name === "AbortError") return;
      drawEmpty("Render failed");
    });
}

function draw() {
  const zoom = Number($("v-zoom").value) / 100;
  const w = Math.round(img.naturalWidth * zoom);
  const h = Math.round(img.naturalHeight * zoom);
  if (_canvasW !== w || _canvasH !== h) {
    canvas.width = w; canvas.height = h;
    _canvasW = w; _canvasH = h;
  } else {
    ctx.clearRect(0, 0, w, h);
  }
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(img, 0, 0, w, h);
}

function drawEmpty(msg) {
  canvas.width = 800; canvas.height = 600;
  ctx.fillStyle = "#03060d"; ctx.fillRect(0, 0, 800, 600);
  ctx.fillStyle = "#94a3b8"; ctx.font = "14px ui-monospace, monospace";
  ctx.textAlign = "center";
  ctx.fillText(msg || "No image", 400, 300);
}

function toggleCine() {
  state.cine.playing = !state.cine.playing;
  const btn = $("v-cine");
  if (btn) {
    btn.textContent = state.cine.playing ? "⏸ Pause" : "▶ Cine";
    btn.classList.toggle("active", state.cine.playing);
  }
  if (state.cine.playing) {
    state.cine.timer = setInterval(() => {
      if (state.instances.length) {
        const nextIdx = (state.instIdx + 1) % state.instances.length;
        renderImage(nextIdx);
      }
    }, 120);
  } else {
    if (state.cine.timer) clearInterval(state.cine.timer);
  }
}

/* ---------------- Controls ---------------- */
function bind() {
  applyTheme(document.documentElement.dataset.theme || "dark");
  const themeBtn = $("btn-theme");
  if (themeBtn) themeBtn.addEventListener("click", () => {
    const next = (document.documentElement.dataset.theme || "dark") === "dark" ? "light" : "dark";
    applyTheme(next, { persist: true });
  });

  $("v-prev").addEventListener("click", () => { if (state.instIdx > 0) renderImage(state.instIdx - 1); });
  $("v-next").addEventListener("click", () => { if (state.instIdx < state.instances.length - 1) renderImage(state.instIdx + 1); });
  $("v-cine").addEventListener("click", toggleCine);
  $("v-back").addEventListener("click", () => { location.href = "/"; });
  $("v-zoom").addEventListener("input", () => {
    $("v-zoom-label").textContent = `${$("v-zoom").value}%`;
    if (img.complete && img.naturalWidth) draw();
  });

  document.querySelectorAll("#v-presets .preset").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#v-presets .preset").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.window = b.dataset.window;
      if (state.instanceUid) renderImage(state.instIdx);
    });
  });

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    if (e.key === "ArrowLeft") $("v-prev").click();
    if (e.key === "ArrowRight") $("v-next").click();
    if (e.key === " ") { e.preventDefault(); toggleCine(); }
  });
}

bind();
boot();
