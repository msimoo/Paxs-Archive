/**
 * PAXS web bridge for the PaxsViewer Control Panel.
 *
 * The control-panel renderer is an Electron app: it talks exclusively through
 * `window.electronApi` (IPC). When served from the PAXS Archive server
 * (`/admin`), this script installs a compatible `window.electronApi` backed by
 * PAXS REST endpoints so the exact same React UI works in a normal browser,
 * with the same-origin archive token (localStorage "paxs_token").
 *
 * Canonical copy lives at build-win/admin-bridge.js — build-win/sync_admin_panel.py
 * copies it into app/static/admin after syncing the renderer bundle.
 */
(function () {
  "use strict";

  var API_BASE = "/api/v1/admin-panel";
  var TOKEN_KEY = "paxs_token";

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY); } catch (e) { return null; }
  }

  function api(path, options) {
    options = options || {};
    var headers = Object.assign({}, options.headers || {});
    var t = getToken();
    if (t) headers["Authorization"] = "Bearer " + t;
    var body = options.body;
    if (body && typeof body !== "string") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    return fetch(API_BASE + path, {
      method: options.method || "GET",
      headers: headers,
      body: body,
    }).then(function (res) {
      if (res.status === 401) {
        try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ }
        // V2-4: carry the reverse-proxy mount prefix (injected by the shell).
        window.location.href = (window.PAXS_BASE_PATH || "") + "/";
        throw new Error("unauthorized");
      }
      if (!res.ok) {
        return res.text().then(function (txt) {
          throw new Error(txt || ("HTTP " + res.status));
        });
      }
      var ct = res.headers.get("content-type") || "";
      return ct.indexOf("application/json") !== -1 ? res.json() : res.text();
    });
  }

  // ── Tiny event bus (mirrors the Electron IPC subscriptions) ──────────
  var bus = { health: [], status: [], log: [] };
  function emit(name, data) {
    (bus[name] || []).forEach(function (cb) {
      try { cb(data); } catch (err) { console.error("[admin-bridge]", err); }
    });
  }
  function on(name, cb) {
    bus[name].push(cb);
    return function () {
      bus[name] = bus[name].filter(function (f) { return f !== cb; });
    };
  }

  // ── Poller: the Electron app pushed updates; the web bridge polls. ────
  var pollTimer = null;
  var lastLogCount = 0;

  function tick() {
    return api("/status").then(function (snap) {
      if (snap && snap.lastChecked) emit("health", snap);
      return api("/logs").then(function (logs) {
        if (Array.isArray(logs)) {
          for (var i = lastLogCount; i < logs.length; i++) emit("log", logs[i]);
          lastLogCount = logs.length;
        }
      });
    }).catch(function () { /* transient — next tick */ });
  }

  function startPolling(interval) {
    if (pollTimer) clearInterval(pollTimer);
    lastLogCount = 0;
    pollTimer = setInterval(tick, Math.max(1000, interval || 5000));
  }

  // After an action the server returns per-service status changes — feed them
  // to the optimistic UI update path exactly like the Electron push events.
  function dispatchChanges(changes) {
    (changes || []).forEach(function (c) {
      if (c && c.name && c.status) emit("status", { name: c.name, status: c.status });
    });
  }

  var electronApi = {
    // ── State retrieval ─────────────────────────────────────────────
    getInitialSnapshot: function () {
      return api("/status").catch(function () { return null; });
    },
    getLogs: function () {
      return api("/logs").then(function (logs) {
        // keep the poller in sync so the first tick doesn't re-emit entries
        // the initial getLogs already showed (avoids duplicate log lines)
        if (Array.isArray(logs)) lastLogCount = logs.length;
        return logs;
      }).catch(function () { return []; });
    },

    // ── Service control ─────────────────────────────────────────────
    startAll: function () {
      return api("/actions/start-all", { method: "POST" }).then(function (r) {
        dispatchChanges(r && r.changes);
        return !!(r && r.ok);
      });
    },
    stopAll: function () {
      return api("/actions/stop-all", { method: "POST" }).then(function (r) {
        dispatchChanges(r && r.changes);
        return undefined;
      });
    },
    restartService: function (service) {
      return api("/actions/restart", { method: "POST", body: { service: service } }).then(function (r) {
        dispatchChanges(r && r.changes);
        return !!(r && r.ok);
      });
    },

    // ── Launch / open ───────────────────────────────────────────────
    launchViewer: function () {
      return api("/actions/launch-viewer", { method: "POST" }).then(function () { return undefined; });
    },
    launchAdmin: function () {
      return api("/actions/launch-admin", { method: "POST" }).then(function () { return undefined; });
    },
    openViewerInBrowser: function () {
      return api("/actions/open-viewer", { method: "POST" }).then(function () { return undefined; });
    },
    openAdminInBrowser: function () {
      return api("/actions/open-admin", { method: "POST" }).then(function () { return undefined; });
    },

    // ── Config management ───────────────────────────────────────────
    getConfig: function () {
      return api("/config").catch(function () { return null; });
    },
    saveConfig: function (config) {
      return api("/config", { method: "POST", body: config }).then(function (r) {
        return { success: !!(r && r.ok), config: (r && r.config) || null, error: (r && r.error) || undefined };
      }).catch(function (err) {
        return { success: false, config: null, error: String((err && err.message) || err) };
      });
    },

    // ── Event subscriptions ─────────────────────────────────────────
    onHealthUpdate: function (cb) { return on("health", cb); },
    onServiceStatusChanged: function (cb) { return on("status", cb); },
    onLogEntry: function (cb) { return on("log", cb); },
  };

  window.electronApi = electronApi;

  // Start the poller once the configured interval is known.
  api("/config").then(function (cfg) {
    var interval = (cfg && cfg.monitorIntervalMs) || 5000;
    tick();
    startPolling(interval);
  }).catch(function () {
    tick();
    startPolling(5000);
  });
})();
