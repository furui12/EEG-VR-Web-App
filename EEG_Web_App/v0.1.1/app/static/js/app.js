let isRecording = false;
let plotInitialized = false;
let updateInterval = null;
let liveRequestInFlight = false;

// Rolling buffer for the sliding-window plot
const WINDOW_SECONDS = 4;
let eegBuffer = [];        // rows of [ch1..chN]
let bufferStartIndex = 0;  // global sample index of eegBuffer[0]
let nextIndex = 0;         // next sample index to request from the server
let srate = 250;
let channelLabels = [];
let liveMarkers = [];

function switchTab(tabId, event) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));

    document.getElementById(`page-${tabId}`).classList.add('active');
    if (event) event.currentTarget.classList.add('active');

    if (tabId === 'setup') {
        loadSetupData();
    } else if (tabId === 'datastream' || tabId === 'export') {
        fetchTrialStatus();
    }
}

function setStatus(text, level = '') {
    const el = document.getElementById('stream-status');
    el.innerText = text || '';
    el.className = 'status-line' + (level ? ` ${level}` : '');
}

async function apiError(res, fallback) {
    try {
        const data = await res.json();
        return data.detail || data.message || fallback;
    } catch (e) {
        return fallback;
    }
}

function initPlotly() {
    if (plotInitialized) return;
    const layout = {
        margin: { t: 30, r: 20, b: 30, l: 60 },
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        xaxis: { title: 'Time (s)' },
        yaxis: { title: 'Ch 1' }
    };
    Plotly.newPlot('plotly-div', [], layout, { responsive: true });
    plotInitialized = true;
}

async function fetchTrialStatus() {
    try {
        const res = await fetch('/api/trials/current');
        const data = await res.json();

        document.getElementById('current-trial-display').innerText = `Trial: ${data.trial.trial_number}`;
        document.getElementById('current-subject-display').innerText = `Subject: ${data.trial.patient_name}`;

        isRecording = data.is_recording;
        updateRecordButtonUI();

        const canExport = !isRecording && data.has_data;
        document.getElementById('export-raw-btn').disabled = !canExport;
        document.getElementById('export-all-btn').disabled = !canExport;
    } catch (err) {
        console.error('Failed to fetch trial status', err);
    }
}

function updateRecordButtonUI() {
    const btn = document.getElementById('record-btn');
    if (isRecording) {
        btn.innerText = 'Stop Recording';
        btn.classList.add('btn-danger');
        if (!updateInterval) {
            updateInterval = setInterval(fetchLiveData, 100);
        }
    } else {
        btn.innerText = 'Start Recording';
        btn.classList.remove('btn-danger');
        if (updateInterval) {
            clearInterval(updateInterval);
            updateInterval = null;
        }
    }
}

function selectedFilters() {
    const ids = { notch: 'chk-notch', bp: 'chk-bp', bl: 'chk-bl', asr: 'chk-asr', wvlt: 'chk-wvlt' };
    return Object.keys(ids).filter(k => document.getElementById(ids[k]).checked);
}

async function toggleRecording() {
    const btn = document.getElementById('record-btn');
    btn.disabled = true;
    try {
        if (!isRecording) {
            setStatus('Looking for LSL streams...');
            const res = await fetch('/api/recording/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filters: selectedFilters() })
            });

            if (res.status === 400) {
                const errData = await res.clone().json().catch(() => ({}));
                if (errData.error === 'unsaved_recording') {
                    setStatus('');
                    document.getElementById('unsaved-modal').style.display = 'flex';
                    return;
                }
            }
            if (!res.ok) {
                setStatus(await apiError(res, 'Could not start recording.'), 'error');
                return;
            }

            const data = await res.json();
            eegBuffer = [];
            bufferStartIndex = 0;
            nextIndex = 0;
            liveMarkers = [];
            const parts = [`Recording ${data.eeg_stream}`];
            if (data.marker_stream) parts.push(`markers: ${data.marker_stream}`);
            const warn = (data.warnings || []).join(' ');
            setStatus(parts.join(' · ') + (warn ? ` — ${warn}` : ''), warn ? 'warning' : '');

            isRecording = true;
            initPlotly();
            updateRecordButtonUI();
            fetchTrialStatus();
        } else {
            const res = await fetch('/api/recording/stop', { method: 'POST' });
            const data = await res.json();
            isRecording = false;
            updateRecordButtonUI();
            setStatus(`Stopped · ${data.samples} samples, ${data.markers} markers in memory. Export the trial to save it.`);
            fetchTrialStatus();
        }
    } finally {
        btn.disabled = false;
    }
}

async function resolveUnsavedRecording(approved) {
    document.getElementById('unsaved-modal').style.display = 'none';
    const res = await fetch('/api/trials/new-approval', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved })
    });
    const data = await res.json();
    if (data.status === 'approved') {
        await fetchTrialStatus();
        toggleRecording();
    }
}

async function fetchLiveData() {
    if (liveRequestInFlight) return;   // never stack requests
    liveRequestInFlight = true;
    try {
        const res = await fetch(`/api/data/live?since=${nextIndex}`);
        const data = await res.json();
        srate = data.srate || srate;
        channelLabels = data.channel_labels || channelLabels;
        liveMarkers = data.markers || [];

        if (!data.is_recording && isRecording) {
            // Recording stopped on the server side
            isRecording = false;
            updateRecordButtonUI();
            fetchTrialStatus();
        }

        if (data.samples && data.samples.length > 0) {
            // Samples are requested by index, so nothing is duplicated. If the server
            // skipped samples (e.g. the tab was in the background), restart the window.
            if (data.start_index !== bufferStartIndex + eegBuffer.length) {
                eegBuffer = [];
                bufferStartIndex = data.start_index;
            }
            for (const row of data.samples) eegBuffer.push(row);
            nextIndex = data.next_index;

            const windowSize = Math.round(WINDOW_SECONDS * srate);
            if (eegBuffer.length > windowSize) {
                const drop = eegBuffer.length - windowSize;
                eegBuffer = eegBuffer.slice(drop);
                bufferStartIndex += drop;
            }
            renderPlot();
        }
    } catch (err) {
        console.error('Error fetching live data', err);
    } finally {
        liveRequestInFlight = false;
    }
}

function renderPlot() {
    if (eegBuffer.length === 0) return;
    const n_ch = eegBuffer[0].length;
    const x = eegBuffer.map((_, i) => (bufferStartIndex + i) / srate);
    const xMin = x[0];
    const xMax = x[x.length - 1];

    const traces = [];
    const layout = {
        grid: { rows: n_ch, columns: 1, pattern: 'independent' },
        margin: { t: 25, r: 20, b: 30, l: 60 },
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        showlegend: false,
        shapes: [],
        annotations: []
    };

    for (let ch = 0; ch < n_ch; ch++) {
        const suffix = ch === 0 ? '' : `${ch + 1}`;
        traces.push({
            x: x,
            y: eegBuffer.map(row => row[ch]),
            type: 'scattergl',
            mode: 'lines',
            line: { width: 1 },
            name: channelLabels[ch] || `Ch ${ch + 1}`,
            xaxis: `x${suffix}`,
            yaxis: `y${suffix}`
        });
        layout[`xaxis${suffix}`] = { range: [xMin, xMax], showticklabels: ch === n_ch - 1 };
        layout[`yaxis${suffix}`] = { title: { text: channelLabels[ch] || `Ch ${ch + 1}` } };

        // VR event markers as vertical lines on every channel
        for (const m of liveMarkers) {
            const mx = m.index / srate;
            if (mx < xMin || mx > xMax) continue;
            layout.shapes.push({
                type: 'line', xref: `x${suffix}`, yref: `y${suffix} domain`,
                x0: mx, x1: mx, y0: 0, y1: 1,
                line: { color: '#e65100', width: 1, dash: 'dot' }
            });
            if (ch === 0) {
                layout.annotations.push({
                    x: mx, y: 1, xref: 'x', yref: 'y domain', yanchor: 'bottom',
                    text: m.label, showarrow: false, font: { size: 10, color: '#e65100' }
                });
            }
        }
    }
    layout[`xaxis${n_ch === 1 ? '' : n_ch}`].title = { text: 'Time (s)' };

    const plotDiv = document.getElementById('plotly-div');
    plotDiv.style.height = `${Math.max(400, n_ch * 140)}px`;
    Plotly.react('plotly-div', traces, layout, { responsive: true });
}

async function submitSubject(event) {
    event.preventDefault();
    const name = document.getElementById('subj-name').value;
    const age = parseInt(document.getElementById('subj-age').value) || null;
    const gender = document.getElementById('subj-gender').value || null;
    const notes = document.getElementById('subj-notes').value || null;

    const res = await fetch('/api/subjects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, age, gender, notes })
    });

    if (res.ok) {
        alert('Subject added successfully!');
        document.getElementById('subject-form').reset();
    } else {
        alert('Error: ' + await apiError(res, 'Could not add subject'));
    }
}

async function loadSetupData() {
    try {
        const [subjRes, streamsRes, trialRes] = await Promise.all([
            fetch('/api/subjects'),
            fetch('/api/streams'),
            fetch('/api/trials/current')
        ]);

        const subjects = await subjRes.json();
        const streamData = await streamsRes.json();
        const trialData = await trialRes.json();

        const subjSelect = document.getElementById('setup-subject');
        subjSelect.innerHTML = '';
        subjects.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.innerText = s.name;
            if (s.id === trialData.trial.patient_id) opt.selected = true;
            subjSelect.appendChild(opt);
        });

        document.getElementById('setup-trial-num').value = trialData.trial.trial_number;

        populateDropdown('setup-hw-name', streamData.options.hardware_prop_names, trialData.trial.hardware_prop_name);
        populateDropdown('setup-hw-value', streamData.options.hardware_prop_values, trialData.trial.hardware_prop_value);
        populateDropdown('setup-vr-name', streamData.options.vr_prop_names, trialData.trial.vr_prop_name);
        populateDropdown('setup-vr-value', streamData.options.vr_prop_values, trialData.trial.vr_prop_value);
    } catch (err) {
        console.error('Error loading setup data', err);
    }
}

function populateDropdown(elementId, items, selectedValue) {
    const select = document.getElementById(elementId);
    select.innerHTML = '';
    const values = [...items];
    if (selectedValue && !values.includes(selectedValue)) values.push(selectedValue);
    values.forEach(item => {
        const opt = document.createElement('option');
        opt.value = item;
        opt.innerText = item;
        if (item === selectedValue) opt.selected = true;
        select.appendChild(opt);
    });
}

function addCustomEntry(type) {
    const val = prompt(`Enter new value for ${type === 'hw' ? 'Hardware Info' : 'VR Event Stream Info'}:`);
    if (val) {
        const select = document.getElementById(type === 'hw' ? 'setup-hw-value' : 'setup-vr-value');
        const opt = document.createElement('option');
        opt.value = val;
        opt.innerText = val;
        opt.selected = true;
        select.appendChild(opt);
    }
}

async function saveConfiguration() {
    const body = {
        patient_id: parseInt(document.getElementById('setup-subject').value),
        trial_number: parseInt(document.getElementById('setup-trial-num').value),
        hardware_prop_name: document.getElementById('setup-hw-name').value,
        hardware_prop_value: document.getElementById('setup-hw-value').value,
        vr_prop_name: document.getElementById('setup-vr-name').value,
        vr_prop_value: document.getElementById('setup-vr-value').value
    };

    const res = await fetch('/api/trials/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });

    if (res.ok) {
        alert('Configuration saved and applied successfully!');
        fetchTrialStatus();
    } else {
        alert('Failed to save configuration: ' + await apiError(res, 'unknown error'));
    }
}

async function exportData(exportType) {
    const statusEl = document.getElementById('export-status');
    statusEl.innerText = 'Exporting XDF file...';

    const res = await fetch('/api/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ export_type: exportType })
    });

    if (res.ok) {
        const data = await res.json();
        statusEl.innerText = `Exported ${data.filename} to the 'data/' folder ` +
            `(${data.streams} stream(s), ${data.samples} EEG samples, ${data.markers} markers).`;
        fetchTrialStatus();
    } else {
        statusEl.innerText = 'Export failed: ' + await apiError(res, 'Unknown error');
    }
}

window.addEventListener('DOMContentLoaded', () => {
    initPlotly();
    fetchTrialStatus();
});
