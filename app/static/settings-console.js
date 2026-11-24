/* ============================================================
   PAXS Archive — settings console shell
   ------------------------------------------------------------
   Two jobs, both presentation:

     1. Summarise the panel: how many settings the server exposes, how many
        only take effect after a restart, how many currently override their
        environment default, and whether this account can edit them. Every
        number is **counted from the rows app.js rendered** — no second
        request, so the chips cannot contradict the form below them.

     2. A three.js accent in the hero: three counter-rotating rings joined by
        struts, which is what "layers of configuration" looks like.

   The engine in paxs-accent.js owns the WebGL guard, lazy load, resize,
   visibility pause and disposal. Nothing here touches data or auth.
   ============================================================ */
(function () {
  "use strict";

  /* ------------------------------------------------------------ the meter */
  function setChip(key, value) {
    var el = document.querySelector('#set-chips [data-chip="' + key + '"] b');
    if (el) el.textContent = value;
  }

  function paint() {
    var stage = document.querySelector('.settings-stage');
    if (!stage) return;
    var rows = stage.querySelectorAll('.cfg-row');
    var restart = stage.querySelectorAll('.restart-tag').length;
    var edited = stage.querySelectorAll('.changed-tag').length;
    /* renderGroup() emits either a per-group Save button or a .cfg-ro notice,
       so "at least one Save button" is the honest signal that this account can
       edit — a single read-only group must not mark the whole console. */
    var editable = stage.querySelectorAll('.cfg-save-one').length > 0;

    setChip('keys', rows.length ? String(rows.length) : '—');
    setChip('restart', rows.length ? String(restart) : '—');
    setChip('edited', rows.length ? String(edited) : '—');
    setChip('access', rows.length ? (editable ? 'admin' : 'read-only') : '—');
  }

  /* Coalesce with a timer, not requestAnimationFrame: a background tab
     produces no frames, so an rAF-coalesced repaint would leave the chips
     stale until the next mutation after the operator returned. */
  function watch() {
    var stage = document.querySelector('.settings-stage');
    if (!stage) return;
    var scheduled = false;
    new MutationObserver(function () {
      if (scheduled) return;
      scheduled = true;
      setTimeout(function () { scheduled = false; paint(); }, 0);
    }).observe(stage, { childList: true, subtree: true });
    paint();
  }

  /* ------------------------------------------------- three.js: the layers
     Three rings of dots at different heights, each rotating at its own rate
     and direction, joined by vertical struts. */
  function buildLayers(THREE, size) {
    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(40, size.width / size.height, 0.1, 100);
    camera.position.set(0, 0.4, 7.2);
    camera.lookAt(0, 0, 0);

    var accent = window.PaxsAccent ? window.PaxsAccent.color() : 0x22d3ee;
    var geos = [];
    var mats = [];
    var root = new THREE.Group();
    var rings = [
      { radius: 2.05, y: -0.95, count: 44, speed: 0.26 },
      { radius: 1.55, y: 0, count: 34, speed: -0.4 },
      { radius: 1.05, y: 0.95, count: 26, speed: 0.55 }
    ];
    var pivots = [];

    rings.forEach(function (ring) {
      var positions = new Float32Array(ring.count * 3);
      for (var i = 0; i < ring.count; i++) {
        var angle = (i / ring.count) * Math.PI * 2;
        positions[i * 3] = Math.cos(angle) * ring.radius;
        positions[i * 3 + 1] = ring.y;
        positions[i * 3 + 2] = Math.sin(angle) * ring.radius;
      }
      var geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      var mat = new THREE.PointsMaterial({
        color: accent, size: 0.075, sizeAttenuation: true,
        transparent: true, opacity: 0.8, depthWrite: false
      });
      geos.push(geo); mats.push(mat);
      var pivot = new THREE.Group();
      pivot.add(new THREE.Points(geo, mat));
      root.add(pivot);
      pivots.push({ group: pivot, speed: ring.speed });
    });

    /* Struts between the outer and inner ring, at eight shared angles. */
    var strut = new Float32Array(8 * 6);
    for (var s = 0; s < 8; s++) {
      var a = (s / 8) * Math.PI * 2;
      strut[s * 6] = Math.cos(a) * rings[0].radius;
      strut[s * 6 + 1] = rings[0].y;
      strut[s * 6 + 2] = Math.sin(a) * rings[0].radius;
      strut[s * 6 + 3] = Math.cos(a) * rings[2].radius;
      strut[s * 6 + 4] = rings[2].y;
      strut[s * 6 + 5] = Math.sin(a) * rings[2].radius;
    }
    var strutGeo = new THREE.BufferGeometry();
    strutGeo.setAttribute('position', new THREE.BufferAttribute(strut, 3));
    var strutMat = new THREE.LineBasicMaterial({ color: accent, transparent: true, opacity: 0.16 });
    geos.push(strutGeo); mats.push(strutMat);
    root.add(new THREE.LineSegments(strutGeo, strutMat));

    root.rotation.x = 0.34;
    scene.add(root);

    return {
      scene: scene,
      camera: camera,
      update: function (elapsed) {
        root.rotation.y = Math.sin(elapsed * 0.16) * 0.2;
        for (var i = 0; i < pivots.length; i++) {
          pivots[i].group.rotation.y = elapsed * pivots[i].speed;
        }
      },
      recolor: function (hex) {
        for (var i = 0; i < mats.length; i++) mats[i].color.setHex(hex);
      },
      dispose: function () {
        for (var i = 0; i < geos.length; i++) geos[i].dispose();
        for (var j = 0; j < mats.length; j++) mats[j].dispose();
      }
    };
  }

  function mountAccent() {
    var host = document.getElementById('settings-lattice');
    if (!host || !window.PaxsAccent) return;
    window.PaxsAccent.mount(host, buildLayers);
  }

  /* -------------------------------------------------------------- bootstrap */
  function init() {
    watch();
    mountAccent();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
