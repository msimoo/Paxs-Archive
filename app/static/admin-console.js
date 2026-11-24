/* ============================================================
   PAXS Archive — admin console shell
   ------------------------------------------------------------
   Three jobs, all of them presentation:
     1. Give every admin panel a header (eyebrow · title · purpose) from one
        map, so the 16 panels cannot drift into 16 different voices.
     2. Fill the hero chips from /api/v1/health instead of hard-coding a port
        that an operator can change in .env.
     3. Mount a lazy three.js point-field in the hero as a quiet accent.

   Nothing here touches data or auth: the panel bodies are still built by
   app.js, and this file only decorates them. Every failure path is silent by
   design — a missing canvas or an unreachable /health must never stop the
   console from working.
   ============================================================ */
(function () {
  "use strict";

  /* [eyebrow, title] keyed by the panel's data-tab. The purpose line is never
     written here: a panel that opens with its own `p.sub` intro has that
     paragraph moved into the header, and one that does not is already
     self-explanatory from its first form or grid. Restating the intro in the
     header produced two near-identical paragraphs on eight panels. */
  var PANELS = {
    sys: ["Overview", "Server overview"],
    license: ["Overview", "License"],
    services: ["DICOM", "DICOM services"],
    protocols: ["DICOM", "Clinical protocols"],
    storage: ["Data", "Storage"],
    migration: ["Data", "Migration"],
    replication: ["Data", "Replication"],
    users: ["Access", "Users"],
    tenants: ["Access", "Tenants"],
    sessions: ["Access", "Sessions"],
    "auth-audit": ["Access", "Auth audit"],
    router: ["Operations", "Smart router"],
    critical: ["Operations", "Critical results"],
    audit: ["Operations", "Audit trail"],
    scripts: ["Operations", "Scripts"],
    dbperf: ["Operations", "DB pool"]
  };

  /* ---------------------------------------------------------------- headers
     Every panel gets the same eyebrow/title header. Where a panel already
     opens with its own `p.sub` intro, that paragraph is **moved** into the
     header rather than copied or restated — the project's own wording is the
     best description, and moving the node keeps any `<code id="…">` inside it
     live for app.js. */
  function addHeaders() {
    Object.keys(PANELS).forEach(function (tab) {
      var panel = document.getElementById("panel-" + tab);
      if (!panel || panel.querySelector(":scope > .admin-head")) return;
      var meta = PANELS[tab];
      var head = document.createElement("header");
      head.className = "admin-head";
      var eyebrow = document.createElement("span");
      eyebrow.className = "admin-head-eyebrow";
      eyebrow.textContent = meta[0];
      var title = document.createElement("h3");
      title.textContent = meta[1];
      head.appendChild(eyebrow);
      head.appendChild(title);

      var lead = panel.firstElementChild;
      if (lead && lead.tagName === "P" && lead.classList.contains("sub")) {
        lead.style.margin = "0";
        lead.classList.add("admin-head-lead");
        head.appendChild(lead);
      }
      panel.insertBefore(head, panel.firstChild);
    });
  }

  /* ------------------------------------------------------------------ chips */
  function setChip(id, html, tone) {
    var chip = document.getElementById(id);
    if (!chip) return;
    chip.innerHTML = html;
    chip.classList.remove("is-live", "is-down");
    if (tone) chip.classList.add(tone);
  }

  function fillChips() {
    var count = document.querySelectorAll("#admin-tabs button").length;
    setChip("admin-chip-modules", "<b>" + count + "</b> module" + (count === 1 ? "" : "s"), null);

    fetch("/api/v1/health", { credentials: "same-origin" })
      .then(function (res) {
        if (res.ok) return res.json();
        /* A gate refusal is not an outage: say "restricted" so an operator
           does not go looking for a dead service. */
        var denied = res.status === 401 || res.status === 403;
        setChip("admin-chip-dimse", "<b>DIMSE</b> " + (denied ? "restricted" : "unavailable"), "is-down");
        setChip("admin-chip-live", "<i aria-hidden=\"true\"></i>" + (denied ? "restricted" : "unreachable"), "is-down");
        return null;
      })
      .then(function (health) {
        if (!health) return;
        var dicom = health.dicom || {};
        if (dicom.enabled) {
          var ae = dicom.ae_title ? String(dicom.ae_title) : "AE";
          setChip("admin-chip-dimse", "<b>" + escapeText(ae) + "</b> :" + escapeText(String(dicom.port || "")), null);
        } else {
          setChip("admin-chip-dimse", "<b>DIMSE</b> off", "is-down");
        }
        setChip("admin-chip-live", "<i aria-hidden=\"true\"></i>reachable", "is-live");
      })
      .catch(function () {
        setChip("admin-chip-live", "<i aria-hidden=\"true\"></i>unreachable", "is-down");
      });
  }

  function escapeText(value) {
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  /* ------------------------------------------------------- rail ergonomics */
  function keepActiveVisible() {
    var rail = document.getElementById("admin-tabs");
    if (!rail) return;
    rail.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-tab]");
      if (button) button.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
  }

  /* --------------------------------------------------------- three.js field
     A 45x23 point grid that undulates. The engine in paxs-accent.js owns the
     WebGL guard, the single lazy script load, the DPR cap, resize, the
     visibility pause and disposal; this file only describes the geometry, so
     the same discipline cannot drift between views. */
  var COLS = 44;
  var ROWS = 22;
  var SPAN_X = 17;
  var SPAN_Y = 8.4;

  function buildLattice(THREE, size) {
    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(40, size.width / size.height, 0.1, 100);
    camera.position.set(0, 0, 9.4);

    var count = (COLS + 1) * (ROWS + 1);
    var positions = new Float32Array(count * 3);
    var baseX = new Float32Array(count);
    var baseY = new Float32Array(count);
    var cursor = 0;
    for (var row = 0; row <= ROWS; row++) {
      for (var col = 0; col <= COLS; col++) {
        baseX[cursor] = -SPAN_X / 2 + (SPAN_X * col) / COLS;
        baseY[cursor] = -SPAN_Y / 2 + (SPAN_Y * row) / ROWS;
        positions[cursor * 3] = baseX[cursor];
        positions[cursor * 3 + 1] = baseY[cursor];
        positions[cursor * 3 + 2] = 0;
        cursor++;
      }
    }

    var geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    var material = new THREE.PointsMaterial({
      color: window.PaxsAccent ? window.PaxsAccent.color() : 0x22d3ee,
      size: 0.072,
      sizeAttenuation: true,
      transparent: true,
      opacity: 0.72,
      depthWrite: false
    });
    var points = new THREE.Points(geometry, material);
    points.rotation.x = -0.46;
    scene.add(points);

    return {
      scene: scene,
      camera: camera,
      update: function (t) {
        var attr = geometry.getAttribute("position");
        for (var i = 0; i < count; i++) {
          attr.array[i * 3 + 2] =
            Math.sin(baseX[i] * 0.52 + t * 1.05) * 0.42 +
            Math.cos(baseY[i] * 0.78 + t * 0.72) * 0.26;
        }
        attr.needsUpdate = true;
        points.rotation.y = Math.sin(t * 0.16) * 0.14;
      },
      recolor: function (hex) { material.color.setHex(hex); },
      dispose: function () { geometry.dispose(); material.dispose(); }
    };
  }

  function mountLattice() {
    var host = document.getElementById("admin-lattice");
    if (!host || !window.PaxsAccent) return;
    window.PaxsAccent.mount(host, buildLattice);
  }

  /* -------------------------------------------------------------- bootstrap */
  function init() {
    addHeaders();
    fillChips();
    keepActiveVisible();
    mountLattice();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
