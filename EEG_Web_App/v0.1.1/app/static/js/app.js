/* VR EEG Studio — guided session workflow
 *
 * Step 1 Participant → Step 2 Connect streams → Step 3 Preview & record → Step 4 Saved
 * The backend keeps the LSL connection open between trials, so after a trial the
 * experimenter goes straight from step 4 back to step 3 for the next one.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
    view: 'home',
    step: 1,
    maxStep: 1,
    subjects: [],
    subjectId: null,
    trialNumber: null,
    subjectTrialNumbers: [],
    config: { hardware_prop_name: 'name', hardware_prop_value: 'AURA', vr_prop_name: 'name', vr_prop_value: 'VR_Vending_Machine_Markers' },
    connected: false,
    markerStream: null,
    recording: false,
    busy: false,
    lastResult: null,
    eegFound: false,
    labels: [],
    frontal: 0,
    parietal: 1,
    home: null,
};

// ------------------------------------------------------------------ helpers
async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) {
        let msg = data && (data.detail || data.message);
        if (Array.isArray(msg)) msg = msg.map(d => d.msg).join('; ');
        throw new Error(msg || `${res.status} ${res.statusText}`);
    }
    return data;
}

let toastTimer = null;
function toast(msg, kind = 'ok') {
    const el = $('#toast');
    el.textContent = msg;
    el.className = `toast ${kind}`;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, kind === 'error' ? 6000 : 3000);
}

function banner(el, msg, kind = 'warning') {
    if (!msg) { el.hidden = true; return; }
    el.textContent = msg;
    el.className = `banner ${kind}`;
    el.hidden = false;
}

function fmtTime(seconds) {
    const s = Math.max(0, Math.floor(seconds || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const mm = String(m).padStart(2, '0'), ss = String(r).padStart(2, '0');
    return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function fmtDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const locale = { en: 'en-GB', es: 'es-MX', ja: 'ja-JP' }[currentLang] || undefined;
    return d.toLocaleString(locale, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function fmtNum(n) {
    return (n ?? 0).toLocaleString();
}

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function confirmDialog(title, text, { okOnly = false } = {}) {
    return new Promise(resolve => {
        $('#confirm-title').textContent = title;
        $('#confirm-text').textContent = text;
        $('#confirm-no').hidden = okOnly;
        $('#confirm-modal').hidden = false;
        const done = (v) => { $('#confirm-modal').hidden = true; resolve(v); };
        $('#confirm-yes').onclick = () => done(true);
        $('#confirm-no').onclick = () => done(false);
    });
}

function currentSubject() {
    return state.subjects.find(s => s.id === state.subjectId) || null;
}

// --------------------------------------------------------------- top level
function showView(name) {
    state.view = name;
    $$('.view').forEach(v => v.classList.toggle('active', v.id === `view-${name}`));
    $$('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.view === name));
    if (name === 'home') loadHome();
    if (name === 'participants') loadParticipants();
    if (name === 'history') loadHistory();
    if (name === 'session' && state.step === 1) loadSubjects();
}

function setPill(id, level, title) {
    const el = $(id);
    el.classList.remove('ok', 'warn', 'off');
    el.classList.add(level);
    el.title = title;
}

function updatePills(eeg, vr) {
    // eeg / vr: 'ok' | 'found' | 'off'
    const map = { ok: 'ok', found: 'warn', off: 'off' };
    setPill('#pill-eeg', map[eeg], t({ ok: 'pill.eeg_ok', found: 'pill.eeg_found', off: 'pill.eeg_off' }[eeg]));
    setPill('#pill-vr', map[vr], t({ ok: 'pill.vr_ok', found: 'pill.vr_found', off: 'pill.vr_off' }[vr]));
    const dot = { ok: 'ok', found: 'warn', off: 'off' };
    $('#home-eeg-dot').className = `status-dot ${dot[eeg]}`;
    $('#home-vr-dot').className = `status-dot ${dot[vr]}`;
    $('#home-eeg-text').textContent = t({ ok: 'pill.eeg_ok', found: 'pill.eeg_found', off: 'pill.eeg_off' }[eeg]);
    $('#home-vr-text').textContent = t({ ok: 'pill.vr_ok', found: 'pill.vr_found', off: 'pill.vr_off' }[vr]);
}

// ------------------------------------------------------------------ home
async function loadHome() {
    let h;
    try { h = await api('GET', '/api/home'); } catch (e) { return; }
    state.home = h;
    $('#home-n-part').textContent = fmtNum(h.participants);
    $('#home-n-trials').textContent = fmtNum(h.trials);
    $('#home-n-today').textContent = fmtNum(h.trials_today);
    $('#home-folder').textContent = h.data_folder;
    $('#home-back-rec').hidden = !h.is_recording;
    $('#home-new').hidden = h.is_recording;
    const cont = $('#home-continue');
    cont.hidden = !h.last || h.is_recording;
    if (h.last) {
        cont.textContent = t('home.continue', { name: h.last.patient_name || '—', n: h.last.trial_number + 1 });
    }
    banner($('#home-recovered'), h.recovered && h.recovered.length ? t('home.recovered', { n: h.recovered.length }) : '');
    const tb = $('#home-recent');
    tb.innerHTML = '';
    for (const tr of h.recent) {
        const row = document.createElement('tr');
        const file = tr.file_all
            ? `<a class="file-link" href="/api/trials/${tr.id}/files/all">.xdf</a>`
            : (tr.recording_now ? `<span class="tag rec">${escapeHtml(t('hist.recording_now'))}</span>`
                                : `<span class="tag warn">${escapeHtml(t('hist.not_saved'))}</span>`);
        row.innerHTML = `<td>${fmtDate(tr.started_at)}</td><td><strong>${escapeHtml(tr.patient_name || '—')}</strong></td>
            <td>${escapeHtml(t('rec.trial'))} ${tr.trial_number}</td>
            <td>${tr.duration_s != null ? fmtTime(tr.duration_s) : '—'}</td><td class="files-cell">${file}</td>`;
        tb.appendChild(row);
    }
    $('#home-recent-empty').hidden = h.recent.length > 0;
}

async function continueLastParticipant() {
    const last = state.home && state.home.last;
    if (!last) return;
    await loadSubjects(last.patient_id);
    state.subjectId = last.patient_id;
    state.trialNumber = null;
    showView('session');
    state.maxStep = Math.max(state.maxStep, 2);
    await goStep(state.connected ? 3 : 2);
}

// ------------------------------------------------------------------ stepper
function renderStepper() {
    $$('#stepper li').forEach(li => {
        const n = Number(li.dataset.step);
        li.classList.toggle('active', n === state.step);
        li.classList.toggle('done', n < state.step || (n <= state.maxStep && n !== state.step && n < 4));
        li.classList.toggle('clickable', !state.recording && n < state.step);
    });
    $$('.step-panel').forEach(p => { p.hidden = p.id !== `step-${state.step}`; });
}

async function goStep(n) {
    if (state.recording && n !== 3) {
        await confirmDialog(t('rec.leave_title'), t('rec.leave_text'), { okOnly: true });
        return;
    }
    const leaving = state.step;
    state.step = n;
    state.maxStep = Math.max(state.maxStep, n);
    renderStepper();
    window.scrollTo({ top: 0 });

    if (leaving === 3 && n !== 3) stopLiveLoop();
    if (n === 1) loadSubjects();
    if (n === 2) enterStep2();
    if (n === 3) enterStep3();
}

// ------------------------------------------------------------ step 1
async function loadSubjects(selectId) {
    try {
        state.subjects = await api('GET', '/api/subjects');
    } catch (e) {
        toast(e.message, 'error');
        return;
    }
    if (selectId) state.subjectId = selectId;
    if (!state.subjects.some(s => s.id === state.subjectId)) {
        state.subjectId = state.subjects.length ? state.subjects[0].id : null;
    }
    const sel = $('#s1-subject');
    sel.innerHTML = '';
    for (const s of state.subjects) {
        const opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = s.age ? `${s.name} (${s.age})` : s.name;
        sel.appendChild(opt);
    }
    sel.disabled = state.subjects.length === 0;
    if (state.subjectId) sel.value = state.subjectId;
    if (!state.subjects.length) $('#s1-new').open = true;
    renderSubjectInfo();
}

function renderSubjectInfo() {
    const s = currentSubject();
    const el = $('#s1-subject-info');
    if (!state.subjects.length) { el.textContent = t('step1.no_subjects'); return; }
    if (!s) { el.textContent = ''; return; }
    el.textContent = s.trial_count
        ? t('step1.info_trials', { n: s.trial_count, date: fmtDate(s.last_trial_at) })
        : t('step1.info_none');
}

async function createSubjectFromForm(prefix) {
    const body = {
        name: $(`#${prefix}-name`).value.trim(),
        age: parseInt($(`#${prefix}-age`).value) || null,
        gender: $(`#${prefix}-gender`).value || null,
        notes: $(`#${prefix}-notes`).value.trim() || null,
    };
    return body;
}

// ------------------------------------------------------------ step 2
let checkTimer = null;

function readStreamInputs() {
    state.config = {
        hardware_prop_name: $('#s2-hw-prop').value,
        hardware_prop_value: $('#s2-hw-value').value.trim(),
        vr_prop_name: $('#s2-vr-prop').value,
        vr_prop_value: $('#s2-vr-value').value.trim(),
    };
    return state.config;
}

function writeStreamInputs() {
    $('#s2-hw-prop').value = state.config.hardware_prop_name;
    $('#s2-hw-value').value = state.config.hardware_prop_value;
    $('#s2-vr-prop').value = state.config.vr_prop_name;
    $('#s2-vr-value').value = state.config.vr_prop_value;
}

function enterStep2() {
    writeStreamInputs();
    $('#s2-eeg-detail').textContent = t('step2.checking');
    $('#s2-vr-detail').textContent = t('step2.checking');
    scanNetwork();
    checkStreams();
}

async function scanNetwork() {
    const list = $('#s2-discovered');
    list.innerHTML = `<div class="muted">${escapeHtml(t('step2.scanning'))}</div>`;
    try {
        const data = await api('GET', '/api/streams');
        fillDatalist('#hw-values', data.options.hardware_prop_values);
        fillDatalist('#vr-values', data.options.vr_prop_values);
        list.innerHTML = '';
        if (!data.discovered.length) {
            list.innerHTML = `<div class="muted">${escapeHtml(t('step2.none_found'))}</div>`;
            return;
        }
        for (const s of data.discovered) {
            const row = document.createElement('div');
            row.className = 'discover-item';
            const meta = s.kind === 'markers' ? `${s.type} · ${s.channels} ch` : `${s.type} · ${s.channels} ch · ${s.rate} Hz`;
            row.innerHTML = `<span class="kind ${s.kind}">${s.kind === 'markers' ? 'VR' : 'EEG'}</span>
                <span class="d-name">${escapeHtml(s.name)}</span>
                <span class="d-meta">${escapeHtml(meta)}</span>
                <button class="btn small">${escapeHtml(t('step2.use'))}</button>`;
            row.querySelector('button').onclick = () => {
                if (s.kind === 'markers') {
                    $('#s2-vr-prop').value = 'name'; $('#s2-vr-value').value = s.name;
                } else {
                    $('#s2-hw-prop').value = 'name'; $('#s2-hw-value').value = s.name;
                }
                checkStreams();
            };
            list.appendChild(row);
        }
    } catch (e) {
        list.innerHTML = `<div class="error-text">${escapeHtml(e.message)}</div>`;
    }
}

function fillDatalist(id, values) {
    const dl = $(id);
    dl.innerHTML = '';
    for (const v of values || []) {
        const o = document.createElement('option');
        o.value = v;
        dl.appendChild(o);
    }
}

let checking = false;
async function checkStreams() {
    if (checking) return;
    checking = true;
    const cfg = state.step === 2 ? readStreamInputs() : state.config;
    try {
        const q = new URLSearchParams(cfg).toString();
        const r = await api('GET', `/api/streams/check?${q}`);
        state.eegFound = r.eeg.found;
        const eegLevel = r.eeg.found ? (r.connected ? 'ok' : 'found') : 'off';
        const vrLevel = r.vr.found ? (r.connected ? 'ok' : 'found') : 'off';
        if (!state.connected) updatePills(eegLevel, vrLevel);
        if (state.step === 2) {
            $('#s2-eeg-dot').className = `status-dot ${r.eeg.found ? 'ok' : 'off'}`;
            $('#s2-vr-dot').className = `status-dot ${r.vr.found ? 'ok' : 'off'}`;
            $('#card-eeg').classList.toggle('found', r.eeg.found);
            $('#card-vr').classList.toggle('found', r.vr.found);
            $('#s2-eeg-detail').textContent = r.eeg.found
                ? t('step2.eeg_found', { name: r.eeg.name, ch: r.eeg.channels, rate: r.eeg.rate })
                : t('step2.not_found');
            $('#s2-vr-detail').textContent = r.vr.found ? t('step2.vr_found', { name: r.vr.name }) : t('step2.vr_missing');
            $('#s2-next').disabled = !r.eeg.found || state.busy;
        }
    } catch (e) {
        console.error(e);
    } finally {
        checking = false;
    }
}

async function connectAndContinue() {
    readStreamInputs();
    if (!state.eegFound) { toast(t('step2.need_eeg'), 'error'); return; }
    const btn = $('#s2-next');
    btn.disabled = true;
    state.busy = true;
    btn.textContent = t('step2.connecting');
    try {
        const info = await api('POST', '/api/stream/connect', { ...state.config, ...filterPayload() });
        state.connected = true;
        state.markerStream = info.marker_stream;
        applyMontageInfo(info);
        state.trialNumber = null;
        await goStep(3);
    } catch (e) {
        toast(e.message, 'error');
    } finally {
        state.busy = false;
        btn.textContent = t('step2.next');
        btn.disabled = false;
    }
}

// ------------------------------------------------------------ step 3
let liveTimer = null;
let liveInFlight = false;
let eegBuffer = [];
let bufferStartIndex = 0;
let nextIndex = 0;
let srate = 250;
let channelLabels = [];
let liveMarkers = [];
let seenMarkers = new Set();
let plotReady = false;
const WINDOW_SECONDS = 4;

async function enterStep3() {
    const s = currentSubject();
    $('#s3-subject').textContent = s ? s.name : '—';
    $('#s3-eeg').textContent = `${state.config.hardware_prop_value}`;
    $('#s3-vr').textContent = state.markerStream || '—';
    banner($('#s3-banner'), state.markerStream ? '' : t('rec.vr_missing_banner'));

    // existing trial numbers for this participant -> suggest the next one
    try {
        const trials = await api('GET', `/api/trials?subject_id=${state.subjectId}`);
        state.subjectTrialNumbers = trials.map(tr => tr.trial_number);
    } catch (e) {
        state.subjectTrialNumbers = [];
    }
    if (!state.trialNumber) {
        state.trialNumber = (state.subjectTrialNumbers.length ? Math.max(...state.subjectTrialNumbers) : 0) + 1;
    }
    $('#s3-trial').value = state.trialNumber;
    checkTrialNumber();
    try { applyMontageInfo(await api('GET', '/api/montage')); } catch (e) { /* not connected */ }
    renderMetrics(null, []);
    resetPlotBuffer();
    renderRecordUI();
    startLiveLoop();
}

function checkTrialNumber() {
    const n = parseInt($('#s3-trial').value);
    state.trialNumber = n > 0 ? n : 1;
    const next = (state.subjectTrialNumbers.length ? Math.max(...state.subjectTrialNumbers) : 0) + 1;
    const exists = state.subjectTrialNumbers.includes(state.trialNumber);
    banner($('#s3-trial-warning'), exists && !state.recording ? t('rec.trial_exists', { n: state.trialNumber, next }) : '');
}

function resetPlotBuffer() {
    eegBuffer = [];
    bufferStartIndex = 0;
    nextIndex = 0;
    liveMarkers = [];
}

function renderRecordUI() {
    const rec = state.recording;
    const btn = $('#record-btn');
    btn.classList.toggle('recording', rec);
    $('#record-btn-label').textContent = state.busy && !rec ? t('rec.saving') : (rec ? t('rec.stop') : t('rec.start'));
    btn.disabled = state.busy;
    $('#s3-mode').textContent = rec ? t('rec.recording_mode') : t('rec.preview');
    $('#s3-mode').classList.toggle('rec', rec);
    $('#s3-rec-hint').textContent = rec ? t('rec.hint_rec') : t('rec.hint_idle');
    $('#s3-trial').disabled = rec;
    $('#s3-change').disabled = rec;
    $('#s3-montage').disabled = rec;
    $('#m-frontal').disabled = rec;
    $('#m-parietal').disabled = rec;
    $$('.filters-card input, .filters-card select').forEach(el => { el.disabled = rec; });
    $('.filters-card').classList.toggle('locked', rec);
    $('#pill-rec').hidden = !rec;
    $$('.nav-btn').forEach(b => { b.disabled = rec && b.dataset.view !== 'session'; });
    renderStepper();
}

function startLiveLoop() {
    stopLiveLoop();
    liveTimer = setInterval(fetchLive, 100);
}

function stopLiveLoop() {
    if (liveTimer) clearInterval(liveTimer);
    liveTimer = null;
}

async function fetchLive() {
    if (liveInFlight) return;
    liveInFlight = true;
    try {
        const d = await api('GET', `/api/data/live?since=${nextIndex}`);
        srate = d.srate || srate;
        channelLabels = d.channel_labels || channelLabels;
        liveMarkers = d.markers || [];

        updatePills(d.receiving ? 'ok' : (d.connected ? 'found' : 'off'), state.markerStream ? 'ok' : 'off');
        if (d.connected && !d.receiving && nextIndex > 0) {
            banner($('#s3-banner'), t('rec.lost'), 'error');
        } else {
            banner($('#s3-banner'), state.markerStream ? '' : t('rec.vr_missing_banner'));
        }

        $('#s3-time').textContent = fmtTime(state.recording ? d.elapsed : 0);
        $('#pill-rec-time').textContent = fmtTime(d.elapsed);
        $('#s3-samples').textContent = fmtNum(state.recording ? d.record_samples : 0);
        $('#s3-markers').textContent = fmtNum(state.recording ? d.record_markers : 0);
        logNewMarkers(d);
        if (d.channel_labels && d.channel_labels.join('|') !== state.labels.join('|')) {
            state.labels = d.channel_labels;
            fillMetricSelects();
        }
        renderQuality(d.quality || []);
        renderMetrics(d.metrics, d.metrics_history || []);
        banner($('#s3-decoding'), d.analysing ? t('rec.analysing') : (d.decoding ? t('rec.decoding', { rate: d.srate }) : ''), 'notice');

        if (d.samples && d.samples.length) {
            if (d.start_index !== bufferStartIndex + eegBuffer.length) {
                eegBuffer = [];
                bufferStartIndex = d.start_index;
            }
            for (const row of d.samples) eegBuffer.push(row);
            nextIndex = d.next_index;
            const windowSize = Math.round(WINDOW_SECONDS * srate);
            if (eegBuffer.length > windowSize) {
                const drop = eegBuffer.length - windowSize;
                eegBuffer = eegBuffer.slice(drop);
                bufferStartIndex += drop;
            }
            renderPlot();
        }
        $('#s3-noplot').hidden = eegBuffer.length > 0;
    } catch (e) {
        console.error('live data', e);
    } finally {
        liveInFlight = false;
    }
}

function logNewMarkers(d) {
    const log = $('#s3-event-log');
    for (const m of liveMarkers) {
        const key = `${m.time}|${m.label}|${m.kind}`;
        if (seenMarkers.has(key)) continue;
        seenMarkers.add(key);
        if (log.querySelector('.muted')) log.innerHTML = '';
        const li = document.createElement('li');
        li.innerHTML = `<span class="ev-time">${state.recording ? fmtTime(d.elapsed) : '·'}</span><span class="ev-label">${escapeHtml(m.label)}</span>`;
        if (m.kind === 'note') li.classList.add('note');
        if (!state.recording) li.classList.add('preview');
        log.prepend(li);
        while (log.children.length > 50) log.lastChild.remove();
    }
}

function clearEventLog() {
    $('#s3-event-log').innerHTML = `<li class="muted">${escapeHtml(t('rec.no_events'))}</li>`;
}

const TRACE_COLORS = ['#2a6fdb', '#e8590c', '#2b8a3e', '#c2255c', '#7048e8', '#0b7285', '#a67c00', '#495057'];

function renderPlot() {
    if (!eegBuffer.length) return;
    const n_ch = eegBuffer[0].length;
    const x = eegBuffer.map((_, i) => (bufferStartIndex + i) / srate);
    const xMin = x[0], xMax = x[x.length - 1];
    const traces = [];
    const layout = {
        grid: { rows: n_ch, columns: 1, pattern: 'independent', ygap: 0.15 },
        margin: { t: 10, r: 10, b: 30, l: 56 },
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        showlegend: false,
        shapes: [],
        annotations: [],
        font: { size: 11, color: '#5b6573' },
    };
    for (let ch = 0; ch < n_ch; ch++) {
        const sfx = ch === 0 ? '' : `${ch + 1}`;
        const label = channelLabels[ch] || `Ch ${ch + 1}`;
        traces.push({
            x, y: eegBuffer.map(r => r[ch]), type: 'scattergl', mode: 'lines',
            line: { width: 1, color: TRACE_COLORS[ch % TRACE_COLORS.length] },
            name: label, xaxis: `x${sfx}`, yaxis: `y${sfx}`, hoverinfo: 'y+name',
        });
        layout[`xaxis${sfx}`] = { range: [xMin, xMax], showticklabels: ch === n_ch - 1, gridcolor: '#eef1f4', zeroline: false };
        layout[`yaxis${sfx}`] = { title: { text: label, standoff: 4 }, gridcolor: '#eef1f4', zeroline: false, nticks: 3 };
        for (const m of liveMarkers) {
            const mx = m.index / srate;
            if (mx < xMin || mx > xMax) continue;
            const color = m.kind === 'note' ? '#1c7ed6' : '#e8590c';
            layout.shapes.push({ type: 'line', xref: `x${sfx}`, yref: `y${sfx} domain`, x0: mx, x1: mx, y0: 0, y1: 1,
                line: { color, width: m.kind === 'note' ? 1.5 : 1, dash: m.kind === 'note' ? 'dash' : 'dot' } });
            if (ch === 0) {
                layout.annotations.push({ x: mx, y: 1, xref: 'x', yref: 'y domain', yanchor: 'bottom', showarrow: false,
                    text: escapeHtml(m.label), font: { size: 10, color } });
            }
        }
    }
    layout[`xaxis${n_ch === 1 ? '' : n_ch}`].title = { text: 's' };
    const div = $('#plotly-div');
    div.style.height = `${Math.max(360, n_ch * 85)}px`;
    Plotly.react(div, traces, layout, { responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d', 'select2d'] });
    plotReady = true;
}

function filterPayload() {
    const filters = [];
    if ($('#f-notch').checked) filters.push('notch');
    if ($('#f-bp').checked) filters.push('bp');
    if ($('#f-bl').checked) filters.push('bl');
    if ($('#f-asr').checked) filters.push('asr');
    if ($('#f-wvlt').checked) filters.push('wvlt');
    return {
        filters,
        filter_params: {
            notch_freq: parseFloat($('#f-notch-freq').value) || 60,
            locut: parseFloat($('#f-lo').value) || 0.5,
            hicut: parseFloat($('#f-hi').value) || 40,
        },
    };
}

function applyFilterState(filters, params) {
    const f = new Set(filters || []);
    $('#f-notch').checked = f.has('notch');
    $('#f-bp').checked = f.has('bp');
    $('#f-bl').checked = f.has('bl');
    $('#f-asr').checked = f.has('asr');
    $('#f-wvlt').checked = f.has('wvlt');
    if (params) {
        if (params.notch_freq) $('#f-notch-freq').value = String(Math.round(params.notch_freq));
        if (params.locut) $('#f-lo').value = params.locut;
        if (params.hicut) $('#f-hi').value = params.hicut;
    }
}

let filterTimer = null;
function onFilterChange() {
    saveFilterPrefs();
    if (!state.connected || state.recording) return;
    clearTimeout(filterTimer);
    filterTimer = setTimeout(async () => {
        try {
            const r = await api('POST', '/api/stream/filters', filterPayload());
            if (r.filter_params) {
                $('#f-lo').value = +r.filter_params.locut.toFixed(2);
                $('#f-hi').value = +r.filter_params.hicut.toFixed(2);
            }
        } catch (e) {
            toast(e.message, 'error');
        }
    }, 250);
}

function saveFilterPrefs() {
    try { localStorage.setItem('vreeg.filters', JSON.stringify(filterPayload())); } catch (e) { /* ignore */ }
}

function loadFilterPrefs() {
    try {
        const p = JSON.parse(localStorage.getItem('vreeg.filters') || 'null');
        if (p) applyFilterState(p.filters, p.filter_params);
    } catch (e) { /* ignore */ }
}

async function toggleRecording() {
    if (state.busy) return;
    if (!state.recording) {
        checkTrialNumber();
        state.busy = true;
        renderRecordUI();
        try {
            await api('POST', '/api/recording/start', { patient_id: state.subjectId, trial_number: state.trialNumber });
            state.recording = true;
            seenMarkers = new Set(liveMarkers.map(m => `${m.time}|${m.label}|${m.kind}`));
            clearEventLog();
            banner($('#s3-trial-warning'), '');
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            state.busy = false;
            renderRecordUI();
        }
    } else {
        state.busy = true;
        state.recording = false;
        renderRecordUI();
        $('#record-btn-label').textContent = t('rec.saving');
        try {
            const result = await api('POST', '/api/recording/stop');
            state.lastResult = result;
            await goStep(4);
            renderResult();
        } catch (e) {
            toast(e.message, 'error');
            await syncStatus();
        } finally {
            state.busy = false;
            renderRecordUI();
        }
    }
}

// ------------------------------------------------- quality / metrics / notes
function applyMontageInfo(info) {
    if (!info) return;
    state.labels = info.labels || info.channel_labels || state.labels;
    state.deviceLabels = info.device_labels || state.deviceLabels;
    if (info.frontal != null) state.frontal = info.frontal;
    if (info.parietal != null) state.parietal = info.parietal;
    fillMetricSelects();
}

function fillMetricSelects() {
    for (const [id, val] of [['#m-frontal', state.frontal], ['#m-parietal', state.parietal]]) {
        const sel = $(id);
        sel.innerHTML = state.labels.map((l, i) => `<option value="${i}">${escapeHtml(l)}</option>`).join('');
        sel.value = String(val);
    }
}

async function saveMetricChannels() {
    state.frontal = Number($('#m-frontal').value);
    state.parietal = Number($('#m-parietal').value);
    try {
        await api('PUT', '/api/montage', { labels: state.labels, frontal: state.frontal, parietal: state.parietal });
    } catch (e) { toast(e.message, 'error'); }
}

function openMontage() {
    if (state.recording) return;
    fillMontage(state.labels);
    $('#montage-modal').hidden = false;
}

function fillMontage(labels) {
    const grid = $('#montage-grid');
    const n = state.labels.length;
    grid.innerHTML = '';
    for (let i = 0; i < n; i++) {
        const div = document.createElement('div');
        div.className = 'field';
        div.innerHTML = `<label>${i + 1}</label><input class="input" maxlength="24" value="${escapeHtml(labels[i] || `Ch ${i + 1}`)}">`;
        grid.appendChild(div);
    }
}

async function saveMontage(ev) {
    ev.preventDefault();
    const labels = $$('#montage-grid input').map((inp, i) => inp.value.trim() || `Ch ${i + 1}`);
    try {
        const r = await api('PUT', '/api/montage', { labels, frontal: null, parietal: null });
        applyMontageInfo(r);
        $('#montage-modal').hidden = true;
        toast(t('montage.saved'));
    } catch (e) { toast(e.message, 'error'); }
}

const QUALITY_KEY = { good: 'quality.good', warn: 'quality.warn', bad: 'quality.bad', flat: 'quality.flat', unknown: 'quality.unknown' };
function renderQuality(q) {
    const el = $('#s3-quality');
    if (!q.length) { el.innerHTML = ''; return; }
    el.title = t('quality.legend');
    el.innerHTML = q.map((c, i) => {
        const label = state.labels[i] || `Ch ${i + 1}`;
        const tip = t('quality.tip', { label, state: t(QUALITY_KEY[c.level] || 'quality.unknown'),
            amp: c.amp ?? '—', line: c.line != null ? Math.round(c.line * 100) : '—' });
        return `<span class="q-chip q-${c.level}" title="${escapeHtml(tip)}"><span class="dot"></span>${escapeHtml(label)}</span>`;
    }).join('');
}

function bandBars(rel) {
    if (!rel) return '';
    const names = { delta: 'δ', theta: 'θ', alpha: 'α', beta: 'β' };
    return Object.entries(names).map(([k, sym]) => {
        const v = rel[k] || 0;
        return `<div class="band"><span class="band-sym">${sym}</span><span class="band-track"><span class="band-fill band-${k}" style="width:${Math.round(v * 100)}%"></span></span><span class="band-pct">${Math.round(v * 100)}%</span></div>`;
    }).join('');
}

let metricsPlotReady = false;
function renderMetrics(m, history) {
    for (const key of ['frontal', 'parietal']) {
        const val = m && m[key] ? m[key].theta_alpha.toFixed(2) : '—';
        $(`#m-${key}-val`).textContent = val;
        $(`#m-${key}-val`).title = m ? '' : t('metrics.wait');
        $(`#m-${key}-bands`).innerHTML = m && m[key] ? bandBars(m[key].relative) : `<span class="hint">${escapeHtml(t('metrics.wait'))}</span>`;
    }
    const div = $('#metrics-plot');
    if (!history.length) {
        if (metricsPlotReady) { Plotly.purge(div); metricsPlotReady = false; }
        return;
    }
    const x = history.map(h => h[0]);
    const lf = state.labels[state.frontal] || 'F', lp = state.labels[state.parietal] || 'P';
    Plotly.react(div, [
        { x, y: history.map(h => h[1]), name: lf, type: 'scatter', mode: 'lines', line: { color: '#7048e8', width: 2 } },
        { x, y: history.map(h => h[2]), name: lp, type: 'scatter', mode: 'lines', line: { color: '#0b7285', width: 2 } },
    ], {
        height: 150, margin: { t: 6, r: 6, b: 26, l: 34 }, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        showlegend: true, legend: { orientation: 'h', x: 0, y: 1.15, font: { size: 10 } },
        xaxis: { ticksuffix: ' s', gridcolor: '#eef1f4', zeroline: false, tickfont: { size: 10 } },
        yaxis: { gridcolor: '#eef1f4', zeroline: false, tickfont: { size: 10 }, rangemode: 'tozero' },
        font: { color: '#5b6573' },
    }, { displayModeBar: false, responsive: true });
    metricsPlotReady = true;
}

async function addNote(label) {
    try {
        const r = await api('POST', '/api/notes', { label });
        toast(r.saved ? t('notes.added', { label: r.label }) : t('notes.preview'), r.saved ? 'ok' : 'error');
    } catch (e) { toast(e.message, 'error'); }
}

// ------------------------------------------------------------ step 4
function renderResult() {
    const r = state.lastResult;
    if (!r) return;
    const tr = r.trial || {};
    $('#s4-ok').hidden = !r.saved;
    $('#s4-fail').hidden = r.saved;
    $('#s4-subject').textContent = tr.patient_name || '—';
    $('#s4-trial').textContent = tr.trial_number ?? '—';
    $('#s4-duration').textContent = fmtTime(r.duration_s);
    $('#s4-samples').textContent = fmtNum(r.samples);
    $('#s4-markers').textContent = fmtNum(r.markers);
    $('#s4-notes-count').textContent = fmtNum(r.notes);
    $('#s4-filters').textContent = tr.filters || '—';
    $('#s4-notes').value = tr.notes || '';
    if (r.saved) {
        $('#s4-file').textContent = r.file.filename;
        $('#s4-folder').textContent = r.file.folder;
        $('#s4-download').href = `/api/trials/${tr.id}/files/all`;
        $('#s4-export-raw').hidden = false;
    } else {
        $('#s4-error').textContent = r.error || '';
    }
}

async function exportFromResult(kind) {
    const tr = state.lastResult && state.lastResult.trial;
    if (!tr) return;
    try {
        const out = await api('POST', `/api/trials/${tr.id}/export`, { export_type: kind });
        if (kind === 'raw') {
            toast(t('step4.raw_saved', { file: out.filename }));
            $('#s4-export-raw').hidden = true;
        } else {
            state.lastResult.saved = true;
            state.lastResult.file = out;
            renderResult();
        }
    } catch (e) {
        toast(e.message, 'error');
    }
}

let notesTimer = null;
function saveTrialNotes(trialId, notes, silent = false) {
    clearTimeout(notesTimer);
    notesTimer = setTimeout(async () => {
        try {
            await api('PUT', `/api/trials/${trialId}`, { notes });
            if (!silent) toast(t('step4.notes_saved'));
        } catch (e) {
            toast(e.message, 'error');
        }
    }, 400);
}

// ------------------------------------------------------- participants view
let editingSubjectId = null;

async function loadParticipants() {
    try {
        state.subjects = await api('GET', '/api/subjects');
    } catch (e) {
        toast(e.message, 'error');
        return;
    }
    renderParticipants();
}

function renderParticipants() {
    const q = $('#p-search').value.trim().toLowerCase();
    const rows = state.subjects.filter(s => !q || s.name.toLowerCase().includes(q) || (s.notes || '').toLowerCase().includes(q));
    const tb = $('#p-table');
    tb.innerHTML = '';
    const genderKey = { Male: 'subj.male', Female: 'subj.female', Other: 'subj.other' };
    for (const s of rows) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td><strong>${escapeHtml(s.name)}</strong></td>
            <td>${s.age ?? '—'}</td>
            <td>${s.gender ? escapeHtml(t(genderKey[s.gender] || s.gender)) : '—'}</td>
            <td>${s.trial_count}</td>
            <td>${fmtDate(s.last_trial_at)}</td>
            <td class="notes-cell">${escapeHtml(s.notes || '')}</td>
            <td class="row-actions"><button class="btn small ghost">${escapeHtml(t('part.edit_btn'))}</button></td>`;
        tr.querySelector('button').onclick = () => openSubjectModal(s);
        tb.appendChild(tr);
    }
    $('#p-empty').hidden = state.subjects.length > 0;
}

function openSubjectModal(s) {
    editingSubjectId = s ? s.id : null;
    $('#subject-modal-title').textContent = s ? t('part.edit') : t('part.new');
    $('#m-name').value = s ? s.name : '';
    $('#m-age').value = s && s.age != null ? s.age : '';
    $('#m-gender').value = s && s.gender ? s.gender : '';
    $('#m-notes').value = s && s.notes ? s.notes : '';
    $('#subject-modal').hidden = false;
    $('#m-name').focus();
}

async function submitSubjectModal(ev) {
    ev.preventDefault();
    const body = await createSubjectFromForm('m');
    try {
        if (editingSubjectId) await api('PUT', `/api/subjects/${editingSubjectId}`, body);
        else await api('POST', '/api/subjects', body);
        $('#subject-modal').hidden = true;
        toast(t('subj.saved'));
        await loadParticipants();
    } catch (e) {
        toast(e.message, 'error');
    }
}

// ------------------------------------------------------------ history view
async function loadHistory() {
    const sel = $('#h-subject');
    const keep = sel.value;
    try {
        state.subjects = await api('GET', '/api/subjects');
    } catch (e) { /* ignore */ }
    sel.innerHTML = `<option value="">${escapeHtml(t('hist.all'))}</option>` +
        state.subjects.map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
    sel.value = keep;
    let trials = [];
    try {
        trials = await api('GET', `/api/trials${sel.value ? `?subject_id=${sel.value}` : ''}`);
    } catch (e) {
        toast(e.message, 'error');
    }
    const tb = $('#h-table');
    tb.innerHTML = '';
    for (const tr of trials) {
        const row = document.createElement('tr');
        let files = '';
        if (tr.recording_now) files = `<span class="tag rec">${escapeHtml(t('hist.recording_now'))}</span>`;
        else {
            if (tr.file_all) files += `<a class="file-link" href="/api/trials/${tr.id}/files/all">${escapeHtml(t('hist.full'))}</a>`;
            if (tr.file_raw) files += `<a class="file-link" href="/api/trials/${tr.id}/files/raw">${escapeHtml(t('hist.eeg'))}</a>`;
            if (!tr.file_all && !tr.file_raw) {
                files = `<span class="tag warn">${escapeHtml(t('hist.not_saved'))}</span>`;
                if (tr.in_memory) files += ` <button class="btn small" data-save="${tr.id}">${escapeHtml(t('hist.save_now'))}</button>`;
            }
        }
        row.innerHTML = `<td>${fmtDate(tr.started_at)}</td>
            <td>${escapeHtml(tr.patient_name || '—')}</td>
            <td>${tr.kind === 'baseline' ? 'B' : tr.trial_number}</td>
            <td>${tr.duration_s != null ? fmtTime(tr.duration_s) : '—'}</td>
            <td>${tr.n_markers ?? '—'}</td>
            <td class="filters-cell">${escapeHtml(tr.filters || '—')}</td>
            <td class="files-cell">${files}</td>
            <td><input class="input notes-input" value="${escapeHtml(tr.notes || '')}" data-trial="${tr.id}"></td>`;
        tb.appendChild(row);
    }
    tb.querySelectorAll('[data-save]').forEach(b => {
        b.onclick = async () => {
            try {
                await api('POST', `/api/trials/${b.dataset.save}/export`, { export_type: 'all' });
                loadHistory();
            } catch (e) { toast(e.message, 'error'); }
        };
    });
    tb.querySelectorAll('.notes-input').forEach(inp => {
        inp.onchange = () => saveTrialNotes(inp.dataset.trial, inp.value);
    });
    $('#h-empty').hidden = trials.length > 0;
}

// ------------------------------------------------------------ background
async function backgroundCheck() {
    if (state.connected && state.step === 3) return;   // live loop keeps pills fresh
    if (state.connected) {
        try {
            const d = await api('GET', '/api/data/live?since=999999999999');
            updatePills(d.receiving ? 'ok' : 'found', state.markerStream ? 'ok' : 'off');
        } catch (e) { /* ignore */ }
        return;
    }
    checkStreams();
}

async function syncStatus() {
    try {
        const st = await api('GET', '/api/status');
        state.connected = st.connected;
        state.recording = st.is_recording;
        state.markerStream = st.marker_stream;
        if (st.config) state.config = st.config;
        if (st.connected) { applyFilterState(st.filters, st.filter_params); applyMontageInfo({ labels: st.channel_labels, device_labels: st.device_labels, frontal: st.frontal, parietal: st.parietal }); }
        if (st.is_recording && st.trial) {
            // page was reloaded during a recording: go back to it
            state.subjectId = st.trial.patient_id;
            state.trialNumber = st.trial.trial_number;
            await loadSubjects(st.trial.patient_id);
            state.maxStep = 3;
            await goStep(3);
        }
        return st;
    } catch (e) {
        return null;
    }
}

async function loadDefaults() {
    try {
        const d = await api('GET', '/api/streams');
        if (!state.connected && d.last_config) state.config = d.last_config;
        fillDatalist('#hw-values', d.options.hardware_prop_values);
        fillDatalist('#vr-values', d.options.vr_prop_values);
    } catch (e) { /* ignore */ }
}

// ------------------------------------------------------------ wiring
function wire() {
    $$('.nav-btn').forEach(b => b.addEventListener('click', () => showView(b.dataset.view)));
    $('#brand').addEventListener('click', () => { if (!state.recording) showView('home'); });
    $('#home-new').addEventListener('click', async () => {
        state.trialNumber = null;
        showView('session');
        await goStep(1);
    });
    $('#home-continue').addEventListener('click', continueLastParticipant);
    $('#home-back-rec').addEventListener('click', () => { showView('session'); });
    $('#home-streams').addEventListener('click', async () => {
        showView('session');
        if (!state.subjectId) { await goStep(1); return; }
        state.maxStep = Math.max(state.maxStep, 2);
        await goStep(2);
    });
    $('#home-history').addEventListener('click', () => showView('history'));

    // annotations
    $$('[data-note]').forEach(b => b.addEventListener('click', () => addNote(b.textContent)));
    $('#note-form').addEventListener('submit', ev => {
        ev.preventDefault();
        const v = $('#note-input').value.trim();
        if (v) { addNote(v); $('#note-input').value = ''; }
    });
    document.addEventListener('keydown', e => {
        if (e.key.toLowerCase() !== 'n' || e.metaKey || e.ctrlKey || e.altKey) return;
        if (state.view !== 'session' || state.step !== 3) return;
        const tag = (document.activeElement && document.activeElement.tagName) || '';
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
        e.preventDefault();
        $('#note-input').focus();
    });

    // channel names & metric channels
    $('#s3-montage').addEventListener('click', openMontage);
    $('#montage-cancel').addEventListener('click', () => { $('#montage-modal').hidden = true; });
    $('#montage-form').addEventListener('submit', saveMontage);
    $('#montage-preset-1020').addEventListener('click', () => fillMontage(['Fz', 'Cz', 'Pz', 'C3', 'C4', 'P3', 'P4', 'Oz']));
    $('#montage-preset-device').addEventListener('click', () => fillMontage(state.deviceLabels || []));
    $('#m-frontal').addEventListener('change', saveMetricChannels);
    $('#m-parietal').addEventListener('change', saveMetricChannels);
    $('#lang-select').addEventListener('change', e => {
        setLanguage(e.target.value);
        renderStepper();
        renderSubjectInfo();
        if (state.step === 3) renderRecordUI();
        if (state.step === 4) renderResult();
        if (state.view === 'participants') renderParticipants();
        if (state.view === 'history') loadHistory();
        if (state.view === 'home') loadHome();
        checkStreams();
    });
    $('#help-btn').addEventListener('click', () => { $('#help-modal').hidden = false; });
    $('#help-close').addEventListener('click', () => {
        $('#help-modal').hidden = true;
        try { localStorage.setItem('vreeg.helpSeen', '1'); } catch (e) { /* ignore */ }
    });

    $('#stepper').addEventListener('click', e => {
        const li = e.target.closest('li');
        if (!li || !li.classList.contains('clickable')) return;
        goStep(Number(li.dataset.step));
    });
    $$('[data-goto]').forEach(b => b.addEventListener('click', () => goStep(Number(b.dataset.goto))));

    // step 1
    $('#s1-subject').addEventListener('change', e => {
        state.subjectId = Number(e.target.value);
        state.trialNumber = null;
        renderSubjectInfo();
    });
    $('#s1-new-form').addEventListener('submit', async ev => {
        ev.preventDefault();
        const body = await createSubjectFromForm('s1');
        try {
            const s = await api('POST', '/api/subjects', body);
            toast(t('subj.saved'));
            $('#s1-new-form').reset();
            $('#s1-new').open = false;
            state.trialNumber = null;
            await loadSubjects(s.id);
        } catch (e) {
            toast(e.message, 'error');
        }
    });
    $('#s1-next').addEventListener('click', () => {
        if (!state.subjectId) { toast(t('step1.choose'), 'error'); return; }
        goStep(2);
    });

    // step 2
    $('#s2-scan').addEventListener('click', scanNetwork);
    ['#s2-hw-prop', '#s2-hw-value', '#s2-vr-prop', '#s2-vr-value'].forEach(id =>
        $(id).addEventListener('change', checkStreams));
    $('#s2-next').addEventListener('click', connectAndContinue);

    // step 3
    $('#record-btn').addEventListener('click', toggleRecording);
    $('#s3-change').addEventListener('click', () => goStep(2));
    $('#s3-trial').addEventListener('input', checkTrialNumber);
    $$('.filters-card input, .filters-card select').forEach(el => el.addEventListener('change', onFilterChange));

    // step 4
    $('#s4-export-raw').addEventListener('click', () => exportFromResult('raw'));
    $('#s4-retry').addEventListener('click', () => exportFromResult('all'));
    $('#s4-notes').addEventListener('input', e => {
        const tr = state.lastResult && state.lastResult.trial;
        if (tr) saveTrialNotes(tr.id, e.target.value, true);
    });
    $('#s4-notes').addEventListener('change', e => {
        const tr = state.lastResult && state.lastResult.trial;
        if (tr) saveTrialNotes(tr.id, e.target.value);
    });
    $('#s4-next-trial').addEventListener('click', () => {
        const tr = state.lastResult && state.lastResult.trial;
        state.trialNumber = tr ? tr.trial_number + 1 : null;
        goStep(3);
    });
    $('#s4-new-subject').addEventListener('click', () => { state.trialNumber = null; goStep(1); });

    // participants
    $('#p-add').addEventListener('click', () => openSubjectModal(null));
    $('#p-search').addEventListener('input', renderParticipants);
    $('#subject-form').addEventListener('submit', submitSubjectModal);
    $('#m-cancel').addEventListener('click', () => { $('#subject-modal').hidden = true; });

    // history
    $('#h-subject').addEventListener('change', loadHistory);

    // close modals with Escape
    document.addEventListener('keydown', e => {
        if (e.key !== 'Escape') return;
        $('#subject-modal').hidden = true;
        $('#help-modal').hidden = true;
        $('#montage-modal').hidden = true;
    });

    window.addEventListener('beforeunload', e => {
        if (state.recording) { e.preventDefault(); e.returnValue = ''; }
    });
}

window.addEventListener('DOMContentLoaded', async () => {
    currentLang = initialLanguage();
    $('#lang-select').value = currentLang;
    applyTranslations();
    wire();
    loadFilterPrefs();
    updatePills('off', 'off');
    await loadDefaults();
    await syncStatus();
    if (state.recording) {
        showView('session');
    } else {
        await loadSubjects();
        showView('home');
    }
    renderStepper();
    setInterval(backgroundCheck, 3000);
    backgroundCheck();
    let helpSeen = false;
    try { helpSeen = localStorage.getItem('vreeg.helpSeen') === '1'; } catch (e) { /* ignore */ }
    if (!helpSeen) $('#help-modal').hidden = false;
});
