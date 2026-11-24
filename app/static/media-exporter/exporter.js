(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const RING_CIRC = 364.42;
  const STEP_ORDER = ["downloading", "preparing", "microdicom", "writing", "verifying"];
  const MOD_COLORS = {
    CT: "#38bdf8", MR: "#a78bfa", US: "#34d399", CR: "#fbbf24",
    DX: "#fbbf24", PT: "#fb7185", NM: "#f472b6", XA: "#2dd4bf", MG: "#f97316",
  };

  const state = {
    tab: "media",
    mediaKind: "usb",
    zipFormat: "dicom-zip",
    selectedUsb: "",
    selectedDisc: "",
    devices: null,
    study: null,
    microdicom: null,
    running: false,
    lastOutputPath: "",
    lastHasMicrodicom: false,
    pollTimer: null,
  };

  function fmtBytes(n) {
    if (!Number.isFinite(n) || n <= 0) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let v = n, i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
    return `${v >= 10 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${u[i]}`;
  }

  function cleanDirName(raw) {
    return (raw || "DICOM").replace(/[^A-Za-z0-9_]+/g, "").toUpperCase().slice(0, 8) || "DICOM";
  }

  function api() {
    return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
  }

  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll(".mode-tab").forEach((b) => {
      const on = b.dataset.tab === tab;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    $("panel-media").classList.toggle("hidden", tab !== "media");
    $("panel-dicom-zip").classList.toggle("hidden", tab !== "dicom-zip");
    updatePreview();
  }

  function setMediaKind(kind) {
    state.mediaKind = kind;
    document.querySelectorAll("#media-kinds .kind-chip").forEach((b) => {
      b.classList.toggle("active", b.dataset.kind === kind);
    });
    $("usb-section").classList.toggle("hidden", kind !== "usb");
    $("disc-section").classList.toggle("hidden", kind !== "disc");
    $("iso-folder-section").classList.toggle("hidden", kind !== "iso" && kind !== "folder");
    updatePreview();
  }

  function setZipFormat(fmt) {
    state.zipFormat = fmt;
    document.querySelectorAll("#zip-format-grid .format-card").forEach((b) => {
      b.classList.toggle("active", b.dataset.fmt === fmt);
    });
  }

  function updatePreview() {
    const dir = cleanDirName($("opt-dicom-dir").value);
    const includeMd = $("opt-include-md").checked;
    const arch = $("opt-md-arch").value;
    const openMode = $("opt-md-open").value;
    const rootPrefix = state.mediaKind === "usb"
      ? ($("usb-write-root").checked ? `${state.selectedUsb || "E:"}\\` : `${state.selectedUsb || "E:"}\\${$("usb-folder-name").value || "PAXS_STUDY"}\\`)
      : (state.mediaKind === "disc" ? `${state.selectedDisc || "D:"}\\` : `${$("media-dest-dir").value || "<ChosenFolder>"}\\`);

    const lines = [
      `${rootPrefix}`,
      ` ├── DICOMDIR                (PS3.10 Index → points to .\\${dir}\\...)`,
      ` ├── ${dir}\\                  (All DICOM study files stored here)`,
    ];
    if (includeMd) {
      lines.push(
        ` ├── autorun.inf             (Auto-opens MicroDicom → ${openMode === "dicomdir" ? "DICOMDIR" : `/scan ${dir}`})`,
        ` ├── START_VIEWER.bat        (1-click Windows launcher for USB & CD/DVD)`,
        ` ├── x64\\mDicom.exe          (MicroDicom 2026.5 64-bit Viewer)`,
      );
      if (arch === "universal") {
        lines.push(` ├── Win32\\mDicom.exe        (MicroDicom 2026.5 32-bit Fallback)`);
      }
      lines.push(` └── settings\\settings.xml   (Pre-configured offline clinical layout)`);
    }
    $("layout-preview").textContent = lines.join("\n");
    $("md-config-grid").style.opacity = includeMd ? "1" : "0.55";
  }

  function renderStudy(study) {
    if (!study) return;
    state.study = study;
    const mods = study.modalities && study.modalities.length ? study.modalities : ["OT"];
    $("study-mods").innerHTML = mods.map((m) =>
      `<span class="mod-pill" style="background:${MOD_COLORS[m] || "#38bdf8"}">${m}</span>`
    ).join("");
    const displayName = (study.patient_name || "Anonymous").replace(/\^+/g, " ").trim();
    $("study-patient").textContent = displayName || "Anonymous";
    $("study-sub").textContent = `ID: ${study.patient_id || "—"}  ·  ${study.description || "Medical Study"}  ·  Acc: ${study.accession || "—"}`;
    $("stat-series").textContent = String(study.n_series ?? "—");
    $("stat-instances").textContent = String(study.n_instances ?? "—");
    $("stat-size").textContent = fmtBytes(study.total_bytes || 0);
    $("stat-date").textContent = study.study_date || "—";

    const safeBase = (study.accession || study.patient_id || "STUDY")
      .replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[._-]+|[._-]+$/g, "").slice(0, 28) || "STUDY";
    const datePart = (study.study_date || "").replace(/[^0-9]/g, "").slice(0, 8);
    const defaultName = `PAXS_${safeBase}${datePart ? "_" + datePart : ""}`;
    if (!$("usb-folder-name").value) $("usb-folder-name").value = defaultName;
    if (!$("zip-output-name").value) $("zip-output-name").value = defaultName;
    updatePreview();
  }

  function renderMicroDicom(md) {
    state.microdicom = md;
    const pill = $("md-pill");
    if (md && md.available) {
      pill.className = "pill pill-ok";
      $("md-pill-text").textContent = `MicroDicom ${md.version} Ready (${fmtBytes(md.compact_bytes)}–${fmtBytes(md.universal_bytes)})`;
    } else {
      pill.className = "pill pill-err";
      $("md-pill-text").textContent = "MicroDicom bundle not found";
      $("opt-include-md").checked = false;
    }
    updatePreview();
  }

  function renderDevices(inv) {
    state.devices = inv;
    const usbBox = $("usb-list");
    const usbs = inv.usb_drives || [];
    if (!usbs.length) {
      usbBox.innerHTML = `<div class="empty-hint">No removable USB flash drive detected. Plug in a USB drive and click <b>Scan Drives</b>.</div>`;
      state.selectedUsb = "";
    } else {
      if (!state.selectedUsb || !usbs.some((u) => u.drive === state.selectedUsb)) {
        state.selectedUsb = usbs[0].drive;
      }
      usbBox.innerHTML = usbs.map((u) => {
        const usedPct = u.total_bytes > 0 ? Math.min(100, Math.round(((u.total_bytes - u.free_bytes) / u.total_bytes) * 100)) : 0;
        const sel = u.drive === state.selectedUsb ? "selected" : "";
        return `
          <button type="button" class="drive-card ${sel}" data-usb="${u.drive}">
            <div class="drive-top">
              <strong>${u.label || "Removable USB"}</strong>
              <span class="drive-letter">${u.drive}</span>
            </div>
            <div class="drive-bar"><i style="width:${usedPct}%"></i></div>
            <div class="drive-meta">${fmtBytes(u.free_bytes)} free of ${fmtBytes(u.total_bytes)} · ${u.filesystem || "USB"}</div>
          </button>`;
      }).join("");
      usbBox.querySelectorAll("[data-usb]").forEach((el) => {
        el.addEventListener("click", () => {
          state.selectedUsb = el.dataset.usb;
          renderDevices(state.devices);
          updatePreview();
        });
      });
    }

    const discBox = $("disc-list");
    const recs = inv.recorders || [];
    if (!recs.length) {
      discBox.innerHTML = `<div class="empty-hint">${inv.recorder_error || "No optical CD/DVD burner detected on this PC."}</div>`;
      state.selectedDisc = "";
    } else {
      if (!state.selectedDisc || !recs.some((r) => r.drive === state.selectedDisc)) {
        state.selectedDisc = recs[0].drive;
      }
      discBox.innerHTML = recs.map((r) => `
        <button type="button" class="drive-card ${r.drive === state.selectedDisc ? "selected" : ""}" data-disc="${r.drive}">
          <div class="drive-top">
            <strong>${r.model || "Optical CD/DVD Recorder"}</strong>
            <span class="drive-letter">${r.drive}</span>
          </div>
          <div class="drive-meta">Windows IMAPI2 Optical Recorder</div>
        </button>`).join("");
      discBox.querySelectorAll("[data-disc]").forEach((el) => {
        el.addEventListener("click", () => {
          state.selectedDisc = el.dataset.disc;
          renderDevices(state.devices);
          updatePreview();
        });
      });
    }
    renderQuickDirs(inv.quick_dirs || []);
    updatePreview();
  }

  function renderQuickDirs(dirs) {
    const html = dirs.map((d) =>
      `<button type="button" class="quick-dir" data-path="${d.path.replace(/"/g, "&quot;")}">${d.label}</button>`
    ).join("");
    ["media-quick-dirs", "zip-quick-dirs"].forEach((id) => {
      const box = $(id);
      box.innerHTML = html;
      box.querySelectorAll(".quick-dir").forEach((btn) => {
        btn.addEventListener("click", () => {
          const targetInput = id === "media-quick-dirs" ? $("media-dest-dir") : $("zip-dest-dir");
          targetInput.value = btn.dataset.path;
          updatePreview();
        });
      });
    });
  }

  function renderProgress(p) {
    const pct = Math.max(0, Math.min(100, Number(p.percent || 0)));
    const ring = $("ring-fg");
    ring.style.strokeDashoffset = String(RING_CIRC * (1 - pct / 100));
    ring.classList.remove("ok", "err");
    if (p.phase === "verified") ring.classList.add("ok");
    if (p.phase === "failed") ring.classList.add("err");

    $("ring-pct").textContent = `${pct}%`;
    $("ring-phase").textContent = (p.phase || "ready").toUpperCase();
    $("status-msg").textContent = p.message || "";
    $("tel-files").textContent = `${p.files_done || 0} / ${p.files_total || 0}`;
    $("tel-bytes").textContent = fmtBytes(p.bytes_written || 0);
    $("tel-speed").textContent = p.speed_mbps > 0 ? `${p.speed_mbps.toFixed(1)} MB/s` : "—";
    $("tel-time").textContent = `${Math.round(p.elapsed_s || 0)}s`;

    const curIdx = STEP_ORDER.indexOf(p.phase);
    document.querySelectorAll("#pipeline-steps li").forEach((li, idx) => {
      li.classList.remove("active", "done", "err");
      if (p.phase === "verified") {
        li.classList.add("done");
      } else if (p.phase === "failed") {
        if (idx < Math.max(0, curIdx)) li.classList.add("done");
        else if (idx === Math.max(0, curIdx)) li.classList.add("err");
      } else if (curIdx >= 0) {
        if (idx < curIdx) li.classList.add("done");
        else if (idx === curIdx) li.classList.add("active");
      }
    });

    if (p.logs && p.logs.length) {
      const logEl = $("activity-log");
      logEl.textContent = p.logs.join("\n");
      logEl.scrollTop = logEl.scrollHeight;
    }

    const isTerminal = p.phase === "verified" || p.phase === "failed";
    state.running = Boolean(p.active && !isTerminal);
    $("btn-start").disabled = state.running;
    $("btn-start").textContent = state.running ? "⏳ Exporting & Verifying…" : "⚡ Start Export & Verify";

    if (p.phase === "failed" && p.error) {
      $("error-banner").textContent = p.error;
      $("error-banner").classList.remove("hidden");
      $("success-banner").classList.add("hidden");
    } else if (p.phase === "verified") {
      state.lastOutputPath = p.output_path || "";
      state.lastHasMicrodicom = Boolean(p.microdicom_verified);
      $("error-banner").classList.add("hidden");
      $("success-banner").textContent = `✓ Export Complete & Verified${p.output_path ? " → " + p.output_path : ""}`;
      $("success-banner").classList.remove("hidden");
      $("done-actions").classList.remove("hidden");
      $("btn-launch-md").classList.toggle("hidden", !state.lastHasMicrodicom);
    } else {
      $("error-banner").classList.add("hidden");
      $("success-banner").classList.add("hidden");
      $("done-actions").classList.add("hidden");
    }

    if (isTerminal && state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function pollStatus() {
    const bridge = api();
    if (!bridge) return;
    try {
      const p = await bridge.get_progress();
      renderProgress(p);
    } catch (_) { /* ignore transient bridge hiccup */ }
  }

  async function startExport() {
    const bridge = api();
    if (!bridge || state.running) return;
    $("error-banner").classList.add("hidden");
    $("success-banner").classList.add("hidden");
    $("done-actions").classList.add("hidden");

    let payload;
    if (state.tab === "media") {
      const kind = state.mediaKind;
      let target = "";
      if (kind === "usb") {
        target = state.selectedUsb;
        if (!target) {
          $("error-banner").textContent = "Please connect and select a USB flash drive first.";
          $("error-banner").classList.remove("hidden");
          return;
        }
      } else if (kind === "disc") {
        target = state.selectedDisc;
        if (!target) {
          $("error-banner").textContent = "Please select a CD/DVD recorder drive first.";
          $("error-banner").classList.remove("hidden");
          return;
        }
      } else {
        target = $("media-dest-dir").value.trim();
        if (!target) {
          $("error-banner").textContent = "Please choose a destination directory first.";
          $("error-banner").classList.remove("hidden");
          return;
        }
      }
      payload = {
        workflow: "media",
        kind,
        target,
        folder_name: $("usb-folder-name").value.trim(),
        write_to_root: Boolean($("usb-write-root").checked),
        volume_label: ($("disc-volume-label").value.trim() || "PAXS_DICOM").toUpperCase(),
        include_microdicom: Boolean($("opt-include-md").checked),
        dicom_dir: cleanDirName($("opt-dicom-dir").value),
        microdicom_arch: $("opt-md-arch").value,
        microdicom_open: $("opt-md-open").value,
        anonymize: Boolean($("opt-anonymize-media").checked),
      };
    } else {
      const destDir = $("zip-dest-dir").value.trim();
      if (!destDir) {
        $("error-banner").textContent = "Please choose a destination directory where the exported files will be saved.";
        $("error-banner").classList.remove("hidden");
        return;
      }
      payload = {
        workflow: "dicom-zip",
        format: state.zipFormat,
        dest_dir: destDir,
        output_name: $("zip-output-name").value.trim(),
        dicom_dir: cleanDirName($("zip-dicom-dir").value),
        microdicom_arch: $("opt-md-arch").value,
        microdicom_open: $("opt-md-open").value,
      };
    }

    const res = await bridge.start_export(payload);
    if (!res.ok) {
      $("error-banner").textContent = res.error || "Could not start export.";
      $("error-banner").classList.remove("hidden");
      return;
    }
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(pollStatus, 350);
    void pollStatus();
  }

  async function init() {
    document.querySelectorAll(".mode-tab").forEach((b) =>
      b.addEventListener("click", () => setTab(b.dataset.tab))
    );
    document.querySelectorAll("#media-kinds .kind-chip").forEach((b) =>
      b.addEventListener("click", () => setMediaKind(b.dataset.kind))
    );
    document.querySelectorAll("#zip-format-grid .format-card").forEach((b) =>
      b.addEventListener("click", () => setZipFormat(b.dataset.fmt))
    );
    ["opt-dicom-dir", "opt-include-md", "opt-md-arch", "opt-md-open", "usb-folder-name", "usb-write-root", "media-dest-dir"].forEach((id) => {
      $(id).addEventListener("input", updatePreview);
      $(id).addEventListener("change", updatePreview);
    });

    $("btn-theme").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
    });
    $("btn-refresh-drives").addEventListener("click", async () => {
      const bridge = api();
      if (bridge) renderDevices(await bridge.scan_devices());
    });
    $("btn-check-disc").addEventListener("click", async () => {
      const bridge = api();
      if (!bridge) return;
      $("disc-check-msg").textContent = "Checking optical drive…";
      const res = await bridge.check_disc(state.selectedDisc);
      $("disc-check-msg").textContent = res.ok
        ? `Ready: ${res.media_type} · ${fmtBytes(res.available_bytes)} free on ${res.drive}`
        : (res.error || "Insert a blank CD/DVD.");
    });
    $("btn-pick-media-dir").addEventListener("click", async () => {
      const bridge = api();
      if (!bridge) return;
      const res = await bridge.pick_folder();
      if (res.ok && res.path) { $("media-dest-dir").value = res.path; updatePreview(); }
    });
    $("btn-pick-zip-dir").addEventListener("click", async () => {
      const bridge = api();
      if (!bridge) return;
      const res = await bridge.pick_folder();
      if (res.ok && res.path) { $("zip-dest-dir").value = res.path; }
    });
    $("btn-register-proto").addEventListener("click", async () => {
      const bridge = api();
      if (!bridge) return;
      const res = await bridge.register_protocol();
      $("status-msg").textContent = res.ok
        ? "✓ Registered paxs-exporter:// protocol for 1-click browser launching."
        : (res.error || "Protocol registration failed.");
    });
    $("btn-start").addEventListener("click", startExport);
    $("btn-open-dest").addEventListener("click", async () => {
      const bridge = api();
      if (bridge && state.lastOutputPath) await bridge.open_folder(state.lastOutputPath);
    });
    $("btn-launch-md").addEventListener("click", async () => {
      const bridge = api();
      if (bridge && state.lastOutputPath) {
        const res = await bridge.launch_microdicom(state.lastOutputPath);
        if (!res.ok) {
          $("error-banner").textContent = res.error || "Could not launch MicroDicom.";
          $("error-banner").classList.remove("hidden");
        }
      }
    });

    const bridge = api();
    if (!bridge) return;
    const boot = await bridge.get_bootstrap();
    if (boot.mode === "dicom-zip") setTab("dicom-zip");
    else setTab("media");
    if (boot.prefs) {
      if (boot.prefs.last_dest_dir) {
        $("zip-dest-dir").value = boot.prefs.last_dest_dir;
        $("media-dest-dir").value = boot.prefs.last_dest_dir;
      }
      if (boot.prefs.dicom_dir) {
        $("opt-dicom-dir").value = boot.prefs.dicom_dir;
        $("zip-dicom-dir").value = boot.prefs.dicom_dir;
      }
      if (boot.prefs.microdicom_arch) $("opt-md-arch").value = boot.prefs.microdicom_arch;
      if (boot.prefs.microdicom_open) $("opt-md-open").value = boot.prefs.microdicom_open;
    }
    renderStudy(boot.study);
    renderMicroDicom(boot.microdicom);
    renderDevices(boot.devices);
    updatePreview();
  }

  window.addEventListener("pywebviewready", init);
  if (window.pywebview && window.pywebview.api) init();
})();
