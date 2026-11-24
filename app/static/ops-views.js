/* ============================================================
   PAXS Archive — workflow + nodes views
   ------------------------------------------------------------
   Two jobs, both presentation:

     1. A meter for each view. Workflow gets a three-stage pipeline
        (worklist -> performed -> commitments) and Nodes gets a roster
        summary. Every number is **counted from the rows the lists actually
        rendered**, so the meter can never disagree with the list under it —
        no second request, no second source of truth.

     2. One three.js accent per hero. The engine in paxs-accent.js owns the
        WebGL guard, lazy load, resize, visibility pause and disposal; this
        file only describes the geometry.

   Nothing here touches data or auth. If a list never renders, its meter
   simply stays empty.
   ============================================================ */
(function () {
  "use strict";

  /* ------------------------------------------------------------ the meter */

  /* A list renders one of three things: rows, an empty-state paragraph, or a
     "Loading…" paragraph. Only the first is a count worth showing. */
  function listState(list) {
    if (!list) return { rows: null, pending: false };
    var rows = list.querySelectorAll(".node-item");
    if (rows.length) return { rows: rows, pending: false };
    var text = (list.textContent || "").trim();
    return { rows: rows, pending: /^Loading/i.test(text) };
  }

  function tally(list) {
    var state = listState(list);
    var out = { total: 0, done: 0, prog: 0, open: 0, cx: 0, pending: state.pending };
    if (!state.rows) return out;
    out.total = state.rows.length;
    for (var i = 0; i < state.rows.length; i++) {
      var badge = state.rows[i].querySelector(".wl-status");
      if (!badge) continue;
      if (badge.classList.contains("done")) out.done++;
      else if (badge.classList.contains("prog")) out.prog++;
      else if (badge.classList.contains("cx")) out.cx++;
      else if (badge.classList.contains("open")) out.open++;
    }
    return out;
  }

  function setCount(key, value) {
    var nodes = document.querySelectorAll('[data-count="' + key + '"]');
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = value;
  }

  function setChip(scopeId, key, value) {
    var chip = document.querySelector("#" + scopeId + ' [data-chip="' + key + '"] b');
    if (chip) chip.textContent = value;
  }

  function paintStage(key, stats) {
    setCount(key, stats.pending ? "…" : String(stats.total));
    var bar = document.querySelector('[data-bar="' + key + '"]');
    if (!bar) return;
    var keys = ["done", "prog", "open", "cx"];
    for (var i = 0; i < keys.length; i++) {
      var seg = bar.querySelector('[data-k="' + keys[i] + '"]');
      if (seg) seg.style.flexGrow = String(stats[keys[i]] || 0);
    }
  }

  function paintWorkflow() {
    var mwl = tally(document.getElementById("worklist-list"));
    var mpps = tally(document.getElementById("mpps-list"));
    var commit = tally(document.getElementById("commit-list"));
    paintStage("mwl", mwl);
    paintStage("mpps", mpps);
    paintStage("commit", commit);
    setChip("wf-chips", "scheduled", mwl.pending ? "…" : String(mwl.open));
    setChip("wf-chips", "performed", mpps.pending ? "…" : String(mpps.total));
    setChip("wf-chips", "committed", commit.pending ? "…" : String(commit.done));
  }

  function paintNodes() {
    var list = document.getElementById("node-list");
    var state = listState(list);
    var total = 0;
    var web = 0;
    var off = 0;
    if (state.rows && state.rows.length) {
      total = state.rows.length;
      for (var i = 0; i < state.rows.length; i++) {
        if (state.rows[i].getAttribute("data-node-web") === "1") web++;
        if (state.rows[i].getAttribute("data-node-disabled") === "1") off++;
      }
    }
    var shown = state.pending ? "…" : String(total);
    setChip("nd-chips", "nodes", shown);
    setChip("nd-chips", "web", state.pending ? "…" : String(web));
    setChip("nd-chips", "disabled", state.pending ? "…" : String(off));
    var badge = document.getElementById("node-count");
    if (badge) badge.textContent = shown;
  }

  /* One observer per list, coalesced into a single paint per turn.
     setTimeout, not requestAnimationFrame: a background tab produces no
     frames, so an rAF-coalesced repaint would leave the meter stale until the
     next mutation after the operator came back. */
  function watch(ids, paint) {
    var scheduled = false;
    function schedule() {
      if (scheduled) return;
      scheduled = true;
      setTimeout(function () {
        scheduled = false;
        paint();
      }, 0);
    }
    for (var i = 0; i < ids.length; i++) {
      var list = document.getElementById(ids[i]);
      if (!list) continue;
      new MutationObserver(schedule).observe(list, { childList: true, subtree: true });
    }
    paint();
  }

  /* ---------------------------------------------------- three.js: the flow
     Seven streams of particles riding faint rails from left to right — the
     shape of a pipeline rather than a decorative blob. Each particle carries
     a slightly different speed so the wraps never happen in unison. */
  function buildFlow(THREE, size) {
    var ROWS = 7;
    var PER_ROW = 46;
    var SPAN = 15.5;
    var ROW_GAP = 0.46;
    var count = ROWS * PER_ROW;

    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(42, size.width / size.height, 0.1, 100);
    camera.position.set(0, 0, 8.2);

    var positions = new Float32Array(count * 3);
    var seed = new Float32Array(count);
    var rowOf = new Uint8Array(count);
    var index = 0;
    for (var row = 0; row < ROWS; row++) {
      for (var k = 0; k < PER_ROW; k++) {
        seed[index] = k / PER_ROW;
        rowOf[index] = row;
        index++;
      }
    }

    var geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    var material = new THREE.PointsMaterial({
      color: window.PaxsAccent ? window.PaxsAccent.color() : 0x22d3ee,
      size: 0.058,
      sizeAttenuation: true,
      transparent: true,
      opacity: 0.8,
      depthWrite: false
    });
    var points = new THREE.Points(geometry, material);
    scene.add(points);

    /* The rails: two vertices per row, so the flow reads as travelling along
       something instead of drifting in space. */
    var railPos = new Float32Array(ROWS * 6);
    for (var r = 0; r < ROWS; r++) {
      var y = (r - (ROWS - 1) / 2) * ROW_GAP;
      railPos[r * 6] = -SPAN / 2;
      railPos[r * 6 + 1] = y;
      railPos[r * 6 + 2] = 0;
      railPos[r * 6 + 3] = SPAN / 2;
      railPos[r * 6 + 4] = y;
      railPos[r * 6 + 5] = 0;
    }
    var railGeo = new THREE.BufferGeometry();
    railGeo.setAttribute("position", new THREE.BufferAttribute(railPos, 3));
    var railMat = new THREE.LineBasicMaterial({
      color: window.PaxsAccent ? window.PaxsAccent.color() : 0x22d3ee,
      transparent: true,
      opacity: 0.14
    });
    var rails = new THREE.LineSegments(railGeo, railMat);
    scene.add(rails);

    var group = new THREE.Group();
    group.add(points);
    group.add(rails);
    group.rotation.x = -0.16;
    scene.add(group);

    return {
      scene: scene,
      camera: camera,
      update: function (t) {
        var attr = geometry.getAttribute("position");
        for (var i = 0; i < count; i++) {
          var row = rowOf[i];
          var speed = 0.055 + row * 0.006 + seed[i] * 0.018;
          var progress = (seed[i] + t * speed) % 1;
          var y = (row - (ROWS - 1) / 2) * ROW_GAP;
          attr.array[i * 3] = (progress - 0.5) * SPAN;
          attr.array[i * 3 + 1] = y + Math.sin(progress * 6.2 + row * 0.8 + t * 0.7) * 0.1;
          attr.array[i * 3 + 2] = Math.sin(progress * 3.4 + t * 0.5) * 0.22;
        }
        attr.needsUpdate = true;
        group.rotation.y = Math.sin(t * 0.18) * 0.1;
      },
      recolor: function (hex) {
        material.color.setHex(hex);
        railMat.color.setHex(hex);
      },
      dispose: function () {
        geometry.dispose();
        material.dispose();
        railGeo.dispose();
        railMat.dispose();
      }
    };
  }

  /* -------------------------------------------------- three.js: the graph
     One archive at the centre, peers on a ring, edges between them — the
     topology an AE registry actually describes. */
  function buildGraph(THREE, size) {
    var PEERS = 7;
    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(40, size.width / size.height, 0.1, 100);
    camera.position.set(0, 0, 6.6);

    var peerPositions = [];
    for (var i = 0; i < PEERS; i++) {
      var angle = (i / PEERS) * Math.PI * 2 - Math.PI / 2;
      var radius = 2.05 + (i % 2) * 0.4;
      peerPositions.push(
        Math.cos(angle) * radius,
        Math.sin(angle) * radius * 0.66,
        Math.sin(i * 1.7) * 0.3
      );
    }

    var nodePos = new Float32Array((PEERS + 1) * 3);
    nodePos[0] = 0; nodePos[1] = 0; nodePos[2] = 0;
    for (var n = 0; n < PEERS; n++) {
      nodePos[(n + 1) * 3] = peerPositions[n * 3];
      nodePos[(n + 1) * 3 + 1] = peerPositions[n * 3 + 1];
      nodePos[(n + 1) * 3 + 2] = peerPositions[n * 3 + 2];
    }

    /* Hub spokes plus the ring between peers — a hub alone reads as a star,
       while a ring of peer-to-peer links reads as a network. */
    var edgeCount = PEERS * 2;
    var edgePos = new Float32Array(edgeCount * 6);
    for (var e = 0; e < PEERS; e++) {
      edgePos[e * 6] = 0;
      edgePos[e * 6 + 1] = 0;
      edgePos[e * 6 + 2] = 0;
      edgePos[e * 6 + 3] = peerPositions[e * 3];
      edgePos[e * 6 + 4] = peerPositions[e * 3 + 1];
      edgePos[e * 6 + 5] = peerPositions[e * 3 + 2];

      var next = (e + 1) % PEERS;
      var base = (PEERS + e) * 6;
      edgePos[base] = peerPositions[e * 3];
      edgePos[base + 1] = peerPositions[e * 3 + 1];
      edgePos[base + 2] = peerPositions[e * 3 + 2];
      edgePos[base + 3] = peerPositions[next * 3];
      edgePos[base + 4] = peerPositions[next * 3 + 1];
      edgePos[base + 5] = peerPositions[next * 3 + 2];
    }

    var accent = window.PaxsAccent ? window.PaxsAccent.color() : 0x22d3ee;

    var nodeGeo = new THREE.BufferGeometry();
    nodeGeo.setAttribute("position", new THREE.BufferAttribute(nodePos, 3));
    var nodeMat = new THREE.PointsMaterial({
      color: accent, size: 0.14, sizeAttenuation: true,
      transparent: true, opacity: 0.9, depthWrite: false
    });
    var nodes = new THREE.Points(nodeGeo, nodeMat);

    var edgeGeo = new THREE.BufferGeometry();
    edgeGeo.setAttribute("position", new THREE.BufferAttribute(edgePos, 3));
    var edgeMat = new THREE.LineBasicMaterial({ color: accent, transparent: true, opacity: 0.2 });
    var edges = new THREE.LineSegments(edgeGeo, edgeMat);

    var group = new THREE.Group();
    group.add(edges);
    group.add(nodes);
    scene.add(group);

    return {
      scene: scene,
      camera: camera,
      update: function (t) {
        group.rotation.y = t * 0.24;
        group.rotation.x = Math.sin(t * 0.34) * 0.14;
        /* Breathe the hub so the archive reads as alive, not as a diagram. */
        nodeMat.size = 0.14 + Math.sin(t * 1.6) * 0.014;
      },
      recolor: function (hex) {
        nodeMat.color.setHex(hex);
        edgeMat.color.setHex(hex);
      },
      dispose: function () {
        nodeGeo.dispose();
        nodeMat.dispose();
        edgeGeo.dispose();
        edgeMat.dispose();
      }
    };
  }

  function mountAccents() {
    if (!window.PaxsAccent) return;
    var flow = document.getElementById("workflow-flow");
    if (flow) window.PaxsAccent.mount(flow, buildFlow);
    var graph = document.getElementById("nodes-graph");
    if (graph) window.PaxsAccent.mount(graph, buildGraph);
  }

  /* -------------------------------------------------------------- bootstrap */
  function init() {
    watch(["worklist-list", "mpps-list", "commit-list"], paintWorkflow);
    watch(["node-list"], paintNodes);
    mountAccents();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
