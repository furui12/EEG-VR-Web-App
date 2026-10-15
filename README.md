# EEG-VR-Web-App
A local web app to visualize and pre-process EEG data in real time streamed through
[LSL](https://labstreaminglayer.org/), synchronized with stimulus/event markers published
over LSL by VR neuroadaptive programs. It also manages subject and trial metadata and exports
each trial to XDF.

## Features
- **Data Stream** – live multi-channel plot (last 4 s) with VR markers drawn as vertical lines.
  Optional real-time filters: notch (60 Hz), band-pass (0.5–80 Hz), running-mean baseline
  correction, robust amplitude clipping (ASR-*inspired*, not full ASR) and wavelet denoising
  (db4, sliding window). The filtered signal is re-published as an LSL stream `<name>_filtered`.
- **Subjects** – add participants (stored in a local SQLite database).
- **Experimental Setup** – choose subject, trial number, EEG stream and VR marker stream
  (matched by LSL `name`, `source_id` or `type`).
- **Export Data** – writes the recorded trial to `data/` as XDF:
  - *Export EEG Data* → raw EEG only
  - *Export All Data* → raw EEG + filtered EEG + VR markers

  All streams use LSL timestamps with clock synchronization, so EEG and markers are aligned.
  Files open with `pyxdf`, EEGLAB (xdfimport) or MNE.

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
Then open <http://localhost:8000>.

### Testing without hardware
In a second terminal (same virtual environment), publish fake EEG + VR marker streams:
```bash
python tools/mock_lsl_streams.py
```
It creates `AURA` (8 channels, 250 Hz) and `VR_Kitchen_Markers`, which match the app defaults.

## Typical workflow
1. **Subjects** → add the participant.
2. **Experimental Setup** → select subject, trial number and streams → *Save & Apply Configuration*.
3. **Data Stream** → tick the filters → *Start Recording* … *Stop Recording*.
4. **Export Data** → *Export EEG Data* and/or *Export All Data*. Files go to `data/`
   (`trial_<n>_subject_<id>_<raw|all>.xdf`).
5. The next *Start Recording* creates the next trial automatically. If you start a new recording
   without exporting, the app warns you that the previous data will be discarded.

> Recorded samples are kept in memory until exported. Restarting the server before exporting
> loses that trial.

### Optional: LabRecorder backup
If the environment variable `LABRECORDER_CLI_PATH` points to `LabRecorderCLI`, the app also
starts it for the whole duration of each recording and writes
`data/trial_<n>_subject_<id>_labrecorder.xdf` as an independent backup.

## Data & privacy
`data/` contains the SQLite database (`app.db`) and the recordings, i.e. participant data.
It is excluded from git via `.gitignore`.
