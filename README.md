# EEG-VR-Web-App
A local web app to visualize and pre-process EEG data in real time streamed through
[LSL](https://labstreaminglayer.org/), synchronized with stimulus/event markers published
over LSL by VR neuroadaptive programs. It also manages subject and trial metadata and exports
each trial to XDF.

## Two views
| URL | For | What it does |
|---|---|---|
| <http://localhost:8000/> | **Operator** (person who runs the experiment) | Dashboard (participants, sessions, statistics), experiments (protocols), and a guided session: checklist → devices → signal quality → baseline and trials with one button. Streams, filters and timing come from the experiment, nothing to configure. |
| <http://localhost:8000/researcher> | **Researcher** (advanced) | Everything described below: streams, filters, channel names, live metrics, free trials, history. |

Both views share the same database and files and link to each other from the top bar.

### Operator view (English)
Designed for the person who applies the experiment: four sections in the top bar.
- **Home** – greeting, *New session* / *Continue* buttons, a "Before you start" check
  (EEG headset, VR game, free disk space), today/week counters, usable-data %, recent sessions
  and the experiments with a *Start session* button.
- **Participants** – searchable list; each participant shows their sessions (continue or open the report).
- **Experiments** – two built-in protocols, both editable:
  - **VR Vending Machine** – 1 min eyes-open baseline, 3 × 5 min trials, 30 s rest
    (VR markers `VR_Vending_Machine_Markers`).
  - **VR Relaxation** – 2 min eyes-closed baseline, 2 × 5 min trials, 60 s rest
    (VR markers `VR_Relaxation_Markers`).

  Each experiment defines baseline, trials, length, rest, the instruction read to the participant,
  the checklist, the target number of participants and, under *Advanced settings*, the streams,
  the filters (applied automatically) and the **channels analysed**.
- **Channels analysed** – the platform uses the **first 5 channels of the LSL EEG array**, the
  frontal electrodes (default names `Fp1, Fp2, F3, Fz, F4`, editable per experiment). The head map,
  the quality checks and the report only show those channels; every channel is still saved in the XDF.
- **Session**: Participant → **Prepare** (head map of the 5 frontal electrodes in green / yellow /
  orange / grey with step-by-step fixing guidance, checklist, devices) → **Record** (timeline of the
  steps, large timer, rest countdown, *Repeat*, instruction for the participant, red recording strip,
  annotation buttons with keys `1`–`4` and `N` for a free note; timed steps stop and save by
  themselves, also if the browser is closed or reloaded) → **Report**.
- **Session report** – steps recorded, usable data, θ/α (cognitive-load index) per step against the
  baseline, a plain-language summary, data-quality notes, a 30-s quality map per electrode, a table
  per step, and downloads: print / PDF, metrics CSV and a ZIP with every XDF.
- **Reports (study progress)** – recruitment vs. target, completed sessions, usable data, median
  preparation time, sessions per week, quality per electrode, the distribution of frontal θ/α per step
  across participants and an automatic quick read; *Export dataset (CSV)* gives one row per step and
  session for R / Python / SPSS.
- Files are named `sub-<id>_ses-<id>_<baseline|trial-NN>_<date-time>_all.xdf`.

## Features
**VR EEG Studio** (UI in English, Español and 日本語 — selector in the top bar).

- **Home** – start a new session or continue with the last participant, live status of the
  EEG and VR streams, recent trials with download links, and counters.
- **Guided session in four steps**
  1. **Participant** – pick or register who takes part.
  2. **Connect streams** – the EEG stream (default `AURA`) and the VR marker stream
     (default `VR_Vending_Machine_Markers`) turn green when found; streams on the network can be
     selected with one click.
  3. **Signal & record**
     - live multi-channel preview with VR events (orange) and annotations (blue);
     - **signal quality per channel** (good / check / poor / no signal) based on amplitude and
       power-line noise, so electrodes can be fixed before recording;
     - **channel names** (10-20 positions) editable and remembered per device;
     - real-time filters to try in the preview: notch 50/60 Hz, band-pass, baseline correction,
       robust clipping (ASR-*inspired*), wavelet denoising;
     - **annotations** by the experimenter (quick buttons, free text, shortcut `N`);
     - **cognitive load index**: theta/alpha ratio at a frontal and a parietal channel
       (default Fz / Pz), relative band powers and the trend during the trial. The metrics are
       saved and also **published live as the LSL stream `VREEG_Metrics`**, so a VR game can
       adapt to them (neuroadaptive loop).
  4. **Saved** – the trial is saved automatically as XDF when the recording stops.
- **Safe recording** – samples are written to disk while recording (`data/.trials/`). If the
  app or the computer stops unexpectedly, the trial is recovered into an XDF file at the next start.
- **Participants** and **History** pages (search, edit, notes, downloads).

Each XDF file (`all`) contains: raw EEG, filtered EEG, VR markers, `Experimenter_Annotations`
and `VREEG_Metrics`, all on the same LSL clock. Files open with `pyxdf`, EEGLAB or MNE.

### AURA / AuraLSL note
AuraLSL v1.1 pushes its whole 9x8 internal buffer on every update instead of one sample at a
time, so a normal LSL client receives ~9x more, scrambled samples. The app detects this format
automatically and reconstructs the real 250 Hz signal (`app/aura_decoder.py`); the XDF header of
the EEG stream records it under `desc/decoding`. Recordings made **before** this fix can be repaired:
```bash
pip install pyxdf
python tools/repair_aura_xdf.py data/<file>.xdf      # writes <file>_repaired.xdf
```

## Requirements
- Python 3.10+
- liblsl. Recent `pylsl` wheels already include it on Windows/macOS/Linux x86_64. If you get
  *"LSL binary library file was not found"*, install it:
  - macOS: `brew install labstreaminglayer/tap/lsl`
  - conda (any OS): `conda install -c conda-forge liblsl`
  - or download it from <https://github.com/sccn/liblsl/releases>

## Installation
```bash
git clone <repo-url>
cd EEG-VR-Web-App/EEG_Web_App/v0.1.1
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --upgrade -r requirements.txt
```

## Running
From the `v0.1.1` folder (the one that contains `app/`):
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
Then open <http://localhost:8000> (operator) or <http://localhost:8000/researcher> (advanced).

### Testing without hardware
In a second terminal (same virtual environment), publish simulated EEG + VR marker streams:
```bash
python tools/mock_lsl_streams.py
```
It creates `AURA_SIM` (8 channels, 250 Hz) and `VR_SIM_Markers`. Add `--aura-ring` to emulate
the AuraLSL output format. In step 2 of the app click
*Use* next to each of them. The names differ from the real `AURA` / `VR_Vending_Machine_Markers` on
purpose, so a simulated stream is never recorded by mistake. Stop it with Ctrl+C before
working with the real device.

## Typical workflow
Open <http://localhost:8000> and follow the steps on screen (the **?** button explains them).
Files are written to `data/` as
`sub-<participant id>_trial-<n>_<date-time>_<all|eeg>.xdf`.

> The recorded samples are kept in memory until the trial is saved (automatically when you press
> *Stop & save*). If saving fails, the app keeps the data and offers to retry.

### Optional: LabRecorder backup
If the environment variable `LABRECORDER_CLI_PATH` points to `LabRecorderCLI`, the app also
starts it for the whole duration of each recording and writes
`data/sub-<id>_trial-<n>_<date-time>_labrecorder.xdf` as an independent backup.

## Data & privacy
`data/` contains the SQLite database (`app.db`) and the recordings, i.e. participant data.
It is excluded from git via `.gitignore`.
