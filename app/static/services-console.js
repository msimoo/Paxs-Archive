/* Services Tab Console for http://localhost:8000/#admin -> Services
 * Provides live controls, settings forms, and interactive diagnostic tools for:
 *   1. DICOM DIMSE Receiver (SCP start/stop/restart + config + self C-ECHO)
 *   2. DCMTK Native Engine (per-tool status matrix, auto-detect, settings, C-ECHO & C-FIND)
 *   3. Web & Integration Layers (DICOMweb, HL7 MLLP start/stop/restart, EMR test, FHIR, Fusion, Webhooks)
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => (typeof window.esc === "function" ? window.esc(s) : String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"));

  async function reqJson(url, opts) {
    const res = await window.api(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = (typeof data.detail === "string" && data.detail)
        || data.error
        || (data.detail && data.detail.error)
        || `Request failed (HTTP ${res.status})`;
      throw new Error(msg);
    }
    return data;
  }

  function showMsg(msg, ok) {
    if (typeof window.toast === "function") {
      window.toast(msg, ok ? "ok" : "err");
    }
  }

  function setOut(elId, text, isErr) {
    const el = $(elId);
    if (!el) return;
    el.hidden = false;
    el.textContent = text;
    el.style.borderColor = isErr ? "rgba(251,113,133,.5)" : "rgba(52,211,153,.45)";
    el.style.color = isErr ? "#fda4af" : "#86efac";
  }

  async function patchSettings(updates) {
    const res = await reqJson("/api/v1/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ updates }),
    });
    const restartNeeded = (res.applied || []).some((item) => item.restart);
    return { ...res, restartNeeded };
  }

  function renderDcmtkTools(dcmtk, detected) {
    const box = $("svc-dcmtk-tools");
    const banner = $("svc-dcmtk-banner");
    if (!box || !banner) return;

    if (dcmtk.available) {
      banner.innerHTML = `<span class="ok-dot"></span> <b>OFFIS DCMTK v${esc(dcmtk.version)}</b> ready · `
        + `Operations: <span class="mono">${esc((dcmtk.operations || []).join(", "))}</span>`
        + (dcmtk.tls_blocked ? ` · <span style="color:#fbbf24">DICOM TLS is enabled (DCMTK only handles non-TLS peers)</span>` : "")
        + (dcmtk.move_reason ? ` · <span class="faint">${esc(dcmtk.move_reason)}</span>` : "");
    } else {
      const hint = detected && detected.length
        ? `Detected ${detected.length} installation(s) — click "Auto-Detect" to apply.`
        : (dcmtk.reason || "Not configured. Install OFFIS DCMTK and set DCMTK_BIN_DIR (e.g. C:\\dcmtk\\bin) or place binaries in .tools\\dcmtk\\bin.");
      banner.innerHTML = `<span class="err-dot"></span> <b>DCMTK unavailable</b> · <span class="faint">${esc(hint)}</span>`;
    }

    box.innerHTML = (dcmtk.tools || []).map((t) => `
      <div class="svc-card ${t.available ? "on" : "off"}">
        <div class="svc-head">${typeof window.svcBadge === "function" ? window.svcBadge(t.available) : (t.available ? "ON" : "OFF")} <b>${esc(t.name)}</b></div>
        <div class="svc-sub">${esc(t.purpose)}</div>
        <div class="svc-sub mono faint">${t.available ? `v${esc(t.version)}` : esc(t.error || "missing")}</div>
      </div>`).join("");
  }

  function fillForms(ov) {
    const d = ov.dicom || {};
    const dt = ov.dcmtk || {};
    const h = ov.hl7 || {};
    const w = ov.web || {};

    if ($("svc-dicom-live-pill")) {
      const disk = ov.storage_disk || {};
      const diskPill = disk.local && typeof disk.free_gb === "number"
        ? ` · Disk: <span class="mono" style="color:${disk.low_space ? "#fb7185" : "#86efac"}">${disk.free_gb} GB free (${disk.free_pct}%)${disk.low_space ? " LOW!" : ""}</span>`
        : "";
      const pduKb = d.max_pdu_size ? Math.round(d.max_pdu_size / 1024) : 128;
      $("svc-dicom-live-pill").innerHTML = d.live
        ? `<span class="ok-dot"></span> LISTENING · <span class="mono">${esc(d.ae_title)} @ ${esc(d.host)}:${d.port}</span> · PDU ${pduKb} KiB · Active stores: ${d.queue?.active_stores ?? 0}${diskPill}`
        : `<span class="err-dot"></span> STOPPED · <span class="mono">${esc(d.ae_title)} :${d.port}</span>${diskPill}`;
    }
    if ($("svc-dicom-enabled")) $("svc-dicom-enabled").checked = Boolean(d.configured);
    if ($("svc-dicom-aet")) $("svc-dicom-aet").value = d.ae_title || "PAXS_SCP";
    if ($("svc-dicom-port")) $("svc-dicom-port").value = String(d.port || 10400);
    if ($("svc-dicom-max-assoc")) $("svc-dicom-max-assoc").value = String(d.max_associations || 16);
    if ($("svc-dicom-tls")) $("svc-dicom-tls").checked = Boolean(d.tls_enabled);
    if ($("svc-dicom-allowed-aes")) $("svc-dicom-allowed-aes").value = d.allowed_calling_aes || "";
    if ($("svc-mwl-aet")) $("svc-mwl-aet").value = d.mwl_default_station_aet || "";

    if ($("svc-dcmtk-dir")) $("svc-dcmtk-dir").value = dt.bin_dir || "";
    if ($("svc-dcmtk-move-port")) $("svc-dcmtk-move-port").value = String(dt.move_port ?? 0);
    if ($("svc-dcmtk-move-aet")) $("svc-dcmtk-move-aet").value = dt.move_ae_title || "PAXS_MOVE";
    if ($("svc-dcmtk-move-profile")) $("svc-dcmtk-move-profile").value = dt.move_profile || "";

    if ($("svc-echo-host") && !$("svc-echo-host").value) $("svc-echo-host").value = "127.0.0.1";
    if ($("svc-echo-port") && !$("svc-echo-port").value) $("svc-echo-port").value = String(d.port || 10400);
    if ($("svc-echo-aet") && !$("svc-echo-aet").value) $("svc-echo-aet").value = d.ae_title || "PAXS_SCP";

    if ($("svc-hl7-live-pill")) {
      $("svc-hl7-live-pill").innerHTML = h.live
        ? `<span class="ok-dot"></span> HL7 LISTENING · <span class="mono">${esc(h.host)}:${h.port}</span>`
        : `<span class="err-dot"></span> HL7 STOPPED · <span class="mono">${esc(h.host)}:${h.port}</span>`;
    }
    if ($("svc-web-pub-read")) $("svc-web-pub-read").checked = Boolean(w.dicomweb_public_read);
    if ($("svc-web-pub-write")) $("svc-web-pub-write").checked = Boolean(w.dicomweb_public_write);
    if ($("svc-web-allow-priv")) $("svc-web-allow-priv").checked = Boolean(w.dicomweb_client_allow_private);
    if ($("svc-web-fhir")) $("svc-web-fhir").checked = Boolean(w.fhir_enabled);
    if ($("svc-web-fusion")) $("svc-web-fusion").checked = Boolean(w.fusion_enabled);
    if ($("svc-web-accel")) $("svc-web-accel").checked = Boolean(w.transfer_accelerator_enabled);
    if ($("svc-web-hl7-on")) $("svc-web-hl7-on").checked = Boolean(h.enabled);
    if ($("svc-web-hl7-host")) $("svc-web-hl7-host").value = h.host || "127.0.0.1";
    if ($("svc-web-hl7-port")) $("svc-web-hl7-port").value = String(h.port || 2575);
    if ($("svc-web-hl7-tls")) $("svc-web-hl7-tls").checked = Boolean(h.tls_enabled);
    if ($("svc-web-hl7-ips")) $("svc-web-hl7-ips").value = h.allowed_source_ips || "";
    if ($("svc-web-emr-on")) $("svc-web-emr-on").checked = Boolean(h.emr_enabled);
    if ($("svc-web-emr-host")) $("svc-web-emr-host").value = h.emr_host || "127.0.0.1";
    if ($("svc-web-emr-port")) $("svc-web-emr-port").value = String(h.emr_port || 6661);
    if ($("svc-web-emr-app")) $("svc-web-emr-app").value = h.emr_application || "EMR";
    if ($("svc-web-emr-fac")) $("svc-web-emr-fac").value = h.emr_facility || "EMR";
    if ($("svc-web-webhooks")) $("svc-web-webhooks").value = w.webhook_urls || "";
  }

  async function loadOverview() {
    try {
      const ov = await reqJson("/api/v1/services/overview");
      fillForms(ov);
      renderDcmtkTools(ov.dcmtk || {}, ov.dcmtk_detected || []);
    } catch (err) {
      if ($("svc-dcmtk-banner")) {
        $("svc-dcmtk-banner").innerHTML = `<span class="faint">Sign in as ADMIN to manage live service controls (${esc(err.message)}).</span>`;
      }
    }
  }

  async function controlDicom(action) {
    try {
      const res = await reqJson(`/api/v1/services/dicom/${action}`, { method: "POST" });
      showMsg(`DICOM SCP ${action}: ${res.live ? "LISTENING" : "STOPPED"}`, true);
      if (typeof window.loadServices === "function") await window.loadServices();
    } catch (err) {
      showMsg(err.message, false);
    }
  }

  async function controlHl7(action) {
    try {
      const res = await reqJson(`/api/v1/services/hl7/${action}`, { method: "POST" });
      showMsg(`HL7 MLLP ${action}: ${res.live ? "LISTENING" : "STOPPED"}`, true);
      if (typeof window.loadServices === "function") await window.loadServices();
    } catch (err) {
      showMsg(err.message, false);
    }
  }

  async function saveDicomSettings(andRestart) {
    try {
      await patchSettings({
        DICOM_RECEIVER_ENABLED: Boolean($("svc-dicom-enabled").checked),
        DICOM_AE_TITLE: $("svc-dicom-aet").value.trim(),
        DICOM_PORT: parseInt($("svc-dicom-port").value, 10),
        DICOM_MAX_ASSOCIATIONS: parseInt($("svc-dicom-max-assoc").value, 10),
        DICOM_TLS_ENABLED: Boolean($("svc-dicom-tls").checked),
        DICOM_ALLOWED_CALLING_AES: $("svc-dicom-allowed-aes").value.trim(),
        MWL_DEFAULT_STATION_AET: $("svc-mwl-aet").value.trim(),
      });
      if (andRestart) {
        await controlDicom("restart");
      } else {
        showMsg("DICOM receiver settings saved.", true);
        if (typeof window.loadServices === "function") await window.loadServices();
      }
    } catch (err) {
      showMsg(err.message, false);
    }
  }

  async function saveDcmtkSettings() {
    try {
      await patchSettings({
        DCMTK_BIN_DIR: $("svc-dcmtk-dir").value.trim(),
        DCMTK_MOVE_PORT: parseInt($("svc-dcmtk-move-port").value || "0", 10),
        DCMTK_MOVE_AE_TITLE: $("svc-dcmtk-move-aet").value.trim() || "PAXS_MOVE",
        DCMTK_MOVE_PROFILE: $("svc-dcmtk-move-profile").value.trim(),
      });
      showMsg("DCMTK configuration saved and applied immediately.", true);
      await loadOverview();
    } catch (err) {
      showMsg(err.message, false);
    }
  }

  async function detectDcmtk() {
    try {
      const res = await reqJson("/api/v1/services/dcmtk/detect", { method: "POST" });
      if (!res.found || !res.found.length) {
        setOut("svc-dcmtk-output", "No genuine OFFIS DCMTK installation was found on this host.\nDownload Windows x64 binaries from https://dicom.offis.de/dcmtk and extract to C:\\dcmtk (or .tools\\dcmtk\\bin).", true);
        return;
      }
      const first = res.found[0];
      $("svc-dcmtk-dir").value = first.bin_dir;
      if (first.profile) $("svc-dcmtk-move-profile").value = first.profile;
      if (!parseInt($("svc-dcmtk-move-port").value || "0", 10)) {
        $("svc-dcmtk-move-port").value = "10401";
      }
      setOut("svc-dcmtk-output", `Detected OFFIS DCMTK v${first.version} at ${first.bin_dir}\nTools: ${first.tools.join(", ")}\nProfile: ${first.profile || "(none)"}\nClick "Save DCMTK Settings" to activate.`, false);
    } catch (err) {
      setOut("svc-dcmtk-output", err.message, true);
    }
  }

  async function runEcho() {
    try {
      setOut("svc-dcmtk-output", "Running C-ECHO probe…", false);
      const res = await reqJson("/api/v1/services/dcmtk/echo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          host: $("svc-echo-host").value.trim() || "127.0.0.1",
          port: parseInt($("svc-echo-port").value || "10400", 10),
          ae_title: $("svc-echo-aet").value.trim() || "PAXS_SCP",
          engine: $("svc-echo-engine").value || "auto",
        }),
      });
      const text = res.ok
        ? `C-ECHO SUCCESS via ${res.engine}${res.version ? ` v${res.version}` : ""} in ${res.latency_ms ?? "?"} ms (status=0x0000)`
        : `C-ECHO FAILED via ${res.engine}: ${res.reason || "unreachable"}`;
      setOut("svc-dcmtk-output", text, !res.ok);
    } catch (err) {
      setOut("svc-dcmtk-output", err.message, true);
    }
  }

  async function runFind() {
    try {
      setOut("svc-dcmtk-output", "Running DCMTK C-FIND (STUDY) probe…", false);
      const res = await reqJson("/api/v1/services/dcmtk/find", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          host: $("svc-echo-host").value.trim() || "127.0.0.1",
          port: parseInt($("svc-echo-port").value || "10400", 10),
          ae_title: $("svc-echo-aet").value.trim() || "PAXS_SCP",
          patient_id: $("svc-find-pid").value.trim(),
          modality: $("svc-find-mod").value.trim(),
          max_results: 20,
        }),
      });
      if (!res.ok) {
        setOut("svc-dcmtk-output", `DCMTK C-FIND FAILED: ${res.reason}`, true);
        return;
      }
      const lines = [`DCMTK C-FIND OK in ${res.latency_ms} ms — ${res.count} study match(es)${res.truncated ? " (truncated)" : ""}:`];
      (res.matches || []).forEach((m, i) => {
        lines.push(`  ${i + 1}. [${m.modality || "OT"}] ${m.study_date || "-"} | ${m.patient_id || "-"} | ${m.description || m.study_uid}`);
      });
      setOut("svc-dcmtk-output", lines.join("\n"), false);
    } catch (err) {
      setOut("svc-dcmtk-output", err.message, true);
    }
  }

  async function saveWebSettings() {
    try {
      const res = await patchSettings({
        DICOMWEB_PUBLIC_READ: Boolean($("svc-web-pub-read").checked),
        DICOMWEB_PUBLIC_WRITE: Boolean($("svc-web-pub-write").checked),
        DICOMWEB_CLIENT_ALLOW_PRIVATE: Boolean($("svc-web-allow-priv").checked),
        FHIR_ENABLED: Boolean($("svc-web-fhir").checked),
        FUSION_ENABLED: Boolean($("svc-web-fusion").checked),
        TRANSFER_ACCELERATOR_ENABLED: Boolean($("svc-web-accel").checked),
        HL7_ENABLED: Boolean($("svc-web-hl7-on").checked),
        HL7_HOST: $("svc-web-hl7-host").value.trim() || "127.0.0.1",
        HL7_PORT: parseInt($("svc-web-hl7-port").value || "2575", 10),
        HL7_TLS_ENABLED: Boolean($("svc-web-hl7-tls").checked),
        HL7_ALLOWED_SOURCE_IPS: $("svc-web-hl7-ips").value.trim(),
        HL7_EMR_ENABLED: Boolean($("svc-web-emr-on").checked),
        HL7_EMR_HOST: $("svc-web-emr-host").value.trim() || "127.0.0.1",
        HL7_EMR_PORT: parseInt($("svc-web-emr-port").value || "6661", 10),
        HL7_EMR_APPLICATION: $("svc-web-emr-app").value.trim() || "EMR",
        HL7_EMR_FACILITY: $("svc-web-emr-fac").value.trim() || "EMR",
        WEBHOOK_URLS: $("svc-web-webhooks").value.trim(),
      });
      showMsg(res.restartNeeded ? "Saved (restart HL7 or server for port/router changes)." : "Web & integration settings saved.", true);
      if (typeof window.loadServices === "function") await window.loadServices();
    } catch (err) {
      showMsg(err.message, false);
    }
  }

  async function testEmr() {
    try {
      const res = await reqJson("/api/v1/services/emr/test", { method: "POST" });
      showMsg(res.ok
        ? `EMR reachable at ${res.host}:${res.port} (${res.latency_ms} ms)`
        : `EMR unreachable at ${res.host}:${res.port} (${res.error})`, res.ok);
    } catch (err) {
      showMsg(err.message, false);
    }
  }

  function bindOnce() {
    if (window.__paxsServicesBound) return;
    window.__paxsServicesBound = true;
    const on = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
    on("svc-dicom-start", () => controlDicom("start"));
    on("svc-dicom-stop", () => controlDicom("stop"));
    on("svc-dicom-restart", () => controlDicom("restart"));
    on("svc-dicom-save", () => saveDicomSettings(false));
    on("svc-dicom-save-restart", () => saveDicomSettings(true));
    on("svc-dcmtk-detect", detectDcmtk);
    on("svc-dcmtk-save", saveDcmtkSettings);
    on("svc-echo-run", runEcho);
    on("svc-find-run", runFind);
    on("svc-hl7-start", () => controlHl7("start"));
    on("svc-hl7-stop", () => controlHl7("stop"));
    on("svc-hl7-restart", () => controlHl7("restart"));
    on("svc-emr-test", testEmr);
    on("svc-web-save", saveWebSettings);
  }

  window.PaxsServicesConsole = {
    load: async function () {
      bindOnce();
      await loadOverview();
    },
  };
})();
