/* Archive Tools tab controller for /settings:
 *   1. DICOM Tag Editor + Study Split & Merge
 *   2. Scheduled Backup & Restore Integrity Verification
 *   3. HL7 / RIS & Modality Worklist (MWL) Management
 */
(function () {
    'use strict';

    function csrfToken() {
        var match = document.cookie.match(/(?:^|;\s*)paxs_csrf=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    async function apiJson(url, opts) {
        var options = Object.assign({ credentials: 'same-origin' }, opts || {});
        var headers = Object.assign({ 'Accept': 'application/json' }, options.headers || {});
        if (options.method && !['GET', 'HEAD'].includes(options.method.toUpperCase())) {
            headers['X-CSRF-Token'] = csrfToken();
            if (options.body && typeof options.body === 'string' && !headers['Content-Type']) {
                headers['Content-Type'] = 'application/json';
            }
        }
        options.headers = headers;
        var res = await fetch(url, options);
        if (res.status === 204) return {};
        var data = await res.json().catch(function () { return {}; });
        if (!res.ok) {
            throw new Error(data.detail || data.error || ('Request failed (HTTP ' + res.status + ')'));
        }
        return data;
    }

    function notify(msg, isErr) {
        if (typeof window.showToast === 'function') {
            window.showToast(msg, isErr ? 'error' : 'success');
        } else {
            alert(msg);
        }
    }

    function esc(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    /* ---------------- 1. DICOM Tag Editor & Split/Merge ---------------- */
    var loadedStudy = null;

    async function loadStudyTags() {
        var uid = (document.getElementById('at_study_uid').value || '').trim();
        if (!uid) { notify('Enter a StudyInstanceUID first.', true); return; }
        try {
            var data = await apiJson('/api/v1/studies/' + encodeURIComponent(uid) + '/tags');
            loadedStudy = data;
            document.getElementById('at_patient_id').value = data.patient_id || '';
            document.getElementById('at_patient_name').value = data.patient_name || '';
            document.getElementById('at_patient_dob').value = data.patient_birth_date || '';
            document.getElementById('at_patient_sex').value = data.patient_sex || '';
            document.getElementById('at_accession').value = data.accession_number || '';
            document.getElementById('at_study_date').value = data.study_date || '';
            document.getElementById('at_study_desc').value = data.study_description || '';
            document.getElementById('at_ref_phys').value = data.referring_physician || '';
            renderSeriesList(data.series || []);
            notify('Loaded tags and ' + (data.series || []).length + ' series.');
        } catch (err) {
            notify(err.message, true);
        }
    }

    function renderSeriesList(series) {
        var host = document.getElementById('at_split_series_list');
        if (!host) return;
        if (!series.length) {
            host.innerHTML = '<div style="color:var(--text-muted);font-size:12px;">No series found for this study.</div>';
            return;
        }
        host.innerHTML = series.map(function (ser) {
            return '<label style="display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;cursor:pointer;">' +
                '<input type="checkbox" class="at-split-cb" value="' + esc(ser.series_instance_uid) + '">' +
                '<span style="font-size:12px;"><b>' + esc(ser.modality || 'OT') + ' #' + esc(ser.series_number || '-') + '</b> — ' +
                esc(ser.series_description || 'Untitled series') + ' (' + esc(ser.instance_count) + ' images)</span></label>';
        }).join('');
    }

    async function saveStudyTags() {
        var uid = (document.getElementById('at_study_uid').value || '').trim();
        if (!uid) { notify('Load a study first.', true); return; }
        var body = {
            patient_id: document.getElementById('at_patient_id').value.trim(),
            patient_name: document.getElementById('at_patient_name').value.trim(),
            patient_birth_date: document.getElementById('at_patient_dob').value.trim(),
            patient_sex: document.getElementById('at_patient_sex').value.trim(),
            accession_number: document.getElementById('at_accession').value.trim(),
            study_date: document.getElementById('at_study_date').value.trim(),
            study_description: document.getElementById('at_study_desc').value.trim(),
            referring_physician: document.getElementById('at_ref_phys').value.trim(),
        };
        try {
            var res = await apiJson('/api/v1/studies/' + encodeURIComponent(uid) + '/tags', {
                method: 'PATCH', body: JSON.stringify(body),
            });
            notify('Saved tags (' + res.instances_updated + ' DICOM files updated).');
        } catch (err) {
            notify(err.message, true);
        }
    }

    async function runStudySplit() {
        var uid = (document.getElementById('at_study_uid').value || '').trim();
        var checked = Array.from(document.querySelectorAll('.at-split-cb:checked')).map(function (c) { return c.value; });
        if (!uid || !checked.length) {
            notify('Load a study and select at least one series to split.', true);
            return;
        }
        try {
            var res = await apiJson('/api/v1/studies/split', {
                method: 'POST',
                body: JSON.stringify({
                    source_study_uid: uid,
                    series_instance_uids: checked,
                    target_patient_id: document.getElementById('at_split_pid').value.trim() || null,
                    target_patient_name: document.getElementById('at_split_pname').value.trim() || null,
                    new_study_description: document.getElementById('at_split_desc').value.trim() || null,
                }),
            });
            notify('Split complete -> New Study UID: ' + res.new_study_instance_uid);
            loadStudyTags();
        } catch (err) {
            notify(err.message, true);
        }
    }

    async function runStudyMerge() {
        var sources = (document.getElementById('at_merge_sources').value || '')
            .split(/[\n,]+/).map(function (s) { return s.trim(); }).filter(Boolean);
        var target = (document.getElementById('at_merge_target').value || '').trim();
        if (!sources.length || !target) {
            notify('Enter at least one source Study UID and a target Study UID.', true);
            return;
        }
        try {
            var res = await apiJson('/api/v1/studies/merge', {
                method: 'POST',
                body: JSON.stringify({ source_study_uids: sources, target_study_uid: target }),
            });
            notify('Merged into ' + res.target_study_instance_uid);
        } catch (err) {
            notify(err.message, true);
        }
    }

    /* ---------------- 2. Scheduled Backup & Verification ---------------- */
    async function loadBackupPanel() {
        try {
            var sched = await apiJson('/api/v1/db/backup-schedule');
            document.getElementById('bk_enabled').checked = Boolean(sched.enabled);
            document.getElementById('bk_time').value = sched.time || '02:00';
            document.getElementById('bk_retention').value = sched.retention_count || 7;
            var meta = document.getElementById('bk_last_run');
            if (meta) {
                meta.textContent = sched.last_run_date
                    ? ('Last scheduled run: ' + sched.last_run_date + ' (' + (sched.last_file || '') + ', verified=' + sched.last_verified + ')')
                    : 'No scheduled backup has run yet.';
            }
            var list = await apiJson('/api/v1/db/backups');
            renderBackups(list);
        } catch (err) {
            var body = document.getElementById('bk_table_body');
            if (body) body.innerHTML = '<tr><td colspan="4" style="color:var(--text-muted);">Admin privileges required or backup list unavailable.</td></tr>';
        }
    }

    function renderBackups(items) {
        var body = document.getElementById('bk_table_body');
        if (!body) return;
        if (!items || !items.length) {
            body.innerHTML = '<tr><td colspan="4" style="color:var(--text-muted);">No backup files yet.</td></tr>';
            return;
        }
        body.innerHTML = items.map(function (b) {
            var kb = Math.max(1, Math.round((b.size || 0) / 1024));
            var safe = esc(b.file);
            return '<tr><td><code>' + safe + '</code></td><td>' + kb + ' KB</td><td>' + esc(b.mtime || '') + '</td>' +
                '<td style="display:flex;gap:6px;flex-wrap:wrap;">' +
                '<button type="button" class="btn" onclick="window.PaxsArchiveTools.verifyBackup(\'' + safe + '\')">Verify</button>' +
                '<a class="btn" href="/api/v1/db/backups/' + encodeURIComponent(b.file) + '" download>Download</a>' +
                '<button type="button" class="btn" onclick="window.PaxsArchiveTools.deleteBackup(\'' + safe + '\')">Delete</button>' +
                '</td></tr>';
        }).join('');
    }

    async function saveBackupSchedule() {
        try {
            await apiJson('/api/v1/db/backup-schedule', {
                method: 'PUT',
                body: JSON.stringify({
                    enabled: document.getElementById('bk_enabled').checked,
                    time: document.getElementById('bk_time').value.trim() || '02:00',
                    retention_count: parseInt(document.getElementById('bk_retention').value, 10) || 7,
                }),
            });
            notify('Backup schedule saved.');
            loadBackupPanel();
        } catch (err) {
            notify(err.message, true);
        }
    }

    async function runBackupNow() {
        try {
            var res = await apiJson('/api/v1/db/backup', { method: 'POST' });
            notify('Backup created: ' + res.file);
            loadBackupPanel();
        } catch (err) {
            notify(err.message, true);
        }
    }

    async function verifyBackup(name) {
        try {
            var res = await apiJson('/api/v1/db/backups/' + encodeURIComponent(name) + '/verify', { method: 'POST' });
            if (res.ok) {
                notify('Verified OK (' + res.format + ', SHA-256: ' + (res.sha256 || '').slice(0, 16) + '...)');
            } else {
                notify('Verification FAILED: ' + (res.error || 'Corrupt archive'), true);
            }
        } catch (err) {
            notify(err.message, true);
        }
    }

    async function deleteBackup(name) {
        if (!confirm('Delete backup ' + name + '?')) return;
        try {
            await apiJson('/api/v1/db/backups/' + encodeURIComponent(name), { method: 'DELETE' });
            notify('Deleted ' + name);
            loadBackupPanel();
        } catch (err) {
            notify(err.message, true);
        }
    }

    /* ---------------- 3. HL7 / RIS & Modality Worklist (MWL) ---------------- */
    async function loadWorklistPanel() {
        try {
            var st = await apiJson('/api/v1/worklist/hl7-status');
            var badge = document.getElementById('hl7_status_summary');
            if (badge) {
                badge.textContent = 'HL7 MLLP: ' + (st.hl7_enabled ? ('ON (' + st.hl7_host + ':' + st.hl7_port + ')') : 'OFF') +
                    '  |  EMR ORU Write-back: ' + (st.emr_enabled ? ('ON (' + st.emr_host + ':' + st.emr_port + ')') : 'OFF');
            }
            var filter = document.getElementById('mwl_filter_status') ? document.getElementById('mwl_filter_status').value : '';
            var qs = filter ? ('?status=' + encodeURIComponent(filter)) : '';
            var steps = await apiJson('/api/v1/worklist' + qs);
            renderWorklist(steps);
        } catch (err) {
            notify(err.message, true);
        }
    }

    function renderWorklist(steps) {
        var body = document.getElementById('mwl_table_body');
        if (!body) return;
        if (!steps || !steps.length) {
            body.innerHTML = '<tr><td colspan="6" style="color:var(--text-muted);">No scheduled procedure steps found.</td></tr>';
            return;
        }
        body.innerHTML = steps.map(function (s) {
            return '<tr><td><code>' + esc(s.sps_id) + '</code></td>' +
                '<td><b>' + esc(s.patient_name || '') + '</b><br><span style="font-size:11px;color:var(--text-muted);">' + esc(s.patient_id) + '</span></td>' +
                '<td>' + esc(s.modality || '') + ' / ' + esc(s.scheduled_station_aet || '*') + '</td>' +
                '<td>' + esc(s.requested_procedure_description || s.accession_number || '-') + '</td>' +
                '<td><code>' + esc(s.status) + '</code></td>' +
                '<td style="display:flex;gap:6px;flex-wrap:wrap;">' +
                '<button type="button" class="btn" onclick="window.PaxsArchiveTools.setMwlStatus(' + s.id + ',\'IN PROGRESS\')">Start</button>' +
                '<button type="button" class="btn" onclick="window.PaxsArchiveTools.setMwlStatus(' + s.id + ',\'COMPLETED\')">Complete</button>' +
                '<button type="button" class="btn" onclick="window.PaxsArchiveTools.setMwlStatus(' + s.id + ',\'CANCELED\')">Cancel</button>' +
                '</td></tr>';
        }).join('');
    }

    async function createWorklistStep() {
        var pid = (document.getElementById('mwl_pid').value || '').trim();
        if (!pid) { notify('Patient ID is required.', true); return; }
        try {
            await apiJson('/api/v1/worklist', {
                method: 'POST',
                body: JSON.stringify({
                    patient_id: pid,
                    patient_name: document.getElementById('mwl_pname').value.trim() || null,
                    modality: (document.getElementById('mwl_mod').value || 'CT').trim().toUpperCase(),
                    scheduled_station_aet: document.getElementById('mwl_aet').value.trim() || null,
                    accession_number: document.getElementById('mwl_acc').value.trim() || null,
                    requested_procedure_description: document.getElementById('mwl_desc').value.trim() || null,
                }),
            });
            notify('Worklist exam scheduled.');
            loadWorklistPanel();
        } catch (err) {
            notify(err.message, true);
        }
    }

    async function setMwlStatus(stepId, status) {
        try {
            await apiJson('/api/v1/worklist/' + stepId, {
                method: 'PATCH',
                body: JSON.stringify({ status: status }),
            });
            notify('Worklist step updated -> ' + status);
            loadWorklistPanel();
        } catch (err) {
            notify(err.message, true);
        }
    }

    function refreshAll() {
        loadBackupPanel();
        loadWorklistPanel();
    }

    window.PaxsArchiveTools = {
        loadStudyTags: loadStudyTags,
        saveStudyTags: saveStudyTags,
        runStudySplit: runStudySplit,
        runStudyMerge: runStudyMerge,
        loadBackupPanel: loadBackupPanel,
        saveBackupSchedule: saveBackupSchedule,
        runBackupNow: runBackupNow,
        verifyBackup: verifyBackup,
        deleteBackup: deleteBackup,
        loadWorklistPanel: loadWorklistPanel,
        createWorklistStep: createWorklistStep,
        setMwlStatus: setMwlStatus,
        refreshAll: refreshAll,
    };

    var origSwitch = window.switchTab;
    window.switchTab = function (tabId) {
        if (typeof origSwitch === 'function') origSwitch(tabId);
        if (tabId === 'archive-tools-tab') refreshAll();
    };
})();
