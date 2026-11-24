/* ============================================================
   PAXS Archive — shared three.js accent engine
   ------------------------------------------------------------
   Several dashboard views carry a small animated field as decoration. The
   interesting part of each is the geometry; everything around it is the same
   boilerplate, and getting that boilerplate wrong is what leaks WebGL contexts:

     · skip entirely without WebGL, or when the visitor asked for reduced motion
     · load /viewer/three.min.js once, lazily, for every host on the page
     · cap the device-pixel ratio so a 3× screen does not render 9× the pixels
     · resize from a ResizeObserver, never from window.resize
     · stop the RAF loop while the tab is hidden, and keep the clock continuous
       across the pause so the animation does not jump on return
     · re-read --accent when data-theme flips
     · dispose geometry, material and renderer on teardown

   A host supplies only `build(THREE, size)` returning:

     { scene, camera, update(elapsedSeconds), recolor?(hex), dispose?() }

   Usage: PaxsAccent.mount(document.getElementById("x"), build)
   ============================================================ */
(function () {
  "use strict";

  var threePromise = null;

  function loadThree() {
    if (window.THREE) return Promise.resolve(window.THREE);
    if (threePromise) return threePromise;
    threePromise = new Promise(function (resolve) {
      var script = document.createElement("script");
      script.src = "/viewer/three.min.js";
      script.async = true;
      script.onload = function () { resolve(window.THREE || null); };
      script.onerror = function () { resolve(null); };
      document.head.appendChild(script);
    });
    return threePromise;
  }

  /* The theme tokens are hex strings; THREE wants an integer. */
  function color(fallback) {
    var raw = "";
    try {
      raw = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
    } catch (error) {
      raw = "";
    }
    if (raw && raw.charAt(0) === "#") return parseInt(raw.slice(1), 16);
    return typeof fallback === "number" ? fallback : 0x22d3ee;
  }

  function mount(host, build) {
    if (!host || !window.WebGLRenderingContext) return null;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return null;
    if (typeof build !== "function") return null;

    var state = {
      disposed: false,
      frame: 0,
      renderer: null,
      field: null,
      observer: null,
      themeObserver: null,
      last: 0,
      elapsed: 0
    };

    function teardown() {
      if (state.disposed) return;
      state.disposed = true;
      if (state.frame) cancelAnimationFrame(state.frame);
      state.frame = 0;
      if (state.observer) state.observer.disconnect();
      if (state.themeObserver) state.themeObserver.disconnect();
      if (state.field && typeof state.field.dispose === "function") state.field.dispose();
      if (state.renderer) state.renderer.dispose();
      state.field = null;
      state.renderer = null;
      document.removeEventListener("visibilitychange", onVisibility);
    }

    function loop(now) {
      if (state.disposed) return;
      if (!state.last) state.last = now;
      state.elapsed += (now - state.last) / 1000;
      state.last = now;
      state.field.update(state.elapsed);
      state.renderer.render(state.field.scene, state.field.camera);
      state.frame = requestAnimationFrame(loop);
    }

    function onVisibility() {
      if (state.disposed) return;
      if (document.hidden) {
        if (state.frame) cancelAnimationFrame(state.frame);
        state.frame = 0;
        state.last = 0;
      } else if (!state.frame) {
        state.last = 0;
        state.frame = requestAnimationFrame(loop);
      }
    }

    function start(THREE) {
      if (state.disposed) return;
      try {
        var width = host.clientWidth || 320;
        var height = host.clientHeight || 140;

        state.renderer = new THREE.WebGLRenderer({ canvas: host, antialias: true, alpha: true });
        state.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
        state.renderer.setSize(width, height, false);

        state.field = build(THREE, { width: width, height: height });
        if (!state.field || !state.field.scene || !state.field.camera) {
          teardown();
          return;
        }

        if (window.ResizeObserver) {
          state.observer = new ResizeObserver(function (entries) {
            if (state.disposed || !entries.length) return;
            var box = entries[0].contentRect;
            if (!box.width || !box.height) return;
            state.field.camera.aspect = box.width / box.height;
            state.field.camera.updateProjectionMatrix();
            state.renderer.setSize(box.width, box.height, false);
          });
          state.observer.observe(host);
        }

        state.themeObserver = new MutationObserver(function () {
          if (state.field && typeof state.field.recolor === "function") {
            state.field.recolor(color());
          }
        });
        state.themeObserver.observe(document.documentElement, {
          attributes: true, attributeFilter: ["data-theme"]
        });

        document.addEventListener("visibilitychange", onVisibility);
        state.frame = requestAnimationFrame(loop);
      } catch (error) {
        /* A lost or refused WebGL context is decoration-only: drop the field
           and leave the panel as plain content. */
        teardown();
      }
    }

    function boot() {
      loadThree().then(function (THREE) {
        if (!THREE || state.disposed) return;
        start(THREE);
      });
    }

    if ("requestIdleCallback" in window) window.requestIdleCallback(boot, { timeout: 1800 });
    else setTimeout(boot, 240);

    window.addEventListener("pagehide", teardown, { once: true });
    return { destroy: teardown };
  }

  window.PaxsAccent = { load: loadThree, color: color, mount: mount };
})();
