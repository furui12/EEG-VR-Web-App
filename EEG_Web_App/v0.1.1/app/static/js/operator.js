/* VR EEG Studio — operator view.
   Home · Participants · Experiments · Reports, and the guided session:
   Participant → Prepare (electrodes + checklist) → Record (timeline, timer, notes) → Report. */
'use strict';

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];

// ------------------------------------------------------------------ helpers
async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    const r = await fetch(url, opts);
    let data = null;
    try { data = await r.json(); } catch (_) { /* empty body */ }
    if (!r.ok) {
        let msg = data && data.detail;
        if (Array.isArray(msg)) msg = msg.map(d => d.msg).join('; ');
        throw new Error(msg || `Request failed (${r.status})`);
    }
    return data;
}

function esc(v) {
    return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtTime(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    const mm = String(m).padStart(2, '0'), ss = String(s).padStart(2, '0');
    return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function fmtDur(sec) {
    sec = Math.round(sec || 0);
    if (!sec) return '—';
    if (sec < 60) return `${sec} s`;
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m >= 60) return `${Math.floor(m / 60)} h ${m % 60} min`;
    return s ? `${m} min ${s} s` : `${m} min`;
}

function fmtHours(h) {
    if (!h) return '0 min';
    if (h < 1) return `${Math.round(h * 60)} min`;
    return `${h.toFixed(1)} h`;
}

const pct = v => (v == null ? '—' : `${Math.round(v * 100)}%`);
const qClass = v => (v == null ? '' : v >= 0.8 ? 'good' : v >= 0.6 ? 'warn' : 'bad');
const qColor = v => (v == null ? 'var(--line)' : v >= 0.8 ? 'var(--good)' : v >= 0.6 ? 'var(--warn)' : 'var(--bad)');

function fmtDate(iso, withTime = true) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    if (sameDay) return withTime ? `Today ${time}` : 'Today';
    const date = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
    return withTime ? `${date} ${time}` : date;
}

function store(key, value) {
    try {
        if (value === undefined) return localStorage.getItem(key);
        localStorage.setItem(key, value);
    } catch (_) { /* storage unavailable */ }
    return null;
}

let toastTimer = null;
function toast(msg, error = false) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.toggle('error', error);
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, error ? 6000 : 3200);
}

function confirmBox(title, text, yes = 'Continue') {
    return new Promise(resolve => {
        $('#confirm-title').textContent = title;
        $('#confirm-text').textContent = text;
        $('#confirm-yes').textContent = yes;
        const modal = $('#confirm-modal');
        modal.hidden = false;
        const done = v => {
            modal.hidden = true;
            $('#confirm-yes').onclick = $('#confirm-no').onclick = null;
            resolve(v);
        };
        $('#confirm-yes').onclick = () => done(true);
        $('#confirm-no').onclick = () => done(false);
    });
}

const STATUS_TEXT = { in_progress: 'In progress', completed: 'Completed', aborted: 'Cancelled' };
const statusPill = s => `<span class="status st-${esc(s)}">${esc(STATUS_TEXT[s] || s)}</span>`;

function stepName(st, exp) {
    if (!st) return '—';
    if (st.kind === 'baseline') return st.baseline_type === 'eyes_closed' ? 'Baseline · eyes closed' : 'Baseline · eyes open';
    const n = exp ? exp.n_trials : null;
    return n ? `Trial ${st.number} of ${n}` : `Trial ${st.number}`;
}
const shortStep = st => (st.kind === 'baseline' ? 'Baseline' : `Trial ${st.number}`);

function planBar(e) {
    const parts = [];
    const total = (e.baseline_s || 0) + e.n_trials * (e.trial_duration_s || 120) + Math.max(0, e.n_trials - 1) * (e.rest_s || 0);
    const w = s => `${Math.max(4, (s / (total || 1)) * 100)}%`;
    if (e.baseline_s) parts.push(`<span class="b" style="width:${w(e.baseline_s)}" title="Baseline ${fmtDur(e.baseline_s)}"></span>`);
    for (let i = 0; i < e.n_trials; i++) {
        if (i && e.rest_s) parts.push(`<span class="r" style="width:${w(e.rest_s)}" title="Rest ${fmtDur(e.rest_s)}"></span>`);
        parts.push(`<span style="width:${w(e.trial_duration_s || 120)}" title="Trial ${i + 1}"></span>`);
    }
    return `<div class="plan-bar" aria-hidden="true">${parts.join('')}</div>`;
}

function planText(e) {
    const bits = [];
    if (e.baseline_s) bits.push(`${fmtDur(e.baseline_s)} baseline`);
    bits.push(`${e.n_trials} × ${e.trial_duration_s ? fmtDur(e.trial_duration_s) : 'manual'} trial${e.n_trials > 1 ? 's' : ''}`);
    if (e.rest_s && e.n_trials > 1) bits.push(`${fmtDur(e.rest_s)} rest`);
    return bits.join(' · ');
}

function totalMinutes(e) {
    const s = (e.baseline_s || 0) + e.n_trials * (e.trial_duration_s || 0) + Math.max(0, e.n_trials - 1) * (e.rest_s || 0);
    return s ? `≈ ${Math.round(s / 60)} min` : '';
}

// ------------------------------------------------------------------ state
const op = {
    view: 'home',
    experiments: [],
    subjects: [],
    session: null,          // _session_out of the open session
    phase: null,            // prepare | record | report
    stepIndex: null,        // selected plan step
    recording: false,
    trialId: null,
    busy: false,
    connected: false,
    receiving: false,
    markerStream: null,
    labels: [],
    nUsed: 5,
    quality: [],
    filtersText: '',
    restUntil: null,
    stopWait: 0,
    lastSaved: null,
    primeLog: true,
    seen: new Set(),
    selectedSubject: null,
    nsExperiment: null,
    editingExp: null,
    editingSubj: null,
};

// ------------------------------------------------------------------ views
function showView(name) {
    if (op.recording && name !== 'session') {
        toast('A recording is running. Stop it before leaving the session.', true);
        return;
    }
    if (name !== 'session' && op.view === 'session') leaveSessionView();
    op.view = name;
    $$('.view').forEach(v => v.classList.toggle('active', v.id === `view-${name}`));
    $$('.nav-link').forEach(b => b.classList.toggle('active', b.dataset.view === name));
    window.scrollTo(0, 0);
    if (name === 'home') loadHome();
    if (name === 'participants') loadParticipants();
    if (name === 'experiments') loadExperiments();
    if (name === 'reports') loadReports();
}

$$('.nav-link').forEach(b => b.addEventListener('click', () => showView(b.dataset.view)));
$$('[data-goto]').forEach(b => b.addEventListener('click', () => showView(b.dataset.goto)));
$('#logo').addEventListener('click', e => { e.preventDefault(); showView('home'); });
$('#to-researcher').addEventListener('click', e => {
    if (op.recording) { e.preventDefault(); toast('Stop the recording before switching to the researcher view.', true); }
});

function setPill(id, state, text) {
    const el = $(id);
    el.classList.remove('ok', 'warn');
    if (state) el.classList.add(state);
    if (text) $('.txt', el).textContent = text;
}

async function loadExperimentList() {
    op.experiments = await api('GET', '/api/experiments');
    return op.experiments;
}

// ================================================================== HOME
async function loadHome() {
    const now = new Date();
    const h = now.getHours();
    $('#home-date').textContent = now.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });
    $('#home-greeting').innerHTML = `${h < 12 ? 'Good morning' : h < 19 ? 'Good afternoon' : 'Good evening'}.<br>Shall we start a session?`;
    try {
        const [sum, exps] = await Promise.all([api('GET', '/api/operator/summary'), loadExperimentList()]);
        renderHome(sum, exps);
        checkDevices(exps, sum);
    } catch (e) {
        toast(e.message, true);
    }
}

function renderHome(sum, exps) {
    $('#k-today').textContent = sum.sessions_today;
    $('#k-today-sub').textContent = `${sum.completed_today} completed`;
    $('#k-week').textContent = sum.sessions_week;
    const u = $('#k-usable');
    u.textContent = pct(sum.usable_pct);
    u.className = `kpi-val ${qClass(sum.usable_pct)}`;
    $('#k-hours').textContent = fmtHours(sum.hours_recorded);
    $('#k-hours-sub').textContent = `${sum.participants} participant${sum.participants === 1 ? '' : 's'} in total`;

    // recording / recovered banners
    const rb = $('#home-recording');
    rb.hidden = !sum.is_recording;
    if (sum.is_recording) rb.innerHTML = '<strong>A recording is running.</strong> Click here to return to it.';
    const rec = $('#home-recovered');
    rec.hidden = !(sum.recovered && sum.recovered.length);
    if (!rec.hidden) rec.innerHTML = `<strong>${sum.recovered.length} interrupted trial(s) were recovered</strong> and saved as XDF in ${esc(sum.data_folder)}.`;

    // continue
    const cont = $('#home-continue');
    const open = sum.in_progress && sum.in_progress[0];
    cont.hidden = !open;
    if (open) {
        const nxt = open.next_step != null ? shortStep(open.plan[open.next_step]) : 'report';
        cont.innerHTML = `Continue ${esc(open.participant ? open.participant.name : '')}<small>${esc(open.experiment ? open.experiment.name : '')} · next: ${esc(nxt)}</small>`;
        cont.onclick = () => openSession(open.id);
    }

    // recent sessions
    const rows = [...(sum.in_progress || []), ...(sum.recent || [])].slice(0, 8);
    $('#home-recent-empty').hidden = rows.length > 0;
    $('#home-recent').innerHTML = rows.map(s => `
        <tr class="clickable" data-session="${s.id}">
            <td><strong>${esc(s.participant ? s.participant.name : '—')}</strong><div class="sub">${esc(fmtDate(s.started_at))}</div></td>
            <td>${esc(s.experiment ? s.experiment.name : '—')}</td>
            <td><div class="mini-bar"><div class="track"><span style="width:${s.total_steps ? (100 * s.completed_steps / s.total_steps) : 0}%;background:var(--action)"></span></div><span class="mono small">${s.completed_steps}/${s.total_steps}</span></div></td>
            <td><div class="mini-bar"><div class="track"><span style="width:${s.good_pct == null ? 0 : s.good_pct * 100}%;background:${qColor(s.good_pct)}"></span></div><span class="mono small">${pct(s.good_pct)}</span></div></td>
            <td>${statusPill(s.status)}</td>
        </tr>`).join('');
    $$('#home-recent tr').forEach(tr => tr.addEventListener('click', () => openSession(+tr.dataset.session)));

    // experiments column
    const byExp = Object.fromEntries((sum.by_experiment || []).map(b => [b.id, b]));
    $('#home-experiments').innerHTML = exps.length ? exps.map(e => {
        const b = byExp[e.id] || { sessions: 0, completed: 0 };
        const target = e.target_participants ? ` / ${e.target_participants}` : '';
        return `<div class="exp-card">
            <div class="top"><div><strong>${esc(e.name)}</strong><span class="sub">${esc(planText(e))}</span></div><span class="mono small">${b.completed}${target}</span></div>
            ${planBar(e)}
            <div class="actions"><button class="btn small primary" data-start="${e.id}">Start session</button></div>
        </div>`;
    }).join('') : '<div class="empty">No experiments yet. Create one in “Experiments”.</div>';
    $$('#home-experiments [data-start]').forEach(b => b.addEventListener('click', () => openNewSession(+b.dataset.start)));
}

$('#home-new').addEventListener('click', () => openNewSession());
$('#home-recording').addEventListener('click', resumeRecording);

async function checkDevices(exps, sum) {
    const icon = (id, state) => {
        const el = $(id);
        el.className = `check-icon ${state}`;
        el.textContent = state === 'ok' ? '✓' : state === 'warn' ? '!' : state === 'bad' ? '×' : '';
    };
    // disk
    const hrs = sum.disk_hours;
    icon('#chk-disk-icon', hrs == null ? 'warn' : hrs >= 4 ? 'ok' : hrs >= 1 ? 'warn' : 'bad');
    $('#chk-disk-text').textContent = hrs == null ? 'Could not read the free space' : `≈ ${hrs} h of recording free`;

    const e = exps[0];
    const params = new URLSearchParams(e ? {
        hardware_prop_name: e.hardware_prop_name, hardware_prop_value: e.hardware_prop_value,
        vr_prop_name: e.vr_prop_name, vr_prop_value: e.vr_prop_value,
    } : {});
    $('#chk-eeg-text').textContent = 'Checking…';
    $('#chk-vr-text').textContent = 'Checking…';
    icon('#chk-eeg-icon', '');
    icon('#chk-vr-icon', '');
    try {
        const r = await api('GET', `/api/streams/check?${params}`);
        if (r.eeg && r.eeg.found) {
            icon('#chk-eeg-icon', 'ok');
            $('#chk-eeg-title').textContent = `EEG headset · ${r.eeg.name || ''}`;
            $('#chk-eeg-text').textContent = `Sending data${r.eeg.channels ? ` · ${r.eeg.channels} channels` : ''}${r.eeg.rate ? ` at ${Math.round(r.eeg.rate)} Hz` : ''}`;
            setPill('#pill-eeg', 'ok');
        } else {
            icon('#chk-eeg-icon', 'bad');
            $('#chk-eeg-text').textContent = 'Not found: turn the headset on and open its LSL app.';
            setPill('#pill-eeg', null);
        }
        if (r.vr && r.vr.found) {
            icon('#chk-vr-icon', 'ok');
            $('#chk-vr-title').textContent = `VR game · ${r.vr.name || ''}`;
            $('#chk-vr-text').textContent = 'Markers stream found';
            setPill('#pill-vr', 'ok');
        } else {
            icon('#chk-vr-icon', 'warn');
            $('#chk-vr-text').textContent = 'Not found: open the VR game (you can still record EEG).';
            setPill('#pill-vr', null);
        }
    } catch (err) {
        icon('#chk-eeg-icon', 'warn');
        $('#chk-eeg-text').textContent = err.message;
    }
}

// ================================================================== PARTICIPANTS
async function loadParticipants() {
    try {
        op.subjects = await api('GET', '/api/subjects');
    } catch (e) { toast(e.message, true); return; }
    renderParticipants();
    if (op.selectedSubject) showParticipant(op.selectedSubject);
}

function renderParticipants() {
    const q = $('#p-search').value.trim().toLowerCase();
    const list = op.subjects.filter(s => !q || s.name.toLowerCase().includes(q));
    $('#p-empty').hidden = list.length > 0;
    $('#p-empty').textContent = op.subjects.length ? 'No participant matches the search.' : 'No participants yet.';
    $('#p-table').innerHTML = list.map(s => `
        <tr class="clickable${op.selectedSubject === s.id ? ' selected' : ''}" data-id="${s.id}">
            <td><strong>${esc(s.name)}</strong>${s.notes ? `<div class="sub">${esc(s.notes)}</div>` : ''}</td>
            <td>${s.age ?? '—'}</td>
            <td>${s.trial_count}</td>
            <td>${esc(fmtDate(s.last_trial_at, false))}</td>
        </tr>`).join('');
    $$('#p-table tr').forEach(tr => tr.addEventListener('click', () => {
        op.selectedSubject = +tr.dataset.id;
        renderParticipants();
        showParticipant(op.selectedSubject);
    }));
}

async function showParticipant(id) {
    const s = op.subjects.find(x => x.id === id);
    const box = $('#p-detail');
    if (!s) { box.innerHTML = '<div class="empty">Select a participant.</div>'; return; }
    box.innerHTML = '<div class="empty">Loading…</div>';
    let sessions = [];
    try { sessions = await api('GET', `/api/sessions?patient_id=${id}`); } catch (_) { /* ignore */ }
    box.innerHTML = `
        <div class="card-head"><h2>${esc(s.name)}</h2><button class="btn small" id="pd-edit">Edit</button></div>
        <p class="muted small">${[s.age != null ? `${s.age} years` : null, s.gender].filter(Boolean).map(esc).join(' · ') || 'No details'}${s.notes ? `<br>${esc(s.notes)}` : ''}</p>
        <button class="btn primary" id="pd-new">New session with ${esc(s.name)}</button>
        <h2 class="col-title">Sessions</h2>
        ${sessions.length ? `<ul class="session-list">${sessions.map(x => `
            <li><div><strong>${esc(x.experiment ? x.experiment.name : '—')}</strong>
                <span class="sub">${esc(fmtDate(x.started_at))} · ${x.completed_steps}/${x.total_steps} steps · ${pct(x.good_pct)} good signal</span></div>
                ${statusPill(x.status)}
                <button class="btn small" data-open="${x.id}">${x.status === 'in_progress' ? 'Continue' : 'Report'}</button></li>`).join('')}</ul>`
        : '<div class="empty">No sessions yet.</div>'}`;
    $('#pd-edit').onclick = () => openSubjectModal(s);
    $('#pd-new').onclick = () => openNewSession(null, s.id);
    $$('[data-open]', box).forEach(b => b.addEventListener('click', () => openSession(+b.dataset.open)));
}

$('#p-search').addEventListener('input', renderParticipants);
$('#p-add').addEventListener('click', () => openSubjectModal(null));

function openSubjectModal(s) {
    op.editingSubj = s;
    $('#subj-title').textContent = s ? `Edit ${s.name}` : 'New participant';
    $('#m-name').value = s ? s.name : '';
    $('#m-age').value = s && s.age != null ? s.age : '';
    $('#m-gender').value = s && s.gender ? s.gender : '';
    $('#m-notes').value = s && s.notes ? s.notes : '';
    $('#subj-modal').hidden = false;
    $('#m-name').focus();
}

$('#m-cancel').addEventListener('click', () => { $('#subj-modal').hidden = true; });
$('#subj-form').addEventListener('submit', async ev => {
    ev.preventDefault();
    const body = {
        name: $('#m-name').value.trim(),
        age: $('#m-age').value === '' ? null : +$('#m-age').value,
        gender: $('#m-gender').value || null,
        notes: $('#m-notes').value.trim() || null,
    };
    try {
        const s = op.editingSubj
            ? await api('PUT', `/api/subjects/${op.editingSubj.id}`, body)
            : await api('POST', '/api/subjects', body);
        $('#subj-modal').hidden = true;
        op.selectedSubject = s.id;
        toast('Participant saved');
        loadParticipants();
    } catch (e) { toast(e.message, true); }
});

// ================================================================== EXPERIMENTS
async function loadExperiments() {
    try { await loadExperimentList(); } catch (e) { toast(e.message, true); return; }
    $('#exp-grid').innerHTML = op.experiments.length ? op.experiments.map(e => `
        <div class="exp-card">
            <div class="top"><div><strong>${esc(e.name)}</strong><span class="sub">${esc(e.description || '')}</span></div><span class="mono small">${totalMinutes(e)}</span></div>
            ${planBar(e)}
            <span class="sub">${esc(planText(e))}${e.baseline_s ? ` · ${e.baseline_type === 'eyes_closed' ? 'eyes closed' : 'eyes open'}` : ''}</span>
            <span class="sub">Channels: ${esc((e.channel_labels.length ? e.channel_labels : [`first ${e.channels_used}`]).join(', '))}</span>
            <span class="sub">Streams: ${esc(e.hardware_prop_value)} + ${esc(e.vr_prop_value || '—')} · ${esc(e.filters_text || 'no filters')}</span>
            <span class="sub">${e.sessions} session${e.sessions === 1 ? '' : 's'}${e.target_participants ? ` · target ${e.target_participants} participants` : ''}</span>
            <div class="actions"><button class="btn small" data-edit="${e.id}">Edit</button><button class="btn small primary" data-start="${e.id}">Start session</button></div>
        </div>`).join('') : '<div class="empty">No experiments yet.</div>';
    $$('#exp-grid [data-edit]').forEach(b => b.addEventListener('click', () => openExperimentModal(op.experiments.find(e => e.id === +b.dataset.edit))));
    $$('#exp-grid [data-start]').forEach(b => b.addEventListener('click', () => openNewSession(+b.dataset.start)));
}

$('#exp-add').addEventListener('click', () => openExperimentModal(null));

const DEFAULT_LABELS = ['Fp1', 'Fp2', 'F3', 'Fz', 'F4'];
const DEFAULT_CHECK = ['Consent form signed', 'EEG gel applied and electrodes seated', 'Participant briefed on the task', 'VR headset fitted and game open'];

function openExperimentModal(e) {
    op.editingExp = e;
    $('#exp-title').textContent = e ? `Edit ${e.name}` : 'New experiment';
    const v = e || {
        name: '', description: '', baseline_s: 60, baseline_type: 'eyes_open', n_trials: 3, trial_duration_s: 300, rest_s: 30,
        target_participants: null, instructions: '', checklist: DEFAULT_CHECK, channels_used: 5, channel_labels: DEFAULT_LABELS,
        hardware_prop_value: 'AURA', vr_prop_value: '', filters: ['notch', 'bp'], filter_params: { notch_freq: 60, locut: 0.5, hicut: 40 },
    };
    $('#e-name').value = v.name;
    $('#e-desc').value = v.description || '';
    const bsel = $('#e-baseline');
    if (![...bsel.options].some(o => +o.value === (v.baseline_s || 0))) bsel.add(new Option(fmtDur(v.baseline_s), v.baseline_s));
    bsel.value = String(v.baseline_s || 0);
    $('#e-btype').value = v.baseline_type || 'eyes_open';
    $('#e-trials').value = v.n_trials;
    $('#e-duration').value = v.trial_duration_s ? +(v.trial_duration_s / 60).toFixed(2) : 0;
    $('#e-rest').value = v.rest_s || 0;
    $('#e-target').value = v.target_participants || '';
    $('#e-instr').value = v.instructions || '';
    $('#e-check').value = (v.checklist || []).join('\n');
    $('#e-nch').value = v.channels_used || 5;
    $('#e-labels').value = (v.channel_labels || []).join(', ');
    $('#e-eeg').value = v.hardware_prop_value || 'AURA';
    $('#e-vr').value = v.vr_prop_value || '';
    const f = v.filters || [], p = v.filter_params || {};
    $('#ef-notch').checked = f.includes('notch');
    $('#ef-notch-freq').value = String(Math.round(p.notch_freq || 60));
    $('#ef-bp').checked = f.includes('bp');
    $('#ef-lo').value = p.locut ?? 0.5;
    $('#ef-hi').value = p.hicut ?? 40;
    $('#ef-bl').checked = f.includes('bl');
    $('#ef-asr').checked = f.includes('asr');
    $('#ef-wvlt').checked = f.includes('wvlt');
    $('#e-archive').hidden = !e;
    $('#exp-modal').hidden = false;
    $('#e-name').focus();
}

$('#e-cancel').addEventListener('click', () => { $('#exp-modal').hidden = true; });
$('#e-archive').addEventListener('click', async () => {
    const e = op.editingExp;
    if (!e || !(await confirmBox(`Archive “${e.name}”?`, 'It will no longer be offered for new sessions. Recorded sessions and files are kept.', 'Archive'))) return;
    try {
        await api('DELETE', `/api/experiments/${e.id}`);
        $('#exp-modal').hidden = true;
        toast('Experiment archived');
        loadExperiments();
    } catch (err) { toast(err.message, true); }
});

$('#exp-form').addEventListener('submit', async ev => {
    ev.preventDefault();
    const nch = Math.max(1, Math.min(64, +$('#e-nch').value || 5));
    const filters = [['ef-notch', 'notch'], ['ef-bp', 'bp'], ['ef-bl', 'bl'], ['ef-asr', 'asr'], ['ef-wvlt', 'wvlt']]
        .filter(([id]) => $(`#${id}`).checked).map(([, k]) => k);
    const body = {
        name: $('#e-name').value.trim(),
        description: $('#e-desc').value.trim() || null,
        baseline_s: +$('#e-baseline').value || 0,
        baseline_type: $('#e-btype').value,
        n_trials: Math.max(1, +$('#e-trials').value || 1),
        trial_duration_s: Math.round((+$('#e-duration').value || 0) * 60),
        rest_s: Math.max(0, +$('#e-rest').value || 0),
        target_participants: $('#e-target').value ? +$('#e-target').value : null,
        instructions: $('#e-instr').value.trim() || null,
        checklist: $('#e-check').value.split('\n').map(x => x.trim()).filter(Boolean),
        channels_used: nch,
        channel_labels: $('#e-labels').value.split(',').map(x => x.trim()).filter(Boolean).slice(0, nch),
        hardware_prop_name: 'name',
        hardware_prop_value: $('#e-eeg').value.trim() || 'AURA',
        vr_prop_name: 'name',
        vr_prop_value: $('#e-vr').value.trim(),
        filters,
        filter_params: { notch_freq: +$('#ef-notch-freq').value, locut: +$('#ef-lo').value || 0.5, hicut: +$('#ef-hi').value || 40 },
    };
    try {
        if (op.editingExp) await api('PUT', `/api/experiments/${op.editingExp.id}`, body);
        else await api('POST', '/api/experiments', body);
        $('#exp-modal').hidden = true;
        toast('Experiment saved');
        if (op.view === 'experiments') loadExperiments(); else loadHome();
    } catch (e) { toast(e.message, true); }
});

// ================================================================== REPORTS (lab statistics)
async function loadReports() {
    try { await loadExperimentList(); } catch (e) { toast(e.message, true); return; }
    const sel = $('#st-exp');
    const keep = sel.value;
    sel.innerHTML = op.experiments.map(e => `<option value="${e.id}">${esc(e.name)}</option>`).join('') + '<option value="">All experiments</option>';
    if (keep !== '' && [...sel.options].some(o => o.value === keep)) sel.value = keep;
    else if (keep === '' && sel.dataset.touched) sel.value = '';
    renderStats();
}

$('#st-exp').addEventListener('change', () => { $('#st-exp').dataset.touched = '1'; renderStats(); });
$('#st-weeks').addEventListener('change', renderStats);

async function renderStats() {
    const expId = $('#st-exp').value;
    const weeks = +$('#st-weeks').value || 8;
    let s;
    try {
        s = await api('GET', `/api/stats?weeks=${weeks}${expId ? `&experiment_id=${expId}` : ''}`);
    } catch (e) { toast(e.message, true); return; }

    const exp = s.experiment;
    $('#st-sub').textContent = exp
        ? `${exp.name} · ${planText(exp)}${exp.target_participants ? ` · target ${exp.target_participants} participants` : ''}`
        : 'All experiments together';
    const exportBtn = $('#st-export');
    exportBtn.href = expId ? `/api/experiments/${expId}/dataset.csv` : '#';
    exportBtn.classList.toggle('disabled', !expId);
    exportBtn.title = expId ? 'One row per step and session, ready for R / Python / SPSS' : 'Choose one experiment to export its dataset';

    // KPIs
    $('#st-recruit').textContent = s.target ? `${s.participants_done} / ${s.target}` : String(s.participants_done);
    $('#st-recruit-bar').style.width = s.target ? `${Math.min(100, 100 * s.participants_done / s.target)}%` : '0%';
    $('#st-complete').textContent = s.completed;
    $('#st-complete-sub').textContent = s.completion_pct == null ? 'no finished sessions yet'
        : `${pct(s.completion_pct)} of finished sessions · ${s.aborted} cancelled`;
    const u = $('#st-usable');
    u.textContent = pct(s.usable_pct);
    u.className = `kpi-val ${qClass(s.usable_pct)}`;
    $('#st-prep').textContent = s.prep_median_s == null ? '—' : `${fmtTime(s.prep_median_s).replace(/^00:/, '0:')} min`;

    // sessions per week
    const maxW = Math.max(1, ...s.weeks.map(w => w.n));
    const cols = `repeat(${s.weeks.length}, minmax(0, 1fr))`;
    const chart = $('#st-weeks-chart');
    chart.style.gridTemplateColumns = cols;
    chart.style.gap = s.weeks.length > 12 ? '3px' : '12px';
    chart.innerHTML = s.weeks.map(w => `<div class="week-col${w.current ? ' current' : ''}" title="${esc(w.label)}: ${w.n} session(s)">${s.weeks.length <= 12 ? `<b>${w.n}</b>` : ''}<span style="height:${(w.n / maxW) * 85}%"></span></div>`).join('');
    const labels = $('#st-weeks-labels');
    labels.style.gridTemplateColumns = cols;
    labels.style.gap = chart.style.gap;
    const every = Math.ceil(s.weeks.length / 8);
    labels.innerHTML = s.weeks.map((w, i) => `<span>${(i % every === 0 || w.current) ? esc(w.current ? 'This wk' : w.label) : ''}</span>`).join('');

    // electrode quality
    const ch = s.channels || [];
    $('#st-channels').innerHTML = ch.length ? ch.map(c => `
        <div class="ch-bar"><strong>${esc(c.label)}</strong><div class="track"><span style="width:${c.good_pct * 100}%;background:${qColor(c.good_pct)}"></span></div><span class="v">${pct(c.good_pct)}</span></div>`).join('')
        : '<div class="empty">No recorded steps yet.</div>';
    const worst = ch.slice().sort((a, b) => a.good_pct - b.good_pct)[0];
    const note = $('#st-channels-note');
    note.hidden = !(worst && worst.good_pct < 0.7);
    if (!note.hidden) note.textContent = `${worst.label} is often not in good condition (${pct(worst.good_pct)} of the time). Check that electrode and its gel before the next sessions.`;

    // theta/alpha distribution
    const dist = s.distribution || [];
    const all = dist.flatMap(d => d.values);
    const box = $('#st-dist');
    if (!all.length) {
        box.style.gridTemplateColumns = '1fr';
        box.innerHTML = '<div class="empty">The distribution appears when there are completed sessions.</div>';
    } else {
        const max = Math.max(...all) * 1.1 || 1;
        box.style.gridTemplateColumns = `44px repeat(${dist.length}, minmax(0, 1fr))`;
        const ticks = [max, max * 0.75, max * 0.5, max * 0.25, 0].map(v => `<span>${v.toFixed(2)}</span>`).join('');
        box.innerHTML = `<div class="dp-axis">${ticks}</div>` + dist.map(d => `
            <div class="dp-col" title="${esc(d.step)} · median ${d.median == null ? '—' : d.median.toFixed(2)} · n=${d.values.length}">
                ${d.values.map((v, i) => `<i style="left:${d.values.length > 1 ? 50 + (((i * 37) % 11) - 5) * 4 : 50}%;bottom:${(v / max) * 100}%"></i>`).join('')}
                ${d.median == null ? '' : `<b style="bottom:${(d.median / max) * 100}%"></b>`}
            </div>`).join('');
    }
    let lab = $('#st-dist-labels');
    if (!lab) {
        lab = document.createElement('div');
        lab.className = 'dp-labels';
        lab.id = 'st-dist-labels';
        box.after(lab);
    }
    lab.style.gridTemplateColumns = box.style.gridTemplateColumns;
    lab.innerHTML = all.length ? `<span></span>${dist.map(d => `<span>${esc(d.step)}</span>`).join('')}` : '';
    $('#st-dist-title').textContent = `Frontal θ/α per step${exp ? '' : ' (choose an experiment)'} · all participants`;

    // quick read
    const q = s.quick || {};
    const items = [];
    items.push(`<strong>${q.participants_with_data || 0}</strong> participant(s) with a completed session.`);
    if (q.participants_with_data) {
        items.push(`In <strong>${q.load_up} of ${q.participants_with_data}</strong>, frontal θ/α during the trials was higher than at the resting baseline.`);
        if (q.habituation) items.push(`<strong>${q.habituation}</strong> showed a lower value in the last trial than at their peak (possible habituation).`);
    }
    if (q.remaining != null) {
        const weeksLeft = q.per_week ? Math.ceil(q.remaining / q.per_week) : null;
        items.push(`<strong>${q.remaining}</strong> participant(s) to reach the target${weeksLeft ? `: about ${weeksLeft} week(s) at the current pace (${q.per_week}/week)` : ''}.`);
    }
    if (s.usable_pct != null) items.push(`Usable data so far: <strong>${pct(s.usable_pct)}</strong> of recorded time with good signal.`);
    $('#st-quick').innerHTML = items.map(i => `<li>${i}</li>`).join('');
}

// ================================================================== NEW SESSION
async function openNewSession(expId = null, subjectId = null) {
    try {
        const [subs] = await Promise.all([api('GET', '/api/subjects'), loadExperimentList()]);
        op.subjects = subs;
    } catch (e) { toast(e.message, true); return; }
    if (!op.experiments.length) {
        toast('Create an experiment first (Experiments → New experiment).', true);
        showView('experiments');
        return;
    }
    const sel = $('#ns-participant');
    sel.innerHTML = '<option value="">— Choose a participant —</option>' + op.subjects.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
    if (subjectId) sel.value = String(subjectId);
    $('#ns-new').hidden = op.subjects.length > 0;
    $('#ns-name').value = '';
    $('#ns-age').value = '';
    op.nsExperiment = expId || (op.experiments.length === 1 ? op.experiments[0].id : null);
    renderExpChoice();
    $('#ns-operator').value = store('vreeg.operator') || '';
    $('#ns-modal').hidden = false;
    (op.subjects.length ? sel : $('#ns-name')).focus();
}

function renderExpChoice() {
    $('#ns-experiments').innerHTML = op.experiments.map(e => `
        <button type="button" class="exp-option${op.nsExperiment === e.id ? ' selected' : ''}" data-id="${e.id}" aria-pressed="${op.nsExperiment === e.id}">
            <strong>${esc(e.name)}</strong><span class="sub">${esc(planText(e))} ${esc(totalMinutes(e))}</span>${planBar(e)}
        </button>`).join('');
    $$('#ns-experiments .exp-option').forEach(b => b.addEventListener('click', () => { op.nsExperiment = +b.dataset.id; renderExpChoice(); }));
}

$('#ns-add').addEventListener('click', () => {
    const box = $('#ns-new');
    box.hidden = !box.hidden;
    if (!box.hidden) { $('#ns-participant').value = ''; $('#ns-name').focus(); }
});
$('#ns-participant').addEventListener('change', () => { if ($('#ns-participant').value) $('#ns-new').hidden = true; });
$('#ns-cancel').addEventListener('click', () => { $('#ns-modal').hidden = true; });

$('#ns-form').addEventListener('submit', async ev => {
    ev.preventDefault();
    if (!op.nsExperiment) { toast('Choose an experiment.', true); return; }
    let pid = +$('#ns-participant').value || null;
    try {
        if (!pid) {
            const name = $('#ns-name').value.trim();
            if (!name) { toast('Choose a participant or type a new name or code.', true); return; }
            const s = await api('POST', '/api/subjects', { name, age: $('#ns-age').value === '' ? null : +$('#ns-age').value });
            pid = s.id;
        }
        const operator = $('#ns-operator').value.trim();
        store('vreeg.operator', operator);
        const ses = await api('POST', '/api/sessions', { experiment_id: op.nsExperiment, patient_id: pid, operator });
        $('#ns-modal').hidden = true;
        openSession(ses.id);
    } catch (e) { toast(e.message, true); }
});

// ================================================================== SESSION
let liveTimer = null, liveBusy = false;
let srate = 250, nextIndex = 0, buf = [], bufStart = 0, liveMarkers = [];

const exp = () => (op.session ? op.session.experiment : null);
const plan = () => (op.session ? op.session.plan : []);
const curStep = () => (op.stepIndex == null ? null : plan()[op.stepIndex]);

async function openSession(id, opts = {}) {
    let ses;
    try { ses = await api('GET', `/api/sessions/${id}`); } catch (e) { toast(e.message, true); return; }
    op.session = ses;
    op.recording = false;
    op.trialId = null;
    op.lastSaved = null;
    op.restUntil = null;
    op.primeLog = true;
    op.stepIndex = ses.next_step;
    $('#s-participant').textContent = ses.participant ? ses.participant.name : '—';
    $('#s-experiment').textContent = ses.experiment ? ses.experiment.name : '—';
    $('#s-operator').textContent = ses.operator || '—';
    $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-session'));
    $$('.nav-link').forEach(b => b.classList.remove('active'));
    op.view = 'session';
    window.scrollTo(0, 0);

    if (ses.status !== 'in_progress' || opts.phase === 'report') { setPhase('report'); return; }

    op.nUsed = ses.experiment ? ses.experiment.channels_used || 5 : 5;
    setPhase(ses.completed_steps > 0 || opts.phase === 'record' ? 'record' : 'prepare');
    await connectSession();

    // a trial of this session may still be running (e.g. the page was reloaded)
    try {
        const st = await api('GET', '/api/status');
        if (st.is_recording && st.trial && st.trial.session_id === ses.id) {
            op.recording = true;
            op.trialId = st.trial.id;
            const idx = plan().findIndex(p => p.kind === st.trial.kind && p.number === st.trial.trial_number);
            if (idx >= 0) op.stepIndex = idx;
            setPhase('record');
        }
    } catch (_) { /* ignore */ }
}

async function resumeRecording() {
    try {
        const st = await api('GET', '/api/status');
        if (st.is_recording && st.trial && st.trial.session_id) openSession(st.trial.session_id);
        else if (st.is_recording) toast('A recording started from the researcher view is running. Open the researcher view to stop it.', true);
        else loadHome();
    } catch (e) { toast(e.message, true); }
}

async function connectSession() {
    const s = op.session;
    $('#p-eeg-text').textContent = 'Connecting…';
    $('#p-vr-text').textContent = 'Connecting…';
    $('#p-eeg-dot').className = 'dot-lg';
    $('#p-vr-dot').className = 'dot-lg';
    try {
        const info = await api('POST', `/api/sessions/${s.id}/connect`);
        op.connected = true;
        op.markerStream = info.marker_stream;
        op.labels = info.channel_labels || [];
        op.nUsed = Math.min(info.channels_used || op.nUsed, info.channels || op.nUsed) || op.nUsed;
        op.filtersText = info.filters_text || '';
        srate = info.srate || srate;
        op.eegText = `${info.eeg_stream} · ${info.channels} channels · ${Math.round(info.srate)} Hz${info.decoding ? ' · ' + info.decoding : ''}`;
        $('#p-eeg-text').textContent = op.eegText;
        $('#p-vr-dot').className = `dot-lg ${info.marker_stream ? 'ok' : 'warn'}`;
        $('#p-vr-text').textContent = info.marker_stream ? info.marker_stream
            : `“${exp().vr_prop_value || '—'}” not found. Open the game, or start anyway (no VR events).`;
        setPill('#pill-vr', info.marker_stream ? 'ok' : null);
        $('#p-filters').textContent = `Filters applied automatically: ${op.filtersText || 'none'}.`;
        $('#p-channels-note').textContent = `Showing the first ${op.nUsed} channels of the ${info.eeg_stream} stream (${info.channels} in total). All channels are still saved in the XDF file.`;
        startLive();
    } catch (e) {
        op.connected = false;
        op.receiving = false;
        op.eegText = '';
        $('#p-eeg-dot').className = 'dot-lg';
        $('#p-eeg-text').textContent = `${e.message} Turn the headset on and press “Connect again”.`;
        $('#p-vr-text').textContent = '—';
        setPill('#pill-eeg', null);
        renderPhase();
    }
}

$('#p-reconnect').addEventListener('click', () => { if (!op.recording) connectSession(); });

function leaveSessionView() {
    stopLive();
    op.session = null;
    op.phase = null;
    $('#rec-strip').hidden = true;
}

$('#s-leave').addEventListener('click', () => {
    if (op.recording) { toast('Stop the recording before leaving the session.', true); return; }
    showView('home');
});

function setPhase(phase) {
    op.phase = phase;
    $('#phase-prepare').hidden = phase !== 'prepare';
    $('#phase-record').hidden = phase !== 'record';
    $('#phase-report').hidden = phase !== 'report';
    const order = ['participant', 'prepare', 'record', 'report'];
    const at = order.indexOf(phase);
    $$('#s-stepper li').forEach(li => {
        const i = order.indexOf(li.dataset.phase);
        li.classList.toggle('done', i < at);
        li.classList.toggle('current', i === at);
    });
    if (phase === 'report') { stopLive(); loadReport(); }
    renderPhase();
    window.scrollTo(0, 0);
}

$$('#s-stepper li').forEach(li => li.addEventListener('click', () => {
    if (!op.session || op.recording) return;
    const p = li.dataset.phase;
    if (p === 'participant') return;
    if (p === 'report') { if (op.session.completed_steps > 0 || op.session.status !== 'in_progress') setPhase('report'); return; }
    if (op.session.status !== 'in_progress') return;
    setPhase(p);
    if (!liveTimer) connectSession();
}));

function renderPhase() {
    if (op.phase === 'prepare') renderPrepare();
    if (op.phase === 'record') renderRecord();
    renderRecStrip();
}

// ------------------------------------------------------------------ live data
function startLive() {
    stopLive();
    nextIndex = 0; buf = []; bufStart = 0;
    liveTimer = setInterval(pollLive, 500);
    pollLive();
}

function stopLive() {
    if (liveTimer) clearInterval(liveTimer);
    liveTimer = null;
}

function plotTarget() {
    if (op.phase === 'prepare' && $('#p-signal').open) return '#p-plot';
    if (op.phase === 'record' && $('#r-plot').closest('details').open) return '#r-plot';
    return null;
}

async function pollLive() {
    if (liveBusy || !op.session) return;
    liveBusy = true;
    try {
        const target = plotTarget();
        const d = await api('GET', `/api/data/live?since=${target ? nextIndex : 1e12}`);
        srate = d.srate || srate;
        op.receiving = d.receiving;
        op.labels = d.channel_labels || op.labels;
        op.quality = (d.quality || []).slice(0, op.nUsed);
        op.elapsed = d.elapsed || 0;
        op.remaining = d.remaining;
        setPill('#pill-eeg', d.receiving ? 'ok' : (d.connected ? 'warn' : null));
        $('#p-eeg-dot').className = `dot-lg ${d.receiving ? 'ok' : d.connected ? 'warn' : ''}`;
        if (d.analysing && !d.receiving) $('#p-eeg-text').textContent = 'Analysing the stream format…';
        else if (op.eegText) $('#p-eeg-text').textContent = op.eegText + (d.receiving ? '' : ' · no data arriving');
        liveMarkers = d.markers || [];
        logEvents();

        if (op.recording && !d.is_recording && !op.busy) {
            // a timed trial ended on the server
            op.busy = true;
            try {
                const last = await api('GET', '/api/recording/last');
                if (last && last.trial && last.trial.id === op.trialId) await onStepSaved(last);
                else if (++op.stopWait > 30) {      // the server is still saving: give it up to ~15 s
                    op.recording = false;
                    op.stopWait = 0;
                    await refreshSession();
                    op.stepIndex = op.session.next_step;
                }
            } finally { op.busy = false; }
        }
        renderPhase();

        if (target && d.samples && d.samples.length) {
            if (d.start_index !== bufStart + buf.length) { buf = []; bufStart = d.start_index; }
            for (const r of d.samples) buf.push(r);
            nextIndex = d.next_index;
            const w = Math.round(4 * srate);
            if (buf.length > w) { bufStart += buf.length - w; buf = buf.slice(buf.length - w); }
            renderPlot(target);
        }
    } catch (e) {
        console.error(e);
    } finally {
        liveBusy = false;
    }
}

const PLOT_COLORS = ['#0B5E6B', '#E08A3C', '#2F7D5B', '#7B6FC4', '#1C5D99', '#9A4A12', '#B4501A', '#5B6570'];
function renderPlot(target) {
    if (!buf.length || typeof Plotly === 'undefined') return;
    const n = Math.min(op.nUsed, buf[0].length);
    const x = buf.map((_, i) => (bufStart + i) / srate);
    const traces = [], layout = {
        grid: { rows: n, columns: 1, pattern: 'independent', ygap: 0.12 },
        margin: { t: 6, r: 6, b: 22, l: 46 }, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        showlegend: false, font: { size: 11, color: '#5B6570', family: 'IBM Plex Sans, sans-serif' }, shapes: [],
    };
    for (let c = 0; c < n; c++) {
        const sfx = c === 0 ? '' : `${c + 1}`;
        traces.push({ x, y: buf.map(r => r[c]), type: 'scattergl', mode: 'lines', line: { width: 1, color: PLOT_COLORS[c % 8] },
            xaxis: `x${sfx}`, yaxis: `y${sfx}`, hoverinfo: 'skip' });
        layout[`xaxis${sfx}`] = { range: [x[0], x[x.length - 1]], showticklabels: c === n - 1, zeroline: false, gridcolor: '#ECEEEA' };
        layout[`yaxis${sfx}`] = { title: { text: op.labels[c] || `${c + 1}`, standoff: 2 }, showticklabels: false, zeroline: false, gridcolor: '#ECEEEA' };
        for (const m of liveMarkers) {
            const mx = m.index / srate;
            if (mx < x[0] || mx > x[x.length - 1]) continue;
            layout.shapes.push({ type: 'line', xref: `x${sfx}`, yref: `y${sfx} domain`, x0: mx, x1: mx, y0: 0, y1: 1,
                line: { color: m.kind === 'note' ? '#1C5D99' : '#9A4A12', width: 1, dash: 'dot' } });
        }
    }
    const div = $(target);
    div.style.height = `${Math.max(240, n * 56)}px`;
    Plotly.react(div, traces, layout, { displayModeBar: false, responsive: true });
}

// ------------------------------------------------------------------ prepare: head map
// approximate 2-D positions of the 10-20 / 10-10 system (x right, y towards the back; head radius = 1)
const ROW_Y = { FP: -0.78, AF: -0.6, F: -0.4, FC: -0.2, FT: -0.2, C: 0, T: 0, CP: 0.2, TP: 0.2, P: 0.4, PO: 0.6, O: 0.78, I: 0.92 };
const OLD_NAMES = { T3: 'T7', T4: 'T8', T5: 'P7', T6: 'P8' };

function electrodePos(label) {
    let l = String(label || '').trim().toUpperCase().replace(/^EEG[\s_-]*/, '');
    l = OLD_NAMES[l] || l;
    const m = l.match(/^(FP|AF|FC|FT|CP|TP|PO|F|C|T|P|O|I)(Z|\d{1,2})$/);
    if (!m) return null;
    const row = m[1], y = ROW_Y[row];
    if (m[2] === 'Z') return { x: 0, y };
    const k = +m[2], side = k % 2 ? -1 : 1, idx = Math.ceil(k / 2);
    let x;
    if (row === 'FP' || row === 'O') x = 0.27 * Math.min(idx, 1.6);
    else if (row === 'T' || row === 'FT' || row === 'TP') x = 0.8 * Math.sqrt(Math.max(0, 1 - (y / 0.82) ** 2));
    else x = Math.min(idx * 0.2, 0.8) * Math.sqrt(Math.max(0, 1 - (y / 0.82) ** 2)) * 1.0;
    return { x: side * x, y };
}

const Q_TEXT = { good: 'Good', warn: 'Check', bad: 'Fix', flat: 'No signal', unknown: 'Waiting' };
const Q_FILL = { good: '#2F7D5B', warn: '#D99A1E', bad: '#B4501A', flat: '#8A949E', unknown: '#C9CEC6' };

function renderHeadmap() {
    const n = op.nUsed;
    const R = 112, cx = 130, cy = 136;
    const labels = Array.from({ length: n }, (_, i) => op.labels[i] || `Ch ${i + 1}`);
    let unknown = 0;
    const pts = labels.map((lab, i) => {
        const p = electrodePos(lab);
        if (p) return p;
        const j = unknown++;                                     // unknown label: spread along the forehead
        const x = n > 1 ? -0.55 + (1.1 * j) / (n - 1) : 0;
        return { x, y: -0.62 + 0.35 * x * x };
    });
    const ref = ['F7', 'F8', 'T7', 'C3', 'CZ', 'C4', 'T8', 'P7', 'P3', 'PZ', 'P4', 'P8', 'O1', 'O2']
        .filter(r => !labels.some(l => l.toUpperCase() === r))
        .map(r => electrodePos(r))
        .map(p => `<circle cx="${cx + p.x * R}" cy="${cy + p.y * R}" r="4" fill="#DDE1DA"></circle>`).join('');
    const dots = pts.map((p, i) => {
        const q = op.receiving ? (op.quality[i] || { level: 'unknown' }) : { level: 'unknown' };
        const fill = Q_FILL[q.level] || Q_FILL.unknown;
        const lab = labels[i];
        const tip = `${lab}: ${Q_TEXT[q.level] || '—'}${q.amp != null ? ` · ${q.amp} µV` : ''}${q.line != null ? ` · ${Math.round(q.line * 100)}% line noise` : ''}`;
        const ring = q.level === 'bad' || q.level === 'warn' ? `<circle cx="${cx + p.x * R}" cy="${cy + p.y * R}" r="24" fill="none" stroke="${fill}" stroke-width="2" opacity=".45"></circle>` : '';
        return `<g><title>${esc(tip)}</title>${ring}<circle cx="${cx + p.x * R}" cy="${cy + p.y * R}" r="18" fill="${fill}"></circle>
            <text x="${cx + p.x * R}" y="${cy + p.y * R + 4.5}" text-anchor="middle" font-family="IBM Plex Sans, sans-serif" font-size="12.5" font-weight="700" fill="#FFFFFF">${esc(lab.slice(0, 4))}</text></g>`;
    }).join('');
    $('#p-headmap').innerHTML = `
        <svg width="260" height="270" viewBox="0 0 260 270" role="img" aria-label="Head map of the ${n} frontal electrodes">
            <path d="M${cx - 14} ${cy - R + 2} L${cx} ${cy - R - 16} L${cx + 14} ${cy - R + 2}" fill="#FFFFFF" stroke="#17212B" stroke-width="2" stroke-linejoin="round"></path>
            <ellipse cx="${cx - R - 4}" cy="${cy}" rx="7" ry="20" fill="#FFFFFF" stroke="#17212B" stroke-width="2"></ellipse>
            <ellipse cx="${cx + R + 4}" cy="${cy}" rx="7" ry="20" fill="#FFFFFF" stroke="#17212B" stroke-width="2"></ellipse>
            <circle cx="${cx}" cy="${cy}" r="${R}" fill="#FFFFFF" stroke="#17212B" stroke-width="2"></circle>
            <path d="M${cx - R * 0.93} ${cy - R * 0.36} Q ${cx} ${cy - R * 0.62} ${cx + R * 0.93} ${cy - R * 0.36}" fill="none" stroke="#E7EAE4" stroke-width="1.5" stroke-dasharray="4 4"></path>
            ${ref}${dots}
            <text x="${cx}" y="${cy + R - 10}" text-anchor="middle" font-family="IBM Plex Sans, sans-serif" font-size="11" fill="#8A949E">back of the head</text>
        </svg>`;
}

const FIX_STEPS = {
    flat: ['Check that the electrode cable is plugged in.', 'Press the electrode gently against the scalp.', 'Add a little gel if it is dry.'],
    bad: ['Part the hair under the electrode.', 'Add gel and press gently for a few seconds.', 'Ask the participant to relax the jaw and forehead.'],
    warn: ['Press the electrode gently for a few seconds.', 'Ask the participant to stay still and relax the face.'],
    line: ['Move the headset away from chargers and power cables.', 'Check that the reference / ground electrode is seated.'],
};

function renderGuide() {
    const g = $('#p-guide');
    const n = op.nUsed;
    if (!op.connected || !op.receiving) {
        g.className = 'guide wait';
        g.innerHTML = `<div class="g-title">${op.connected ? 'Waiting for EEG data…' : 'The headset is not connected'}</div><span>${op.connected ? 'The signal appears here a few seconds after the headset starts sending.' : 'Turn on the headset and its LSL app, then press “Connect again”.'}</span>`;
        return;
    }
    const q = op.quality;
    const rank = { flat: 3, bad: 2, warn: 1, good: 0, unknown: -1 };
    let worst = -1;
    q.forEach((c, i) => { if (worst < 0 || rank[c.level] > rank[q[worst].level]) worst = i; });
    const good = q.filter(c => c.level === 'good').length;
    if (worst < 0 || q[worst].level === 'good' || q[worst].level === 'unknown') {
        g.className = 'guide ok';
        g.innerHTML = `<div class="g-title">✓ All ${n} electrodes look good</div><span>Ask the participant to stay relaxed. You can start when the checklist is complete.</span>`;
        return;
    }
    const c = q[worst];
    const lab = op.labels[worst] || `Ch ${worst + 1}`;
    const isLine = c.line != null && c.line >= 0.5 && c.level !== 'flat';
    const steps = isLine ? FIX_STEPS.line : FIX_STEPS[c.level];
    const head = c.level === 'flat' ? `No signal on ${lab}` : c.level === 'bad' ? `Fix ${lab}` : `Check ${lab}`;
    const why = c.level === 'flat' ? 'The electrode is not picking up anything.'
        : isLine ? `Electrical noise (${Math.round(c.line * 100)}% at the mains frequency).`
            : `Signal too large or unstable (${c.amp ?? '—'} µV).`;
    const others = q.filter((x, i) => i !== worst && x.level !== 'good').length;
    g.className = `guide ${c.level}`;
    g.innerHTML = `<div class="g-title">${esc(head)}</div><span>${esc(why)}</span><ol>${steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>
        <span class="small">${good} of ${n} good${others ? ` · ${others} more to check after this one` : ''}</span>`;
}

function renderChecklist() {
    const e = exp();
    const items = (e && e.checklist) || [];
    const done = new Set(op.session.checklist_done || []);
    const box = $('#p-checklist');
    if (box.dataset.sid !== String(op.session.id) || box.children.length !== items.length) {
        box.dataset.sid = op.session.id;
        box.innerHTML = items.length ? items.map((it, i) => `
            <label class="${done.has(it) ? 'checked' : ''}"><input type="checkbox" data-i="${i}" ${done.has(it) ? 'checked' : ''}><span>${esc(it)}</span></label>`).join('')
            : '<div class="empty">This experiment has no checklist.</div>';
        $$('input', box).forEach(cb => cb.addEventListener('change', async () => {
            const list = items.filter((_, i) => $(`input[data-i="${i}"]`, box).checked);
            cb.closest('label').classList.toggle('checked', cb.checked);
            op.session.checklist_done = list;
            renderPrepare();
            try { await api('PUT', `/api/sessions/${op.session.id}`, { checklist_done: list }); } catch (err) { toast(err.message, true); }
        }));
    }
    const n = items.filter(it => done.has(it)).length;
    $('#p-check-count').textContent = items.length ? `${n}/${items.length}` : '';
    return { total: items.length, done: n };
}

function renderPrepare() {
    if (!op.session) return;
    renderHeadmap();
    renderGuide();
    const ck = renderChecklist();
    const n = op.nUsed;
    const good = op.receiving ? op.quality.filter(c => c.level === 'good').length : 0;
    const gc = $('#p-good-count');
    gc.textContent = op.receiving ? `${good} / ${n} good` : '';
    gc.className = `q-count ${good === n ? 'good' : good >= n - 1 ? 'warn' : 'bad'}`;

    const st = curStep();
    const btn = $('#p-begin');
    btn.textContent = st ? `Start ${st.kind === 'baseline' ? 'baseline' : `trial ${st.number}`} →` : 'Go to recording →';
    const left = ck.total - ck.done;
    let title, text;
    if (!op.connected) { title = 'Headset not connected'; text = 'Connect the headset to continue.'; }
    else if (!op.receiving) { title = 'Waiting for the signal'; text = 'The EEG stream is connected but no data is arriving yet.'; }
    else if (left > 0) { title = `Complete the checklist (${left} left)`; text = 'Tick every item before starting.'; }
    else if (good < n) { title = 'Almost ready'; text = `${n - good} electrode(s) are not good yet. You can fix them or start anyway.`; }
    else { title = 'Ready to start'; text = st && st.kind === 'baseline' ? 'The baseline starts as soon as you press the button.' : 'The first step starts as soon as you press the button.'; }
    $('#p-ready-title').textContent = title;
    $('#p-ready-text').textContent = text;
    btn.disabled = !op.receiving || left > 0 || op.busy;
}

$('#p-begin').addEventListener('click', async () => {
    const n = op.nUsed;
    const bad = op.quality.filter(c => c.level === 'bad' || c.level === 'flat').length;
    if (bad && !(await confirmBox('Some electrodes are not good', `${bad} of ${n} electrodes still show “Fix” or “No signal”. That part of the data may not be usable. Start anyway?`, 'Start anyway'))) return;
    setPhase('record');
    if (curStep()) startStep();
});

// ------------------------------------------------------------------ record
function renderTimeline() {
    const e = exp();
    $('#r-timeline').innerHTML = plan().map((st, i) => {
        const live = op.recording && i === op.stepIndex;
        const cur = !op.recording && i === op.stepIndex;
        const stats = st.trial && st.trial.stats;
        let state = 'Pending';
        if (live) state = '● Recording';
        else if (st.done) state = `✓ ${stats && stats.good_pct != null ? pct(stats.good_pct) : 'Saved'}`;
        else if (cur) state = 'Next';
        let w = 0;
        if (live) {
            w = st.duration_s ? Math.min(100, 100 * (op.elapsed || 0) / st.duration_s) : 50;
        }
        const cls = [st.kind === 'baseline' ? 'baseline' : '', st.done && !live ? 'done' : '', cur ? 'current' : '', live ? 'live' : ''].join(' ');
        return `<li class="${cls}" data-i="${i}" title="${esc(stepName(st, e))}${st.attempts > 1 ? ` · ${st.attempts} attempts` : ''}">
            <div class="t-head"><span>${esc(shortStep(st))}</span><span class="mono small">${st.duration_s ? fmtTime(st.duration_s) : '—'}</span></div>
            <span class="t-state">${esc(state)}${st.attempts > 1 && !live ? ` <span class="muted small">×${st.attempts}</span>` : ''}</span>
            <div class="t-bar"><span${live ? ` style="width:${w}%"` : ''}></span></div>
        </li>`;
    }).join('');
    $$('#r-timeline li').forEach(li => li.addEventListener('click', () => {
        if (op.recording || op.busy) return;
        op.stepIndex = +li.dataset.i;
        renderRecord();
    }));
}

function baselineInstruction(st) {
    return st.baseline_type === 'eyes_closed'
        ? 'Close your eyes, relax and stay still until I tell you. Try not to move your face.'
        : 'Sit still, relax and look at a fixed point in front of you. Try not to blink too much.';
}

function renderRecord() {
    if (!op.session) return;
    renderTimeline();
    const e = exp();
    const st = curStep();
    const main = $('#r-main');
    const rest = $('#r-rest');
    const rep = $('#r-repeat');

    if (op.recording && st) {
        $('#r-kicker').textContent = 'Recording';
        $('#r-title').textContent = stepName(st, e);
        if (st.duration_s) {
            const remaining = op.remaining != null ? op.remaining : Math.max(0, st.duration_s - (op.elapsed || 0));
            $('#r-timer').textContent = fmtTime(remaining);
            $('#r-timer-sub').textContent = `remaining · stops and saves by itself`;
            $('#r-progress').style.width = `${Math.min(100, 100 * (op.elapsed || 0) / st.duration_s)}%`;
        } else {
            $('#r-timer').textContent = fmtTime(op.elapsed);
            $('#r-timer-sub').textContent = 'elapsed · press Stop when the task is finished';
            $('#r-progress').style.width = '0%';
        }
        rest.hidden = true;
        main.className = 'btn xl stop';
        main.innerHTML = `<span class="sq"></span>${st.duration_s ? 'Stop early and save' : 'Stop and save'}`;
        main.disabled = op.busy;
        rep.hidden = true;
        $('#r-saved').hidden = true;
    } else if (st) {
        const next = st;
        $('#r-kicker').textContent = next.done ? 'Selected step (already recorded)' : `Next · step ${op.stepIndex + 1} of ${plan().length}`;
        $('#r-title').textContent = stepName(next, e);
        $('#r-timer').textContent = next.duration_s ? fmtTime(next.duration_s) : '00:00';
        $('#r-timer-sub').textContent = next.duration_s ? 'planned length · stops by itself' : 'no fixed length · you stop it';
        $('#r-progress').style.width = '0%';
        const now = Date.now();
        if (op.restUntil) {
            rest.hidden = false;
            const left = (op.restUntil - now) / 1000;
            rest.classList.toggle('ready', left <= 0);
            rest.textContent = left > 0
                ? `Rest · ${fmtTime(left)} left. Let the participant relax; you can start earlier if they are ready.`
                : 'Rest finished. Start when the participant is ready.';
        } else rest.hidden = true;
        main.className = 'btn xl rec';
        main.innerHTML = `<span class="circle"></span>${next.done ? 'Record again' : 'Start'} ${next.kind === 'baseline' ? 'baseline' : `trial ${next.number}`}`;
        main.disabled = op.busy || !op.receiving;
        main.title = op.receiving ? '' : 'Waiting for EEG data';
        const prev = op.lastSaved && op.lastSaved.step;
        rep.hidden = !prev || (prev.kind === next.kind && prev.number === next.number);
        if (!rep.hidden) rep.textContent = `Repeat ${prev.kind === 'baseline' ? 'baseline' : `trial ${prev.number}`}`;
    } else {
        // every step recorded
        $('#r-kicker').textContent = 'Done';
        $('#r-title').textContent = 'All steps are recorded';
        $('#r-timer').textContent = fmtTime(op.session.recorded_s);
        $('#r-timer-sub').textContent = 'of EEG saved in this session';
        $('#r-progress').style.width = '100%';
        rest.hidden = true;
        main.className = 'btn xl primary';
        main.textContent = 'See the session report →';
        main.disabled = op.busy;
        const prev = op.lastSaved && op.lastSaved.step;
        rep.hidden = !prev;
        if (prev) rep.textContent = `Repeat ${prev.kind === 'baseline' ? 'baseline' : `trial ${prev.number}`}`;
    }
    $('#r-instruction').textContent = st
        ? (st.kind === 'baseline' ? baselineInstruction(st) : (e.instructions || 'Explain the task to the participant.'))
        : 'Thank the participant. You can remove the headset.';

    // saved line
    const sv = $('#r-saved');
    if (!op.recording && op.lastSaved) {
        const r = op.lastSaved.result;
        sv.hidden = false;
        if (r.saved) {
            const g = r.trial && r.trial.stats ? r.trial.stats.good_pct : null;
            sv.className = 'saved-line ok';
            sv.textContent = `✓ ${shortStep(op.lastSaved.step)} saved · ${fmtTime(r.duration_s)} · ${g == null ? '' : `${pct(g)} good signal · `}${r.markers} VR events${r.auto_stopped ? ' · stopped automatically' : ''}`;
        } else {
            sv.className = 'saved-line fail';
            sv.textContent = `✗ ${shortStep(op.lastSaved.step)} was not saved: ${r.error || 'unknown error'}`;
        }
    } else if (!op.recording) sv.hidden = true;

    // signal cells
    const n = op.nUsed;
    const q = op.receiving ? op.quality : [];
    $('#r-quality').style.gridTemplateColumns = `repeat(${Math.min(n, 5)}, minmax(0, 1fr))`;
    $('#r-quality').innerHTML = Array.from({ length: n }, (_, i) => {
        const c = q[i] || { level: 'unknown' };
        const lab = op.labels[i] || `Ch ${i + 1}`;
        return `<div class="q-cell ${c.level}" title="${esc(lab)}: ${esc(Q_TEXT[c.level] || '—')}">${esc(lab)}<div class="small">${esc(Q_TEXT[c.level] || '—')}</div></div>`;
    }).join('');
    const good = q.filter(c => c.level === 'good').length;
    const gc = $('#r-good-count');
    gc.textContent = op.receiving ? `${good} / ${n}` : 'no data';
    gc.className = `q-count ${!op.receiving ? 'bad' : good === n ? 'good' : good >= n - 1 ? 'warn' : 'bad'}`;

    // next-step note
    const after = st ? plan().slice(op.stepIndex + 1).find(p => !p.done) : null;
    $('#r-next-note').textContent = st
        ? (after ? `After this: ${shortStep(after)}${e.rest_s ? ` (rest ${fmtDur(e.rest_s)} first)` : ''}.` : 'This is the last step of the session.')
        : '';
    $$('.note-btn').forEach(b => { b.disabled = !op.recording; });
}

function renderRecStrip() {
    const strip = $('#rec-strip');
    strip.hidden = !op.recording;
    document.body.classList.toggle('is-recording', op.recording);
    if (!op.recording || !op.session) return;
    const st = curStep();
    $('#rec-title').textContent = `Recording · ${st ? shortStep(st) : ''}`;
    $('#rec-context').textContent = `${op.session.participant ? op.session.participant.name : ''} · ${exp() ? exp().name : ''} · ${fmtTime(op.elapsed)}`;
}

$('#r-main').addEventListener('click', () => {
    if (op.recording) stopStep();
    else if (curStep()) startStep();
    else setPhase('report');
});

$('#r-repeat').addEventListener('click', () => {
    const prev = op.lastSaved && op.lastSaved.step;
    if (!prev || op.recording) return;
    const idx = plan().findIndex(p => p.kind === prev.kind && p.number === prev.number);
    if (idx < 0) return;
    op.stepIndex = idx;
    op.restUntil = null;
    startStep();
});

async function startStep() {
    const st = curStep();
    if (!st || op.busy || op.recording) return;
    op.busy = true;
    renderRecord();
    try {
        const r = await api('POST', '/api/recording/start', {
            patient_id: op.session.participant.id,
            trial_number: st.number,
            session_id: op.session.id,
            kind: st.kind,
            duration_s: st.duration_s || null,
        });
        op.recording = true;
        op.trialId = r.trial_id;
        op.restUntil = null;
        op.elapsed = 0;
        op.remaining = st.duration_s || null;
        clearEvents();
        (r.warnings || []).forEach(w => toast(w, true));
    } catch (e) {
        toast(e.message, true);
    } finally {
        op.busy = false;
        renderPhase();
    }
}

async function stopStep() {
    if (!op.recording || op.busy) return;
    const st = curStep();
    if (st && st.duration_s && (op.remaining || 0) > 3) {
        if (!(await confirmBox('Stop this step early?', `${fmtTime(op.remaining)} are still left. The data recorded so far is saved; you can repeat the step afterwards.`, 'Stop and save'))) return;
    }
    op.busy = true;
    renderRecord();
    try {
        const r = await api('POST', '/api/recording/stop');
        await onStepSaved(r);
    } catch (e) {
        toast(e.message, true);
    } finally {
        op.busy = false;
        renderPhase();
    }
}

async function onStepSaved(result) {
    const step = curStep();
    op.stopWait = 0;
    op.recording = false;
    op.trialId = null;
    op.lastSaved = { step: step ? { kind: step.kind, number: step.number } : null, result };
    await refreshSession();
    const e = exp();
    if (result.saved) {
        toast(`${step ? shortStep(step) : 'Step'} saved`);
        op.stepIndex = op.session.next_step;
        op.restUntil = (op.stepIndex != null && e && e.rest_s) ? Date.now() + e.rest_s * 1000 : null;
    } else {
        toast(result.error || 'The step could not be saved.', true);
    }
    renderPhase();
}

async function refreshSession() {
    if (!op.session) return;
    try { op.session = await api('GET', `/api/sessions/${op.session.id}`); } catch (_) { /* keep the old copy */ }
}

$('#r-abort').addEventListener('click', async () => {
    if (!op.session) return;
    if (!(await confirmBox('Cancel this session?', 'The steps already recorded are kept, but the session is marked as cancelled.', 'Cancel session'))) return;
    try {
        if (op.recording) {
            const r = await api('POST', '/api/recording/stop');
            op.recording = false;
            if (!r.saved && r.error) toast(r.error, true);
        }
        await api('PUT', `/api/sessions/${op.session.id}`, { status: 'aborted' });
        toast('Session cancelled');
        showView('home');
    } catch (e) { toast(e.message, true); }
});

// ------------------------------------------------------------------ notes & events
async function addNote(label) {
    if (!op.recording) { toast('Notes are saved during a recording.', true); return; }
    try {
        await api('POST', '/api/notes', { label });
        const btn = $(`.note-btn[data-note="${CSS.escape(label)}"]`);
        if (btn) { btn.classList.add('flash'); setTimeout(() => btn.classList.remove('flash'), 400); }
    } catch (e) { toast(e.message, true); }
}

$$('.note-btn').forEach(b => b.addEventListener('click', () => addNote(b.dataset.note)));
$('#note-form').addEventListener('submit', ev => {
    ev.preventDefault();
    const v = $('#note-input').value.trim();
    if (!v) return;
    addNote(v);
    $('#note-input').value = '';
    $('#note-input').blur();
});

function clearEvents() {
    op.seen = new Set(liveMarkers.map(m => `${m.time}|${m.label}|${m.kind}`));
    $('#r-events').innerHTML = '<li class="muted">No events yet</li>';
}

function logEvents() {
    if (op.primeLog) {            // after (re)opening a session: don't replay buffered events
        liveMarkers.forEach(m => op.seen.add(`${m.time}|${m.label}|${m.kind}`));
        op.primeLog = false;
        return;
    }
    const list = $('#r-events');
    for (const m of liveMarkers) {
        const key = `${m.time}|${m.label}|${m.kind}`;
        if (op.seen.has(key)) continue;
        op.seen.add(key);
        if (!op.recording) continue;
        if (list.querySelector('.muted')) list.innerHTML = '';
        const li = document.createElement('li');
        li.innerHTML = `<span class="ev-t">${fmtTime(op.elapsed)}</span><span class="${m.kind === 'note' ? 'ev-note' : 'ev-vr'}">${m.kind === 'note' ? 'Note' : 'VR'}</span><span>${esc(m.label)}</span>`;
        list.prepend(li);
        while (list.children.length > 40) list.lastChild.remove();
    }
}

document.addEventListener('keydown', ev => {
    if (ev.key === 'Escape') {
        ['#ns-modal', '#exp-modal', '#subj-modal'].forEach(id => { $(id).hidden = true; });
        return;
    }
    if (op.view !== 'session' || op.phase !== 'record' || !op.recording) return;
    if (ev.target.closest('input, textarea, select') || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    const btn = $$('.note-btn').find(b => $('kbd', b) && $('kbd', b).textContent === ev.key);
    if (btn) { ev.preventDefault(); addNote(btn.dataset.note); return; }
    if (ev.key === 'n' || ev.key === 'N') { ev.preventDefault(); $('#note-input').focus(); }
});

// rest countdown / timer smoothness between polls
setInterval(() => {
    if (op.view === 'session' && op.phase === 'record' && !op.recording && op.restUntil) renderRecord();
}, 1000);

// ------------------------------------------------------------------ report
let report = null;

async function loadReport() {
    if (!op.session) return;
    try { report = await api('GET', `/api/sessions/${op.session.id}/report`); } catch (e) { toast(e.message, true); return; }
    renderReport();
}

function renderReport() {
    const r = report, t = r.totals;
    const st = $('#rep-status');
    st.textContent = r.status === 'completed' ? '✓ Session completed' : r.status === 'aborted' ? 'Session cancelled' : 'Session in progress';
    st.className = `pill st-${r.status}`;
    $('#rep-title').textContent = `${r.participant ? r.participant.name : '—'} · ${r.experiment ? r.experiment.name : '—'}`;
    $('#rep-meta').textContent = [fmtDate(r.started_at), r.operator ? `operator ${r.operator}` : null,
        t.session_s ? `session ${fmtDur(t.session_s)}` : null, t.prep_s != null ? `preparation ${fmtDur(t.prep_s)}` : null].filter(Boolean).join(' · ');
    $('#rep-csv').href = `/api/sessions/${r.id}/metrics.csv`;
    $('#rep-zip').href = `/api/sessions/${r.id}/files.zip`;

    const noteCount = Object.values(t.notes || {}).reduce((a, b) => a + b, 0);
    const kpis = [
        ['Steps recorded', `${t.steps_done} / ${t.steps_total}`, t.repeats ? `${t.repeats} repeated` : 'no repeats', ''],
        ['Usable data', pct(t.good_pct), 'time with good signal', qClass(t.good_pct)],
        ['EEG recorded', fmtDur(t.recorded_s), t.srate ? `${t.srate} Hz · ${r.channels.length} channels analysed` : '', ''],
        ['VR events', String(t.markers), 'markers from the game', ''],
        ['Annotations', String(noteCount), Object.entries(t.notes || {}).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(', ') || 'none', ''],
    ];
    $('#rep-kpis').innerHTML = kpis.map(([l, v, s, c]) => `<div class="kpi"><span class="kpi-label">${esc(l)}</span><span class="kpi-val ${c}">${esc(v)}</span><span class="kpi-sub">${esc(s)}</span></div>`).join('');

    // theta/alpha chart
    const steps = r.steps;
    const fl = r.frontal_label || 'Frontal', pl = r.parietal_label || 'Second channel';
    $('#rep-ta-legend').innerHTML = `<span class="lg fz"><span></span>${esc(fl)}</span><span class="lg pz"><span></span>${esc(pl)}</span>`;
    const vals = steps.flatMap(s => [s.ta_frontal, s.ta_parietal]).filter(v => v != null);
    const max = vals.length ? Math.max(...vals) * 1.15 : 1;
    const cols = `repeat(${steps.length || 1}, minmax(0, 1fr))`;
    const chart = $('#rep-ta');
    chart.style.gridTemplateColumns = cols;
    chart.setAttribute('aria-label', 'Theta/alpha ratio per step');
    const bar = (v, cls) => v == null ? '<div class="ta-bar none" title="no data"></div>'
        : `<div class="ta-bar ${cls}" style="height:${Math.max(6, (v / max) * 100)}%"><span>${v.toFixed(2)}</span></div>`;
    chart.innerHTML = steps.map(s => {
        const d = s.kind === 'baseline' ? '<span class="ta-delta base">reference</span>'
            : s.delta_frontal != null ? `<span class="ta-delta">${s.delta_frontal > 0 ? '+' : ''}${Math.round(s.delta_frontal)}%</span>` : '<span class="ta-delta base">—</span>';
        return `<div class="ta-group">${d}<div class="ta-bars">${bar(s.ta_frontal, 'fz')}${bar(s.ta_parietal, 'pz')}</div></div>`;
    }).join('');
    const labs = $('#rep-ta-labels');
    labs.style.gridTemplateColumns = cols;
    labs.innerHTML = steps.map(s => `<span>${esc(s.name)}</span>`).join('');
    $('#rep-ta-note').textContent = `θ/α = theta (4–8 Hz) power divided by alpha (8–13 Hz) power, averaged over each step. % = change of ${fl} against the resting baseline. Higher values usually go with more mental effort.`;

    $('#rep-plain').textContent = plainWords(r);

    // warnings
    const icon = { warn: '!', info: 'i', ok: '✓' };
    $('#rep-warnings').innerHTML = (r.warnings.length ? r.warnings : [{ level: 'info', text: 'No recorded steps yet.' }])
        .map(w => `<li><span class="wi ${w.level}">${icon[w.level] || 'i'}</span><span>${esc(w.text)}</span></li>`).join('');

    renderHeat(r);
    renderStepTable(r);

    $('#rep-notes').value = r.notes || '';
    const fin = $('#rep-finish');
    fin.textContent = r.status === 'in_progress' ? 'Finish session' : 'Save notes and go home';
    $('#rep-repeat').textContent = r.status === 'in_progress' ? (r.next_step != null ? 'Back to recording' : 'Record another step') : 'Reopen and record more';
}

function plainWords(r) {
    const t = r.totals;
    const recorded = r.steps.filter(s => s.trial && s.trial.stats);
    if (!recorded.length) return 'Nothing has been recorded in this session yet.';
    const parts = [];
    if (t.good_pct != null) {
        parts.push(t.good_pct >= 0.8 ? `The signal was good ${pct(t.good_pct)} of the time, so the data should be usable.`
            : t.good_pct >= 0.6 ? `The signal was good ${pct(t.good_pct)} of the time; check the marked periods before analysing.`
                : `The signal was good only ${pct(t.good_pct)} of the time; consider repeating the session.`);
    }
    const tasks = r.steps.filter(s => s.kind === 'task' && s.delta_frontal != null);
    if (tasks.length) {
        const top = tasks.reduce((a, b) => (b.delta_frontal > a.delta_frontal ? b : a));
        if (top.delta_frontal > 5) parts.push(`Compared with rest, the frontal cognitive-load index went up during the task (highest in ${top.name}, ${top.delta_frontal > 0 ? '+' : ''}${Math.round(top.delta_frontal)}%).`);
        else if (top.delta_frontal < -5) parts.push('The frontal cognitive-load index stayed below the resting level during the task.');
        else parts.push('The frontal cognitive-load index stayed close to the resting level during the task.');
        if (tasks.length >= 2) {
            const last = tasks[tasks.length - 1];
            if (last !== top && last.delta_frontal < top.delta_frontal - 5) parts.push(`It came down in ${last.name}, which may mean the participant got used to the task.`);
        }
    } else if (!r.steps.some(s => s.kind === 'baseline' && s.trial)) {
        parts.push('There is no baseline, so the index cannot be compared with rest.');
    }
    return parts.join(' ');
}

function renderHeat(r) {
    const labels = r.channels.length ? r.channels : Array.from({ length: op.nUsed }, (_, i) => `Ch ${i + 1}`);
    const recorded = r.steps.filter(s => s.trial && s.trial.stats && s.trial.stats.segments && s.trial.stats.segments.length);
    const box = $('#rep-heat');
    if (!recorded.length) { box.innerHTML = '<div class="empty">No recorded data yet.</div>'; return; }
    const head = `<div class="heat-row heat-head"><span></span><div class="heat-cells">${recorded.map((s, i) =>
        `${i ? '<span class="rest"></span>' : ''}<span style="flex:${s.trial.stats.segments.length} 1 0">${esc(s.name)}</span>`).join('')}</div></div>`;
    const rows = labels.map((lab, c) => `<div class="heat-row"><strong class="small">${esc(lab)}</strong><div class="heat-cells">${recorded.map((s, i) =>
        (i ? '<span class="rest" title="rest"></span>' : '') + s.trial.stats.segments.map((seg, k) =>
            `<span class="${seg[c] || 'warn'}" title="${esc(lab)} · ${esc(s.name)} · ${fmtTime(k * 30)}–${fmtTime((k + 1) * 30)}: ${esc(Q_TEXT[seg[c]] || '—')}"></span>`).join('')).join('')}</div></div>`).join('');
    box.innerHTML = head + rows;
}

function renderStepTable(r) {
    const rows = r.steps.map(s => {
        const tr = s.trial;
        const stats = tr && tr.stats;
        const delta = s.kind === 'baseline' ? 'reference' : s.delta_frontal == null ? '—' : `${s.delta_frontal > 0 ? '+' : ''}${Math.round(s.delta_frontal)}%`;
        return `<tr>
            <td><strong>${esc(s.name)}</strong>${s.attempts > 1 ? `<div class="sub">${s.attempts} attempts (last one kept)</div>` : ''}</td>
            <td>${tr ? esc(fmtDate(tr.started_at)) : '<span class="muted">not recorded</span>'}</td>
            <td class="mono">${tr ? fmtTime(tr.duration_s) : '—'}</td>
            <td>${stats ? `<div class="mini-bar"><div class="track"><span style="width:${(stats.good_pct || 0) * 100}%;background:${qColor(stats.good_pct)}"></span></div><span class="mono small">${pct(stats.good_pct)}</span></div>` : '—'}</td>
            <td class="mono">${s.ta_frontal == null ? '—' : s.ta_frontal.toFixed(2)}</td>
            <td class="mono">${s.ta_parietal == null ? '—' : s.ta_parietal.toFixed(2)}</td>
            <td class="mono">${delta}</td>
            <td class="mono">${tr ? tr.n_markers ?? 0 : '—'}</td>
            <td class="mono">${tr ? tr.n_notes ?? 0 : '—'}</td>
            <td>${tr && tr.file_all ? `<a class="link" href="/api/trials/${tr.id}/files/all" title="${esc(tr.file_all)}">XDF</a>` : tr && !tr.saved ? '<span class="muted">not saved</span>' : '—'}</td>
        </tr>`;
    }).join('');
    $('#rep-table').innerHTML = `<thead><tr><th>Step</th><th>Started</th><th>Length</th><th>Good signal</th><th>θ/α ${esc(r.frontal_label || 'frontal')}</th><th>θ/α ${esc(r.parietal_label || '2nd')}</th><th>Δ vs baseline</th><th>VR events</th><th>Notes</th><th>File</th></tr></thead><tbody>${rows}</tbody>`;
}

$('#rep-print').addEventListener('click', () => window.print());
$('#rep-notes').addEventListener('change', async () => {
    if (!report) return;
    try { await api('PUT', `/api/sessions/${report.id}`, { notes: $('#rep-notes').value }); } catch (e) { toast(e.message, true); }
});

$('#rep-finish').addEventListener('click', async () => {
    if (!report) return;
    try {
        const body = { notes: $('#rep-notes').value };
        if (report.status === 'in_progress') {
            if (report.next_step != null && !(await confirmBox('Finish with steps missing?',
                `${report.total_steps - report.completed_steps} step(s) were not recorded. The session will be marked as completed anyway.`, 'Finish session'))) return;
            body.status = 'completed';
        }
        await api('PUT', `/api/sessions/${report.id}`, body);
        toast(body.status ? 'Session completed. Thank you!' : 'Notes saved');
        showView('home');
    } catch (e) { toast(e.message, true); }
});

$('#rep-repeat').addEventListener('click', async () => {
    if (!report) return;
    try {
        if (report.status !== 'in_progress') await api('PUT', `/api/sessions/${report.id}`, { status: 'in_progress' });
        await openSession(report.id, { phase: 'record' });
    } catch (e) { toast(e.message, true); }
});

// ================================================================== start-up
(async function init() {
    try {
        const st = await api('GET', '/api/status');
        if (st.is_recording && st.trial && st.trial.session_id) { await openSession(st.trial.session_id); return; }
    } catch (_) { /* server starting */ }
    loadHome();
})();

// keep the header pills fresh outside a session
setInterval(async () => {
    if (op.view === 'session' || document.hidden) return;
    try {
        const st = await api('GET', '/api/status');
        if (st.connected) {
            setPill('#pill-eeg', 'ok');
            setPill('#pill-vr', st.marker_stream ? 'ok' : null);
        }
        if (op.view === 'home') $('#home-recording').hidden = !st.is_recording;
    } catch (_) { /* ignore */ }
}, 5000);
