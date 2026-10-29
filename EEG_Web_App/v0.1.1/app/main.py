import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import (
    DATA_DIR, Experiment, ExperimentSession, SessionLocal, Subject, Trial, init_db, now_iso,
)
from app import settings
from app.models import (
    ConnectRequest, ExperimentIn, ExportRequest, FiltersRequest, MontageRequest, NoteRequest,
    RecordingStartRequest, SessionCreate, SessionUpdate, StreamConfig, SubjectCreate,
    SubjectResponse, TrialNotesUpdate,
)
from app.signal_pipeline import StreamNotFoundError, describe_filters, manager
from app.trial_store import (
    export_trial as export_trial_folder, folder_stats, load_trial, remove_trial, sample_count,
)

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG = StreamConfig().model_dump()
TRIALS_DIR = DATA_DIR / ".trials"          # per-trial files written while recording
TRIALS_DIR.mkdir(parents=True, exist_ok=True)
RECOVERED_NOTE = "Recovered automatically after an interruption."


def trial_folder(trial_id):
    return TRIALS_DIR / f"trial-{trial_id}"


def _file_name(trial, kind):
    stamp = datetime.fromisoformat(trial.started_at).strftime("%Y%m%d-%H%M%S") if trial.started_at else "x"
    ses = f"_ses-{trial.session_id:03d}" if getattr(trial, "session_id", None) else ""
    part = "baseline" if getattr(trial, "kind", None) == "baseline" else f"trial-{trial.trial_number:02d}"
    return f"sub-{trial.patient_id:03d}{ses}_{part}_{stamp}_{kind}.xdf"


def _channels_used(db, trial):
    """Operator sessions analyse the first N channels of the experiment; free trials all of them."""
    if trial.session_id:
        ses = db.query(ExperimentSession).filter(ExperimentSession.id == trial.session_id).first()
        if ses:
            e = db.query(Experiment).filter(Experiment.id == ses.experiment_id).first()
            if e and e.channels_used:
                return e.channels_used
    return 64


def _store_stats(db, trial, folder, line_freq=60.0):
    try:
        trial.stats = json.dumps(folder_stats(folder, _channels_used(db, trial), line_freq))
        db.commit()
    except Exception as exc:          # statistics are a bonus; never block saving
        print(f"stats for trial {trial.id} failed: {exc}")


def recover_interrupted_trials():
    """Trials whose recording was interrupted (crash, power loss, server stopped) still have
    their files in data/.trials/. Turn them into XDF files; drop folders already saved."""
    db = SessionLocal()
    recovered = []
    try:
        for folder in sorted(TRIALS_DIR.glob("trial-*")):
            try:
                tid = int(folder.name.split("-", 1)[1])
            except ValueError:
                continue
            t = db.query(Trial).filter(Trial.id == tid).first()
            if t is None or t.saved:
                remove_trial(folder)
                continue
            if sample_count(folder) == 0:
                remove_trial(folder)
                continue
            try:
                filename = _file_name(t, "all")
                summary = export_trial_folder(folder, "all", str(DATA_DIR / filename))
            except Exception as exc:          # leave the folder for a manual retry
                print(f"Could not recover trial {tid}: {exc}")
                continue
            t.file_name_all = filename
            t.saved = True
            t.duration_s = summary["duration_s"]
            t.n_samples = summary["samples"]
            t.n_markers = summary["markers"]
            t.n_notes = summary["notes"]
            t.notes = f"{RECOVERED_NOTE} {t.notes or ''}".strip()
            db.commit()
            _store_stats(db, t, folder)
            recovered.append(tid)
            remove_trial(folder)
    finally:
        db.close()
    return recovered


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.recovered = recover_interrupted_trials()
    app.state.last_result = None
    stop_flag = threading.Event()
    watcher = threading.Thread(target=_auto_stop_watcher, args=(stop_flag,), daemon=True)
    watcher.start()
    yield
    stop_flag.set()
    manager.disconnect()


app = FastAPI(title="VR EEG Studio", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
@app.get("/operator", response_class=HTMLResponse)
def operator_page(request: Request):
    """Simple view for the person who runs the experiment (default entry point)."""
    return templates.TemplateResponse(request, "operator.html")


@app.get("/researcher", response_class=HTMLResponse)
def researcher_page(request: Request):
    """Advanced view: full control of streams, filters, channels and trials."""
    return templates.TemplateResponse(request, "index.html")


# ------------------------------------------------------------------ helpers
def _subject_out(db, s):
    trials = db.query(Trial).filter(Trial.patient_id == s.id, Trial.recording.is_(True))
    count = trials.count()
    last = trials.order_by(Trial.id.desc()).first()
    max_n = db.query(func.max(Trial.trial_number)).filter(
        Trial.patient_id == s.id, Trial.recording.is_(True)).scalar() or 0
    return SubjectResponse(
        id=s.id, name=s.name, age=s.age, gender=s.gender, notes=s.notes,
        created_at=s.created_at, trial_count=count,
        last_trial_at=last.started_at if last else None,
        next_trial_number=max_n + 1,
    )


def _trial_out(t, subject_name=None):
    return {
        "id": t.id,
        "trial_number": t.trial_number,
        "patient_id": t.patient_id,
        "patient_name": subject_name,
        "started_at": t.started_at,
        "duration_s": t.duration_s,
        "n_samples": t.n_samples,
        "n_markers": t.n_markers,
        "n_notes": t.n_notes,
        "filters": t.filters,
        "notes": t.notes,
        "session_id": t.session_id,
        "kind": t.kind or "task",
        "eeg_stream": f"{t.hardware_prop_name}={t.hardware_prop_value}",
        "vr_stream": f"{t.vr_prop_name}={t.vr_prop_value}",
        "saved": t.saved,
        "file_all": t.file_name_all,
        "file_raw": t.file_name_raw,
        "in_memory": sample_count(trial_folder(t.id)) > 0,
        "recording_now": manager.is_recording and manager.trial_id == t.id,
        "stats": json.loads(t.stats) if t.stats else None,
    }


def _last_config(db):
    last = db.query(Trial).order_by(Trial.id.desc()).first()
    if not last:
        return dict(DEFAULT_CONFIG)
    return {
        "hardware_prop_name": last.hardware_prop_name,
        "hardware_prop_value": last.hardware_prop_value,
        "vr_prop_name": last.vr_prop_name,
        "vr_prop_value": last.vr_prop_value,
    }


def _export(db, trial, export_type):
    filename = _file_name(trial, "all" if export_type == "all" else "eeg")
    if manager.is_recording and manager.trial_id == trial.id:
        raise RuntimeError("Stop the recording before exporting.")
    folder = trial_folder(trial.id)
    if sample_count(folder) == 0:
        raise RuntimeError("The recorded data of this trial is no longer available.")
    summary = export_trial_folder(folder, export_type, str(DATA_DIR / filename))
    if export_type == "all":
        trial.file_name_all = filename
    else:
        trial.file_name_raw = filename
    trial.saved = True
    db.commit()
    return {"filename": filename, "folder": str(DATA_DIR), **summary}


# ------------------------------------------------------------------ subjects
@app.get("/api/subjects", response_model=list[SubjectResponse])
def get_subjects(db: Session = Depends(get_db)):
    return [_subject_out(db, s) for s in db.query(Subject).order_by(Subject.name).all()]


@app.post("/api/subjects", response_model=SubjectResponse)
def create_subject(subj: SubjectCreate, db: Session = Depends(get_db)):
    name = subj.name.strip()
    if db.query(Subject).filter(Subject.name == name).first():
        raise HTTPException(status_code=400, detail="A participant with this name already exists.")
    new_subj = Subject(name=name, age=subj.age, gender=subj.gender or None,
                       notes=subj.notes or None, created_at=now_iso())
    db.add(new_subj)
    db.commit()
    db.refresh(new_subj)
    return _subject_out(db, new_subj)


@app.put("/api/subjects/{subject_id}", response_model=SubjectResponse)
def update_subject(subject_id: int, subj: SubjectCreate, db: Session = Depends(get_db)):
    s = db.query(Subject).filter(Subject.id == subject_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Participant not found.")
    name = subj.name.strip()
    clash = db.query(Subject).filter(Subject.name == name, Subject.id != subject_id).first()
    if clash:
        raise HTTPException(status_code=400, detail="A participant with this name already exists.")
    s.name, s.age, s.gender, s.notes = name, subj.age, subj.gender or None, subj.notes or None
    db.commit()
    db.refresh(s)
    return _subject_out(db, s)


# ------------------------------------------------------------------- streams
@app.get("/api/streams")
def get_lsl_streams(db: Session = Depends(get_db)):
    discovered = manager.discover_streams()
    trials = db.query(Trial).all()

    def uniq(values):
        out = []
        for v in values:
            if v and v not in out:
                out.append(v)
        return out

    hw_values = uniq(["AURA"] + [t.hardware_prop_value for t in trials])
    vr_values = uniq(["VR_Vending_Machine_Markers"] + [t.vr_prop_value for t in trials])
    for s in discovered:
        is_marker = s["type"].lower() in ("markers", "marker", "events", "event") or s["rate"] == 0
        target = vr_values if is_marker else hw_values
        if s["name"] not in target:
            target.append(s["name"])
        s["kind"] = "markers" if is_marker else "signal"

    return {
        "discovered": discovered,
        "options": {"hardware_prop_values": hw_values, "vr_prop_values": vr_values},
        "last_config": _last_config(db),
    }


@app.get("/api/streams/check")
def check_streams(hardware_prop_name: str = "name", hardware_prop_value: str = "AURA",
                  vr_prop_name: str = "name", vr_prop_value: str = "VR_Vending_Machine_Markers"):
    if manager.connected and manager.config == {
        "hardware_prop_name": hardware_prop_name, "hardware_prop_value": hardware_prop_value,
        "vr_prop_name": vr_prop_name, "vr_prop_value": vr_prop_value,
    }:
        last = manager.last_sample_wall
        receiving = bool(last and time.time() - last < 2.0)
        return {
            "eeg": {"found": receiving, "name": manager.eeg_name,
                    "channels": manager.eeg_channels, "rate": manager.eeg_srate},
            "vr": {"found": manager.marker_name is not None, "name": manager.marker_name},
            "connected": True,
        }
    result = manager.check_streams(hardware_prop_name, hardware_prop_value, vr_prop_name, vr_prop_value)
    result["connected"] = False
    return result


@app.post("/api/stream/connect")
def connect_streams(req: ConnectRequest):
    config = StreamConfig(**req.model_dump(include=set(DEFAULT_CONFIG))).model_dump()
    try:
        info = manager.connect(config, req.filters, req.filter_params.model_dump(),
                               montage=settings.get_montage(_montage_key(config)))
    except StreamNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {**info, "filters": manager.filters, "filter_params": manager.filter_params}


def _montage_key(config):
    return f"{config['hardware_prop_name']}={config['hardware_prop_value']}"


@app.get("/api/montage")
def get_montage():
    if not manager.connected:
        raise HTTPException(status_code=400, detail="Streams are not connected.")
    return {"labels": manager.eeg_labels, "device_labels": manager.device_labels,
            "frontal": manager.frontal, "parietal": manager.parietal}


@app.put("/api/montage")
def put_montage(req: MontageRequest):
    if not manager.connected:
        raise HTTPException(status_code=400, detail="Streams are not connected.")
    if len(req.labels) != manager.eeg_channels:
        raise HTTPException(status_code=400, detail=f"Expected {manager.eeg_channels} channel names.")
    try:
        result = manager.set_montage(req.labels, req.frontal, req.parietal)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    settings.set_montage(_montage_key(manager.config), result["labels"], result["frontal"], result["parietal"])
    return result


@app.post("/api/notes")
def add_note(req: NoteRequest):
    if not manager.connected:
        raise HTTPException(status_code=400, detail="Streams are not connected.")
    try:
        return manager.add_note(req.label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/stream/filters")
def set_filters(req: FiltersRequest):
    if not manager.connected:
        raise HTTPException(status_code=400, detail="Streams are not connected.")
    try:
        return manager.set_filters(req.filters, req.filter_params.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/stream/disconnect")
def disconnect_streams():
    if manager.is_recording:
        raise HTTPException(status_code=400, detail="Stop the recording first.")
    manager.disconnect()
    return {"connected": False}


@app.get("/api/status")
def get_status(db: Session = Depends(get_db)):
    trial = None
    if manager.trial_id is not None:
        t = db.query(Trial).filter(Trial.id == manager.trial_id).first()
        if t:
            s = db.query(Subject).filter(Subject.id == t.patient_id).first()
            trial = _trial_out(t, s.name if s else None)
    return {
        **manager.connection_info(),
        "is_recording": manager.is_recording,
        "elapsed": round(manager.elapsed(), 1),
        "filters": manager.filters,
        "filter_params": manager.filter_params,
        "trial": trial,
        "data_folder": str(DATA_DIR),
    }


# ----------------------------------------------------------------- recording
@app.post("/api/recording/start")
def start_recording(req: RecordingStartRequest, db: Session = Depends(get_db)):
    if not manager.connected:
        raise HTTPException(status_code=400, detail="Connect the streams first.")
    if manager.is_recording:
        raise HTTPException(status_code=400, detail="A recording is already running.")
    if not db.query(Subject).filter(Subject.id == req.patient_id).first():
        raise HTTPException(status_code=400, detail="Unknown participant.")

    cfg = manager.config
    trial = Trial(
        trial_number=req.trial_number, patient_id=req.patient_id,
        hardware_prop_name=cfg["hardware_prop_name"], hardware_prop_value=cfg["hardware_prop_value"],
        vr_prop_name=cfg["vr_prop_name"], vr_prop_value=cfg["vr_prop_value"],
        recording=True, saved=False, started_at=now_iso(),
        filters=describe_filters(manager.filters, manager.filter_params),
        session_id=req.session_id, kind=req.kind or "task",
    )
    if req.session_id:
        ses = db.query(ExperimentSession).filter(ExperimentSession.id == req.session_id).first()
        if not ses or ses.patient_id != req.patient_id:
            raise HTTPException(status_code=400, detail="Unknown session for this participant.")
    db.add(trial)
    db.commit()
    db.refresh(trial)

    stamp = datetime.fromisoformat(trial.started_at).strftime("%Y%m%d-%H%M%S")
    lr_file = str(DATA_DIR / f"sub-{trial.patient_id:03d}_trial-{trial.trial_number:02d}_{stamp}_labrecorder.xdf")
    # files of earlier trials that are already saved as XDF are no longer needed
    for folder in TRIALS_DIR.glob("trial-*"):
        try:
            tid = int(folder.name.split("-", 1)[1])
        except ValueError:
            continue
        old = db.query(Trial).filter(Trial.id == tid).first()
        if old is None or old.saved:
            remove_trial(folder)
    try:
        manager.begin_recording(trial.id, trial_folder(trial.id), labrecorder_file=lr_file)
        manager.auto_stop_at = time.time() + req.duration_s if req.duration_s else None
    except RuntimeError as exc:
        db.delete(trial)
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "recording", "trial_id": trial.id, "warnings": list(manager.warnings)}


@app.post("/api/recording/stop")
def stop_recording(db: Session = Depends(get_db)):
    """Stop storing data and save the trial automatically (raw + filtered EEG + markers)."""
    if not manager.is_recording:
        raise HTTPException(status_code=400, detail="No recording is running.")
    return finish_recording(db)


_finish_lock = threading.Lock()


def _auto_stop_watcher(stop_flag):
    """Ends timed trials (operator protocols) even if the browser is closed."""
    while not stop_flag.wait(0.25):
        at = getattr(manager, "auto_stop_at", None)
        if manager.is_recording and at and time.time() >= at:
            db = SessionLocal()
            try:
                finish_recording(db, auto=True)
            except Exception as exc:
                print(f"auto-stop failed: {exc}")
            finally:
                db.close()


def finish_recording(db, auto=False):
    with _finish_lock:
        if not manager.is_recording:
            if app.state.last_result:
                return app.state.last_result
            raise HTTPException(status_code=400, detail="No recording is running.")
        manager.auto_stop_at = None
        summary = manager.end_recording()
        return _finish_saved(db, summary, auto)


def _finish_saved(db, summary, auto):
    trial = db.query(Trial).filter(Trial.id == manager.trial_id).first()
    if trial is None:
        raise HTTPException(status_code=500, detail="Trial not found in the database.")
    trial.duration_s = summary["duration_s"]
    trial.n_samples = summary["samples"]
    trial.n_markers = summary["markers"]
    trial.n_notes = summary["notes"]
    db.commit()

    result = {"trial": None, "saved": False, "file": None, "error": None, **summary}
    if summary["samples"] == 0:
        result["error"] = "No EEG samples were received during the recording."
    else:
        try:
            result["file"] = _export(db, trial, "all")
            result["saved"] = True
            _store_stats(db, trial, trial_folder(trial.id), manager.line_freq)
        except Exception as exc:  # the trial files stay on disk so the user can retry
            result["error"] = str(exc)
    s = db.query(Subject).filter(Subject.id == trial.patient_id).first()
    result["trial"] = _trial_out(trial, s.name if s else None)
    result["auto_stopped"] = auto
    app.state.last_result = result
    return result


@app.get("/api/recording/last")
def last_recording_result():
    """Result of the most recent stop (used when a timed trial ended on its own)."""
    return app.state.last_result or {}


@app.get("/api/home")
def home_summary(db: Session = Depends(get_db)):
    trials = db.query(Trial).filter(Trial.recording.is_(True))
    names = {s.id: s.name for s in db.query(Subject).all()}
    recent = trials.order_by(Trial.id.desc()).limit(6).all()
    today = datetime.now().date().isoformat()
    last = recent[0] if recent else None
    return {
        "participants": len(names),
        "trials": trials.count(),
        "trials_today": trials.filter(Trial.started_at >= today).count(),
        "recent": [_trial_out(t, names.get(t.patient_id)) for t in recent],
        "last": _trial_out(last, names.get(last.patient_id)) if last else None,
        "recovered": getattr(app.state, "recovered", []),
        "data_folder": str(DATA_DIR),
        "is_recording": manager.is_recording,
        "connected": manager.connected,
    }


@app.get("/api/data/live")
def get_live_data(since: int = 0):
    return manager.get_live(since)


# -------------------------------------------------------------------- trials
@app.get("/api/trials")
def list_trials(subject_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(Trial).filter(Trial.recording.is_(True))
    if subject_id:
        q = q.filter(Trial.patient_id == subject_id)
    names = {s.id: s.name for s in db.query(Subject).all()}
    return [_trial_out(t, names.get(t.patient_id)) for t in q.order_by(Trial.id.desc()).all()]


@app.put("/api/trials/{trial_id}")
def update_trial(trial_id: int, req: TrialNotesUpdate, db: Session = Depends(get_db)):
    t = db.query(Trial).filter(Trial.id == trial_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Trial not found.")
    t.notes = req.notes or None
    db.commit()
    return {"status": "ok"}


@app.post("/api/trials/{trial_id}/export")
def export_trial(trial_id: int, req: ExportRequest, db: Session = Depends(get_db)):
    t = db.query(Trial).filter(Trial.id == trial_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Trial not found.")
    try:
        return _export(db, t, req.export_type)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/trials/{trial_id}/files/{kind}")
def download_trial_file(trial_id: int, kind: str, db: Session = Depends(get_db)):
    t = db.query(Trial).filter(Trial.id == trial_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Trial not found.")
    filename = t.file_name_all if kind == "all" else t.file_name_raw
    if not filename or not (DATA_DIR / filename).exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(DATA_DIR / filename, filename=filename, media_type="application/octet-stream")


# ================================================================ operator view
def _experiment_out(e):
    return {
        "id": e.id, "name": e.name, "description": e.description,
        "n_trials": e.n_trials, "trial_duration_s": e.trial_duration_s,
        "baseline_s": e.baseline_s, "baseline_type": e.baseline_type, "rest_s": e.rest_s,
        "checklist": json.loads(e.checklist or "[]"),
        "hardware_prop_name": e.hardware_prop_name, "hardware_prop_value": e.hardware_prop_value,
        "vr_prop_name": e.vr_prop_name, "vr_prop_value": e.vr_prop_value,
        "filters": json.loads(e.filters or "[]"),
        "filter_params": json.loads(e.filter_params or "{}"),
        "filters_text": describe_filters(json.loads(e.filters or "[]"), json.loads(e.filter_params or "{}")),
        "channels_used": e.channels_used or 5,
        "channel_labels": json.loads(e.channel_labels or "[]"),
        "target_participants": e.target_participants,
        "instructions": e.instructions,
        "created_at": e.created_at,
    }


def _apply_experiment(e, req: ExperimentIn):
    e.name, e.description = req.name.strip(), req.description or None
    e.n_trials, e.trial_duration_s = req.n_trials, req.trial_duration_s
    e.baseline_s, e.baseline_type, e.rest_s = req.baseline_s, req.baseline_type, req.rest_s
    e.checklist = json.dumps([c.strip() for c in req.checklist if c.strip()])
    e.hardware_prop_name, e.hardware_prop_value = req.hardware_prop_name, req.hardware_prop_value.strip()
    e.vr_prop_name, e.vr_prop_value = req.vr_prop_name, req.vr_prop_value.strip()
    e.filters = json.dumps(req.filters)
    e.filter_params = json.dumps(req.filter_params.model_dump())
    e.channels_used = req.channels_used
    e.channel_labels = json.dumps([l.strip() for l in req.channel_labels if l.strip()][: req.channels_used])
    e.target_participants = req.target_participants
    e.instructions = (req.instructions or "").strip() or None


@app.get("/api/experiments")
def list_experiments(db: Session = Depends(get_db)):
    q = db.query(Experiment).filter(Experiment.archived.is_(False)).order_by(Experiment.name)
    out = []
    for e in q.all():
        d = _experiment_out(e)
        d["sessions"] = db.query(ExperimentSession).filter(ExperimentSession.experiment_id == e.id).count()
        out.append(d)
    return out


@app.post("/api/experiments")
def create_experiment(req: ExperimentIn, db: Session = Depends(get_db)):
    e = Experiment()
    _apply_experiment(e, req)
    db.add(e)
    db.commit()
    db.refresh(e)
    return _experiment_out(e)


@app.put("/api/experiments/{exp_id}")
def update_experiment(exp_id: int, req: ExperimentIn, db: Session = Depends(get_db)):
    e = db.query(Experiment).filter(Experiment.id == exp_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    _apply_experiment(e, req)
    db.commit()
    return _experiment_out(e)


@app.delete("/api/experiments/{exp_id}")
def archive_experiment(exp_id: int, db: Session = Depends(get_db)):
    e = db.query(Experiment).filter(Experiment.id == exp_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    e.archived = True          # sessions keep pointing to it
    db.commit()
    return {"status": "archived"}


def _session_plan(e):
    steps = []
    if e.baseline_s:
        steps.append({"kind": "baseline", "number": 0, "duration_s": e.baseline_s, "baseline_type": e.baseline_type})
    for n in range(1, e.n_trials + 1):
        steps.append({"kind": "task", "number": n, "duration_s": e.trial_duration_s})
    return steps


def _session_out(db, ses):
    e = db.query(Experiment).filter(Experiment.id == ses.experiment_id).first()
    subj = db.query(Subject).filter(Subject.id == ses.patient_id).first()
    trials = db.query(Trial).filter(Trial.session_id == ses.id, Trial.recording.is_(True)).order_by(Trial.id).all()
    plan = _session_plan(e) if e else []
    for step in plan:
        done = [t for t in trials if (t.kind or "task") == step["kind"] and t.trial_number == step["number"]]
        step["attempts"] = len(done)
        last = done[-1] if done else None
        step["done"] = bool(last and last.saved)
        step["trial"] = _trial_out(last, subj.name if subj else None) if last else None
    next_step = next((i for i, st in enumerate(plan) if not st["done"]), None)
    return {
        "id": ses.id, "status": ses.status, "operator": ses.operator,
        "started_at": ses.started_at, "ended_at": ses.ended_at, "notes": ses.notes,
        "checklist_done": json.loads(ses.checklist_done or "[]"),
        "experiment": _experiment_out(e) if e else None,
        "participant": {"id": subj.id, "name": subj.name} if subj else None,
        "plan": plan,
        "next_step": next_step,
        "completed_steps": sum(1 for st in plan if st["done"]),
        "total_steps": len(plan),
        "recorded_s": round(sum((t.duration_s or 0) for t in trials if t.saved), 1),
        "good_pct": _session_quality(plan),
    }


@app.post("/api/sessions")
def create_session(req: SessionCreate, db: Session = Depends(get_db)):
    if not db.query(Experiment).filter(Experiment.id == req.experiment_id).first():
        raise HTTPException(status_code=404, detail="Experiment not found.")
    if not db.query(Subject).filter(Subject.id == req.patient_id).first():
        raise HTTPException(status_code=404, detail="Participant not found.")
    ses = ExperimentSession(experiment_id=req.experiment_id, patient_id=req.patient_id,
                            operator=(req.operator or "").strip() or None,
                            checklist_done=json.dumps(req.checklist_done), started_at=now_iso())
    db.add(ses)
    db.commit()
    db.refresh(ses)
    return _session_out(db, ses)


@app.get("/api/sessions")
def list_sessions(status: Optional[str] = None, patient_id: Optional[int] = None,
                  limit: int = 50, db: Session = Depends(get_db)):
    q = db.query(ExperimentSession)
    if status:
        q = q.filter(ExperimentSession.status == status)
    if patient_id:
        q = q.filter(ExperimentSession.patient_id == patient_id)
    return [_session_out(db, s) for s in q.order_by(ExperimentSession.id.desc()).limit(limit).all()]


@app.get("/api/sessions/{ses_id}")
def get_session(ses_id: int, db: Session = Depends(get_db)):
    ses = db.query(ExperimentSession).filter(ExperimentSession.id == ses_id).first()
    if not ses:
        raise HTTPException(status_code=404, detail="Session not found.")
    return _session_out(db, ses)


@app.put("/api/sessions/{ses_id}")
def update_session(ses_id: int, req: SessionUpdate, db: Session = Depends(get_db)):
    ses = db.query(ExperimentSession).filter(ExperimentSession.id == ses_id).first()
    if not ses:
        raise HTTPException(status_code=404, detail="Session not found.")
    if req.status:
        if req.status != "in_progress" and manager.is_recording:
            raise HTTPException(status_code=400, detail="Stop the recording first.")
        ses.status = req.status
        ses.ended_at = now_iso() if req.status != "in_progress" else None
    if req.notes is not None:
        ses.notes = req.notes or None
    if req.checklist_done is not None:
        ses.checklist_done = json.dumps(req.checklist_done)
    db.commit()
    return _session_out(db, ses)


@app.post("/api/sessions/{ses_id}/connect")
def connect_session(ses_id: int, db: Session = Depends(get_db)):
    """Connect the streams with the experiment's settings (filters applied automatically)."""
    ses = db.query(ExperimentSession).filter(ExperimentSession.id == ses_id).first()
    if not ses:
        raise HTTPException(status_code=404, detail="Session not found.")
    e = db.query(Experiment).filter(Experiment.id == ses.experiment_id).first()
    config = {"hardware_prop_name": e.hardware_prop_name, "hardware_prop_value": e.hardware_prop_value,
              "vr_prop_name": e.vr_prop_name, "vr_prop_value": e.vr_prop_value}
    filters, params = json.loads(e.filters or "[]"), json.loads(e.filter_params or "{}")
    if manager.is_recording:      # e.g. the page was reloaded during a trial: keep it running
        return {**manager.connection_info(), "filters_text": describe_filters(manager.filters, manager.filter_params),
                "channels_used": e.channels_used or 5}
    try:
        info = manager.connect(config, filters, params, montage=settings.get_montage(_montage_key(config)))
        manager.set_filters(filters, params)       # also when it was already connected
        exp_labels = json.loads(e.channel_labels or "[]")
        if exp_labels:                             # the experiment names its first N channels
            labels = list(manager.eeg_labels)
            labels[: len(exp_labels)] = exp_labels[: len(labels)]
            manager.set_montage(labels, None, None)
            info = manager.connection_info()
    except StreamNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {**info, "filters_text": describe_filters(manager.filters, manager.filter_params),
            "channels_used": e.channels_used or 5}


def _overall_usable(db):
    num = den = 0.0
    for t in db.query(Trial).filter(Trial.saved.is_(True), Trial.stats.isnot(None)).all():
        st = json.loads(t.stats)
        if st.get("good_pct") is not None and st.get("duration_s"):
            num += st["good_pct"] * st["duration_s"]
            den += st["duration_s"]
    return (num / den) if den else None


def _disk_hours(channels=8, srate=250):
    try:
        free = shutil.disk_usage(DATA_DIR).free
    except OSError:
        return None
    per_hour = srate * 3600 * (8 + channels * 8 + channels * 4) * 2   # trial files + XDF
    return round(free / per_hour, 0)


@app.get("/api/operator/summary")
def operator_summary(db: Session = Depends(get_db)):
    today = datetime.now().date().isoformat()
    week_ago = (datetime.now() - timedelta(days=7)).date().isoformat()
    sessions = db.query(ExperimentSession)
    trials = db.query(Trial).filter(Trial.recording.is_(True), Trial.saved.is_(True))
    hours = sum((t.duration_s or 0) for t in trials.all()) / 3600
    by_exp = []
    for e in db.query(Experiment).filter(Experiment.archived.is_(False)).all():
        q = sessions.filter(ExperimentSession.experiment_id == e.id)
        by_exp.append({"id": e.id, "name": e.name, "sessions": q.count(),
                       "completed": q.filter(ExperimentSession.status == "completed").count()})
    return {
        "participants": db.query(Subject).count(),
        "sessions_today": sessions.filter(ExperimentSession.started_at >= today).count(),
        "sessions_week": sessions.filter(ExperimentSession.started_at >= week_ago).count(),
        "completed": sessions.filter(ExperimentSession.status == "completed").count(),
        "in_progress": [_session_out(db, s) for s in sessions.filter(ExperimentSession.status == "in_progress")
                        .order_by(ExperimentSession.id.desc()).limit(5).all()],
        "recent": [_session_out(db, s) for s in sessions.filter(ExperimentSession.status != "in_progress")
                   .order_by(ExperimentSession.id.desc()).limit(8).all()],
        "hours_recorded": round(hours, 2),
        "usable_pct": _overall_usable(db),
        "disk_hours": _disk_hours(),
        "completed_today": sessions.filter(ExperimentSession.status == "completed", ExperimentSession.started_at >= today).count(),
        "by_experiment": by_exp,
        "is_recording": manager.is_recording,
        "recording_trial": manager.trial_id if manager.is_recording else None,
        "data_folder": str(DATA_DIR),
        "recovered": getattr(app.state, "recovered", []),
    }


# ================================================================ reports & statistics
import csv
import io
import shutil
import statistics
import zipfile

from fastapi.responses import Response, StreamingResponse


def _fmt_mmss(seconds):
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


def _step_name(step):
    return "Baseline" if step["kind"] == "baseline" else f"Trial {step['number']}"


def _session_quality(steps):
    """Duration-weighted share of time with good signal over the recorded steps."""
    num = den = 0.0
    for st in steps:
        stats = (st.get("trial") or {}).get("stats") or {}
        if stats.get("good_pct") is not None and stats.get("duration_s"):
            num += stats["good_pct"] * stats["duration_s"]
            den += stats["duration_s"]
    return (num / den) if den else None


def _build_report(db, ses):
    out = _session_out(db, ses)
    e = db.query(Experiment).filter(Experiment.id == ses.experiment_id).first()
    trials = db.query(Trial).filter(Trial.session_id == ses.id, Trial.recording.is_(True)).all()
    steps = out["plan"]
    base = next((st for st in steps if st["kind"] == "baseline" and st.get("trial")), None)
    base_ta = ((base or {}).get("trial") or {}).get("stats", {}) if base else {}
    base_ta = (base_ta or {}).get("theta_alpha", {}) if base_ta else {}
    for st in steps:
        stats = ((st.get("trial") or {}).get("stats")) or {}
        ta = stats.get("theta_alpha") or {}
        st["name"] = _step_name(st)
        st["ta_frontal"] = ta.get("frontal")
        st["ta_parietal"] = ta.get("parietal")
        for key in ("frontal", "parietal"):
            b = base_ta.get(key) if base_ta else None
            v = ta.get(key)
            st[f"delta_{key}"] = (round((v - b) / b * 100, 1) if (v is not None and b) and st["kind"] != "baseline" else None)

    labels, frontal_label, parietal_label, srate = [], None, None, None
    notes_total, markers_total, samples, gaps = {}, 0, 0, 0
    warnings = []
    for st in steps:
        tr = st.get("trial") or {}
        stats = tr.get("stats") or {}
        if not stats:
            continue
        labels = labels or stats.get("channels", [])
        frontal_label = frontal_label or stats.get("frontal_label")
        parietal_label = parietal_label or stats.get("parietal_label")
        markers_total += tr.get("n_markers") or 0
        samples += stats.get("samples", 0)
        gaps += stats.get("gaps", 0)
        if stats.get("samples") and stats.get("duration_s"):
            srate = srate or round(stats["samples"] / max(stats["duration_s"], 1e-9))
        for k, v in (stats.get("notes") or {}).items():
            notes_total[k] = notes_total.get(k, 0) + v
        for label, bad_s in zip(stats.get("channels", []), stats.get("not_good_s", [])):
            if bad_s >= 20:
                warnings.append({"level": "warn", "text": f"{label} was not in good condition for {_fmt_mmss(bad_s)} during the {st['name'].lower()}."})
        n_notes = sum((stats.get("notes") or {}).values())
        if n_notes:
            kinds = ", ".join(f"{v}× {k.lower()}" for k, v in (stats.get("notes") or {}).items())
            warnings.append({"level": "info", "text": f"{n_notes} annotation(s) in {st['name']} ({kinds}): consider excluding ±2 s around them in the analysis."})
        if stats.get("gaps"):
            warnings.append({"level": "warn", "text": f"{stats['gaps']} gap(s) in the EEG data during {st['name']}."})
        if st["kind"] == "task" and (tr.get("n_markers") or 0) == 0 and e and e.vr_prop_value:
            warnings.append({"level": "info", "text": f"No VR events were received in {st['name']}: was the game running?"})
    if not any(w["level"] == "warn" for w in warnings) and samples:
        warnings.append({"level": "ok", "text": f"No data gaps: {samples:,} samples" + (f" at {srate} Hz" if srate else "") + "; EEG and VR events share the same clock."})

    started = datetime.fromisoformat(ses.started_at) if ses.started_at else None
    trial_starts = sorted(t.started_at for t in trials if t.started_at)
    end = datetime.fromisoformat(ses.ended_at) if ses.ended_at else datetime.now()
    out.update({
        "steps": steps,
        "channels": labels,
        "frontal_label": frontal_label,
        "parietal_label": parietal_label,
        "warnings": warnings,
        "totals": {
            "recorded_s": round(sum((t.duration_s or 0) for t in trials if t.saved), 1),
            "session_s": round((end - started).total_seconds(), 0) if started else None,
            "prep_s": round((datetime.fromisoformat(trial_starts[0]) - started).total_seconds(), 0) if (started and trial_starts) else None,
            "good_pct": _session_quality(steps),
            "steps_done": out["completed_steps"],
            "steps_total": out["total_steps"],
            "repeats": max(0, len([t for t in trials if t.saved]) - out["completed_steps"]),
            "markers": markers_total,
            "notes": notes_total,
            "samples": samples,
            "gaps": gaps,
            "srate": srate,
        },
        "data_folder": str(DATA_DIR),
        "files": [st["trial"]["file_all"] for st in steps if st.get("trial") and st["trial"].get("file_all")],
    })
    return out


@app.get("/api/sessions/{ses_id}/report")
def session_report(ses_id: int, db: Session = Depends(get_db)):
    ses = db.query(ExperimentSession).filter(ExperimentSession.id == ses_id).first()
    if not ses:
        raise HTTPException(status_code=404, detail="Session not found.")
    return _build_report(db, ses)


CSV_COLUMNS = ["participant", "session_id", "experiment", "session_status", "step", "kind", "trial_number",
               "started_at", "duration_s", "good_signal_pct", "theta_alpha_frontal", "theta_alpha_parietal",
               "delta_frontal_pct", "delta_parietal_pct", "vr_events", "annotations", "file"]


def _report_rows(rep):
    rows = []
    for st in rep["steps"]:
        tr = st.get("trial") or {}
        stats = tr.get("stats") or {}
        rows.append({
            "participant": (rep.get("participant") or {}).get("name"),
            "session_id": rep["id"],
            "experiment": (rep.get("experiment") or {}).get("name"),
            "session_status": rep["status"],
            "step": st["name"], "kind": st["kind"], "trial_number": st["number"],
            "started_at": tr.get("started_at"), "duration_s": tr.get("duration_s"),
            "good_signal_pct": None if stats.get("good_pct") is None else round(stats["good_pct"] * 100, 1),
            "theta_alpha_frontal": st.get("ta_frontal"), "theta_alpha_parietal": st.get("ta_parietal"),
            "delta_frontal_pct": st.get("delta_frontal"), "delta_parietal_pct": st.get("delta_parietal"),
            "vr_events": tr.get("n_markers"), "annotations": tr.get("n_notes"), "file": tr.get("file_all"),
        })
    return rows


def _csv_response(rows, filename):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/sessions/{ses_id}/metrics.csv")
def session_csv(ses_id: int, db: Session = Depends(get_db)):
    ses = db.query(ExperimentSession).filter(ExperimentSession.id == ses_id).first()
    if not ses:
        raise HTTPException(status_code=404, detail="Session not found.")
    rep = _build_report(db, ses)
    return _csv_response(_report_rows(rep), f"session-{ses_id:03d}_metrics.csv")


@app.get("/api/sessions/{ses_id}/files.zip")
def session_zip(ses_id: int, db: Session = Depends(get_db)):
    ses = db.query(ExperimentSession).filter(ExperimentSession.id == ses_id).first()
    if not ses:
        raise HTTPException(status_code=404, detail="Session not found.")
    files = [t.file_name_all for t in db.query(Trial).filter(Trial.session_id == ses_id, Trial.saved.is_(True)).all()
             if t.file_name_all and (DATA_DIR / t.file_name_all).exists()]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(DATA_DIR / f, arcname=f)
        z.writestr(f"session-{ses_id:03d}_metrics.csv", _report_csv_text(_build_report(db, ses)))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="session-{ses_id:03d}.zip"'})


def _report_csv_text(rep):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    w.writeheader()
    for r in _report_rows(rep):
        w.writerow(r)
    return buf.getvalue()


@app.get("/api/experiments/{exp_id}/dataset.csv")
def experiment_csv(exp_id: int, db: Session = Depends(get_db)):
    sessions = db.query(ExperimentSession).filter(ExperimentSession.experiment_id == exp_id).order_by(ExperimentSession.id).all()
    rows = []
    for ses in sessions:
        rows.extend(_report_rows(_build_report(db, ses)))
    return _csv_response(rows, f"experiment-{exp_id:03d}_dataset.csv")


@app.get("/api/stats")
def lab_stats(experiment_id: Optional[int] = None, weeks: int = 8, db: Session = Depends(get_db)):
    q = db.query(ExperimentSession)
    e = None
    if experiment_id:
        q = q.filter(ExperimentSession.experiment_id == experiment_id)
        e = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    sessions = q.order_by(ExperimentSession.id).all()
    reports = [_build_report(db, s) for s in sessions]
    completed = [r for r in reports if r["status"] == "completed"]
    finished = [r for r in reports if r["status"] != "in_progress"]
    done_participants = {r["participant"]["id"] for r in completed if r.get("participant")}

    # usable data and electrode quality over every recorded step
    num = den = 0.0
    by_label = {}
    for r in reports:
        for st in r["steps"]:
            stats = (st.get("trial") or {}).get("stats") or {}
            if stats.get("good_pct") is not None and stats.get("duration_s"):
                num += stats["good_pct"] * stats["duration_s"]
                den += stats["duration_s"]
            for label, g in zip(stats.get("channels", []), stats.get("good_by_channel", [])):
                if g is not None:
                    by_label.setdefault(label, []).append(g)
    channels = [{"label": k, "good_pct": round(sum(v) / len(v), 3)} for k, v in by_label.items()]
    preps = [r["totals"]["prep_s"] for r in reports if r["totals"].get("prep_s") is not None]

    # sessions per ISO week
    today = datetime.now().date()
    this_monday = today - timedelta(days=today.weekday())
    week_rows = []
    for i in range(weeks - 1, -1, -1):
        start = this_monday - timedelta(weeks=i)
        end = start + timedelta(days=7)
        n = sum(1 for s in sessions if s.started_at and start.isoformat() <= s.started_at[:10] < end.isoformat())
        week_rows.append({"label": f"W{start.isocalendar()[1]}", "n": n, "current": i == 0})

    # theta/alpha (frontal) per step, latest completed session per participant
    latest = {}
    for r in completed:
        if r.get("participant"):
            latest[r["participant"]["id"]] = r
    plan_names = [_step_name(st) for st in (_session_plan(e) if e else (completed[-1]["plan"] if completed else []))]
    dist = {name: [] for name in plan_names}
    up_count = habituation = 0
    for r in latest.values():
        vals = {st["name"]: st.get("ta_frontal") for st in r["steps"]}
        for name in plan_names:
            if vals.get(name) is not None:
                dist[name].append(vals[name])
        task_vals = [vals[n] for n in plan_names if n != "Baseline" and vals.get(n) is not None]
        if vals.get("Baseline") is not None and task_vals and max(task_vals) > vals["Baseline"]:
            up_count += 1
        if len(task_vals) >= 2 and task_vals[-1] < max(task_vals[:-1]):
            habituation += 1
    distribution = [{"step": name, "values": v, "median": (statistics.median(v) if v else None)} for name, v in dist.items()]
    recent_completed = sum(1 for r in completed if r.get("ended_at") and r["ended_at"][:10] >= (today - timedelta(days=14)).isoformat())
    target = e.target_participants if e else None
    return {
        "experiment": _experiment_out(e) if e else None,
        "participants_done": len(done_participants),
        "target": target,
        "sessions": len(reports),
        "completed": len(completed),
        "finished": len(finished),
        "completion_pct": (len(completed) / len(finished)) if finished else None,
        "aborted": sum(1 for r in reports if r["status"] == "aborted"),
        "usable_pct": (num / den) if den else None,
        "prep_median_s": statistics.median(preps) if preps else None,
        "weeks": week_rows,
        "channels": channels,
        "distribution": distribution,
        "quick": {
            "participants_with_data": len(latest),
            "load_up": up_count,
            "habituation": habituation,
            "remaining": (max(0, target - len(done_participants)) if target else None),
            "per_week": round(recent_completed / 2, 1),
        },
    }
