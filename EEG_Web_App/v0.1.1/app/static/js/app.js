let isRecording = false;
let plotInitialized = false;
let updateInterval = null;

function switchTab(tabId, event) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    
    document.getElementById(`page-${tabId}`).classList.add('active');
    if (event) event.currentTarget.classList.add('active');

    if (tabId === 'setup') {
        loadSetupData();
    } else if (tabId === 'datastream') {
        fetchTrialStatus();
    }
}

function initPlotly() {
    if (plotInitialized) return;
    const layout = {
        title: 'Real-Time Raw EEG Channels',
        margin: { t: 40, r: 20, b: 40, l: 50 },
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        xaxis: { title: 'Sample Index' },
        yaxis: { title: 'Amplitude' }
    };
    Plotly.newPlot('plotly-div', [], layout, {responsive: true});
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

        const exportRawBtn = document.getElementById('export-raw-btn');
        const exportAllBtn = document.getElementById('export-all-btn');
        if (!isRecording) {
            exportRawBtn.disabled = false;
            exportAllBtn.disabled = false;
        } else {
            exportRawBtn.disabled = true;
            exportAllBtn.disabled = true;
        }
    } catch (err) {
        console.error("Failed to fetch trial status", err);
    }
}

function updateRecordButtonUI() {
    const btn = document.getElementById('record-btn');
    if (isRecording) {
        btn.innerText = "Stop Recording";
        btn.classList.add("btn-danger");
        if (!updateInterval) {
            updateInterval = setInterval(fetchLiveData, 200);
        }
    } else {
        btn.innerText = "Start Recording";
        btn.classList.remove("btn-danger");
        if (updateInterval) {
            clearInterval(updateInterval);
            updateInterval = null;
        }
    }
}

async function toggleRecording() {
    if (!isRecording) {
        const filters = [];
        if (document.getElementById('chk-notch').checked) filters.push('notch');
        if (document.getElementById('chk-bp').checked) filters.push('bp');
        if (document.getElementById('chk-bl').checked) filters.push('bl');
        if (document.getElementById('chk-asr').checked) filters.push('asr');
        if (document.getElementById('chk-wvlt').checked) filters.push('wvlt');

        const res = await fetch('/api/recording/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ filters })
        });

        if (res.status === 400) {
            const errData = await res.json();
            if (errData.error === 'unsaved_recording') {
                document.getElementById('unsaved-modal').style.display = 'flex';
                return;
            }
        }
        
        if (res.ok) {
            isRecording = true;
            updateRecordButtonUI();
            initPlotly();
        }
    } else {
        await fetch('/api/recording/stop', { method: 'POST' });
        isRecording = false;
        updateRecordButtonUI();
        fetchTrialStatus();
    }
}

async function resolveUnsavedRecording(approved) {
    document.getElementById('unsaved-modal').style.display = 'none';
    const res = await fetch('/api/trials/new-approval', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ approved })
    });
    const data = await res.json();
    if (data.status === 'approved') {
        toggleRecording();
    }
}

async function fetchLiveData() {
    try {
        const res = await fetch('/api/data/live');
        const data = await res.json();
        if (data.chunk && data.chunk.length > 0) {
            const chunk = data.chunk;
            const n_ch = chunk[0].length;
            const traces = [];
            for (let ch = 0; ch < n_ch; ch++) {
                const y = chunk.map(row => row[ch]);
                const x = Array.from({length: y.length}, (_, i) => i);
                traces.push({
                    y: y,
                    x: x,
                    type: 'scatter',
                    mode: 'lines',
                    name: `Ch ${ch+1}`
                });
            }
            Plotly.react('plotly-div', traces, {
                title: 'Real-Time Processed EEG Data',
                margin: { t: 40, r: 20, b: 40, l: 50 },
                paper_bgcolor: 'transparent',
                plot_bgcolor: 'transparent'
            });
        }
    } catch (err) {
        console.error("Error fetching live data", err);
    }
}

async function submitSubject(event) {
    event.preventDefault();
    const name = document.getElementById('subj-name').value;
    const age = parseInt(document.getElementById('subj-age').value) || null;
    const gender = document.getElementById('subj-gender').value;
    const notes = document.getElementById('subj-notes').value;

    const res = await fetch('/api/subjects', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ name, age, gender, notes })
    });

    if (res.ok) {
        alert("Subject added successfully!");
        document.getElementById('subject-form').reset();
    } else {
        const err = await res.json();
        alert("Error: " + (err.detail || "Could not add subject"));
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
        console.error("Error loading setup data", err);
    }
}

function populateDropdown(elementId, items, selectedValue) {
    const select = document.getElementById(elementId);
    select.innerHTML = '';
    items.forEach(item => {
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
    const patient_id = parseInt(document.getElementById('setup-subject').value);
    const trial_number = parseInt(document.getElementById('setup-trial-num').value);
    const hardware_prop_name = document.getElementById('setup-hw-name').value;
    const hardware_prop_value = document.getElementById('setup-hw-value').value;
    const vr_prop_name = document.getElementById('setup-vr-name').value;
    const vr_prop_value = document.getElementById('setup-vr-value').value;

    const res = await fetch('/api/trials/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ trial_number, patient_id, hardware_prop_name, hardware_prop_value, vr_prop_name, vr_prop_value })
    });

    if (res.ok) {
        alert("Configuration saved and applied successfully!");
        fetchTrialStatus();
    } else {
        alert("Failed to save configuration.");
    }
}

async function exportData(exportType) {
    const statusEl = document.getElementById('export-status');
    statusEl.innerText = "Exporting XDF file...";

    const res = await fetch('/api/export', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ export_type: exportType })
    });

    if (res.ok) {
        const data = await res.json();
        statusEl.innerText = `Successfully exported to ${data.filename} in 'data/' directory.`;
    } else {
        const err = await res.json();
        statusEl.innerText = "Export failed: " + (err.detail || "Unknown error");
    }
}

window.addEventListener('DOMContentLoaded', () => {
    initPlotly();
    fetchTrialStatus();
});