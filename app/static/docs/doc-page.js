/* ============================================================================
   PAXS Archive — documentation page behaviour
   Theme, reading progress, outline scroll-spy, table search, back-to-top.
   No framework, no build step: this page is server-rendered HTML.
   ========================================================================== */
(function () {
  "use strict";

  var doc = document.getElementById("doc");
  if (!doc) return;

  var root = document.documentElement;
  var topbar = document.querySelector(".topbar");
  var TOPBAR = topbar ? topbar.offsetHeight : 60;
  var ARABIC = ["٠", "١", "٢", "٣", "٤", "٥", "٦", "٧", "٨", "٩"];

  function arabic(value) {
    return String(value).replace(/[0-9]/g, function (d) {
      return ARABIC[+d];
    });
  }

  /* ------------------------------------------------------------- theme ---- */

  var themeBtn = document.getElementById("themeBtn");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try {
        localStorage.setItem("paxs_theme", next);
      } catch (e) {
        /* storage blocked — the choice simply does not persist */
      }
    });
  }

  /* ------------------------------------------------------------ print ----- */

  var printBtn = document.getElementById("printBtn");
  if (printBtn) {
    printBtn.addEventListener("click", function () {
      window.print();
    });
  }

  /* ------------------------------------------------- progress + to-top ---- */

  var bar = document.getElementById("scrollProgress");
  var toTop = document.getElementById("toTop");
  var ticking = false;

  function onScroll() {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(function () {
      ticking = false;
      var max = doc.scrollHeight - window.innerHeight;
      var top = window.pageYOffset || root.scrollTop;
      if (bar) {
        var pct = max > 0 ? Math.min(100, Math.max(0, (top / max) * 100)) : 0;
        bar.style.width = pct.toFixed(2) + "%";
      }
      if (toTop) toTop.classList.toggle("show", top > 480);
      spy();
    });
  }

  if (toTop) {
    toTop.addEventListener("click", function () {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  /* --------------------------------------------------------- scroll-spy --- */

  var links = {};
  Array.prototype.forEach.call(document.querySelectorAll(".toc-link"), function (link) {
    var id = (link.getAttribute("href") || "").replace(/^#/, "");
    if (id) links[id] = link;
  });

  var targets = [];
  Array.prototype.forEach.call(
    doc.querySelectorAll(".doc-sec[id], .doc-h3[id]"),
    function (node) {
      if (links[node.id]) targets.push(node);
    }
  );

  var currentId = "";

  function spy() {
    if (!targets.length) return;
    var line = TOPBAR + 48;
    var active = targets[0];
    for (var i = 0; i < targets.length; i += 1) {
      if (targets[i].getBoundingClientRect().top <= line) active = targets[i];
    }
    if (!active || active.id === currentId) return;
    if (currentId && links[currentId]) {
      links[currentId].classList.remove("active");
      links[currentId].removeAttribute("aria-current");
    }
    currentId = active.id;
    var link = links[currentId];
    if (!link) return;
    link.classList.add("active");
    link.setAttribute("aria-current", "true");
    var nav = document.getElementById("tocNav");
    if (nav && nav.scrollHeight > nav.clientHeight) {
      var box = link.getBoundingClientRect();
      var area = nav.getBoundingClientRect();
      if (box.top < area.top || box.bottom > area.bottom) {
        nav.scrollTop += box.top - area.top - area.height / 2 + box.height / 2;
      }
    }
  }

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll, { passive: true });

  /* ---------------------------------------------------- mobile outline ---- */

  var tocToggle = document.getElementById("tocToggle");
  var toc = document.getElementById("toc");
  if (tocToggle && toc) {
    tocToggle.addEventListener("click", function () {
      var open = toc.getAttribute("data-open") === "true";
      toc.setAttribute("data-open", open ? "false" : "true");
      tocToggle.setAttribute("aria-expanded", open ? "false" : "true");
    });
  }

  /* ---------------------------------------------------- table search ------ */

  var input = document.getElementById("tocSearch");
  var counter = document.getElementById("searchCount");
  if (!input) return;

  var sections = Array.prototype.slice.call(doc.querySelectorAll(".doc-sec"));
  var heads = Array.prototype.slice.call(doc.querySelectorAll(".doc-h3"));
  var rows = Array.prototype.slice.call(doc.querySelectorAll(".doc-table tbody tr"));
  var wraps = Array.prototype.slice.call(doc.querySelectorAll(".table-wrap"));
  var original = new Map();
  rows.forEach(function (row) {
    original.set(row, row.innerHTML);
  });

  function clearHighlight(row) {
    var html = original.get(row);
    if (html !== undefined) row.innerHTML = html;
  }

  function highlight(row, needle) {
    var walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT, null);
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(function (node) {
      var at = node.nodeValue.toLowerCase().indexOf(needle);
      if (at < 0) return;
      var range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + needle.length);
      var mark = document.createElement("mark");
      mark.style.background = "var(--accent-soft)";
      mark.style.color = "var(--accent)";
      mark.style.borderRadius = "3px";
      try {
        range.surroundContents(mark);
      } catch (e) {
        /* range crossed an element boundary — leave this node unmarked */
      }
    });
  }

  function reset() {
    rows.forEach(function (row) {
      row.hidden = false;
      clearHighlight(row);
    });
    wraps.forEach(function (wrap) {
      wrap.classList.remove("no-match");
    });
    sections.forEach(function (section) {
      section.classList.remove("is-dimmed");
    });
    heads.forEach(function (head) {
      head.classList.remove("is-dimmed");
    });
    if (counter) counter.hidden = true;
  }

  /* A sub-heading whose tables all came up empty would be left dangling above
     nothing, so it is folded away with them. */
  function pruneHeads(needle) {
    heads.forEach(function (head) {
      var keep = head.textContent.toLowerCase().indexOf(needle) >= 0;
      var node = head.nextElementSibling;
      while (node && !node.matches(".doc-h3")) {
        if (node.classList.contains("table-wrap")) {
          if (!node.classList.contains("no-match")) keep = true;
        } else if (node.textContent.toLowerCase().indexOf(needle) >= 0) {
          keep = true;
        }
        node = node.nextElementSibling;
      }
      head.classList.toggle("is-dimmed", !keep);
    });
  }

  function filter(raw) {
    var needle = raw.trim().toLowerCase();
    if (needle.length < 2) {
      reset();
      return;
    }
    var hits = 0;
    rows.forEach(function (row) {
      var match = row.textContent.toLowerCase().indexOf(needle) >= 0;
      row.hidden = !match;
      if (match) {
        hits += 1;
        clearHighlight(row);
        highlight(row, needle);
      }
    });
    wraps.forEach(function (wrap) {
      var visible = wrap.querySelectorAll("tbody tr:not([hidden])").length;
      wrap.classList.toggle("no-match", visible === 0);
    });
    sections.forEach(function (section) {
      var own = section.querySelectorAll(".table-wrap");
      if (!own.length) return;
      var visible = section.querySelectorAll("tbody tr:not([hidden])").length;
      section.classList.toggle("is-dimmed", visible === 0);
    });
    pruneHeads(needle);
    if (counter) {
      counter.textContent = hits ? arabic(hits) : "٠";
      counter.hidden = false;
    }
  }

  var timer = 0;
  input.addEventListener("input", function () {
    window.clearTimeout(timer);
    timer = window.setTimeout(function () {
      filter(input.value);
    }, 90);
  });
  input.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      input.value = "";
      reset();
    }
  });

  onScroll();
})();
